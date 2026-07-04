import Foundation

/// Speaks the LMA WebSocket transcriber protocol:
///   1. connect wss://.../api/v1/ws  (Cognito access token in the Authorization header)
///   2. send one JSON text frame  { ..., callEvent: "START" }
///   3. stream raw interleaved 16-bit LE PCM as binary frames
///   4. send JSON text frame { ..., callEvent: "END" } and close
///
/// Native clients — unlike browsers — can set request headers, so we send the
/// token as an `Authorization: Bearer` header rather than a query param.
/// (The server accepts either; see jwt-verifier.ts.)
final class TranscriberSocket: NSObject, URLSessionWebSocketDelegate {
    private let config: Config
    private var session: URLSession!
    private var task: URLSessionWebSocketTask!
    private var isOpen = false
    private let queue = DispatchQueue(label: "lma.ws.send")

    init(config: Config) {
        self.config = config
        super.init()
    }

    func connect() {
        guard let url = URL(string: config.endpoint) else {
            fatalError("Bad endpoint URL: \(config.endpoint)")
        }
        var req = URLRequest(url: url)
        // Native advantage: real HTTP headers. Browsers can't do this.
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

    /// Interleaved 16-bit LE PCM chunk → binary WS frame.
    func sendPCM(_ data: Data) {
        guard isOpen else { return }
        queue.async {
            self.task.send(.data(data)) { err in
                if let err = err { FileHandle.standardError.write("PCM send error: \(err)\n".data(using: .utf8)!) }
            }
        }
    }

    func close() {
        task?.cancel(with: .goingAway, reason: nil)
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
                FileHandle.standardError.write("WS receive/closed: \(err)\n".data(using: .utf8)!)
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
        print("✓ WebSocket open → \(config.endpoint)")
        sendStart()
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        isOpen = false
        let reasonStr = reason.flatMap { String(data: $0, encoding: .utf8) } ?? ""
        print("✗ WebSocket closed (code \(closeCode.rawValue)) \(reasonStr)")
    }
}
