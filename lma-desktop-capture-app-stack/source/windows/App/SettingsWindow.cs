using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace LMA;

/// <summary>
/// Settings in a dedicated, resizable window (opened from the panel's ⚙ gear) —
/// the Windows counterpart to the macOS SettingsWindowView.
///
/// Why a separate window rather than an expanding section in the tray popup: the
/// settings surface keeps growing (speaker labels, mic device, screen video,
/// consent record), and expanding it inline pushed the borderless popup past the
/// screen edge with content clipped and no way to scroll or resize it. A real
/// window scrolls, resizes, and is where users expect settings to live.
///
/// The controls themselves are owned by <see cref="PanelView"/> (they bind to
/// AppSettings and the controller); this window just hosts them, so there is
/// exactly one implementation of each setting.
/// </summary>
public sealed class SettingsWindow : Window
{
    public SettingsWindow(UIElement settingsContent)
    {
        Title = string.IsNullOrEmpty(AppIdentity.StackName)
            ? "LMA Capture Settings"
            : $"LMA Capture Settings — {AppIdentity.StackName}";
        Width = 420;
        Height = 520;
        MinWidth = 360;
        MinHeight = 300;
        ResizeMode = ResizeMode.CanResize;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        ShowInTaskbar = true;
        Background = new SolidColorBrush(Color.FromRgb(0xFA, 0xFA, 0xFA));
        System.Windows.Documents.TextElement.SetForeground(
            this, new SolidColorBrush(Color.FromRgb(0x1A, 0x1A, 0x1A)));

        var root = new StackPanel { Margin = new Thickness(18) };
        root.Children.Add(settingsContent);
        root.Children.Add(new Separator { Margin = new Thickness(0, 12, 0, 8) });
        root.Children.Add(new TextBlock
        {
            Text = AppIdentity.AboutLine,
            Foreground = new SolidColorBrush(Color.FromRgb(0x5A, 0x5A, 0x5A)),
            FontSize = 10,
            TextWrapping = TextWrapping.Wrap,
        });

        Content = new ScrollViewer
        {
            Content = root,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        };
    }
}
