import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// "Continue an existing meeting…": capture into a meeting that already exists,
/// even one shown as Done in LMA, on the same transcript timeline.
///
/// Before streaming, the app asks LMA's AppSync API to reopen the meeting
/// (the `reopenMeeting` mutation). The server checks that the signed-in user is
/// the meeting's owner or an Admin, that the meeting is not still being
/// captured and that its transcript has not been deleted, and answers with the
/// next capture session's number. The app then streams with the meeting's ID as
/// its callId and names that session in its START frame (`sessionNumber`), and
/// the meeting is reopened when the transcriber receives it. See
/// docs/desktop-capture-app.md#continue-an-existing-meeting.
///
/// Foundation only, like Consent.swift, so its `--selftest` cases
/// (MeetingReopenSelfTest.swift) also build where the rest of the app does not.
/// The Windows client's MeetingReopen.cs does the same.
enum MeetingReopen {
    /// The session the server allowed.
    struct Session: Equatable {
        let callId: String
        let sessionNumber: Int
        let offsetSeconds: Double
    }

    enum Failure: Error, Equatable {
        /// This download has no AppSync endpoint in its lma-config.json.
        case notConfigured
        /// What was pasted is neither a meeting ID nor a meeting page link.
        case invalidMeetingId
        /// The server refused; `code` is its error type.
        case refused(code: String, message: String)
        /// The request did not get an answer the app could read.
        case unavailable(String)
    }

    static let mutation =
        "mutation ReopenMeeting($input: ReopenMeetingInput!) { "
        + "reopenMeeting(input: $input) { CallId SessionNumber SessionOffsetSeconds ReopenedAt } }"

    /// The meeting ID in what the user pasted: the ID itself, or the link to the
    /// meeting's page in LMA (`https://…/#/calls/<encoded meeting ID>`). nil
    /// when there is nothing usable.
    static func meetingId(from input: String) -> String? {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if let marker = text.range(of: "#/calls/") {
            var encoded = String(text[marker.upperBound...])
            // A link copied with a query or a trailing slash.
            if let query = encoded.firstIndex(of: "?") { encoded = String(encoded[..<query]) }
            while encoded.hasSuffix("/") { encoded.removeLast() }
            let decoded = (encoded.removingPercentEncoding ?? encoded)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            return decoded.isEmpty ? nil : decoded
        }
        let lowered = text.lowercased()
        if lowered.hasPrefix("http://") || lowered.hasPrefix("https://") { return nil }
        return text
    }

    /// The POST to the AppSync endpoint, authorized with the user's Cognito token.
    static func request(endpoint: String, token: String, callId: String) -> URLRequest? {
        guard let url = URL(string: endpoint), url.scheme?.lowercased() == "https",
              let body = try? JSONSerialization.data(withJSONObject: [
                  "query": mutation,
                  "variables": ["input": ["CallId": callId]],
              ]) else { return nil }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(token, forHTTPHeaderField: "Authorization")
        req.httpBody = body
        req.timeoutInterval = 20
        return req
    }

    /// The answer to the mutation: the session, or why there is none.
    static func parseResponse(_ data: Data, status: Int) -> Result<Session, Failure> {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return .failure(.unavailable("LMA answered with HTTP \(status)."))
        }
        if let errors = obj["errors"] as? [[String: Any]], let first = errors.first {
            let code = (first["errorType"] as? String) ?? ""
            let message = (first["message"] as? String) ?? ""
            return .failure(.refused(code: code, message: message))
        }
        guard let payload = (obj["data"] as? [String: Any])?["reopenMeeting"] as? [String: Any],
              let callId = payload["CallId"] as? String,
              let number = (payload["SessionNumber"] as? NSNumber)?.intValue, number >= 2 else {
            return .failure(.unavailable("LMA did not reopen the meeting (HTTP \(status))."))
        }
        let offset = (payload["SessionOffsetSeconds"] as? NSNumber)?.doubleValue ?? 0
        return .success(Session(callId: callId, sessionNumber: number, offsetSeconds: offset))
    }

    /// What the user is told. The server's own message for the refusals it names.
    static func describe(_ failure: Failure) -> String {
        switch failure {
        case .notConfigured:
            return "This copy of the app cannot continue meetings: its configuration has no LMA API "
                + "endpoint. Download the app again from LMA."
        case .invalidMeetingId:
            return "Paste the meeting ID, or the link to the meeting's page in LMA."
        case .refused(let code, let message):
            let fallback: String
            switch code {
            case "MeetingNotFound": fallback = "There is no meeting with this ID."
            case "Unauthorized": fallback = "Only the meeting's owner or an Admin can continue this meeting."
            case "TranscriptDeleted": fallback = "This meeting's transcript has been deleted, so it cannot be continued."
            case "MeetingInProgress": fallback = "This meeting is still being captured. Continue it once it shows as Done."
            case "MeetingChanged": fallback = "The meeting changed while it was being reopened. Try again."
            default:
                // Not one of reopenMeeting's refusals (an API error, for example).
                return message.isEmpty ? "LMA could not reopen the meeting." : "LMA could not reopen the meeting: \(message)"
            }
            return message.isEmpty ? fallback : message
        case .unavailable(let why):
            return "LMA could not be reached to reopen the meeting. \(why)"
        }
    }

    /// Ask LMA to reopen the meeting. `token` is the user's Cognito ID token (the
    /// access token works as well).
    static func reopen(endpoint: String, token: String, callId: String,
                       session: URLSession = .shared) async -> Result<Session, Failure> {
        guard !endpoint.isEmpty else { return .failure(.notConfigured) }
        guard let req = request(endpoint: endpoint, token: token, callId: callId) else {
            return .failure(.notConfigured)
        }
        do {
            // A data task rather than `data(for:)`, which swift-corelibs-foundation
            // lacks: the same code then builds for the Linux self-test check.
            let (data, response): (Data, URLResponse?) = try await withCheckedThrowingContinuation { done in
                session.dataTask(with: req) { data, response, error in
                    if let error = error {
                        done.resume(throwing: error)
                    } else {
                        done.resume(returning: (data ?? Data(), response))
                    }
                }.resume()
            }
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status == 401 || status == 403 {
                return .failure(.unavailable("Your session was not accepted (HTTP \(status)); sign in again."))
            }
            return parseResponse(data, status: status)
        } catch {
            return .failure(.unavailable(error.localizedDescription))
        }
    }
}
