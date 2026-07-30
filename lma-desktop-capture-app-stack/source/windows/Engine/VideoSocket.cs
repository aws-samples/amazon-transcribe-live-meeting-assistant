using System.Net.WebSockets;
using System.Text;
using System.Text.Json;

namespace LMA;

/// <summary>
/// Second WebSocket, dedicated to the optional desktop-video stream:
///   1. connect wss://.../api/v1/ws  (same endpoint + query-param auth as the
///      audio socket — see the AUTH NOTE in TranscriberSocket.cs)
///   2. send one JSON text frame { callEvent: "START_VIDEO", callId, ... }
///   3. stream fragmented-MP4 segments as binary frames
///   4. send { callEvent: "END_VIDEO" } and close
///
/// Keeping video on its own socket means video bytes can never delay the
/// real-time audio PCM (no head-of-line blocking), and old servers — which
/// ignore the unknown START_VIDEO event and drop binary frames on a
/// session-less socket — degrade to exactly the pre-video behavior.
///
/// Reconnect semantics differ from audio: the fMP4 encoder session on our side
/// keeps running across a socket drop, so the bytes CONTINUE one stream. On
/// reconnect we re-send START_VIDEO with videoResume=true and flush everything
/// buffered — segments must never be dropped mid-stream (unlike audio, a gap
/// makes the remainder undecodable). If the outage outlasts the buffer cap we
/// give up on video for this call (audio is the priority) via OnOverflow.
///
/// Ported from macOS VideoSocket.swift.
/// </summary>
public sealed class VideoSocket
{
    private readonly Config _config;
    private ClientWebSocket? _ws;
    private CancellationTokenSource? _cts;
    private volatile bool _isOpen;
    private readonly object _sendLock = new();

    private volatile bool _intentionalClose;
    private int _reconnectAttempt;
    private const double MaxBackoff = 10;
    private readonly object _reconnectLock = new();
    private bool _reconnectScheduled;
    private bool _everOpened;
    private int _handshakeFailures;
    private const int MaxHandshakeFailures = 4;

    /// <summary>True after the first successful START_VIDEO: reconnects resume.</summary>
    private bool _startedOnce;

    /// <summary>
    /// ms between the audio stream's start and the first video frame; sent on
    /// START_VIDEO so the server can align video with the transcript timeline.
    /// </summary>
    public int VideoTimeOffsetMs;

    // Segments buffered while the socket is down. Generous cap (~32MB) because
    // dropping mid-stream corrupts the video.
    private readonly List<byte[]> _pending = new();
    private int _pendingBytes;
    private const int MaxPendingBytes = 32 * 1024 * 1024;
    private volatile bool _overflowed;

    /// <summary>
    /// Fired once if the buffer cap is exceeded during an outage (or the video
    /// handshake keeps failing) — the caller should stop video capture for this
    /// call. Audio continues unaffected.
    /// </summary>
    public Action? OnOverflow;

    public VideoSocket(Config config)
    {
        _config = config;
    }

    public void Connect()
    {
        _ = ConnectAsync();
    }

    private async Task ConnectAsync()
    {
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
            if (_intentionalClose) return;
            HandleTaskError(err);
            return;
        }

        lock (_sendLock) { _isOpen = true; }
        _everOpened = true;
        _reconnectAttempt = 0;
        _handshakeFailures = 0;
        Console.WriteLine("✓ video WebSocket open");
        SendStartVideo();
        FlushPending();
        _ = ReceiveLoop(ws, _cts.Token);
    }

    /// <summary>fMP4 segment → binary WS frame (buffered while the socket is down).</summary>
    public void SendSegment(byte[] data)
    {
        lock (_sendLock)
        {
            if (_overflowed) return;
            if (!_isOpen || _ws is not { State: WebSocketState.Open })
            {
                _pending.Add(data);
                _pendingBytes += data.Length;
                if (_pendingBytes > MaxPendingBytes)
                {
                    _overflowed = true;
                    _pending.Clear();
                    _pendingBytes = 0;
                    Console.Error.WriteLine(
                        "✗ video buffer overflow during outage — stopping video (audio unaffected)");
                    OnOverflow?.Invoke();
                }
                return;
            }
            var ws = _ws;
            var ct = _cts?.Token ?? CancellationToken.None;
            _ = ws.SendAsync(new ArraySegment<byte>(data), WebSocketMessageType.Binary, true, ct)
                .ContinueWith(t =>
                {
                    if (t.Exception != null && !_intentionalClose)
                        Console.Error.WriteLine($"video send error: {t.Exception.GetBaseException().Message}");
                }, TaskContinuationOptions.OnlyOnFaulted);
        }
    }

    public void SendEnd()
    {
        var meta = new Dictionary<string, object>
        {
            ["callId"] = _config.CallId,
            ["callEvent"] = "END_VIDEO",
        };
        SendJson(meta, "END_VIDEO");
    }

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
        lock (_sendLock) { _isOpen = false; }
    }

    private void SendStartVideo()
    {
        var meta = new Dictionary<string, object>
        {
            ["callId"] = _config.CallId,
            ["callEvent"] = "START_VIDEO",
            ["videoTimeOffsetMs"] = VideoTimeOffsetMs,
        };
        // Same encoder session continuing over a new socket: the server appends
        // to the current segment instead of rotating files.
        if (_startedOnce) meta["videoResume"] = true;
        SendJson(meta, _startedOnce ? "START_VIDEO (resume)" : "START_VIDEO");
        _startedOnce = true;
    }

    private void FlushPending()
    {
        lock (_sendLock)
        {
            if (!_isOpen || _pending.Count == 0 || _ws is not { State: WebSocketState.Open }) return;
            int count = _pending.Count;
            var ws = _ws;
            var ct = _cts?.Token ?? CancellationToken.None;
            foreach (var d in _pending)
                _ = ws.SendAsync(new ArraySegment<byte>(d), WebSocketMessageType.Binary, true, ct);
            _pending.Clear();
            _pendingBytes = 0;
            Console.WriteLine($"↺ flushed {count} buffered video segments after reconnect");
        }
    }

    private void ScheduleReconnect(string why)
    {
        lock (_reconnectLock)
        {
            if (_intentionalClose || _reconnectScheduled || _overflowed) return;
            _reconnectScheduled = true;
            lock (_sendLock) { _isOpen = false; }
            _reconnectAttempt++;
            double delay = Math.Min(MaxBackoff, Math.Pow(2.0, _reconnectAttempt - 1) * 0.5);
            Console.WriteLine($"⟳ video WS reconnect #{_reconnectAttempt} in {delay:F1}s ({why})");
            _ = Task.Delay(TimeSpan.FromSeconds(delay)).ContinueWith(_ =>
            {
                if (_intentionalClose) return;
                lock (_reconnectLock) { _reconnectScheduled = false; }
                Connect();
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
                    lock (_sendLock) { _isOpen = false; }
                    if (_intentionalClose) return;
                    ScheduleReconnect($"server closed {(int?)ws.CloseStatus}");
                    return;
                }
                // The transcriber doesn't send app-level frames back on the
                // video socket; ignore anything received.
            }
            if (!_intentionalClose)
            {
                lock (_sendLock) { _isOpen = false; }
                ScheduleReconnect("receive ended");
            }
        }
        catch (Exception err)
        {
            if (_intentionalClose) return;
            Console.Error.WriteLine($"video WS receive/closed: {err.Message}");
            lock (_sendLock) { _isOpen = false; }
            ScheduleReconnect("receive error");
        }
    }

    private void HandleTaskError(Exception error)
    {
        lock (_sendLock) { _isOpen = false; }
        if (_intentionalClose) return;
        if (!_everOpened)
        {
            _handshakeFailures++;
            if (_handshakeFailures >= MaxHandshakeFailures)
            {
                // Unlike the audio socket, a dead video connection is not fatal
                // to the app: give up on video only (auth failures also hit the
                // audio socket, which owns the loud failure path).
                Console.Error.WriteLine(
                    $"✗ video WebSocket handshake keeps failing — video disabled for this call: {error.Message}");
                _intentionalClose = true;
                OnOverflow?.Invoke();
                return;
            }
        }
        ScheduleReconnect("connect error");
    }
}
