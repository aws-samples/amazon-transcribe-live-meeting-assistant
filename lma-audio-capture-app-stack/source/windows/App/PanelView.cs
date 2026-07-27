using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;

namespace LMA;

/// <summary>
/// The tray popup panel — the WPF equivalent of macOS MenuBarContentView.
/// States (mirrors CaptureController state machine):
///   • Signed out: Email + Password + "Remember my email" + Sign In + error text.
///   • Authenticated, not streaming: Meeting name + Start Recording +
///     "Start automatically at login" + Sign out + "Open LMA meetings".
///   • Streaming: per-channel VU meters, ● Live / ○ Reconnecting…, Pause/Resume,
///     Stop, Mute mic, Mute system, "Open this meeting in LMA".
/// Built in code (no XAML) so Program.Main can be the explicit entry point.
/// </summary>
public sealed class PanelView : Border
{
    private readonly CaptureController _c;

    // Light-theme text colors: a medium-dark gray for secondary text that stays
    // readable on the near-white panel background (LightGray/Gray were too faint).
    private static readonly Brush Secondary = new SolidColorBrush(Color.FromRgb(0x5A, 0x5A, 0x5A));

    // Header
    private readonly System.Windows.Shapes.Ellipse _statusDot = new() { Width = 10, Height = 10, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock _statusText = new() { FontSize = 11, Foreground = Secondary, VerticalAlignment = VerticalAlignment.Center };

    // Signed-out controls
    private readonly TextBox _email = new();
    private readonly PasswordBox _password = new();
    private readonly CheckBox _remember = new() { Content = "Remember my email" };
    private readonly Button _signIn = new() { Content = "Sign In" };
    private readonly TextBlock _error = new() { Foreground = Brushes.Red, TextWrapping = TextWrapping.Wrap, FontSize = 11 };

    // Authenticated / not streaming
    private readonly TextBox _meetingName = new();
    private readonly Button _start = new() { Content = "● Start Recording" };
    private readonly CheckBox _launchAtLogin = new() { Content = "Start automatically at login" };
    private readonly Button _signOut = new() { Content = "Sign out" };

    // Streaming
    private readonly LevelBar _sysBar = new("System");
    private readonly LevelBar _micBar = new("Mic");
    private readonly TextBlock _liveStatus = new() { FontSize = 11 };
    private readonly Button _pause = new() { Content = "Pause" };
    private readonly Button _stop = new() { Content = "Stop" };
    private readonly ToggleButton _muteMic = new() { Content = "Mute mic" };
    private readonly ToggleButton _muteSys = new() { Content = "Mute system" };

    // Shared
    private readonly Button _openLma = new();
    private readonly TextBlock _logLine = new() { Foreground = Secondary, FontSize = 10, TextWrapping = TextWrapping.Wrap };

    // Settings gear (speaker labels + mic picker)
    private bool _showSettings;
    private readonly Button _gear = new()
    {
        Content = "⚙", FontSize = 14, Padding = new Thickness(4, 0, 4, 0),
        Background = Brushes.Transparent, BorderThickness = new Thickness(0),
        VerticalAlignment = VerticalAlignment.Center, Cursor = System.Windows.Input.Cursors.Hand,
    };
    private readonly TextBox _micLabel = new();
    private readonly TextBox _systemLabel = new();
    private readonly ComboBox _micPicker = new() { FontSize = 11, Margin = new Thickness(0, 0, 0, 6) };
    // Grey placeholder showing what a blank field will actually be sent as, so the
    // defaults are visible without having to hover a tooltip.
    private readonly TextBlock _micHint = HintText();
    private readonly TextBlock _systemHint = HintText();
    private readonly Grid _micLabelBox;
    private readonly Grid _systemLabelBox;
    // RefreshMicPicker rebuilds the list, and setting SelectedItem fires
    // SelectionChanged — which would persist a transient "System Default" over the
    // saved device before the matching item is found.
    private bool _loadingMicPicker;

    private readonly StackPanel _body = new();

    public Action? RequestClosePopup;

    public PanelView(CaptureController controller)
    {
        _c = controller;

        Width = 300;
        // Light theme: dark text on a light background for readability. A near-
        // white panel with a subtle border, near-black default text.
        Background = new SolidColorBrush(Color.FromRgb(0xFA, 0xFA, 0xFA));
        BorderBrush = new SolidColorBrush(Color.FromRgb(0xC8, 0xC8, 0xC8));
        BorderThickness = new Thickness(1);
        CornerRadius = new CornerRadius(8);
        Padding = new Thickness(14);
        System.Windows.Documents.TextElement.SetForeground(this, new SolidColorBrush(Color.FromRgb(0x1A, 0x1A, 0x1A)));

        var root = new StackPanel();

        // Header row: recording dot + title + status text.
        var header = new DockPanel { LastChildFill = false, Margin = new Thickness(0, 0, 0, 8) };
        var left = new StackPanel { Orientation = Orientation.Horizontal };
        left.Children.Add(_statusDot);
        left.Children.Add(new TextBlock
        {
            Text = "  LMA Audio Capture",
            FontWeight = FontWeights.Bold,
            VerticalAlignment = VerticalAlignment.Center,
        });
        DockPanel.SetDock(left, Dock.Left);
        DockPanel.SetDock(_gear, Dock.Right);
        DockPanel.SetDock(_statusText, Dock.Right);
        header.Children.Add(left);
        header.Children.Add(_gear);
        header.Children.Add(_statusText);
        root.Children.Add(header);
        root.Children.Add(Sep());
        root.Children.Add(_body);
        root.Children.Add(_logLine);

        Child = root;

        // Wire controls.
        _signIn.Click += (_, _) => DoLogin();
        _signOut.Click += (_, _) => { _c.Logout(); _password.Clear(); if (!_remember.IsChecked!.Value) _email.Clear(); Refresh(); };
        _start.Click += (_, _) => DoStart();
        _stop.Click += (_, _) => _c.Stop();
        _pause.Click += (_, _) => { _c.SetPaused(!_c.IsPaused); _pause.Content = _c.IsPaused ? "Resume" : "Pause"; };
        _muteMic.Click += (_, _) => _c.SetMicMuted(_muteMic.IsChecked ?? false);
        _muteSys.Click += (_, _) => _c.SetMeetingMuted(_muteSys.IsChecked ?? false);
        _openLma.Click += (_, _) => OpenLma();
        _remember.Click += (_, _) => AppSettings.PersistLoginPreference(_remember.IsChecked ?? false, _email.Text);
        _launchAtLogin.Click += (_, _) =>
        {
            try { AppSettings.LaunchAtLogin = _launchAtLogin.IsChecked ?? false; }
            catch (Exception ex) { OnLog($"Couldn't change Start-at-login: {ex.Message}"); }
        };

        StyleBox(_email); StyleBox(_meetingName); StyleBox(_micLabel); StyleBox(_systemLabel);
        _password.Margin = _email.Margin = _meetingName.Margin = new Thickness(0, 0, 0, 6);
        _password.Padding = new Thickness(4);

        // The two label fields live inside a placeholder wrapper, which owns the
        // bottom margin instead of the TextBox (the grey hint has to sit over the
        // field itself, not over the gap below it).
        _micLabel.Margin = _systemLabel.Margin = new Thickness(0);
        _micLabelBox = PlaceholderBox(_micLabel, _micHint);
        _systemLabelBox = PlaceholderBox(_systemLabel, _systemHint);

        // Settings gear: toggle the section; save on every edit (no Apply).
        // Disabled while streaming — labels ride the START frame, so mid-meeting
        // changes wouldn't take effect anyway.
        _gear.Click += (_, _) =>
        {
            _showSettings = !_showSettings;
            if (_showSettings) RefreshMicPicker();
            Refresh();
        };
        _micLabel.TextChanged += (_, _) => { AppSettings.MicLabel = _micLabel.Text; UpdateLabelHints(); };
        _systemLabel.TextChanged += (_, _) => { AppSettings.SystemLabel = _systemLabel.Text; UpdateLabelHints(); };
        // Typing in the email field changes the mic-label default, so keep the grey
        // hint honest while the user is still signing in.
        _email.TextChanged += (_, _) => UpdateLabelHints();
        _micPicker.SelectionChanged += (_, _) =>
        {
            // Ignore the churn from rebuilding the list — only persist real picks,
            // or an unplugged device would silently erase the saved selection.
            if (_loadingMicPicker) return;
            if (_micPicker.SelectedItem is ComboBoxItem it)
                AppSettings.MicDeviceId = (it.Tag as string) ?? "";
        };
    }

    public void InitFromSettings()
    {
        _remember.IsChecked = AppSettings.RememberLogin;
        _email.Text = AppSettings.RememberLogin ? AppSettings.SavedUsername : _c.Config.Username;
        _micLabel.Text = AppSettings.MicLabel;
        _systemLabel.Text = AppSettings.SystemLabel;
        UpdateLabelHints();
        PushSettingsToController();
        Refresh();
    }

    /// <summary>
    /// Apply persisted settings to the controller. The effective mic label
    /// falls back to the signed-in email (the requested default), then the
    /// config's AgentId ("Me"); the system label falls back to
    /// "Other participants".
    /// </summary>
    public void PushSettingsToController()
    {
        var mic = AppSettings.MicLabel;
        if (string.IsNullOrEmpty(mic)) mic = CurrentEmail();
        if (string.IsNullOrEmpty(mic)) mic = _c.Config.AgentId;
        _c.MicLabel = mic;
        var sys = AppSettings.SystemLabel;
        _c.SystemLabel = string.IsNullOrEmpty(sys) ? AppSettings.DefaultSystemLabel : sys;
        _c.MicDeviceId = AppSettings.MicDeviceId;
    }

    private string CurrentEmail()
    {
        if (!string.IsNullOrEmpty(_email.Text)) return _email.Text;
        return AppSettings.RememberLogin ? AppSettings.SavedUsername : _c.Config.Username;
    }

    /// <summary>The label that will actually be sent for the mic channel if the field is blank.</summary>
    private string DefaultMicLabel()
    {
        var email = CurrentEmail();
        return !string.IsNullOrEmpty(email) ? email : _c.Config.AgentId;
    }

    /// <summary>
    /// Show/hide the grey placeholder over each label field. A blank field means
    /// "use the default", so spell that default out in the field itself rather than
    /// leaving the user to guess (or hover a tooltip).
    /// </summary>
    private void UpdateLabelHints()
    {
        _micHint.Text = DefaultMicLabel();
        _micHint.Visibility = string.IsNullOrEmpty(_micLabel.Text) ? Visibility.Visible : Visibility.Collapsed;
        _systemHint.Text = AppSettings.DefaultSystemLabel;
        _systemHint.Visibility = string.IsNullOrEmpty(_systemLabel.Text) ? Visibility.Visible : Visibility.Collapsed;
    }

    /// <summary>Repopulate the mic dropdown from live device enumeration (hotplug-friendly).</summary>
    private void RefreshMicPicker()
    {
        _loadingMicPicker = true;
        try
        {
            _micPicker.Items.Clear();
            var selected = AppSettings.MicDeviceId;
            var def = new ComboBoxItem { Content = "System Default", Tag = "" };
            _micPicker.Items.Add(def);
            _micPicker.SelectedItem = def;
            foreach (var (id, name) in AudioCapture.ListMicDevices())
            {
                var item = new ComboBoxItem { Content = name, Tag = id };
                _micPicker.Items.Add(item);
                if (id == selected) _micPicker.SelectedItem = item;
            }
            // Saved device unplugged → selection shows System Default, matching the
            // engine's fallback at capture start. The saved ID is deliberately kept,
            // so re-plugging the device restores the choice.
        }
        finally { _loadingMicPicker = false; }
    }

    public void FocusFirstField()
    {
        if (_c.IsAuthenticated) return;
        // Defer to Input priority so focus lands after the window has activated
        // and the field is laid out; set both logical and keyboard focus.
        Dispatcher.BeginInvoke(
            System.Windows.Threading.DispatcherPriority.Input,
            new Action(() =>
            {
                _email.Focus();
                System.Windows.Input.Keyboard.Focus(_email);
            }));
    }

    // MARK: - Engine callbacks (already marshaled onto the UI thread by TrayApp)

    public void OnState(CaptureController.State s)
    {
        _statusText.Text = s.Kind switch
        {
            CaptureController.StateKind.Idle => "Not signed in",
            CaptureController.StateKind.SigningIn => "Signing in…",
            CaptureController.StateKind.Authenticated => "Ready",
            CaptureController.StateKind.Starting => "Starting…",
            CaptureController.StateKind.Streaming => "Recording",
            CaptureController.StateKind.Stopping => "Stopping…",
            CaptureController.StateKind.Error => "Error",
            _ => "",
        };
        bool streaming = s.Kind == CaptureController.StateKind.Streaming;
        _statusDot.Fill = streaming ? Brushes.Red : Brushes.Gray;
        if (s.Kind == CaptureController.StateKind.Error && !string.IsNullOrEmpty(s.Message))
            _error.Text = s.Message;
        Refresh();
    }

    public void OnLevels(float meeting, float mic, bool connected, bool paused)
    {
        _sysBar.SetLevel(meeting, _c.IsMeetingMuted);
        _micBar.SetLevel(mic, _c.IsMicMuted);
        _liveStatus.Text = (connected ? "● Live" : "○ Reconnecting…") + (paused ? " · Paused" : "");
        _liveStatus.Foreground = connected
            ? new SolidColorBrush(Color.FromRgb(0x1B, 0x8A, 0x2F))   // readable green on light bg
            : new SolidColorBrush(Color.FromRgb(0xC0, 0x56, 0x00));  // readable orange on light bg
    }

    public void OnLog(string msg) => _logLine.Text = msg;

    // MARK: - Rebuild the body for the current state

    public void Refresh()
    {
        _body.Children.Clear();

        // Settings gear is only actionable between recordings.
        bool isStreaming = _c.CurrentState.Kind == CaptureController.StateKind.Streaming;
        _gear.IsEnabled = !isStreaming;
        _gear.ToolTip = isStreaming ? "Stop recording to change settings" : "Settings";
        _gear.Foreground = _showSettings ? Brushes.SteelBlue : Secondary;

        if (_showSettings && !isStreaming)
        {
            _body.Children.Add(Label("Speaker labels — shown in the LMA transcript"));
            // Blank field = use the default, shown as grey placeholder text in the
            // field (and echoed in the tooltip for screen readers / narrow panels).
            UpdateLabelHints();
            _body.Children.Add(Label("My mic"));
            _micLabel.ToolTip = $"Default: {DefaultMicLabel()}";
            _body.Children.Add(_micLabelBox);
            _body.Children.Add(Label("System audio"));
            _systemLabel.ToolTip = $"Default: {AppSettings.DefaultSystemLabel}";
            _body.Children.Add(_systemLabelBox);
            _body.Children.Add(Label("Microphone"));
            _body.Children.Add(_micPicker);
            _body.Children.Add(new TextBlock
            {
                Text = "Greyed text is the default that will be used if you leave a label blank. " +
                       "System Default follows Windows' input device; if a chosen mic is unplugged, " +
                       "recording falls back to the default.",
                Foreground = Secondary, FontSize = 10, TextWrapping = TextWrapping.Wrap,
                Margin = new Thickness(0, 0, 0, 4),
            });
            _body.Children.Add(Sep());
        }

        if (!_c.IsAuthenticated)
        {
            _body.Children.Add(Label("Email"));
            _body.Children.Add(_email);
            _body.Children.Add(Label("Password"));
            _body.Children.Add(_password);
            _body.Children.Add(_remember);
            _signIn.Margin = new Thickness(0, 8, 0, 0);
            _signIn.IsEnabled = _c.CurrentState.Kind != CaptureController.StateKind.SigningIn;
            _body.Children.Add(_signIn);
            if (!string.IsNullOrEmpty(_error.Text)) _body.Children.Add(_error);
        }
        else
        {
            bool streaming = _c.CurrentState.Kind == CaptureController.StateKind.Streaming;
            if (!streaming)
            {
                _body.Children.Add(Label("Meeting name (optional)"));
                _body.Children.Add(_meetingName);
                _start.Margin = new Thickness(0, 4, 0, 8);
                _body.Children.Add(_start);
            }
            else
            {
                _body.Children.Add(_sysBar);
                _body.Children.Add(_micBar);
                _liveStatus.Margin = new Thickness(0, 4, 0, 4);
                _body.Children.Add(_liveStatus);
                _pause.Content = _c.IsPaused ? "Resume" : "Pause";
                _body.Children.Add(Row(_pause, _stop));
                _muteMic.IsChecked = _c.IsMicMuted;
                _muteSys.IsChecked = _c.IsMeetingMuted;
                _body.Children.Add(Row(_muteMic, _muteSys));
            }

            _body.Children.Add(Sep());
            _openLma.Content = streaming ? "Open this meeting in LMA" : "Open LMA meetings";
            _body.Children.Add(_openLma);

            if (!streaming)
            {
                try { _launchAtLogin.IsChecked = AppSettings.LaunchAtLogin; } catch { }
                _launchAtLogin.Margin = new Thickness(0, 6, 0, 0);
                _body.Children.Add(_launchAtLogin);
                _body.Children.Add(_signOut);
            }
        }

        _body.Children.Add(Sep());
        // While recording, point at the taskbar button: it's the always-visible way
        // back to these controls (and to Stop) if the tray icon ends up in the ▲
        // overflow. Closing that window doesn't stop the recording, so say so.
        bool recording = _c.CurrentState.Kind == CaptureController.StateKind.Streaming;
        _body.Children.Add(new TextBlock
        {
            Text = recording
                ? "Hover the taskbar button for Pause/Stop, or right-click it for quick actions. Closing that window keeps recording."
                : "Right-click the tray icon to Quit. Leave it running in the background between meetings.",
            Foreground = Secondary, FontSize = 10, TextWrapping = TextWrapping.Wrap,
        });
    }

    // MARK: - Actions

    private void DoLogin()
    {
        _error.Text = "";
        AppSettings.PersistLoginPreference(_remember.IsChecked ?? false, _email.Text);
        var u = _email.Text;
        var p = _password.Password;
        if (string.IsNullOrEmpty(u) || string.IsNullOrEmpty(p)) return;
        _ = _c.LoginAsync(u, p).ContinueWith(_ =>
            Dispatcher.Invoke(() => { _password.Clear(); Refresh(); }));
    }

    private void DoStart()
    {
        // Push current settings (the mic label may depend on the signed-in email).
        PushSettingsToController();
        var name = string.IsNullOrEmpty(_meetingName.Text)
            ? null
            : $"{_meetingName.Text} - {DateTime.Now:yyyy-MM-dd HH:mm}";
        _c.Start(name);
    }

    private void OpenLma()
    {
        var url = _c.LmaUrl();
        if (!string.IsNullOrEmpty(url))
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(url) { UseShellExecute = true });
    }

    // MARK: - Small view helpers

    private static void StyleBox(TextBox t)
    {
        t.Padding = new Thickness(4);
        t.Margin = new Thickness(0, 0, 0, 6);
    }

    /// <summary>The grey "this is what you'll get if you leave it blank" overlay text.</summary>
    private static TextBlock HintText() => new()
    {
        FontSize = 12,
        // Lighter than Secondary so it reads as a placeholder rather than as a
        // value the user typed.
        Foreground = new SolidColorBrush(Color.FromRgb(0x8C, 0x8C, 0x8C)),
        // Line up with the TextBox's own 4px padding + border so the hint sits
        // exactly where the caret/typed text will.
        Margin = new Thickness(6, 0, 6, 0),
        VerticalAlignment = VerticalAlignment.Center,
        IsHitTestVisible = false,   // clicks fall through to the TextBox underneath
        TextTrimming = TextTrimming.CharacterEllipsis,
    };

    /// <summary>
    /// Stack a grey placeholder behind a TextBox. WPF has no built-in placeholder,
    /// and a watermark written into Text would get saved as a real value — so the
    /// hint is a separate, non-hit-testable TextBlock shown only while empty.
    /// </summary>
    private static Grid PlaceholderBox(TextBox box, TextBlock hint)
    {
        var g = new Grid { Margin = new Thickness(0, 0, 0, 6) };
        g.Children.Add(box);
        g.Children.Add(hint);
        return g;
    }

    private static TextBlock Label(string s) => new()
    {
        Text = s, FontSize = 11, Foreground = Secondary, Margin = new Thickness(0, 2, 0, 2),
    };

    private static Border Sep() => new()
    {
        Height = 1, Background = new SolidColorBrush(Color.FromRgb(0xDD, 0xDD, 0xDD)),
        Margin = new Thickness(0, 6, 0, 6),
    };

    private static Grid Row(UIElement a, UIElement b)
    {
        var g = new Grid { Margin = new Thickness(0, 2, 0, 2) };
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        g.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        FrameworkElement fa = (FrameworkElement)a, fb = (FrameworkElement)b;
        fa.Margin = new Thickness(0, 0, 3, 0);
        fb.Margin = new Thickness(3, 0, 0, 0);
        // These are persistent controls reused across Refresh() rebuilds. After
        // _body.Children.Clear(), the previous Row grid is orphaned but still owns
        // them as logical children, so re-adding here would throw "already the
        // logical child of another element". Detach from any prior parent first.
        Detach(a); Detach(b);
        Grid.SetColumn(a, 0); Grid.SetColumn(b, 1);
        g.Children.Add(a); g.Children.Add(b);
        return g;
    }

    /// <summary>Remove an element from its current Panel parent, if any.</summary>
    private static void Detach(UIElement e)
    {
        if (e is FrameworkElement fe && fe.Parent is Panel p)
        {
            p.Children.Remove(e);
        }
    }
}

/// <summary>A labeled horizontal VU bar (×3 gain, matching the CLI meter).</summary>
public sealed class LevelBar : Grid
{
    private readonly System.Windows.Shapes.Rectangle _fill = new() { HorizontalAlignment = HorizontalAlignment.Left };
    private readonly Border _track;

    public LevelBar(string label)
    {
        Margin = new Thickness(0, 3, 0, 3);
        ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(48) });
        ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

        var lbl = new TextBlock { Text = label, FontSize = 11, Foreground = new SolidColorBrush(Color.FromRgb(0x5A, 0x5A, 0x5A)), VerticalAlignment = VerticalAlignment.Center };
        Grid.SetColumn(lbl, 0);
        Children.Add(lbl);

        // Readable green fill on a light gray track (the old translucent-white
        // track was invisible on the light panel background).
        _fill.Fill = new SolidColorBrush(Color.FromRgb(0x1B, 0x8A, 0x2F));
        _fill.Height = 8;
        _fill.RadiusX = 3; _fill.RadiusY = 3;
        _track = new Border
        {
            Height = 8,
            Background = new SolidColorBrush(Color.FromRgb(0xE0, 0xE0, 0xE0)),
            CornerRadius = new CornerRadius(3),
            Child = _fill,
            VerticalAlignment = VerticalAlignment.Center,
        };
        Grid.SetColumn(_track, 1);
        Children.Add(_track);
    }

    public void SetLevel(float level, bool muted)
    {
        double frac = Math.Min(1.0, Math.Max(0.0, level * 3.0)); // ×3 gain, matches macOS
        _fill.Fill = muted ? Brushes.Gray : new SolidColorBrush(Color.FromRgb(0x1B, 0x8A, 0x2F));
        double w = _track.ActualWidth > 0 ? _track.ActualWidth : 230;
        _fill.Width = w * frac;
    }
}
