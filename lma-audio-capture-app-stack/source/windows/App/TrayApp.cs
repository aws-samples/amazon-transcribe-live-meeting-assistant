using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Hardcodet.Wpf.TaskbarNotification;

namespace LMA;

/// <summary>
/// System-tray UI for the LMA Audio Capture App — mirrors macOS MenuBarApp.swift.
///
/// Tray-only (no taskbar window when idle). Left-click the tray icon → popup
/// panel; right-click → context menu with Quit (kept out of the panel so it
/// can't be confused with Stop). While recording, the tray icon turns red.
///
/// Thin view layer over CaptureController (the same engine the CLI uses). Only
/// launched when the app starts with NO CLI args (see Program.Main).
/// </summary>
public sealed class TrayApp
{
    private readonly CaptureController _controller;
    private TaskbarIcon _tray = null!;
    private Popup _popup = null!;
    private PanelView _panel = null!;
    private Application _app = null!;

    // Icons: a neutral (idle) and a red (recording) generated GDI+ icon.
    private System.Drawing.Icon _idleIcon = null!;
    private System.Drawing.Icon _recordingIcon = null!;

    private TrayApp(Config config)
    {
        _controller = new CaptureController(config);
    }

    public static int Run(Config config)
    {
        var trayApp = new TrayApp(config);
        return trayApp.Launch();
    }

    private int Launch()
    {
        // WinExe crashes go to Windows Error Reporting, not a console — log any
        // startup/dispatcher exception to a file next to %TEMP% so failures are
        // diagnosable.
        AppDomain.CurrentDomain.UnhandledException += (_, e) => LogCrash(e.ExceptionObject as Exception);
        _app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        _app.DispatcherUnhandledException += (_, e) => { LogCrash(e.Exception); e.Handled = false; };
        _app.Startup += (_, _) =>
        {
            try { Setup(); }
            catch (Exception ex) { LogCrash(ex); throw; }
        };
        return _app.Run();
    }

    private static void LogCrash(Exception? ex)
    {
        try
        {
            var path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "lma-tray-crash.log");
            System.IO.File.AppendAllText(path, $"[{DateTime.Now:u}] {ex}\n\n");
        }
        catch { }
    }

    private void Setup()
    {
        _idleIcon = IconFactory.Make(recording: false);
        _recordingIcon = IconFactory.Make(recording: true);

        _panel = new PanelView(_controller);

        _popup = new Popup
        {
            Child = _panel,
            StaysOpen = false,
            AllowsTransparency = true,
            PlacementTarget = null,
            Placement = PlacementMode.AbsolutePoint,
        };

        _tray = new TaskbarIcon
        {
            Icon = _idleIcon,
            ToolTipText = "LMA Audio Capture",
        };
        _tray.TrayLeftMouseUp += (_, _) => TogglePopup();
        _tray.ContextMenu = BuildContextMenu();

        // Bind state → icon + panel refresh. Marshal engine callbacks onto the UI thread.
        _controller.OnStateChange = s => _app.Dispatcher.Invoke(() =>
        {
            _panel.OnState(s);
            _tray.Icon = (s.Kind == CaptureController.StateKind.Streaming) ? _recordingIcon : _idleIcon;
            _tray.ToolTipText = s.Kind == CaptureController.StateKind.Streaming ? "LMA Audio Capture — Recording" : "LMA Audio Capture";
        });
        _controller.OnLevels = (m, k, c, p) => _app.Dispatcher.Invoke(() => _panel.OnLevels(m, k, c, p));
        _controller.OnLog = msg => _app.Dispatcher.Invoke(() => _panel.OnLog(msg));

        _panel.RequestClosePopup = () => _popup.IsOpen = false;

        // Prefill remembered email.
        _panel.InitFromSettings();
    }

    private ContextMenu BuildContextMenu()
    {
        var menu = new ContextMenu();
        var open = new MenuItem { Header = "Open LMA Audio Capture" };
        open.Click += (_, _) => ShowPopup();
        var quit = new MenuItem { Header = "Quit LMA Audio Capture" };
        quit.Click += (_, _) =>
        {
            // Stop cleanly if streaming, then shut down.
            if (_controller.CurrentState.Kind == CaptureController.StateKind.Streaming) _controller.Stop();
            _tray.Dispose();
            _app.Shutdown();
        };
        menu.Items.Add(open);
        menu.Items.Add(new Separator());
        menu.Items.Add(quit);
        return menu;
    }

    private void TogglePopup()
    {
        if (_popup.IsOpen) _popup.IsOpen = false;
        else ShowPopup();
    }

    private void ShowPopup()
    {
        _panel.Refresh();
        // Place the popup near the cursor / tray (bottom-right corner area).
        var pos = System.Windows.Forms.Control.MousePosition;
        var src = PresentationSource.FromVisual(_panel) ?? null;
        // Convert device pixels to WPF DIPs (96 dpi baseline).
        double dpiScale = GetDpiScale();
        _popup.HorizontalOffset = pos.X / dpiScale - 300;
        _popup.VerticalOffset = pos.Y / dpiScale - 420;
        if (_popup.HorizontalOffset < 0) _popup.HorizontalOffset = 0;
        if (_popup.VerticalOffset < 0) _popup.VerticalOffset = 0;
        _popup.IsOpen = true;
        _panel.FocusFirstField();
    }

    private static double GetDpiScale()
    {
        var src = System.Windows.PresentationSource.FromVisual(System.Windows.Application.Current.MainWindow ?? new Window());
        return src?.CompositionTarget?.TransformToDevice.M11 ?? 1.0;
    }
}

/// <summary>
/// Generates simple tray icons at runtime via GDI+ (no image assets needed).
/// Idle = neutral gray disc with a white waveform; recording = red disc with a
/// white dot — mirrors the macOS menu-bar icon turning red while recording.
/// </summary>
internal static class IconFactory
{
    public static System.Drawing.Icon Make(bool recording)
    {
        const int size = 32;
        using var bmp = new System.Drawing.Bitmap(size, size);
        using (var g = System.Drawing.Graphics.FromImage(bmp))
        {
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            g.Clear(System.Drawing.Color.Transparent);
            var bg = recording ? System.Drawing.Color.FromArgb(0xD4, 0x2A, 0x2A)
                               : System.Drawing.Color.FromArgb(0x53, 0x5B, 0x66);
            using var bgBrush = new System.Drawing.SolidBrush(bg);
            g.FillEllipse(bgBrush, 1, 1, size - 2, size - 2);
            using var fg = new System.Drawing.SolidBrush(System.Drawing.Color.White);
            if (recording)
            {
                g.FillEllipse(fg, size / 2 - 6, size / 2 - 6, 12, 12);
            }
            else
            {
                int[] heights = { 6, 12, 9, 14, 7 };
                float x = 7;
                foreach (var h in heights)
                {
                    g.FillRectangle(fg, x, (size - h) / 2f, 3, h);
                    x += 4.5f;
                }
            }
        }
        // Bitmap → HICON → managed Icon (cloned so we can free the GDI handle).
        var hicon = bmp.GetHicon();
        using var tmp = System.Drawing.Icon.FromHandle(hicon);
        return (System.Drawing.Icon)tmp.Clone();
    }
}
