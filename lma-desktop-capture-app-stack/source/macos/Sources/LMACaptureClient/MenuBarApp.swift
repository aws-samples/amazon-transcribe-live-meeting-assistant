#if canImport(SwiftUI) && canImport(AppKit)
import SwiftUI
import AppKit
import ServiceManagement
import UserNotifications

/// Menu-bar (tray) UI for the LMA Desktop Capture App.
///
/// Lives in the macOS menu bar via NSStatusItem. Clicking the icon opens a
/// popover with: sign in/out, start/stop/pause, mute mic, mute system audio,
/// live per-channel level meters, elapsed recording time, and "Open in LMA".
/// While streaming, the menu-bar icon turns red (with the elapsed time beside
/// it) so recording is obvious at a glance. Settings live in their own window.
///
/// This is a thin view layer over CaptureController (the same engine the CLI
/// uses). It is only compiled/launched when the app starts with NO CLI args
/// (see main.swift); passing --username/--selftest/etc. keeps the headless CLI.

@available(macOS 13.0, *)
final class MenuBarAppState: ObservableObject {
    @Published var state: CaptureController.State = .idle
    @Published var meetingLevel: Float = 0
    @Published var micLevel: Float = 0
    @Published var connected = false
    @Published var paused = false
    @Published var micMuted = false
    @Published var meetingMuted = false
    @Published var username = ""
    @Published var password = ""
    @Published var meetingName = ""
    @Published var lastLog = ""

    @Published var rememberLogin = false

    // Settings (own window, opened from the gear): per-channel speaker labels,
    // mic device, screen video. Persisted in UserDefaults; applied to the
    // controller so they ride the START frame.
    @Published var micLabel = ""
    @Published var systemLabel = ""
    @Published var micDeviceUID = ""            // "" = System Default
    @Published var micDevices: [MicDevices.Device] = []

    // Optional desktop-video capture (screen recording streamed alongside
    // audio; saved as an MP4 in LMA). Off by default — opt-in per user.
    @Published var videoEnabled = false
    @Published var videoSourceID = ""           // "" = main display
    @Published var videoSources: [VideoCapture.Source] = []

    // Recording-consent disclaimer: shown once, before the FIRST recording ever
    // starts on this machine (same pattern as the browser extension's popup).
    // Agreement is persisted; Cancel just doesn't start.
    @Published var showDisclaimer = false

    /// Wall-clock seconds since the current recording started (0 when idle).
    /// Drives the elapsed-time readout in the panel and the menu-bar tooltip.
    @Published var elapsedSeconds = 0
    // Elapsed RECORDED time, which excludes paused spans — the audio recording
    // skips paused audio, so wall-clock would overstate the recording's length
    // (a 10-minute meeting paused for 8 would have reported 10:00).
    private var segmentStartedAt: Date?
    private var accumulatedSeconds = 0
    private var elapsedTimer: Timer?

    /// Recently used meeting names (most recent first), for quick re-selection.
    @Published var recentMeetingNames: [String] = []
    private static let kRecentMeetings = "lma.recentMeetingNames"
    private static let maxRecentMeetings = 8

    let controller: CaptureController

    /// Per-stack preferences store, so settings, the remembered email, and the
    /// recording-consent record never leak between LMA deployments.
    ///
    /// IMPORTANT: the suite name must NOT equal the app's own bundle identifier.
    /// macOS explicitly rejects that ("Using your own bundle identifier as an
    /// NSUserDefaults suite name does not make sense and will not work") and
    /// `UserDefaults(suiteName:)` returns nil — which would silently fall back to
    /// `.standard` and defeat the separation entirely. Hence the `.settings.`
    /// infix: it keeps the suite distinct from the bundle id that make-app.sh
    /// assigns (com.amazon.lma.captureclient.<slug>).
    private let defaults: UserDefaults

    private static func defaultsSuite(for config: Config) -> UserDefaults {
        let slug = config.stackSlug
        guard !slug.isEmpty else { return .standard }
        let suiteName = "com.amazon.lma.captureclient.settings.\(slug)"
        guard let suite = UserDefaults(suiteName: suiteName) else {
            // Should not happen now that the name differs from the bundle id,
            // but never fail silently: say so, because the fallback shares
            // settings across stacks.
            FileHandle.standardError.write(
                "⚠ couldn't open preferences suite \(suiteName); settings will not be per-stack\n"
                    .data(using: .utf8)!)
            return .standard
        }
        return suite
    }

    // UserDefaults keys for the optional "remember login id" feature. Only the
    // username (email) is stored — never the password, which stays in memory.
    private static let kRemember = "lma.rememberLogin"
    private static let kUsername = "lma.savedUsername"
    private static let kMicLabel = "lma.micLabel"
    private static let kSystemLabel = "lma.systemLabel"
    private static let kMicDeviceUID = "lma.micDeviceUID"
    private static let kVideoEnabled = "lma.videoEnabled"
    private static let kVideoSourceID = "lma.videoSourceID"
    private static let kDisclaimerAgreed = "lma.disclaimerAgreed"
    private static let kDisclaimerAgreedAt = "lma.disclaimerAgreedAt"
    private static let kDisclaimerAgreedText = "lma.disclaimerAgreedText"

    /// Default label for the system channel when the user hasn't set one.
    static let defaultSystemLabel = "Other participants"

    init(controller: CaptureController) {
        self.controller = controller
        let defaults = Self.defaultsSuite(for: controller.config)
        self.defaults = defaults
        self.rememberLogin = defaults.bool(forKey: Self.kRemember)
        // Prefill the login id from the remembered value, else the config.
        self.username = rememberLogin ? (defaults.string(forKey: Self.kUsername) ?? "") : controller.config.username
        self.micLabel = defaults.string(forKey: Self.kMicLabel) ?? ""
        self.systemLabel = defaults.string(forKey: Self.kSystemLabel) ?? ""
        self.micDeviceUID = defaults.string(forKey: Self.kMicDeviceUID) ?? ""
        self.videoEnabled = defaults.bool(forKey: Self.kVideoEnabled)
        self.videoSourceID = defaults.string(forKey: Self.kVideoSourceID) ?? ""
        self.recentMeetingNames = defaults.stringArray(forKey: Self.kRecentMeetings) ?? []
        pushSettingsToController()
        // The video source can vanish mid-recording (window closed) — surface
        // that rather than silently falling back to the full screen.
        controller.onVideoFallback = { [weak self] message in
            self?.lastLog = message
            Notifier.notify(title: "Screen video changed", body: message)
        }
        controller.onStateChange = { [weak self] s in
            guard let self = self else { return }
            let wasStreaming = self.isStreaming
            self.state = s
            let nowStreaming = self.isStreaming
            if nowStreaming && !wasStreaming {
                self.beginElapsedTimer()
            } else if !nowStreaming && wasStreaming {
                // An error (bad token, capture failure) or sign-out means nothing
                // was uploaded — don't tell the user a recording is on its way.
                let failed: Bool
                if case .error = s { failed = true } else { failed = false }
                self.endElapsedTimer(succeeded: !failed)
            }
        }
        controller.onLevels = { [weak self] m, k, c, p in
            self?.meetingLevel = m; self?.micLevel = k; self?.connected = c; self?.paused = p
        }
        controller.onLog = { [weak self] msg in self?.lastLog = msg }
    }

    // MARK: - Settings

    /// The mic label shown/used when the user hasn't customized one: their
    /// signed-in email, else the config's agentId ("Me").
    var effectiveMicLabel: String {
        if !micLabel.isEmpty { return micLabel }
        if !username.isEmpty { return username }
        return controller.config.agentId
    }
    var effectiveSystemLabel: String {
        systemLabel.isEmpty ? Self.defaultSystemLabel : systemLabel
    }

    func saveSettings() {
        let defaults = self.defaults
        defaults.set(micLabel, forKey: Self.kMicLabel)
        defaults.set(systemLabel, forKey: Self.kSystemLabel)
        defaults.set(micDeviceUID, forKey: Self.kMicDeviceUID)
        defaults.set(videoEnabled, forKey: Self.kVideoEnabled)
        defaults.set(videoSourceID, forKey: Self.kVideoSourceID)
        pushSettingsToController()
    }

    /// Refresh capturable displays/windows when the panel opens or the video
    /// toggle turns on (window lists go stale quickly).
    func refreshVideoSources() {
        Task { @MainActor in
            let sources = await VideoCapture.listSources()
            self.videoSources = sources
            // If the saved source disappeared (window closed), fall back to
            // the main display rather than showing a stale selection.
            if !self.videoSourceID.isEmpty && !sources.contains(where: { $0.id == self.videoSourceID }) {
                self.videoSourceID = ""
            }
        }
    }

    /// Refresh the device list each time the panel opens (hotplug-friendly).
    func refreshMicDevices() {
        micDevices = MicDevices.list()
        // If the saved device disappeared, show System Default rather than a
        // stale selection (capture already falls back at start).
        if !micDeviceUID.isEmpty && !micDevices.contains(where: { $0.uid == micDeviceUID }) {
            micDeviceUID = ""
        }
    }

    private func pushSettingsToController() {
        controller.micLabel = effectiveMicLabel
        controller.systemLabel = effectiveSystemLabel
        controller.micDeviceUID = micDeviceUID
        controller.videoEnabled = videoEnabled
        controller.videoSourceID = videoSourceID
    }

    var isStreaming: Bool { if case .streaming = state { return true }; return false }
    var isBusy: Bool { state == .signingIn || state == .starting || state == .stopping }

    func login() {
        let u = username, p = password
        persistLoginPreference()
        Task { _ = await controller.login(username: u, password: p); await MainActor.run { self.password = "" } }
    }

    func logout() {
        controller.logout()
        password = ""
        // Keep username prefilled only if the user opted to remember it.
        if !rememberLogin { username = "" }
    }

    /// Save (or clear) the remembered login id per the toggle. Called on login
    /// and whenever the toggle changes.
    func persistLoginPreference() {
        let defaults = self.defaults
        defaults.set(rememberLogin, forKey: Self.kRemember)
        if rememberLogin {
            defaults.set(username, forKey: Self.kUsername)
        } else {
            defaults.removeObject(forKey: Self.kUsername)
        }
    }

    // MARK: - Start at login (SMAppService, macOS 13+)

    /// Whether the app is registered to launch at login. Reflects the real
    /// system state, so it stays correct even if changed in System Settings.
    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            do {
                if newValue { try SMAppService.mainApp.register() }
                else { try SMAppService.mainApp.unregister() }
            } catch {
                lastLog = "Couldn't change Start-at-login: \(error.localizedDescription)"
            }
            objectWillChange.send()
        }
    }
    func start() {
        // One-time recording-consent gate (mirrors the browser extension): the
        // first Start on this machine shows the disclaimer; Agree persists and
        // proceeds, Cancel does nothing. The gate returns if the DEPLOYMENT'S
        // DISCLAIMER TEXT has changed since consent — the recorded consent
        // covers the text the user actually saw, not later revisions. (Users
        // who agreed before the text was recorded re-consent once.)
        let defaults = self.defaults
        let agreed = defaults.bool(forKey: Self.kDisclaimerAgreed)
        let agreedText = defaults.string(forKey: Self.kDisclaimerAgreedText)
        guard agreed, agreedText == controller.config.recordingDisclaimer else {
            showDisclaimer = true
            return
        }
        reallyStart()
    }

    /// Agree on the consent dialog: record WHAT was agreed to and WHEN, then
    /// start the recording that was gated.
    func agreeDisclaimerAndStart() {
        let defaults = self.defaults
        defaults.set(true, forKey: Self.kDisclaimerAgreed)
        defaults.set(Date(), forKey: Self.kDisclaimerAgreedAt)
        defaults.set(controller.config.recordingDisclaimer, forKey: Self.kDisclaimerAgreedText)
        showDisclaimer = false
        objectWillChange.send()   // consent record shown in Settings/reminder
        reallyStart()
    }

    /// When the user acknowledged the disclaimer, or nil if not yet (or if the
    /// consent predates timestamp recording).
    var disclaimerAgreedDate: Date? {
        defaults.object(forKey: Self.kDisclaimerAgreedAt) as? Date
    }

    /// The exact disclaimer text the user agreed to (for the consent record).
    var disclaimerAgreedText: String? {
        defaults.string(forKey: Self.kDisclaimerAgreedText)
    }

    static func consentDateString(_ d: Date) -> String {
        let f = DateFormatter()
        f.dateStyle = .medium; f.timeStyle = .short
        return f.string(from: d)
    }

    private func reallyStart() {
        // Push current settings (labels may depend on the signed-in email).
        pushSettingsToController()
        rememberMeetingName(meetingName)
        let name = meetingName.isEmpty ? "" : "\(meetingName) - \(Self.timestamp())"
        controller.start(callId: name.isEmpty ? nil : name)
    }
    func stop() { controller.stop() }
    func togglePause() {
        paused.toggle()
        controller.setPaused(paused)
        pauseElapsed(paused)
    }
    func toggleMic() { micMuted.toggle(); controller.setMicMuted(micMuted) }
    func toggleMeeting() { meetingMuted.toggle(); controller.setMeetingMuted(meetingMuted) }
    func openLMA() { if let u = controller.lmaURL() { NSWorkspace.shared.open(u) } }

    static func timestamp() -> String {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd HH:mm"; return f.string(from: Date())
    }

    // MARK: - Elapsed recording time

    private func beginElapsedTimer() {
        segmentStartedAt = Date()
        accumulatedSeconds = 0
        elapsedSeconds = 0
        elapsedTimer?.invalidate()
        elapsedTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.elapsedSeconds = self.currentElapsedSeconds()
        }
        Notifier.notify(
            title: "Recording started",
            body: videoEnabled
                ? "LMA is recording this meeting's audio and screen video."
                : "LMA is recording this meeting's audio.")
    }

    /// Ends the timer. `succeeded` is false when the recording stopped because of
    /// an error or sign-out, in which case nothing was uploaded and promising the
    /// user a recording would be a lie.
    private func endElapsedTimer(succeeded: Bool) {
        elapsedTimer?.invalidate()
        elapsedTimer = nil
        let duration = Self.formatElapsed(currentElapsedSeconds())
        segmentStartedAt = nil
        accumulatedSeconds = 0
        elapsedSeconds = 0
        guard succeeded else { return }
        // The recording lands in LMA shortly after END (the server uploads and
        // muxes at call end), so point the user at it rather than claiming it's
        // ready this instant.
        Notifier.notify(
            title: "Recording stopped (\(duration))",
            body: "Your meeting is being processed. Click to open it in LMA.",
            openURL: controller.lmaURL())
    }

    /// Recorded seconds so far: banked time plus the current unpaused span.
    private func currentElapsedSeconds() -> Int {
        guard let start = segmentStartedAt else { return accumulatedSeconds }
        return accumulatedSeconds + Int(Date().timeIntervalSince(start))
    }

    /// Called by togglePause so the clock tracks RECORDED time, not wall clock.
    private func pauseElapsed(_ paused: Bool) {
        if paused {
            if let start = segmentStartedAt {
                accumulatedSeconds += Int(Date().timeIntervalSince(start))
                segmentStartedAt = nil
            }
        } else if segmentStartedAt == nil {
            segmentStartedAt = Date()
        }
        elapsedSeconds = currentElapsedSeconds()
    }

    /// "7:12" / "1:03:44" — compact elapsed time.
    static func formatElapsed(_ seconds: Int) -> String {
        let h = seconds / 3600, m = (seconds % 3600) / 60, sec = seconds % 60
        return h > 0
            ? String(format: "%d:%02d:%02d", h, m, sec)
            : String(format: "%d:%02d", m, sec)
    }

    var elapsedText: String { Self.formatElapsed(elapsedSeconds) }

    // MARK: - Recent meeting names

    func clearRecentMeetingNames() {
        recentMeetingNames = []
        defaults.removeObject(forKey: Self.kRecentMeetings)
    }

    /// "LMA Capture Client v0.3.6.dev16 · stack LMA-Bob" — shown in the panel
    /// footer and the Settings window.
    var aboutLine: String {
        var parts: [String] = ["LMA Capture Client"]
        let v = controller.config.appVersion
        if !v.isEmpty { parts.append("v\(v)") }
        let stack = controller.config.stackName
        if !stack.isEmpty { parts.append("· stack \(stack)") }
        return parts.joined(separator: " ")
    }

    private func rememberMeetingName(_ name: String) {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        var list = recentMeetingNames.filter { $0 != trimmed }
        list.insert(trimmed, at: 0)
        if list.count > Self.maxRecentMeetings { list = Array(list.prefix(Self.maxRecentMeetings)) }
        recentMeetingNames = list
        defaults.set(list, forKey: Self.kRecentMeetings)
    }
}

/// Thin wrapper over UserNotifications so start/stop events are visible even
/// when the app has no window open — reinforcing that recording is in progress
/// (you can't record without noticing) and surfacing the finished recording.
///
/// Authorization is requested lazily on the first notification; if the user
/// declines, every call is a silent no-op (never an error path that could
/// interfere with recording).
@available(macOS 13.0, *)
enum Notifier {
    private static var requested = false

    /// UNUserNotificationCenter.current() raises NSInternalInconsistencyException
    /// ("bundleProxyForCurrentProcess is nil") when the process has no bundle —
    /// e.g. running .build/release/LMACaptureClient directly, which the README
    /// describes for development. That is an Objective-C exception, so it CANNOT
    /// be caught by Swift do/catch: it aborts the process (SIGABRT). Notifications
    /// are a nicety, so skip them entirely rather than crash mid-recording.
    static var isAvailable: Bool { Bundle.main.bundleIdentifier != nil }

    static func notify(title: String, body: String, openURL: URL? = nil) {
        guard isAvailable else { return }
        let center = UNUserNotificationCenter.current()
        let deliver = {
            let content = UNMutableNotificationContent()
            content.title = title
            content.body = body
            if let u = openURL {
                content.userInfo = ["lmaURL": u.absoluteString]
            }
            let req = UNNotificationRequest(
                identifier: UUID().uuidString, content: content, trigger: nil)
            center.add(req)
        }
        if requested {
            deliver()
            return
        }
        requested = true
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            if granted { deliver() }
        }
    }
}

@available(macOS 13.0, *)
struct MenuBarContentView: View {
    @ObservedObject var s: MenuBarAppState

    /// Opens the standalone Settings window (set by MenuBarController).
    var openSettings: () -> Void = {}

    var body: some View {
        // Scrollable body with a bounded height: the content grows (consent
        // gate, error text, meters), and a fixed-height container would clip it
        // with no way to reach the rest. ScrollView + a max height means content
        // is always reachable, never truncated.
        ScrollView {
            content
                .padding(14)
                // Let the width be driven by the container so the standalone
                // window can be resized wider; keep a sane minimum.
                .frame(minWidth: 280, maxWidth: .infinity, alignment: .leading)
        }
        .frame(minWidth: 300, maxHeight: 620)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Circle().fill(s.isStreaming ? .red : .secondary).frame(width: 10, height: 10)
                VStack(alignment: .leading, spacing: 1) {
                    Text("LMA Capture").font(.headline)
                    // Which LMA deployment this app talks to — essential once a
                    // user has the clients for more than one stack installed.
                    if !s.controller.config.stackName.isEmpty {
                        Text(s.controller.config.stackName)
                            .font(.caption2).foregroundColor(.secondary)
                            .lineLimit(1).truncationMode(.middle)
                    }
                }
                Spacer(minLength: 4)
                Text(statusText)
                    .font(.caption).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .multilineTextAlignment(.trailing)
                // Settings lives in its OWN window: the popover stays small and
                // predictable (it used to grow past the screen edge when the
                // settings section expanded), and settings get room to breathe.
                Button(action: openSettings) {
                    Image(systemName: "gearshape")
                }
                .buttonStyle(.plain)
                .foregroundColor(.secondary)
                .disabled(s.isStreaming)
                .help(s.isStreaming ? "Stop recording to change settings" : "Settings…")
            }

            Divider()

            if !s.controller.isAuthenticated {
                // Sign-in form
                TextField("Email", text: $s.username).textFieldStyle(.roundedBorder)
                SecureField("Password", text: $s.password).textFieldStyle(.roundedBorder)
                Toggle(isOn: Binding(get: { s.rememberLogin }, set: { v in
                    s.rememberLogin = v; s.persistLoginPreference()
                })) {
                    Text("Remember my email").font(.caption)
                }
                Button(action: s.login) {
                    HStack { if s.state == .signingIn { ProgressView().scaleEffect(0.6) }; Text("Sign In") }
                }
                .disabled(s.username.isEmpty || s.password.isEmpty || s.state == .signingIn)
                .keyboardShortcut(.defaultAction)
                if case .error(let m) = s.state {
                    Text(m).font(.caption2).foregroundColor(.red).lineLimit(3)
                }
            } else {
                // Streaming controls
                if s.showDisclaimer {
                    // One-time recording-consent gate (same text and Agree/Cancel
                    // shape as the browser extension's popup). Shown in place of
                    // the Start controls so it can't be missed or clicked past.
                    VStack(alignment: .leading, spacing: 8) {
                        Label("Important", systemImage: "exclamationmark.triangle.fill")
                            .font(.headline).foregroundColor(.orange)
                        Text(s.controller.config.recordingDisclaimer)
                            .font(.caption)
                            .fixedSize(horizontal: false, vertical: true)
                        if s.videoEnabled {
                            Label("Screen video is ON: your selected screen or window is also recorded.",
                                  systemImage: "video.fill")
                                .font(.caption2).foregroundColor(.orange)
                        }
                        HStack {
                            Spacer()
                            Button("Cancel") { s.showDisclaimer = false }
                            Button("Agree") { s.agreeDisclaimerAndStart() }
                                .keyboardShortcut(.defaultAction)
                        }
                    }
                    .padding(10)
                    .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.08)))
                } else if !s.isStreaming {
                    HStack(spacing: 4) {
                        TextField("Meeting name (optional)", text: $s.meetingName)
                            .textFieldStyle(.roundedBorder)
                        // Recent meeting names — recurring meetings are the norm,
                        // so make re-picking one a click instead of retyping.
                        if !s.recentMeetingNames.isEmpty {
                            Menu {
                                ForEach(s.recentMeetingNames, id: \.self) { name in
                                    Button(name) { s.meetingName = name }
                                }
                                Divider()
                                Button("Clear recent") { s.clearRecentMeetingNames() }
                            } label: {
                                Image(systemName: "clock.arrow.circlepath")
                            }
                            .menuStyle(.borderlessButton)
                            .frame(width: 24)
                            .help("Recent meeting names")
                        }
                    }
                    Button(action: s.start) {
                        Label("Start Recording", systemImage: "record.circle").frame(maxWidth: .infinity)
                    }
                    .disabled(s.isBusy)
                    .keyboardShortcut(.defaultAction)
                    // Persistent consent reminder: the full disclaimer is a
                    // one-time gate, but the obligation is per-meeting — keep a
                    // one-liner next to Start so it stays visible. It reflects
                    // the recorded consent (date on hover, alongside the exact
                    // text agreed to); the full record lives in Settings (⚙).
                    Label("Ensure all participants have consented to recording.",
                          systemImage: "checkmark.shield")
                        .font(.caption2).foregroundColor(.secondary)
                        .help(consentTooltip)
                } else {
                    // Live meters
                    LevelBar(label: "System", level: s.meetingLevel, muted: s.meetingMuted)
                    LevelBar(label: "Mic", level: s.micLevel, muted: s.micMuted)
                    HStack {
                        Text(s.connected ? "● Live" : "○ Reconnecting…")
                            .font(.caption).foregroundColor(s.connected ? .green : .orange)
                        // Elapsed recording time — monospaced so the width
                        // doesn't jitter as the digits change.
                        Text(s.elapsedText)
                            .font(.caption.monospacedDigit())
                            .foregroundColor(.secondary)
                        if s.paused { Text("· Paused").font(.caption).foregroundColor(.orange) }
                        if s.controller.isVideoActive {
                            Image(systemName: "video.fill")
                                .font(.caption).foregroundColor(.secondary)
                                .help("Screen video is being recorded")
                        }
                        Spacer()
                    }
                    HStack {
                        Button(action: s.togglePause) {
                            Label(s.paused ? "Resume" : "Pause", systemImage: s.paused ? "play.fill" : "pause.fill")
                        }
                        Button(action: s.stop) {
                            Label("Stop", systemImage: "stop.fill")
                        }
                    }
                    HStack {
                        Toggle(isOn: Binding(get: { s.micMuted }, set: { _ in s.toggleMic() })) {
                            Label("Mute mic", systemImage: s.micMuted ? "mic.slash" : "mic")
                        }
                        Toggle(isOn: Binding(get: { s.meetingMuted }, set: { _ in s.toggleMeeting() })) {
                            Label("Mute system", systemImage: s.meetingMuted ? "speaker.slash" : "speaker.wave.2")
                        }
                    }.toggleStyle(.button).font(.caption)
                }

                Divider()
                Button(action: s.openLMA) {
                    Label(s.isStreaming ? "Open this meeting in LMA" : "Open LMA meetings", systemImage: "safari")
                }
                if !s.isStreaming {
                    Toggle(isOn: Binding(get: { s.launchAtLogin }, set: { s.launchAtLogin = $0 })) {
                        Text("Start automatically at login").font(.caption)
                    }
                    Button(action: s.logout) {
                        Label("Sign out", systemImage: "rectangle.portrait.and.arrow.right")
                    }
                }
            }

            if !s.lastLog.isEmpty {
                Text(s.lastLog)
                    .font(.caption2).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Divider()
            // Quit lives in the right-click menu (not here) so it can't be
            // mistaken for Stop. Leave the app running in the background between
            // meetings; it uses no audio/CPU when idle.
            Text("Right-click the menu-bar icon to Quit. Leave it running in the background between meetings.")
                .font(.caption2).foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            // About line: version + stack, so support questions ("which build /
            // which deployment?") are answerable at a glance.
            Text(s.aboutLine)
                .font(.caption2).foregroundColor(.secondary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// Hover text for the standing reminder: the full disclaimer, prefixed with
    /// the recorded consent date when we have one.
    private var consentTooltip: String {
        let text = s.disclaimerAgreedText ?? s.controller.config.recordingDisclaimer
        if let d = s.disclaimerAgreedDate {
            return "You agreed to this on \(MenuBarAppState.consentDateString(d)):\n\n\(text)"
        }
        return text
    }

    private var statusText: String {
        switch s.state {
        case .idle: return "Not signed in"
        case .signingIn: return "Signing in…"
        case .authenticated: return "Ready"
        case .starting: return "Starting…"
        case .streaming: return "Recording"
        case .stopping: return "Stopping…"
        case .error: return "Error"
        }
    }
}

/// Settings, hosted in its OWN resizable window (opened from the gear).
///
/// Why a separate window rather than an expanding section in the popover: the
/// settings surface keeps growing (labels, mic, video source, consent record),
/// and inline expansion pushed the popover past the screen edge with no way to
/// scroll or resize. A window is also the platform-native place for settings.
@available(macOS 13.0, *)
struct SettingsWindowView: View {
    @ObservedObject var s: MenuBarAppState

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                SettingsView(s: s)
                Divider()
                Text(s.aboutLine)
                    .font(.caption2).foregroundColor(.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(18)
            .frame(minWidth: 320, maxWidth: .infinity, alignment: .leading)
        }
        .frame(minWidth: 360, minHeight: 320)
    }
}

/// Settings body: speaker labels for the two channels, mic picker, screen-video
/// options, and the recording-consent record. Saved on every edit (no Apply).
@available(macOS 13.0, *)
struct SettingsView: View {
    @ObservedObject var s: MenuBarAppState

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Speaker labels").font(.caption).foregroundColor(.secondary)
            HStack {
                Text("My mic").font(.caption).frame(width: 70, alignment: .leading)
                TextField(s.username.isEmpty ? "Me" : s.username,
                          text: Binding(get: { s.micLabel }, set: { s.micLabel = $0; s.saveSettings() }))
                    .textFieldStyle(.roundedBorder).font(.caption)
            }
            HStack {
                Text("System").font(.caption).frame(width: 70, alignment: .leading)
                TextField(MenuBarAppState.defaultSystemLabel,
                          text: Binding(get: { s.systemLabel }, set: { s.systemLabel = $0; s.saveSettings() }))
                    .textFieldStyle(.roundedBorder).font(.caption)
            }
            Text("These appear as the speaker names in the LMA transcript. Leave blank for the defaults shown.")
                .font(.caption2).foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            Text("Microphone").font(.caption).foregroundColor(.secondary).padding(.top, 2)
            Picker("", selection: Binding(get: { s.micDeviceUID }, set: { s.micDeviceUID = $0; s.saveSettings() })) {
                Text("System Default").tag("")
                ForEach(s.micDevices) { d in
                    Text(d.name).tag(d.uid)
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .font(.caption)
            Text("System Default follows your Sound settings. If a chosen mic is unplugged, recording falls back to the default.")
                .font(.caption2).foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            // Optional desktop-video capture: streams the chosen screen/window
            // alongside audio; LMA saves a video recording of the meeting.
            Text("Screen video").font(.caption).foregroundColor(.secondary).padding(.top, 2)
            Toggle(isOn: Binding(get: { s.videoEnabled }, set: { v in
                s.videoEnabled = v; s.saveSettings()
                if v { s.refreshVideoSources() }
            })) {
                Text("Also record screen video").font(.caption)
            }
            if s.videoEnabled {
                Picker("", selection: Binding(get: { s.videoSourceID }, set: { s.videoSourceID = $0; s.saveSettings() })) {
                    Text("Entire screen").tag("")
                    ForEach(s.videoSources.filter { $0.id != "" }) { src in
                        Text(src.name).tag(src.id)
                    }
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .font(.caption)
                Text("The selected screen or window is recorded with the meeting and saved as a video in LMA. Uses the Screen Recording permission you already granted.")
                    .font(.caption2).foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            }

            // Consent record: when + what the user agreed to, so the one-time
            // acknowledgment is inspectable afterwards (auditable, not a
            // fire-and-forget dialog). Collapsed by default to keep the panel
            // compact; absent entirely until the user has agreed.
            if let d = s.disclaimerAgreedDate {
                Text("Recording consent").font(.caption).foregroundColor(.secondary).padding(.top, 2)
                DisclosureGroup {
                    Text(s.disclaimerAgreedText ?? s.controller.config.recordingDisclaimer)
                        .font(.caption2).foregroundColor(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 2)
                } label: {
                    Label("Agreed \(MenuBarAppState.consentDateString(d))", systemImage: "checkmark.seal")
                        .font(.caption2).foregroundColor(.secondary)
                }
            }
        }
    }
}

@available(macOS 13.0, *)
struct LevelBar: View {
    let label: String
    let level: Float
    let muted: Bool
    var body: some View {
        HStack(spacing: 6) {
            Text(label).font(.caption).frame(width: 46, alignment: .leading)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 3).fill(Color.secondary.opacity(0.2))
                    RoundedRectangle(cornerRadius: 3)
                        .fill(muted ? Color.gray : Color.green)
                        // ×3 gain for visibility, matches the CLI meter bar
                        .frame(width: geo.size.width * CGFloat(min(1, max(0, level * 3))))
                }
            }.frame(height: 8)
            if muted { Image(systemName: "slash.circle").font(.caption2).foregroundColor(.gray) }
        }
    }
}

/// AppKit glue: creates the status-bar item + popover and hosts the SwiftUI view.
///
/// The app is also a REGULAR Dock app (not LSUIElement): on notched MacBooks a
/// crowded menu bar silently hides status items — and starting a recording adds
/// the system's orange mic indicator, which can push OUR icon out of view at
/// the exact moment recording begins. The Dock tile is the always-visible
/// fallback: it shows a "REC" badge while recording, its right-click menu has
/// Start/Pause/Stop, and clicking it opens the same panel as the menu-bar icon.
@available(macOS 13.0, *)
final class MenuBarController: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private var panelWindow: NSWindow?
    private var settingsWindow: NSWindow?
    private let state: MenuBarAppState
    private var pollTimer: Timer?

    init(state: MenuBarAppState) { self.state = state }

    /// The content view, with the Settings action wired to open the standalone
    /// settings window (each host — popover and panel window — gets its own
    /// instance, so the closure is attached per construction).
    private func makeContentView() -> MenuBarContentView {
        var v = MenuBarContentView(s: state)
        v.openSettings = { [weak self] in self?.showSettingsWindow() }
        return v
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Clicking a start/stop notification should bring up the app (and for
        // the post-stop one, open the meeting in LMA). Guarded for the same
        // reason as Notifier.notify: current() aborts without a bundle id.
        if Notifier.isAvailable {
            UNUserNotificationCenter.current().delegate = self
        }

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        // Always give the button a visible text title as a fallback so the item
        // is never zero-width even if the SF Symbol image fails to load.
        statusItem.button?.title = "LMA"
        updateIcon()

        popover = NSPopover()
        popover.behavior = .transient
        // Size the popover to its CONTENT rather than a fixed 300x360 box: the
        // panel grows (consent gate, errors, meters) and a fixed height clipped
        // the top/bottom with no way to scroll or resize. NSHostingController
        // reports a SwiftUI-derived fitting size, and the view's own ScrollView
        // caps how tall that can get.
        let host = NSHostingController(rootView: makeContentView())
        host.sizingOptions = [.preferredContentSize]
        popover.contentViewController = host

        // Left-click opens the popover; right-click (or control-click) shows a
        // small menu with Quit — kept OUT of the popover so it can't be confused
        // with Stop.
        statusItem.button?.action = #selector(statusButtonClicked)
        statusItem.button?.target = self
        statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])

        // Repaint the menu-bar icon (red while recording) + Dock badge as state
        // changes.
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.updateIcon()
            self?.updateDockBadge()
        }
    }

    // MARK: - Dock integration

    /// Recording state on the Dock tile, visible even when the menu-bar icon is
    /// hidden under the notch: a "REC" badge ("⏸" when paused) AND a red dot
    /// drawn onto the icon itself, so the state change is obvious at Dock size.
    private var lastBadge = ""
    private var recordingDockView: RecordingDockTileView?
    private func updateDockBadge() {
        let badge = state.isStreaming ? (state.paused ? "⏸" : "REC") : ""
        if badge != lastBadge {
            NSApp.dockTile.badgeLabel = badge
            if state.isStreaming, recordingDockView == nil {
                let v = RecordingDockTileView(frame: NSRect(x: 0, y: 0, width: 128, height: 128))
                recordingDockView = v
                NSApp.dockTile.contentView = v
            } else if !state.isStreaming {
                recordingDockView = nil
                NSApp.dockTile.contentView = nil // restore the normal app icon
            }
            NSApp.dockTile.display()
            lastBadge = badge
        }
    }

    /// Clicking the Dock tile (with no windows open) opens the control panel as
    /// a regular window. The popover variant is unusable here: it anchors to the
    /// status-item button, which may be hidden under the notch — the very
    /// problem the Dock presence exists to solve.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { showPanelWindow() }
        return true
    }

    /// Right-click Dock menu: recording controls that work sight-unseen.
    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let menu = NSMenu()
        if state.isStreaming {
            let pause = NSMenuItem(
                title: state.paused ? "Resume Recording" : "Pause Recording",
                action: #selector(dockTogglePause), keyEquivalent: "")
            pause.target = self
            menu.addItem(pause)
            let stop = NSMenuItem(title: "Stop Recording", action: #selector(dockStop), keyEquivalent: "")
            stop.target = self
            menu.addItem(stop)
        } else if state.controller.isAuthenticated {
            let start = NSMenuItem(title: "Start Recording", action: #selector(dockStart), keyEquivalent: "")
            start.target = self
            menu.addItem(start)
        }
        let open = NSMenuItem(title: "Open Control Panel", action: #selector(dockOpenPanel), keyEquivalent: "")
        open.target = self
        menu.addItem(open)
        return menu
    }

    @objc private func dockTogglePause() { state.togglePause() }
    @objc private func dockStop() { state.stop() }
    @objc private func dockStart() {
        state.start()
        // First-ever recording: start() gates on the consent disclaimer instead
        // of starting. Surface the panel so the dialog is actually visible.
        if state.showDisclaimer { showPanelWindow() }
    }
    @objc private func dockOpenPanel() { showPanelWindow() }

    /// The same SwiftUI content as the popover, hosted in a titled, RESIZABLE
    /// window for Dock-initiated opens. Closing it leaves the app running
    /// (standard for menu-bar/Dock hybrid utilities).
    ///
    /// `.resizable` matters: content height varies with state, and a fixed
    /// window clipped the top/bottom with no recourse. The view also scrolls, so
    /// between the two the content is always reachable.
    private func showPanelWindow() {
        if let w = panelWindow {
            w.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let w = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 340, height: 460),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        w.title = state.controller.config.appDisplayName
        w.contentViewController = NSHostingController(rootView: makeContentView())
        w.contentMinSize = NSSize(width: 300, height: 240)
        w.isReleasedWhenClosed = false
        w.center()
        panelWindow = w
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Settings in a dedicated, resizable window (opened from the gear). Keeps
    /// the popover small and gives the growing settings surface room.
    private func showSettingsWindow() {
        state.refreshMicDevices()
        if state.videoEnabled { state.refreshVideoSources() }
        if let w = settingsWindow {
            w.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let w = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 480),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        w.title = state.controller.config.stackName.isEmpty
            ? "LMA Capture Settings"
            : "LMA Capture Settings — \(state.controller.config.stackName)"
        w.contentViewController = NSHostingController(rootView: SettingsWindowView(s: state))
        w.contentMinSize = NSSize(width: 360, height: 280)
        w.isReleasedWhenClosed = false
        w.center()
        settingsWindow = w
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Build the right-click menu (Quit + a hint line). Rebuilt on demand so it
    /// stays simple; also reachable if the popover is ever unavailable.
    private func makeContextMenu() -> NSMenu {
        let menu = NSMenu()
        let name = state.controller.config.appDisplayName
        let openItem = NSMenuItem(title: "Open \(name)", action: #selector(openPopoverFromMenu), keyEquivalent: "")
        openItem.target = self
        menu.addItem(openItem)
        let settingsItem = NSMenuItem(title: "Settings…", action: #selector(openSettingsFromMenu), keyEquivalent: ",")
        settingsItem.target = self
        settingsItem.isEnabled = !state.isStreaming
        menu.addItem(settingsItem)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit \(name)", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        return menu
    }

    @objc private func openSettingsFromMenu() { showSettingsWindow() }

    // MARK: - Notification clicks

    /// Tapping a notification opens the panel; the post-stop notification also
    /// carries the meeting's LMA URL so "your recording is ready" is one click
    /// from actually seeing it.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if let urlString = response.notification.request.content.userInfo["lmaURL"] as? String,
           let url = URL(string: urlString) {
            NSWorkspace.shared.open(url)
        } else {
            showPanelWindow()
        }
        completionHandler()
    }

    /// Show our notifications even when the app is frontmost — the recording
    /// start/stop signal is the point, and suppressing it would defeat it.
    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    @objc private func quitApp() { NSApp.terminate(nil) }

    @objc private func openPopoverFromMenu() { showPopover() }

    private func updateIcon() {
        guard let button = statusItem.button else { return }
        let recording = state.isStreaming

        // The label is ALWAYS "LMA". Recording state is shown solely by the icon
        // in front of it: a red filled circle while recording, else the (template,
        // auto-tinted) waveform glyph. This avoids the earlier black "●REC" text.
        if recording {
            // Bake red directly into the symbol via a palette configuration and
            // mark it NON-template so AppKit renders that color as-is (relying on
            // contentTintColor did not work on the status button).
            let cfg = NSImage.SymbolConfiguration(paletteColors: [.systemRed])
            let img = NSImage(systemSymbolName: "record.circle.fill", accessibilityDescription: "LMA recording")?
                .withSymbolConfiguration(cfg)
            img?.isTemplate = false
            button.image = img
            button.contentTintColor = .systemRed
        } else {
            let img = NSImage(systemSymbolName: "waveform.circle",
                              accessibilityDescription: state.controller.config.appDisplayName)
            img?.isTemplate = true
            button.image = img
            button.contentTintColor = nil
        }
        // While recording, the title carries the elapsed time so you can see how
        // long you've been recording without opening anything.
        button.attributedTitle = NSAttributedString(
            string: recording ? " LMA \(state.elapsedText)" : " LMA",
            attributes: [.font: NSFont.menuBarFont(ofSize: 0)])
        button.imagePosition = .imageLeading
        // Hover text names the stack (which deployment is this?) and the
        // recording state/duration.
        let stack = state.controller.config.stackName
        var tip = state.controller.config.appDisplayName
        if recording {
            tip += state.paused
                ? " — paused at \(state.elapsedText)"
                : " — recording \(state.elapsedText)"
        } else if !stack.isEmpty {
            tip += " — idle"
        }
        button.toolTip = tip
    }

    @objc private func statusButtonClicked() {
        // Right-click (or control-click) → context menu with Quit; left-click →
        // toggle the popover. Popping up a menu on the status item briefly
        // overrides its action, so we attach, pop, then detach.
        let event = NSApp.currentEvent
        let isRightClick = event?.type == .rightMouseUp
            || (event?.modifierFlags.contains(.control) ?? false)
        if isRightClick {
            let menu = makeContextMenu()
            statusItem.menu = menu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
        } else {
            togglePopover()
        }
    }

    private func togglePopover() {
        if popover.isShown { popover.performClose(nil) } else { showPopover() }
    }

    private func showPopover() {
        guard let button = statusItem.button else { return }
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
        NSApp.activate(ignoringOtherApps: true)
    }
}

/// Dock tile content while recording: the app icon dimmed slightly with a red
/// recording dot in the lower-right corner. NSDockTile has no "tint" API, so a
/// custom contentView drawing the icon + overlay is the supported way to make
/// the Dock icon itself reflect recording state.
@available(macOS 13.0, *)
final class RecordingDockTileView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        NSApp.applicationIconImage?.draw(in: bounds)
        let d = bounds.width * 0.30
        let dotRect = NSRect(x: bounds.maxX - d - bounds.width * 0.06,
                             y: bounds.width * 0.06, width: d, height: d)
        // White ring behind the dot so it reads against any icon artwork.
        NSColor.white.setFill()
        NSBezierPath(ovalIn: dotRect.insetBy(dx: -2, dy: -2)).fill()
        NSColor.systemRed.setFill()
        NSBezierPath(ovalIn: dotRect).fill()
    }
}

/// Entry point for GUI mode. Runs a standard AppKit run loop as a REGULAR app
/// (Dock icon + menu bar item — see MenuBarController docs for why).
@available(macOS 13.0, *)
func runMenuBarApp(config: Config) -> Never {
    let app = NSApplication.shared
    app.setActivationPolicy(.regular) // Dock icon + menu-bar item
    let controller = CaptureController(config: config)
    let state = MenuBarAppState(controller: controller)
    let delegate = MenuBarController(state: state)
    app.delegate = delegate
    // Retain the delegate for the process lifetime.
    objc_setAssociatedObject(app, "lmaDelegate", delegate, .OBJC_ASSOCIATION_RETAIN)
    app.run()
    exit(0)
}
#endif
