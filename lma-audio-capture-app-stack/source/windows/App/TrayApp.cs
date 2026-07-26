using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Shell;
using Hardcodet.Wpf.TaskbarNotification;

namespace LMA;

/// <summary>
/// System-tray UI for the LMA Audio Capture App — mirrors macOS MenuBarApp.swift.
///
/// Tray-only when idle (no taskbar button, no window). Left-click the tray icon →
/// popup panel; right-click → context menu with Quit (kept out of the panel so it
/// can't be confused with Stop). While recording, the tray icon turns red.
///
/// While recording the app also takes a **taskbar button** (the Windows analog of
/// the macOS Dock tile added in the macOS client): a red dot overlay on the icon,
/// a yellow paused wash when paused, thumbnail-toolbar buttons for Pause/Stop, and
/// a JumpList of quick actions. It appears when recording starts and disappears
/// when it stops, so the idle app stays out of the way — but you always have a
/// visible, always-on-screen way to see and stop a live recording even if Windows
/// tucks the tray icon into the ▲ overflow.
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

    // The recording-time taskbar presence: a normal (titled, minimizable) window
    // that is only ever *shown* while recording, so the taskbar button exists
    // exactly while there's something to show state for.
    private Window _taskbarWindow = null!;
    private TaskbarItemInfo _taskbarInfo = null!;
    private ThumbButtonInfo _thumbPause = null!;
    private ThumbButtonInfo _thumbStop = null!;

    // PanelView is a single instance shared by both windows. WPF allows an element
    // exactly one parent, so it is moved between them rather than duplicated; this
    // is the host that currently owns it.
    private Window? _panelHost;

    // Cached images (frozen, so re-assigning them is cheap).
    private BitmapSource _iconIdleImage = null!;
    private BitmapSource _iconRecordingImage = null!;
    private BitmapSource _overlayRecording = null!;
    private BitmapSource _overlayPaused = null!;
    private BitmapSource _thumbPauseImage = null!;
    private BitmapSource _thumbResumeImage = null!;

    // Last applied taskbar visuals, so the 100 ms level callback doesn't rewrite
    // the shell's overlay/progress state on every audio buffer.
    private bool? _shownStreaming;
    private bool? _shownPaused;

    // Set by Quit so the taskbar window's Closing handler stops intercepting the
    // close (it normally cancels it to keep a recording alive).
    private bool _shuttingDown;

    // Guards the "click the tray icon to close" race: clicking the icon while the
    // flyout is open first fires Deactivated (which hides it), then TrayLeftMouseUp
    // — without this the toggle would immediately reopen it. Set in Deactivated.
    private DateTime _hiddenAt = DateTime.MinValue;

    // Icons: a neutral (idle) and a red (recording) generated GDI+ icon.
    private System.Drawing.Icon _idleIcon = null!;
    private System.Drawing.Icon _recordingIcon = null!;

    // A JumpList verb that launched this process because no instance was running
    // (see Program.Main); applied once the UI is up.
    private readonly string? _pendingCommand;

    private TrayApp(Config config, string? pendingCommand)
    {
        _controller = new CaptureController(config);
        _pendingCommand = pendingCommand;
    }

    public static int Run(Config config, string? pendingCommand = null)
    {
        var trayApp = new TrayApp(config, pendingCommand);
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

        _iconIdleImage = TaskbarImages.AppIcon(recording: false);
        _iconRecordingImage = TaskbarImages.AppIcon(recording: true);
        _overlayRecording = TaskbarImages.RecordingOverlay();
        _overlayPaused = TaskbarImages.PausedOverlay();
        _thumbPauseImage = TaskbarImages.ThumbPause();
        _thumbResumeImage = TaskbarImages.ThumbResume();

        _panel = new PanelView(_controller);

        // Host the panel in a real borderless Window (NOT a Popup). A standalone
        // WPF Popup never gets an activated top-level HWND, so TextBox/PasswordBox
        // inside it cannot receive keyboard focus — you can click the fields but
        // can't type. A Window gets a proper HWND and activates, so input works.
        _flyout = new Window
        {
            WindowStyle = WindowStyle.None,
            ResizeMode = ResizeMode.NoResize,
            AllowsTransparency = true,
            Background = System.Windows.Media.Brushes.Transparent,
            ShowInTaskbar = false,
            Topmost = true,
            SizeToContent = SizeToContent.WidthAndHeight,
            ShowActivated = true,
        };
        // Close when the user clicks away (flyout behavior). Not while the panel has
        // been handed to the taskbar window — then the flyout owns nothing and any
        // deactivation is somebody else's business.
        _flyout.Deactivated += (_, _) =>
        {
            if (_panelHost != _flyout) return;
            _hiddenAt = DateTime.Now;
            _flyout.Hide();
        };

        SetupTaskbarWindow();

        _tray = new TaskbarIcon
        {
            Icon = _idleIcon,
            ToolTipText = "LMA Audio Capture",
        };
        _tray.TrayLeftMouseUp += (_, _) => TogglePopup();
        _tray.ContextMenu = BuildContextMenu();

        // Ask Windows to keep our tray icon out of the overflow (▲) flyout so it's
        // always visible — especially the red recording icon. Best-effort. The
        // shell records the icon a moment after it first appears, so re-assert on
        // short delays to catch that first registration.
        TrayIconVisibility.PromoteToTaskbar();
        foreach (var ms in new[] { 3000, 10000 })
        {
            var t = new System.Windows.Threading.DispatcherTimer { Interval = TimeSpan.FromMilliseconds(ms) };
            t.Tick += (_, _) => { t.Stop(); TrayIconVisibility.PromoteToTaskbar(); };
            t.Start();
        }

        // Bind state → icon + panel refresh. Marshal engine callbacks onto the UI thread.
        _controller.OnStateChange = s => _app.Dispatcher.Invoke(() =>
        {
            _panel.OnState(s);
            bool streaming = s.Kind == CaptureController.StateKind.Streaming;
            _tray.Icon = streaming ? _recordingIcon : _idleIcon;
            _tray.ToolTipText = streaming ? "LMA Audio Capture — Recording" : "LMA Audio Capture";
            // Re-assert promotion when recording starts so the red icon is visible.
            if (streaming) TrayIconVisibility.PromoteToTaskbar();
            SyncTaskbar();
        });
        _controller.OnLevels = (m, k, c, p) => _app.Dispatcher.Invoke(() =>
        {
            _panel.OnLevels(m, k, c, p);
            // Pause/resume can also be driven from the thumbnail toolbar or jump
            // list, so track the mixer's paused flag here rather than only in the
            // panel's click handler. SyncTaskbar() no-ops unless something changed.
            SyncTaskbar();
        });
        _controller.OnLog = msg => _app.Dispatcher.Invoke(() => _panel.OnLog(msg));

        _panel.RequestClosePopup = () => { if (_panelHost == _flyout) _flyout.Hide(); };

        // Accept verbs forwarded by a JumpList launch (see TrayIpc / Program.Main).
        TrayIpc.StartServer(cmd => _app.Dispatcher.Invoke(() => HandleCommand(cmd)));
        BuildJumpList();

        // Prefill remembered email.
        _panel.InitFromSettings();
        MovePanelTo(_flyout);

        // Launched by a jump-list verb with no instance running: nothing was
        // recording, so the only sensible verbs are Start (needs a saved session,
        // else the panel explains why) and Open Control Panel. Both land in the
        // panel, which is also the right answer for a cold "Stop".
        if (_pendingCommand != null)
        {
            if (_pendingCommand == TrayIpc.CmdStart) HandleCommand(TrayIpc.CmdStart);
            else ShowPopup();
        }
    }

    // MARK: - Recording-time taskbar presence (Windows analog of the macOS Dock tile)

    /// <summary>
    /// Create the window that owns the taskbar button. It is a normal titled window
    /// (so it has a real HWND and keyboard input works, same reason the flyout is a
    /// Window and not a Popup) and is only ever shown while recording.
    /// </summary>
    private void SetupTaskbarWindow()
    {
        _thumbPause = new ThumbButtonInfo
        {
            ImageSource = _thumbPauseImage,
            Description = "Pause recording",
            DismissWhenClicked = false,
        };
        _thumbPause.Click += (_, _) => TogglePause();

        _thumbStop = new ThumbButtonInfo
        {
            ImageSource = TaskbarImages.ThumbStop(),
            Description = "Stop recording",
            DismissWhenClicked = false,
        };
        _thumbStop.Click += (_, _) => StopRecording();

        _taskbarInfo = new TaskbarItemInfo { Description = "LMA Audio Capture" };
        _taskbarInfo.ThumbButtonInfos.Add(_thumbPause);
        _taskbarInfo.ThumbButtonInfos.Add(_thumbStop);

        _taskbarWindow = new Window
        {
            Title = "LMA Audio Capture",
            Icon = _iconIdleImage,
            ResizeMode = ResizeMode.CanMinimize,
            SizeToContent = SizeToContent.WidthAndHeight,
            WindowStartupLocation = WindowStartupLocation.CenterScreen,
            ShowInTaskbar = true,
            TaskbarItemInfo = _taskbarInfo,
            Background = new SolidColorBrush(Color.FromRgb(0xFA, 0xFA, 0xFA)),
        };

        // Clicking the taskbar button restores the window — that's when it needs the
        // panel. Taking it on restore (rather than up front) keeps the flyout usable
        // for the whole recording, since only one window can own the panel.
        _taskbarWindow.StateChanged += (_, _) =>
        {
            if (_taskbarWindow.WindowState == WindowState.Normal)
            {
                MovePanelTo(_taskbarWindow);
                _panel.FocusFirstField();
            }
        };

        // Closing or minimizing the window must NEVER stop the recording — that
        // would make the taskbar's X button a silent "discard my meeting" control.
        // Minimize instead of hide so the taskbar button (and its recording overlay)
        // stays on screen; the tray icon remains the way back in either way.
        _taskbarWindow.Closing += (_, e) =>
        {
            if (_shuttingDown) return;
            e.Cancel = true;
            if (_controller.CurrentState.Kind == CaptureController.StateKind.Streaming)
            {
                _taskbarWindow.WindowState = WindowState.Minimized;
            }
            else
            {
                _taskbarWindow.Hide();
            }
        };
    }

    /// <summary>
    /// Reflect the engine state on the taskbar button: show/hide it, set the icon
    /// overlay (red dot recording, amber pause badge paused), and mirror the same
    /// thing in the progress bar so the state is readable even when the icon is
    /// small — Paused renders yellow, Normal green.
    /// </summary>
    private void SyncTaskbar()
    {
        bool streaming = _controller.CurrentState.Kind == CaptureController.StateKind.Streaming;
        bool paused = streaming && _controller.IsPaused;
        if (_shownStreaming == streaming && _shownPaused == paused) return;
        _shownStreaming = streaming;
        _shownPaused = paused;

        if (streaming)
        {
            _taskbarInfo.Overlay = paused ? _overlayPaused : _overlayRecording;
            // ProgressValue must be non-zero for the wash to render at all; a full
            // bar reads as "a session is running", tinted by ProgressState.
            _taskbarInfo.ProgressValue = 1.0;
            _taskbarInfo.ProgressState = paused ? TaskbarItemProgressState.Paused
                                                : TaskbarItemProgressState.Normal;
            _taskbarInfo.Description = paused ? "LMA Audio Capture — Paused"
                                              : "LMA Audio Capture — Recording";
            _taskbarWindow.Title = _taskbarInfo.Description;
            _taskbarWindow.Icon = _iconRecordingImage;
            _thumbPause.ImageSource = paused ? _thumbResumeImage : _thumbPauseImage;
            _thumbPause.Description = paused ? "Resume recording" : "Pause recording";
            ShowTaskbarButton();
        }
        else
        {
            _taskbarInfo.Overlay = null;
            _taskbarInfo.ProgressState = TaskbarItemProgressState.None;
            _taskbarInfo.ProgressValue = 0;
            _taskbarInfo.Description = "LMA Audio Capture";
            _taskbarWindow.Title = "LMA Audio Capture";
            _taskbarWindow.Icon = _iconIdleImage;
            HideTaskbarButton();
        }
        BuildJumpList();
    }

    /// <summary>
    /// Give the app a taskbar button. A window only gets one once it's been shown,
    /// so start it minimized: the button (with its recording overlay) appears
    /// without stealing focus from the meeting app the user is actually in.
    /// </summary>
    private void ShowTaskbarButton()
    {
        if (_taskbarWindow.IsVisible) return;
        // Shown minimized and initially empty: the button's appearance comes from
        // Window.Icon + TaskbarItemInfo, not from its content, and the panel stays
        // with the flyout (which may be open right now — the user just clicked
        // Start in it). The panel moves over only when this window is restored.
        _taskbarWindow.WindowState = WindowState.Minimized;
        _taskbarWindow.Show();
    }

    /// <summary>Back to tray-only: no taskbar button when nothing is recording.</summary>
    private void HideTaskbarButton()
    {
        if (!_taskbarWindow.IsVisible) return;
        _taskbarWindow.Hide();
        MovePanelTo(_flyout);
    }

    /// <summary>
    /// Move the shared PanelView between the two host windows. WPF gives an element
    /// exactly one logical parent, so the previous host must release it first — the
    /// same constraint PanelView.Row()/Detach() works around when rebuilding rows.
    /// </summary>
    private void MovePanelTo(Window host)
    {
        if (_panelHost == host) return;
        if (_panelHost != null)
        {
            _panelHost.Content = null;
            // An empty flyout would linger as a stray (transparent, topmost) window,
            // so close it out rather than leave a contentless shell on screen.
            if (_panelHost == _flyout) _flyout.Hide();
        }
        host.Content = _panel;
        _panelHost = host;
        _panel.Refresh();
    }

    /// <summary>
    /// Right-click menu on the taskbar button, matching the macOS Dock menu. A
    /// JumpTask can only relaunch the exe with arguments, so each task passes a verb
    /// that the new process forwards to this instance over a named pipe (TrayIpc).
    /// </summary>
    private void BuildJumpList()
    {
        try
        {
            var exe = Environment.ProcessPath;
            if (string.IsNullOrEmpty(exe)) return;

            bool streaming = _controller.CurrentState.Kind == CaptureController.StateKind.Streaming;
            var list = new JumpList { ShowRecentCategory = false, ShowFrequentCategory = false };

            JumpTask Task(string title, string args, string description) => new()
            {
                Title = title,
                ApplicationPath = exe,
                Arguments = args,
                IconResourcePath = exe,
                Description = description,
                CustomCategory = "Recording",
            };

            if (streaming)
            {
                list.JumpItems.Add(_controller.IsPaused
                    ? Task("Resume Recording", TrayIpc.CmdPause, "Resume the paused recording")
                    : Task("Pause Recording", TrayIpc.CmdPause, "Pause the recording"));
                list.JumpItems.Add(Task("Stop Recording", TrayIpc.CmdStop, "Stop and finalize the recording"));
            }
            else
            {
                list.JumpItems.Add(Task("Start Recording", TrayIpc.CmdStart, "Start capturing this meeting"));
            }
            list.JumpItems.Add(Task("Open Control Panel", TrayIpc.CmdPanel, "Show the LMA Audio Capture controls"));

            JumpList.SetJumpList(_app, list);
            list.Apply();
        }
        catch (Exception ex)
        {
            // The shell rejects jump lists in some contexts (no registered file
            // association, policy). Cosmetic — never let it break the tray.
            LogCrash(ex);
        }
    }

    /// <summary>Handle a verb from the thumbnail buttons, jump list, or a forwarded launch.</summary>
    private void HandleCommand(string cmd)
    {
        switch (cmd)
        {
            case TrayIpc.CmdStart:
                if (_controller.IsAuthenticated &&
                    _controller.CurrentState.Kind != CaptureController.StateKind.Streaming)
                {
                    _controller.Start();
                }
                else
                {
                    // Not signed in (or already recording) — show the panel so the
                    // user can see why nothing happened.
                    ShowPopup();
                }
                break;
            case TrayIpc.CmdPause: TogglePause(); break;
            case TrayIpc.CmdStop: StopRecording(); break;
            case TrayIpc.CmdPanel: ShowControlPanel(); break;
        }
    }

    private void TogglePause()
    {
        if (_controller.CurrentState.Kind != CaptureController.StateKind.Streaming) return;
        _controller.SetPaused(!_controller.IsPaused);
        _panel.Refresh();
        SyncTaskbar();
    }

    private void StopRecording()
    {
        if (_controller.CurrentState.Kind == CaptureController.StateKind.Streaming) _controller.Stop();
    }

    /// <summary>
    /// Bring up the controls. While recording the taskbar window already exists, so
    /// restore that (keeping the taskbar button); otherwise use the tray flyout.
    /// </summary>
    private void ShowControlPanel()
    {
        if (_taskbarWindow.IsVisible)
        {
            MovePanelTo(_taskbarWindow);
            _taskbarWindow.WindowState = WindowState.Normal;
            _taskbarWindow.Activate();
            _panel.FocusFirstField();
        }
        else
        {
            ShowPopup();
        }
    }

    private ContextMenu BuildContextMenu()
    {
        var menu = new ContextMenu();
        var open = new MenuItem { Header = "Open LMA Audio Capture" };
        open.Click += (_, _) => ShowControlPanel();
        var quit = new MenuItem { Header = "Quit LMA Audio Capture" };
        quit.Click += (_, _) =>
        {
            // Stop cleanly if streaming, then shut down.
            if (_controller.CurrentState.Kind == CaptureController.StateKind.Streaming) _controller.Stop();
            _shuttingDown = true;   // let the taskbar window actually close
            try { _flyout.Close(); } catch { }
            try { _taskbarWindow.Close(); } catch { }
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
        if (_flyout.IsVisible && _panelHost == _flyout) _flyout.Hide();
        else ShowPopup();
    }

    private void ShowPopup()
    {
        MovePanelTo(_flyout);
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
            Draw(g, size, recording);
        }
        // Bitmap → HICON → managed Icon (cloned so we can free the GDI handle).
        var hicon = bmp.GetHicon();
        using var tmp = System.Drawing.Icon.FromHandle(hicon);
        return (System.Drawing.Icon)tmp.Clone();
    }

    /// <summary>
    /// Draw the LMA glyph into an existing surface at an arbitrary size. Shared by
    /// the tray icon and the taskbar-button image (TaskbarImages) so the two can't
    /// drift apart; tools/make-icon-images.ps1 reproduces the same geometry for the
    /// docs screenshot. Coordinates are authored for 32px and scaled from there.
    /// </summary>
    public static void Draw(System.Drawing.Graphics g, int size, bool recording)
    {
        float k = size / 32f;
        var bg = recording ? System.Drawing.Color.FromArgb(0xD4, 0x2A, 0x2A)
                           : System.Drawing.Color.FromArgb(0x53, 0x5B, 0x66);
        using var bgBrush = new System.Drawing.SolidBrush(bg);
        g.FillEllipse(bgBrush, 1 * k, 1 * k, (size - 2 * k), (size - 2 * k));
        using var fg = new System.Drawing.SolidBrush(System.Drawing.Color.White);
        if (recording)
        {
            g.FillEllipse(fg, size / 2f - 6 * k, size / 2f - 6 * k, 12 * k, 12 * k);
        }
        else
        {
            int[] heights = { 6, 12, 9, 14, 7 };
            float x = 7 * k;
            foreach (var h in heights)
            {
                g.FillRectangle(fg, x, (size - h * k) / 2f, 3 * k, h * k);
                x += 4.5f * k;
            }
        }
    }
}

/// <summary>
/// Best-effort control over whether our tray icon sits on the taskbar (always
/// visible) versus tucked into the overflow (▲) flyout.
///
/// Windows 11 hides newly-seen tray icons in the overflow by default. It records
/// each icon's placement under HKCU\Control Panel\NotifyIconSettings\{hash}, keyed
/// by the icon's ExecutablePath, with an IsPromoted DWORD (1 = shown on the
/// taskbar). We set IsPromoted=1 for our executable so the icon — especially the
/// red recording icon — stays visible. This is undocumented and version-specific,
/// so it's wrapped in try/catch; if it fails, the user can still drag the icon out
/// of the overflow or toggle it in Settings ▸ Personalization ▸ Taskbar.
/// </summary>
internal static class TrayIconVisibility
{
    public static void PromoteToTaskbar()
    {
        try
        {
            var exe = Environment.ProcessPath;
            if (string.IsNullOrEmpty(exe)) return;
            using var root = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(
                @"Control Panel\NotifyIconSettings", writable: true);
            if (root == null) return;

            bool matchedExisting = false;
            foreach (var subName in root.GetSubKeyNames())
            {
                using var sub = root.OpenSubKey(subName, writable: true);
                var path = sub?.GetValue("ExecutablePath") as string;
                if (path != null && string.Equals(path, exe, StringComparison.OrdinalIgnoreCase))
                {
                    sub!.SetValue("IsPromoted", 1, Microsoft.Win32.RegistryValueKind.DWord);
                    matchedExisting = true;
                }
            }

            // If Windows hasn't recorded our icon yet (first run before the shell
            // registers it), seed an entry so the first placement is "promoted".
            if (!matchedExisting)
            {
                // Key name is a shell-computed hash we can't reproduce reliably;
                // seeding a guessable name won't be picked up, so we skip creating
                // one and rely on the re-assert on the next launch/record. This
                // keeps the call side-effect-free when there's nothing to update.
            }
        }
        catch
        {
            // Undocumented/opportunistic — never let it break the tray.
        }
    }
}
