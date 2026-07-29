using System.Windows;

namespace LMA;

/// <summary>
/// Start/stop recording notifications, so recording state is visible even when
/// no window is open — reinforcing that you can't be recording without noticing,
/// and surfacing the finished recording afterwards.
///
/// Implemented with the tray icon's balloon tip rather than a WinRT toast: toasts
/// require a registered AppUserModelID + Start Menu shortcut to be reliable, and
/// this app is a portable build-from-source package that may not be installed via
/// the Start Menu at all. The balloon works unconditionally and needs no manifest.
///
/// TrayApp sets <see cref="Sink"/> once the tray icon exists; before then (or if
/// the shell suppresses balloons) every call is a silent no-op.
/// </summary>
public static class Notifier
{
    /// <summary>
    /// Delivers (title, body, optional URL to open when clicked). Set by TrayApp.
    /// </summary>
    public static Action<string, string, string?>? Sink;

    public static void Notify(string title, string body, string? openUrl = null)
    {
        var sink = Sink;
        if (sink == null) return;
        try
        {
            // Marshal onto the UI thread: engine callbacks arrive on capture /
            // socket threads, and the tray icon is a WPF object.
            var app = Application.Current;
            if (app?.Dispatcher != null && !app.Dispatcher.CheckAccess())
                app.Dispatcher.BeginInvoke(new Action(() => sink(title, body, openUrl)));
            else
                sink(title, body, openUrl);
        }
        catch
        {
            // Notifications are best-effort; never let them break recording.
        }
    }
}
