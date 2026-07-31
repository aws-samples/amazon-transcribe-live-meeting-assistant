using System.IO;
using System.Runtime.InteropServices;
using ScreenRecorderLib;

namespace LMA;

/// <summary>
/// Optional desktop-video capture (Windows counterpart to macOS VideoCapture):
/// records the chosen display or window at a low frame rate, H.264-encoded in
/// fragmented-MP4 (CMAF-style) via ScreenRecorderLib (which wraps
/// Windows.Graphics.Capture + Media Foundation), and hands each fMP4 chunk to a
/// callback for streaming over the video websocket.
///
/// ScreenRecorderLib writes to a Stream in fragmented mode; we plug in a
/// <see cref="CallbackStream"/> so every write becomes a socket send — nothing
/// is buffered to disk on the client.
///
/// Encoder invariants the server/pipeline relies on (matching the macOS client):
///   • Fragmented MP4 (fragmented moov + moof/mdat) so the server file is
///     playable/probe-able as it grows and ffmpeg can mux at call end.
///   • Keyframe at least every ~2 seconds (KeyFrameIntervalInSeconds) for cheap
///     seeking by future frame-extraction features.
///   • H.264, ~5 fps, capped resolution — screen content compresses well.
///
/// Video is best-effort: any failure here is logged and reported via OnFailed;
/// it never touches the audio path.
/// </summary>
public sealed class VideoCapture
{
    /// <summary>
    /// A capturable video source (display or window) for the Settings picker.
    /// Carries enough detail to tell two similar sources apart: whether it is a
    /// whole display, and its pixel size. Mirrors the macOS VideoCapture.Source.
    /// </summary>
    public readonly record struct Source(
        string Id, string Name, bool IsDisplay = false, int Width = 0, int Height = 0)
    {
        /// <summary>"2560 × 1440", or "" when the size isn't known.</summary>
        public string DimensionsText =>
            Width > 0 && Height > 0 ? $"{Width} × {Height}" : "";
    }

    /// <summary>Frames per second — matches the macOS client and the VP.</summary>
    public const int Fps = 5;
    /// <summary>Cap the longest edge; enough for legible slide text.</summary>
    public const int MaxDimension = 1920;
    /// <summary>Target H.264 bitrate (bits/sec). Screen content at 5 fps is light.</summary>
    public const int BitsPerSecond = 1_500_000;

    private readonly string _sourceId;
    private readonly Action<byte[]> _onSegment;
    private Recorder? _recorder;
    private CallbackStream? _stream;
    private volatile bool _stopped;

    /// <summary>Fired (once) if capture fails to start or dies mid-recording.</summary>
    public Action<string>? OnFailed;
    /// <summary>Fired when the first frame is written (offset reporting).</summary>
    public Action? OnFirstFrame;
    /// <summary>
    /// Fired when the requested source wasn't available and capture fell back to
    /// a different one (privacy-relevant: a window choice becoming a whole
    /// display). Passes a user-facing message.
    /// </summary>
    public Action<string>? OnFallback;
    private bool _firstFrameSeen;
    public DateTime? FirstFrameDate { get; private set; }

    public VideoCapture(string sourceId, Action<byte[]> onSegment)
    {
        _sourceId = sourceId;
        _onSegment = onSegment;
    }

    // MARK: - Source enumeration (Settings picker)

    /// <summary>
    /// List capturable sources: every display, then titled windows (untitled or
    /// system windows are noise, not meeting content).
    ///
    /// The FIRST display carries the EMPTY id, which is the picker's default and
    /// means "the primary display at capture time" — ResolveSource maps an empty
    /// id to DisplayRecordingSource.MainMonitor, so the two must agree.
    ///
    /// Displays are named from the monitor's own description (e.g.
    /// "DELL U2720Q") with their resolution, so two screens can be told apart —
    /// "Display 2" alone is not something a user can act on.
    /// </summary>
    public static List<Source> ListSources()
    {
        var outList = new List<Source>();
        try
        {
            var displays = Recorder.GetDisplays();
            for (int i = 0; i < displays.Count; i++)
            {
                var device = displays[i].DeviceName;
                var (w, h) = DisplayResolution(device);
                outList.Add(new Source(
                    // Index 0 is the picker default and takes the empty id.
                    Id: i == 0 ? "" : $"display:{device}",
                    Name: DisplayName(device, i),
                    IsDisplay: true, Width: w, Height: h));
            }
            // Defensive: if display enumeration came back empty, the default
            // source must still be offerable — otherwise the picker would have
            // no Screens entry at all and nothing mapping to the empty id.
            if (outList.Count == 0)
                outList.Add(new Source("", "Main display", IsDisplay: true));

            foreach (var win in Recorder.GetWindows())
            {
                if (string.IsNullOrWhiteSpace(win.Title)) continue;
                var (w, h) = WindowSize(win.Handle);
                // Skip tiny utility windows, matching the macOS client.
                if (w > 0 && h > 0 && (w < 200 || h < 150)) continue;
                outList.Add(new Source($"window:{win.Handle}", win.Title,
                                       IsDisplay: false, Width: w, Height: h));
            }
        }
        catch (Exception e)
        {
            Console.Error.WriteLine($"video source enumeration failed: {e.Message}");
        }
        return outList;
    }

    // MARK: - Display/window metadata (Win32)
    //
    // These use plain user32 P/Invoke rather than ScreenRecorderLib properties so
    // the picker's labels don't depend on that library's surface. Each is
    // best-effort: on failure the caller gets a positional name and no size, and
    // the UI simply omits the resolution.

    /// <summary>
    /// The monitor's own description (what Display Settings shows), e.g.
    /// "DELL U2720Q". Falls back to a positional label.
    /// </summary>
    private static string DisplayName(string deviceName, int index)
    {
        try
        {
            var dd = new DISPLAY_DEVICE { cb = Marshal.SizeOf<DISPLAY_DEVICE>() };
            // iDevNum 0 with EDD_GET_DEVICE_INTERFACE_NAME(0) on a display name
            // returns the MONITOR attached to that adapter output.
            if (EnumDisplayDevices(deviceName, 0, ref dd, 0)
                && !string.IsNullOrWhiteSpace(dd.DeviceString))
            {
                var name = dd.DeviceString.Trim();
                // "Generic PnP Monitor" is the uninformative default; a
                // positional label is no worse and stays consistent.
                if (!name.Equals("Generic PnP Monitor", StringComparison.OrdinalIgnoreCase))
                    return index == 0 ? $"{name} (main)" : name;
            }
        }
        catch { /* fall through to the positional label */ }
        return index == 0 ? "Main display" : $"Display {index + 1}";
    }

    /// <summary>Current mode's pixel size for a display, or (0,0) if unknown.</summary>
    private static (int, int) DisplayResolution(string deviceName)
    {
        try
        {
            var dm = new DEVMODE { dmSize = (short)Marshal.SizeOf<DEVMODE>() };
            if (EnumDisplaySettings(deviceName, ENUM_CURRENT_SETTINGS, ref dm))
                return (dm.dmPelsWidth, dm.dmPelsHeight);
        }
        catch { /* size is optional in the UI */ }
        return (0, 0);
    }

    /// <summary>On-screen size of a window, or (0,0) if unknown.</summary>
    private static (int, int) WindowSize(IntPtr handle)
    {
        try
        {
            if (GetWindowRect(handle, out var r))
                return (r.Right - r.Left, r.Bottom - r.Top);
        }
        catch { /* size is optional in the UI */ }
        return (0, 0);
    }

    private const int ENUM_CURRENT_SETTINGS = -1;

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct DISPLAY_DEVICE
    {
        public int cb;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
        public int StateFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceID;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
    }

    // Only the mode fields we read are named; the rest is padding laid out to
    // match the documented DEVMODEW so dmPelsWidth/Height land at the right
    // offsets.
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct DEVMODE
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
        public short dmSpecVersion, dmDriverVersion, dmSize, dmDriverExtra;
        public int dmFields;
        public int dmPositionX, dmPositionY;
        public int dmDisplayOrientation, dmDisplayFixedOutput;
        public short dmColor, dmDuplex, dmYResolution, dmTTOption, dmCollate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
        public short dmLogPixels;
        public int dmBitsPerPel, dmPelsWidth, dmPelsHeight;
        public int dmDisplayFlags, dmDisplayFrequency;
        public int dmICMMethod, dmICMIntent, dmMediaType, dmDitherType;
        public int dmReserved1, dmReserved2, dmPanningWidth, dmPanningHeight;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool EnumDisplayDevices(
        string? lpDevice, uint iDevNum, ref DISPLAY_DEVICE lpDisplayDevice, uint dwFlags);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool EnumDisplaySettings(
        string? lpszDeviceName, int iModeNum, ref DEVMODE lpDevMode);

    [DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);

    // MARK: - Lifecycle

    public void Start()
    {
        // Resolve the persisted source id to a ScreenRecorderLib source; fall
        // back to the primary display when it's gone (window closed, display
        // unplugged, stale id after reboot).
        RecordingSourceBase source = ResolveSource(_sourceId);

        var opts = new RecorderOptions
        {
            SourceOptions = new SourceOptions { RecordingSources = { source } },
            OutputOptions = new OutputOptions
            {
                RecorderMode = RecorderMode.Video,
                // Cap the longest edge; keep aspect ratio (0 = auto for the
                // other dimension). ScreenRecorderLib letterboxes as needed.
                OutputFrameSize = new ScreenSize(MaxDimension, 0),
                Stretch = StretchMode.Uniform,
            },
            VideoEncoderOptions = new VideoEncoderOptions
            {
                Encoder = new H264VideoEncoder
                {
                    BitrateMode = H264BitrateControlMode.UnconstrainedVBR,
                },
                Bitrate = BitsPerSecond,
                Framerate = Fps,
                // ~2s keyframe cadence => cheap seeking for frame extraction.
                Quality = 70,
                IsFragmentedMp4Enabled = true,
                IsHardwareEncodingEnabled = true,
                IsLowLatencyEnabled = true,
            },
            AudioOptions = new AudioOptions { IsAudioEnabled = false }, // audio is AudioCapture's job
            MouseOptions = new MouseOptions { IsMousePointerEnabled = true },
        };

        try
        {
            _stream = new CallbackStream(OnChunk);
            _recorder = Recorder.CreateRecorder(opts);
            _recorder.OnRecordingComplete += (_, _) => { };
            _recorder.OnRecordingFailed += (_, e) =>
            {
                if (!_stopped) OnFailed?.Invoke(e.Error);
            };
            _recorder.Record(_stream);
            Console.WriteLine($"✓ Screen video capture started (H.264 @ {Fps} fps → fMP4)");
        }
        catch (Exception e)
        {
            OnFailed?.Invoke(e.Message);
            throw;
        }
    }

    /// <summary>
    /// Mirror the audio mixer's pause state. ScreenRecorderLib's Pause stops
    /// emitting frames and resumes the timeline where it left off, so the video
    /// stays aligned with the audio recording (which also excises paused time).
    /// </summary>
    public void SetPaused(bool paused)
    {
        var rec = _recorder;
        if (rec == null) return;
        try
        {
            if (paused) rec.Pause();
            else rec.Resume();
        }
        catch (Exception e)
        {
            Console.Error.WriteLine($"video pause/resume failed: {e.Message}");
        }
    }

    /// <summary>Stop capture and flush the final fMP4 fragment(s).</summary>
    public async Task StopAsync()
    {
        _stopped = true;
        var rec = _recorder;
        _recorder = null;
        if (rec == null) return;

        var done = new TaskCompletionSource();
        void Handler(object? _, RecordingCompleteEventArgs __) => done.TrySetResult();
        rec.OnRecordingComplete += Handler;
        try
        {
            rec.Stop();
            // Bounded wait: don't hang shutdown if the encoder stalls.
            await Task.WhenAny(done.Task, Task.Delay(TimeSpan.FromSeconds(5)));
        }
        catch { /* best effort */ }
        finally
        {
            try { rec.Dispose(); } catch { }
            _stream?.Dispose();
            _stream = null;
        }
        Console.WriteLine("✓ Screen video capture stopped");
    }

    private RecordingSourceBase ResolveSource(string id)
    {
        try
        {
            if (id.StartsWith("window:") && long.TryParse(id.AsSpan("window:".Length), out var handle))
            {
                var win = Recorder.GetWindows().FirstOrDefault(w => w.Handle == (IntPtr)handle);
                if (win != null) return new WindowRecordingSource(win.Handle);
                // Falling back from a window to the WHOLE SCREEN changes what
                // gets recorded — tell the user instead of doing it silently.
                Report("The window you chose for screen video is no longer open — recording the whole screen instead.");
            }
            else if (id.StartsWith("display:"))
            {
                var name = id.Substring("display:".Length);
                var disp = Recorder.GetDisplays().FirstOrDefault(d => d.DeviceName == name);
                if (disp != null) return new DisplayRecordingSource(disp.DeviceName);
                Report("The display you chose for screen video isn't available — recording the main display instead.");
            }
        }
        catch (Exception e)
        {
            Console.Error.WriteLine($"video source resolve failed ({e.Message}); using primary display");
        }
        return new DisplayRecordingSource(DisplayRecordingSource.MainMonitor);
    }

    private void Report(string message)
    {
        Console.Error.WriteLine($"⚠ {message}");
        OnFallback?.Invoke(message);
    }

    private void OnChunk(byte[] data)
    {
        if (_stopped || data.Length == 0) return;
        if (!_firstFrameSeen)
        {
            _firstFrameSeen = true;
            FirstFrameDate = DateTime.UtcNow;
            OnFirstFrame?.Invoke();
        }
        _onSegment(data);
    }
}

/// <summary>
/// A write-only Stream that forwards every write to a callback (the video
/// socket send). ScreenRecorderLib writes fragmented-MP4 bytes here as they are
/// encoded, so nothing is staged to disk on the client. Reads/seeks are not
/// supported (the encoder only writes).
/// </summary>
internal sealed class CallbackStream : Stream
{
    private readonly Action<byte[]> _onWrite;
    private long _position;

    public CallbackStream(Action<byte[]> onWrite) { _onWrite = onWrite; }

    public override bool CanRead => false;
    public override bool CanSeek => false;
    public override bool CanWrite => true;
    public override long Length => _position;
    public override long Position { get => _position; set { } }

    public override void Write(byte[] buffer, int offset, int count)
    {
        if (count <= 0) return;
        var chunk = new byte[count];
        Buffer.BlockCopy(buffer, offset, chunk, 0, count);
        _position += count;
        _onWrite(chunk);
    }

    public override void Flush() { }
    public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
}
