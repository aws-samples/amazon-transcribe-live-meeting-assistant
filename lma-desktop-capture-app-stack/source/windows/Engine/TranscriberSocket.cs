using System.Net.WebSockets;
using System.Text;
using System.Text.Json;

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
    // server with an expired token) from a transient network drop. We only ever
    // succeeded if the socket opened at least once; if we never open AND keep
    // failing the handshake, that's almost certainly auth — warn loudly and give up.
    private bool _everOpened;
    private int _handshakeFailures;
    private const int MaxHandshakeFailures = 4;

    // --- Reconnect buffering ------------------------------------------------
    // Briefly hold PCM produced while the socket is down so a short reconnect
    // doesn't punch an audio gap. Bounded so a long outage can't grow unbounded.
    private readonly List<byte[]> _pending = new();
    private const int MaxPendingBytes = 48000 * 2 * 2 * 3;   // ~3s of stereo audio
    private int _pendingBytes;
    private int _droppedWhileDown;

    /// <summary>Callback so the UI/meter can reflect live connection state.</summary>
    public Action<bool>? OnStateChange;

    /// <summary>Fatal-auth callback: fired when the handshake keeps failing (expired/invalid token).</summary>
    public Action<string>? OnFatalAuth;

    private readonly object _reconnectLock = new();
    private bool _reconnectScheduled;

    public TranscriberSocket(Config config)
    {
        _config = config;
    }

    public void Connect()
    {
        _ = ConnectAsync();
    }

    private async Task ConnectAsync()
    {
        // Append auth as query params (see AUTH NOTE above) — CloudFront forwards
        // the query string but strips the Authorization header. Mirrors the
        // browser client's useWebSocket queryParams.
        var builder = new UriBuilder(_config.Endpoint);
        var query = new StringBuilder(builder.Query.TrimStart('?'));
        void AddParam(string k, string v)
        {
            if (query.Length > 0) query.Append('&');
            query.Append(Uri.EscapeDataString(k)).Append('=').Append(Uri.EscapeDataString(v));
        }
        AddParam("authorization", $"Bearer {_config.AccessToken}");
        if (!string.IsNullOrEmpty(_config.IdToken)) AddParam("id_token", _config.IdToken);
        AddParam("refresh_token", "");
        builder.Query = query.ToString();
        var url = builder.Uri;

        var ws = new ClientWebSocket();
        // Also set the headers — harmless if forwarded, and lets this same client
        // work if pointed straight at the origin/ALB (no CloudFront) in future.
        try
        {
            ws.Options.SetRequestHeader("authorization", $"Bearer {_config.AccessToken}");
            if (!string.IsNullOrEmpty(_config.IdToken)) ws.Options.SetRequestHeader("id_token", _config.IdToken);
            ws.Options.SetRequestHeader("refresh_token", "");
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
            if (_intentionalClose) return;
            HandleTaskError(err);
            return;
        }

        // didOpen: healthy connection resets backoff, sends a fresh START, flushes buffer.
        _isOpen = true;
        _everOpened = true;
        _reconnectAttempt = 0;
        _handshakeFailures = 0;
        Console.WriteLine($"✓ WebSocket open → {_config.Endpoint}");
        OnStateChange?.Invoke(true);
        SendStart();       // fresh START every (re)connect; server has no resume
        FlushPending();    // replay audio buffered during the outage
        _ = ReceiveLoop(ws, _cts.Token);
    }

    /// <summary>START handshake — must be the first frame; audio before this is dropped server-side.</summary>
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
        };
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
    /// guarded so the receive-failure and close paths don't double up.
    /// </summary>
    private void ScheduleReconnect(string why)
    {
        lock (_reconnectLock)
        {
            if (_intentionalClose || _reconnectScheduled) return;
            _reconnectScheduled = true;
            _isOpen = false;
            _reconnectAttempt++;
            double delay = Math.Min(MaxBackoff, Math.Pow(2.0, _reconnectAttempt - 1) * 0.5);
            Console.WriteLine($"⟳ WS reconnect #{_reconnectAttempt} in {delay:F1}s ({why})");
            _ = Task.Delay(TimeSpan.FromSeconds(delay)).ContinueWith(_ =>
            {
                if (_intentionalClose) return;
                lock (_reconnectLock) { _reconnectScheduled = false; }
                Connect(); // new socket; open path sends fresh START
            });
        }
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
            Console.Error.WriteLine($"WS receive/closed: {err.Message}");
            _isOpen = false;
            OnStateChange?.Invoke(false);
            ScheduleReconnect("receive error");
        }
    }

    // Fires when the underlying connect fails — including a rejected WS upgrade
    // (bad/expired token → 401). If we NEVER opened and keep failing the
    // handshake, it's almost certainly auth: warn clearly and stop rather than
    // hammering the server with a dead token.
    private void HandleTaskError(Exception error)
    {
        _isOpen = false;
        OnStateChange?.Invoke(false);
        if (_intentionalClose) return;
        if (!_everOpened)
        {
            _handshakeFailures++;
            if (_handshakeFailures >= MaxHandshakeFailures)
            {
                var msg =
                    "✗ WebSocket handshake keeps failing and the connection never opened.\n" +
                    "  This is almost always an EXPIRED or INVALID access token (the server\n" +
                    "  returns 401). Cognito access tokens last ~1 hour. Fetch a fresh token\n" +
                    $"  and re-run.\n  Underlying error: {error.Message}\n";
                Console.Error.WriteLine(msg);
                _intentionalClose = true;
                OnFatalAuth?.Invoke(msg);
                return;
            }
        }
        // Transient (or early) failure: let ScheduleReconnect handle backoff.
        ScheduleReconnect("connect error");
    }
}
