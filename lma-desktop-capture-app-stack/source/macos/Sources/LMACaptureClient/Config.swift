import Foundation

/// Runtime configuration. Values are resolved with this precedence (first wins):
///   1. CLI flag        (e.g. --endpoint ...)
///   2. environment var (e.g. LMA_WS_ENDPOINT)
///   3. lma-config.json  — a deployment config file baked into the download
///      package (endpoint + Cognito pool/client/region). See findConfigFile().
///   4. built-in default
///
/// This layering is what lets the downloadable app "just work": publish.sh bakes
/// the deployment's endpoint + Cognito identifiers into lma-config.json inside
/// the zip, so the end user only supplies --username / --password (or is
/// prompted). Developers can still override anything via flags/env for testing.
///
///   --endpoint   wss://<cloudfront-domain>/api/v1/ws   (or LMA_WS_ENDPOINT)
///   --token      <Cognito ACCESS token, no "Bearer " prefix>   (or LMA_ACCESS_TOKEN)
///   --id-token   <Cognito ID token>                            (or LMA_ID_TOKEN)
///   --call-id    "My meeting - 2026-07-04"                     (or LMA_CALL_ID)
///   --agent-id   "alice@example.com"  (label for the mic channel)
///   --from       "Other participants" (label for the meeting channel)
///   --sample-rate 48000
///   --debug-wav  /tmp/lma-debug.wav  (tee exact streamed PCM for verification)
///   --config     /path/to/lma-config.json  (override config-file location)
struct Config {
    var endpoint: String
    var accessToken: String
    var idToken: String
    var callId: String
    var agentId: String
    var fromNumber: String
    var toNumber: String
    var sampleRate: Int
    var debugWavPath: String   // empty = disabled

    // In-app SRP login (alternative to pasting --token/--id-token).
    var username: String
    var password: String
    var userPoolId: String
    var clientId: String
    var region: String
    /// Web UI base URL (CloudFront), for the "Open in LMA" deep link. This is a
    /// DIFFERENT CloudFront distribution than the WebSocket endpoint, so it must
    /// be configured explicitly rather than derived from `endpoint`.
    var webEndpoint: String
    /// Recording-consent disclaimer shown before the first recording. Baked into
    /// lma-config.json from the deployment's RecordingDisclaimer parameter (same
    /// text the browser extension shows); falls back to the standard wording.
    var recordingDisclaimer: String
    /// CLI: also capture and stream desktop video (--video / LMA_VIDEO=1). The
    /// GUI ignores this and uses its own persisted Settings toggle instead.
    var videoEnabled: Bool
    /// CLI: video source id ("display:<id>" / "window:<id>"; "" = main display).
    var videoSourceID: String

    /// Name of the LMA CloudFormation stack this download came from. Shown in
    /// the UI and — critically — used to namespace all per-machine identifiers
    /// (bundle id, preferences, install path) so the apps for two different LMA
    /// stacks can coexist. Empty for hand-built dev copies.
    var stackName: String
    /// LMA version this package was built from (shown in the About footer).
    var appVersion: String

    /// The stack name reduced to a safe identifier fragment: alphanumerics and
    /// dashes only, lowercased. Used in the bundle id / defaults suite / paths.
    /// Empty when no stack name is configured (dev builds), in which case the
    /// callers fall back to their unsuffixed defaults.
    var stackSlug: String {
        let allowed = Set("abcdefghijklmnopqrstuvwxyz0123456789-")
        let lowered = stackName.lowercased()
        let mapped = lowered.map { allowed.contains($0) ? $0 : "-" }
        // Collapse runs of dashes and trim them from the ends.
        var out = ""
        var lastWasDash = false
        for ch in mapped {
            if ch == "-" {
                if !lastWasDash { out.append(ch) }
                lastWasDash = true
            } else {
                out.append(ch)
                lastWasDash = false
            }
        }
        return out.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
    }

    /// Human-readable app label, stack-qualified when known:
    /// "LMA Capture Client (LMA-Bob)" — so multiple copies are distinguishable
    /// in the Dock, window titles, and menu bar.
    var appDisplayName: String {
        stackName.isEmpty ? "LMA Capture Client" : "LMA Capture Client (\(stackName))"
    }

    /// Fallback consent text when the deployment config predates the setting.
    static let defaultDisclaimer =
        "Important: You are responsible for complying with legal, corporate, and ethical "
        + "restrictions that apply to recording meetings and calls. Do not use this solution "
        + "to stream, record, or transcribe calls if otherwise prohibited."

    static func parse() -> Config {
        let env = ProcessInfo.processInfo.environment
        var args: [String: String] = [:]
        var it = CommandLine.arguments.dropFirst().makeIterator()
        while let a = it.next() {
            if a.hasPrefix("--") {
                let key = String(a.dropFirst(2))
                args[key] = it.next() ?? ""
            }
        }

        // Load the deployment config file (baked into the download package), if
        // present. CLI flags and env vars still take precedence over it.
        let fileValues = loadConfigFile(explicitPath: args["config"] ?? env["LMA_CONFIG"])

        func value(_ flag: String, _ envKey: String, _ fileKey: String? = nil, _ fallback: String = "") -> String {
            if let a = args[flag] { return a }
            if let e = env[envKey] { return e }
            if let fk = fileKey, let fv = fileValues[fk], !fv.isEmpty { return fv }
            return fallback
        }

        let sr = Int(value("sample-rate", "LMA_SAMPLE_RATE", "samplingRate", "48000")) ?? 48000
        let defaultCallId = "LMA native prototype - \(ISO8601DateFormatter().string(from: Date()))"

        return Config(
            endpoint: value("endpoint", "LMA_WS_ENDPOINT", "wssEndpoint"),
            accessToken: value("token", "LMA_ACCESS_TOKEN"),
            idToken: value("id-token", "LMA_ID_TOKEN"),
            callId: value("call-id", "LMA_CALL_ID", nil, defaultCallId),
            agentId: value("agent-id", "LMA_AGENT_ID", nil, "Me"),
            fromNumber: value("from", "LMA_FROM", nil, "Other participants"),
            toNumber: value("to", "LMA_TO", nil, "System"),
            sampleRate: sr,
            debugWavPath: value("debug-wav", "LMA_DEBUG_WAV"),
            username: value("username", "LMA_USERNAME"),
            password: value("password", "LMA_PASSWORD"),
            userPoolId: value("user-pool-id", "LMA_USER_POOL_ID", "userPoolId"),
            clientId: value("client-id", "LMA_CLIENT_ID", "clientId"),
            region: value("region", "LMA_REGION", "region"),
            webEndpoint: value("web-endpoint", "LMA_WEB_ENDPOINT", "webEndpoint"),
            recordingDisclaimer: value("disclaimer", "LMA_RECORDING_DISCLAIMER", "recordingDisclaimer",
                                       Config.defaultDisclaimer),
            videoEnabled: ["1", "true", "yes"].contains(value("video", "LMA_VIDEO").lowercased()),
            videoSourceID: value("video-source", "LMA_VIDEO_SOURCE"),
            stackName: value("stack-name", "LMA_STACK_NAME", "stackName"),
            appVersion: value("app-version", "LMA_APP_VERSION", "appVersion")
        )
    }

    /// Locate and parse lma-config.json. Search order:
    ///   1. explicit path (--config / LMA_CONFIG)
    ///   2. next to the executable  (how the download package ships it)
    ///   3. current working directory
    /// Returns a flat [String: String] of the JSON's top-level string values;
    /// empty if no file is found (all-flags/env usage still works).
    private static func loadConfigFile(explicitPath: String?) -> [String: String] {
        var candidates: [String] = []
        if let p = explicitPath, !p.isEmpty { candidates.append(p) }
        let exe = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        let exeDir = exe.deletingLastPathComponent()
        candidates.append(exeDir.appendingPathComponent("lma-config.json").path)
        // Also check a Resources dir (in case bundled under an .app later).
        candidates.append(exeDir.deletingLastPathComponent().appendingPathComponent("Resources/lma-config.json").path)
        candidates.append(FileManager.default.currentDirectoryPath + "/lma-config.json")

        for path in candidates {
            guard FileManager.default.fileExists(atPath: path),
                  let data = FileManager.default.contents(atPath: path),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
            var out: [String: String] = [:]
            for (k, v) in obj {
                if let s = v as? String { out[k] = s }
                else if let n = v as? NSNumber { out[k] = n.stringValue }
            }
            return out
        }
        return [:]
    }

    /// True when the user wants in-app SRP login instead of pasted tokens.
    var wantsLogin: Bool { !username.isEmpty && accessToken.isEmpty }

    /// Region inferred from the user pool id (e.g. "us-west-2_abc" → "us-west-2")
    /// when --region isn't given explicitly.
    var effectiveRegion: String {
        if !region.isEmpty { return region }
        if let us = userPoolId.split(separator: "_").first { return String(us) }
        return "us-east-1"
    }

    /// Fail fast with a helpful message if required values are missing.
    func validate() -> String? {
        if endpoint.isEmpty { return "Missing --endpoint (or LMA_WS_ENDPOINT)." }
        if !endpoint.hasPrefix("wss://") && !endpoint.hasPrefix("ws://") {
            return "Endpoint must start with wss:// (got: \(endpoint))."
        }
        // Either a pasted access token OR SRP login credentials are required.
        if accessToken.isEmpty {
            if username.isEmpty {
                return "Provide either --token (pasted Cognito access token) or --username/--password for in-app login."
            }
            if userPoolId.isEmpty || clientId.isEmpty {
                return "In-app login needs --user-pool-id and --client-id (region inferred from the pool id, or pass --region)."
            }
        }
        return nil
    }
}
