using System.Net.WebSockets;
using System.Text;
using System.Text.Json;

namespace LMA;

/// <summary>
/// Reads the meeting consent settings from the transcriber before Start.
///
/// The app connects to the transcriber only when a recording starts, so asking
/// what the consent step should show needs a connection of its own: open a
/// socket with the same authentication as TranscriberSocket, send
/// CONSENT_SETTINGS, wait for the answer, close. No meeting is started on it.
///
/// null when nothing answered in time: a transcriber that predates the consent
/// frames never answers, and the app then shows no step, as before. The server
/// still applies the deployment's settings to the START that follows.
/// Ported from macOS ConsentSettingsFetcher.swift.
/// </summary>
public static class ConsentSettingsFetcher
{
    public static async Task<Consent.Settings?> FetchAsync(Config config, TokenStore? tokens,
                                                           TimeSpan? timeout = null)
    {
        try
        {
            if (tokens is { NeedsRefreshBeforeUse: true }) await tokens.RefreshIfNeededAsync();
            var access = tokens?.AccessToken ?? config.AccessToken;
            var idToken = tokens?.IdToken ?? config.IdToken;
            if (string.IsNullOrEmpty(access)) return null;

            using var cts = new CancellationTokenSource(timeout ?? TimeSpan.FromSeconds(6));
            using var ws = new ClientWebSocket();
            TranscriberSocket.ApplyAuthHeaders(ws, access, idToken);
            await ws.ConnectAsync(TranscriberSocket.AuthorizedUri(config.Endpoint, access, idToken), cts.Token);

            var request = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(Consent.SettingsRequest));
            await ws.SendAsync(new ArraySegment<byte>(request), WebSocketMessageType.Text, true, cts.Token);

            var buffer = new byte[8192];
            var message = new StringBuilder();
            while (ws.State == WebSocketState.Open)
            {
                var result = await ws.ReceiveAsync(new ArraySegment<byte>(buffer), cts.Token);
                if (result.MessageType == WebSocketMessageType.Close) return null;
                if (result.MessageType != WebSocketMessageType.Text) continue;
                message.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));
                if (!result.EndOfMessage) continue;
                var settings = Consent.ParseSettings(message.ToString());
                message.Clear();
                if (settings != null)
                {
                    try
                    {
                        await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, null, CancellationToken.None);
                    }
                    catch { /* closing is best effort */ }
                    return settings;
                }
            }
            return null;
        }
        catch (Exception e) when (e is OperationCanceledException or WebSocketException or UriFormatException
                                       or InvalidOperationException or System.Net.Http.HttpRequestException)
        {
            return null;
        }
    }
}
