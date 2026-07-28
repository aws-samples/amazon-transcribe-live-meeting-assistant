import Foundation
import AVFoundation
import ScreenCaptureKit
import CoreMedia

/// Optional desktop-video capture: a SECOND ScreenCaptureKit stream (separate
/// from AudioCapture's) that captures the chosen display or window at a low
/// frame rate, H.264-encodes it via AVAssetWriter in fragmented-MP4 (CMAF)
/// mode, and hands each fMP4 segment to a callback for streaming.
///
/// Why a separate SCStream rather than adding .screen output to the audio
/// stream: the audio stream MUST keep its full-display filter (a window filter
/// would restrict captured system audio to that one app), while video should
/// capture exactly the display/window the user picked. Two streams keep the
/// two concerns independent — and video can fail/stop without touching audio.
///
/// Uses the SAME "Screen Recording" TCC permission the audio capture already
/// holds (ScreenCaptureKit requires it even for audio-only), so enabling video
/// adds no new permission prompt.
///
/// Encoder invariants the server/pipeline relies on:
///   • Fragmented MP4 (init segment first, then moof/mdat fragments) — the
///     server appends segments to a file that is playable/probe-able as it
///     grows, and ffmpeg muxes it with the call audio at call end.
///   • Keyframe at least every 2 seconds (segment interval forces this), so
///     future frame-extraction features can seek cheaply.
///   • Timestamps rebased to ZERO at the first captured frame — call-relative
///     alignment is carried separately as videoTimeOffsetMs on START_VIDEO.
@available(macOS 13.0, *)
final class VideoCapture: NSObject, SCStreamOutput, SCStreamDelegate, AVAssetWriterDelegate {

    /// A capturable video source (display or window) for the Settings picker.
    struct Source: Identifiable, Equatable {
        /// Stable-ish persisted id: "display:<displayID>" or "window:<windowID>".
        let id: String
        let name: String
    }

    /// Frames per second. Screen content changes slowly; 5 fps matches the
    /// Virtual Participant's recording and keeps CPU/bandwidth low.
    static let fps = 5
    /// Cap the longest edge; enough for legible slide text without huge bitrate.
    static let maxDimension = 1920
    /// Target H.264 bitrate. Screen content at 5 fps compresses very well.
    static let bitsPerSecond = 1_500_000

    private let onSegment: (Data) -> Void
    private let sourceID: String

    private var scStream: SCStream?
    private var writer: AVAssetWriter?
    private var writerInput: AVAssetWriterInput?
    private var sessionStarted = false
    private var firstFramePTS: CMTime = .invalid
    private(set) var firstFrameDate: Date?
    /// Called once when the first frame is captured (offset reporting).
    var onFirstFrame: (() -> Void)?

    private let encodeQueue = DispatchQueue(label: "lma.sck.video")
    private var stopped = false

    // Pause: the audio path CONSUMES AND DISCARDS samples while paused (its
    // recording is shorter than wall clock), so video must excise the same
    // span or everything after a pause is misaligned in the muxed MP4. While
    // paused we drop frames; on resume we subtract the accumulated pause time
    // from every subsequent PTS so the video timeline matches the audio's.
    private var paused = false
    private var pauseBegan: CMTime = .invalid
    private var pausedTotal: CMTime = .zero

    /// Mirror the audio mixer's pause state (called from the controller).
    /// The paused span itself is measured in capture-clock time by the frame
    /// handler: it opens at the first dropped frame and closes at the first
    /// post-resume frame.
    func setPaused(_ p: Bool) {
        encodeQueue.async { [self] in paused = p }
    }

    init(sourceID: String, onSegment: @escaping (Data) -> Void) {
        self.sourceID = sourceID
        self.onSegment = onSegment
        super.init()
    }

    // MARK: - Source enumeration (Settings picker)

    /// List capturable sources beyond the default: additional displays (the
    /// main display is the picker's built-in "Entire screen" = empty id), then
    /// on-screen windows that have a title (untitled/system chrome windows are
    /// noise, not meeting content).
    static func listSources() async -> [Source] {
        guard let content = try? await SCShareableContent.excludingDesktopWindows(
            true, onScreenWindowsOnly: true) else { return [] }
        var out: [Source] = []
        for (i, d) in content.displays.enumerated().dropFirst() {
            out.append(Source(id: "display:\(d.displayID)", name: "Display \(i + 1)"))
        }
        for w in content.windows {
            guard let title = w.title, !title.isEmpty,
                  let app = w.owningApplication, !app.applicationName.isEmpty,
                  w.frame.width >= 200, w.frame.height >= 150 // skip tiny utility windows
            else { continue }
            out.append(Source(id: "window:\(w.windowID)",
                              name: "\(app.applicationName) — \(title)"))
        }
        return out
    }

    // MARK: - Lifecycle

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false)

        // Resolve the persisted source id; fall back to the main display when
        // it is gone (window closed, display unplugged, stale id after reboot).
        var filter: SCContentFilter
        var captureSize: CGSize
        if sourceID.hasPrefix("window:"),
           let wid = UInt32(sourceID.dropFirst("window:".count)),
           let win = content.windows.first(where: { $0.windowID == wid }) {
            filter = SCContentFilter(desktopIndependentWindow: win)
            captureSize = win.frame.size
        } else {
            var display = content.displays.first
            if sourceID.hasPrefix("display:"),
               let did = UInt32(sourceID.dropFirst("display:".count)),
               let d = content.displays.first(where: { $0.displayID == did }) {
                display = d
            } else if !sourceID.isEmpty && !sourceID.hasPrefix("display:") {
                print("⚠ video source '\(sourceID)' not found; recording the main display")
            }
            guard let d = display else {
                throw NSError(domain: "LMA", code: 2, userInfo: [
                    NSLocalizedDescriptionKey: "No display available for video capture."])
            }
            filter = SCContentFilter(display: d, excludingApplications: [], exceptingWindows: [])
            captureSize = CGSize(width: d.width, height: d.height)
        }

        // Scale to cap the longest edge; encoder wants even dimensions.
        let scale = min(1, CGFloat(Self.maxDimension) / max(captureSize.width, captureSize.height, 1))
        let w = max(2, Int(captureSize.width * scale) & ~1)
        let h = max(2, Int(captureSize.height * scale) & ~1)

        let cfg = SCStreamConfiguration()
        cfg.capturesAudio = false            // audio is AudioCapture's job
        cfg.width = w
        cfg.height = h
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(Self.fps))
        cfg.queueDepth = 5
        cfg.showsCursor = true
        cfg.pixelFormat = kCVPixelFormatType_32BGRA

        try startWriter(width: w, height: h)

        let stream = SCStream(filter: filter, configuration: cfg, delegate: self)
        try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: encodeQueue)
        try await stream.startCapture()
        scStream = stream
        print("✓ Screen video capture started (\(w)x\(h) @ \(Self.fps) fps → fMP4)")
    }

    /// Stop capture and finish the writer, flushing the final segment(s).
    func stop() async {
        encodeQueue.sync { stopped = true }
        if let stream = scStream {
            try? await stream.stopCapture()
            scStream = nil
        }
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            encodeQueue.async { [self] in
                guard let writer = writer, writer.status == .writing else {
                    cont.resume(); return
                }
                writerInput?.markAsFinished()
                writer.finishWriting {
                    cont.resume()
                }
            }
        }
        writer = nil
        writerInput = nil
        print("✓ Screen video capture stopped")
    }

    // MARK: - AVAssetWriter (fragmented MP4)

    private func startWriter(width: Int, height: Int) throws {
        // CMAF fragmented-MP4 profile: segments are delivered to the delegate
        // instead of a file. Every segment boundary is a keyframe, so the
        // 2-second interval is also our max keyframe interval.
        let writer = AVAssetWriter(contentType: .mpeg4Movie)
        writer.outputFileTypeProfile = .mpeg4CMAFCompliant
        writer.preferredOutputSegmentInterval = CMTime(seconds: 2, preferredTimescale: 1)
        writer.initialSegmentStartTime = .zero
        writer.delegate = self

        let settings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: Self.bitsPerSecond,
                AVVideoMaxKeyFrameIntervalKey: 2 * Self.fps,        // ≤2s in frames
                AVVideoMaxKeyFrameIntervalDurationKey: 2.0,          // ≤2s in time
                AVVideoProfileLevelKey: AVVideoProfileLevelH264MainAutoLevel,
                AVVideoAllowFrameReorderingKey: false,               // no B-frames
            ],
        ]
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        input.expectsMediaDataInRealTime = true
        guard writer.canAdd(input) else {
            throw NSError(domain: "LMA", code: 3, userInfo: [
                NSLocalizedDescriptionKey: "AVAssetWriter rejected the H.264 video input."])
        }
        writer.add(input)
        guard writer.startWriting() else {
            throw writer.error ?? NSError(domain: "LMA", code: 4, userInfo: [
                NSLocalizedDescriptionKey: "AVAssetWriter failed to start."])
        }
        self.writer = writer
        self.writerInput = input
    }

    /// fMP4 segments (init first, then media fragments) → the streaming callback.
    func assetWriter(_ writer: AVAssetWriter,
                     didOutputSegmentData segmentData: Data,
                     segmentType: AVAssetSegmentType,
                     segmentReport: AVAssetSegmentReport?) {
        onSegment(segmentData)
    }

    // MARK: - SCStreamOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .screen, !stopped,
              CMSampleBufferDataIsReady(sampleBuffer),
              let writer = writer, let input = writerInput else { return }

        // Skip idle/incomplete frames (SCK emits status attachments per frame).
        if let atts = CMSampleBufferGetSampleAttachmentsArray(sampleBuffer, createIfNecessary: false)
            as? [[SCStreamFrameInfo: Any]],
           let statusRaw = atts.first?[.status] as? Int,
           let status = SCFrameStatus(rawValue: statusRaw),
           status != .complete {
            return
        }

        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)

        // Paused: drop the frame, but remember when the pause began (in
        // capture-clock time) so the span can be excised from the timeline.
        if paused {
            if !pauseBegan.isValid { pauseBegan = pts }
            return
        }
        if pauseBegan.isValid {
            // First frame after a resume: bank the paused span.
            pausedTotal = CMTimeAdd(pausedTotal, CMTimeSubtract(pts, pauseBegan))
            pauseBegan = .invalid
        }

        if !sessionStarted {
            guard writer.status == .writing else { return }
            firstFramePTS = pts
            firstFrameDate = Date()
            writer.startSession(atSourceTime: .zero)
            sessionStarted = true
            DispatchQueue.main.async { [weak self] in self?.onFirstFrame?() }
        }

        guard input.isReadyForMoreMediaData else { return } // drop frame under pressure

        // Rebase timestamps: media time starts at zero (call-relative
        // alignment is reported separately via videoTimeOffsetMs) and paused
        // spans are excised — matching the audio recording, which also skips
        // paused time.
        var timing = CMSampleTimingInfo(
            duration: CMTime(value: 1, timescale: CMTimeScale(Self.fps)),
            presentationTimeStamp: CMTimeSubtract(CMTimeSubtract(pts, firstFramePTS), pausedTotal),
            decodeTimeStamp: .invalid)
        var rebased: CMSampleBuffer?
        let status = CMSampleBufferCreateCopyWithNewTiming(
            allocator: kCFAllocatorDefault, sampleBuffer: sampleBuffer,
            sampleTimingEntryCount: 1, sampleTimingArray: &timing,
            sampleBufferOut: &rebased)
        guard status == noErr, let rb = rebased else { return }
        if !input.append(rb) {
            if writer.status == .failed {
                FileHandle.standardError.write(
                    "video encoder failed: \(writer.error.map { "\($0)" } ?? "unknown")\n".data(using: .utf8)!)
                stopped = true
            }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write("SCK video stream stopped with error: \(error)\n".data(using: .utf8)!)
    }
}
