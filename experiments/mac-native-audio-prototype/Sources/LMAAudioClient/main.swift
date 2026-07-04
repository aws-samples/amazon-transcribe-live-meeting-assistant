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
//   swift run LMAAudioClient \
//     --endpoint wss://<cloudfront-domain>/api/v1/ws \
//     --token <cognito-access-token> \
//     --id-token <cognito-id-token> \
//     --call-id "Test meeting"
//
// (Or set LMA_WS_ENDPOINT / LMA_ACCESS_TOKEN / LMA_ID_TOKEN env vars.)

let config = Config.parse()
if let err = config.validate() {
    FileHandle.standardError.write("Config error: \(err)\n".data(using: .utf8)!)
    exit(2)
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

// Clean shutdown on Ctrl-C: send END, close socket, stop capture.
let sigHandler = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
signal(SIGINT, SIG_IGN)
sigHandler.setEventHandler {
    print("\nStopping…")
    capture.stop()
    mixer.stop()
    socket.sendEnd()
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
        socket.close()
        exit(0)
    }
}
sigHandler.resume()

socket.connect()      // opens WS; sends START in didOpenWithProtocol
mixer.start()         // begins 100ms flush cadence

Task {
    do {
        try await capture.start()
        print("\nStreaming… press Ctrl-C to stop.\n")
    } catch {
        FileHandle.standardError.write("Capture failed to start: \(error)\n".data(using: .utf8)!)
        FileHandle.standardError.write("If this is a permissions error, grant Screen Recording + Microphone in System Settings › Privacy & Security, then re-run.\n".data(using: .utf8)!)
        exit(1)
    }
}

// Keep the CLI alive; audio callbacks and the WS run on their own queues.
RunLoop.main.run()
