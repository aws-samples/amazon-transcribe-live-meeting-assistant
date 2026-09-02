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
    /// Source of truth for the tokens we present, and the thing that keeps them
    /// alive. Read through it rather than through `config` — `config` is a
    /// struct, so this socket's copy is frozen at the moment it was created and
    /// would present a stale token after any refresh. nil only when there is
    /// nothing to refresh with (a hand-pasted `--token`), in which case we fall
    /// back to `config` and the old give-up behaviour.
    private let tokens: TokenStore?
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

    // --- Connect state ------------------------------------------------------
    // Guarded by `stateLock`: these are touched from the URLSession delegate
    // queue, `reconnectQueue`, the caller's queue, AND Swift-concurrency threads
    // (the refresh Task), so unsynchronised access is a genuine data race — for
    // `presentedAccessToken`, a non-atomic String, it is undefined behaviour.
    private let stateLock = NSLock()
    // The access token this socket actually presented on the current attempt.
    // Compared against the store's token to spot "someone else already
    // refreshed while I was failing" — then a plain retry is enough and we
    // needn't spend a refresh.
    private var presentedAccessToken = ""
    // One reactive refresh per connection cycle (reset on a successful open), so
    // a server 401ing for a NON-expiry reason can't turn into a refresh loop.
    private var refreshAttemptedThisCycle = false
    /// True from the moment a connect attempt begins until it resolves (open or
    /// error). Without this, two attempts can overlap and the loser is ORPHANED:
    /// `task`/`session` get overwritten while the abandoned socket is still
    /// resumed with `self` as its delegate, so it can send a SECOND START for
    /// the same callId (duplicating the meeting server-side) and its later
    /// callbacks keep mutating the failure counters. Two callers make this
    /// reachable: `connect()` finishes inside a Task when a refresh is needed,
    /// and a reconnect timer can fire during that window.
    private var connectInFlight = false

    // --- Reconnect buffering ------------------------------------------------
    // Briefly hold PCM produced while the socket is down so a short reconnect
    // doesn't punch an audio gap. Bounded so a long outage can't grow unbounded.
    private var pending: [Data] = []
    private let maxPendingBytes = 48000 * 2 * 2 * 3   // ~3s of stereo audio
    private var pendingBytes = 0
    private var droppedWhileDown = 0

    /// Callback so the UI/meter can reflect live connection state.
    var onStateChange: ((_ connected: Bool) -> Void)?

    /// Fired when auth is broken in a way reconnecting cannot fix — the token was
    /// rejected AND could not be refreshed. The OWNER decides what that means:
    /// the menu-bar app stops the stream and re-shows the sign-in form; the
    /// headless CLI exits non-zero. Mirrors the Windows client's `OnFatalAuth`
    /// (windows/Engine/TranscriberSocket.cs).
    ///
    /// Deliberately NOT exit(1) from in here, which is what this used to do: a
    /// menu-bar app that kills itself over a recoverable auth error forces the
    /// user to relaunch it, which is the headline symptom of issue #535.
    var onFatalAuth: ((String) -> Void)?

    /// User-facing text for a hopeless auth state (the GUI shows this verbatim).
    static let fatalAuthMessage =
        "Your session expired and could not be renewed. Please sign in again."

    init(config: Config, tokens: TokenStore? = nil) {
        self.config = config
        self.tokens = tokens
        super.init()
    }

    // Present whatever the store holds NOW, falling back to the frozen config
    // copy when there is no store (pasted-token path).
    private var accessToken: String { tokens?.accessToken ?? config.accessToken }
    private var idToken: String { tokens?.idToken ?? config.idToken }

    // MARK: Connect-state helpers (all lock-guarded — see `stateLock`)

    /// Claim the right to open a socket. Returns false when an attempt is
    /// already in flight (or we're shutting down), in which case the caller
    /// simply DROPS its retry: whoever holds the attempt will either succeed or
    /// fail into `didCompleteWithError`, which schedules the next one.
    private func beginConnectAttempt() -> Bool {
        stateLock.lock(); defer { stateLock.unlock() }
        if connectInFlight || intentionalClose { return false }
        connectInFlight = true
        return true
    }

    /// Mark the current attempt resolved (opened, or failed). Idempotent.
    private func endConnectAttempt() {
        stateLock.lock(); connectInFlight = false; stateLock.unlock()
    }

    func connect() {
        // Single-flight: never let two attempts overlap (see `connectInFlight`).
        guard beginConnectAttempt() else { return }
        // Renew FIRST when the token we are about to present is at/near expiry.
        // Without this, a Start after the Mac woke from sleep — or after an hour
        // idle in the menu bar — opens a socket we already know will 401, and
        // the user watches it fail before it recovers.
        guard let tokens = tokens, tokens.needsRefreshBeforeUse else {
            openSocket()
            return
        }
        Task { [weak self] in
            await tokens.refreshIfNeeded()
            guard let self = self else { return }
            guard !self.intentionalClose else { self.endConnectAttempt(); return }
            // Open regardless of the refresh outcome: if it failed transiently
            // the old token may still work, and if it failed for good the
            // handshake's own error path produces the right message.
            self.openSocket()
        }
    }

    private func openSocket() {
        // Append auth as query params (see AUTH NOTE above) — CloudFront forwards
        // the query string but strips the Authorization header. Mirrors the
        // browser client's useWebSocket queryParams.
        guard var comps = URLComponents(string: config.endpoint) else {
            fatalError("Bad endpoint URL: \(config.endpoint)")
        }
        let access = accessToken
        stateLock.lock(); presentedAccessToken = access; stateLock.unlock()
        var items = comps.queryItems ?? []
        items.append(URLQueryItem(name: "authorization", value: "Bearer \(access)"))
        if !idToken.isEmpty {
            items.append(URLQueryItem(name: "id_token", value: idToken))
        }
        // NOTE: `refresh_token` is deliberately NOT sent. The server only copies
        // it into the Kinesis START/END events as `RefreshToken` and has no
        // consumer for it, so sending the real one would persist a long-lived
        // credential into CloudFront/ALB access logs and the event stream for no
        // benefit. Omitting beats the old `refresh_token=""` placeholder, which
        // claimed to carry something and carried nothing. The param is optional
        // server-side (`refreshToken?: string` — see index.ts / eventtypes.ts).
        comps.queryItems = items
        guard let url = comps.url else {
            fatalError("Failed to build endpoint URL from: \(config.endpoint)")
        }
        var req = URLRequest(url: url)
        // Also set the headers — harmless if forwarded, and lets this same client
        // work if pointed straight at the origin/ALB (no CloudFront) in future.
        req.setValue("Bearer \(access)", forHTTPHeaderField: "authorization")
        if !idToken.isEmpty {
            req.setValue(idToken, forHTTPHeaderField: "id_token")
        }

        // Release the PREVIOUS session before replacing it. A URLSession retains
        // its delegate (us) until invalidated, so reconnecting without this
        // leaks a session per attempt. `finishTasksAndInvalidate` rather than
        // `invalidateAndCancel`: the old task has already finished by now, and
        // cancelling would fire a spurious error callback into our reconnect
        // logic.
        session?.finishTasksAndInvalidate()

        session = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
        task = session.webSocketTask(with: req)
        task.resume()
        receiveLoop() // drain server frames (also surfaces close/errors)
    }

    /// START handshake — must be the first frame; audio before this is dropped server-side.
    ///
    /// Sent afresh on EVERY (re)connect, so the diarization flags must be included
    /// here rather than only on the first connect — otherwise speaker
    /// identification would silently switch itself off after a network blip.
    func sendStart() {
        var meta: [String: Any] = [
            "callId": config.callId,
            "agentId": config.agentId,      // mic channel (ch_1 → AGENT)
            "fromNumber": config.fromNumber, // meeting channel (ch_0 → CALLER)
            "toNumber": config.toNumber,
            "samplingRate": config.sampleRate,
            "callEvent": "START",
            "diarizeSystemChannel": config.diarizeSystemChannel,
            "diarizeMicChannel": config.diarizeMicChannel,
        ]
        // Omitted when unset so the server keeps its own default, rather than this
        // client pinning every meeting to one engine by accident.
        if !config.asrEngine.isEmpty {
            meta["asrEngine"] = config.asrEngine
        }
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
            let delay = Self.backoffDelay(attempt: self.reconnectAttempt, cap: self.maxBackoff)
            print("⟳ WS reconnect #\(self.reconnectAttempt) in \(String(format: "%.1f", delay))s (\(why))")
            self.reconnectQueue.asyncAfter(deadline: .now() + delay) {
                if self.intentionalClose { return }
                self.reconnectScheduled = false
                self.connect() // new task; didOpen sends fresh START
            }
        }
    }

    /// Capped exponential backoff: 0.5s, 1s, 2s, 4s, 8s, then `cap`.
    /// Pure + static so `--selftest` can pin the curve.
    static func backoffDelay(attempt: Int, cap: TimeInterval = 10) -> TimeInterval {
        min(cap, pow(2.0, Double(max(1, attempt) - 1)) * 0.5)
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
                // Redacted: this error embeds the request URL, which carries the
                // access token in its query string.
                FileHandle.standardError.write(
                    "WS receive/closed: \(Self.redactingTokens("\(err)"))\n".data(using: .utf8)!)
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
        endConnectAttempt()      // this attempt resolved — a later drop may retry
        reconnectAttempt = 0     // healthy connection resets backoff
        stateLock.lock()
        everOpened = true
        handshakeFailures = 0
        refreshAttemptedThisCycle = false
        stateLock.unlock()
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
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    didCompleteWithError error: Error?) {
        isOpen = false
        endConnectAttempt()      // this attempt resolved (in failure)
        onStateChange?(false)
        guard !intentionalClose else { return }
        let status = (task.response as? HTTPURLResponse)?.statusCode

        // An EXPIRED access token is by far the most common cause of a rejected
        // upgrade (issue #535). Renew and retry BEFORE this counts toward the
        // fatal threshold, so the user never sees it. Only when refreshing
        // genuinely fails does it become an error worth surfacing.
        if Self.isAuthFailure(status: status, error: error), let tokens = tokens, tokens.canRefresh {
            stateLock.lock()
            let presented = presentedAccessToken
            let alreadyTried = refreshAttemptedThisCycle
            if !alreadyTried { refreshAttemptedThisCycle = true }
            stateLock.unlock()

            if presented != tokens.accessToken {
                // Something already refreshed while this socket was failing (the
                // proactive timer, or a sibling socket): reconnecting is enough,
                // don't spend another refresh. The pending reconnect reads the
                // token through `tokens` at connect time, so it picks up the new
                // one automatically.
                scheduleReconnect("newer access token available")
                return
            }
            if !alreadyTried {
                Task { [weak self] in
                    let ok = await tokens.refreshNow(reason: "WebSocket upgrade rejected the access token")
                    guard let self = self, !self.intentionalClose else { return }
                    if ok {
                        // Reconnect through the SAME guarded path as every other
                        // retry. An earlier version retried immediately here to
                        // skip the backoff; that could open a second socket
                        // alongside a reconnect whose timer had already fired,
                        // orphaning one of them mid-call. The ≤backoff delay is
                        // not worth that risk.
                        self.scheduleReconnect("refreshed access token")
                    } else {
                        self.countHandshakeFailure(status: status, error: error)
                    }
                }
                return
            }
        }
        countHandshakeFailure(status: status, error: error)
    }

    /// Count a failed handshake and decide whether auth is now hopeless.
    ///
    /// `everOpened` still keeps a mid-call NETWORK blip from being misread as bad
    /// auth, but an auth-looking rejection is now judged on its own merits even
    /// after a successful open: on a call running longer than the token's TTL the
    /// token expires UNDER an already-open socket, and the old `!everOpened` guard
    /// skipped the counter entirely there — leaving the client to reconnect-loop
    /// forever against a dead token instead of saying so (issue #535).
    private func countHandshakeFailure(status: Int?, error: Error?) {
        let authLooking = Self.isAuthFailure(status: status, error: error)
        stateLock.lock()
        let countIt = !everOpened || authLooking
        if countIt { handshakeFailures += 1 }
        let failures = handshakeFailures
        stateLock.unlock()
        if countIt {
            if failures >= maxHandshakeFailures {
                FileHandle.standardError.write("""
                ✗ WebSocket handshake keeps failing (\(failures) attempts).
                  This is almost always an EXPIRED or INVALID access token (the server
                  returns 401, seen here as HTTP \(status.map(String.init) ?? "-") /
                  NSURLError -1011). Cognito access tokens last ~1 hour and this client
                  could not renew it — sign in again.
                  Underlying error: \(Self.redactingTokens(error.map { "\($0)" } ?? "none"))\n
                """.data(using: .utf8)!)
                intentionalClose = true
                onFatalAuth?(Self.fatalAuthMessage)
                return
            }
        }
        // Transient (or early) failure: let scheduleReconnect handle backoff.
        scheduleReconnect("task error")
    }

    /// Does this failure look like the SERVER rejecting our token, rather than the
    /// network being unavailable? Pure + static so `--selftest` can pin it.
    ///
    /// A refused WS upgrade surfaces two ways, depending on how far it got:
    ///   • an HTTP response we can read → 401/403 from the server's jwtVerifier
    ///   • no response at all, just NSURLErrorBadServerResponse (-1011) —
    ///     URLSession collapses "the upgrade was refused" into this
    /// Everything else (offline, DNS, timeout, TLS, 5xx) is transient. Getting
    /// this wrong in the permissive direction would spend refreshes on outages;
    /// in the strict direction it would leave expired tokens unrefreshed.
    /// Strip JWT-shaped substrings out of text destined for a log.
    ///
    /// URLSession's error descriptions embed the failing URL
    /// (`NSErrorFailingURLStringKey`), and ours carries the access token in the
    /// query string (see the AUTH NOTE) — so printing one verbatim writes a LIVE
    /// credential to stdout/stderr, where it can be captured by a launcher,
    /// pasted into a bug report, or shipped to a log collector. Redacting only
    /// the token keeps the rest of the diagnostic (host, path, error code)
    /// intact, and catches the percent-encoded `Bearer%20eyJ…` form too.
    ///
    /// Cognito access/id tokens are always JWTs, hence matching on that shape.
    static func redactingTokens(_ text: String) -> String {
        // header.payload.signature — base64url segments, header always "eyJ…".
        let pattern = "eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+"
        guard let re = try? NSRegularExpression(pattern: pattern) else { return text }
        return re.stringByReplacingMatches(
            in: text, range: NSRange(text.startIndex..., in: text),
            withTemplate: "<redacted-jwt>")
    }

    static func isAuthFailure(status: Int?, error: Error?) -> Bool {
        if let status = status { return status == 401 || status == 403 }
        guard let error = error else { return false }
        let ns = error as NSError
        guard ns.domain == NSURLErrorDomain else { return false }
        return ns.code == NSURLErrorBadServerResponse          // -1011
            || ns.code == NSURLErrorUserAuthenticationRequired // -1013
    }
}
