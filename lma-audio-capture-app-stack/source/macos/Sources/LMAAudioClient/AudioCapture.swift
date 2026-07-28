import Foundation
import AVFoundation
import ScreenCaptureKit
import CoreMedia
import AudioToolbox

/// Captures the two audio sources and feeds mono Float samples (resampled to the
/// target rate) into a StereoMixer.
///
///   • Meeting/system audio  → ScreenCaptureKit (macOS 13+). Captures ALL system
///     output regardless of which app plays it — this is what lets us transcribe
///     a NATIVE Zoom/Teams client, which the browser getDisplayMedia path cannot.
///   • Microphone            → AVAudioEngine input node.
///
/// ScreenCaptureKit requires the "Screen Recording" permission (even audio-only).
/// The microphone requires the "Microphone" permission. Both are prompted at
/// first use; see the plan doc for the Info.plist / TCC details.
@available(macOS 13.0, *)
final class AudioCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    private let mixer: StereoMixer
    private let targetRate: Double

    // System audio (SCK)
    private var scStream: SCStream?

    // Mic (AVAudioEngine)
    private let engine = AVAudioEngine()
    private var micConverter: AVAudioConverter?
    private lazy var targetFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32,
        sampleRate: targetRate,
        channels: 1,
        interleaved: false
    )!

    /// CoreAudio UID of the mic to use; empty = system default (Settings picker).
    private let micDeviceUID: String

    init(mixer: StereoMixer, targetRate: Int, micDeviceUID: String = "") {
        self.mixer = mixer
        self.targetRate = Double(targetRate)
        self.micDeviceUID = micDeviceUID
        super.init()
    }

    func start() async throws {
        applyMicDeviceSelection()
        try startMic()
        observeDeviceChanges()
        try await startSystemAudio()
    }

    /// Point AVAudioEngine's input AUHAL at the chosen device (Settings picker).
    /// No-op for "" (system default) or if the device is unplugged — both fall
    /// back to the default input, matching the pre-Settings behavior.
    private func applyMicDeviceSelection() {
        guard !micDeviceUID.isEmpty, var devId = MicDevices.deviceID(forUID: micDeviceUID) else {
            if !micDeviceUID.isEmpty {
                print("⚠ selected mic not connected; using system default input")
            }
            return
        }
        let au = engine.inputNode.audioUnit!
        let err = AudioUnitSetProperty(
            au, kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global, 0, &devId, UInt32(MemoryLayout<AudioDeviceID>.size))
        if err != noErr {
            print("⚠ couldn't select mic (\(err)); using system default input")
        }
    }

    func stop() {
        NotificationCenter.default.removeObserver(self)
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        scStream?.stopCapture { _ in }
    }

    // MARK: - Device changes (default input/output switched mid-meeting)

    /// AVAudioEngine posts `configurationChange` when the audio graph is
    /// invalidated — e.g. the user switches the default input (unplugs a
    /// headset) or output device. The installed tap goes silent until the
    /// engine is rebuilt, so we tear the tap down and reinstall it against the
    /// NEW input format. Mic samples briefly pause but channel alignment holds
    /// (the mixer only drains frames both channels can supply).
    private func observeDeviceChanges() {
        NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine, queue: nil
        ) { [weak self] _ in
            guard let self = self else { return }
            print("⟳ audio device/config changed — rebuilding mic tap")
            self.engine.inputNode.removeTap(onBus: 0)
            do {
                try self.startMic() // reinstalls tap against the new input format
            } catch {
                FileHandle.standardError.write("mic restart after device change failed: \(error)\n".data(using: .utf8)!)
            }
        }
    }

    // MARK: - Microphone via AVAudioEngine

    private func startMic() throws {
        let input = engine.inputNode
        // Re-read the CURRENT hardware input format each call so this also works
        // when re-invoked after a device change (new default input, new rate).
        let inputFormat = input.inputFormat(forBus: 0)
        // A 0-channel/0-rate format means the input device is momentarily absent
        // (e.g. mid-switch). Skip; the next configurationChange will retry.
        guard inputFormat.channelCount > 0, inputFormat.sampleRate > 0 else {
            FileHandle.standardError.write("mic input format not ready (\(inputFormat)); will retry on next device change\n".data(using: .utf8)!)
            return
        }
        micConverter = AVAudioConverter(from: inputFormat, to: targetFormat)

        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { [weak self] buffer, _ in
            guard let self = self, let conv = self.micConverter else { return }
            if let mono = self.convert(buffer, using: conv) {
                self.mixer.appendMic(mono)
            }
        }
        engine.prepare()
        if !engine.isRunning {
            try engine.start()
        }
        print("✓ Microphone capture started (input rate \(inputFormat.sampleRate) Hz → \(targetRate) Hz)")
    }

    // MARK: - System audio via ScreenCaptureKit

    private func startSystemAudio() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false,
                                                                           onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            throw NSError(domain: "LMA", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "No display available for SCK filter."])
        }
        // A content filter is required even though we only want audio.
        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])

        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = true
        cfg.sampleRate = Int(targetRate)   // SCK resamples system audio to this
        cfg.channelCount = 1               // ask for mono; we downmix if it gives us more
        cfg.excludesCurrentProcessAudio = true // don't capture our own output
        // Minimal video config (SCK still requires a video path on older OSes).
        cfg.width = 2
        cfg.height = 2
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let stream = SCStream(filter: filter, configuration: cfg, delegate: self)
        try stream.addStreamOutput(self, type: .audio,
                                   sampleHandlerQueue: DispatchQueue(label: "lma.sck.audio"))
        try await stream.startCapture()
        scStream = stream
        print("✓ System audio capture started (ScreenCaptureKit @ \(targetRate) Hz)")
    }

    // SCStreamOutput: system-audio sample buffers land here.
    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, CMSampleBufferDataIsReady(sampleBuffer) else { return }
        if let mono = Self.monoFloats(from: sampleBuffer) {
            mixer.appendMeeting(mono)
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write("SCK stopped with error: \(error)\n".data(using: .utf8)!)
    }

    // MARK: - Helpers

    /// Resample/downmix an AVAudioPCMBuffer to mono Float at the target rate.
    private func convert(_ buffer: AVAudioPCMBuffer, using conv: AVAudioConverter) -> [Float]? {
        let ratio = targetRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
        guard let out = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity) else { return nil }

        var fed = false
        var err: NSError?
        conv.convert(to: out, error: &err) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return buffer
        }
        if let err = err { FileHandle.standardError.write("mic convert error: \(err)\n".data(using: .utf8)!); return nil }
        guard let ch = out.floatChannelData else { return nil }
        return Array(UnsafeBufferPointer(start: ch[0], count: Int(out.frameLength)))
    }

    /// Extract mono Float samples from a CMSampleBuffer of PCM audio.
    /// SCK delivers float32; if multi-channel, average to mono.
    private static func monoFloats(from sb: CMSampleBuffer) -> [Float]? {
        guard let fmt = CMSampleBufferGetFormatDescription(sb),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fmt)?.pointee else { return nil }
        let channels = Int(asbd.mChannelsPerFrame)

        var blockBuffer: CMBlockBuffer?
        var abl = AudioBufferList()
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sb,
            bufferListSizeNeededOut: nil,
            bufferListOut: &abl,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBuffer
        )
        guard status == noErr, let data = abl.mBuffers.mData else { return nil }
        let byteCount = Int(abl.mBuffers.mDataByteSize)
        let floatCount = byteCount / MemoryLayout<Float>.size
        let ptr = data.assumingMemoryBound(to: Float.self)
        let interleaved = Array(UnsafeBufferPointer(start: ptr, count: floatCount))
        // Note: `blockBuffer` is returned +1 by …WithRetainedBlockBuffer, but as
        // an imported Swift `CMBlockBuffer?` it is ARC-managed and released when
        // this local goes out of scope — no manual CFRelease needed here.
        _ = blockBuffer

        if channels <= 1 { return interleaved }
        // Downmix interleaved multichannel → mono average.
        let frames = floatCount / channels
        var mono = [Float](repeating: 0, count: frames)
        for f in 0..<frames {
            var acc: Float = 0
            for c in 0..<channels { acc += interleaved[f * channels + c] }
            mono[f] = acc / Float(channels)
        }
        return mono
    }
}
