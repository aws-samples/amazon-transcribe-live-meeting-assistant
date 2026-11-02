#if canImport(SwiftUI) && canImport(AppKit)
import SwiftUI
import AppKit
import ImageIO

/// The meeting consent step on the main panel (Notify / Opt-in deployments).
///
/// Before Start: the notice with Copy, the confirmation and how it was given, an
/// optional note, and Capture evidence (one frame of the selected screen-video
/// source). While recording: the consent status, Capture evidence, and
/// Participant objected. Renders nothing when the deployment needs no step or
/// the transcriber did not say. See docs/meeting-consent.md.
@available(macOS 13.0, *)
struct ConsentPanelView: View {
    @ObservedObject var s: MenuBarAppState

    var body: some View {
        if let settings = s.consentSettings, settings.required {
            panel(settings)
        } else if let settings = s.consentSettings, !settings.available {
            // The transcriber answered but could not read the settings: say so
            // rather than show nothing. It applies the settings in force at Start.
            Text("Meeting consent settings unavailable: \(settings.message.isEmpty ? "try again" : settings.message)")
                .font(.caption2).foregroundColor(.orange)
                .fixedSize(horizontal: false, vertical: true)
        } else if s.consentFetchInFlight {
            Text("Checking meeting consent settings…")
                .font(.caption2).foregroundColor(.secondary)
        }
    }

    private func panel(_ settings: Consent.Settings) -> some View {
        let optIn = settings.mode == .optIn
        return VStack(alignment: .leading, spacing: 6) {
            Label("Meeting consent", systemImage: "person.2.wave.2")
                .font(.subheadline.weight(.semibold))
            if !settings.supported {
                Text(settings.reason)
                    .font(.caption).foregroundColor(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(optIn
                 ? "Nothing is recorded or transcribed until everyone has agreed. Read this out or paste it into the meeting chat:"
                 : "Tell everyone in the meeting that it is recorded — read this out or paste it into the chat:")
                .font(.caption2).foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Text(s.consentNoticeText)
                .font(.caption)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .padding(6)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.08)))
            Button(action: s.copyConsentNotice) {
                Label("Copy notice", systemImage: "doc.on.doc")
            }
            .font(.caption)

            if !s.isStreaming {
                Toggle(isOn: $s.consentStep.confirmed) {
                    Text(optIn
                         ? "Everyone in the meeting agreed to it being recorded and transcribed"
                         : "I have told everyone in the meeting, with the notice above")
                        .font(.caption)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .toggleStyle(.checkbox)
                Picker(optIn ? "How everyone agreed" : "How they were told", selection: $s.consentStep.method) {
                    Text("Choose…").tag(Consent.Method?.none)
                    ForEach(Consent.Method.allCases) { method in
                        Text(method.label).tag(Consent.Method?.some(method))
                    }
                }
                .pickerStyle(.menu)
                .font(.caption)
                TextField("Note (optional)", text: $s.consentStep.statement)
                    .textFieldStyle(.roundedBorder)
                    .font(.caption)
            }

            HStack(spacing: 6) {
                Button(action: s.captureConsentEvidence) {
                    Label("Capture evidence", systemImage: "camera")
                }
                .disabled(s.capturingEvidence)
                .help("Keeps one frame of \(s.videoSourceName) on the meeting as consent evidence")
                Text(s.videoSourceName)
                    .font(.caption2).foregroundColor(.secondary)
                    .lineLimit(1).truncationMode(.middle)
                if s.consentEvidenceJPEG != nil && !s.isStreaming {
                    Button("Remove") { s.clearConsentEvidence() }.font(.caption)
                }
            }
            .font(.caption)
            if let preview = s.consentEvidencePreview {
                Image(decorative: preview, scale: 1)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
                    .frame(maxHeight: 90)
            }
            if !s.consentEvidenceStatus.isEmpty {
                Text(s.consentEvidenceStatus)
                    .font(.caption2).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !s.consentStatus.isEmpty {
                Text(s.consentStatus)
                    .font(.caption2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let why = s.consentBlockedReason {
                Text(why)
                    .font(.caption).foregroundColor(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if s.isStreaming {
                HStack(spacing: 6) {
                    TextField("Their name (optional)", text: $s.objectorName)
                        .textFieldStyle(.roundedBorder)
                    Button(action: s.reportObjection) {
                        Label("Participant objected", systemImage: "hand.raised")
                    }
                    .help("Stops recording and transcription now, and records the objection on the meeting")
                }
                .font(.caption)
            }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.accentColor.opacity(0.06)))
    }
}

/// JPEG encoding for evidence screenshots, lowering the quality until the image
/// fits what the consent evidence checker reads.
enum EvidenceImage {
    static func jpeg(_ image: CGImage) -> Data? {
        for quality in [0.85, 0.7, 0.55, 0.4] {
            let data = NSMutableData()
            guard let destination = CGImageDestinationCreateWithData(data, "public.jpeg" as CFString, 1, nil) else {
                return nil
            }
            CGImageDestinationAddImage(
                destination, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
            guard CGImageDestinationFinalize(destination) else { return nil }
            if data.length <= Consent.maxEvidenceBytes { return data as Data }
        }
        return nil
    }
}
#endif
