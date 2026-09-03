# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The developer calibration tool behind every speakerThreshold in catalog.json.

Pure statistics and audio handling; the embedder is never loaded here."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest
from scripts.calibrate import (
    SAMPLE_RATE,
    Embedded,
    derive_operating_point,
    dominant_segments,
    percentile,
    read_stereo_wav,
    resample_pcm16,
    spread,
)


def _vec(angle: float) -> tuple[float, ...]:
    """Unit vector in the plane; cosine between two is cos(angle difference)."""
    return (math.cos(angle), math.sin(angle), 0.0)


def _cluster(channel: int, centre: float, jitter: float, count: int, duration: float = 5.0):
    return [
        Embedded(channel, duration, _vec(centre + jitter * ((i % 3) - 1)))
        for i in range(count)
    ]


def test_percentile_uses_the_nearest_rank_rule_the_shipped_thresholds_used() -> None:
    values = [0.1, 0.2, 0.3, 0.4, 0.5]
    assert percentile(values, 0.5) == 0.3
    assert percentile(values, 0.95) == 0.5
    assert percentile(values, 0.05) == 0.1
    assert math.isnan(percentile([], 0.5))


def test_two_well_separated_speakers_yield_a_midpoint_threshold() -> None:
    # Same-speaker pairs score ~cos(0.1)=0.995; different-speaker pairs ~cos(1.4)=0.17.
    segments = _cluster(0, 0.0, 0.05, 5) + _cluster(1, 1.4, 0.05, 5)
    point = derive_operating_point(segments)

    assert point.confidence == "good"
    assert point.speaker_threshold is not None
    assert point.different_speaker_max < point.speaker_threshold < point.same_speaker_p5
    # Midpoint of the gap, to three decimals.
    expected = round(point.different_speaker_p95 + point.separation * 0.5, 3)
    assert point.speaker_threshold == expected


def test_overlapping_distributions_refuse_to_return_a_number() -> None:
    # Both channels drawn from the same spread of directions: no gap exists.
    segments = _cluster(0, 0.0, 0.8, 6) + _cluster(1, 0.4, 0.8, 6)
    point = derive_operating_point(segments)

    assert point.confidence == "unusable"
    assert point.speaker_threshold is None
    assert any("overlap" in note for note in point.notes)


def test_too_few_pairs_is_unusable_rather_than_a_guess() -> None:
    point = derive_operating_point([Embedded(0, 5.0, _vec(0.0)), Embedded(1, 5.0, _vec(1.5))])
    assert point.confidence == "unusable"
    assert point.speaker_threshold is None


def test_the_threshold_clears_the_highest_observed_different_speaker_pair() -> None:
    # Two tight clusters plus one stray member on each side. The strays' cross pair
    # (cos 0.7 = 0.765) is a single different-speaker score far above the p95 (0.54),
    # so the midpoint (~0.75) would sit BELOW an observed different-speaker pair.
    same = _cluster(0, 0.0, 0.01, 8) + [Embedded(0, 5.0, _vec(0.3))]
    other = _cluster(1, 1.3, 0.01, 8) + [Embedded(1, 5.0, _vec(1.0))]
    point = derive_operating_point(same + other)

    assert point.speaker_threshold is not None
    assert point.speaker_threshold > point.different_speaker_max
    assert any(note.startswith("Raised above") for note in point.notes)


def test_short_utterances_that_embed_worse_produce_a_minimum_utterance_floor() -> None:
    long = [Embedded(0, 6.0, _vec(0.02 * ((i % 3) - 1))) for i in range(6)]
    short = [Embedded(0, 1.5, _vec(0.6 + 0.02 * ((i % 3) - 1))) for i in range(4)]
    other = _cluster(1, 2.0, 0.02, 5)
    point = derive_operating_point(long + short + other)

    assert point.min_segment_ms == 2500
    assert any("inherit the current speaker" in note for note in point.notes)


def _tone(seconds: float, amplitude: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    n = int(seconds * sample_rate)
    samples = (int(amplitude * math.sin(2 * math.pi * 220 * i / sample_rate)) for i in range(n))
    return struct.pack("<" + "h" * n, *samples)


def _silence(seconds: float, sample_rate: int = SAMPLE_RATE) -> bytes:
    return bytes(int(seconds * sample_rate) * 2)


def test_dominant_segments_attribute_speech_to_the_louder_channel_only() -> None:
    # ch0 speaks for 3s, both talk over each other for 2s, then ch1 speaks for 3s.
    ch0 = _tone(3, 8000) + _tone(2, 8000) + _silence(3)
    ch1 = _silence(3) + _tone(2, 8000) + _tone(3, 8000)

    first = dominant_segments(ch0, ch1, 0)
    second = dominant_segments(ch1, ch0, 1)

    assert len(first) == 1
    assert abs(first[0].start_sec - 0.0) < 0.05 and abs(first[0].end_sec - 3.0) < 0.35
    assert len(second) == 1 and abs(second[0].start_sec - 5.0) < 0.05
    # Cross-talk (3-5s) is in neither: it would corrupt both distributions.
    assert first[0].end_sec <= 3.35 and second[0].start_sec >= 4.95


def test_dominant_segments_ignore_a_channel_that_never_speaks() -> None:
    assert dominant_segments(_silence(4), _tone(4, 8000), 0) == []


def test_spread_samples_across_the_whole_recording() -> None:
    items = [Embedded(0, 1.0, _vec(0.0)) for _ in range(10)]
    picked = spread(items, 4)  # type: ignore[arg-type]
    assert len(picked) == 4
    assert spread(items[:2], 4) == items[:2]  # type: ignore[arg-type]


def test_resample_changes_length_proportionally_and_keeps_amplitude() -> None:
    src = _tone(1.0, 8000, sample_rate=48000)
    out = resample_pcm16(src, 48000, SAMPLE_RATE)
    assert abs(len(out) / 2 - SAMPLE_RATE) <= 2
    peak = max(abs(v) for v in struct.unpack("<" + "h" * (len(out) // 2), out))
    assert 7000 <= peak <= 8000
    assert resample_pcm16(src, 48000, 48000) is src


def test_read_stereo_wav_rejects_mono(tmp_path: Path) -> None:
    mono = tmp_path / "mono.wav"
    with wave.open(str(mono), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(_silence(1))
    with pytest.raises(ValueError, match="two-channel"):
        read_stereo_wav(mono)


def test_read_stereo_wav_deinterleaves(tmp_path: Path) -> None:
    stereo = tmp_path / "stereo.wav"
    left = struct.pack("<hhh", 1, 2, 3)
    right = struct.pack("<hhh", -1, -2, -3)
    interleaved = b"".join(left[i : i + 2] + right[i : i + 2] for i in range(0, 6, 2))
    with wave.open(str(stereo), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(interleaved)
    ch0, ch1, rate = read_stereo_wav(stereo)
    assert (ch0, ch1, rate) == (left, right, SAMPLE_RATE)
