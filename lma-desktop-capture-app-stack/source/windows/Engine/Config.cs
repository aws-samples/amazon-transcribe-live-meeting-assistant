using System.IO;
using System.Text.Json;

namespace LMA;

/// <summary>
/// Runtime configuration. Values are resolved with this precedence (first wins):
///   1. CLI flag        (e.g. --endpoint ...)
///   2. environment var (e.g. LMA_WS_ENDPOINT)
///   3. lma-config.json  — a deployment config file baked into the download
///      package (endpoint + Cognito pool/client/region). See FindConfigFile().
///   4. built-in default
///
/// This layering is what lets the downloadable app "just work": the packaging
/// pipeline bakes the deployment's endpoint + Cognito identifiers into
/// lma-config.json next to the executable, so the end user only supplies
/// --username / --password (or is prompted). Developers can still override
/// anything via flags/env for testing. Ported verbatim from macOS Config.swift.
/// </summary>
public sealed class Config
{
    public string Endpoint = "";
    public string AccessToken = "";
    public string IdToken = "";
    /// <summary>
    /// Cognito REFRESH token. Retained (it used to be discarded at sign-in —
    /// issue #535) so the ~1 h access token can be renewed without another
    /// interactive sign-in. Redeemed by TokenStore, never sent to the server.
    /// Pasted-token path: prefer LMA_REFRESH_TOKEN over --refresh-token — a
    /// refresh token is a ~30-day credential and command lines are visible to
    /// other local users (Task Manager, WMI Win32_Process).
    /// </summary>
    public string RefreshToken = "";
    public string CallId = "";
    public string AgentId = "";
    public string FromNumber = "";
    public string ToNumber = "";
    public int SampleRate = 48000;
    public string DebugWavPath = "";   // empty = disabled

    // In-app SRP login (alternative to pasting --token/--id-token).
    public string Username = "";
    public string Password = "";
    public string UserPoolId = "";
    public string ClientId = "";
    public string Region = "";
    /// <summary>
    /// Web UI base URL (CloudFront), for the "Open in LMA" deep link. This is a
    /// DIFFERENT CloudFront distribution than the WebSocket endpoint, so it must
    /// be configured explicitly rather than derived from `endpoint`.
    /// </summary>
    public string WebEndpoint = "";

    /// <summary>
    /// Recording-consent disclaimer shown before the first recording. Baked into
    /// lma-config.json from the deployment's RecordingDisclaimer parameter (same
    /// text the browser extension shows); falls back to the standard wording.
    /// </summary>
    public string RecordingDisclaimer = "";

    /// <summary>CLI: also capture and stream desktop video (--video 1 / LMA_VIDEO). The
    /// GUI ignores this and uses its own persisted Settings toggle instead.</summary>
    public bool VideoEnabled = false;
    /// <summary>CLI: video source id ("display:&lt;name&gt;" / "window:&lt;handle&gt;"; "" = primary display).</summary>
    public string VideoSourceId = "";

    /// <summary>
    /// Name of the LMA CloudFormation stack this download came from. Shown in
    /// the UI and used to namespace every machine-scoped identifier (registry
    /// key, mutex, pipe, start-at-login entry) so the clients for multiple LMA
    /// stacks can coexist. Empty for hand-built dev copies. See AppIdentity.
    /// </summary>
    public string StackName = "";
    /// <summary>LMA version this package was built from (shown in About lines).</summary>
    public string AppVersion = "";

    /// <summary>Fallback consent text when the deployment config predates the setting.</summary>
    public const string DefaultDisclaimer =
        "Important: You are responsible for complying with legal, corporate, and ethical " +
        "restrictions that apply to recording meetings and calls. Do not use this solution " +
        "to stream, record, or transcribe calls if otherwise prohibited.";

    public static Config Parse(string[] argv)
    {
        var env = Environment.GetEnvironmentVariables();
        var args = new Dictionary<string, string>();
        // Parse "--key value" pairs (a flag with no following value → empty string).
        for (int i = 0; i < argv.Length; i++)
        {
            if (argv[i].StartsWith("--"))
            {
                var key = argv[i].Substring(2);
                var val = (i + 1 < argv.Length && !argv[i + 1].StartsWith("--")) ? argv[++i] : "";
                args[key] = val;
            }
        }

        string? EnvVal(string k) => env.Contains(k) ? env[k] as string : null;

        // Load the deployment config file (baked into the download package), if
        // present. CLI flags and env vars still take precedence over it.
        var fileValues = LoadConfigFile(args.GetValueOrDefault("config") ?? EnvVal("LMA_CONFIG"));

        string Value(string flag, string envKey, string? fileKey = null, string fallback = "")
        {
            if (args.TryGetValue(flag, out var a)) return a;
            var e = EnvVal(envKey);
            if (e != null) return e;
            if (fileKey != null && fileValues.TryGetValue(fileKey, out var fv) && !string.IsNullOrEmpty(fv)) return fv;
            return fallback;
        }

        int sr = int.TryParse(Value("sample-rate", "LMA_SAMPLE_RATE", "samplingRate", "48000"), out var p) ? p : 48000;
        var defaultCallId = "LMA native prototype - " + DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ");

        return new Config
        {
            Endpoint = Value("endpoint", "LMA_WS_ENDPOINT", "wssEndpoint"),
            AccessToken = Value("token", "LMA_ACCESS_TOKEN"),
            IdToken = Value("id-token", "LMA_ID_TOKEN"),
            RefreshToken = Value("refresh-token", "LMA_REFRESH_TOKEN"),
            CallId = Value("call-id", "LMA_CALL_ID", null, defaultCallId),
            AgentId = Value("agent-id", "LMA_AGENT_ID", null, "Me"),
            FromNumber = Value("from", "LMA_FROM", null, "Other participants"),
            ToNumber = Value("to", "LMA_TO", null, "System"),
            SampleRate = sr,
            DebugWavPath = Value("debug-wav", "LMA_DEBUG_WAV"),
            Username = Value("username", "LMA_USERNAME"),
            Password = Value("password", "LMA_PASSWORD"),
            UserPoolId = Value("user-pool-id", "LMA_USER_POOL_ID", "userPoolId"),
            ClientId = Value("client-id", "LMA_CLIENT_ID", "clientId"),
            Region = Value("region", "LMA_REGION", "region"),
            WebEndpoint = Value("web-endpoint", "LMA_WEB_ENDPOINT", "webEndpoint"),
            RecordingDisclaimer = Value("disclaimer", "LMA_RECORDING_DISCLAIMER", "recordingDisclaimer",
                                        DefaultDisclaimer),
            VideoEnabled = new[] { "1", "true", "yes" }.Contains(Value("video", "LMA_VIDEO").ToLowerInvariant()),
            VideoSourceId = Value("video-source", "LMA_VIDEO_SOURCE"),
            StackName = Value("stack-name", "LMA_STACK_NAME", "stackName"),
            AppVersion = Value("app-version", "LMA_APP_VERSION", "appVersion"),
        };
    }

    /// <summary>
    /// Locate and parse lma-config.json. Search order:
    ///   1. explicit path (--config / LMA_CONFIG)
    ///   2. next to the executable  (how the download package ships it)
    ///   3. a Resources/ sibling dir
    ///   4. current working directory
    /// Returns a flat map of the JSON's top-level string/number values; empty if
    /// no file is found (all-flags/env usage still works).
    /// </summary>
    private static Dictionary<string, string> LoadConfigFile(string? explicitPath)
    {
        var candidates = new List<string>();
        if (!string.IsNullOrEmpty(explicitPath)) candidates.Add(explicitPath);

        // AppContext.BaseDirectory is the exe's directory (works for both
        // framework-dependent and self-contained publishes).
        var exeDir = AppContext.BaseDirectory;
        candidates.Add(Path.Combine(exeDir, "lma-config.json"));
        var parent = Directory.GetParent(exeDir.TrimEnd(Path.DirectorySeparatorChar))?.FullName;
        if (parent != null) candidates.Add(Path.Combine(parent, "Resources", "lma-config.json"));
        candidates.Add(Path.Combine(Directory.GetCurrentDirectory(), "lma-config.json"));

        foreach (var path in candidates)
        {
            try
            {
                if (!File.Exists(path)) continue;
                using var doc = JsonDocument.Parse(File.ReadAllText(path));
                var outMap = new Dictionary<string, string>();
                foreach (var prop in doc.RootElement.EnumerateObject())
                {
                    outMap[prop.Name] = prop.Value.ValueKind switch
                    {
                        JsonValueKind.String => prop.Value.GetString() ?? "",
                        JsonValueKind.Number => prop.Value.GetRawText(),
                        _ => prop.Value.ToString(),
                    };
                }
                return outMap;
            }
            catch { /* try next candidate */ }
        }
        return new Dictionary<string, string>();
    }

    /// <summary>True when the user wants in-app SRP login instead of pasted tokens.</summary>
    public bool WantsLogin => !string.IsNullOrEmpty(Username) && string.IsNullOrEmpty(AccessToken);

    /// <summary>
    /// Region inferred from the user pool id (e.g. "us-west-2_abc" → "us-west-2")
    /// when --region isn't given explicitly.
    /// </summary>
    public string EffectiveRegion
    {
        get
        {
            if (!string.IsNullOrEmpty(Region)) return Region;
            var us = UserPoolId.Split('_');
            if (us.Length > 0 && !string.IsNullOrEmpty(us[0])) return us[0];
            return "us-east-1";
        }
    }

    /// <summary>Fail fast with a helpful message if required values are missing.</summary>
    public string? Validate()
    {
        if (string.IsNullOrEmpty(Endpoint)) return "Missing --endpoint (or LMA_WS_ENDPOINT).";
        if (!Endpoint.StartsWith("wss://") && !Endpoint.StartsWith("ws://"))
            return $"Endpoint must start with wss:// (got: {Endpoint}).";
        // Either a pasted access token OR SRP login credentials are required.
        if (string.IsNullOrEmpty(AccessToken))
        {
            if (string.IsNullOrEmpty(Username))
                return "Provide either --token (pasted Cognito access token) or --username/--password for in-app login.";
            if (string.IsNullOrEmpty(UserPoolId) || string.IsNullOrEmpty(ClientId))
                return "In-app login needs --user-pool-id and --client-id (region inferred from the pool id, or pass --region).";
        }
        return null;
    }
}
