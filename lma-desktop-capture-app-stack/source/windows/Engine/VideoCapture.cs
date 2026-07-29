using System.IO;
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
    /// <summary>A capturable video source (display or window) for the picker.</summary>
    public readonly record struct Source(string Id, string Name);

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
    private bool _firstFrameSeen;
    public DateTime? FirstFrameDate { get; private set; }

    public VideoCapture(string sourceId, Action<byte[]> onSegment)
    {
        _sourceId = sourceId;
        _onSegment = onSegment;
    }

    // MARK: - Source enumeration (Settings picker)

    /// <summary>
    /// List capturable sources beyond the default: the default "Entire screen"
    /// is the picker's built-in empty-id option; here we add any additional
    /// displays and titled windows (untitled/system windows are noise).
    /// </summary>
    public static List<Source> ListSources()
    {
        var outList = new List<Source>();
        try
        {
            var displays = Recorder.GetDisplays();
            for (int i = 1; i < displays.Count; i++) // index 0 == default "Entire screen"
                outList.Add(new Source($"display:{displays[i].DeviceName}", $"Display {i + 1}"));

            foreach (var w in Recorder.GetWindows())
            {
                if (string.IsNullOrWhiteSpace(w.Title)) continue;
                outList.Add(new Source($"window:{w.Handle}", w.Title));
            }
        }
        catch (Exception e)
        {
            Console.Error.WriteLine($"video source enumeration failed: {e.Message}");
        }
        return outList;
    }

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
                Console.Error.WriteLine($"⚠ video window '{id}' not found; recording the primary display");
            }
            else if (id.StartsWith("display:"))
            {
                var name = id.Substring("display:".Length);
                var disp = Recorder.GetDisplays().FirstOrDefault(d => d.DeviceName == name);
                if (disp != null) return new DisplayRecordingSource(disp.DeviceName);
                Console.Error.WriteLine($"⚠ video display '{id}' not found; recording the primary display");
            }
        }
        catch (Exception e)
        {
            Console.Error.WriteLine($"video source resolve failed ({e.Message}); using primary display");
        }
        return new DisplayRecordingSource(DisplayRecordingSource.MainMonitor);
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
