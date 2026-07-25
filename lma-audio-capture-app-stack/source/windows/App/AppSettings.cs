using Microsoft.Win32;

namespace LMA;

/// <summary>
/// Lightweight persistence for the tray app, mirroring the macOS UserDefaults +
/// SMAppService behavior:
///   • "Remember my email" — stores the login email ONLY (never the password)
///     under HKCU so the field can prefill next launch.
///   • "Start automatically at login" — reflects/toggles the real HKCU ...\Run
///     registry entry, so it stays correct even if changed elsewhere.
///
/// All Windows-specific; kept out of the UI-agnostic engine.
/// </summary>
public static class AppSettings
{
    private const string SettingsKeyPath = @"Software\AmazonLMA\AudioCapture";
    private const string RunKeyPath = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string RunValueName = "LMAAudioCapture";

    private const string RememberValue = "RememberLogin";
    private const string UsernameValue = "SavedUsername";

    // MARK: - Remember email (email only; never the password)

    public static bool RememberLogin
    {
        get
        {
            using var k = Registry.CurrentUser.OpenSubKey(SettingsKeyPath);
            return (k?.GetValue(RememberValue) as int?) == 1;
        }
    }

    public static string SavedUsername
    {
        get
        {
            using var k = Registry.CurrentUser.OpenSubKey(SettingsKeyPath);
            return (k?.GetValue(UsernameValue) as string) ?? "";
        }
    }

    /// <summary>Persist (or clear) the remembered login id per the toggle.</summary>
    public static void PersistLoginPreference(bool remember, string username)
    {
        using var k = Registry.CurrentUser.CreateSubKey(SettingsKeyPath);
        k.SetValue(RememberValue, remember ? 1 : 0, RegistryValueKind.DWord);
        if (remember)
            k.SetValue(UsernameValue, username, RegistryValueKind.String);
        else
            k.DeleteValue(UsernameValue, throwOnMissingValue: false);
    }

    // MARK: - Start at login (HKCU ...\Run)

    /// <summary>
    /// Whether the app is registered to launch at login. Reflects the real
    /// registry state, so it stays correct even if changed elsewhere. Setting it
    /// writes/removes the HKCU Run value pointing at this executable (with --gui
    /// so it always launches the tray, never the CLI).
    /// </summary>
    public static bool LaunchAtLogin
    {
        get
        {
            using var k = Registry.CurrentUser.OpenSubKey(RunKeyPath);
            return k?.GetValue(RunValueName) != null;
        }
        set
        {
            using var k = Registry.CurrentUser.CreateSubKey(RunKeyPath);
            if (value)
            {
                var exe = Environment.ProcessPath ?? "";
                if (!string.IsNullOrEmpty(exe))
                    k.SetValue(RunValueName, $"\"{exe}\" --gui", RegistryValueKind.String);
            }
            else
            {
                k.DeleteValue(RunValueName, throwOnMissingValue: false);
            }
        }
    }
}
