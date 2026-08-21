using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace LMA;

/// <summary>
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
/// whitelist that does NOT include `Authorization`. CloudFront therefore STRIPS
/// the Authorization header before it reaches the origin, so header auth yields
/// a 401. QueryStringBehavior is `all`, so the query string survives. We must
/// authenticate the browser's way: query params. The Node CLI gets away with
/// headers only because it connects straight to the origin/ALB, bypassing CloudFront.
///
/// Ported from macOS TranscriberSocket.swift; uses ClientWebSocket (BCL).
/// </summary>
public sealed class TranscriberSocket
{
    private readonly Config _config;
    /// <summary>
    /// Source of truth for the tokens we present, and the thing that keeps them
    /// alive (issue #535). Read through it rather than through `_config` so a
    /// socket created before a refresh presents the token we have NOW, and so
    /// token ownership is explicit (mirrors macOS, where Config is a struct and
    /// reading through it presented frozen stale copies). null only when there
    /// is nothing to refresh with (a hand-pasted `--token` with no refresh
    /// token), in which case we fall back to `_config` and the old give-up
    /// behaviour.
    /// </summary>
    private readonly TokenStore? _tokens;
    private ClientWebSocket? _ws;
    private CancellationTokenSource? _cts;
    private volatile bool _isOpen;
    private readonly object _sendLock = new();

    // --- Reconnect state ----------------------------------------------------
    // The server has NO session resume: on any drop we must open a NEW socket
    // and send a FRESH START (same callId) before audio. We reconnect with
    // capped exponential backoff until intentionalClose is set (Stop/Ctrl-C).
    private volatile bool _intentionalClose;
    private int _reconnectAttempt;
    private const double MaxBackoff = 10;

    // Distinguish a bad-token/handshake failure (fatal — no point hammering the
    // server with an expired token) from a transient network drop. An
    // auth-looking rejection counts toward the fatal threshold even after a
    // successful open (see CountHandshakeFailure); a mid-call NETWORK blip does
    // not, which is what `_everOpened` still guards.
    private bool _everOpened;
    private int _handshakeFailures;
    private const int MaxHandshakeFailures = 4;

    // --- Connect state ------------------------------------------------------
    // Guarded by `_stateLock`: these are touched from the caller's thread, the
    // reconnect timer's thread-pool continuation, AND the refresh task, so
    // unsynchronised access is a genuine data race.
    private readonly object _stateLock = new();
    // The access token this socket actually presented on the current attempt.
    // Compared against the store's token to spot "someone else already
    // refreshed while I was failing" — then a plain retry is enough and we
    // needn't spend a refresh.
    private string _presentedAccessToken = "";
    // One reactive refresh per connection cycle (reset on a successful open), so
    // a server 401ing for a NON-expiry reason can't turn into a refresh loop.
    private bool _refreshAttemptedThisCycle;
    /// <summary>
    /// True from the moment a connect attempt begins until it resolves (open or
    /// error). Without this, two attempts can overlap and the loser is ORPHANED:
    /// `_ws`/`_cts` get overwritten while the abandoned socket may still open,
    /// send a SECOND START for the same callId (duplicating the meeting
    /// server-side), and keep mutating the failure counters. Two callers make
    /// this reachable: ConnectAsync awaits a token refresh before opening, and
    /// a reconnect timer can fire during that window (the same double-connect
    /// race caught in review of the macOS fix, PR #572).
    /// </summary>
    private bool _connectInFlight;

    // --- Reconnect buffering ------------------------------------------------
    // Briefly hold PCM produced while the socket is down so a short reconnect
    // doesn't punch an audio gap. Bounded so a long outage can't grow unbounded.
    private readonly List<byte[]> _pending = new();
    private const int MaxPendingBytes = 48000 * 2 * 2 * 3;   // ~3s of stereo audio
    private int _pendingBytes;
    private int _droppedWhileDown;

    /// <summary>Callback so the UI/meter can reflect live connection state.</summary>
    public Action<bool>? OnStateChange;

    /// <summary>
    /// Fired when auth is broken in a way reconnecting cannot fix — the token
    /// was rejected AND could not be refreshed. The OWNER decides what that
    /// means: the tray app stops the stream and returns to the sign-in form;
    /// the headless CLI exits non-zero.
    /// </summary>
    public Action<string>? OnFatalAuth;

    /// <summary>User-facing text for a hopeless auth state (the GUI shows this verbatim).</summary>
    public const string FatalAuthMessage =
        "Your session expired and could not be renewed. Please sign in again.";

    private readonly object _reconnectLock = new();
    private bool _reconnectScheduled;

    public TranscriberSocket(Config config, TokenStore? tokens = null)
    {
        _config = config;
        _tokens = tokens;
    }

    // Present whatever the store holds NOW, falling back to the config copy
    // when there is no store (pasted-token path).
    private string AccessToken => _tokens?.AccessToken ?? _config.AccessToken;
    private string IdToken => _tokens?.IdToken ?? _config.IdToken;

    // MARK: Connect-state helpers (all lock-guarded — see `_stateLock`)

    /// <summary>
    /// Claim the right to open a socket. Returns false when an attempt is
    /// already in flight (or we're shutting down), in which case the caller
    /// simply DROPS its retry: whoever holds the attempt will either succeed or
    /// fail into HandleTaskError, which schedules the next one.
    /// </summary>
    private bool BeginConnectAttempt()
    {
        lock (_stateLock)
        {
            if (_connectInFlight || _intentionalClose) return false;
            _connectInFlight = true;
            return true;
        }
    }

    /// <summary>Mark the current attempt resolved (opened, or failed). Idempotent.</summary>
    private void EndConnectAttempt()
    {
        lock (_stateLock) { _connectInFlight = false; }
    }

    public void Connect()
    {
        _ = ConnectAsync();
    }

    private async Task ConnectAsync()
    {
        // Single-flight: never let two attempts overlap (see `_connectInFlight`).
        if (!BeginConnectAttempt()) return;

        // Renew FIRST when the token we are about to present is at/near expiry.
        // Without this, a Start after the PC woke from sleep — or after an hour
        // idle in the tray — opens a socket we already know will 401, and the
        // user watches it fail before it recovers.
        if (_tokens is { NeedsRefreshBeforeUse: true })
        {
            // Open regardless of the refresh outcome: if it failed transiently
            // the old token may still work, and if it failed for good the
            // handshake's own error path produces the right message.
            await _tokens.RefreshIfNeededAsync();
            if (_intentionalClose) { EndConnectAttempt(); return; }
        }

        // Append auth as query params (see AUTH NOTE above) — CloudFront forwards
        // the query string but strips the Authorization header. Mirrors the
        // browser client's useWebSocket queryParams.
        var access = AccessToken;
        var idToken = IdToken;
        lock (_stateLock) { _presentedAccessToken = access; }

        var builder = new UriBuilder(_config.Endpoint);
        var query = new StringBuilder(builder.Query.TrimStart('?'));
        void AddParam(string k, string v)
        {
            if (query.Length > 0) query.Append('&');
            query.Append(Uri.EscapeDataString(k)).Append('=').Append(Uri.EscapeDataString(v));
        }
        AddParam("authorization", $"Bearer {access}");
        if (!string.IsNullOrEmpty(idToken)) AddParam("id_token", idToken);
        // NOTE: `refresh_token` is deliberately NOT sent. The server only copies
        // it into the Kinesis START/END events as `RefreshToken` and has no
        // consumer for it, so sending the real one would persist a long-lived
        // credential into CloudFront/ALB access logs and the event stream for no
        // benefit. Omitting beats the old `refresh_token=""` placeholder, which
        // claimed to carry something and carried nothing. The param is optional
        // server-side (`refreshToken?: string` — see index.ts / eventtypes.ts).
        builder.Query = query.ToString();
        var url = builder.Uri;

        var ws = new ClientWebSocket();
        // Keep the response around on a failed upgrade so a 401/403 is readable
        // as a STATUS CODE (ws.HttpStatusCode) instead of having to be scraped
        // out of the exception message — that is what drives the auth-vs-
        // transient classification below.
        ws.Options.CollectHttpResponseDetails = true;
        // Also set the headers — harmless if forwarded, and lets this same client
        // work if pointed straight at the origin/ALB (no CloudFront) in future.
        try
        {
            ws.Options.SetRequestHeader("authorization", $"Bearer {access}");
            if (!string.IsNullOrEmpty(idToken)) ws.Options.SetRequestHeader("id_token", idToken);
        }
        catch { /* some headers are restricted; query params carry the auth anyway */ }

        _ws = ws;
        _cts = new CancellationTokenSource();
        try
        {
            await ws.ConnectAsync(url, _cts.Token);
        }
        catch (Exception err)
        {
            // Failed WS upgrade — including a rejected handshake (bad/expired token → 401).
            int? status = null;
            try
            {
                var code = (int)ws.HttpStatusCode;
                if (code != 0) status = code;   // 0 = never reached an HTTP response
            }
            catch { /* not collected */ }
            EndConnectAttempt();      // this attempt resolved (in failure)
            if (_intentionalClose) return;
            HandleTaskError(status, err);
            return;
        }

        // didOpen: healthy connection resets backoff, sends a fresh START, flushes buffer.
        _isOpen = true;
        EndConnectAttempt();      // this attempt resolved — a later drop may retry
        _reconnectAttempt = 0;
        lock (_stateLock)
        {
            _everOpened = true;
            _handshakeFailures = 0;
            _refreshAttemptedThisCycle = false;
        }
        Console.WriteLine($"✓ WebSocket open → {_config.Endpoint}");
        OnStateChange?.Invoke(true);
        SendStart();       // fresh START every (re)connect; server has no resume
        FlushPending();    // replay audio buffered during the outage
        _ = ReceiveLoop(ws, _cts.Token);
    }

    /// <summary>
    /// START handshake — must be the first frame; audio before this is dropped
    /// server-side.
    ///
    /// Sent afresh on EVERY (re)connect, so the diarization flags must be included
    /// here rather than only on the first connect — otherwise speaker
    /// identification would silently switch itself off after a network blip.
    /// </summary>
    public void SendStart()
    {
        var meta = new Dictionary<string, object>
        {
            ["callId"] = _config.CallId,
            ["agentId"] = _config.AgentId,       // mic channel (ch_1 → AGENT)
            ["fromNumber"] = _config.FromNumber, // meeting channel (ch_0 → CALLER)
            ["toNumber"] = _config.ToNumber,
            ["samplingRate"] = _config.SampleRate,
            ["callEvent"] = "START",
            ["diarizeSystemChannel"] = _config.DiarizeSystemChannel,
            ["diarizeMicChannel"] = _config.DiarizeMicChannel,
        };
        // Omitted when unset so the server keeps its own default, rather than this
        // client pinning every meeting to one engine by accident.
        if (!string.IsNullOrWhiteSpace(_config.AsrEngine))
        {
            meta["asrEngine"] = _config.AsrEngine;
        }
        SendJson(meta, "START");
    }

    public void SendEnd()
    {
        var meta = new Dictionary<string, object>
        {
            ["callId"] = _config.CallId,
            ["agentId"] = _config.AgentId,
            ["fromNumber"] = _config.FromNumber,
            ["toNumber"] = _config.ToNumber,
            ["samplingRate"] = _config.SampleRate,
            ["callEvent"] = "END",
        };
        SendJson(meta, "END");
    }

    /// <summary>
    /// Interleaved 16-bit LE PCM chunk → binary WS frame. When the socket is down
    /// (reconnecting), buffer up to MaxPendingBytes so a brief drop doesn't gap
    /// the audio; beyond that we drop oldest and count it.
    /// </summary>
    public void SendPcm(byte[] data)
    {
        lock (_sendLock)
        {
            if (!_isOpen || _ws is not { State: WebSocketState.Open })
            {
                _pending.Add(data);
                _pendingBytes += data.Length;
                while (_pendingBytes > MaxPendingBytes && _pending.Count > 0)
                {
                    var d = _pending[0];
                    _pending.RemoveAt(0);
                    _pendingBytes -= d.Length;
                    _droppedWhileDown += d.Length;
                }
                return;
            }
            var ws = _ws;
            var ct = _cts?.Token ?? CancellationToken.None;
            _ = ws.SendAsync(new ArraySegment<byte>(data), WebSocketMessageType.Binary, true, ct)
                .ContinueWith(t =>
                {
                    if (t.Exception != null && !_intentionalClose)
                        Console.Error.WriteLine($"PCM send error: {t.Exception.GetBaseException().Message}");
                }, TaskContinuationOptions.OnlyOnFaulted);
        }
    }

    /// <summary>Flush buffered PCM after a reconnect (called once the new START is sent).</summary>
    private void FlushPending()
    {
        lock (_sendLock)
        {
            if (!_isOpen || _pending.Count == 0 || _ws is not { State: WebSocketState.Open }) return;
            int count = _pending.Count;
            int dropped = _droppedWhileDown;
            var ws = _ws;
            var ct = _cts?.Token ?? CancellationToken.None;
            foreach (var d in _pending)
                _ = ws.SendAsync(new ArraySegment<byte>(d), WebSocketMessageType.Binary, true, ct);
            _pending.Clear();
            _pendingBytes = 0;
            _droppedWhileDown = 0;
            var msg = $"↺ flushed {count} buffered PCM frames after reconnect";
            if (dropped > 0) msg += $" (dropped {dropped / (48000 * 2 * 2)}s that overflowed the buffer)";
            Console.WriteLine(msg);
        }
    }

    /// <summary>
    /// Mark that we are intentionally shutting down. Call this BEFORE SendEnd()
    /// so the subsequent socket teardown is treated as expected and stays quiet —
    /// no scary errors or reconnect attempts on a normal Stop.
    /// </summary>
    public void BeginClose()
    {
        _intentionalClose = true;
    }

    public void Close()
    {
        _intentionalClose = true;
        try
        {
            var ws = _ws;
            if (ws is { State: WebSocketState.Open })
                _ = ws.CloseAsync(WebSocketCloseStatus.NormalClosure, null, CancellationToken.None);
        }
        catch { /* ignore */ }
        try { _cts?.Cancel(); } catch { }
        _isOpen = false;
    }

    /// <summary>
    /// Schedule a reconnect after an UNEXPECTED drop. Opens a new socket; the open
    /// path re-sends a fresh START (server has no resume). Idempotent per drop:
    /// guarded so the receive-failure and close paths don't double up. This is the
    /// ONE route every retry takes — including a retry after a reactive token
    /// refresh. There is deliberately no retry-immediately shortcut: it could
    /// open a second socket alongside a reconnect whose timer had already fired,
    /// orphaning one of them mid-call (the double-connect race from the macOS
    /// review). The pending reconnect reads tokens through the store at connect
    /// time, so it picks up a fresh token anyway; the cost is ≤ the backoff delay.
    /// </summary>
    private void ScheduleReconnect(string why)
    {
        lock (_reconnectLock)
        {
            if (_intentionalClose || _reconnectScheduled) return;
            _reconnectScheduled = true;
            _isOpen = false;
            _reconnectAttempt++;
            double delay = BackoffDelay(_reconnectAttempt, MaxBackoff);
            Console.WriteLine($"⟳ WS reconnect #{_reconnectAttempt} in {delay:F1}s ({why})");
            _ = Task.Delay(TimeSpan.FromSeconds(delay)).ContinueWith(_ =>
            {
                if (_intentionalClose) return;
                lock (_reconnectLock) { _reconnectScheduled = false; }
                Connect(); // new socket; open path sends fresh START
            });
        }
    }

    /// <summary>
    /// Capped exponential backoff: 0.5s, 1s, 2s, 4s, 8s, then `cap`.
    /// Pure + static so `--selftest` can pin the curve.
    /// </summary>
    public static double BackoffDelay(int attempt, double cap = 10)
    {
        return Math.Min(cap, Math.Pow(2.0, Math.Max(1, attempt) - 1) * 0.5);
    }

    private void SendJson(Dictionary<string, object> obj, string label)
    {
        var ws = _ws;
        if (ws is not { State: WebSocketState.Open }) return;
        var json = JsonSerializer.Serialize(obj);
        var bytes = Encoding.UTF8.GetBytes(json);
        var ct = _cts?.Token ?? CancellationToken.None;
        _ = ws.SendAsync(new ArraySegment<byte>(bytes), WebSocketMessageType.Text, true, ct)
            .ContinueWith(t =>
            {
                if (t.Exception != null)
                    Console.Error.WriteLine($"{label} send error: {t.Exception.GetBaseException().Message}");
                else
                    Console.WriteLine($"→ sent {label}");
            });
    }

    private async Task ReceiveLoop(ClientWebSocket ws, CancellationToken ct)
    {
        var buffer = new byte[8192];
        try
        {
            while (ws.State == WebSocketState.Open && !ct.IsCancellationRequested)
            {
                var result = await ws.ReceiveAsync(new ArraySegment<byte>(buffer), ct);
                if (result.MessageType == WebSocketMessageType.Close)
                {
                    _isOpen = false;
                    OnStateChange?.Invoke(false);
                    if (_intentionalClose) return;   // normal Stop/close — nothing to report
                    Console.WriteLine($"✗ WebSocket closed (code {(int?)ws.CloseStatus}) {ws.CloseStatusDescription}");
                    ScheduleReconnect($"server closed {(int?)ws.CloseStatus}");
                    return;
                }
                if (result.MessageType == WebSocketMessageType.Text)
                {
                    // The transcriber doesn't send app-level frames back; log anything unexpected.
                    var s = Encoding.UTF8.GetString(buffer, 0, result.Count);
                    Console.WriteLine($"← server: {s}");
                }
            }
            // Loop exited without an explicit close frame (aborted socket).
            if (!_intentionalClose)
            {
                _isOpen = false;
                OnStateChange?.Invoke(false);
                ScheduleReconnect("receive ended");
            }
        }
        catch (Exception err)
        {
            // Expected during an intentional Stop/close — stay silent (the pending
            // receive always errors when we cancel/abort the socket).
            if (_intentionalClose) return;
            // Redacted: some socket errors embed the request URL, which carries
            // the access token in its query string.
            Console.Error.WriteLine($"WS receive/closed: {RedactTokens(err.Message)}");
            _isOpen = false;
            OnStateChange?.Invoke(false);
            ScheduleReconnect("receive error");
        }
    }

    // Fires when the connect fails — including a rejected WS upgrade
    // (bad/expired token → 401, readable via ws.HttpStatusCode).
    private void HandleTaskError(int? status, Exception error)
    {
        _isOpen = false;
        OnStateChange?.Invoke(false);
        if (_intentionalClose) return;

        // An EXPIRED access token is by far the most common cause of a rejected
        // upgrade (issue #535). Renew and retry BEFORE this counts toward the
        // fatal threshold, so the user never sees it. Only when refreshing
        // genuinely fails does it become an error worth surfacing.
        if (IsAuthFailure(status, error.Message) && _tokens is { CanRefresh: true } tokens)
        {
            string presented;
            bool alreadyTried;
            lock (_stateLock)
            {
                presented = _presentedAccessToken;
                alreadyTried = _refreshAttemptedThisCycle;
                if (!alreadyTried) _refreshAttemptedThisCycle = true;
            }

            if (presented != tokens.AccessToken)
            {
                // Something already refreshed while this socket was failing (the
                // proactive timer): reconnecting is enough, don't spend another
                // refresh. The pending reconnect reads the token through the
                // store at connect time, so it picks up the new one automatically.
                ScheduleReconnect("newer access token available");
                return;
            }
            if (!alreadyTried)
            {
                _ = Task.Run(async () =>
                {
                    var ok = await tokens.RefreshNowAsync("WebSocket upgrade rejected the access token");
                    if (_intentionalClose) return;
                    if (ok)
                    {
                        // Reconnect through the SAME guarded path as every other
                        // retry — see the no-immediate-retry note on ScheduleReconnect.
                        ScheduleReconnect("refreshed access token");
                    }
                    else
                    {
                        CountHandshakeFailure(status, error);
                    }
                });
                return;
            }
        }
        CountHandshakeFailure(status, error);
    }

    /// <summary>
    /// Count a failed handshake and decide whether auth is now hopeless.
    ///
    /// `_everOpened` still keeps a mid-call NETWORK blip from being misread as
    /// bad auth, but an auth-looking rejection is now judged on its own merits
    /// even after a successful open: on a call running longer than the token's
    /// TTL the token expires UNDER an already-open socket, and the old
    /// `!_everOpened` guard skipped the counter entirely there — leaving the
    /// client to reconnect-loop forever against a dead token instead of saying
    /// so (issue #535).
    /// </summary>
    private void CountHandshakeFailure(int? status, Exception error)
    {
        bool authLooking = IsAuthFailure(status, error.Message);
        bool countIt;
        int failures;
        lock (_stateLock)
        {
            countIt = !_everOpened || authLooking;
            if (countIt) _handshakeFailures++;
            failures = _handshakeFailures;
        }
        if (countIt && failures >= MaxHandshakeFailures)
        {
            var msg =
                $"✗ WebSocket handshake keeps failing ({failures} attempts).\n" +
                "  This is almost always an EXPIRED or INVALID access token (the server\n" +
                $"  returns 401, seen here as HTTP {(status is { } s ? s.ToString() : "-")}). Cognito access\n" +
                "  tokens last ~1 hour and this client could not renew it — sign in again.\n" +
                $"  Underlying error: {RedactTokens(error.Message)}\n";
            Console.Error.WriteLine(msg);
            _intentionalClose = true;
            OnFatalAuth?.Invoke(FatalAuthMessage);
            return;
        }
        // Transient (or early) failure: let ScheduleReconnect handle backoff.
        ScheduleReconnect("connect error");
    }

    /// <summary>
    /// Does this failure look like the SERVER rejecting our token, rather than
    /// the network being unavailable? Pure + static so `--selftest` can pin it.
    ///
    /// A refused WS upgrade surfaces two ways on ClientWebSocket:
    ///   • CollectHttpResponseDetails gives us the HTTP status → 401/403 from
    ///     the server's jwtVerifier (the authoritative signal — measured against
    ///     the live transcriber, ws.HttpStatusCode reads 401 after the failed
    ///     ConnectAsync).
    ///   • no readable status (older path / details not collected) — fall back
    ///     to the WebSocketException message, which names the code exactly:
    ///     "The server returned status code '401' when status code '101' was
    ///     expected." Matched as a quoted 3-digit code, NOT a bare substring, so
    ///     an unrelated number in a message can't misclassify.
    /// Everything else (offline, DNS, timeout, TLS, 5xx) is transient. Getting
    /// this wrong in the permissive direction would spend refreshes on outages;
    /// in the strict direction it would leave expired tokens unrefreshed.
    /// </summary>
    public static bool IsAuthFailure(int? status, string? errorMessage)
    {
        if (status is { } s) return s == 401 || s == 403;
        if (string.IsNullOrEmpty(errorMessage)) return false;
        var m = Regex.Match(errorMessage, @"status code '(\d{3})'");
        if (m.Success && int.TryParse(m.Groups[1].Value, out var code))
            return code == 401 || code == 403;
        return false;
    }

    /// <summary>
    /// Strip JWT-shaped substrings out of text destined for a log.
    ///
    /// Handshake errors can embed the failing URL, and ours carries the access
    /// token in the query string (see the AUTH NOTE) — so printing one verbatim
    /// writes a LIVE credential to stdout/stderr, where it can be captured by a
    /// launcher, pasted into a bug report, or shipped to a log collector.
    /// Redacting only the token keeps the rest of the diagnostic (host, path,
    /// error code) intact, and catches the percent-encoded `Bearer%20eyJ…` form
    /// too (base64url characters and '.' survive percent-encoding unchanged).
    ///
    /// Cognito access/id tokens are always JWTs, hence matching on that shape.
    /// </summary>
    public static string RedactTokens(string text)
    {
        // header.payload.signature — base64url segments, header always "eyJ…".
        return Regex.Replace(text, "eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+",
                             "<redacted-jwt>");
    }
}
