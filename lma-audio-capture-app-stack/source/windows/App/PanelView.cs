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

    // Header
    private readonly System.Windows.Shapes.Ellipse _statusDot = new() { Width = 10, Height = 10, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock _statusText = new() { FontSize = 11, Foreground = Brushes.Gray, VerticalAlignment = VerticalAlignment.Center };

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
    private readonly TextBlock _logLine = new() { Foreground = Brushes.Gray, FontSize = 10, TextWrapping = TextWrapping.Wrap };

    private readonly StackPanel _body = new();

    public Action? RequestClosePopup;

    public PanelView(CaptureController controller)
    {
        _c = controller;

        Width = 300;
        Background = new SolidColorBrush(Color.FromRgb(0x2B, 0x2B, 0x2B));
        BorderBrush = new SolidColorBrush(Color.FromRgb(0x11, 0x11, 0x11));
        BorderThickness = new Thickness(1);
        CornerRadius = new CornerRadius(8);
        Padding = new Thickness(14);
        System.Windows.Documents.TextElement.SetForeground(this, Brushes.White);

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
        DockPanel.SetDock(_statusText, Dock.Right);
        header.Children.Add(left);
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

        StyleBox(_email); StyleBox(_meetingName);
        _password.Margin = _email.Margin = _meetingName.Margin = new Thickness(0, 0, 0, 6);
        _password.Padding = new Thickness(4);
    }

    public void InitFromSettings()
    {
        _remember.IsChecked = AppSettings.RememberLogin;
        _email.Text = AppSettings.RememberLogin ? AppSettings.SavedUsername : _c.Config.Username;
        Refresh();
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
        _liveStatus.Foreground = connected ? Brushes.LightGreen : Brushes.Orange;
    }

    public void OnLog(string msg) => _logLine.Text = msg;

    // MARK: - Rebuild the body for the current state

    public void Refresh()
    {
        _body.Children.Clear();

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
        _body.Children.Add(new TextBlock
        {
            Text = "Right-click the tray icon to Quit. Leave it running in the background between meetings.",
            Foreground = Brushes.Gray, FontSize = 10, TextWrapping = TextWrapping.Wrap,
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

    private static TextBlock Label(string s) => new()
    {
        Text = s, FontSize = 11, Foreground = Brushes.LightGray, Margin = new Thickness(0, 2, 0, 2),
    };

    private static Border Sep() => new()
    {
        Height = 1, Background = new SolidColorBrush(Color.FromRgb(0x44, 0x44, 0x44)),
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
        Grid.SetColumn(a, 0); Grid.SetColumn(b, 1);
        g.Children.Add(a); g.Children.Add(b);
        return g;
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

        var lbl = new TextBlock { Text = label, FontSize = 11, Foreground = Brushes.LightGray, VerticalAlignment = VerticalAlignment.Center };
        Grid.SetColumn(lbl, 0);
        Children.Add(lbl);

        _fill.Fill = Brushes.LimeGreen;
        _fill.Height = 8;
        _fill.RadiusX = 3; _fill.RadiusY = 3;
        _track = new Border
        {
            Height = 8,
            Background = new SolidColorBrush(Color.FromArgb(0x33, 0xFF, 0xFF, 0xFF)),
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
        _fill.Fill = muted ? Brushes.Gray : Brushes.LimeGreen;
        double w = _track.ActualWidth > 0 ? _track.ActualWidth : 230;
        _fill.Width = w * frac;
    }
}
