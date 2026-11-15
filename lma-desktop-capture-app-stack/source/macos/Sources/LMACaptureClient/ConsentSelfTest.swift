import Foundation

/// `--selftest` cases for the meeting consent step (Consent.swift), as
/// (name, got, want). Kept apart from SelfTest.swift and free of anything but
/// Foundation, so the same cases also run where the rest of the app does not
/// build. The Windows client's SelfTest.cs checks the same behaviour.
enum ConsentSelfTest {
    static func cases() -> [(String, String, String)] {
        var out: [(String, String, String)] = []
        func check(_ name: String, _ got: String, _ want: String) { out.append((name, got, want)) }

        // Frames from the transcriber.
        let settingsJSON = """
        {"event":"CONSENT_SETTINGS","available":true,"mode":"opt_in","rule":"owner_attests","supported":true,
         "noticeTemplate":"{LMA_USER} would like to record {meeting}. Reply \\"{consent_phrase}\\".",
         "consentPhrase":"I consent","objectionPhrase":"I object"}
        """
        guard case .settings(let optIn)? = Consent.parseFrame(settingsJSON) else {
            check("CONSENT_SETTINGS parses", "nil", "settings")
            return out
        }
        check("settings: mode", optIn.mode.rawValue, "opt_in")
        check("settings: required", "\(optIn.required)", "true")
        var off = optIn; off.mode = .off
        check("settings: off needs no step", "\(off.required)", "false")
        var unavailable = optIn; unavailable.available = false
        check("settings: unavailable needs no step", "\(unavailable.required)", "false")
        check("non-consent frame is ignored", "\(Consent.parseFrame("{\"event\":\"OTHER\"}") == nil)", "true")
        check("non-JSON frame is ignored", "\(Consent.parseFrame("hello") == nil)", "true")

        let stop = Consent.parseFrame("""
        {"event":"CONSENT_STATE","callId":"m","captureAllowed":false,"code":"CONSENT_NOT_RECORDED","message":"Not recorded."}
        """)
        if case .state(let s)? = stop {
            check("state: captureAllowed false stops capture", "\(s.stopsCapture)", "true")
            check("state: message is shown as sent", Consent.describe(s), "Not recorded.")
        } else {
            check("CONSENT_STATE parses", "nil", "state")
        }
        let granted = Consent.parseFrame("{\"event\":\"CONSENT_STATE\",\"captureAllowed\":true,\"status\":\"granted\"}")
        if case .state(let s)? = granted {
            check("state: granted does not stop capture", "\(s.stopsCapture)", "false")
        }
        let evidence = Consent.parseFrame(
            "{\"event\":\"CONSENT_STATE\",\"evidence\":{\"evidenceId\":\"e\",\"stored\":true,\"verdict\":\"inconclusive\"}}")
        if case .state(let s)? = evidence {
            check("state: evidence result is evidence", "\(s.isEvidence)", "true")
            check("state: evidence result keeps capture", "\(s.stopsCapture)", "false")
            check("state: evidence verdict described",
                  Consent.describe(s), "Evidence screenshot stored; the check was inconclusive.")
        }

        // The notice, filled as the server fills it.
        check("notice placeholders filled",
              Consent.renderNotice(optIn.noticeTemplate, lmaUser: "Ana", meeting: "Sync",
                                   consentPhrase: optIn.consentPhrase, objectionPhrase: optIn.objectionPhrase),
              "Ana would like to record Sync. Reply \"I consent\".")
        check("notice defaults and unknown braces",
              Consent.renderNotice("{LMA_USER}/{meeting}/{other}", lmaUser: "", meeting: "",
                                   consentPhrase: "", objectionPhrase: ""),
              "The meeting organizer/this meeting/{other}")
        check("notice: a value holding a placeholder is not filled again",
              Consent.renderNotice("{LMA_USER}", lmaUser: "{meeting}", meeting: "X",
                                   consentPhrase: "", objectionPhrase: ""),
              "{meeting}")

        // Start is held for an unconfirmed opt-in meeting only.
        check("opt-in unconfirmed is held",
              Consent.startBlockedReason(settings: optIn, step: Consent.Step()) ?? "nil",
              "Confirm that everyone agreed before you start.")
        check("opt-in without a method is held",
              Consent.startBlockedReason(settings: optIn, step: Consent.Step(confirmed: true)) ?? "nil",
              "Choose how everyone agreed.")
        check("opt-in confirmed starts",
              Consent.startBlockedReason(settings: optIn,
                                         step: Consent.Step(confirmed: true, method: .chat)) ?? "nil",
              "nil")
        var notify = optIn; notify.mode = .notify
        check("notify starts unconfirmed",
              Consent.startBlockedReason(settings: notify, step: Consent.Step()) ?? "nil", "nil")
        check("no settings starts", Consent.startBlockedReason(settings: nil, step: Consent.Step()) ?? "nil", "nil")
        var unsupported = optIn; unsupported.supported = false; unsupported.reason = "Use a Virtual Participant."
        check("unsupported rule is held",
              Consent.startBlockedReason(settings: unsupported,
                                         step: Consent.Step(confirmed: true, method: .verbal)) ?? "nil",
              "Use a Virtual Participant.")

        // What START carries.
        let payload = Consent.payload(settings: optIn,
                                      step: Consent.Step(confirmed: true, method: .inviteTerms, statement: "  in the invite "),
                                      lmaUser: "Ana", meetingName: "Sync", noticeText: "N")
        check("payload: mode", payload["mode"] as? String ?? "-", "opt_in")
        check("payload: confirmed", "\(payload["confirmed"] as? Bool ?? false)", "true")
        check("payload: method", payload["method"] as? String ?? "-", "invite_terms")
        check("payload: note trimmed", payload["statement"] as? String ?? "-", "in the invite")
        check("payload: client named", payload["client"] as? String ?? "-", Consent.clientName)
        let bare = Consent.payload(settings: notify, step: Consent.Step(), lmaUser: "Ana", meetingName: "", noticeText: "N")
        check("payload: no method or note when none given",
              "\(bare["method"] == nil && bare["statement"] == nil)", "true")

        let notShown = Consent.payload(settings: .notShown, step: Consent.Step(), lmaUser: "Ana",
                                       meetingName: "", noticeText: "")
        check("payload without a step says mode off, unconfirmed",
              "\(notShown["mode"] as? String ?? "-")|\(notShown["confirmed"] as? Bool ?? true)", "off|false")

        // Objection and evidence frames.
        let objection = Consent.objectionFrame(participantName: " Bo Chen ")
        check("objection frame", "\(objection["callEvent"] as? String ?? "-")|\(objection["participantName"] as? String ?? "-")|\(objection["source"] as? String ?? "-")",
              "CONSENT_OBJECTION|Bo Chen|voice")
        check("objection without a name omits it",
              "\(Consent.objectionFrame(participantName: "  ")["participantName"] == nil)", "true")
        let jpeg = Data([0xff, 0xd8, 0xff, 0xe0, 0x01])
        let frame = Consent.evidenceFrame(jpeg: jpeg, capturedAt: Date(timeIntervalSince1970: 0), note: "n")
        check("evidence frame image", frame?["image"] as? String ?? "-", "data:image/jpeg;base64,/9j/4AE=")
        check("evidence frame time", frame?["capturedAt"] as? String ?? "-", "1970-01-01T00:00:00.000Z")
        check("evidence over the checker limit is not sent",
              "\(Consent.evidenceFrame(jpeg: Data(count: Consent.maxEvidenceBytes + 1), capturedAt: Date(), note: "") == nil)",
              "true")
        return out
    }
}
