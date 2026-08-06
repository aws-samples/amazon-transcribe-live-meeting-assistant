import Foundation

/// Second WebSocket, dedicated to the optional desktop-video stream:
///   1. connect wss://.../api/v1/ws  (same endpoint + query-param auth as the
///      audio socket — see the AUTH NOTE in TranscriberSocket.swift)
///   2. send one JSON text frame { callEvent: "START_VIDEO", callId, ... }
///   3. stream fragmented-MP4 segments as binary frames
///   4. send { callEvent: "END_VIDEO" } and close
///
/// Keeping video on its own socket means video bytes can never delay the
/// real-time audio PCM (no head-of-line blocking), and old servers — which
/// ignore the unknown START_VIDEO event and drop binary frames on a session-
/// less socket — degrade to exactly the pre-video behavior.
///
/// Reconnect semantics differ from audio: the fMP4 encoder session on our side
/// keeps running across a socket drop, so the bytes CONTINUE one stream. On
/// reconnect we re-send START_VIDEO with videoResume=true and flush everything
/// buffered — segments must never be dropped mid-stream (unlike audio, a gap
/// makes the remainder undecodable). If the outage outlasts the buffer cap we
/// give up on video for this call (audio is the priority) via onOverflow.
final class VideoSocket: NSObject, URLSessionWebSocketDelegate, URLSessionTaskDelegate {
    private let config: Config
    private var session: URLSession!
    private var task: URLSessionWebSocketTask!
    private var isOpen = false
    private let queue = DispatchQueue(label: "lma.ws.video.send")

    private var intentionalClose = false
    private var reconnectAttempt = 0
    private let maxBackoff: TimeInterval = 10
    private let reconnectQueue = DispatchQueue(label: "lma.ws.video.reconnect")
    private var reconnectScheduled = false
    private var everOpened = false
    private var handshakeFailures = 0
    private let maxHandshakeFailures = 4

    /// True after the first successful START_VIDEO: reconnects resume.
    private var startedOnce = false

    /// ms between the audio stream's start and the first video frame; sent on
    /// START_VIDEO so the server can align video with the transcript timeline.
    var videoTimeOffsetMs: Int = 0

    // Segments buffered while the socket is down. Generous cap (~2 minutes at
    // the configured bitrate) because dropping mid-stream corrupts the video.
    private var pending: [Data] = []
    private var pendingBytes = 0
    private let maxPendingBytes = 32 * 1024 * 1024
    private var overflowed = false

    /// Fired once if the buffer cap is exceeded during an outage — the caller
    /// should stop video capture for this call (audio continues unaffected).
    var onOverflow: (() -> Void)?

    init(config: Config) {
        self.config = config
        super.init()
    }

    func connect() {
        guard var comps = URLComponents(string: config.endpoint) else {
            FileHandle.standardError.write("Bad endpoint URL: \(config.endpoint)\n".data(using: .utf8)!)
            return
        }
        var items = comps.queryItems ?? []
        items.append(URLQueryItem(name: "authorization", value: "Bearer \(config.accessToken)"))
        if !config.idToken.isEmpty {
            items.append(URLQueryItem(name: "id_token", value: config.idToken))
        }
        items.append(URLQueryItem(name: "refresh_token", value: ""))
        comps.queryItems = items
        guard let url = comps.url else { return }
        var req = URLRequest(url: url)
        req.setValue("Bearer \(config.accessToken)", forHTTPHeaderField: "authorization")
        if !config.idToken.isEmpty {
            req.setValue(config.idToken, forHTTPHeaderField: "id_token")
        }
        req.setValue("", forHTTPHeaderField: "refresh_token")

        session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
        task = session.webSocketTask(with: req)
        task.resume()
        receiveLoop()
    }

    /// fMP4 segment → binary WS frame (buffered while the socket is down).
    func sendSegment(_ data: Data) {
        queue.async {
            guard !self.overflowed else { return }
            guard self.isOpen else {
                self.pending.append(data)
                self.pendingBytes += data.count
                if self.pendingBytes > self.maxPendingBytes {
                    // Outage outlasted the buffer: video for this call is lost
                    // from here. Let the controller stop the capture cleanly.
                    self.overflowed = true
                    self.pending.removeAll(); self.pendingBytes = 0
                    FileHandle.standardError.write(
                        "✗ video buffer overflow during outage — stopping video (audio unaffected)\n".data(using: .utf8)!)
                    DispatchQueue.main.async { self.onOverflow?() }
                }
                return
            }
            self.task.send(.data(data)) { err in
                if let err = err { FileHandle.standardError.write("video send error: \(err)\n".data(using: .utf8)!) }
            }
        }
    }

    func sendEnd() {
        let meta: [String: Any] = [
            "callId": config.callId,
            "callEvent": "END_VIDEO",
        ]
        sendJSON(meta, label: "END_VIDEO")
    }

    func beginClose() { intentionalClose = true }

    func close() {
        intentionalClose = true
        task?.cancel(with: .goingAway, reason: nil)
    }

    private func sendStartVideo() {
        var meta: [String: Any] = [
            "callId": config.callId,
            "callEvent": "START_VIDEO",
            "videoTimeOffsetMs": videoTimeOffsetMs,
        ]
        if startedOnce {
            // Same encoder session continuing over a new socket: the server
            // appends to the current segment instead of rotating files.
            meta["videoResume"] = true
        }
        sendJSON(meta, label: startedOnce ? "START_VIDEO (resume)" : "START_VIDEO")
        startedOnce = true
    }

    private func flushPending() {
        queue.async {
            guard self.isOpen, !self.pending.isEmpty else { return }
            let count = self.pending.count
            for d in self.pending {
                self.task.send(.data(d)) { _ in }
            }
            self.pending.removeAll(); self.pendingBytes = 0
            print("↺ flushed \(count) buffered video segments after reconnect")
        }
    }

    private func scheduleReconnect(_ why: String) {
        reconnectQueue.async { [weak self] in
            guard let self = self else { return }
            if self.intentionalClose || self.reconnectScheduled || self.overflowed { return }
            self.reconnectScheduled = true
            self.isOpen = false
            self.reconnectAttempt += 1
            let delay = min(self.maxBackoff, pow(2.0, Double(self.reconnectAttempt - 1)) * 0.5)
            print("⟳ video WS reconnect #\(self.reconnectAttempt) in \(String(format: "%.1f", delay))s (\(why))")
            self.reconnectQueue.asyncAfter(deadline: .now() + delay) {
                if self.intentionalClose { return }
                self.reconnectScheduled = false
                self.connect()
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
                if self.intentionalClose { return }
                FileHandle.standardError.write("video WS receive/closed: \(err)\n".data(using: .utf8)!)
                self.scheduleReconnect("receive error")
            case .success:
                self.receiveLoop()
            }
        }
    }

    // MARK: URLSessionWebSocketDelegate

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol protocol: String?) {
        queue.async {
            self.isOpen = true
        }
        everOpened = true
        reconnectAttempt = 0
        handshakeFailures = 0
        print("✓ video WebSocket open")
        sendStartVideo()
        flushPending()
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        queue.async { self.isOpen = false }
        if intentionalClose { return }
        scheduleReconnect("server closed \(closeCode.rawValue)")
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    didCompleteWithError error: Error?) {
        queue.async { self.isOpen = false }
        guard !intentionalClose else { return }
        if !everOpened {
            handshakeFailures += 1
            if handshakeFailures >= maxHandshakeFailures {
                // Unlike the audio socket, a dead video connection is not fatal
                // to the app: give up on video only (likely an old server that
                // rejects nothing — auth failures would also hit the audio
                // socket, which owns the loud failure path).
                FileHandle.standardError.write(
                    "✗ video WebSocket handshake keeps failing — video disabled for this call\n".data(using: .utf8)!)
                intentionalClose = true
                DispatchQueue.main.async { self.onOverflow?() }
                return
            }
        }
        scheduleReconnect("task error")
    }
}
