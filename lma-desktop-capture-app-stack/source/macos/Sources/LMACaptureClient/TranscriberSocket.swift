import Foundation

/// Speaks the LMA WebSocket transcriber protocol:
///   1. connect wss://.../api/v1/ws  (Cognito access token in the query string)
///   2. send one JSON text frame  { ..., callEvent: "START" }
///   3. stream raw interleaved 16-bit LE PCM as binary frames
///   4. send JSON text frame { ..., callEvent: "END" } and close
///
/// AUTH NOTE (learned the hard way, do NOT "fix" back to headers):
/// The server's jwtVerifier accepts the token from EITHER the `authorization`
/// header OR the `authorization` query param. BUT the transcriber sits behind a
/// CloudFront distribution whose OriginRequestPolicy uses an explicit header
/// whitelist that does NOT include `Authorization` (see
/// lma-websocket-transcriber.yaml `CloudFrontOriginRequestPolicy`). CloudFront
/// therefore STRIPS the Authorization header before it reaches the origin, so
/// header auth yields a 401. QueryStringBehavior is `all`, so the query string
/// survives. We must authenticate the browser's way: query params. The Node CLI
/// (utilities/websocket-client) gets away with headers only because it connects
/// straight to the origin/ALB, bypassing CloudFront.
final class TranscriberSocket: NSObject, URLSessionWebSocketDelegate, URLSessionTaskDelegate {
    private let config: Config
    private var session: URLSession!
    private var task: URLSessionWebSocketTask!
    private var isOpen = false
    private let queue = DispatchQueue(label: "lma.ws.send")

    // --- Reconnect state ----------------------------------------------------
    // The server has NO session resume: on any drop we must open a NEW socket
    // and send a FRESH START (same callId) before audio. We reconnect with
    // capped exponential backoff until `intentionalClose` is set (Ctrl-C).
    private var intentionalClose = false
    private var reconnectAttempt = 0
    private let maxBackoff: TimeInterval = 10
    private let reconnectQueue = DispatchQueue(label: "lma.ws.reconnect")

    // Distinguish a bad-token/handshake failure (fatal — no point hammering the
    // server with an expired token) from a transient network drop. We only ever
    // succeeded if didOpen fired at least once; if we never open AND keep failing
    // the handshake, that's almost certainly auth — warn loudly and give up.
    private var everOpened = false
    private var handshakeFailures = 0
    private let maxHandshakeFailures = 4

    // --- Reconnect buffering ------------------------------------------------
    // Briefly hold PCM produced while the socket is down so a short reconnect
    // doesn't punch an audio gap. Bounded so a long outage can't grow unbounded.
    private var pending: [Data] = []
    private let maxPendingBytes = 48000 * 2 * 2 * 3   // ~3s of stereo audio
    private var pendingBytes = 0
    private var droppedWhileDown = 0

    /// Callback so the UI/meter can reflect live connection state.
    var onStateChange: ((_ connected: Bool) -> Void)?

    init(config: Config) {
        self.config = config
        super.init()
    }

    func connect() {
        // Append auth as query params (see AUTH NOTE above) — CloudFront forwards
        // the query string but strips the Authorization header. Mirrors the
        // browser client's useWebSocket queryParams.
        guard var comps = URLComponents(string: config.endpoint) else {
            fatalError("Bad endpoint URL: \(config.endpoint)")
        }
        var items = comps.queryItems ?? []
        items.append(URLQueryItem(name: "authorization", value: "Bearer \(config.accessToken)"))
        if !config.idToken.isEmpty {
            items.append(URLQueryItem(name: "id_token", value: config.idToken))
        }
        items.append(URLQueryItem(name: "refresh_token", value: ""))
        comps.queryItems = items
        guard let url = comps.url else {
            fatalError("Failed to build endpoint URL from: \(config.endpoint)")
        }
        var req = URLRequest(url: url)
        // Also set the headers — harmless if forwarded, and lets this same client
        // work if pointed straight at the origin/ALB (no CloudFront) in future.
        req.setValue("Bearer \(config.accessToken)", forHTTPHeaderField: "authorization")
        if !config.idToken.isEmpty {
            req.setValue(config.idToken, forHTTPHeaderField: "id_token")
        }
        req.setValue("", forHTTPHeaderField: "refresh_token")

        session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
        task = session.webSocketTask(with: req)
        task.resume()
        receiveLoop() // drain server frames (also surfaces close/errors)
    }

    /// START handshake — must be the first frame; audio before this is dropped server-side.
    func sendStart() {
        let meta: [String: Any] = [
            "callId": config.callId,
            "agentId": config.agentId,      // mic channel (ch_1 → AGENT)
            "fromNumber": config.fromNumber, // meeting channel (ch_0 → CALLER)
            "toNumber": config.toNumber,
            "samplingRate": config.sampleRate,
            "callEvent": "START",
            // Selects the MicroVM ASR engine server-side; the transcriber falls
            // back to Amazon Transcribe when that engine is not deployed.
            "enableDiarization": config.diarizationEnabled,
        ]
        sendJSON(meta, label: "START")
    }

    func sendEnd() {
        let meta: [String: Any] = [
            "callId": config.callId,
            "agentId": config.agentId,
            "fromNumber": config.fromNumber,
            "toNumber": config.toNumber,
            "samplingRate": config.sampleRate,
            "callEvent": "END",
        ]
        sendJSON(meta, label: "END")
    }

    /// Interleaved 16-bit LE PCM chunk → binary WS frame. When the socket is
    /// down (reconnecting), buffer up to `maxPendingBytes` so a brief drop
    /// doesn't gap the audio; beyond that we drop oldest and count it.
    func sendPCM(_ data: Data) {
        queue.async {
            guard self.isOpen else {
                self.pending.append(data)
                self.pendingBytes += data.count
                while self.pendingBytes > self.maxPendingBytes, !self.pending.isEmpty {
                    let d = self.pending.removeFirst()
                    self.pendingBytes -= d.count
                    self.droppedWhileDown += d.count
                }
                return
            }
            self.task.send(.data(data)) { err in
                if let err = err { FileHandle.standardError.write("PCM send error: \(err)\n".data(using: .utf8)!) }
            }
        }
    }

    /// Flush buffered PCM after a reconnect (called once the new START is sent).
    private func flushPending() {
        queue.async {
            guard self.isOpen, !self.pending.isEmpty else { return }
            let count = self.pending.count
            let dropped = self.droppedWhileDown
            for d in self.pending {
                self.task.send(.data(d)) { _ in }
            }
            self.pending.removeAll(); self.pendingBytes = 0; self.droppedWhileDown = 0
            var msg = "↺ flushed \(count) buffered PCM frames after reconnect"
            if dropped > 0 { msg += " (dropped \(dropped / (48000*2*2))s that overflowed the buffer)" }
            print(msg)
        }
    }

    /// Mark that we are intentionally shutting down. Call this BEFORE sendEnd()
    /// so the subsequent socket teardown (receive error / close / task error)
    /// is treated as expected and stays quiet — no scary "Socket is not
    /// connected" dumps or reconnect attempts on a normal Stop.
    func beginClose() {
        intentionalClose = true
    }

    func close() {
        intentionalClose = true
        task?.cancel(with: .goingAway, reason: nil)
    }

    /// Schedule a reconnect after an UNEXPECTED drop. Opens a new socket; the
    /// didOpen delegate re-sends a fresh START (server has no resume). Idempotent
    /// per drop: guarded so the receive-failure and didClose paths don't double up.
    private var reconnectScheduled = false
    private func scheduleReconnect(_ why: String) {
        reconnectQueue.async { [weak self] in
            guard let self = self else { return }
            if self.intentionalClose || self.reconnectScheduled { return }
            self.reconnectScheduled = true
            self.isOpen = false
            self.reconnectAttempt += 1
            let delay = min(self.maxBackoff, pow(2.0, Double(self.reconnectAttempt - 1)) * 0.5)
            print("⟳ WS reconnect #\(self.reconnectAttempt) in \(String(format: "%.1f", delay))s (\(why))")
            self.reconnectQueue.asyncAfter(deadline: .now() + delay) {
                if self.intentionalClose { return }
                self.reconnectScheduled = false
                self.connect() // new task; didOpen sends fresh START
            }
        }
    }

    private func sendJSON(_ obj: [String: Any], label: String) {
        guard let json = try? JSONSerialization.data(withJSONObject: obj),
              let str = String(data: json, encoding: .utf8) else { return }
        task.send(.string(str)) { err in
            if let err = err {
                FileHandle.standardError.write("\(label) send error: \(err)\n".data(using: .utf8)!)
            } else {
                print("→ sent \(label)")
            }
        }
    }

    private func receiveLoop() {
        task.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let err):
                // Expected during an intentional Stop/close — stay silent (the
                // pending receive always errors when we cancel the task).
                if self.intentionalClose { return }
                FileHandle.standardError.write("WS receive/closed: \(err)\n".data(using: .utf8)!)
                self.scheduleReconnect("receive error")
            case .success(let msg):
                // The transcriber doesn't send app-level frames back; log anything unexpected.
                if case let .string(s) = msg { print("← server: \(s)") }
                self.receiveLoop()
            }
        }
    }

    // MARK: URLSessionWebSocketDelegate

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol protocol: String?) {
        isOpen = true
        everOpened = true
        reconnectAttempt = 0     // healthy connection resets backoff
        handshakeFailures = 0
        print("✓ WebSocket open → \(config.endpoint)")
        onStateChange?(true)
        sendStart()              // fresh START every (re)connect; server has no resume
        flushPending()           // replay audio buffered during the outage
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        isOpen = false
        onStateChange?(false)
        if intentionalClose { return }   // normal Stop/close — nothing to report
        let reasonStr = reason.flatMap { String(data: $0, encoding: .utf8) } ?? ""
        print("✗ WebSocket closed (code \(closeCode.rawValue)) \(reasonStr)")
        scheduleReconnect("server closed \(closeCode.rawValue)")
    }

    // Fires when the underlying HTTP request fails — including a rejected WS
    // upgrade (bad/expired token → 401, surfaced as NSURLErrorDomain -1011).
    // If we NEVER opened and keep failing the handshake, it's almost certainly
    // auth: warn clearly and stop rather than hammering with a dead token.
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    didCompleteWithError error: Error?) {
        isOpen = false
        onStateChange?(false)
        guard !intentionalClose else { return }
        if !everOpened {
            handshakeFailures += 1
            if handshakeFailures >= maxHandshakeFailures {
                FileHandle.standardError.write("""
                ✗ WebSocket handshake keeps failing and the connection never opened.
                  This is almost always an EXPIRED or INVALID access token (the server
                  returns 401, seen here as NSURLError -1011). Cognito access tokens
                  last ~1 hour. Fetch a fresh token and re-run.
                  Underlying error: \(error.map { "\($0)" } ?? "none")\n
                """.data(using: .utf8)!)
                intentionalClose = true
                exit(1)
            }
        }
        // Transient (or early) failure: let scheduleReconnect handle backoff.
        scheduleReconnect("task error")
    }
}
