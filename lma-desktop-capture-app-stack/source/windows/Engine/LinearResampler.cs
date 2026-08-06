namespace LMA;

/// <summary>
/// Continuous-phase linear resampler for a single mono float stream, fed
/// block-by-block (push model). Maintains inter-block state (the last input
/// sample + fractional read position) so successive blocks resample seamlessly
/// with no clicks at block boundaries.
///
/// The macOS client uses AVAudioConverter (a higher-order resampler); here we
/// use linear interpolation, which is inaudible-quality for speech at the small
/// rate ratios in play (device 44.1/48 kHz → 48 kHz target) and keeps the code
/// dependency-light and deterministic. Passthrough when in == out.
/// </summary>
public sealed class LinearResampler
{
    private readonly int _inRate;
    private readonly int _outRate;
    private readonly double _outStep;   // input-time advance per output sample
    private float _prev;                // last input sample of the previous block (virtual index -1)
    private bool _primed;
    private double _nextOut;            // input-time (relative to this block's index 0) of the next output

    public LinearResampler(int inRate, int outRate)
    {
        _inRate = inRate;
        _outRate = outRate;
        _outStep = (double)inRate / outRate;
    }

    public float[] Process(float[] inp)
    {
        if (_inRate == _outRate) return inp;                 // passthrough
        if (inp.Length == 0) return Array.Empty<float>();

        if (!_primed)
        {
            _prev = inp[0];
            _primed = true;
            _nextOut = 0;
        }

        // Virtual indexing: index -1 → _prev, index j → inp[j]. Emit output while
        // we can interpolate between two available samples (i0 and i0+1 present).
        var outp = new List<float>((int)(inp.Length / _outStep) + 2);
        while (_nextOut < inp.Length - 1)
        {
            int i0 = (int)Math.Floor(_nextOut);
            double frac = _nextOut - i0;
            float a = (i0 == -1) ? _prev : inp[i0];
            float b = inp[i0 + 1];
            outp.Add((float)(a * (1 - frac) + b * frac));
            _nextOut += _outStep;
        }

        // Carry state to the next block: this block's last sample becomes the new
        // virtual index -1, and the read position rebases by the block length.
        _prev = inp[inp.Length - 1];
        _nextOut -= inp.Length;
        return outp.ToArray();
    }
}
