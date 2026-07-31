using System.IO;
using System.Text;

namespace LMA;

/// <summary>
/// Debug aid: tee the exact interleaved 16-bit stereo PCM we stream to the
/// server into a local .wav file. Lets us verify OFFLINE that the two channels
/// are correct (ch0/Left = system/meeting audio, ch1/Right = mic) and not
/// swapped, skewed, or garbled — by measuring per-channel RMS on the captured
/// file rather than trusting our ears.
///
/// Writes a canonical 44-byte PCM WAV header up front with placeholder sizes,
/// appends raw frames as they stream, then patches the RIFF/data sizes on stop.
/// Not part of the streaming path's correctness — purely a diagnostic.
///
/// Ported verbatim from macOS WavTee.swift.
/// </summary>
public sealed class WavTee
{
    private readonly FileStream _handle;
    private readonly int _sampleRate;
    private readonly int _channels;
    private uint _dataBytes;
    private readonly object _lock = new();
    private bool _closed;

    private WavTee(FileStream handle, int sampleRate, int channels)
    {
        _handle = handle;
        _sampleRate = sampleRate;
        _channels = channels;
    }

    /// <summary>Open a WAV tee; returns null (and logs) if the file can't be created.</summary>
    public static WavTee? Create(string path, int sampleRate, int channels = 2)
    {
        try
        {
            var h = new FileStream(path, FileMode.Create, FileAccess.ReadWrite);
            var tee = new WavTee(h, sampleRate, channels);
            var header = Header(sampleRate, channels, 0);
            h.Write(header, 0, header.Length);
            return tee;
        }
        catch (Exception e)
        {
            Console.Error.WriteLine($"WavTee: cannot open {path}: {e.Message}");
            return null;
        }
    }

    public void Append(byte[] pcm)
    {
        lock (_lock)
        {
            if (_closed) return;
            _handle.Write(pcm, 0, pcm.Length);
            _dataBytes += (uint)pcm.Length;
        }
    }

    /// <summary>Patch header sizes and close.</summary>
    public void Finish()
    {
        lock (_lock)
        {
            if (_closed) return;
            _closed = true;
            var header = Header(_sampleRate, _channels, _dataBytes);
            _handle.Seek(0, SeekOrigin.Begin);
            _handle.Write(header, 0, header.Length);
            _handle.Flush();
            _handle.Dispose();
        }
    }

    private static byte[] Header(int sampleRate, int channels, uint dataBytes)
    {
        ushort bitsPerSample = 16;
        uint byteRate = (uint)(sampleRate * channels * 2);
        ushort blockAlign = (ushort)(channels * 2);
        using var ms = new MemoryStream();
        void U32(uint v) => ms.Write(BitConverter.GetBytes(v), 0, 4);   // little-endian on x64
        void U16(ushort v) => ms.Write(BitConverter.GetBytes(v), 0, 2);
        void Ascii(string s) => ms.Write(Encoding.ASCII.GetBytes(s), 0, s.Length);

        Ascii("RIFF");
        U32(36u + dataBytes);          // ChunkSize
        Ascii("WAVE");
        Ascii("fmt ");
        U32(16);                       // Subchunk1Size (PCM)
        U16(1);                        // AudioFormat = PCM
        U16((ushort)channels);
        U32((uint)sampleRate);
        U32(byteRate);
        U16(blockAlign);
        U16(bitsPerSample);
        Ascii("data");
        U32(dataBytes);                // Subchunk2Size
        return ms.ToArray();
    }
}
