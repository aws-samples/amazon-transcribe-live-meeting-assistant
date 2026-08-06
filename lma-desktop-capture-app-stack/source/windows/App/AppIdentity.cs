namespace LMA;

/// <summary>
/// Per-stack identity for the Windows client — the single source of truth for
/// every machine-scoped name the app uses.
///
/// The clients for multiple LMA deployments are expected to coexist on one
/// machine, so anything machine-global MUST be namespaced by the LMA stack the
/// package was downloaded from (baked into lma-config.json as `stackName`):
///
///   • HKCU settings key        Software\AmazonLMA\CaptureClient\&lt;Stack&gt;
///   • Run (start-at-login)     LMACaptureClient-&lt;slug&gt;
///   • single-instance mutex     Local\LMACaptureClient.&lt;slug&gt;.instance.&lt;user&gt;
///   • control pipe              LMACaptureClient.&lt;slug&gt;.control.&lt;user&gt;
///   • install dir / shortcut    LMA Capture Client (&lt;Stack&gt;)
///
/// Without the stack qualifier, launching the client for stack B would be
/// treated as a second instance of stack A's client (mutex collision), would
/// share its settings and recording-consent record, and would fight over the
/// same start-at-login entry.
///
/// Hand-built dev copies have no stackName and fall back to unsuffixed names,
/// matching the macOS client's behavior.
/// </summary>
public static class AppIdentity
{
    /// <summary>Raw stack name from lma-config.json ("" for dev builds).</summary>
    public static string StackName { get; private set; } = "";

    /// <summary>LMA version this package was built from ("" if unknown).</summary>
    public static string AppVersion { get; private set; } = "";

    /// <summary>
    /// Lowercase alphanumerics-and-dashes form of the stack name, used inside
    /// identifiers. Empty when there is no stack name.
    ///
    /// The same algorithm (lowercase, map anything outside [a-z0-9-] to '-',
    /// collapse runs, trim ends, append a 6-char digest) exists in FIVE places
    /// and they must agree, or
    /// the installer and the app would use different identifiers and settings /
    /// OS permissions wouldn't line up: Config.swift's `stackSlug`,
    /// macos/make-app.sh, macos/install-macos.sh, build-windows.ps1, and this
    /// file — FIVE copies in total.
    /// ASCII-only by design — CloudFormation stack names are [A-Za-z][A-Za-z0-9-]*.
    /// </summary>
    public static string StackSlug { get; private set; } = "";

    /// <summary>
    /// Call once at startup, before anything reads settings or takes the
    /// single-instance mutex (see Program.Main).
    /// </summary>
    public static void Initialize(Config config)
    {
        StackName = config.StackName ?? "";
        AppVersion = config.AppVersion ?? "";
        StackSlug = Slugify(StackName);
    }

    internal static string Slugify(string value)
    {
        if (string.IsNullOrEmpty(value)) return "";
        var chars = value.ToLowerInvariant().Select(c =>
            (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' ? c : '-');
        var collapsed = new System.Text.StringBuilder();
        bool lastDash = false;
        foreach (var c in chars)
        {
            if (c == '-')
            {
                if (!lastDash) collapsed.Append(c);
                lastDash = true;
            }
            else
            {
                collapsed.Append(c);
                lastDash = false;
            }
        }
        var b = collapsed.ToString().Trim('-');
        // The character mapping is LOSSY, so append a digest of the EXACT stack
        // name: "LMA-Bob" and "lma-bob" are both legal CloudFormation names and
        // can coexist, but would otherwise collapse to one slug — making the two
        // stacks' clients share the registry key, the Run entry, and (worst) the
        // single-instance mutex, so launching one would just raise the other.
        var d = StackDigest(value);
        return b.Length == 0 ? d : $"{b}-{d}";
    }

    /// <summary>
    /// First 6 hex chars of SHA-256(stackName), lowercase. MUST match
    /// Config.swift's stackDigest, make-app.sh, install-macos.sh, and
    /// build-windows.ps1 — the app and the installer have to agree.
    /// </summary>
    internal static string StackDigest(string value)
    {
        var bytes = System.Security.Cryptography.SHA256.HashData(
            System.Text.Encoding.UTF8.GetBytes(value));
        return Convert.ToHexString(bytes).ToLowerInvariant().Substring(0, 6);
    }

    /// <summary>Human-readable name, stack-qualified when known.</summary>
    public static string DisplayName =>
        string.IsNullOrEmpty(StackName) ? "LMA Capture Client" : $"LMA Capture Client ({StackName})";

    /// <summary>Identifier fragment appended to machine-global names ("" for dev).</summary>
    private static string Suffix => string.IsNullOrEmpty(StackSlug) ? "" : $".{StackSlug}";

    /// <summary>HKCU key holding this stack's settings + consent record.</summary>
    public static string SettingsKeyPath =>
        string.IsNullOrEmpty(StackSlug)
            ? @"Software\AmazonLMA\CaptureClient"
            : $@"Software\AmazonLMA\CaptureClient\{StackSlug}";

    /// <summary>HKCU ...\Run value name for start-at-login.</summary>
    public static string RunValueName =>
        string.IsNullOrEmpty(StackSlug) ? "LMACaptureClient" : $"LMACaptureClient-{StackSlug}";

    /// <summary>Single-instance mutex name (also scoped per user by the caller).</summary>
    public static string MutexName(string userSuffix) =>
        $@"Local\LMACaptureClient{Suffix}.instance.{userSuffix}";

    /// <summary>Control pipe name (also scoped per user by the caller).</summary>
    public static string PipeName(string userSuffix) =>
        $"LMACaptureClient{Suffix}.control.{userSuffix}";

    /// <summary>"LMA Capture Client v0.3.6 · stack LMA-Bob" for About/footer lines.</summary>
    public static string AboutLine
    {
        get
        {
            var s = "LMA Capture Client";
            if (!string.IsNullOrEmpty(AppVersion)) s += $" v{AppVersion}";
            if (!string.IsNullOrEmpty(StackName)) s += $" · stack {StackName}";
            return s;
        }
    }
}
