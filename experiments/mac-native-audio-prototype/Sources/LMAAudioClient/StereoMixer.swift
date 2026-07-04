import Foundation

/// Combines two independent mono float sources (meeting audio + mic) that arrive
/// on separate threads at their own cadence, and emits interleaved 16-bit LE PCM
/// chunks matching the LMA transcriber wire format:
///
///   [ch0_s0][ch1_s0][ch0_s1][ch1_s1]...   (ch0 = meeting, ch1 = mic)
///
/// Both inputs MUST already be resampled to the same target sample rate (done by
/// the capture layer via AVAudioConverter). This class just buffers each channel
/// and, on a fixed cadence, drains the number of frames both channels can supply.
///
/// NOTE: unlike the browser worklet (recording-processor.js) we start the
/// interleave at index 0 — the browser version starts at index 1 and drops the
/// first mic sample. Do not replicate that bug.
final class StereoMixer {
    private let lock = NSLock()
    private var meeting: [Float] = []   // ch0
    private var mic: [Float] = []       // ch1
    private let onChunk: (Data) -> Void
    private var timer: DispatchSourceTimer?
    private let sampleRate: Int
    private let queue = DispatchQueue(label: "lma.mixer")

    init(sampleRate: Int, onChunk: @escaping (Data) -> Void) {
        self.sampleRate = sampleRate
        self.onChunk = onChunk
    }

    func appendMeeting(_ samples: [Float]) {
        lock.lock(); meeting.append(contentsOf: samples); lock.unlock()
    }

    func appendMic(_ samples: [Float]) {
        lock.lock(); mic.append(contentsOf: samples); lock.unlock()
    }

    /// Flush interleaved PCM every ~100 ms (matches the server's BlockStream size).
    func start() {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + .milliseconds(100), repeating: .milliseconds(100))
        t.setEventHandler { [weak self] in self?.drain() }
        t.resume()
        timer = t
    }

    func stop() {
        timer?.cancel()
        timer = nil
        drain() // flush remainder
    }

    private func drain() {
        lock.lock()
        // Drain the number of frames BOTH channels can supply so they stay aligned.
        // If one source stalls, we hold back rather than skewing the channels.
        let n = min(meeting.count, mic.count)
        guard n > 0 else { lock.unlock(); return }
        let m = Array(meeting.prefix(n))
        let k = Array(mic.prefix(n))
        meeting.removeFirst(n)
        mic.removeFirst(n)
        lock.unlock()

        var out = Data(capacity: n * 4) // 2 channels * 2 bytes
        out.withUnsafeMutableBytes { _ in } // no-op; build via append below
        var buf = [Int16](repeating: 0, count: n * 2)
        for i in 0..<n {
            buf[i * 2]     = Self.floatToInt16(m[i]) // ch0 meeting
            buf[i * 2 + 1] = Self.floatToInt16(k[i]) // ch1 mic
        }
        buf.withUnsafeBytes { raw in out.append(contentsOf: raw) }
        onChunk(out)
    }

    /// Match the browser's clamping (floatTo16BitPCM): [-1,1] → Int16.
    @inline(__always)
    static func floatToInt16(_ s: Float) -> Int16 {
        let clamped = max(-1.0, min(1.0, s))
        return Int16(clamped < 0 ? clamped * 32768.0 : clamped * 32767.0)
    }
}
