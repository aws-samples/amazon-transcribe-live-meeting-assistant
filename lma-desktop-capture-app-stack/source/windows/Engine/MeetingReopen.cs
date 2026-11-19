using System.Net.Http;
using System.Text;
using System.Text.Json;

namespace LMA;

/// <summary>
/// "Continue an existing meeting…": capture into a meeting that already exists,
/// even one shown as Done in LMA, on the same transcript timeline.
///
/// Before streaming, the app asks LMA's AppSync API to reopen the meeting (the
/// `reopenMeeting` mutation). The server checks that the signed-in user is the
/// meeting's owner or an Admin, that the meeting is not still being captured
/// and that its transcript has not been deleted, and answers with the next
/// capture session's number. The app then streams with the meeting's ID as its
/// callId and names that session in its START frame (`sessionNumber`), and the
/// meeting is reopened when the transcriber receives it. See
/// docs/desktop-capture-app.md#continue-an-existing-meeting.
///
/// Ported from macOS MeetingReopen.swift; SelfTestCases() checks the same
/// behaviour as MeetingReopenSelfTest.swift.
/// </summary>
public static class MeetingReopen
{
    /// <summary>The session the server allowed.</summary>
    public sealed record Session(string CallId, int SessionNumber, double OffsetSeconds);

    public enum FailureKind { NotConfigured, InvalidMeetingId, Refused, Unavailable }

    /// <summary>Why there is no session. For Refused, Code is the server's error type.</summary>
    public sealed record Failure(FailureKind Kind, string Code = "", string Message = "");

    /// <summary>The answer: exactly one of Session and Failure is set.</summary>
    public sealed record Result(Session? Session, Failure? Failure)
    {
        public static Result Ok(Session s) => new(s, null);
        public static Result Fail(Failure f) => new(null, f);
    }

    public const string Mutation =
        "mutation ReopenMeeting($input: ReopenMeetingInput!) { " +
        "reopenMeeting(input: $input) { CallId SessionNumber SessionOffsetSeconds ReopenedAt } }";

    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(20) };

    /// <summary>
    /// The meeting ID in what the user pasted: the ID itself, or the link to the
    /// meeting's page in LMA (`https://…/#/calls/&lt;encoded meeting ID&gt;`). null
    /// when there is nothing usable.
    /// </summary>
    public static string? MeetingId(string? input)
    {
        var text = (input ?? "").Trim();
        if (text.Length == 0) return null;
        const string marker = "#/calls/";
        var at = text.IndexOf(marker, StringComparison.Ordinal);
        if (at >= 0)
        {
            var encoded = text.Substring(at + marker.Length);
            // A link copied with a query or a trailing slash.
            var query = encoded.IndexOf('?');
            if (query >= 0) encoded = encoded.Substring(0, query);
            encoded = encoded.TrimEnd('/');
            string decoded;
            try { decoded = Uri.UnescapeDataString(encoded).Trim(); }
            catch (UriFormatException) { decoded = encoded.Trim(); }
            return decoded.Length == 0 ? null : decoded;
        }
        if (text.StartsWith("http://", StringComparison.OrdinalIgnoreCase) ||
            text.StartsWith("https://", StringComparison.OrdinalIgnoreCase)) return null;
        return text;
    }

    /// <summary>The POST to the AppSync endpoint, authorized with the user's Cognito token; null for a non-https endpoint.</summary>
    public static HttpRequestMessage? BuildRequest(string endpoint, string token, string callId)
    {
        if (!Uri.TryCreate(endpoint, UriKind.Absolute, out var uri) || uri.Scheme != Uri.UriSchemeHttps) return null;
        var body = JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["query"] = Mutation,
            ["variables"] = new Dictionary<string, object>
            {
                ["input"] = new Dictionary<string, object> { ["CallId"] = callId },
            },
        });
        var req = new HttpRequestMessage(HttpMethod.Post, uri)
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json"),
        };
        req.Headers.TryAddWithoutValidation("Authorization", token);
        return req;
    }

    /// <summary>The answer to the mutation: the session, or why there is none.</summary>
    public static Result ParseResponse(string json, int status)
    {
        JsonDocument doc;
        try { doc = JsonDocument.Parse(json); }
        catch (JsonException) { return Result.Fail(new Failure(FailureKind.Unavailable, Message: $"LMA answered with HTTP {status}.")); }
        using (doc)
        {
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                return Result.Fail(new Failure(FailureKind.Unavailable, Message: $"LMA answered with HTTP {status}."));
            if (root.TryGetProperty("errors", out var errors) && errors.ValueKind == JsonValueKind.Array &&
                errors.GetArrayLength() > 0)
            {
                var first = errors[0];
                var code = first.TryGetProperty("errorType", out var t) && t.ValueKind == JsonValueKind.String ? t.GetString() ?? "" : "";
                var message = first.TryGetProperty("message", out var m) && m.ValueKind == JsonValueKind.String ? m.GetString() ?? "" : "";
                return Result.Fail(new Failure(FailureKind.Refused, code, message));
            }
            if (root.TryGetProperty("data", out var data) && data.ValueKind == JsonValueKind.Object &&
                data.TryGetProperty("reopenMeeting", out var answer) && answer.ValueKind == JsonValueKind.Object &&
                answer.TryGetProperty("CallId", out var id) && id.ValueKind == JsonValueKind.String &&
                answer.TryGetProperty("SessionNumber", out var n) && n.ValueKind == JsonValueKind.Number &&
                n.TryGetInt32(out var number) && number >= 2)
            {
                double offset = answer.TryGetProperty("SessionOffsetSeconds", out var o) &&
                                o.ValueKind == JsonValueKind.Number ? o.GetDouble() : 0;
                return Result.Ok(new Session(id.GetString() ?? "", number, offset));
            }
            return Result.Fail(new Failure(FailureKind.Unavailable, Message: $"LMA did not reopen the meeting (HTTP {status})."));
        }
    }

    /// <summary>What the user is told. The server's own message for the refusals it names.</summary>
    public static string Describe(Failure failure)
    {
        switch (failure.Kind)
        {
            case FailureKind.NotConfigured:
                return "This copy of the app cannot continue meetings: its configuration has no LMA API " +
                       "endpoint. Download the app again from LMA.";
            case FailureKind.InvalidMeetingId:
                return "Paste the meeting ID, or the link to the meeting's page in LMA.";
            case FailureKind.Refused:
                string fallback;
                switch (failure.Code)
                {
                    case "MeetingNotFound": fallback = "There is no meeting with this ID."; break;
                    case "Unauthorized": fallback = "Only the meeting's owner or an Admin can continue this meeting."; break;
                    case "TranscriptDeleted": fallback = "This meeting's transcript has been deleted, so it cannot be continued."; break;
                    case "MeetingInProgress": fallback = "This meeting is still being captured. Continue it once it shows as Done."; break;
                    case "MeetingChanged": fallback = "The meeting changed while it was being reopened. Try again."; break;
                    default:
                        // Not one of reopenMeeting's refusals (an API error, for example).
                        return string.IsNullOrEmpty(failure.Message)
                            ? "LMA could not reopen the meeting."
                            : $"LMA could not reopen the meeting: {failure.Message}";
                }
                return string.IsNullOrEmpty(failure.Message) ? fallback : failure.Message;
            default:
                return $"LMA could not be reached to reopen the meeting. {failure.Message}";
        }
    }

    /// <summary>Ask LMA to reopen the meeting. `token` is the user's Cognito ID token (the access token works as well).</summary>
    public static async Task<Result> ReopenAsync(string endpoint, string token, string callId)
    {
        if (string.IsNullOrEmpty(endpoint)) return Result.Fail(new Failure(FailureKind.NotConfigured));
        using var req = BuildRequest(endpoint, token, callId);
        if (req == null) return Result.Fail(new Failure(FailureKind.NotConfigured));
        try
        {
            using var response = await Http.SendAsync(req).ConfigureAwait(false);
            var status = (int)response.StatusCode;
            if (status == 401 || status == 403)
                return Result.Fail(new Failure(FailureKind.Unavailable,
                    Message: $"Your session was not accepted (HTTP {status}); sign in again."));
            var body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
            return ParseResponse(body, status);
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or InvalidOperationException)
        {
            return Result.Fail(new Failure(FailureKind.Unavailable, Message: e.Message));
        }
    }

    /// <summary>`--selftest` cases, as (name, got, want): the same as MeetingReopenSelfTest.swift.</summary>
    public static List<(string Name, string Got, string Want)> SelfTestCases()
    {
        var o = new List<(string, string, string)>();
        void Check(string n, string g, string w) => o.Add((n, g, w));

        Check("meeting ID is taken as it is",
            MeetingId("  Weekly sync - 2026-11-18 09:00 \n") ?? "null", "Weekly sync - 2026-11-18 09:00");
        Check("meeting page link gives its meeting ID",
            MeetingId("https://d111.cloudfront.net/#/calls/Weekly%20sync%20-%202026-11-18%2009%3A00") ?? "null",
            "Weekly sync - 2026-11-18 09:00");
        Check("meeting page link with a trailing slash and query",
            MeetingId("https://d111.cloudfront.net/#/calls/abc%2F1/?tab=summary") ?? "null", "abc/1");
        Check("a link that is not a meeting page is refused",
            MeetingId("https://d111.cloudfront.net/#/calls") ?? "null", "null");
        Check("an empty paste is refused", MeetingId("   ") ?? "null", "null");
        Check("a link with no meeting ID is refused", MeetingId("https://x/#/calls/") ?? "null", "null");

        using (var req = BuildRequest("https://abc.appsync-api.us-east-1.amazonaws.com/graphql", "id-token", "m 1"))
        {
            Check("request is a POST", req?.Method.Method ?? "null", "POST");
            Check("request carries the token as Authorization",
                req != null && req.Headers.TryGetValues("Authorization", out var v) ? string.Join(",", v) : "null",
                "id-token");
            var body = req?.Content?.ReadAsStringAsync().GetAwaiter().GetResult() ?? "{}";
            using var parsed = JsonDocument.Parse(body);
            var callId = parsed.RootElement.GetProperty("variables").GetProperty("input").GetProperty("CallId").GetString();
            Check("request names the meeting", callId ?? "null", "m 1");
            Check("request names the mutation",
                (parsed.RootElement.GetProperty("query").GetString() ?? "").Contains("reopenMeeting(input: $input)").ToString(),
                "True");
        }
        Check("an endpoint that is not https is refused",
            (BuildRequest("http://x/graphql", "t", "m") == null).ToString(), "True");

        var ok = ParseResponse(
            "{\"data\":{\"reopenMeeting\":{\"CallId\":\"m 1\",\"SessionNumber\":3,\"SessionOffsetSeconds\":1807,\"ReopenedAt\":\"x\"}}}", 200);
        Check("answer: session number", ok.Session?.SessionNumber.ToString() ?? "null", "3");
        Check("answer: meeting ID", ok.Session?.CallId ?? "null", "m 1");
        Check("answer: offset", ok.Session?.OffsetSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "null", "1807");

        var refused = ParseResponse(
            "{\"data\":{\"reopenMeeting\":null},\"errors\":[{\"errorType\":\"TranscriptDeleted\",\"message\":\"Deleted.\"}]}", 200);
        Check("refusal: its code", refused.Failure?.Code ?? "null", "TranscriptDeleted");
        Check("refusal: the server's message is shown", refused.Failure is { } f ? Describe(f) : "null", "Deleted.");
        Check("refusal without a message has one of its own",
            Describe(new Failure(FailureKind.Refused, "Unauthorized")),
            "Only the meeting's owner or an Admin can continue this meeting.");
        Check("other errors say the meeting was not reopened",
            Describe(new Failure(FailureKind.Refused, "Lambda:Unhandled", "boom")),
            "LMA could not reopen the meeting: boom");
        Check("non-JSON answer is unavailable",
            (ParseResponse("<html>", 502).Failure?.Kind == FailureKind.Unavailable).ToString(), "True");
        Check("a first-session answer is not a reopen",
            (ParseResponse("{\"data\":{\"reopenMeeting\":{\"CallId\":\"m\",\"SessionNumber\":1}}}", 200).Failure?.Kind
             == FailureKind.Unavailable).ToString(), "True");
        Check("a new meeting names no session", (Config.StartSessionNumber(0) == null).ToString(), "True");
        Check("a continued meeting names its session", (Config.StartSessionNumber(3) ?? 0).ToString(), "3");
        return o;
    }
}
