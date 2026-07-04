import Foundation

/// Runtime configuration for the prototype. Values come from CLI flags or
/// environment variables so you can spike without building a config UI.
///
///   --endpoint   wss://<cloudfront-domain>/api/v1/ws   (or LMA_WS_ENDPOINT)
///   --token      <Cognito ACCESS token, no "Bearer " prefix>   (or LMA_ACCESS_TOKEN)
///   --id-token   <Cognito ID token>                            (or LMA_ID_TOKEN)
///   --call-id    "My meeting - 2026-07-04"                     (or LMA_CALL_ID)
///   --agent-id   "alice@example.com"  (label for the mic channel)
///   --from       "Other participants" (label for the meeting channel)
///   --sample-rate 48000
struct Config {
    var endpoint: String
    var accessToken: String
    var idToken: String
    var callId: String
    var agentId: String
    var fromNumber: String
    var toNumber: String
    var sampleRate: Int

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

        func value(_ flag: String, _ envKey: String, _ fallback: String = "") -> String {
            args[flag] ?? env[envKey] ?? fallback
        }

        let sr = Int(value("sample-rate", "LMA_SAMPLE_RATE", "48000")) ?? 48000
        let defaultCallId = "LMA native prototype - \(ISO8601DateFormatter().string(from: Date()))"

        return Config(
            endpoint: value("endpoint", "LMA_WS_ENDPOINT"),
            accessToken: value("token", "LMA_ACCESS_TOKEN"),
            idToken: value("id-token", "LMA_ID_TOKEN"),
            callId: value("call-id", "LMA_CALL_ID", defaultCallId),
            agentId: value("agent-id", "LMA_AGENT_ID", "Me"),
            fromNumber: value("from", "LMA_FROM", "Other participants"),
            toNumber: value("to", "LMA_TO", "System"),
            sampleRate: sr
        )
    }

    /// Fail fast with a helpful message if required values are missing.
    func validate() -> String? {
        if endpoint.isEmpty { return "Missing --endpoint (or LMA_WS_ENDPOINT)." }
        if !endpoint.hasPrefix("wss://") && !endpoint.hasPrefix("ws://") {
            return "Endpoint must start with wss:// (got: \(endpoint))."
        }
        if accessToken.isEmpty { return "Missing --token / LMA_ACCESS_TOKEN (Cognito ACCESS token)." }
        return nil
    }
}
