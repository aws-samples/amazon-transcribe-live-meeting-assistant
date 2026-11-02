using System.Text;
using System.Text.Json;

namespace LMA;

/// <summary>
/// Meeting consent for a desktop recording, with no UI and no network in it.
///
/// When the deployment's meeting consent mode is Notify or Opt-in, the app shows
/// a consent step before Start: the notice to read out or paste into the meeting
/// chat, the user's confirmation and how it was given, and an optional evidence
/// screenshot of the selected meeting window. The step rides the START frame;
/// the transcriber records it on the meeting once the meeting exists and only
/// transcribes and records while the meeting's consent allows it. The protocol
/// is in docs/websocket-streaming-api.md ("Meeting Consent Frames") and the
/// behaviour in docs/meeting-consent.md.
///
/// Ported from macOS Consent.swift; SelfTest.cs pins the same behaviour.
/// </summary>
public static class Consent
{
    public enum Mode { Off, Notify, OptIn }

    /// <summary>How everyone was told, or agreed.</summary>
    public enum Method { Verbal, Chat, InviteTerms }

    public static string Wire(Mode m) => m switch { Mode.Notify => "notify", Mode.OptIn => "opt_in", _ => "off" };

    public static string Wire(Method m) => m switch
    {
        Method.Verbal => "verbal",
        Method.Chat => "chat",
        _ => "invite_terms",
    };

    public static string Label(Method m) => m switch
    {
        Method.Verbal => "Out loud",
        Method.Chat => "In the meeting chat",
        _ => "In the invitation's terms",
    };

    public static Method? ParseMethod(string? s) => s switch
    {
        "verbal" => Method.Verbal,
        "chat" => Method.Chat,
        "invite_terms" => Method.InviteTerms,
        _ => null,
    };

    private static Mode ParseMode(string? s) => s switch
    {
        "notify" => Mode.Notify,
        "opt_in" => Mode.OptIn,
        _ => Mode.Off,
    };

    /// <summary>The transcriber's answer to CONSENT_SETTINGS.</summary>
    public sealed record Settings(
        bool Available,
        Mode Mode,
        bool Supported,
        string Reason,
        string NoticeTemplate,
        string ConsentPhrase,
        string ObjectionPhrase,
        string Message)
    {
        /// <summary>Whether a consent step is needed before Start.</summary>
        public bool Required => Available && Mode != Mode.Off;

        /// <summary>
        /// What START carries when no step was shown: mode off. Sent rather than
        /// nothing, so a transcriber in an opt-in deployment refuses the meeting
        /// (and the app reads the settings again) instead of taking this app for a
        /// client without a consent step.
        /// </summary>
        public static readonly Settings NotShown = new(false, Mode.Off, true, "", "", "", "", "");
    }

    /// <summary>What the user filled in.</summary>
    public sealed record Step(bool Confirmed = false, Method? Method = null, string Statement = "");

    /// <summary>A CONSENT_STATE frame.</summary>
    public sealed record State(
        string? Status,
        bool? CaptureAllowed,
        string? Code,
        string? Message,
        string? Reason,
        bool EvidenceStored,
        string? EvidenceVerdict)
    {
        /// <summary>
        /// False only when the transcriber stopped (or will not start) capturing
        /// the meeting: it has ended the meeting and is closing the connection.
        /// </summary>
        public bool StopsCapture => CaptureAllowed == false;

        public bool IsEvidence => EvidenceStored || (Code ?? "").StartsWith("EVIDENCE_", StringComparison.Ordinal);
    }

    /// <summary>The client name sent with START, used in the attestation's statement.</summary>
    public const string ClientName = "the LMA Desktop Capture App";

    /// <summary>The consent evidence checker reads at most this many bytes.</summary>
    public const int MaxEvidenceBytes = 3_932_160;

    public static readonly Dictionary<string, object> SettingsRequest = new() { ["callEvent"] = "CONSENT_SETTINGS" };

    // MARK: Frames from the server

    private static string? Str(JsonElement o, string name) =>
        o.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    private static bool? Bool(JsonElement o, string name) =>
        o.TryGetProperty(name, out var v) && (v.ValueKind == JsonValueKind.True || v.ValueKind == JsonValueKind.False)
            ? v.GetBoolean() : null;

    /// <summary>A CONSENT_SETTINGS frame, or null for anything else.</summary>
    public static Settings? ParseSettings(string text)
    {
        var root = Parse(text, "CONSENT_SETTINGS");
        if (root is not { } o) return null;
        return new Settings(
            Available: Bool(o, "available") ?? false,
            Mode: ParseMode(Str(o, "mode")),
            Supported: Bool(o, "supported") ?? true,
            Reason: Str(o, "reason") ?? "",
            NoticeTemplate: Str(o, "noticeTemplate") ?? "",
            ConsentPhrase: Str(o, "consentPhrase") ?? "",
            ObjectionPhrase: Str(o, "objectionPhrase") ?? "",
            Message: Str(o, "message") ?? "");
    }

    /// <summary>A CONSENT_STATE frame, or null for anything else.</summary>
    public static State? ParseState(string text)
    {
        var root = Parse(text, "CONSENT_STATE");
        if (root is not { } o) return null;
        bool stored = false;
        string? verdict = null;
        if (o.TryGetProperty("evidence", out var ev) && ev.ValueKind == JsonValueKind.Object)
        {
            stored = Bool(ev, "stored") ?? false;
            verdict = Str(ev, "verdict");
        }
        return new State(Str(o, "status"), Bool(o, "captureAllowed"), Str(o, "code"), Str(o, "message"),
            Str(o, "reason"), stored, verdict);
    }

    private static JsonElement? Parse(string text, string wantEvent)
    {
        try
        {
            using var doc = JsonDocument.Parse(text);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object || Str(root, "event") != wantEvent) return null;
            return root.Clone();
        }
        catch (JsonException)
        {
            return null;
        }
    }

    // MARK: The step

    /// <summary>
    /// The notice with its placeholders filled as the server fills them:
    /// {LMA_USER}, {meeting}, {consent_phrase} and {objection_phrase}; any other
    /// text in braces is left as written. One left-to-right pass, so a value
    /// containing a placeholder is not filled again.
    /// </summary>
    public static string RenderNotice(string template, string lmaUser, string meeting,
                                      string consentPhrase, string objectionPhrase)
    {
        var values = new (string Key, string Value)[]
        {
            ("{LMA_USER}", string.IsNullOrEmpty(lmaUser) ? "The meeting organizer" : lmaUser),
            ("{meeting}", string.IsNullOrEmpty(meeting) ? "this meeting" : meeting),
            ("{consent_phrase}", consentPhrase),
            ("{objection_phrase}", objectionPhrase),
        };
        var sb = new StringBuilder();
        int i = 0;
        while (i < template.Length)
        {
            bool matched = false;
            foreach (var (key, value) in values)
            {
                if (string.CompareOrdinal(template, i, key, 0, key.Length) == 0)
                {
                    sb.Append(value);
                    i += key.Length;
                    matched = true;
                    break;
                }
            }
            if (!matched) { sb.Append(template[i]); i++; }
        }
        var s = sb.ToString();
        return s.Length > 2000 ? s.Substring(0, 2000) : s;
    }

    /// <summary>
    /// Why Start must wait, or null when it may go ahead. Opt-in needs the
    /// confirmation and how it was given; notify may start without them.
    /// </summary>
    public static string? StartBlockedReason(Settings? settings, Step step)
    {
        if (settings is not { Required: true }) return null;
        if (!settings.Supported)
            return string.IsNullOrEmpty(settings.Reason)
                ? "This deployment does not allow recording from this app." : settings.Reason;
        if (settings.Mode == Mode.OptIn)
        {
            if (!step.Confirmed) return "Confirm that everyone agreed before you start.";
            if (step.Method == null) return "Choose how everyone agreed.";
        }
        return null;
    }

    /// <summary>The `consent` field of START.</summary>
    public static Dictionary<string, object> Payload(Settings settings, Step step, string lmaUser,
                                                     string meetingName, string noticeText)
    {
        var o = new Dictionary<string, object>
        {
            ["mode"] = Wire(settings.Mode),
            ["confirmed"] = step.Confirmed,
            ["noticeText"] = noticeText,
            ["lmaUser"] = lmaUser,
            ["meetingName"] = meetingName,
            ["client"] = ClientName,
        };
        if (step.Method is { } m) o["method"] = Wire(m);
        var note = (step.Statement ?? "").Trim();
        if (note.Length > 0) o["statement"] = note;
        return o;
    }

    public static Dictionary<string, object> ObjectionFrame(string participantName)
    {
        var o = new Dictionary<string, object> { ["callEvent"] = "CONSENT_OBJECTION", ["source"] = "voice" };
        var name = (participantName ?? "").Trim();
        if (name.Length > 0) o["participantName"] = name;
        return o;
    }

    /// <summary>
    /// A CONSENT_EVIDENCE frame for a JPEG, or null when the image is too large
    /// for the checker (the caller re-encodes smaller).
    /// </summary>
    public static Dictionary<string, object>? EvidenceFrame(byte[] jpeg, DateTime capturedAtUtc, string note)
    {
        if (jpeg.Length == 0 || jpeg.Length > MaxEvidenceBytes) return null;
        return new Dictionary<string, object>
        {
            ["callEvent"] = "CONSENT_EVIDENCE",
            ["image"] = "data:image/jpeg;base64," + Convert.ToBase64String(jpeg),
            ["capturedAt"] = IsoTimestamp(capturedAtUtc),
            ["note"] = note,
        };
    }

    public static string IsoTimestamp(DateTime utc) =>
        utc.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", System.Globalization.CultureInfo.InvariantCulture);

    /// <summary>One line for the panel about a CONSENT_STATE frame.</summary>
    public static string Describe(State state)
    {
        if (!string.IsNullOrEmpty(state.Message)) return state.Message;
        if (state.EvidenceStored)
        {
            return state.EvidenceVerdict switch
            {
                "supports_consent" => "Evidence screenshot stored; the check found the notice and no objection.",
                "objection_found" => "Evidence screenshot stored; the check found an objection.",
                "inconclusive" => "Evidence screenshot stored; the check was inconclusive.",
                _ => "Evidence screenshot stored on the meeting.",
            };
        }
        if (state.Status == "objected") return "A participant objected. Recording stopped.";
        if (state.CaptureAllowed == true)
            return $"Consent recorded on the meeting ({state.Status ?? "granted"}). Recording and transcribing.";
        return $"Consent {state.Status ?? "pending"}: not recording or transcribing yet.";
    }

    /// <summary>
    /// `--selftest` cases, as (name, got, want): the same behaviour the macOS
    /// client's ConsentSelfTest.swift checks.
    /// </summary>
    public static List<(string Name, string Got, string Want)> SelfTestCases()
    {
        var o = new List<(string, string, string)>();
        void Check(string n, string g, string w) => o.Add((n, g, w));

        var optIn = ParseSettings(
            "{\"event\":\"CONSENT_SETTINGS\",\"available\":true,\"mode\":\"opt_in\",\"rule\":\"owner_attests\","
            + "\"supported\":true,\"noticeTemplate\":\"{LMA_USER} would like to record {meeting}. Reply \\\"{consent_phrase}\\\".\","
            + "\"consentPhrase\":\"I consent\",\"objectionPhrase\":\"I object\"}");
        if (optIn == null) { Check("CONSENT_SETTINGS parses", "null", "settings"); return o; }
        Check("settings: mode", Wire(optIn.Mode), "opt_in");
        Check("settings: required", optIn.Required.ToString(), "True");
        Check("settings: off needs no step", (optIn with { Mode = Mode.Off }).Required.ToString(), "False");
        Check("settings: unavailable needs no step", (optIn with { Available = false }).Required.ToString(), "False");
        Check("non-consent frame is ignored", (ParseSettings("{\"event\":\"OTHER\"}") == null).ToString(), "True");
        Check("non-JSON frame is ignored", (ParseState("hello") == null).ToString(), "True");

        var stop = ParseState("{\"event\":\"CONSENT_STATE\",\"callId\":\"m\",\"captureAllowed\":false,"
            + "\"code\":\"CONSENT_NOT_RECORDED\",\"message\":\"Not recorded.\"}");
        Check("state: captureAllowed false stops capture", (stop?.StopsCapture ?? false).ToString(), "True");
        Check("state: message is shown as sent", stop == null ? "null" : Describe(stop), "Not recorded.");
        var granted = ParseState("{\"event\":\"CONSENT_STATE\",\"captureAllowed\":true,\"status\":\"granted\"}");
        Check("state: granted does not stop capture", (granted?.StopsCapture ?? true).ToString(), "False");
        var evidence = ParseState(
            "{\"event\":\"CONSENT_STATE\",\"evidence\":{\"evidenceId\":\"e\",\"stored\":true,\"verdict\":\"inconclusive\"}}");
        Check("state: evidence result is evidence", (evidence?.IsEvidence ?? false).ToString(), "True");
        Check("state: evidence result keeps capture", (evidence?.StopsCapture ?? true).ToString(), "False");
        Check("state: evidence verdict described", evidence == null ? "null" : Describe(evidence),
            "Evidence screenshot stored; the check was inconclusive.");

        Check("notice placeholders filled",
            RenderNotice(optIn.NoticeTemplate, "Ana", "Sync", optIn.ConsentPhrase, optIn.ObjectionPhrase),
            "Ana would like to record Sync. Reply \"I consent\".");
        Check("notice defaults and unknown braces",
            RenderNotice("{LMA_USER}/{meeting}/{other}", "", "", "", ""), "The meeting organizer/this meeting/{other}");
        Check("notice: a value holding a placeholder is not filled again",
            RenderNotice("{LMA_USER}", "{meeting}", "X", "", ""), "{meeting}");

        Check("opt-in unconfirmed is held", StartBlockedReason(optIn, new Step()) ?? "null",
            "Confirm that everyone agreed before you start.");
        Check("opt-in without a method is held", StartBlockedReason(optIn, new Step(true)) ?? "null",
            "Choose how everyone agreed.");
        Check("opt-in confirmed starts", StartBlockedReason(optIn, new Step(true, Method.Chat)) ?? "null", "null");
        var notify = optIn with { Mode = Mode.Notify };
        Check("notify starts unconfirmed", StartBlockedReason(notify, new Step()) ?? "null", "null");
        Check("no settings starts", StartBlockedReason(null, new Step()) ?? "null", "null");
        Check("unsupported rule is held",
            StartBlockedReason(optIn with { Supported = false, Reason = "Use a Virtual Participant." },
                new Step(true, Method.Verbal)) ?? "null",
            "Use a Virtual Participant.");

        var payload = Payload(optIn, new Step(true, Method.InviteTerms, "  in the invite "), "Ana", "Sync", "N");
        Check("payload: mode", payload["mode"].ToString()!, "opt_in");
        Check("payload: confirmed", payload["confirmed"].ToString()!, "True");
        Check("payload: method", payload["method"].ToString()!, "invite_terms");
        Check("payload: note trimmed", payload["statement"].ToString()!, "in the invite");
        Check("payload: client named", payload["client"].ToString()!, ClientName);
        var bare = Payload(notify, new Step(), "Ana", "", "N");
        Check("payload: no method or note when none given",
            (!bare.ContainsKey("method") && !bare.ContainsKey("statement")).ToString(), "True");
        Check("payload serializes as the server reads it",
            JsonSerializer.Serialize(Payload(notify, new Step(true, Method.Verbal), "A", "M", "N")),
            "{\"mode\":\"notify\",\"confirmed\":true,\"noticeText\":\"N\",\"lmaUser\":\"A\",\"meetingName\":\"M\","
            + "\"client\":\"the LMA Desktop Capture App\",\"method\":\"verbal\"}");

        var notShown = Payload(Settings.NotShown, new Step(), "Ana", "", "");
        Check("payload without a step says mode off, unconfirmed", $"{notShown["mode"]}|{notShown["confirmed"]}", "off|False");

        var objection = ObjectionFrame(" Bo Chen ");
        Check("objection frame", $"{objection["callEvent"]}|{objection["participantName"]}|{objection["source"]}",
            "CONSENT_OBJECTION|Bo Chen|voice");
        Check("objection without a name omits it", (!ObjectionFrame("  ").ContainsKey("participantName")).ToString(), "True");
        var frame = EvidenceFrame(new byte[] { 0xff, 0xd8, 0xff, 0xe0, 0x01 },
            new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc), "n");
        Check("evidence frame image", frame?["image"].ToString() ?? "-", "data:image/jpeg;base64,/9j/4AE=");
        Check("evidence frame time", frame?["capturedAt"].ToString() ?? "-", "1970-01-01T00:00:00.000Z");
        Check("evidence over the checker limit is not sent",
            (EvidenceFrame(new byte[MaxEvidenceBytes + 1], DateTime.UtcNow, "") == null).ToString(), "True");
        return o;
    }
}
