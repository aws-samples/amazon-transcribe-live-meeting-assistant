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
    private Window _flyout = null!;
    private PanelView _panel = null!;
    private Application _app = null!;

    // Guards the "click the tray icon to close" race: clicking the icon while the
    // flyout is open first fires Deactivated (which hides it), then TrayLeftMouseUp
    // — without this the toggle would immediately reopen it. Set in Deactivated.
    private DateTime _hiddenAt = DateTime.MinValue;

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

        // Host the panel in a real borderless Window (NOT a Popup). A standalone
        // WPF Popup never gets an activated top-level HWND, so TextBox/PasswordBox
        // inside it cannot receive keyboard focus — you can click the fields but
        // can't type. A Window gets a proper HWND and activates, so input works.
        _flyout = new Window
        {
            Content = _panel,
            WindowStyle = WindowStyle.None,
            ResizeMode = ResizeMode.NoResize,
            AllowsTransparency = true,
            Background = System.Windows.Media.Brushes.Transparent,
            ShowInTaskbar = false,
            Topmost = true,
            SizeToContent = SizeToContent.WidthAndHeight,
            ShowActivated = true,
        };
        // Close when the user clicks away (flyout behavior).
        _flyout.Deactivated += (_, _) =>
        {
            _hiddenAt = DateTime.Now;
            _flyout.Hide();
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

        _panel.RequestClosePopup = () => _flyout.Hide();

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
            try { _flyout.Close(); } catch { }
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
        // If we just hid it via Deactivated (from this same click on the tray
        // icon), don't immediately reopen — treat it as a close.
        if ((DateTime.Now - _hiddenAt).TotalMilliseconds < 250) return;
        if (_flyout.IsVisible) _flyout.Hide();
        else ShowPopup();
    }

    private void ShowPopup()
    {
        _panel.Refresh();
        // Show first so SizeToContent has measured the window, then position it
        // near the tray (bottom-right), clamped to the working area.
        _flyout.Show();

        double dpiScale = GetDpiScale();
        var wa = System.Windows.Forms.Screen.PrimaryScreen?.WorkingArea
                 ?? new System.Drawing.Rectangle(0, 0, 1920, 1080);
        double waRightDip = wa.Right / dpiScale;
        double waBottomDip = wa.Bottom / dpiScale;

        double w = _flyout.ActualWidth > 0 ? _flyout.ActualWidth : 300;
        double h = _flyout.ActualHeight > 0 ? _flyout.ActualHeight : 420;
        // Anchor to the bottom-right corner (above the notification area), with a
        // small margin. Clamp so it never lands off-screen.
        double left = Math.Max(0, waRightDip - w - 12);
        double top = Math.Max(0, waBottomDip - h - 12);
        _flyout.Left = left;
        _flyout.Top = top;

        _flyout.Activate();
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
