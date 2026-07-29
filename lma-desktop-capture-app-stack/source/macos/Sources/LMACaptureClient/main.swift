import Foundation
import AVFoundation

// LMA native macOS audio client — PROTOTYPE.
//
// Captures microphone + system audio, interleaves to 2-channel 16-bit PCM, and
// streams to the LMA WebSocket transcriber. Proves that a native app can feed
// the same pipeline as the browser "Stream Audio" tab, WITHOUT a browser and
// WITHOUT a Virtual Participant bot — capturing audio straight from a native
// Zoom/Teams/Meet desktop client via system-audio loopback.
//
// Usage:
//   swift run LMACaptureClient \
//     --endpoint wss://<cloudfront-domain>/api/v1/ws \
//     --token <cognito-access-token> \
//     --id-token <cognito-id-token> \
//     --call-id "Test meeting"
//
// (Or set LMA_WS_ENDPOINT / LMA_ACCESS_TOKEN / LMA_ID_TOKEN env vars.)

// Unbuffered stdout: Swift block-buffers stdout when it is not a TTY (e.g. piped
// to a file or captured by a launcher), which hides all progress output until
// exit. Force unbuffered so logs appear live when redirected.
setvbuf(stdout, nil, _IONBF, 0)

// Decode a base64url segment (JWT payload) — for --login-only diagnostics.
func base64urlDecode(_ s: String) -> Data? {
    var b = s.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
    while b.count % 4 != 0 { b += "=" }
    return Data(base64Encoded: b)
}

// `--selftest`: validate BigUInt + the Cognito SRP signature against baked-in
// known-answers (offline), then exit. Run this after any change to the crypto.
if CommandLine.arguments.contains("--selftest") {
    exit(SelfTest.run())
}

// Mode dispatch: with NO CLI flags (e.g. double-clicked as an .app), launch the
// SwiftUI menu-bar GUI. With any --flag, run the existing headless CLI below so
// the downloadable package's scripted/CLI usage is completely unchanged.
// `--cli` forces CLI mode even with no other flags; `--gui` forces GUI mode.
let userFlags = CommandLine.arguments.dropFirst().filter { $0.hasPrefix("--") && $0 != "--cli" && $0 != "--gui" }
let wantGUI = CommandLine.arguments.contains("--gui") || (userFlags.isEmpty && !CommandLine.arguments.contains("--cli"))
if wantGUI {
    if #available(macOS 13.0, *) {
        runMenuBarApp(config: Config.parse())
    } else {
        FileHandle.standardError.write("The menu-bar app requires macOS 13+.\n".data(using: .utf8)!)
        exit(1)
    }
}

var config = Config.parse()
if let err = config.validate() {
    FileHandle.standardError.write("Config error: \(err)\n".data(using: .utf8)!)
    exit(2)
}

// In-app login: exchange username/password for Cognito tokens via a
// dependency-free SRP handshake (same USER_SRP_AUTH flow the web UI uses; see
// SRP.swift for why this rather than Amplify). Runs synchronously before we
// open the socket. Keeps the pasted --token path working — login only happens
// when --username is given and no --token was supplied.
if config.wantsLogin {
    var password = config.password
    if password.isEmpty {
        // Prompt without echoing (getpass) when running interactively.
        if let p = String(validatingUTF8: getpass("Password for \(config.username): ")) {
            password = p
        }
    }
    if password.isEmpty {
        FileHandle.standardError.write("No password provided for --username \(config.username).\n".data(using: .utf8)!)
        exit(2)
    }
    let sem = DispatchSemaphore(value: 0)
    var loginError: Error?
    print("Signing in as \(config.username) via Cognito SRP…")
    Task {
        do {
            let tokens = try await SRP.login(
                username: config.username, password: password,
                poolId: config.userPoolId, clientId: config.clientId,
                region: config.effectiveRegion)
            config.accessToken = tokens.accessToken
            config.idToken = tokens.idToken
            print("✓ Signed in — got Cognito tokens (access/id\(tokens.refreshToken.isEmpty ? "" : "/refresh"))")
            if CommandLine.arguments.contains("--login-only") {
                // Print token metadata (not the token) so we can verify the login
                // worked in isolation, without opening audio/socket.
                let parts = tokens.accessToken.split(separator: ".")
                if parts.count == 3,
                   let payload = base64urlDecode(String(parts[1])),
                   let obj = try? JSONSerialization.jsonObject(with: payload) as? [String: Any] {
                    let user = obj["username"] ?? obj["sub"] ?? "?"
                    let exp = (obj["exp"] as? Double).map { Date(timeIntervalSince1970: $0) }
                    print("  token user: \(user)")
                    if let exp = exp { print("  expires:    \(exp)") }
                }
                print("  access token length: \(tokens.accessToken.count) chars")
                exit(0)
            }
        } catch {
            loginError = error
        }
        sem.signal()
    }
    sem.wait()
    if let e = loginError {
        FileHandle.standardError.write("Login failed: \(e)\n".data(using: .utf8)!)
        exit(1)
    }
}

guard #available(macOS 13.0, *) else {
    FileHandle.standardError.write("Requires macOS 13 (Ventura) or later for ScreenCaptureKit audio.\n".data(using: .utf8)!)
    exit(1)
}

print("LMA native audio client (prototype)")
print("  endpoint : \(config.endpoint)")
print("  callId   : \(config.callId)")
print("  rate     : \(config.sampleRate) Hz, 2ch interleaved 16-bit PCM")
print("  channels : ch0=meeting(system audio)  ch1=mic")
print("")

let socket = TranscriberSocket(config: config)
let mixer = StereoMixer(sampleRate: config.sampleRate) { chunk in
    socket.sendPCM(chunk)
}
let capture = AudioCapture(mixer: mixer, targetRate: config.sampleRate)

// Optional desktop-video lane (--video 1 [--video-source display:<id>]):
// second SCK stream + second websocket, fMP4 segments. Best-effort — failures
// never affect the audio stream.
var videoSocket: VideoSocket?
var videoCapture: VideoCapture?
let audioStartDate = Date()
if config.videoEnabled {
    let vSock = VideoSocket(config: config)
    let vCap = VideoCapture(sourceID: config.videoSourceID) { [weak vSock] seg in
        vSock?.sendSegment(seg)
    }
    vCap.onFirstFrame = { [weak vSock, weak vCap] in
        if let first = vCap?.firstFrameDate {
            vSock?.videoTimeOffsetMs = max(0, Int(first.timeIntervalSince(audioStartDate) * 1000))
        }
    }
    vSock.onOverflow = {
        print("✗ screen video stopped (connection lost); audio unaffected")
        Task { await videoCapture?.stop() }
        videoCapture = nil
        videoSocket = nil
    }
    videoSocket = vSock
    videoCapture = vCap
}

// Reflect live WS connection state in the meter so "sent" isn't misleading
// while the socket is down (during reconnect audio is buffered, not sent).
socket.onStateChange = { connected in mixer.setConnected(connected) }

// Optional: tee the exact streamed PCM to a local stereo WAV for offline
// verification (per-channel RMS proves ch0=system / ch1=mic, not swapped).
var debugTee: WavTee?
if !config.debugWavPath.isEmpty {
    debugTee = WavTee(path: config.debugWavPath, sampleRate: config.sampleRate, channels: 2)
    mixer.tee = debugTee
    if debugTee != nil { print("  debug-wav: \(config.debugWavPath) (ch0=Left=system, ch1=Right=mic)") }
}

// Clean shutdown on Ctrl-C: send END, close socket, stop capture.
let sigHandler = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
signal(SIGINT, SIG_IGN)
sigHandler.setEventHandler {
    print("\nStopping…")
    capture.stop()
    mixer.stop()
    debugTee?.finish()
    socket.beginClose()   // mark intentional first so teardown stays quiet
    socket.sendEnd()
    Task {
        // Flush the encoder's final fMP4 segments, then END_VIDEO.
        await videoCapture?.stop()
        videoSocket?.beginClose()
        videoSocket?.sendEnd()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
            videoSocket?.close()
            socket.close()
            exit(0)
        }
    }
}
sigHandler.resume()

// Interactive controls when attached to a TTY: 'm' toggles mic mute, 'q' quits.
// (When output is redirected to a file there's no TTY, so this is skipped.)
var micMuted = false

// Signal-based mic-mute toggle: `kill -USR1 <pid>` flips mute. Works whether or
// not there's a TTY (e.g. when launched from a script or as a background app),
// and is the control the eventual menu-bar UI would drive internally.
let muteSignal = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: .main)
signal(SIGUSR1, SIG_IGN)
muteSignal.setEventHandler {
    micMuted.toggle()
    mixer.setMicMuted(micMuted)
}
muteSignal.resume()

if isatty(STDIN_FILENO) != 0 {
    print("  controls : press 'm' + Enter to toggle mic mute, 'q' + Enter to stop (or Ctrl-C)")
    // Read raw bytes from stdin on a dispatch source (do NOT combine with the
    // blocking readLine() — that starves the source and drops keystrokes).
    let stdinSrc = DispatchSource.makeReadSource(fileDescriptor: STDIN_FILENO, queue: .main)
    stdinSrc.setEventHandler {
        var buf = [UInt8](repeating: 0, count: 64)
        let n = read(STDIN_FILENO, &buf, buf.count)
        guard n > 0 else { return }
        for b in buf[0..<n] {
            switch b {
            case UInt8(ascii: "m"), UInt8(ascii: "M"):
                micMuted.toggle()
                mixer.setMicMuted(micMuted)
            case UInt8(ascii: "q"), UInt8(ascii: "Q"):
                raise(SIGINT)
            default:
                break // ignore newlines/other keys
            }
        }
    }
    stdinSrc.resume()
}

socket.connect()      // opens WS; sends START in didOpenWithProtocol
mixer.start()         // begins 100ms flush cadence

Task {
    do {
        try await capture.start()
        if let vSock = videoSocket, let vCap = videoCapture {
            vSock.connect()
            do {
                try await vCap.start()
            } catch {
                FileHandle.standardError.write("Screen video failed to start (audio unaffected): \(error)\n".data(using: .utf8)!)
                vSock.close()
                videoSocket = nil
                videoCapture = nil
            }
        }
        print("\nStreaming… press Ctrl-C to stop.\n")
    } catch {
        FileHandle.standardError.write("Capture failed to start: \(error)\n".data(using: .utf8)!)
        FileHandle.standardError.write("If this is a permissions error, grant Screen Recording + Microphone in System Settings › Privacy & Security, then re-run.\n".data(using: .utf8)!)
        exit(1)
    }
}

// Keep the CLI alive; audio callbacks and the WS run on their own queues.
RunLoop.main.run()
