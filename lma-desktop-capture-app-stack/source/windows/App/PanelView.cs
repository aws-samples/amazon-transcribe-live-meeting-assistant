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

    // Settings gear — opens the standalone Settings window.
    private readonly Button _gear = new()
    {
        Content = "⚙", FontSize = 14, Padding = new Thickness(4, 0, 4, 0),
        Background = Brushes.Transparent, BorderThickness = new Thickness(0),
        VerticalAlignment = VerticalAlignment.Center, Cursor = System.Windows.Input.Cursors.Hand,
    };
    private readonly TextBox _micLabel = new();
    private readonly TextBox _systemLabel = new();
    private readonly ComboBox _micPicker = new() { FontSize = 11, Margin = new Thickness(0, 0, 0, 6) };
    // Optional desktop-video capture.
    private readonly CheckBox _videoEnabled = new() { Content = "Also record screen video", FontSize = 11 };
    private readonly ComboBox _videoPicker = new() { FontSize = 11, Margin = new Thickness(0, 0, 0, 6) };
    // Holds the source picker + help text so toggling "Also record screen video"
    // can show/hide them without rebuilding the whole settings window.
    private readonly StackPanel _videoSourceHost = new();
    private bool _loadingVideoPicker;

    // Elapsed recording time (UX: see at a glance how long you've been recording).
    private readonly System.Windows.Threading.DispatcherTimer _elapsedTimer =
        new() { Interval = TimeSpan.FromSeconds(1) };
    private DateTime? _recordingStartedAt;
    private readonly TextBlock _elapsedText = new()
    {
        FontSize = 11,
        Foreground = Secondary,
        Margin = new Thickness(6, 0, 0, 0),
        VerticalAlignment = VerticalAlignment.Center,
    };

    /// <summary>Elapsed recording time as "7:12" / "1:03:44" ("" when idle).</summary>
    public string ElapsedText
    {
        get
        {
            if (_recordingStartedAt is not DateTime start) return "";
            var span = DateTime.Now - start;
            return span.TotalHours >= 1
                ? $"{(int)span.TotalHours}:{span.Minutes:D2}:{span.Seconds:D2}"
                : $"{span.Minutes}:{span.Seconds:D2}";
        }
    }

    /// <summary>Opens the standalone Settings window (wired by TrayApp).</summary>
    public Action? OpenSettingsWindow;

    /// <summary>Recent meeting names (most recent first), persisted per stack.</summary>
    private readonly Button _recentMeetings = new()
    {
        Content = "🕘",
        FontSize = 11,
        Padding = new Thickness(4, 0, 4, 0),
        Margin = new Thickness(4, 0, 0, 0),
        ToolTip = "Recent meeting names",
    };
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

    // Rows that CONTAIN long-lived controls must themselves be long-lived.
    // Refresh() calls _body.Children.Clear(), which only detaches the row — the
    // controls inside remain that row's logical children, so building a fresh row
    // each Refresh and re-adding them would throw ("already the logical child of
    // another element"). Building the rows once and re-adding the ROW is safe.
    private readonly DockPanel _meetingNameRow = new() { LastChildFill = true, Margin = new Thickness(0, 0, 0, 2) };
    private readonly StackPanel _liveRow = new() { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 2) };

    public Action? RequestClosePopup;

    public PanelView(CaptureController controller)
    {
        _c = controller;

        // Minimum width, not a fixed one, so the window can be resized wider and
        // long text (labels, stack names, error messages) has room to wrap.
        MinWidth = 300;
        // Light theme: dark text on a light background for readability. A near-
        // white panel with a subtle border, near-black default text.
        Background = new SolidColorBrush(Color.FromRgb(0xFA, 0xFA, 0xFA));
        BorderBrush = new SolidColorBrush(Color.FromRgb(0xC8, 0xC8, 0xC8));
        BorderThickness = new Thickness(1);
        CornerRadius = new CornerRadius(8);
        Padding = new Thickness(14);
        System.Windows.Documents.TextElement.SetForeground(this, new SolidColorBrush(Color.FromRgb(0x1A, 0x1A, 0x1A)));

        var root = new StackPanel();

        // Header row: recording dot + title (+ stack) + status text.
        var header = new DockPanel { LastChildFill = false, Margin = new Thickness(0, 0, 0, 8) };
        var left = new StackPanel { Orientation = Orientation.Horizontal };
        left.Children.Add(_statusDot);
        var titleStack = new StackPanel { Margin = new Thickness(6, 0, 0, 0) };
        titleStack.Children.Add(new TextBlock
        {
            Text = "LMA Capture",
            FontWeight = FontWeights.Bold,
            VerticalAlignment = VerticalAlignment.Center,
        });
        // Which LMA deployment this app talks to — essential once a user has the
        // clients for more than one stack installed.
        if (!string.IsNullOrEmpty(AppIdentity.StackName))
        {
            titleStack.Children.Add(new TextBlock
            {
                Text = AppIdentity.StackName,
                Foreground = Secondary,
                FontSize = 10,
                TextTrimming = TextTrimming.CharacterEllipsis,
                MaxWidth = 170,
            });
        }
        left.Children.Add(titleStack);
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
        // About line: version + stack, so support questions ("which build/which
        // deployment?") are answerable at a glance.
        root.Children.Add(new TextBlock
        {
            Text = AppIdentity.AboutLine,
            Foreground = Secondary,
            FontSize = 10,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 6, 0, 0),
        });

        // Wrap in a ScrollViewer: the body's height varies with state (consent
        // gate, expanded sections, error text). Without this the content was
        // clipped at the top/bottom of a fixed window with no way to reach it.
        // TrayApp sets MaxHeight from the screen work area.
        Child = new ScrollViewer
        {
            Content = root,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            Padding = new Thickness(0),
        };

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

        // Assemble the persistent rows once (see the field comment).
        DockPanel.SetDock(_recentMeetings, Dock.Right);
        _meetingNameRow.Children.Add(_recentMeetings);
        _meetingNameRow.Children.Add(_meetingName);
        _liveRow.Children.Add(_liveStatus);
        _liveRow.Children.Add(_elapsedText);

        // Settings gear: toggle the section; save on every edit (no Apply).
        // Disabled while streaming — labels ride the START frame, so mid-meeting
        // changes wouldn't take effect anyway.
        // Settings live in their OWN window: the popup stays small and
        // predictable (expanding it inline used to overflow the screen), and
        // settings get room for the growing set of options.
        _gear.Click += (_, _) => OpenSettingsWindow?.Invoke();
        _recentMeetings.Click += (_, _) => ShowRecentMeetingsMenu();
        _elapsedTimer.Tick += (_, _) => _elapsedText.Text = ElapsedText;
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
        _videoEnabled.Click += (_, _) =>
        {
            AppSettings.VideoEnabled = _videoEnabled.IsChecked ?? false;
            PushSettingsToController();
            // Show/hide the source picker in place (settings live in their own
            // window now, so there's no panel rebuild to piggyback on).
            SyncVideoSourceHost();
        };
        _videoPicker.SelectionChanged += (_, _) =>
        {
            if (_loadingVideoPicker) return;
            if (_videoPicker.SelectedItem is ComboBoxItem it)
                AppSettings.VideoSourceId = (it.Tag as string) ?? "";
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
        _c.VideoEnabled = AppSettings.VideoEnabled;
        _c.VideoSourceId = AppSettings.VideoSourceId;
    }

    /// <summary>Repopulate the video source dropdown (displays + titled windows).</summary>
    private void RefreshVideoPicker()
    {
        _loadingVideoPicker = true;
        try
        {
            _videoPicker.Items.Clear();
            var selected = AppSettings.VideoSourceId;
            var def = new ComboBoxItem { Content = "Entire screen", Tag = "" };
            _videoPicker.Items.Add(def);
            _videoPicker.SelectedItem = def;
            foreach (var src in VideoCapture.ListSources())
            {
                var item = new ComboBoxItem { Content = src.Name, Tag = src.Id };
                _videoPicker.Items.Add(item);
                if (src.Id == selected) _videoPicker.SelectedItem = item;
            }
            // Saved source gone (window closed) → selection shows Entire screen,
            // matching the engine's fallback at capture start.
        }
        finally { _loadingVideoPicker = false; }
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

        // Elapsed-time tracking + start/stop notifications on the transitions.
        if (streaming && _recordingStartedAt == null)
        {
            _recordingStartedAt = DateTime.Now;
            _elapsedText.Text = ElapsedText;
            _elapsedTimer.Start();
            Notifier.Notify("Recording started",
                AppSettings.VideoEnabled
                    ? "LMA is recording this meeting's audio and screen video."
                    : "LMA is recording this meeting's audio.");
        }
        else if (!streaming && _recordingStartedAt != null)
        {
            var duration = ElapsedText;
            _elapsedTimer.Stop();
            _recordingStartedAt = null;
            _elapsedText.Text = "";
            // The recording lands in LMA shortly after END (the server uploads
            // and muxes at call end), so point at it rather than claiming it's
            // ready this instant.
            Notifier.Notify($"Recording stopped ({duration})",
                "Your meeting is being processed. Open it in LMA when ready.",
                _c.LmaUrl());
        }
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

        // Settings gear is only actionable between recordings (labels ride the
        // START frame, so mid-meeting changes wouldn't take effect anyway).
        bool isStreaming = _c.CurrentState.Kind == CaptureController.StateKind.Streaming;
        _gear.IsEnabled = !isStreaming;
        _gear.ToolTip = isStreaming ? "Stop recording to change settings" : "Settings…";
        _gear.Foreground = Secondary;

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
            if (_showDisclaimer && !streaming)
            {
                // One-time recording-consent gate (same text and Agree/Cancel shape
                // as the browser extension's popup). Rendered in place of the Start
                // controls so it can't be missed or clicked past.
                _body.Children.Add(BuildDisclaimerBlock());
            }
            else if (!streaming)
            {
                _body.Children.Add(Label("Meeting name (optional)"));
                _recentMeetings.Visibility = AppSettings.RecentMeetingNames.Count > 0
                    ? Visibility.Visible : Visibility.Collapsed;
                _body.Children.Add(_meetingNameRow);
                _start.Margin = new Thickness(0, 4, 0, 4);
                _body.Children.Add(_start);
                // Persistent consent reminder: the full disclaimer is a one-time
                // gate, but the obligation is per-meeting — keep a one-liner next
                // to Start. Hover shows the consent record (when + the exact text
                // agreed to); the full record also lives in Settings (⚙).
                var agreedAt = AppSettings.DisclaimerAgreedAt;
                var agreedText = AppSettings.DisclaimerAgreedText;
                var tooltip = string.IsNullOrEmpty(agreedText) ? EffectiveDisclaimer : agreedText;
                if (agreedAt is DateTime at)
                    tooltip = $"You agreed to this on {at:g}:\n\n{tooltip}";
                _body.Children.Add(new TextBlock
                {
                    Text = "✓ Ensure all participants have consented to recording.",
                    Foreground = Secondary,
                    FontSize = 10,
                    TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(0, 0, 0, 8),
                    ToolTip = tooltip,
                });
            }
            else
            {
                _body.Children.Add(_sysBar);
                _body.Children.Add(_micBar);
                _liveStatus.Margin = new Thickness(0, 4, 0, 4);
                _elapsedText.Text = ElapsedText;
                _body.Children.Add(_liveRow);
                if (_c.IsVideoActive)
                {
                    _body.Children.Add(new TextBlock
                    {
                        Text = "🎥 Screen video",
                        Foreground = Secondary, FontSize = 10,
                        Margin = new Thickness(0, 0, 0, 4),
                    });
                }
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

    // The settings content is built ONCE and reused: it contains long-lived
    // controls (label boxes, pickers), and rebuilding the container on every
    // Settings-window open would try to re-parent controls that still belong to
    // the previous container — WPF throws on that.
    private StackPanel? _settingsContent;

    /// <summary>
    /// The settings controls, for hosting in the standalone Settings window.
    /// These are the SAME control instances the panel owns (WPF allows exactly
    /// one parent, so they live in one place — the settings window). Cached, so
    /// closing and reopening the window re-hosts the same panel.
    /// </summary>
    public UIElement BuildSettingsContent()
    {
        RefreshMicPicker();
        if (_settingsContent != null)
        {
            // Refresh the dynamic bits, then hand back the existing panel. It
            // must be detached from the closed window first, or adding it to the
            // new window throws.
            UpdateLabelHints();
            _videoEnabled.IsChecked = AppSettings.VideoEnabled;
            SyncVideoSourceHost();
            if (_settingsContent.Parent is ScrollViewer sv) sv.Content = null;
            else if (_settingsContent.Parent is Panel parent) parent.Children.Remove(_settingsContent);
            return _settingsContent;
        }
        var v = new StackPanel();
        v.Children.Add(Label("Speaker labels — shown in the LMA transcript"));
        // Blank field = use the default, shown as grey placeholder text in the
        // field (and echoed in the tooltip for screen readers / narrow panels).
        UpdateLabelHints();
        v.Children.Add(Label("My mic"));
        _micLabel.ToolTip = $"Default: {DefaultMicLabel()}";
        v.Children.Add(_micLabelBox);
        v.Children.Add(Label("System audio"));
        _systemLabel.ToolTip = $"Default: {AppSettings.DefaultSystemLabel}";
        v.Children.Add(_systemLabelBox);
        v.Children.Add(Label("Microphone"));
        v.Children.Add(_micPicker);
        v.Children.Add(new TextBlock
        {
            Text = "Greyed text is the default that will be used if you leave a label blank. " +
                   "System Default follows Windows' input device; if a chosen mic is unplugged, " +
                   "recording falls back to the default.",
            Foreground = Secondary, FontSize = 10, TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 4),
        });

        // Optional desktop-video capture: stream the chosen screen/window
        // alongside audio; LMA saves a video recording of the meeting.
        v.Children.Add(Label("Screen video"));
        _videoEnabled.IsChecked = AppSettings.VideoEnabled;
        v.Children.Add(_videoEnabled);
        SyncVideoSourceHost();
        v.Children.Add(_videoSourceHost);

        // Consent record: when + what the user agreed to, so the one-time
        // acknowledgment is inspectable afterwards (auditable, not a
        // fire-and-forget dialog). Collapsed by default; absent until agreed.
        if (AppSettings.DisclaimerAgreedAt is DateTime consentAt)
        {
            v.Children.Add(Label("Recording consent"));
            v.Children.Add(new Expander
            {
                Header = new TextBlock
                {
                    Text = $"✓ Agreed {consentAt:g}",
                    Foreground = Secondary, FontSize = 10,
                },
                Content = new TextBlock
                {
                    Text = string.IsNullOrEmpty(AppSettings.DisclaimerAgreedText)
                        ? EffectiveDisclaimer : AppSettings.DisclaimerAgreedText,
                    Foreground = Secondary, FontSize = 10, TextWrapping = TextWrapping.Wrap,
                    Margin = new Thickness(4, 2, 0, 2),
                },
                Margin = new Thickness(0, 0, 0, 4),
            });
        }
        _settingsContent = v;
        return v;
    }

    /// <summary>
    /// Show or hide the video source picker + help text in place, matching the
    /// "Also record screen video" toggle.
    /// </summary>
    private void SyncVideoSourceHost()
    {
        _videoSourceHost.Children.Clear();
        if (!AppSettings.VideoEnabled) return;
        RefreshVideoPicker();
        _videoSourceHost.Children.Add(_videoPicker);
        _videoSourceHost.Children.Add(new TextBlock
        {
            Text = "The selected screen or window is recorded with the meeting and saved as a " +
                   "video in LMA.",
            Foreground = Secondary, FontSize = 10, TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 4),
        });
    }

    /// <summary>
    /// Recent meeting names — recurring meetings are the norm, so re-picking one
    /// should be a click rather than retyping it.
    /// </summary>
    private void ShowRecentMeetingsMenu()
    {
        var names = AppSettings.RecentMeetingNames;
        var menu = new ContextMenu();
        if (names.Count == 0)
        {
            menu.Items.Add(new MenuItem { Header = "(no recent meetings)", IsEnabled = false });
        }
        else
        {
            foreach (var name in names)
            {
                var item = new MenuItem { Header = name };
                var captured = name;
                item.Click += (_, _) => _meetingName.Text = captured;
                menu.Items.Add(item);
            }
            menu.Items.Add(new Separator());
            var clear = new MenuItem { Header = "Clear recent" };
            clear.Click += (_, _) => { AppSettings.ClearRecentMeetingNames(); Refresh(); };
            menu.Items.Add(clear);
        }
        menu.PlacementTarget = _recentMeetings;
        menu.IsOpen = true;
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
        // One-time recording-consent gate (mirrors the browser extension): the
        // first Start on this machine shows the disclaimer; Agree persists and
        // proceeds, Cancel does nothing.
        if (NeedsDisclaimer)
        {
            _showDisclaimer = true;
            Refresh();
            return;
        }
        ReallyStart();
    }

    /// <summary>The disclaimer text currently in effect for this deployment.</summary>
    private string EffectiveDisclaimer =>
        string.IsNullOrEmpty(_c.Config.RecordingDisclaimer) ? Config.DefaultDisclaimer : _c.Config.RecordingDisclaimer;

    private void ReallyStart()
    {
        // Push current settings (the mic label may depend on the signed-in email).
        PushSettingsToController();
        AppSettings.RememberMeetingName(_meetingName.Text);
        var name = string.IsNullOrEmpty(_meetingName.Text)
            ? null
            : $"{_meetingName.Text} - {DateTime.Now:yyyy-MM-dd HH:mm}";
        _c.Start(name);
    }

    /// <summary>
    /// Whether Start would currently be gated on the consent disclaimer — used by
    /// TrayApp so a JumpList "Start Recording" surfaces the panel with the dialog
    /// instead of silently doing nothing. Also true when the DEPLOYMENT'S
    /// DISCLAIMER TEXT changed since consent — the recorded consent covers the
    /// text the user actually saw, not later revisions. (Consents that predate
    /// text recording re-consent once.)
    /// </summary>
    public bool NeedsDisclaimer =>
        !AppSettings.DisclaimerAgreed || AppSettings.DisclaimerAgreedText != EffectiveDisclaimer;

    /// <summary>Show the consent gate (for Start attempts that arrive from outside the panel).</summary>
    public void ShowDisclaimerGate()
    {
        _showDisclaimer = true;
        Refresh();
    }

    // Consent-gate UI state + block. Built fresh each Refresh (cheap, and avoids
    // the shared-parent pitfalls of persistent controls).
    private bool _showDisclaimer;

    private Border BuildDisclaimerBlock()
    {
        var panel = new StackPanel();
        panel.Children.Add(new TextBlock
        {
            Text = "⚠ Important",
            FontWeight = FontWeights.Bold,
            Foreground = new SolidColorBrush(Color.FromRgb(0xB0, 0x6A, 0x00)),
            Margin = new Thickness(0, 0, 0, 4),
        });
        panel.Children.Add(new TextBlock
        {
            Text = EffectiveDisclaimer,
            FontSize = 11,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 8),
        });
        if (AppSettings.VideoEnabled)
        {
            panel.Children.Add(new TextBlock
            {
                Text = "🎥 Screen video is ON: your selected screen or window is also recorded.",
                FontSize = 11,
                TextWrapping = TextWrapping.Wrap,
                Foreground = new SolidColorBrush(Color.FromRgb(0xB0, 0x6A, 0x00)),
                Margin = new Thickness(0, 0, 0, 8),
            });
        }
        var cancel = new Button { Content = "Cancel" };
        cancel.Click += (_, _) => { _showDisclaimer = false; Refresh(); };
        var agree = new Button { Content = "Agree", FontWeight = FontWeights.Bold };
        agree.Click += (_, _) =>
        {
            // Record WHAT was agreed to and WHEN (shown in Settings afterwards).
            AppSettings.RecordDisclaimerConsent(EffectiveDisclaimer);
            _showDisclaimer = false;
            Refresh();
            ReallyStart();
        };
        panel.Children.Add(Row(cancel, agree));
        return new Border
        {
            Background = new SolidColorBrush(Color.FromArgb(0x14, 0xE0, 0x91, 0x00)),
            BorderBrush = new SolidColorBrush(Color.FromRgb(0xE0, 0x91, 0x00)),
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(10),
            Margin = new Thickness(0, 2, 0, 8),
            Child = panel,
        };
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
