import Foundation

/// Shared engine controller used by BOTH the headless CLI (main.swift) and the
/// menu-bar GUI (MenuBarApp.swift). Owns the socket + mixer + capture lifecycle
/// and exposes simple, thread-safe operations (login, start, stop, pause, mute)
/// plus callbacks the UI can bind to. Keeping this UI-agnostic means the CLI
/// path (which the downloadable package ships) is unaffected by the GUI.
///
/// State transitions:
///   idle → (login) → authenticated → (start) → streaming ⇄ paused → (stop) → idle
final class CaptureController {
    enum State: Equatable {
        case idle
        case signingIn
        case authenticated
        case starting
        case streaming
        case stopping
        case error(String)
    }

    private(set) var config: Config
    private var socket: TranscriberSocket?
    private var mixer: StereoMixer?
    private var capture: AudioCapture?
    private var videoSocket: VideoSocket?
    private var videoCapture: VideoCapture?
    /// When the audio stream started — baseline for videoTimeOffsetMs.
    private var audioStartDate: Date?
    private let lock = NSLock()
    private var _state: State = .idle

    // UI callbacks (invoked on the main queue).
    var onStateChange: ((State) -> Void)?
    var onLevels: ((_ meetingRMS: Float, _ micRMS: Float, _ connected: Bool, _ paused: Bool) -> Void)?
    var onLog: ((String) -> Void)?

    /// The callId of the active/most-recent stream (for "Open in LMA" deep link).
    private(set) var activeCallId: String = ""

    // Settings (persisted by the GUI, applied at start). Empty = use the
    // config/CLI value, so the headless CLI path is completely unaffected.
    /// Speaker label for the mic channel (ch_1 → agentId). GUI default: the
    /// signed-in email.
    var micLabel: String = ""
    /// Speaker label for the system-audio channel (ch_0 → fromNumber).
    var systemLabel: String = ""
    /// CoreAudio UID of the mic to capture from. Empty = system default.
    var micDeviceUID: String = ""
    /// Also capture and stream desktop video (screen or window). Default off.
    var videoEnabled: Bool = false
    /// Persisted video source id ("display:<id>" / "window:<id>"; "" = main display).
    var videoSourceID: String = ""
    /// True while a video stream is running for the active call (UI badge).
    private(set) var isVideoActive: Bool = false

    init(config: Config) { self.config = config }

    var state: State { lock.lock(); defer { lock.unlock() }; return _state }

    private func setState(_ s: State) {
        lock.lock(); _state = s; lock.unlock()
        DispatchQueue.main.async { [weak self] in self?.onStateChange?(s) }
    }

    private func log(_ msg: String) {
        DispatchQueue.main.async { [weak self] in self?.onLog?(msg) }
    }

    // MARK: - Auth

    /// Sign in via SRP and store tokens on the config. `password` is not retained.
    func login(username: String, password: String) async -> Bool {
        setState(.signingIn)
        do {
            let tokens = try await SRP.login(
                username: username, password: password,
                poolId: config.userPoolId, clientId: config.clientId,
                region: config.effectiveRegion)
            config.username = username
            config.accessToken = tokens.accessToken
            config.idToken = tokens.idToken
            log("Signed in as \(username)")
            setState(.authenticated)
            return true
        } catch {
            log("Login failed: \(error)")
            setState(.error("\(error)"))
            return false
        }
    }

    /// True when we already have an access token (pasted or from a prior login).
    var isAuthenticated: Bool {
        if case .streaming = state { return true }
        return !config.accessToken.isEmpty || state == .authenticated
    }

    /// Sign out: stop any active stream, clear tokens, return to idle. The
    /// remembered login id (if any) is left intact so the field can prefill.
    func logout() {
        if socket != nil { stop() }
        config.accessToken = ""
        config.idToken = ""
        log("Signed out")
        setState(.idle)
    }

    // MARK: - Streaming lifecycle

    /// Begin capture + streaming. Uses a fresh callId per session unless the
    /// caller set one on the config. Safe to call only when authenticated.
    func start(callId: String? = nil) {
        guard !config.accessToken.isEmpty else { setState(.error("Not signed in")); return }
        setState(.starting)

        if let c = callId, !c.isEmpty { config.callId = c }
        activeCallId = config.callId

        // Apply Settings overrides: speaker labels ride the START frame as
        // agentId (mic/ch_1) and fromNumber (system/ch_0).
        if !micLabel.isEmpty { config.agentId = micLabel }
        if !systemLabel.isEmpty { config.fromNumber = systemLabel }

        let sock = TranscriberSocket(config: config)
        let mix = StereoMixer(sampleRate: config.sampleRate) { [weak sock] chunk in sock?.sendPCM(chunk) }
        let cap = AudioCapture(mixer: mix, targetRate: config.sampleRate, micDeviceUID: micDeviceUID)

        sock.onStateChange = { connected in mix.setConnected(connected) }
        mix.onLevels = { [weak self] mRMS, kRMS, connected, paused in
            DispatchQueue.main.async { self?.onLevels?(mRMS, kRMS, connected, paused) }
        }

        socket = sock; mixer = mix; capture = cap

        sock.connect()
        mix.start()
        audioStartDate = Date()
        Task { [weak self] in
            do {
                try await cap.start()
                self?.log("Streaming \(self?.activeCallId ?? "")")
                self?.setState(.streaming)
                self?.startVideoIfEnabled()
            } catch {
                self?.log("Capture failed: \(error)")
                self?.setState(.error("\(error)"))
            }
        }
    }

    /// Start the optional desktop-video lane: a second SCK stream feeding
    /// fMP4 segments over a second websocket. Failures here never touch the
    /// audio path — video is best-effort.
    private func startVideoIfEnabled() {
        guard videoEnabled else { return }
        let vSock = VideoSocket(config: config)
        let vCap = VideoCapture(sourceID: videoSourceID) { [weak vSock] segment in
            vSock?.sendSegment(segment)
        }
        // The offset between audio-stream start and the first video frame lets
        // the server align video with the transcript timeline when muxing.
        vCap.onFirstFrame = { [weak self, weak vSock] in
            guard let self = self, let start = self.audioStartDate,
                  let first = self.videoCapture?.firstFrameDate else { return }
            vSock?.videoTimeOffsetMs = max(0, Int(first.timeIntervalSince(start) * 1000))
        }
        // Buffer overflow during a long outage (or a server without video
        // support): abandon video for this call; audio continues.
        vSock.onOverflow = { [weak self] in
            self?.stopVideo(sendEnd: false)
            self?.log("Screen video stopped (connection lost); audio unaffected")
        }
        videoSocket = vSock
        videoCapture = vCap
        vSock.connect()
        Task { [weak self] in
            do {
                try await vCap.start()
                self?.isVideoActive = true
                self?.log("Streaming with screen video")
            } catch {
                self?.log("Screen video failed to start (audio unaffected): \(error)")
                self?.stopVideo(sendEnd: false)
            }
        }
    }

    /// Tear down the video lane. When `sendEnd`, flush the encoder's final
    /// segments and send END_VIDEO so the server finalizes the recording.
    private func stopVideo(sendEnd: Bool) {
        guard videoSocket != nil || videoCapture != nil else { return }
        isVideoActive = false
        let vCap = videoCapture
        let vSock = videoSocket
        videoCapture = nil
        videoSocket = nil
        Task {
            // Stopping the writer flushes remaining fMP4 segments through the
            // onSegment callback BEFORE we send END_VIDEO/close.
            await vCap?.stop()
            if sendEnd {
                vSock?.beginClose()
                vSock?.sendEnd()
                try? await Task.sleep(nanoseconds: 400_000_000)
                vSock?.close()
            } else {
                vSock?.close()
            }
        }
    }

    /// Stop: send END, tear down capture + socket, return to authenticated/idle.
    func stop() {
        setState(.stopping)
        stopVideo(sendEnd: true)
        capture?.stop()
        mixer?.stop()
        socket?.beginClose()   // mark intentional first so teardown stays quiet
        socket?.sendEnd()
        let sock = socket
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            sock?.close()
            self?.socket = nil; self?.mixer = nil; self?.capture = nil
            self?.setState(self?.config.accessToken.isEmpty == true ? .idle : .authenticated)
        }
    }

    // MARK: - Controls (no-ops when not streaming)

    func setPaused(_ p: Bool) { mixer?.setPaused(p); videoCapture?.setPaused(p) }
    func setMicMuted(_ m: Bool) { mixer?.setMicMuted(m) }
    func setMeetingMuted(_ m: Bool) { mixer?.setMeetingMuted(m) }

    var isPaused: Bool { mixer?.isPaused ?? false }
    var isMicMuted: Bool { mixer?.isMicMuted ?? false }
    var isMeetingMuted: Bool { mixer?.isMeetingMuted ?? false }

    /// URL to open the active meeting in the LMA web UI, or the meetings list if
    /// nothing is streaming.
    ///
    /// Uses the configured `webEndpoint` (the web-app CloudFront distribution),
    /// which is DIFFERENT from the WebSocket endpoint host — deriving it from the
    /// WS host yields a 404. Only if webEndpoint is unset do we fall back to the
    /// WS host (best effort).
    func lmaURL() -> URL? {
        var base = config.webEndpoint
        if base.isEmpty {
            guard let comps = URLComponents(string: config.endpoint), let host = comps.host else { return nil }
            base = "https://\(host)/"
        }
        // Normalize to "https://host/" (strip any trailing slash, then add one).
        if base.hasSuffix("/") { base.removeLast() }
        var s = "\(base)/#/calls"
        if !activeCallId.isEmpty {
            // encodeURIComponent-equivalent: encode everything not URL-safe
            // (the web UI uses encodeURIComponent, and callIds contain spaces/colons).
            let allowed = CharacterSet(charactersIn:
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()")
            let enc = activeCallId.addingPercentEncoding(withAllowedCharacters: allowed) ?? activeCallId
            s += "/" + enc
        }
        return URL(string: s)
    }
}
