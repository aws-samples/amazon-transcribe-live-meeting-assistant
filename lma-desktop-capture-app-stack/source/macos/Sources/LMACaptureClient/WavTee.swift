import Foundation

/// Debug aid: tee the exact interleaved 16-bit stereo PCM we stream to the
/// server into a local .wav file. Lets us verify OFFLINE that the two channels
/// are correct (ch0/Left = system/meeting audio, ch1/Right = mic) and not
/// swapped, skewed, or garbled — by measuring per-channel RMS on the captured
/// file rather than trusting our ears.
///
/// Writes a canonical 44-byte PCM WAV header up front with placeholder sizes,
/// appends raw frames as they stream, then patches the RIFF/data sizes on stop.
/// Not part of the streaming path's correctness — purely a diagnostic.
final class WavTee {
    private let handle: FileHandle
    private let sampleRate: Int
    private let channels: Int
    private var dataBytes: UInt32 = 0
    private let lock = NSLock()
    private var closed = false

    init?(path: String, sampleRate: Int, channels: Int = 2) {
        self.sampleRate = sampleRate
        self.channels = channels
        FileManager.default.createFile(atPath: path, contents: nil)
        guard let h = FileHandle(forWritingAtPath: path) else {
            FileHandle.standardError.write("WavTee: cannot open \(path)\n".data(using: .utf8)!)
            return nil
        }
        self.handle = h
        h.write(Self.header(sampleRate: sampleRate, channels: channels, dataBytes: 0))
    }

    func append(_ pcm: Data) {
        lock.lock(); defer { lock.unlock() }
        guard !closed else { return }
        handle.write(pcm)
        dataBytes &+= UInt32(pcm.count)
    }

    /// Patch header sizes and close.
    func finish() {
        lock.lock(); defer { lock.unlock() }
        guard !closed else { return }
        closed = true
        let header = Self.header(sampleRate: sampleRate, channels: channels, dataBytes: dataBytes)
        try? handle.seek(toOffset: 0)
        handle.write(header)
        try? handle.close()
    }

    private static func header(sampleRate: Int, channels: Int, dataBytes: UInt32) -> Data {
        let bitsPerSample: UInt16 = 16
        let byteRate = UInt32(sampleRate * channels * 2)
        let blockAlign = UInt16(channels * 2)
        var d = Data()
        func u32(_ v: UInt32) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 4)) }
        func u16(_ v: UInt16) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 2)) }
        d.append("RIFF".data(using: .ascii)!)
        u32(36 &+ dataBytes)          // ChunkSize
        d.append("WAVE".data(using: .ascii)!)
        d.append("fmt ".data(using: .ascii)!)
        u32(16)                       // Subchunk1Size (PCM)
        u16(1)                        // AudioFormat = PCM
        u16(UInt16(channels))
        u32(UInt32(sampleRate))
        u32(byteRate)
        u16(blockAlign)
        u16(bitsPerSample)
        d.append("data".data(using: .ascii)!)
        u32(dataBytes)                // Subchunk2Size
        return d
    }
}
