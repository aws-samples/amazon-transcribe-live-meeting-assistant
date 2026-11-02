import Foundation

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
/// Foundation only, so `--selftest` can pin it and it compiles anywhere.
/// The Windows client's Engine/Consent.cs is the same logic.
enum Consent {
    enum Mode: String {
        case off
        case notify
        case optIn = "opt_in"
    }

    /// How everyone was told, or agreed.
    enum Method: String, CaseIterable, Identifiable {
        case verbal
        case chat
        case inviteTerms = "invite_terms"

        var id: String { rawValue }

        var label: String {
            switch self {
            case .verbal: return "Out loud"
            case .chat: return "In the meeting chat"
            case .inviteTerms: return "In the invitation's terms"
            }
        }
    }

    /// The transcriber's answer to CONSENT_SETTINGS.
    struct Settings: Equatable {
        var available: Bool
        var mode: Mode
        /// False when the deployment's opt-in rule cannot be met from this app.
        var supported: Bool
        var reason: String
        var noticeTemplate: String
        var consentPhrase: String
        var objectionPhrase: String
        /// Why the settings are unavailable, when they are.
        var message: String

        /// Whether a consent step is needed before Start.
        var required: Bool { available && mode != .off }

        /// What START carries when no step was shown: mode off. Sent rather than
        /// nothing, so a transcriber in an opt-in deployment refuses the meeting
        /// (and the app reads the settings again) instead of taking this app for
        /// a client without a consent step.
        static let notShown = Settings(available: false, mode: .off, supported: true, reason: "",
                                       noticeTemplate: "", consentPhrase: "", objectionPhrase: "",
                                       message: "")
    }

    /// What the user filled in.
    struct Step: Equatable {
        var confirmed = false
        var method: Method?
        var statement = ""
    }

    /// A CONSENT_STATE frame.
    struct State: Equatable {
        var status: String?
        /// False only when the transcriber stopped (or will not start) capturing
        /// the meeting: it has ended the meeting and is closing the connection.
        var captureAllowed: Bool?
        var code: String?
        var message: String?
        var reason: String?
        var evidenceStored = false
        var evidenceVerdict: String?

        var stopsCapture: Bool { captureAllowed == false }
        var isEvidence: Bool { evidenceStored || (code ?? "").hasPrefix("EVIDENCE_") }
    }

    enum ServerFrame: Equatable {
        case settings(Settings)
        case state(State)
    }

    /// The client name sent with START, used in the attestation's statement.
    static let clientName = "the LMA Desktop Capture App"

    /// The consent evidence checker reads at most this many bytes.
    static let maxEvidenceBytes = 3_932_160

    // MARK: Frames from the server

    /// A JSON text frame from the transcriber, or nil for anything else.
    static func parseFrame(_ text: String) -> ServerFrame? {
        guard let data = text.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let event = obj["event"] as? String else { return nil }
        switch event {
        case "CONSENT_SETTINGS":
            let available = obj["available"] as? Bool ?? false
            return .settings(Settings(
                available: available,
                mode: Mode(rawValue: obj["mode"] as? String ?? "") ?? .off,
                supported: obj["supported"] as? Bool ?? true,
                reason: obj["reason"] as? String ?? "",
                noticeTemplate: obj["noticeTemplate"] as? String ?? "",
                consentPhrase: obj["consentPhrase"] as? String ?? "",
                objectionPhrase: obj["objectionPhrase"] as? String ?? "",
                message: obj["message"] as? String ?? ""))
        case "CONSENT_STATE":
            let evidence = obj["evidence"] as? [String: Any]
            return .state(State(
                status: obj["status"] as? String,
                captureAllowed: obj["captureAllowed"] as? Bool,
                code: obj["code"] as? String,
                message: obj["message"] as? String,
                reason: obj["reason"] as? String,
                evidenceStored: evidence?["stored"] as? Bool ?? false,
                evidenceVerdict: evidence?["verdict"] as? String))
        default:
            return nil
        }
    }

    // MARK: The step

    /// The notice with its placeholders filled as the server fills them:
    /// {LMA_USER}, {meeting}, {consent_phrase} and {objection_phrase}; any other
    /// text in braces is left as written.
    static func renderNotice(_ template: String, lmaUser: String, meeting: String,
                             consentPhrase: String, objectionPhrase: String) -> String {
        let values: [(String, String)] = [
            ("{LMA_USER}", lmaUser.isEmpty ? "The meeting organizer" : lmaUser),
            ("{meeting}", meeting.isEmpty ? "this meeting" : meeting),
            ("{consent_phrase}", consentPhrase),
            ("{objection_phrase}", objectionPhrase),
        ]
        // One left-to-right pass, so a value containing a placeholder is not
        // filled again.
        var out = ""
        var rest = Substring(template)
        outer: while !rest.isEmpty {
            for (key, value) in values where rest.hasPrefix(key) {
                out += value
                rest = rest.dropFirst(key.count)
                continue outer
            }
            out.append(rest.removeFirst())
        }
        return String(out.prefix(2000))
    }

    /// Why Start must wait, or nil when it may go ahead. Opt-in needs the
    /// confirmation and how it was given; notify may start without them.
    static func startBlockedReason(settings: Settings?, step: Step) -> String? {
        guard let settings = settings, settings.required else { return nil }
        if !settings.supported {
            return settings.reason.isEmpty ? "This deployment does not allow recording from this app." : settings.reason
        }
        if settings.mode == .optIn {
            if !step.confirmed { return "Confirm that everyone agreed before you start." }
            if step.method == nil { return "Choose how everyone agreed." }
        }
        return nil
    }

    /// The `consent` field of START.
    static func payload(settings: Settings, step: Step, lmaUser: String, meetingName: String,
                        noticeText: String) -> [String: Any] {
        var out: [String: Any] = [
            "mode": settings.mode.rawValue,
            "confirmed": step.confirmed,
            "noticeText": noticeText,
            "lmaUser": lmaUser,
            "meetingName": meetingName,
            "client": clientName,
        ]
        if let method = step.method { out["method"] = method.rawValue }
        let note = step.statement.trimmingCharacters(in: .whitespacesAndNewlines)
        if !note.isEmpty { out["statement"] = note }
        return out
    }

    static func objectionFrame(participantName: String) -> [String: Any] {
        var out: [String: Any] = ["callEvent": "CONSENT_OBJECTION", "source": "voice"]
        let name = participantName.trimmingCharacters(in: .whitespacesAndNewlines)
        if !name.isEmpty { out["participantName"] = name }
        return out
    }

    /// A CONSENT_EVIDENCE frame for a JPEG, or nil when the image is too large
    /// for the checker (the caller re-encodes smaller).
    static func evidenceFrame(jpeg: Data, capturedAt: Date, note: String) -> [String: Any]? {
        guard !jpeg.isEmpty, jpeg.count <= maxEvidenceBytes else { return nil }
        return [
            "callEvent": "CONSENT_EVIDENCE",
            "image": "data:image/jpeg;base64," + jpeg.base64EncodedString(),
            "capturedAt": isoTimestamp(capturedAt),
            "note": note,
        ]
    }

    static let settingsRequest: [String: Any] = ["callEvent": "CONSENT_SETTINGS"]

    static func isoTimestamp(_ date: Date) -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f.string(from: date)
    }

    /// One line for the panel about a CONSENT_STATE frame.
    static func describe(_ state: State) -> String {
        if let message = state.message, !message.isEmpty { return message }
        if state.evidenceStored {
            switch state.evidenceVerdict {
            case "supports_consent": return "Evidence screenshot stored; the check found the notice and no objection."
            case "objection_found": return "Evidence screenshot stored; the check found an objection."
            case "inconclusive": return "Evidence screenshot stored; the check was inconclusive."
            default: return "Evidence screenshot stored on the meeting."
            }
        }
        if state.status == "objected" { return "A participant objected. Recording stopped." }
        if state.captureAllowed == true {
            return "Consent recorded on the meeting (\(state.status ?? "granted")). Recording and transcribing."
        }
        return "Consent \(state.status ?? "pending"): not recording or transcribing yet."
    }
}
