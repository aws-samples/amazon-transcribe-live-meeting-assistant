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

    /// Optional debug tee: the exact interleaved PCM we stream is also written
    /// here so we can verify offline that ch0/Left = system audio and ch1/Right
    /// = mic (not swapped/skewed/garbled) by measuring per-channel RMS.
    var tee: WavTee?
    /// When true, ch1 (mic) is zeroed before interleaving. Toggle via setMicMuted.
    private var micMuted = false
    /// When true, ch0 (system/meeting audio) is zeroed. Toggle via setMeetingMuted.
    private var meetingMuted = false
    /// When true, drain() consumes+discards buffered audio and sends nothing, so
    /// the WS stays open but no PCM flows (pause). Toggle via setPaused.
    private var paused = false
    /// Live WS connection state (for the meter line). While false, produced PCM
    /// is buffered by the socket, not actually sent.
    private var connected = false

    /// Optional per-second level callback for a UI (RMS+peak in [0,1] per channel,
    /// plus link/mute/pause state). Fired on the mixer queue; hop to main in the UI.
    var onLevels: ((_ meetingRMS: Float, _ micRMS: Float, _ connected: Bool, _ paused: Bool) -> Void)?

    // --- Level metering (verifies both channels are actually live) ----------
    // Accumulated per drain; summarized ~1×/sec to stdout as a VU-style bar.
    private var meterMeetingPeak: Float = 0
    private var meterMicPeak: Float = 0
    private var meterMeetingSumSq: Double = 0
    private var meterMicSumSq: Double = 0
    private var meterFrames: Int = 0
    private var bytesSent: Int = 0
    private var meterTick = 0
    /// Total interleaved frames emitted (each = one ch0+ch1 sample pair).
    private(set) var framesEmitted: Int = 0

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

    /// Mute/unmute the mic channel (ch1). When muted we still consume mic frames
    /// (so the channels stay time-aligned) but zero them before interleaving.
    func setMicMuted(_ muted: Bool) {
        lock.lock(); micMuted = muted; lock.unlock()
        print(muted ? "🔇 mic muted (ch1 will stream silence)" : "🎙️  mic unmuted")
    }

    /// Mute/unmute the meeting/system channel (ch0). Same approach as mic mute:
    /// frames are still consumed to keep alignment, but zeroed before interleave.
    func setMeetingMuted(_ muted: Bool) {
        lock.lock(); meetingMuted = muted; lock.unlock()
        print(muted ? "🔇 system audio muted (ch0 will stream silence)" : "🔊 system audio unmuted")
    }

    /// Pause/resume streaming. While paused we keep the WS open but drop buffered
    /// audio instead of sending it, so the server sees a gap rather than a stop.
    func setPaused(_ p: Bool) {
        lock.lock(); paused = p; lock.unlock()
        print(p ? "⏸  paused (no audio sent)" : "▶️  resumed")
    }

    var isMicMuted: Bool { lock.lock(); defer { lock.unlock() }; return micMuted }
    var isMeetingMuted: Bool { lock.lock(); defer { lock.unlock() }; return meetingMuted }
    var isPaused: Bool { lock.lock(); defer { lock.unlock() }; return paused }

    /// Update live WS connection state shown in the meter line.
    func setConnected(_ c: Bool) {
        lock.lock(); connected = c; lock.unlock()
    }

    /// Flush interleaved PCM every ~100 ms (matches the server's BlockStream size).
    func start() {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + .milliseconds(100), repeating: .milliseconds(100))
        var ticks = 0
        t.setEventHandler { [weak self] in
            guard let self = self else { return }
            self.drain()
            ticks += 1
            if ticks % 10 == 0 { self.logMeter() } // ~1×/sec
        }
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
        let micIsMuted = micMuted
        let meetingIsMuted = meetingMuted
        let isPaused = paused
        lock.unlock()

        // Paused: consume the buffered audio (already removed above) but send
        // nothing, so buffers don't grow unbounded and the server sees silence.
        if isPaused {
            onLevels?(0, 0, connected, true)
            return
        }

        var out = Data(capacity: n * 4) // 2 channels * 2 bytes
        var buf = [Int16](repeating: 0, count: n * 2)
        var mPeak: Float = 0, kPeak: Float = 0
        var mSum: Double = 0, kSum: Double = 0
        for i in 0..<n {
            let ms = meetingIsMuted ? 0 : m[i]  // system-mute: zero ch0, keep alignment
            let ks = micIsMuted ? 0 : k[i]      // mic-mute: zero ch1, keep alignment
            buf[i * 2]     = Self.floatToInt16(ms) // ch0 meeting
            buf[i * 2 + 1] = Self.floatToInt16(ks) // ch1 mic
            let ma = abs(ms), ka = abs(ks)
            if ma > mPeak { mPeak = ma }
            if ka > kPeak { kPeak = ka }
            mSum += Double(ms * ms); kSum += Double(ks * ks)
        }
        buf.withUnsafeBytes { raw in out.append(contentsOf: raw) }
        tee?.append(out)   // debug: mirror exact streamed PCM to local WAV

        // Fold this chunk into the metering accumulators (protected by lock so
        // the summary read is consistent).
        lock.lock()
        if mPeak > meterMeetingPeak { meterMeetingPeak = mPeak }
        if kPeak > meterMicPeak { meterMicPeak = kPeak }
        meterMeetingSumSq += mSum
        meterMicSumSq += kSum
        meterFrames += n
        bytesSent += out.count
        framesEmitted += n
        lock.unlock()

        onChunk(out)
    }

    /// Print a one-line VU meter for both channels ~1×/sec. Lets you confirm at
    /// a glance that ch0 (meeting/system) and ch1 (mic) are BOTH live and not
    /// swapped — silence on a channel here means that capture source is dead.
    private func logMeter() {
        lock.lock()
        let frames = meterFrames
        let mPeak = meterMeetingPeak, kPeak = meterMicPeak
        let mRms = frames > 0 ? (meterMeetingSumSq / Double(frames)).squareRoot() : 0
        let kRms = frames > 0 ? (meterMicSumSq / Double(frames)).squareRoot() : 0
        let sent = bytesSent
        let isConnected = connected
        let muted = micMuted
        meterMeetingPeak = 0; meterMicPeak = 0
        meterMeetingSumSq = 0; meterMicSumSq = 0; meterFrames = 0
        meterTick += 1
        let tick = meterTick
        lock.unlock()

        // Seconds of audio drained this interval (should be ~1.0 when healthy).
        let secs = Double(frames) / Double(sampleRate)
        let kb = Double(sent) / 1024.0
        let link = isConnected ? "● live" : "○ buffering(WS down)"
        let micTag = muted ? " [MUTED]" : ""
        // Feed a UI (menu-bar meters) if attached. Values already reflect mute
        // (muted channels contribute zero energy).
        onLevels?(Float(mRms), Float(kRms), isConnected, false)
        print(String(format: "[meter %3ds] %@ | ch0 meeting %@ rms %.3f peak %.3f | ch1 mic%@ %@ rms %.3f peak %.3f | %.2fs, %.0f KB",
                     tick, link,
                     Self.bar(mRms), mRms, mPeak,
                     micTag, Self.bar(kRms), kRms, kPeak,
                     secs, kb))
        if frames == 0 {
            print("           ⚠️  no audio drained this second — both channels must have samples to flush (check mic + system-audio permissions / that audio is playing)")
        }
    }

    /// Tiny 10-cell ASCII VU bar from an RMS level in [0,1].
    private static func bar(_ rms: Double) -> String {
        let cells = 10
        let lit = max(0, min(cells, Int((rms * 3.0) * Double(cells)))) // ×3 gain for visibility
        return "[" + String(repeating: "#", count: lit) + String(repeating: "-", count: cells - lit) + "]"
    }

    /// Match the browser's clamping (floatTo16BitPCM): [-1,1] → Int16.
    @inline(__always)
    static func floatToInt16(_ s: Float) -> Int16 {
        let clamped = max(-1.0, min(1.0, s))
        return Int16(clamped < 0 ? clamped * 32768.0 : clamped * 32767.0)
    }
}
