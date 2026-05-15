# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Generate the default fixture WAV used by the load-simulator drivers.

We avoid shipping binary audio in git — this script generates a small
stereo PCM16 clip consisting of two alternating tone-bursts so that
Amazon Transcribe produces non-empty (if nonsensical) segments. For
meaningful transcripts in production benchmarks, supply your own WAV via
``--wav <path>`` on the driver CLI.

Usage::

    python -m lma_load.fixtures.generate [--output stereo-16k-30s.wav]
    python -m lma_load.fixtures.generate --duration 60 --sample-rate 16000
"""

from __future__ import annotations

import argparse
import math
import struct
import wave
from pathlib import Path


def generate(
    path: Path,
    duration_s: float = 30.0,
    sample_rate: int = 16000,
    freq_left: float = 220.0,
    freq_right: float = 330.0,
    amplitude: float = 0.25,
) -> None:
    """Write a stereo PCM16 WAV with alternating L/R tone bursts."""
    n_samples = int(duration_s * sample_rate)
    amp_int = int(amplitude * 32767)

    # 1-second bursts, alternating channels.
    burst_samples = sample_rate

    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)   # 16-bit
        wf.setframerate(sample_rate)
        frames = bytearray(n_samples * 2 * 2)  # 2 channels × 2 bytes
        for i in range(n_samples):
            cycle = (i // burst_samples) % 2
            # cycle 0 → left channel on, right off; cycle 1 → reverse.
            t = i / sample_rate
            if cycle == 0:
                left = int(amp_int * math.sin(2 * math.pi * freq_left * t))
                right = 0
            else:
                left = 0
                right = int(amp_int * math.sin(2 * math.pi * freq_right * t))
            struct.pack_into("<hh", frames, i * 4, left, right)
        wf.writeframes(bytes(frames))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "stereo-16k-30s.wav",
        help="Output WAV path (default: shipped fixture location).",
    )
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--sample-rate", type=int, default=16000)
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    generate(args.output, duration_s=args.duration, sample_rate=args.sample_rate)
    print(f"Wrote fixture: {args.output}")
