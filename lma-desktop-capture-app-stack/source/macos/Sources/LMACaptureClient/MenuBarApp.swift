#if canImport(SwiftUI) && canImport(AppKit)
import SwiftUI
import AppKit
import CoreGraphics
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
    /// Live previews for the source picker, keyed by source id. Populated
    /// asynchronously (each is a screen capture), so the picker renders
    /// immediately and thumbnails fill in — never blocking source selection.
    @Published var videoThumbnails: [String: CGImage] = [:]

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
    /// Turns the state stream into begin/end recording events (see
    /// CaptureController.SessionTracker).
    private var sessionTracker = CaptureController.SessionTracker()

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
            self.state = s
            // An error (rejected token, capture failure) means nothing was
            // uploaded — don't tell the user a recording is on its way. The
            // outcome is taken from the terminal state AFTER teardown, not from
            // the `.stopping` that every teardown passes through; see
            // CaptureController.SessionTracker for why that distinction matters.
            switch self.sessionTracker.observe(s) {
            case .none:
                break
            case .started:
                self.beginElapsedTimer()
            case .ended(let succeeded):
                self.endElapsedTimer(succeeded: succeeded)
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
    ///
    /// `withThumbnails` is only for the Settings picker, where previews are the
    /// point. Every thumbnail is a real screen capture, so the main panel's
    /// one-line summary asks for just the SELECTED source instead — otherwise
    /// merely opening the panel (including mid-recording) would capture a dozen
    /// screenshots.
    func refreshVideoSources(withThumbnails: Bool = false) {
        Task { @MainActor in
            let sources = await VideoCapture.listSources()
            self.videoSources = sources
            // If the saved source disappeared (window closed), fall back to
            // the main display rather than showing a stale selection.
            if !self.videoSourceID.isEmpty && !sources.contains(where: { $0.id == self.videoSourceID }) {
                self.videoSourceID = ""
            }
            // Drop previews for sources that no longer exist, so a closed
            // window's thumbnail can't linger and misrepresent the list.
            let liveIDs = Set(sources.map { $0.id })
            self.videoThumbnails = self.videoThumbnails.filter { liveIDs.contains($0.key) }
            if withThumbnails {
                self.loadThumbnails(for: sources)
            } else {
                self.loadSelectedThumbnail()
            }
        }
    }

    /// Capture a preview for each source, sequentially. Sequential (not
    /// concurrent) on purpose: each thumbnail is a real screen capture, and
    /// firing dozens at once competes with the audio/video capture already
    /// running. The `videoSources` identity check makes a stale pass abandon
    /// itself as soon as the list changes underneath it.
    @MainActor
    private func loadThumbnails(for sources: [VideoCapture.Source]) {
        Task { @MainActor in
            for src in sources.prefix(Self.maxThumbnails) {
                guard self.videoSources == sources else { return } // list moved on
                if let img = await VideoCapture.thumbnail(sourceID: src.id) {
                    self.videoThumbnails[src.id] = img
                }
            }
        }
    }

    /// One preview, for the selected source only — what the main panel's summary
    /// needs.
    @MainActor
    private func loadSelectedThumbnail() {
        let id = videoSourceID
        Task { @MainActor in
            if let img = await VideoCapture.thumbnail(sourceID: id) {
                // Ignore a late result for a source the user has since changed.
                guard self.videoSourceID == id else { return }
                self.videoThumbnails[id] = img
            }
        }
    }

    /// Cap on previews captured per refresh. Windows are listed in front-to-back
    /// order, so the first ones are the ones a user is most likely to pick; the
    /// rest still show name, size, and app icon.
    private static let maxThumbnails = 12

    /// Display name of the chosen video source, for the at-a-glance indicator on
    /// the main panel. Falls back to a generic label before the source list has
    /// loaded (or if the saved source has since vanished).
    var videoSourceName: String {
        if let s = videoSources.first(where: { $0.id == videoSourceID }) { return s.name }
        return videoSourceID.isEmpty ? "Main display" : "Selected source"
    }

    /// Display name of the chosen microphone, for the same indicator.
    var micDeviceName: String {
        if micDeviceUID.isEmpty { return "System Default" }
        if let d = micDevices.first(where: { $0.uid == micDeviceUID }) { return d.name }
        return "System Default"    // saved device not currently connected
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

    /// Where this instance is being displayed. The two hosts need genuinely
    /// different sizing behaviour, and getting it wrong produced both reported
    /// window bugs — see `body`.
    enum Host {
        /// Menu-bar popover: self-sizing, so the content must state an exact
        /// width and a height ceiling.
        case popover
        /// Standalone resizable window: the content fills whatever size the user
        /// chose, and only declares a floor.
        case window
    }
    var host: Host = .popover

    /// Popover width, and the window's opening width. Wide enough for the
    /// meeting-name field and the capture-inputs summary without wrapping every
    /// label.
    static let defaultWidth: CGFloat = 340
    /// Smallest usable window size, propagated to the window via
    /// NSHostingSizingOptions.minSize (NSWindow.contentMinSize is IGNORED once a
    /// contentViewController is set — verified: the window still shrank to
    /// 50×50 with contentMinSize set, and clamps correctly with .minSize).
    static let minWindowWidth: CGFloat = 320
    static let minWindowHeight: CGFloat = 260
    /// Height ceiling for the popover, so a tall state (consent gate + error +
    /// meters) scrolls instead of running off the screen edge.
    static let maxPopoverHeight: CGFloat = 620

    var body: some View {
        // The content grows and shrinks with state (consent gate, error text,
        // meters), so it always scrolls — a fixed-size container clipped the
        // top/bottom with no way to reach the rest.
        //
        // WIDTH is the subtle part. NSPopover adopts the hosting controller's
        // fitting size, and with `maxWidth: .infinity` SwiftUI reports its
        // *ideal* width — measured at 505pt for this content, which was the
        // "opens really wide" symptom. So the popover states an exact width.
        // A window, by contrast, should let the content fill whatever width the
        // user resized to, and only declare a minimum.
        ScrollView {
            content
                .padding(14)
                .frame(width: host == .popover ? Self.defaultWidth : nil, alignment: .leading)
                .frame(maxWidth: host == .window ? .infinity : nil, alignment: .leading)
        }
        .frame(
            minWidth: host == .window ? Self.minWindowWidth : nil,
            minHeight: host == .window ? Self.minWindowHeight : nil,
            maxHeight: host == .popover ? Self.maxPopoverHeight : nil)
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
                    // What will actually be captured, BEFORE pressing Start —
                    // the mic, and (when screen video is on) which screen or
                    // window. Previously both lived only in Settings, so you
                    // couldn't tell what you were about to record.
                    CaptureInputsView(s: s)
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
                    // Live meters, over the same inputs summary — while recording
                    // it answers "what is being captured right now?".
                    CaptureInputsView(s: s)
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

/// At-a-glance summary of what this app will capture: the microphone, and —
/// when screen video is enabled — which screen or window, with a live preview.
///
/// This exists because the selections live in the Settings window: without a
/// summary on the main panel you had to open Settings to answer "which mic?" and
/// "which screen am I about to share?". The video row deliberately includes a
/// thumbnail: for a user with two identical monitors, a name alone doesn't
/// distinguish them, and accidentally recording the wrong screen is a privacy
/// problem, not just an annoyance.
@available(macOS 13.0, *)
struct CaptureInputsView: View {
    @ObservedObject var s: MenuBarAppState

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 5) {
                Image(systemName: "mic").font(.caption2).foregroundColor(.secondary)
                Text(s.micDeviceName)
                    .font(.caption2).foregroundColor(.secondary)
                    .lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 0)
            }
            if s.videoEnabled {
                HStack(spacing: 5) {
                    Image(systemName: "video").font(.caption2).foregroundColor(.secondary)
                    SourceThumbnail(image: s.videoThumbnails[s.videoSourceID], width: 44, height: 28)
                    Text(s.videoSourceName)
                        .font(.caption2).foregroundColor(.secondary)
                        .lineLimit(2).truncationMode(.middle)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
            } else {
                HStack(spacing: 5) {
                    Image(systemName: "video.slash").font(.caption2).foregroundColor(.secondary)
                    Text("Screen video off").font(.caption2).foregroundColor(.secondary)
                    Spacer(minLength: 0)
                }
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.08)))
        .help(s.videoEnabled
              ? "Recording mic “\(s.micDeviceName)” and screen video from “\(s.videoSourceName)”. Change these in Settings (⚙)."
              : "Recording mic “\(s.micDeviceName)”. Screen video is off — enable it in Settings (⚙).")
        // Keep the summary honest while the panel is open: a mic can be
        // unplugged and a chosen window closed at any moment.
        .onAppear {
            s.refreshMicDevices()
            if s.videoEnabled { s.refreshVideoSources() }
        }
    }
}

/// A source preview, or a placeholder frame when no capture is available yet
/// (still loading, permission not granted, or macOS 13 where the screenshot API
/// doesn't exist). The placeholder keeps the row height stable so the list
/// doesn't jump as thumbnails arrive.
@available(macOS 13.0, *)
struct SourceThumbnail: View {
    let image: CGImage?
    var width: CGFloat = 96
    var height: CGFloat = 60

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 3).fill(Color.secondary.opacity(0.15))
            if let img = image {
                Image(decorative: img, scale: 1)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
            } else {
                Image(systemName: "display").font(.caption2).foregroundColor(.secondary)
            }
        }
        .frame(width: width, height: height)
        .clipShape(RoundedRectangle(cornerRadius: 3))
        .overlay(RoundedRectangle(cornerRadius: 3).strokeBorder(Color.secondary.opacity(0.25)))
        .accessibilityHidden(true)   // the adjacent name text carries the meaning
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
    /// Smallest usable size, propagated to the window via
    /// NSHostingSizingOptions.minSize (contentMinSize is ignored — see the note
    /// in MenuBarController.showPanelWindow).
    static let minWidth: CGFloat = 380
    static let minHeight: CGFloat = 320

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
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(minWidth: Self.minWidth, minHeight: Self.minHeight)
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
                if v { s.refreshVideoSources(withThumbnails: true) }
            })) {
                Text("Also record screen video").font(.caption)
            }
            if s.videoEnabled {
                VideoSourceListView(s: s)
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

/// Visual picker for the screen-video source: a scrollable list of thumbnail
/// rows, one per display and window, with the selected row clearly marked.
///
/// This replaces a text-only dropdown, which couldn't answer the question that
/// matters most here — "which of my screens is this?". Two monitors of the same
/// model produce two indistinguishable menu entries; a thumbnail plus the System
/// Settings display name plus the resolution makes the choice unambiguous, and
/// recording the wrong screen is a privacy mistake worth designing out.
@available(macOS 13.0, *)
struct VideoSourceListView: View {
    @ObservedObject var s: MenuBarAppState

    private var displays: [VideoCapture.Source] { s.videoSources.filter { $0.isDisplay } }
    private var windows: [VideoCapture.Source] { s.videoSources.filter { !$0.isDisplay } }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Choose what to record").font(.caption2).foregroundColor(.secondary)
                Spacer()
                Button {
                    s.refreshVideoSources(withThumbnails: true)
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise").font(.caption2)
                }
                .buttonStyle(.plain)
                .foregroundColor(.accentColor)
                .help("Re-scan displays and windows, and refresh the previews")
            }

            if s.videoSources.isEmpty {
                // Either the scan hasn't finished or Screen Recording permission
                // is missing — say so rather than showing an empty box.
                Text("Looking for screens and windows… If nothing appears, grant Screen Recording in System Settings ▸ Privacy & Security.")
                    .font(.caption2).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        sectionLabel("Screens")
                        ForEach(displays) { src in row(src) }
                        if !windows.isEmpty {
                            sectionLabel("Windows").padding(.top, 6)
                            ForEach(windows) { src in row(src) }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                // Bounded so a machine with many open windows doesn't push the
                // rest of Settings off-screen; the list scrolls within it.
                .frame(maxHeight: 260)
            }
        }
    }

    private func sectionLabel(_ text: String) -> some View {
        Text(text.uppercased())
            .font(.system(size: 9, weight: .semibold))
            .foregroundColor(.secondary)
    }

    private func row(_ src: VideoCapture.Source) -> some View {
        let selected = src.id == s.videoSourceID
        return Button {
            s.videoSourceID = src.id
            s.saveSettings()
        } label: {
            HStack(spacing: 8) {
                SourceThumbnail(image: s.videoThumbnails[src.id])
                VStack(alignment: .leading, spacing: 2) {
                    Text(src.name)
                        .font(.caption)
                        .lineLimit(2).truncationMode(.middle)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 4) {
                        Image(systemName: src.isDisplay ? "display" : "macwindow")
                            .font(.system(size: 9))
                        if !src.dimensionsText.isEmpty {
                            Text(src.dimensionsText).font(.system(size: 9))
                        }
                    }
                    .foregroundColor(.secondary)
                }
                Spacer(minLength: 4)
                // A checkmark AND a tinted background AND a border: selection has
                // to be obvious at caption size, not inferred from a subtle tint.
                if selected {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundColor(.accentColor)
                }
            }
            .padding(6)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 5)
                .fill(selected ? Color.accentColor.opacity(0.15) : Color.clear))
            .overlay(RoundedRectangle(cornerRadius: 5)
                .strokeBorder(selected ? Color.accentColor.opacity(0.6) : Color.clear))
            .contentShape(Rectangle())   // the whole row is clickable, not just the text
        }
        .buttonStyle(.plain)
        .help(src.dimensionsText.isEmpty ? src.name : "\(src.name) — \(src.dimensionsText)")
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
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
final class MenuBarController: NSObject, NSApplicationDelegate, NSWindowDelegate,
                               UNUserNotificationCenterDelegate {
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private var panelWindow: NSWindow?
    private var settingsWindow: NSWindow?
    /// True when Settings was opened from a visible panel/popover, so closing it
    /// should bring the main panel back rather than leaving nothing on screen.
    private var restorePanelAfterSettings = false
    private let state: MenuBarAppState
    private var pollTimer: Timer?

    init(state: MenuBarAppState) { self.state = state }

    /// The content view, with the Settings action wired to open the standalone
    /// settings window (each host — popover and panel window — gets its own
    /// instance, so the closure is attached per construction).
    ///
    /// `host` selects the sizing behaviour the two presentations need — see the
    /// WIDTH note in MenuBarContentView.body.
    private func makeContentView(host: MenuBarContentView.Host) -> MenuBarContentView {
        var v = MenuBarContentView(s: state, host: host)
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
        // Size the popover to its CONTENT height rather than a fixed box: the
        // panel grows (consent gate, errors, meters) and a fixed height clipped
        // the top/bottom with no way to scroll or resize. NSHostingController
        // reports a SwiftUI-derived fitting size, and the view's own ScrollView
        // caps how tall that can get. The WIDTH is pinned by the content view so
        // the popover can't adopt SwiftUI's much larger ideal width.
        let host = NSHostingController(rootView: makeContentView(host: .popover))
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
            // Defence in depth: if the window ever ends up degenerate (a stale
            // frame restored by AppKit, a display reconfiguration), reopening it
            // should still yield something usable rather than a sliver.
            if w.contentLayoutRect.width < MenuBarContentView.minWindowWidth
                || w.contentLayoutRect.height < MenuBarContentView.minWindowHeight {
                w.setContentSize(Self.panelContentSize)
                w.center()
            }
            w.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let w = NSWindow(
            contentRect: NSRect(origin: .zero, size: Self.panelContentSize),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        w.title = state.controller.config.appDisplayName
        // sizingOptions must NOT include .preferredContentSize: that made
        // assigning the controller immediately resize the window to SwiftUI's
        // fitting size, which for this scrolling content is (minWidth, 0) — the
        // "opens as a thin bar with just the app name" symptom. Verified: the
        // window reported 300×0 on assignment with the default options.
        //
        // .minSize IS included, because it is what actually enforces a floor:
        // NSWindow.contentMinSize is ignored once a contentViewController is set
        // (verified — the window still shrank to 50×50), whereas .minSize
        // propagates the content's declared minimum and clamps correctly.
        let host = NSHostingController(rootView: makeContentView(host: .window))
        host.sizingOptions = [.minSize]
        w.contentViewController = host
        // The explicit size is what the window opens at; it must come AFTER the
        // controller assignment, which otherwise overrides it.
        w.setContentSize(Self.panelContentSize)
        w.isReleasedWhenClosed = false
        w.center()
        panelWindow = w
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Opening size of the standalone panel window.
    private static let panelContentSize = NSSize(width: 360, height: 520)

    /// Settings in a dedicated, resizable window (opened from the gear). Keeps
    /// the popover small and gives the growing settings surface room.
    ///
    /// Opening Settings from the menu-bar popover DISMISSES that popover: it is
    /// `.transient`, so it closes as soon as the settings window takes focus.
    /// That left the user with nothing to return to when they closed Settings,
    /// so we remember the popover was the opener and bring the main panel back
    /// on close (as a window — the popover's status-item anchor may be hidden
    /// under the notch, which is exactly why the window path exists).
    private func showSettingsWindow() {
        state.refreshMicDevices()
        // Thumbnails here: the Settings picker is where previews are the point.
        if state.videoEnabled { state.refreshVideoSources(withThumbnails: true) }
        // Capture this BEFORE the window opens and steals focus from the popover.
        restorePanelAfterSettings = popover?.isShown == true || panelWindow?.isVisible == true
        if let w = settingsWindow {
            w.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let w = NSWindow(
            contentRect: NSRect(origin: .zero, size: Self.settingsContentSize),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        w.title = state.controller.config.stackName.isEmpty
            ? "LMA Capture Settings"
            : "LMA Capture Settings — \(state.controller.config.stackName)"
        // Same sizing rules as the panel window: no .preferredContentSize (it
        // collapses the window on assignment), and .minSize for the floor.
        let host = NSHostingController(rootView: SettingsWindowView(s: state))
        host.sizingOptions = [.minSize]
        w.contentViewController = host
        w.setContentSize(Self.settingsContentSize)
        w.isReleasedWhenClosed = false
        w.delegate = self
        w.center()
        settingsWindow = w
        w.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Opening size of the Settings window. Taller than the panel: the video
    /// source list with thumbnails needs the room.
    private static let settingsContentSize = NSSize(width: 440, height: 560)

    // MARK: - NSWindowDelegate

    /// Closing Settings returns the user to the main panel when Settings was
    /// opened from it — otherwise closing the red X appears to close the whole
    /// app, with no visible way back short of the menu-bar icon.
    func windowWillClose(_ notification: Notification) {
        guard (notification.object as? NSWindow) === settingsWindow else { return }
        guard restorePanelAfterSettings else { return }
        restorePanelAfterSettings = false
        // Settings edits (mic, video source) change what the panel summarises,
        // so refresh before showing it again.
        state.refreshMicDevices()
        if state.videoEnabled { state.refreshVideoSources() }
        // Deferred: during windowWillClose the settings window is still key, and
        // ordering another window front here can leave focus on the closing one.
        DispatchQueue.main.async { [weak self] in self?.showPanelWindow() }
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
