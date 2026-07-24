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
    private readonly object _lock = new();
    private State _state = State.Idle;

    // UI callbacks (invoked directly; UI layers marshal to their own thread).
    public Action<State>? OnStateChange;
    public Action<float, float, bool, bool>? OnLevels; // meetingRMS, micRMS, connected, paused
    public Action<string>? OnLog;

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
    public void Start(string? callId = null)
    {
        if (string.IsNullOrEmpty(Config.AccessToken)) { SetState(State.Err("Not signed in")); return; }
        SetState(State.Starting);

        if (!string.IsNullOrEmpty(callId)) Config.CallId = callId!;
        ActiveCallId = Config.CallId;

        var sock = new TranscriberSocket(Config);
        var mix = new StereoMixer(Config.SampleRate, chunk => sock.SendPcm(chunk));
        var cap = new AudioCapture(mix, Config.SampleRate);

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
        try
        {
            cap.Start();
            Log($"Streaming {ActiveCallId}");
            SetState(State.Streaming);
        }
        catch (Exception e)
        {
            Log($"Capture failed: {e.Message}");
            SetState(State.Err(e.Message));
        }
    }

    /// <summary>Stop: send END, tear down capture + socket, return to authenticated/idle.</summary>
    public void Stop()
    {
        SetState(State.Stopping);
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

    public void SetPaused(bool p) => _mixer?.SetPaused(p);
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
