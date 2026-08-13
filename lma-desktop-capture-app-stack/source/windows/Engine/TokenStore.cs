using System.Text.Json;

namespace LMA;

/// <summary>
/// Keeps the Cognito ACCESS token fresh for as long as the process is signed in.
///
/// Why this exists (GitHub issue #535): the client signed in, kept the access
/// token forever, and threw the refresh token away. Cognito access tokens last
/// ~1 hour and the transcriber's jwtVerifier 401s an expired one, so any session
/// outliving the TTL — or any Start after an hour idle — failed the WebSocket
/// upgrade, ended the meeting, and demanded a fresh interactive sign-in.
///
/// Two complementary paths, because either alone leaves a hole:
///   • PROACTIVE — a timer armed `RefreshLead` seconds before the token's `exp`.
///     This is what PREVENTS the failure instead of recovering from it, and it
///     is what keeps a long meeting streaming without a visible blip.
///   • ON DEMAND / REACTIVE — `RefreshIfNeededAsync()` before we present the
///     token, and `RefreshNowAsync()` when the server rejects it anyway. Covers
///     the cases a timer cannot: a sleep that outlasted the deadline, client
///     clock skew, or a token invalidated server-side.
///
/// Tokens live HERE, in a shared reference object, rather than only on `Config`
/// — a socket built ten minutes ago must present the token we have NOW, so
/// TranscriberSocket reads through this object. (`Config` is a mutable class on
/// Windows, unlike macOS's struct, so its copies don't freeze — but routing
/// reads through the store keeps the ownership explicit and matches macOS.)
///
/// SLEEP/HIBERNATE (the Windows counterpart of macOS's wall-clock deadline):
/// `exp` is wall-clock. .NET timers (System.Threading.Timer / Task.Delay) queue
/// against Environment.TickCount64 = GetTickCount64, which INCLUDES time the
/// system spends in sleep or hibernation — so a timer whose due time elapsed
/// during a lunchtime sleep fires promptly at wake rather than an hour later
/// (unlike macOS's DispatchTime, whose monotonic clock stops while asleep).
/// Belt and braces anyway, because that behaviour is subtle and clock changes
/// exist: the timer callback re-checks the WALL clock and re-arms if it fired
/// early, and `SystemEvents.PowerModeChanged` (Resume) forces a re-evaluation
/// the moment the machine wakes. The on-demand path at connect time is the
/// final backstop.
///
/// Thread-safety: all mutable state is behind `_lock`; `RefreshNowAsync()`
/// coalesces concurrent callers onto a single in-flight network call.
/// </summary>
public sealed class TokenStore : IDisposable
{
    /// <summary>
    /// Refresh this many seconds BEFORE `exp`. Generous enough to absorb a slow
    /// network and modest client clock skew, small enough that we are not
    /// re-minting constantly against a ~1 h TTL.
    /// </summary>
    public const double RefreshLeadSeconds = 300;

    /// <summary>
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
    /// </summary>
    public const double MinRefreshIntervalSeconds = 20;

    /// <summary>
    /// Wait between retries after a TRANSIENT refresh failure (e.g. offline),
    /// so we still get another go before the token actually dies.
    /// </summary>
    public const double RetryIntervalSeconds = 60;

    /// <summary>
    /// Cap on consecutive transient retries. Past this we stop polling and let
    /// the on-demand path (`RefreshIfNeededAsync()` at connect time) recover —
    /// an app left offline for hours should not be talking to Cognito every
    /// minute.
    /// </summary>
    public const int MaxTransientRetries = 5;

    private readonly object _lock = new();
    private string _access;
    private string _id;
    private string _refresh;
    private readonly string _clientId;
    private readonly string _region;

    /// <summary>
    /// `exp` of the token the pending proactive timer was armed for. A timer
    /// whose captured `exp` no longer matches this is stale (a newer token, and
    /// a newer timer, has landed) and must do nothing.
    /// </summary>
    private DateTimeOffset? _armedFor;
    private DateTimeOffset? _lastAttempt;
    private Task<bool>? _inFlight;
    private int _transientFailures;
    /// <summary>
    /// Set by Invalidate() on sign-out: stops refreshing and prevents an
    /// in-flight refresh from writing tokens back after the user signed out.
    /// </summary>
    private bool _invalidated;

    private Timer? _timer;
    private bool _resumeHooked;

    /// <summary>
    /// Fired after a successful refresh so the owner can mirror the new tokens
    /// onto its own `Config` (which VideoSocket and `IsAuthenticated` read).
    /// </summary>
    public Action<string, string>? OnRefresh;

    /// <summary>
    /// Log sink. Refresh is invisible when it works, so these lines are the
    /// only evidence it happened — keep them.
    /// </summary>
    public Action<string>? OnLog;

    public TokenStore(string accessToken, string idToken, string refreshToken,
                      string clientId, string region)
    {
        _access = accessToken;
        _id = idToken;
        _refresh = refreshToken;
        _clientId = clientId;
        _region = region;
    }

    // MARK: - Current tokens

    public string AccessToken { get { lock (_lock) { return _access; } } }
    public string IdToken { get { lock (_lock) { return _id; } } }
    public string RefreshToken { get { lock (_lock) { return _refresh; } } }

    /// <summary>
    /// False when there is nothing to redeem — a hand-pasted `--token` with no
    /// `--refresh-token`, or after sign-out. Callers must fall back to their
    /// pre-existing "give up and ask the user" behaviour in that case.
    /// </summary>
    public bool CanRefresh
    {
        get
        {
            lock (_lock)
            {
                return !_invalidated && _refresh.Length > 0 && _clientId.Length > 0;
            }
        }
    }

    /// <summary>
    /// True when the access token is inside the lead window (or already dead)
    /// and should be renewed BEFORE we present it.
    /// </summary>
    public bool NeedsRefreshBeforeUse
    {
        get
        {
            if (!CanRefresh) return false;
            return NeedsRefresh(DateTimeOffset.UtcNow, ExpiryOfJwt(AccessToken));
        }
    }

    // MARK: - Pure scheduling logic (pinned by SelfTest)

    /// <summary>
    /// JWT payload claims, WITHOUT verifying the signature — we only read `exp`
    /// to schedule our own refresh. Validating the token is the SERVER's job
    /// (jwt-verifier.ts); trusting `exp` here can at worst make us refresh at
    /// the wrong moment, which the reactive path then corrects.
    /// </summary>
    public static JsonElement? JwtPayload(string jwt)
    {
        var parts = jwt.Split('.');
        if (parts.Length != 3) return null;
        // base64url → base64, then re-pad to a multiple of 4.
        var b = parts[1].Replace('-', '+').Replace('_', '/');
        while (b.Length % 4 != 0) b += "=";
        try
        {
            var data = Convert.FromBase64String(b);
            using var doc = JsonDocument.Parse(data);
            if (doc.RootElement.ValueKind != JsonValueKind.Object) return null;
            return doc.RootElement.Clone();
        }
        catch
        {
            return null;
        }
    }

    /// <summary>The token's expiry, or null if it isn't a readable JWT with a numeric `exp`.</summary>
    public static DateTimeOffset? ExpiryOfJwt(string jwt)
    {
        var payload = JwtPayload(jwt);
        if (payload is not { } p) return null;
        if (!p.TryGetProperty("exp", out var expEl) || expEl.ValueKind != JsonValueKind.Number) return null;
        if (!expEl.TryGetDouble(out var exp)) return null;
        return DateTimeOffset.FromUnixTimeMilliseconds((long)(exp * 1000));
    }

    /// <summary>
    /// Seconds to wait before proactively refreshing a token expiring at `exp`.
    /// 0 means "already inside the lead window — refresh now".
    /// </summary>
    public static double SecondsUntilRefresh(DateTimeOffset now, DateTimeOffset exp,
                                             double lead = RefreshLeadSeconds)
    {
        return Math.Max(0, (exp - now).TotalSeconds - lead);
    }

    /// <summary>
    /// True when the token should be refreshed before use. An UNKNOWN expiry
    /// returns false: we cannot schedule against what we cannot read, so opaque
    /// tokens are left to the reactive path rather than refreshed speculatively.
    /// </summary>
    public static bool NeedsRefresh(DateTimeOffset now, DateTimeOffset? exp,
                                    double lead = RefreshLeadSeconds)
    {
        if (exp is not { } e) return false;
        return SecondsUntilRefresh(now, e, lead) <= 0;
    }

    // MARK: - Refreshing

    /// <summary>
    /// Refresh only if the current access token is at/near expiry. Call this
    /// before USING the token: it is what makes "wake the PC, click Start an
    /// hour later" work, where a timer alone would already have missed.
    /// </summary>
    public async Task<bool> RefreshIfNeededAsync()
    {
        if (!NeedsRefreshBeforeUse) return false;
        return await RefreshNowAsync("access token at/near expiry");
    }

    /// <summary>
    /// Redeem the refresh token for a new access token. Concurrent callers await
    /// the SAME network call. Returns false if refreshing is impossible,
    /// rate-limited, or Cognito refused.
    /// </summary>
    public async Task<bool> RefreshNowAsync(string reason)
    {
        var task = ClaimRefresh(reason);
        if (task == null) return false;
        return await task;
    }

    /// <summary>
    /// The in-flight refresh task, starting one if needed. null when refreshing
    /// is impossible or too soon after the last attempt.
    /// </summary>
    private Task<bool>? ClaimRefresh(string reason)
    {
        lock (_lock)
        {
            if (_inFlight != null) return _inFlight;
            if (_invalidated || _refresh.Length == 0 || _clientId.Length == 0) return null;
            if (_lastAttempt is { } last &&
                (DateTimeOffset.UtcNow - last).TotalSeconds < MinRefreshIntervalSeconds)
            {
                return null;
            }
            _lastAttempt = DateTimeOffset.UtcNow;
            var t = Task.Run(() => PerformRefreshAsync(reason));
            _inFlight = t;
            return t;
        }
    }

    private async Task<bool> PerformRefreshAsync(string reason)
    {
        Log($"↻ refreshing Cognito access token ({reason})…");
        try
        {
            var fresh = await Srp.RefreshAsync(RefreshToken, _clientId, _region);
            string access, id;
            lock (_lock)
            {
                _inFlight = null;
                // The user signed out while the call was in flight: we must NOT
                // resurrect their session.
                if (_invalidated) return false;
                _access = fresh.AccessToken;
                if (!string.IsNullOrEmpty(fresh.IdToken)) _id = fresh.IdToken;
                // Only present with rotation enabled; keeping the old one
                // otherwise is required, not merely tidy (see Srp.RefreshAsync).
                if (!string.IsNullOrEmpty(fresh.RefreshToken)) _refresh = fresh.RefreshToken!;
                _transientFailures = 0;
                access = _access;
                id = _id;
            }
            var exp = ExpiryOfJwt(access);
            Log($"✓ access token refreshed{(exp is { } e ? $" (valid until {e:u})" : "")}");
            OnRefresh?.Invoke(access, id);
            ArmProactiveRefresh();
            return true;
        }
        catch (Exception error)
        {
            int attempts;
            lock (_lock)
            {
                _inFlight = null;
                _transientFailures++;
                attempts = _transientFailures;
            }
            // A rejected refresh token is terminal: only an interactive sign-in
            // recovers, so stop retrying and let the caller decide what to tell
            // the user (TranscriberSocket turns this into OnFatalAuth).
            if (error is Srp.SrpException { IsCredentialRejected: true })
            {
                Log($"✗ Cognito rejected the refresh token — sign in again ({error.Message})");
                return false;
            }
            Log($"✗ token refresh failed ({error.Message})");
            if (attempts <= MaxTransientRetries) ArmRetry();
            return false;
        }
    }

    // MARK: - Proactive schedule

    /// <summary>
    /// Arm (or re-arm) the proactive refresh for the CURRENT access token, and
    /// hook system resume so a sleep that outlasted the deadline is caught the
    /// moment the machine wakes (see the sleep note in the class comment).
    /// </summary>
    public void ArmProactiveRefresh()
    {
        if (!CanRefresh) return;
        HookResume();
        if (ExpiryOfJwt(AccessToken) is not { } exp)
        {
            // Opaque/unreadable token (a hand-pasted one): nothing to schedule
            // against. RefreshIfNeededAsync()/the reactive path still cover it.
            return;
        }
        var delay = SecondsUntilRefresh(DateTimeOffset.UtcNow, exp);
        lock (_lock)
        {
            _armedFor = exp;
            _timer?.Dispose();
            _timer = new Timer(_ => OnProactiveTimer(exp), null,
                               TimeSpan.FromSeconds(delay), Timeout.InfiniteTimeSpan);
        }
    }

    private void OnProactiveTimer(DateTimeOffset armedExp)
    {
        bool stillCurrent;
        lock (_lock) { stillCurrent = _armedFor == armedExp && !_invalidated; }
        if (!stillCurrent) return;
        // Re-check the WALL clock before spending a refresh: if the timer fired
        // early relative to wall time (a backwards clock change), re-arm for the
        // remainder instead of re-minting a token that is still fresh.
        if (!NeedsRefresh(DateTimeOffset.UtcNow, armedExp))
        {
            ArmProactiveRefresh();
            return;
        }
        _ = RefreshNowAsync("proactive, before expiry");
    }

    /// <summary>
    /// Re-arm after a transient failure so we get another go before the token
    /// actually dies, rather than waiting for a socket to 401.
    /// </summary>
    private void ArmRetry()
    {
        lock (_lock)
        {
            _armedFor = null;   // supersede the pending proactive timer
            _timer?.Dispose();
            _timer = new Timer(_ =>
            {
                if (CanRefresh) _ = RefreshNowAsync("retry after failed refresh");
            }, null, TimeSpan.FromSeconds(RetryIntervalSeconds), Timeout.InfiniteTimeSpan);
        }
    }

    /// <summary>
    /// On resume from sleep/hibernate, re-evaluate immediately: refresh if the
    /// deadline passed while asleep, otherwise re-arm (correcting any drift).
    /// Subscribed once; SystemEvents runs its own broadcast thread, so this
    /// works in both the tray app and the headless CLI.
    /// </summary>
    private void HookResume()
    {
        lock (_lock)
        {
            if (_resumeHooked || _invalidated) return;
            _resumeHooked = true;
        }
        try
        {
            Microsoft.Win32.SystemEvents.PowerModeChanged += OnPowerModeChanged;
        }
        catch
        {
            // Best-effort (e.g. no session for the broadcast window). The
            // wall-clock timer + connect-time on-demand refresh still cover it.
            lock (_lock) { _resumeHooked = false; }
        }
    }

    private void OnPowerModeChanged(object sender, Microsoft.Win32.PowerModeChangedEventArgs e)
    {
        if (e.Mode != Microsoft.Win32.PowerModes.Resume) return;
        if (!CanRefresh) return;
        if (NeedsRefreshBeforeUse)
        {
            _ = RefreshNowAsync("resumed from sleep past the refresh deadline");
        }
        else
        {
            ArmProactiveRefresh();
        }
    }

    /// <summary>
    /// Sign-out: forget the credentials and stop refreshing. A pending timer
    /// finds `_armedFor`/`_invalidated` changed and does nothing; an in-flight
    /// refresh discards its result rather than restoring the session.
    /// </summary>
    public void Invalidate()
    {
        bool unhook;
        lock (_lock)
        {
            _invalidated = true;
            _access = ""; _id = ""; _refresh = "";
            _armedFor = null;
            _timer?.Dispose();
            _timer = null;
            unhook = _resumeHooked;
            _resumeHooked = false;
        }
        if (unhook)
        {
            try { Microsoft.Win32.SystemEvents.PowerModeChanged -= OnPowerModeChanged; }
            catch { /* best-effort */ }
        }
    }

    public void Dispose() => Invalidate();

    private void Log(string msg)
    {
        Console.WriteLine(msg);
        OnLog?.Invoke(msg);
    }
}
