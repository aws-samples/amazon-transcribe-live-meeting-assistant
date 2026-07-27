using System.Drawing.Imaging;
using System.IO;
using System.IO.Pipes;
using System.Windows.Media.Imaging;

namespace LMA;

/// <summary>
/// Runtime-generated images for the taskbar button: the window/app icon, the
/// recording-state overlays (TaskbarItemInfo.Overlay), and the thumbnail-toolbar
/// glyphs (TaskbarItemInfo.ThumbButtonInfos). Mirrors the macOS Dock tile, which
/// draws the app icon plus a red dot while recording.
///
/// Drawn with GDI+ like the tray icons (no image assets to ship or keep in sync),
/// then handed to WPF as a PNG-backed BitmapSource — round-tripping through PNG
/// preserves the alpha channel, which CreateBitmapSourceFromHBitmap does not.
/// </summary>
internal static class TaskbarImages
{
    /// <summary>The LMA glyph as a WPF image, for Window.Icon (the taskbar button).</summary>
    public static BitmapSource AppIcon(bool recording)
    {
        using var bmp = new System.Drawing.Bitmap(64, 64);
        using (var g = System.Drawing.Graphics.FromImage(bmp))
        {
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            g.Clear(System.Drawing.Color.Transparent);
            IconFactory.Draw(g, 64, recording);
        }
        return ToBitmapSource(bmp);
    }

    /// <summary>Red dot with a white ring — the "recording" overlay badge.</summary>
    public static BitmapSource RecordingOverlay() =>
        Badge(System.Drawing.Color.FromArgb(0xD4, 0x2A, 0x2A), pause: false);

    /// <summary>Amber pause badge — the "paused" overlay badge.</summary>
    public static BitmapSource PausedOverlay() =>
        Badge(System.Drawing.Color.FromArgb(0xE0, 0x91, 0x00), pause: true);

    public static BitmapSource ThumbPause() => Glyph(g =>
    {
        using var w = new System.Drawing.SolidBrush(System.Drawing.Color.White);
        g.FillRectangle(w, 9, 7, 5, 18);
        g.FillRectangle(w, 18, 7, 5, 18);
    });

    public static BitmapSource ThumbResume() => Glyph(g =>
    {
        using var w = new System.Drawing.SolidBrush(System.Drawing.Color.White);
        g.FillPolygon(w, new[]
        {
            new System.Drawing.PointF(10, 6),
            new System.Drawing.PointF(25, 16),
            new System.Drawing.PointF(10, 26),
        });
    });

    public static BitmapSource ThumbStop() => Glyph(g =>
    {
        using var w = new System.Drawing.SolidBrush(System.Drawing.Color.White);
        g.FillRectangle(w, 8, 8, 16, 16);
    });

    // Overlay badges are composited by the shell into the lower-right corner of the
    // taskbar icon at 16x16, so they're drawn at 32x32 (for high-DPI) with a white
    // ring that keeps them legible against any wallpaper or icon underneath.
    private static BitmapSource Badge(System.Drawing.Color fill, bool pause)
    {
        using var bmp = new System.Drawing.Bitmap(32, 32);
        using (var g = System.Drawing.Graphics.FromImage(bmp))
        {
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            g.Clear(System.Drawing.Color.Transparent);
            using var ring = new System.Drawing.SolidBrush(System.Drawing.Color.White);
            using var body = new System.Drawing.SolidBrush(fill);
            g.FillEllipse(ring, 0, 0, 31, 31);
            g.FillEllipse(body, 3, 3, 25, 25);
            if (pause)
            {
                g.FillRectangle(ring, 11, 10, 3.5f, 12);
                g.FillRectangle(ring, 17.5f, 10, 3.5f, 12);
            }
        }
        return ToBitmapSource(bmp);
    }

    // Thumbnail-toolbar buttons are drawn by the shell on a dark chrome strip, so
    // the glyphs are plain white on transparent (same convention as Media Player).
    private static BitmapSource Glyph(Action<System.Drawing.Graphics> draw)
    {
        using var bmp = new System.Drawing.Bitmap(32, 32);
        using (var g = System.Drawing.Graphics.FromImage(bmp))
        {
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            g.Clear(System.Drawing.Color.Transparent);
            draw(g);
        }
        return ToBitmapSource(bmp);
    }

    private static BitmapSource ToBitmapSource(System.Drawing.Bitmap bmp)
    {
        using var ms = new MemoryStream();
        bmp.Save(ms, ImageFormat.Png);
        ms.Position = 0;
        var img = new BitmapImage();
        img.BeginInit();
        img.CacheOption = BitmapCacheOption.OnLoad;   // read now so we can dispose the stream
        img.StreamSource = ms;
        img.EndInit();
        img.Freeze();                                 // usable from any thread, cheap to reuse
        return img;
    }
}

/// <summary>
/// Single-instance command channel for the taskbar JumpList.
///
/// A JumpTask can only *launch the executable with arguments* — it can't talk to
/// the running app. Without this, right-clicking the taskbar button and choosing
/// "Stop Recording" would start a SECOND process that knows nothing about the
/// first one's capture. So the freshly-launched process forwards the verb over a
/// per-user named pipe to the instance that owns the recording and exits; only if
/// nobody answers does it start the GUI itself (see Program.Main).
/// </summary>
internal static class TrayIpc
{
    // Verbs used by both the JumpList tasks (as command-line arguments) and the
    // pipe protocol, so there's exactly one spelling of each.
    public const string CmdStart = "--lma-start";
    public const string CmdPause = "--lma-pause";
    public const string CmdStop = "--lma-stop";
    public const string CmdPanel = "--lma-panel";

    private static readonly string[] AllCommands = { CmdStart, CmdPause, CmdStop, CmdPanel };

    /// <summary>The first taskbar/JumpList control verb in argv, or null.</summary>
    public static string? FindCommand(string[] args) =>
        args.FirstOrDefault(a => AllCommands.Contains(a, StringComparer.OrdinalIgnoreCase));

    private static string PipeName
    {
        get
        {
            // Pipe names are machine-global, so scope by user; strip anything that
            // isn't safe in a pipe name (domain\user, spaces, ...).
            var user = new string((Environment.UserName ?? "user")
                .Where(char.IsLetterOrDigit).ToArray());
            if (user.Length == 0) user = "user";
            return $"LMAAudioCapture.control.{user}";
        }
    }

    /// <summary>Send a verb to the running instance. False = nobody is listening.</summary>
    public static bool TrySend(string command)
    {
        try
        {
            using var client = new NamedPipeClientStream(".", PipeName, PipeDirection.Out);
            client.Connect(500);
            using var w = new StreamWriter(client) { AutoFlush = true };
            w.WriteLine(command);
            return true;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// Listen for forwarded verbs on a background thread. <paramref name="onCommand"/>
    /// is called off the UI thread — marshal it yourself. Best-effort: if another
    /// instance already owns the pipe we give up rather than spin forever.
    /// </summary>
    public static void StartServer(Action<string> onCommand)
    {
        var thread = new Thread(() =>
        {
            int consecutiveFailures = 0;
            while (consecutiveFailures < 5)
            {
                try
                {
                    using var server = new NamedPipeServerStream(PipeName, PipeDirection.In, 1);
                    server.WaitForConnection();
                    consecutiveFailures = 0;
                    using var r = new StreamReader(server);
                    var line = r.ReadLine();
                    if (!string.IsNullOrWhiteSpace(line)) onCommand(line.Trim());
                }
                catch
                {
                    consecutiveFailures++;
                    Thread.Sleep(250);
                }
            }
        })
        {
            IsBackground = true,
            Name = "lma-taskbar-ipc",
        };
        thread.Start();
    }
}
