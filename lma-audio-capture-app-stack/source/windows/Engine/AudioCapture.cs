using NAudio.CoreAudioApi;
using NAudio.CoreAudioApi.Interfaces;
using NAudio.Wave;

namespace LMA;

/// <summary>
/// Captures the two audio sources and feeds mono Float samples (resampled to the
/// target rate) into a StereoMixer.
///
///   • Meeting/system audio  → WASAPI loopback on the default RENDER endpoint.
///     Captures ALL system output regardless of which app plays it — this is what
///     lets us transcribe a NATIVE Zoom/Teams client, which the browser
///     getDisplayMedia path cannot. Needs NO special permission on Windows.
///   • Microphone            → WASAPI capture on the default CAPTURE endpoint.
///     Subject to the Windows Settings ▸ Privacy ▸ Microphone gate.
///
/// Device changes mid-meeting (user switches default render/capture device) are
/// handled via an IMMNotificationClient that rebuilds the affected capture —
/// mirrors AudioCapture.observeDeviceChanges() on macOS. Channel alignment holds
/// because the mixer only drains frames both channels can supply.
///
/// Ported from macOS AudioCapture.swift (ScreenCaptureKit + AVAudioEngine → WASAPI).
/// </summary>
public sealed class AudioCapture : IMMNotificationClient
{
    private readonly StereoMixer _mixer;
    private readonly int _targetRate;

    private readonly MMDeviceEnumerator _enumerator = new();

    // System audio (loopback on the render endpoint).
    private WasapiLoopbackCapture? _loopback;
    private LinearResampler? _sysResampler;
    private int _sysChannels;
    private readonly object _sysLock = new();

    // Mic (capture on the capture endpoint).
    private WasapiCapture? _micCapture;
    private LinearResampler? _micResampler;
    private int _micChannels;
    private readonly object _micLock = new();

    private volatile bool _running;
    private volatile bool _rebuildScheduled;

    public AudioCapture(StereoMixer mixer, int targetRate)
    {
        _mixer = mixer;
        _targetRate = targetRate;
    }

    public void Start()
    {
        _running = true;
        StartMic();
        StartSystemAudio();
        // Register for default-device change notifications (input/output switched
        // mid-meeting). Rebuilds the affected capture against the new device.
        _enumerator.RegisterEndpointNotificationCallback(this);
    }

    public void Stop()
    {
        _running = false;
        try { _enumerator.UnregisterEndpointNotificationCallback(this); } catch { }
        lock (_sysLock)
        {
            try { _loopback?.StopRecording(); } catch { }
            try { _loopback?.Dispose(); } catch { }
            _loopback = null;
        }
        lock (_micLock)
        {
            try { _micCapture?.StopRecording(); } catch { }
            try { _micCapture?.Dispose(); } catch { }
            _micCapture = null;
        }
    }

    // MARK: - System audio via WASAPI loopback

    private void StartSystemAudio()
    {
        lock (_sysLock)
        {
            try
            {
                var device = _enumerator.GetDefaultAudioEndpoint(DataFlow.Render, Role.Multimedia);
                var cap = new WasapiLoopbackCapture(device);
                var fmt = cap.WaveFormat;
                _sysChannels = fmt.Channels;
                _sysResampler = new LinearResampler(fmt.SampleRate, _targetRate);
                cap.DataAvailable += (_, e) =>
                {
                    var mono = ToMono(e.Buffer, e.BytesRecorded, fmt);
                    var res = _sysResampler!.Process(mono);
                    if (res.Length > 0) _mixer.AppendMeeting(res);
                };
                cap.RecordingStopped += (_, _) => { };
                cap.StartRecording();
                _loopback = cap;
                Console.WriteLine($"✓ System audio capture started (WASAPI loopback @ {fmt.SampleRate} Hz {fmt.Channels}ch → {_targetRate} Hz mono)");
            }
            catch (Exception e)
            {
                Console.Error.WriteLine($"System-audio (loopback) capture failed to start: {e.Message}");
                throw;
            }
        }
    }

    // MARK: - Microphone via WASAPI capture

    private void StartMic()
    {
        lock (_micLock)
        {
            try
            {
                var device = _enumerator.GetDefaultAudioEndpoint(DataFlow.Capture, Role.Communications);
                var cap = new WasapiCapture(device);
                var fmt = cap.WaveFormat;
                _micChannels = fmt.Channels;
                _micResampler = new LinearResampler(fmt.SampleRate, _targetRate);
                cap.DataAvailable += (_, e) =>
                {
                    var mono = ToMono(e.Buffer, e.BytesRecorded, fmt);
                    var res = _micResampler!.Process(mono);
                    if (res.Length > 0) _mixer.AppendMic(res);
                };
                cap.RecordingStopped += (_, _) => { };
                cap.StartRecording();
                _micCapture = cap;
                Console.WriteLine($"✓ Microphone capture started (input rate {fmt.SampleRate} Hz {fmt.Channels}ch → {_targetRate} Hz mono)");
            }
            catch (Exception e)
            {
                // The Windows Settings ▸ Privacy ▸ Microphone gate surfaces here as
                // an access-denied (E_ACCESSDENIED / UnauthorizedAccess). Point the
                // user at the setting rather than dying — system audio can still stream.
                Console.Error.WriteLine(
                    $"⚠️  Microphone capture failed: {e.Message}\n" +
                    "   If this is a permission error, enable microphone access in\n" +
                    "   Settings ▸ Privacy & security ▸ Microphone (allow desktop apps), then restart.");
            }
        }
    }

    // MARK: - Device changes

    /// <summary>
    /// Rebuild the affected capture when the default render/capture device changes
    /// mid-meeting. Debounced so a burst of notifications only triggers one rebuild.
    /// Mic/system samples briefly pause but channel alignment holds (the mixer only
    /// drains frames both channels can supply).
    /// </summary>
    private void RebuildFor(DataFlow flow)
    {
        if (!_running || _rebuildScheduled) return;
        _rebuildScheduled = true;
        Task.Run(async () =>
        {
            await Task.Delay(300); // debounce
            _rebuildScheduled = false;
            if (!_running) return;
            Console.WriteLine($"⟳ audio device/config changed ({flow}) — rebuilding capture");
            if (flow == DataFlow.Render)
            {
                lock (_sysLock)
                {
                    try { _loopback?.StopRecording(); } catch { }
                    try { _loopback?.Dispose(); } catch { }
                    _loopback = null;
                }
                StartSystemAudio();
            }
            else
            {
                lock (_micLock)
                {
                    try { _micCapture?.StopRecording(); } catch { }
                    try { _micCapture?.Dispose(); } catch { }
                    _micCapture = null;
                }
                StartMic();
            }
        });
    }

    // IMMNotificationClient — fires on the system's audio device changes.
    public void OnDefaultDeviceChanged(DataFlow flow, Role role, string defaultDeviceId)
    {
        // Only react to the roles we actually use (Multimedia render, Comms capture).
        if (flow == DataFlow.Render && role == Role.Multimedia) RebuildFor(DataFlow.Render);
        else if (flow == DataFlow.Capture && role == Role.Communications) RebuildFor(DataFlow.Capture);
    }

    public void OnDeviceStateChanged(string deviceId, DeviceState newState) { }
    public void OnDeviceAdded(string pwstrDeviceId) { }
    public void OnDeviceRemoved(string deviceId) { }
    public void OnPropertyValueChanged(string pwstrDeviceId, PropertyKey key) { }

    // MARK: - Helpers

    /// <summary>
    /// Convert a WASAPI capture buffer to mono float at the device rate. WASAPI
    /// shared-mode captures are IEEE float32; if multi-channel, average to mono.
    /// Falls back to 16-bit PCM handling if a device ever reports that encoding.
    /// </summary>
    private static float[] ToMono(byte[] buffer, int bytesRecorded, WaveFormat fmt)
    {
        int channels = fmt.Channels;
        if (fmt.Encoding == WaveFormatEncoding.IeeeFloat || fmt.BitsPerSample == 32)
        {
            int floatCount = bytesRecorded / 4;
            int frames = floatCount / channels;
            var mono = new float[frames];
            for (int f = 0; f < frames; f++)
            {
                float acc = 0;
                int baseIdx = f * channels * 4;
                for (int c = 0; c < channels; c++)
                    acc += BitConverter.ToSingle(buffer, baseIdx + c * 4);
                mono[f] = acc / channels;
            }
            return mono;
        }
        else // 16-bit PCM fallback
        {
            int sampleCount = bytesRecorded / 2;
            int frames = sampleCount / channels;
            var mono = new float[frames];
            for (int f = 0; f < frames; f++)
            {
                float acc = 0;
                int baseIdx = f * channels * 2;
                for (int c = 0; c < channels; c++)
                    acc += BitConverter.ToInt16(buffer, baseIdx + c * 2) / 32768f;
                mono[f] = acc / channels;
            }
            return mono;
        }
    }
}
