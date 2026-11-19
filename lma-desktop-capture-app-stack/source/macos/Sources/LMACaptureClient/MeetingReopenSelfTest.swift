import Foundation

/// `--selftest` cases for "Continue an existing meeting…" (MeetingReopen.swift),
/// as (name, got, want). Foundation only, like ConsentSelfTest.swift. The
/// Windows client's SelfTest.cs checks the same behaviour.
enum MeetingReopenSelfTest {
    static func cases() -> [(String, String, String)] {
        var out: [(String, String, String)] = []
        func check(_ name: String, _ got: String, _ want: String) { out.append((name, got, want)) }

        // What the user pastes.
        check("meeting ID is taken as it is",
              MeetingReopen.meetingId(from: "  Weekly sync - 2026-11-18 09:00 \n") ?? "nil",
              "Weekly sync - 2026-11-18 09:00")
        check("meeting page link gives its meeting ID",
              MeetingReopen.meetingId(
                from: "https://d111.cloudfront.net/#/calls/Weekly%20sync%20-%202026-11-18%2009%3A00") ?? "nil",
              "Weekly sync - 2026-11-18 09:00")
        check("meeting page link with a trailing slash and query",
              MeetingReopen.meetingId(from: "https://d111.cloudfront.net/#/calls/abc%2F1/?tab=summary") ?? "nil",
              "abc/1")
        check("a link that is not a meeting page is refused",
              MeetingReopen.meetingId(from: "https://d111.cloudfront.net/#/calls") ?? "nil", "nil")
        check("an empty paste is refused", MeetingReopen.meetingId(from: "   ") ?? "nil", "nil")
        check("a link with no meeting ID is refused",
              MeetingReopen.meetingId(from: "https://x/#/calls/") ?? "nil", "nil")

        // The request.
        let req = MeetingReopen.request(endpoint: "https://abc.appsync-api.us-east-1.amazonaws.com/graphql",
                                        token: "id-token", callId: "m 1")
        check("request is a POST", req?.httpMethod ?? "nil", "POST")
        check("request carries the token as Authorization",
              req?.value(forHTTPHeaderField: "Authorization") ?? "nil", "id-token")
        let body = req?.httpBody.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        let input = ((body?["variables"] as? [String: Any])?["input"] as? [String: Any])?["CallId"] as? String
        check("request names the meeting", input ?? "nil", "m 1")
        check("request names the mutation",
              "\((body?["query"] as? String)?.contains("reopenMeeting(input: $input)") ?? false)", "true")
        check("an endpoint that is not https is refused",
              "\(MeetingReopen.request(endpoint: "http://x/graphql", token: "t", callId: "m") == nil)", "true")

        // The answer.
        func parse(_ json: String, _ status: Int = 200) -> Result<MeetingReopen.Session, MeetingReopen.Failure> {
            MeetingReopen.parseResponse(Data(json.utf8), status: status)
        }
        if case .success(let s) = parse("""
            {"data":{"reopenMeeting":{"CallId":"m 1","SessionNumber":3,"SessionOffsetSeconds":1807,"ReopenedAt":"x"}}}
            """) {
            check("answer: session number", "\(s.sessionNumber)", "3")
            check("answer: meeting ID", s.callId, "m 1")
            check("answer: offset", "\(s.offsetSeconds)", "1807.0")
        } else {
            check("answer parses", "failure", "success")
        }
        if case .failure(let f) = parse("""
            {"data":{"reopenMeeting":null},"errors":[{"errorType":"TranscriptDeleted","message":"Deleted."}]}
            """) {
            check("refusal: its code", "\(f == .refused(code: "TranscriptDeleted", message: "Deleted."))", "true")
            check("refusal: the server's message is shown", MeetingReopen.describe(f), "Deleted.")
        } else {
            check("refusal parses", "success", "failure")
        }
        check("refusal without a message has one of its own",
              MeetingReopen.describe(.refused(code: "Unauthorized", message: "")),
              "Only the meeting's owner or an Admin can continue this meeting.")
        check("other errors say the meeting was not reopened",
              MeetingReopen.describe(.refused(code: "Lambda:Unhandled", message: "boom")),
              "LMA could not reopen the meeting: boom")
        if case .failure(.unavailable) = parse("<html>", 502) {
            check("non-JSON answer is unavailable", "true", "true")
        } else {
            check("non-JSON answer is unavailable", "false", "true")
        }
        if case .failure(.unavailable) = parse("{\"data\":{\"reopenMeeting\":{\"CallId\":\"m\",\"SessionNumber\":1}}}") {
            check("a first-session answer is not a reopen", "true", "true")
        } else {
            check("a first-session answer is not a reopen", "false", "true")
        }
        return out
    }
}
