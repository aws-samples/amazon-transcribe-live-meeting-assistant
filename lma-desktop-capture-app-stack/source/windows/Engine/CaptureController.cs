namespace LMA;

/// <summary>
/// Shared engine controller used by BOTH the headless CLI (Program.cs) and the
/// system-tray GUI (TrayApp.cs). Owns the socket + mixer + capture lifecycle and
/// exposes simple, thread-safe operations (login, start, stop, pause, mute) plus
/// callbacks the UI can bind to. Keeping this UI-agnostic means the CLI path
/// (which the downloadable package ships) is unaffected by the GUI.
///
/// State transitions:
///   idle → (login) → authenticated → (start) → streaming ⇄ paused → (stop) → idle
///
/// Ported verbatim from macOS CaptureController.swift.
/// </summary>
public sealed class CaptureController
{
    public enum StateKind { Idle, SigningIn, Authenticated, Starting, Streaming, Stopping, Error }

    public readonly record struct State(StateKind Kind, string Message = "")
    {
        public static readonly State Idle = new(StateKind.Idle);
        public static readonly State SigningIn = new(StateKind.SigningIn);
        public static readonly State Authenticated = new(StateKind.Authenticated);
        public static readonly State Starting = new(StateKind.Starting);
        public static readonly State Streaming = new(StateKind.Streaming);
        public static readonly State Stopping = new(StateKind.Stopping);
        public static State Err(string m) => new(StateKind.Error, m);
    }

    public Config Config { get; }
    private TranscriberSocket? _socket;
    private StereoMixer? _mixer;
    private AudioCapture? _capture;
    private WavTee? _tee;
    private VideoSocket? _videoSocket;
    private VideoCapture? _videoCapture;
    private DateTime _audioStartUtc;
    private readonly object _lock = new();
    private State _state = State.Idle;

    // UI callbacks (invoked directly; UI layers marshal to their own thread).
    public Action<State>? OnStateChange;
    public Action<float, float, bool, bool>? OnLevels; // meetingRMS, micRMS, connected, paused
    public Action<string>? OnLog;
    /// <summary>
    /// Fired when the chosen video source became unavailable and capture fell
    /// back to something else (e.g. the selected window closed, so the whole
    /// display is now being recorded). Privacy-relevant, so it is surfaced
    /// rather than logged quietly.
    /// </summary>
    public Action<string>? OnVideoFallback;

    /// <summary>The callId of the active/most-recent stream (for "Open in LMA" deep link).</summary>
    public string ActiveCallId { get; private set; } = "";

    public CaptureController(Config config) { Config = config; }

    public State CurrentState { get { lock (_lock) { return _state; } } }

    private void SetState(State s)
    {
        lock (_lock) { _state = s; }
        OnStateChange?.Invoke(s);
    }

    private void Log(string msg) => OnLog?.Invoke(msg);

    // MARK: - Auth

    /// <summary>Sign in via SRP and store tokens on the config. `password` is not retained.</summary>
    public async Task<bool> LoginAsync(string username, string password)
    {
        SetState(State.SigningIn);
        try
        {
            var tokens = await Srp.LoginAsync(
                username, password,
                Config.UserPoolId, Config.ClientId, Config.EffectiveRegion);
            Config.Username = username;
            Config.AccessToken = tokens.AccessToken;
            Config.IdToken = tokens.IdToken;
            Log($"Signed in as {username}");
            SetState(State.Authenticated);
            return true;
        }
        catch (Exception e)
        {
            Log($"Login failed: {e.Message}");
            SetState(State.Err(e.Message));
            return false;
        }
    }

    /// <summary>True when we already have an access token (pasted or from a prior login).</summary>
    public bool IsAuthenticated
    {
        get
        {
            var s = CurrentState;
            if (s.Kind == StateKind.Streaming) return true;
            return !string.IsNullOrEmpty(Config.AccessToken) || s.Kind == StateKind.Authenticated;
        }
    }

    /// <summary>
    /// Sign out: stop any active stream, clear tokens, return to idle. The
    /// remembered login id (if any) is left intact so the field can prefill.
    /// </summary>
    public void Logout()
    {
        if (_socket != null) Stop();
        Config.AccessToken = "";
        Config.IdToken = "";
        Log("Signed out");
        SetState(State.Idle);
    }

    // MARK: - Streaming lifecycle

    /// <summary>
    /// Begin capture + streaming. Uses a fresh callId per session unless the
    /// caller set one on the config. Safe to call only when authenticated.
    /// </summary>
    // Settings (persisted by the GUI, applied at start). Empty = use the
    // config/CLI value, so the headless CLI path is completely unaffected.
    /// <summary>Speaker label for the mic channel (ch_1 → AgentId). GUI default: the signed-in email.</summary>
    public string MicLabel = "";
    /// <summary>Speaker label for the system-audio channel (ch_0 → FromNumber).</summary>
    public string SystemLabel = "";
    /// <summary>
    /// Ask Transcribe to tell apart individual voices on the system/meeting
    /// channel (ch_0), appending (spk_0), (spk_1), … to SystemLabel.
    ///
    /// Nullable because null means "not set by the GUI", so the config/CLI value
    /// (--diarize-system / LMA_DIARIZE_SYSTEM) stands — the same convention the
    /// empty-string labels above use. A plain bool would default to false and
    /// silently override the CLI flag, since the headless path never pushes
    /// settings to the controller.
    /// </summary>
    public bool? DiarizeSystemChannel;
    /// <summary>Same for the mic channel (ch_1) — a shared conference-room microphone.</summary>
    public bool? DiarizeMicChannel;
    /// <summary>MMDevice ID of the mic to capture from. Empty = system default.</summary>
    public string MicDeviceId = "";
    /// <summary>Also capture and stream desktop video (screen or window). Default off.</summary>
    public bool VideoEnabled = false;
    /// <summary>Persisted video source id ("display:&lt;name&gt;" / "window:&lt;handle&gt;"; "" = primary display).</summary>
    public string VideoSourceId = "";
    /// <summary>True while a video stream is running for the active call (UI badge).</summary>
    public bool IsVideoActive { get; private set; }

    public void Start(string? callId = null)
    {
        if (string.IsNullOrEmpty(Config.AccessToken)) { SetState(State.Err("Not signed in")); return; }
        SetState(State.Starting);

        if (!string.IsNullOrEmpty(callId)) Config.CallId = callId!;
        ActiveCallId = Config.CallId;

        // Apply Settings overrides: speaker labels ride the START frame as
        // AgentId (mic/ch_1) and FromNumber (system/ch_0).
        if (!string.IsNullOrEmpty(MicLabel)) Config.AgentId = MicLabel;
        if (!string.IsNullOrEmpty(SystemLabel)) Config.FromNumber = SystemLabel;
        // Per-channel speaker identification: only the GUI sets these, so leave
        // the CLI/env value alone when null.
        if (DiarizeSystemChannel.HasValue) Config.DiarizeSystemChannel = DiarizeSystemChannel.Value;
        if (DiarizeMicChannel.HasValue) Config.DiarizeMicChannel = DiarizeMicChannel.Value;

        var sock = new TranscriberSocket(Config);
        var mix = new StereoMixer(Config.SampleRate, chunk => sock.SendPcm(chunk));
        var cap = new AudioCapture(mix, Config.SampleRate, MicDeviceId);

        // Optional: tee the exact streamed PCM to a local stereo WAV for offline
        // verification (per-channel RMS proves ch0=system / ch1=mic, not swapped).
        if (!string.IsNullOrEmpty(Config.DebugWavPath))
        {
            _tee = WavTee.Create(Config.DebugWavPath, Config.SampleRate, 2);
            mix.Tee = _tee;
            if (_tee != null) Console.WriteLine($"  debug-wav: {Config.DebugWavPath} (ch0=Left=system, ch1=Right=mic)");
        }

        sock.OnStateChange = connected => mix.SetConnected(connected);
        sock.OnFatalAuth = msg => { Log("Token rejected — sign in again."); SetState(State.Err(msg)); Stop(); };
        mix.OnLevels = (m, k, connected, paused) => OnLevels?.Invoke(m, k, connected, paused);

        _socket = sock; _mixer = mix; _capture = cap;

        sock.Connect();
        mix.Start();
        _audioStartUtc = DateTime.UtcNow;
        try
        {
            cap.Start();
            Log($"Streaming {ActiveCallId}");
            SetState(State.Streaming);
            StartVideoIfEnabled();
        }
        catch (Exception e)
        {
            Log($"Capture failed: {e.Message}");
            SetState(State.Err(e.Message));
        }
    }

    /// <summary>
    /// Start the optional desktop-video lane: a second capture (screen/window)
    /// feeding fMP4 fragments over a second websocket. Best-effort — failures
    /// here never touch the audio path.
    /// </summary>
    private void StartVideoIfEnabled()
    {
        if (!VideoEnabled) return;
        var vSock = new VideoSocket(Config);
        var vCap = new VideoCapture(VideoSourceId, seg => vSock.SendSegment(seg));
        vCap.OnFirstFrame = () =>
        {
            if (vCap.FirstFrameDate is { } first)
                vSock.VideoTimeOffsetMs = Math.Max(0, (int)(first - _audioStartUtc).TotalMilliseconds);
        };
        vSock.OnOverflow = () =>
        {
            StopVideo(sendEnd: false);
            Log("Screen video stopped (connection lost); audio unaffected");
        };
        vCap.OnFallback = msg => OnVideoFallback?.Invoke(msg);
        vCap.OnFailed = msg =>
        {
            Log($"Screen video failed (audio unaffected): {msg}");
            StopVideo(sendEnd: false);
        };
        _videoSocket = vSock;
        _videoCapture = vCap;
        vSock.Connect();
        try
        {
            vCap.Start();
            IsVideoActive = true;
            Log("Streaming with screen video");
        }
        catch (Exception e)
        {
            Log($"Screen video failed to start (audio unaffected): {e.Message}");
            StopVideo(sendEnd: false);
        }
    }

    /// <summary>
    /// Tear down the video lane. When sendEnd, flush the encoder's final
    /// fragments and send END_VIDEO so the server finalizes the recording.
    /// </summary>
    private void StopVideo(bool sendEnd)
    {
        var vCap = _videoCapture;
        var vSock = _videoSocket;
        if (vCap == null && vSock == null) return;
        IsVideoActive = false;
        _videoCapture = null;
        _videoSocket = null;
        _ = Task.Run(async () =>
        {
            // Stopping the recorder flushes remaining fMP4 fragments through the
            // callback stream BEFORE we send END_VIDEO/close.
            if (vCap != null) await vCap.StopAsync();
            if (sendEnd)
            {
                vSock?.BeginClose();
                vSock?.SendEnd();
                await Task.Delay(TimeSpan.FromMilliseconds(400));
                vSock?.Close();
            }
            else
            {
                vSock?.Close();
            }
        });
    }

    /// <summary>Stop: send END, tear down capture + socket, return to authenticated/idle.</summary>
    public void Stop()
    {
        SetState(State.Stopping);
        StopVideo(sendEnd: true);
        _capture?.Stop();
        _mixer?.Stop();
        _tee?.Finish();
        _socket?.BeginClose();   // mark intentional first so teardown stays quiet
        _socket?.SendEnd();
        var sock = _socket;
        Task.Delay(TimeSpan.FromMilliseconds(400)).ContinueWith(_ =>
        {
            sock?.Close();
            _socket = null; _mixer = null; _capture = null; _tee = null;
            SetState(string.IsNullOrEmpty(Config.AccessToken) ? State.Idle : State.Authenticated);
        });
    }

    // MARK: - Controls (no-ops when not streaming)

    public void SetPaused(bool p) { _mixer?.SetPaused(p); _videoCapture?.SetPaused(p); }
    public void SetMicMuted(bool m) => _mixer?.SetMicMuted(m);
    public void SetMeetingMuted(bool m) => _mixer?.SetMeetingMuted(m);

    public bool IsPaused => _mixer?.IsPaused ?? false;
    public bool IsMicMuted => _mixer?.IsMicMuted ?? false;
    public bool IsMeetingMuted => _mixer?.IsMeetingMuted ?? false;

    /// <summary>
    /// URL to open the active meeting in the LMA web UI, or the meetings list if
    /// nothing is streaming.
    ///
    /// Uses the configured `webEndpoint` (the web-app CloudFront distribution),
    /// which is DIFFERENT from the WebSocket endpoint host — deriving it from the
    /// WS host yields a 404. Only if webEndpoint is unset do we fall back to the
    /// WS host (best effort).
    /// </summary>
    public string? LmaUrl()
    {
        var baseUrl = Config.WebEndpoint;
        if (string.IsNullOrEmpty(baseUrl))
        {
            try { baseUrl = $"https://{new Uri(Config.Endpoint).Host}/"; }
            catch { return null; }
        }
        // Normalize to "https://host/" (strip any trailing slash, then add one).
        if (baseUrl.EndsWith("/")) baseUrl = baseUrl.Substring(0, baseUrl.Length - 1);
        var s = $"{baseUrl}/#/calls";
        if (!string.IsNullOrEmpty(ActiveCallId))
        {
            // encodeURIComponent-equivalent (Uri.EscapeDataString matches JS
            // encodeURIComponent's unreserved set: A-Za-z0-9-_.!~*'() ).
            s += "/" + Uri.EscapeDataString(ActiveCallId);
        }
        return s;
    }
}
