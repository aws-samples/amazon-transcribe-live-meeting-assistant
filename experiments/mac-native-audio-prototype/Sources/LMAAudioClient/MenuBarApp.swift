#if canImport(SwiftUI) && canImport(AppKit)
import SwiftUI
import AppKit
import ServiceManagement

/// Menu-bar (tray) UI for the LMA Audio Capture App — MVP.
///
/// Lives in the macOS menu bar via NSStatusItem. Clicking the icon opens a
/// popover with: sign in/out, start/stop/pause, mute mic, mute system audio,
/// live per-channel level meters, and "Open in LMA". While streaming, the
/// menu-bar icon turns red so recording is obvious at a glance.
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

    let controller: CaptureController
    // UserDefaults keys for the optional "remember login id" feature. Only the
    // username (email) is stored — never the password, which stays in memory.
    private static let kRemember = "lma.rememberLogin"
    private static let kUsername = "lma.savedUsername"

    init(controller: CaptureController) {
        self.controller = controller
        let defaults = UserDefaults.standard
        self.rememberLogin = defaults.bool(forKey: Self.kRemember)
        // Prefill the login id from the remembered value, else the config.
        self.username = rememberLogin ? (defaults.string(forKey: Self.kUsername) ?? "") : controller.config.username
        controller.onStateChange = { [weak self] s in self?.state = s }
        controller.onLevels = { [weak self] m, k, c, p in
            self?.meetingLevel = m; self?.micLevel = k; self?.connected = c; self?.paused = p
        }
        controller.onLog = { [weak self] msg in self?.lastLog = msg }
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
        let defaults = UserDefaults.standard
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
        let name = meetingName.isEmpty ? "" : "\(meetingName) - \(Self.timestamp())"
        controller.start(callId: name.isEmpty ? nil : name)
    }
    func stop() { controller.stop() }
    func togglePause() { paused.toggle(); controller.setPaused(paused) }
    func toggleMic() { micMuted.toggle(); controller.setMicMuted(micMuted) }
    func toggleMeeting() { meetingMuted.toggle(); controller.setMeetingMuted(meetingMuted) }
    func openLMA() { if let u = controller.lmaURL() { NSWorkspace.shared.open(u) } }

    static func timestamp() -> String {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd HH:mm"; return f.string(from: Date())
    }
}

@available(macOS 13.0, *)
struct MenuBarContentView: View {
    @ObservedObject var s: MenuBarAppState

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Circle().fill(s.isStreaming ? .red : .secondary).frame(width: 10, height: 10)
                Text("LMA Audio Capture").font(.headline)
                Spacer()
                Text(statusText).font(.caption).foregroundColor(.secondary)
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
                if !s.isStreaming {
                    TextField("Meeting name (optional)", text: $s.meetingName).textFieldStyle(.roundedBorder)
                    Button(action: s.start) {
                        Label("Start Recording", systemImage: "record.circle").frame(maxWidth: .infinity)
                    }
                    .disabled(s.isBusy)
                    .keyboardShortcut(.defaultAction)
                } else {
                    // Live meters
                    LevelBar(label: "System", level: s.meetingLevel, muted: s.meetingMuted)
                    LevelBar(label: "Mic", level: s.micLevel, muted: s.micMuted)
                    HStack {
                        Text(s.connected ? "● Live" : "○ Reconnecting…")
                            .font(.caption).foregroundColor(s.connected ? .green : .orange)
                        if s.paused { Text("· Paused").font(.caption).foregroundColor(.orange) }
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
                Text(s.lastLog).font(.caption2).foregroundColor(.secondary).lineLimit(2)
            }

            Divider()
            // Quit lives in the right-click menu (not here) so it can't be
            // mistaken for Stop. Leave the app running in the background between
            // meetings; it uses no audio/CPU when idle.
            Text("Right-click the menu-bar icon to Quit. Leave it running in the background between meetings.")
                .font(.caption2).foregroundColor(.secondary)
        }
        .padding(14)
        .frame(width: 300)
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
@available(macOS 13.0, *)
final class MenuBarController: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private let state: MenuBarAppState
    private var pollTimer: Timer?

    init(state: MenuBarAppState) { self.state = state }

    func applicationDidFinishLaunching(_ notification: Notification) {
        FileHandle.standardError.write("LMA: applicationDidFinishLaunching\n".data(using: .utf8)!)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        // Always give the button a visible text title as a fallback so the item
        // is never zero-width even if the SF Symbol image fails to load.
        statusItem.button?.title = "LMA"
        updateIcon()
        FileHandle.standardError.write("LMA: status item created, button=\(statusItem.button != nil)\n".data(using: .utf8)!)

        popover = NSPopover()
        popover.contentSize = NSSize(width: 300, height: 360)
        popover.behavior = .transient
        popover.contentViewController = NSHostingController(rootView: MenuBarContentView(s: state))

        // Left-click opens the popover; right-click (or control-click) shows a
        // small menu with Quit — kept OUT of the popover so it can't be confused
        // with Stop.
        statusItem.button?.action = #selector(statusButtonClicked)
        statusItem.button?.target = self
        statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])

        // Repaint the menu-bar icon (red while recording) as state changes.
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.updateIcon()
        }
    }

    /// Build the right-click menu (Quit + a hint line). Rebuilt on demand so it
    /// stays simple; also reachable if the popover is ever unavailable.
    private func makeContextMenu() -> NSMenu {
        let menu = NSMenu()
        let openItem = NSMenuItem(title: "Open LMA Audio Capture", action: #selector(openPopoverFromMenu), keyEquivalent: "")
        openItem.target = self
        menu.addItem(openItem)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit LMA Audio Capture", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        return menu
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
            let img = NSImage(systemSymbolName: "waveform.circle", accessibilityDescription: "LMA Audio Capture")
            img?.isTemplate = true
            button.image = img
            button.contentTintColor = nil
        }
        button.attributedTitle = NSAttributedString(
            string: " LMA",
            attributes: [.font: NSFont.menuBarFont(ofSize: 0)])
        button.imagePosition = .imageLeading
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

/// Entry point for GUI mode. Runs a standard AppKit run loop with an
/// LSUIElement (menu-bar-only) activation policy.
@available(macOS 13.0, *)
func runMenuBarApp(config: Config) -> Never {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory) // menu-bar only, no Dock icon
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
