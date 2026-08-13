import Foundation

/// Keeps the Cognito ACCESS token fresh for as long as the process is signed in.
///
/// Why this exists (GitHub issue #535): the client signed in, kept the access
/// token forever, and threw the refresh token away. Cognito access tokens last
/// ~1 hour and the transcriber's jwtVerifier 401s an expired one, so any session
/// outliving the TTL — or any Start after an hour idle — failed the WebSocket
/// upgrade with no way back except quitting and relaunching the app.
///
/// Two complementary paths, because either alone leaves a hole:
///   • PROACTIVE — a timer armed `refreshLead` seconds before the token's `exp`.
///     This is what PREVENTS the failure instead of recovering from it, and it
///     is what keeps a long meeting streaming without a visible blip.
///   • ON DEMAND / REACTIVE — `refreshIfNeeded()` before we present the token,
///     and `refreshNow()` when the server rejects it anyway. Covers the cases a
///     timer cannot: a sleep that outlasted the deadline, client clock skew, or
///     a token invalidated server-side.
///
/// Tokens live HERE, in a reference type, rather than on `Config` — `Config` is
/// a struct, so every holder gets a private copy that goes stale the instant
/// anything refreshes. A socket built ten minutes ago must present the token we
/// have NOW, which is why TranscriberSocket reads through this object.
///
/// Thread-safety: all mutable state is behind `lock`; `refreshNow()` coalesces
/// concurrent callers onto a single in-flight network call.
final class TokenStore {
    /// Refresh this many seconds BEFORE `exp`. Generous enough to absorb a slow
    /// network and modest client clock skew, small enough that we are not
    /// re-minting constantly against a ~1 h TTL.
    static let refreshLead: TimeInterval = 300

    /// Floor on the gap between refresh ATTEMPTS. A backstop: without it, a
    /// reconnect storm — or a 401 caused by something other than expiry — could
    /// hammer Cognito's InitiateAuth.
    ///
    /// Deliberate consequence: if a token is rejected within this window of a
    /// SUCCESSFUL refresh, the reactive attempt is refused and the socket
    /// escalates to fatal instead of refreshing again. That is the right
    /// outcome — a token minted seconds ago being rejected means the session was
    /// revoked server-side, not that it expired, and re-minting cannot fix it.
    /// Note the socket only reaches here when it already presented the CURRENT
    /// token; if it presented an older one it just reconnects (no refresh spent).
    static let minRefreshInterval: TimeInterval = 20

    /// Wait between retries after a TRANSIENT refresh failure (e.g. offline),
    /// so we still get another go before the token actually dies.
    static let retryInterval: TimeInterval = 60

    /// Cap on consecutive transient retries. Past this we stop polling and let
    /// the on-demand path (`refreshIfNeeded()` at connect time) recover — an app
    /// left offline for hours should not be talking to Cognito every minute.
    static let maxTransientRetries = 5

    private let lock = NSLock()
    private var _access: String
    private var _id: String
    private var _refresh: String
    private let clientId: String
    private let region: String

    /// `exp` of the token the pending proactive timer was armed for. A timer
    /// whose captured `exp` no longer matches this is stale (a newer token, and
    /// a newer timer, has landed) and must do nothing.
    private var armedFor: Date?
    private var lastAttempt: Date?
    private var inFlight: Task<Bool, Never>?
    private var transientFailures = 0
    /// Set by invalidate() on sign-out: stops refreshing and prevents an
    /// in-flight refresh from writing tokens back after the user signed out.
    private var invalidated = false

    private let timerQueue = DispatchQueue(label: "lma.token.refresh")

    /// Fired on the main queue after a successful refresh so the owner can
    /// mirror the new tokens onto its own `Config` copy.
    var onRefresh: ((_ accessToken: String, _ idToken: String) -> Void)?

    /// Log sink (main queue). Refresh is invisible when it works, so these lines
    /// are the only evidence it happened — keep them.
    var onLog: ((String) -> Void)?

    init(accessToken: String, idToken: String, refreshToken: String,
         clientId: String, region: String) {
        self._access = accessToken
        self._id = idToken
        self._refresh = refreshToken
        self.clientId = clientId
        self.region = region
    }

    // MARK: - Current tokens

    var accessToken: String { lock.lock(); defer { lock.unlock() }; return _access }
    var idToken: String { lock.lock(); defer { lock.unlock() }; return _id }
    var refreshToken: String { lock.lock(); defer { lock.unlock() }; return _refresh }

    /// False when there is nothing to redeem — a hand-pasted `--token` with no
    /// `--refresh-token`, or after sign-out. Callers must fall back to their
    /// pre-existing "give up and ask the user" behaviour in that case.
    var canRefresh: Bool {
        lock.lock(); defer { lock.unlock() }
        return !invalidated && !_refresh.isEmpty && !clientId.isEmpty
    }

    /// True when the access token is inside the lead window (or already dead)
    /// and should be renewed BEFORE we present it.
    var needsRefreshBeforeUse: Bool {
        guard canRefresh else { return false }
        return Self.needsRefresh(now: Date(), exp: Self.expiry(ofJWT: accessToken))
    }

    // MARK: - Pure scheduling logic (pinned by SelfTest)

    /// JWT payload claims, WITHOUT verifying the signature — we only read `exp`
    /// to schedule our own refresh. Validating the token is the SERVER's job
    /// (jwt-verifier.ts); trusting `exp` here can at worst make us refresh at
    /// the wrong moment, which the reactive path then corrects.
    static func jwtPayload(_ jwt: String) -> [String: Any]? {
        let parts = jwt.split(separator: ".")
        guard parts.count == 3 else { return nil }
        // base64url → base64, then re-pad to a multiple of 4.
        var b = String(parts[1])
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        while b.count % 4 != 0 { b += "=" }
        guard let data = Data(base64Encoded: b),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return obj
    }

    /// The access token's expiry, or nil if it isn't a readable JWT.
    static func expiry(ofJWT jwt: String) -> Date? {
        guard let exp = jwtPayload(jwt)?["exp"] as? Double else { return nil }
        return Date(timeIntervalSince1970: exp)
    }

    /// Seconds to wait before proactively refreshing a token expiring at `exp`.
    /// 0 means "already inside the lead window — refresh now".
    static func secondsUntilRefresh(now: Date, exp: Date,
                                    lead: TimeInterval = refreshLead) -> TimeInterval {
        max(0, exp.timeIntervalSince(now) - lead)
    }

    /// True when the token should be refreshed before use. An UNKNOWN expiry
    /// returns false: we cannot schedule against what we cannot read, so opaque
    /// tokens are left to the reactive path rather than refreshed speculatively.
    static func needsRefresh(now: Date, exp: Date?, lead: TimeInterval = refreshLead) -> Bool {
        guard let exp = exp else { return false }
        return secondsUntilRefresh(now: now, exp: exp, lead: lead) <= 0
    }

    // MARK: - Refreshing

    /// Refresh only if the current access token is at/near expiry. Call this
    /// before USING the token: it is what makes "wake the Mac, click Start an
    /// hour later" work, where a timer alone would already have missed.
    @discardableResult
    func refreshIfNeeded() async -> Bool {
        guard needsRefreshBeforeUse else { return false }
        return await refreshNow(reason: "access token at/near expiry")
    }

    /// Redeem the refresh token for a new access token. Concurrent callers await
    /// the SAME network call. Returns false if refreshing is impossible,
    /// rate-limited, or Cognito refused.
    @discardableResult
    func refreshNow(reason: String) async -> Bool {
        guard let task = claimRefresh(reason: reason) else { return false }
        return await task.value
    }

    /// The in-flight refresh task, starting one if needed. nil when refreshing
    /// is impossible or too soon after the last attempt.
    private func claimRefresh(reason: String) -> Task<Bool, Never>? {
        lock.lock(); defer { lock.unlock() }
        if let t = inFlight { return t }
        guard !invalidated, !_refresh.isEmpty, !clientId.isEmpty else { return nil }
        if let last = lastAttempt, Date().timeIntervalSince(last) < Self.minRefreshInterval {
            return nil
        }
        lastAttempt = Date()
        let t = Task { await self.performRefresh(reason: reason) }
        inFlight = t
        return t
    }

    private func performRefresh(reason: String) async -> Bool {
        log("↻ refreshing Cognito access token (\(reason))…")
        do {
            let fresh = try await SRP.refresh(refreshToken: refreshToken,
                                              clientId: clientId, region: region)
            // Mutation is factored into sync helpers so no lock is taken from an
            // async context (illegal under Swift 6 concurrency checking).
            guard let (access, id) = commit(fresh) else { return false }
            let until = Self.expiry(ofJWT: access).map { " (valid until \($0))" } ?? ""
            log("✓ access token refreshed\(until)")
            notifyRefresh(access: access, id: id)
            armProactiveRefresh()
            return true
        } catch {
            let attempts = noteFailure()
            // A rejected refresh token is terminal: only an interactive sign-in
            // recovers, so stop retrying and let the caller decide what to tell
            // the user (TranscriberSocket turns this into onFatalAuth).
            if (error as? SRP.SRPError)?.isCredentialRejected == true {
                log("✗ Cognito rejected the refresh token — sign in again (\(error))")
                return false
            }
            log("✗ token refresh failed (\(error))")
            if attempts <= Self.maxTransientRetries { armRetry() }
            return false
        }
    }

    /// Store a successful refresh. Returns the new (access, id) pair, or nil if
    /// the user signed out while the call was in flight — in which case we must
    /// NOT resurrect their session.
    private func commit(_ fresh: SRP.RefreshedTokens) -> (String, String)? {
        lock.lock(); defer { lock.unlock() }
        inFlight = nil
        if invalidated { return nil }
        _access = fresh.accessToken
        if !fresh.idToken.isEmpty { _id = fresh.idToken }
        // Only present with rotation enabled; keeping the old one otherwise is
        // required, not merely tidy (see SRP.refresh).
        if let r = fresh.refreshToken, !r.isEmpty { _refresh = r }
        transientFailures = 0
        return (_access, _id)
    }

    /// Record a failed refresh; returns the consecutive-failure count.
    private func noteFailure() -> Int {
        lock.lock(); defer { lock.unlock() }
        inFlight = nil
        transientFailures += 1
        return transientFailures
    }

    // MARK: - Proactive schedule

    /// Arm (or re-arm) the proactive refresh for the CURRENT access token.
    ///
    /// Uses a WALL-clock deadline deliberately. `exp` is wall-clock, but
    /// DispatchTime's monotonic clock STOPS while the Mac is asleep — a
    /// monotonic timer set for 55 minutes would fire an hour late after a
    /// lunchtime sleep, i.e. precisely inside the window this bug lives in.
    /// `asyncAfter(wallDeadline:)` fires on schedule across sleep.
    func armProactiveRefresh() {
        guard canRefresh else { return }
        guard let exp = Self.expiry(ofJWT: accessToken) else {
            // Opaque/unreadable token (a hand-pasted one): nothing to schedule
            // against. refreshIfNeeded()/the reactive path still cover it.
            return
        }
        let delay = Self.secondsUntilRefresh(now: Date(), exp: exp)
        lock.lock(); armedFor = exp; lock.unlock()
        timerQueue.asyncAfter(wallDeadline: .now() + delay) { [weak self] in
            guard let self = self else { return }
            self.lock.lock()
            let stillCurrent = self.armedFor == exp && !self.invalidated
            self.lock.unlock()
            guard stillCurrent else { return }
            Task { await self.refreshNow(reason: "proactive, before expiry") }
        }
    }

    /// Re-arm after a transient failure so we get another go before the token
    /// actually dies, rather than waiting for a socket to 401.
    private func armRetry() {
        lock.lock(); armedFor = nil; lock.unlock()   // supersede the pending timer
        timerQueue.asyncAfter(wallDeadline: .now() + Self.retryInterval) { [weak self] in
            guard let self = self, self.canRefresh else { return }
            Task { await self.refreshNow(reason: "retry after failed refresh") }
        }
    }

    /// Sign-out: forget the credentials and stop refreshing. A pending timer
    /// finds `armedFor`/`invalidated` changed and does nothing; an in-flight
    /// refresh discards its result rather than restoring the session.
    func invalidate() {
        lock.lock()
        invalidated = true
        _access = ""; _id = ""; _refresh = ""
        armedFor = nil
        lock.unlock()
    }

    /// Hop the callbacks to the main queue. Deliberately non-async wrappers: a
    /// `DispatchQueue.main.async` closure created directly inside an async
    /// function is @Sendable-checked and warns about capturing `self`.
    private func notifyRefresh(access: String, id: String) {
        DispatchQueue.main.async { [weak self] in self?.onRefresh?(access, id) }
    }

    private func log(_ msg: String) {
        print(msg)
        DispatchQueue.main.async { [weak self] in self?.onLog?(msg) }
    }
}
