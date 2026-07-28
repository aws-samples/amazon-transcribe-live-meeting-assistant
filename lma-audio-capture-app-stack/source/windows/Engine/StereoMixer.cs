using System.Diagnostics;

namespace LMA;

/// <summary>
/// Combines two independent mono float sources (meeting audio + mic) that arrive
/// on separate threads at their own cadence, and emits interleaved 16-bit LE PCM
/// chunks matching the LMA transcriber wire format:
///
///   [ch0_s0][ch1_s0][ch0_s1][ch1_s1]...   (ch0 = meeting, ch1 = mic)
///
/// Both inputs MUST already be resampled to the same target sample rate (done by
/// the capture layer). This class just buffers each channel and, on a fixed
/// cadence, drains the number of frames both channels can supply.
///
/// NOTE: unlike the browser worklet (recording-processor.js) we start the
/// interleave at index 0 — the browser version starts at index 1 and drops the
/// first mic sample. Do not replicate that bug.
///
/// Ported verbatim from macOS StereoMixer.swift (pure, platform-agnostic).
/// </summary>
public sealed class StereoMixer
{
    private readonly object _lock = new();
    private readonly List<float> _meeting = new();   // ch0
    private readonly List<float> _mic = new();        // ch1
    private readonly Action<byte[]> _onChunk;
    private readonly int _sampleRate;
    private System.Threading.Timer? _timer;
    private int _ticks;

    /// <summary>
    /// Optional debug tee: the exact interleaved PCM we stream is also written
    /// here so we can verify offline that ch0/Left = system audio and ch1/Right
    /// = mic (not swapped/skewed/garbled) by measuring per-channel RMS.
    /// </summary>
    public WavTee? Tee;

    private bool _micMuted;      // when true, ch1 (mic) is zeroed before interleaving
    private bool _meetingMuted;  // when true, ch0 (system/meeting) is zeroed
    private bool _paused;        // when true, drain() consumes+discards and sends nothing
    private bool _connected;     // live WS connection state (for the meter line)

    /// <summary>
    /// Optional per-second level callback for a UI (RMS+peak in [0,1] per channel,
    /// plus link/pause state). Fired on the timer thread; hop to UI thread in the UI.
    /// </summary>
    public Action<float, float, bool, bool>? OnLevels; // (meetingRMS, micRMS, connected, paused)

    // --- Level metering (verifies both channels are actually live) ----------
    private float _meterMeetingPeak;
    private float _meterMicPeak;
    private double _meterMeetingSumSq;
    private double _meterMicSumSq;
    private int _meterFrames;
    private long _bytesSent;
    private int _meterTick;
    /// <summary>Total interleaved frames emitted (each = one ch0+ch1 sample pair).</summary>
    public long FramesEmitted { get; private set; }

    public StereoMixer(int sampleRate, Action<byte[]> onChunk)
    {
        _sampleRate = sampleRate;
        _onChunk = onChunk;
    }

    public void AppendMeeting(float[] samples)
    {
        lock (_lock) { _meeting.AddRange(samples); }
    }

    public void AppendMic(float[] samples)
    {
        lock (_lock) { _mic.AddRange(samples); }
    }

    /// <summary>
    /// Mute/unmute the mic channel (ch1). When muted we still consume mic frames
    /// (so the channels stay time-aligned) but zero them before interleaving.
    /// </summary>
    public void SetMicMuted(bool muted)
    {
        lock (_lock) { _micMuted = muted; }
        Console.WriteLine(muted ? "🔇 mic muted (ch1 will stream silence)" : "🎙️  mic unmuted");
    }

    /// <summary>
    /// Mute/unmute the meeting/system channel (ch0). Same approach as mic mute:
    /// frames are still consumed to keep alignment, but zeroed before interleave.
    /// </summary>
    public void SetMeetingMuted(bool muted)
    {
        lock (_lock) { _meetingMuted = muted; }
        Console.WriteLine(muted ? "🔇 system audio muted (ch0 will stream silence)" : "🔊 system audio unmuted");
    }

    /// <summary>
    /// Pause/resume streaming. While paused we keep the WS open but drop buffered
    /// audio instead of sending it, so the server sees a gap rather than a stop.
    /// </summary>
    public void SetPaused(bool p)
    {
        lock (_lock) { _paused = p; }
        Console.WriteLine(p ? "⏸  paused (no audio sent)" : "▶️  resumed");
    }

    public bool IsMicMuted { get { lock (_lock) { return _micMuted; } } }
    public bool IsMeetingMuted { get { lock (_lock) { return _meetingMuted; } } }
    public bool IsPaused { get { lock (_lock) { return _paused; } } }

    /// <summary>Update live WS connection state shown in the meter line.</summary>
    public void SetConnected(bool c)
    {
        lock (_lock) { _connected = c; }
    }

    /// <summary>Flush interleaved PCM every ~100 ms (matches the server's BlockStream size).</summary>
    public void Start()
    {
        _ticks = 0;
        _timer = new System.Threading.Timer(_ =>
        {
            Drain();
            _ticks++;
            if (_ticks % 10 == 0) LogMeter(); // ~1×/sec
        }, null, TimeSpan.FromMilliseconds(100), TimeSpan.FromMilliseconds(100));
    }

    public void Stop()
    {
        _timer?.Dispose();
        _timer = null;
        Drain(); // flush remainder
    }

    private void Drain()
    {
        float[] m, k;
        bool micIsMuted, meetingIsMuted, isPaused;
        lock (_lock)
        {
            // Drain the number of frames BOTH channels can supply so they stay
            // aligned. If one source stalls, we hold back rather than skewing.
            int n = Math.Min(_meeting.Count, _mic.Count);
            if (n <= 0) return;
            m = new float[n];
            k = new float[n];
            _meeting.CopyTo(0, m, 0, n);
            _mic.CopyTo(0, k, 0, n);
            _meeting.RemoveRange(0, n);
            _mic.RemoveRange(0, n);
            micIsMuted = _micMuted;
            meetingIsMuted = _meetingMuted;
            isPaused = _paused;
        }

        int count = m.Length;

        // Paused: consume the buffered audio (already removed above) but send
        // nothing, so buffers don't grow unbounded and the server sees silence.
        if (isPaused)
        {
            OnLevels?.Invoke(0, 0, _connected, true);
            return;
        }

        var outBytes = new byte[count * 4]; // 2 channels * 2 bytes
        float mPeak = 0, kPeak = 0;
        double mSum = 0, kSum = 0;
        for (int i = 0; i < count; i++)
        {
            float ms = meetingIsMuted ? 0 : m[i];  // system-mute: zero ch0, keep alignment
            float ks = micIsMuted ? 0 : k[i];       // mic-mute: zero ch1, keep alignment
            short s0 = FloatToInt16(ms); // ch0 meeting
            short s1 = FloatToInt16(ks); // ch1 mic
            int off = i * 4;
            outBytes[off] = (byte)(s0 & 0xff);
            outBytes[off + 1] = (byte)((s0 >> 8) & 0xff);
            outBytes[off + 2] = (byte)(s1 & 0xff);
            outBytes[off + 3] = (byte)((s1 >> 8) & 0xff);
            float ma = Math.Abs(ms), ka = Math.Abs(ks);
            if (ma > mPeak) mPeak = ma;
            if (ka > kPeak) kPeak = ka;
            mSum += ms * ms; kSum += ks * ks;
        }

        Tee?.Append(outBytes);   // debug: mirror exact streamed PCM to local WAV

        // Fold this chunk into the metering accumulators (protected by lock so
        // the summary read is consistent).
        lock (_lock)
        {
            if (mPeak > _meterMeetingPeak) _meterMeetingPeak = mPeak;
            if (kPeak > _meterMicPeak) _meterMicPeak = kPeak;
            _meterMeetingSumSq += mSum;
            _meterMicSumSq += kSum;
            _meterFrames += count;
            _bytesSent += outBytes.Length;
            FramesEmitted += count;
        }

        _onChunk(outBytes);
    }

    /// <summary>
    /// Print a one-line VU meter for both channels ~1×/sec. Lets you confirm at
    /// a glance that ch0 (meeting/system) and ch1 (mic) are BOTH live and not
    /// swapped — silence on a channel here means that capture source is dead.
    /// </summary>
    private void LogMeter()
    {
        int frames;
        float mPeak, kPeak;
        double mRms, kRms;
        long sent;
        bool isConnected, muted;
        int tick;
        lock (_lock)
        {
            frames = _meterFrames;
            mPeak = _meterMeetingPeak; kPeak = _meterMicPeak;
            mRms = frames > 0 ? Math.Sqrt(_meterMeetingSumSq / frames) : 0;
            kRms = frames > 0 ? Math.Sqrt(_meterMicSumSq / frames) : 0;
            sent = _bytesSent;
            isConnected = _connected;
            muted = _micMuted;
            _meterMeetingPeak = 0; _meterMicPeak = 0;
            _meterMeetingSumSq = 0; _meterMicSumSq = 0; _meterFrames = 0;
            _meterTick++;
            tick = _meterTick;
        }

        // Seconds of audio drained this interval (should be ~1.0 when healthy).
        double secs = (double)frames / _sampleRate;
        double kb = sent / 1024.0;
        string link = isConnected ? "● live" : "○ buffering(WS down)";
        string micTag = muted ? " [MUTED]" : "";
        // Feed a UI (tray meters) if attached. Values already reflect mute
        // (muted channels contribute zero energy).
        OnLevels?.Invoke((float)mRms, (float)kRms, isConnected, false);
        Console.WriteLine(
            $"[meter {tick,3}s] {link} | ch0 meeting {Bar(mRms)} rms {mRms:F3} peak {mPeak:F3} | " +
            $"ch1 mic{micTag} {Bar(kRms)} rms {kRms:F3} peak {kPeak:F3} | {secs:F2}s, {kb:F0} KB");
        if (frames == 0)
        {
            Console.WriteLine("           ⚠️  no audio drained this second — both channels must have samples to flush " +
                              "(check mic + system-audio permissions / that audio is playing)");
        }
    }

    /// <summary>Tiny 10-cell ASCII VU bar from an RMS level in [0,1].</summary>
    private static string Bar(double rms)
    {
        const int cells = 10;
        int lit = Math.Max(0, Math.Min(cells, (int)(rms * 3.0 * cells))); // ×3 gain for visibility
        return "[" + new string('#', lit) + new string('-', cells - lit) + "]";
    }

    /// <summary>Match the browser's clamping (floatTo16BitPCM): [-1,1] → Int16.</summary>
    public static short FloatToInt16(float s)
    {
        float clamped = Math.Max(-1.0f, Math.Min(1.0f, s));
        return (short)(clamped < 0 ? clamped * 32768.0f : clamped * 32767.0f);
    }
}
