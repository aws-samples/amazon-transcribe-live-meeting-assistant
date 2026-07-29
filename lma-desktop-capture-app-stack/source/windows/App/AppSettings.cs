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

    // MARK: - Settings gear (speaker labels + mic device)

    private const string MicLabelValue = "MicLabel";
    private const string SystemLabelValue = "SystemLabel";
    private const string MicDeviceValue = "MicDeviceId";

    /// <summary>Default label for the system channel when the user hasn't set one.</summary>
    public const string DefaultSystemLabel = "Other participants";

    /// <summary>Custom mic-channel speaker label ("" = default: signed-in email).</summary>
    public static string MicLabel
    {
        get { using var k = Registry.CurrentUser.OpenSubKey(SettingsKeyPath); return (k?.GetValue(MicLabelValue) as string) ?? ""; }
        set { using var k = Registry.CurrentUser.CreateSubKey(SettingsKeyPath); k.SetValue(MicLabelValue, value, RegistryValueKind.String); }
    }

    /// <summary>Custom system-channel speaker label ("" = DefaultSystemLabel).</summary>
    public static string SystemLabel
    {
        get { using var k = Registry.CurrentUser.OpenSubKey(SettingsKeyPath); return (k?.GetValue(SystemLabelValue) as string) ?? ""; }
        set { using var k = Registry.CurrentUser.CreateSubKey(SettingsKeyPath); k.SetValue(SystemLabelValue, value, RegistryValueKind.String); }
    }

    /// <summary>MMDevice ID of the chosen mic ("" = system default).</summary>
    public static string MicDeviceId
    {
        get { using var k = Registry.CurrentUser.OpenSubKey(SettingsKeyPath); return (k?.GetValue(MicDeviceValue) as string) ?? ""; }
        set { using var k = Registry.CurrentUser.CreateSubKey(SettingsKeyPath); k.SetValue(MicDeviceValue, value, RegistryValueKind.String); }
    }

    // MARK: - Optional desktop-video capture (screen recording)

    private const string VideoEnabledValue = "VideoEnabled";
    private const string VideoSourceValue = "VideoSourceId";

    /// <summary>Whether to also capture and stream desktop video. Default off.</summary>
    public static bool VideoEnabled
    {
        get { using var k = Registry.CurrentUser.OpenSubKey(SettingsKeyPath); return (k?.GetValue(VideoEnabledValue) as int?) == 1; }
        set { using var k = Registry.CurrentUser.CreateSubKey(SettingsKeyPath); k.SetValue(VideoEnabledValue, value ? 1 : 0, RegistryValueKind.DWord); }
    }

    /// <summary>Chosen video source id ("display:&lt;name&gt;" / "window:&lt;handle&gt;"; "" = primary display).</summary>
    public static string VideoSourceId
    {
        get { using var k = Registry.CurrentUser.OpenSubKey(SettingsKeyPath); return (k?.GetValue(VideoSourceValue) as string) ?? ""; }
        set { using var k = Registry.CurrentUser.CreateSubKey(SettingsKeyPath); k.SetValue(VideoSourceValue, value, RegistryValueKind.String); }
    }

    // MARK: - Recording-consent disclaimer (one-time acknowledgment)

    private const string DisclaimerAgreedValue = "DisclaimerAgreed";
    private const string DisclaimerAgreedAtValue = "DisclaimerAgreedAt";
    private const string DisclaimerAgreedTextValue = "DisclaimerAgreedText";

    /// <summary>
    /// Whether the user has acknowledged the recording-consent disclaimer (shown
    /// once before the first recording, same pattern as the browser extension).
    /// </summary>
    public static bool DisclaimerAgreed
    {
        get { using var k = Registry.CurrentUser.OpenSubKey(SettingsKeyPath); return (k?.GetValue(DisclaimerAgreedValue) as int?) == 1; }
    }

    /// <summary>When consent was recorded, or null (also null for consents that predate timestamping).</summary>
    public static DateTime? DisclaimerAgreedAt
    {
        get
        {
            using var k = Registry.CurrentUser.OpenSubKey(SettingsKeyPath);
            var s = k?.GetValue(DisclaimerAgreedAtValue) as string;
            return DateTime.TryParse(s, null, System.Globalization.DateTimeStyles.RoundtripKind, out var d) ? d : null;
        }
    }

    /// <summary>The exact disclaimer text the user agreed to (for the consent record).</summary>
    public static string DisclaimerAgreedText
    {
        get { using var k = Registry.CurrentUser.OpenSubKey(SettingsKeyPath); return (k?.GetValue(DisclaimerAgreedTextValue) as string) ?? ""; }
    }

    /// <summary>Record consent: the flag, WHEN it happened, and WHAT text was shown.</summary>
    public static void RecordDisclaimerConsent(string disclaimerText)
    {
        using var k = Registry.CurrentUser.CreateSubKey(SettingsKeyPath);
        k.SetValue(DisclaimerAgreedValue, 1, RegistryValueKind.DWord);
        k.SetValue(DisclaimerAgreedAtValue, DateTime.Now.ToString("o"), RegistryValueKind.String);
        k.SetValue(DisclaimerAgreedTextValue, disclaimerText, RegistryValueKind.String);
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
