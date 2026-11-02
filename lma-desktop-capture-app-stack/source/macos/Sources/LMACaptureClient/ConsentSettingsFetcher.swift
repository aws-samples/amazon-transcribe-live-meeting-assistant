import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Reads the meeting consent settings from the transcriber before Start.
///
/// The app connects to the transcriber only when a recording starts, so asking
/// what the consent step should show needs a connection of its own: open a
/// socket with the same authentication as TranscriberSocket, send
/// CONSENT_SETTINGS, wait for the answer, close. No meeting is started on it.
///
/// nil when nothing answered in time: a transcriber that predates the consent
/// frames never answers, and the app then shows no step, as before. The server
/// still applies the deployment's settings to the START that follows.
enum ConsentSettingsFetcher {
    static func fetch(config: Config, tokens: TokenStore?, timeout: TimeInterval = 6) async -> Consent.Settings? {
        if let tokens = tokens, tokens.needsRefreshBeforeUse {
            _ = await tokens.refreshIfNeeded()
        }
        let access = tokens?.accessToken ?? config.accessToken
        let idToken = tokens?.idToken ?? config.idToken
        guard !access.isEmpty,
              let request = TranscriberSocket.authorizedRequest(
                endpoint: config.endpoint, accessToken: access, idToken: idToken),
              let body = try? JSONSerialization.data(withJSONObject: Consent.settingsRequest),
              let text = String(data: body, encoding: .utf8) else { return nil }

        let session = URLSession(configuration: .ephemeral)
        let task = session.webSocketTask(with: request)
        task.resume()
        defer {
            task.cancel(with: .normalClosure, reason: nil)
            session.invalidateAndCancel()
        }
        return await withTaskGroup(of: Consent.Settings?.self) { group in
            // The send is inside the timed task too: it waits for the upgrade, so
            // an unreachable endpoint is bounded by `timeout` rather than by the
            // session's request timeout.
            group.addTask {
                do {
                    try await task.send(.string(text))
                } catch {
                    return nil
                }
                while true {
                    guard let message = try? await task.receive() else { return nil }
                    if case .string(let s) = message, case .settings(let settings)? = Consent.parseFrame(s) {
                        return settings
                    }
                }
            }
            group.addTask {
                try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                return nil
            }
            let first = await group.next() ?? nil
            // Cancelling the socket is what ends a receive still waiting.
            task.cancel(with: .normalClosure, reason: nil)
            group.cancelAll()
            return first
        }
    }
}
