# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for pyannote-based speaker-turn detection (no model weights).

The failure this guards against is a false split: cutting a transcript row where
one person was still speaking is more visible to a user than the missed turn it is
meant to fix, so the minimum-duration and window-trust rules are tested directly.
"""

from __future__ import annotations

import pytest
from asr_server.segmentation import (
    POWERSET,
    SegmentationConfig,
    TurnDetector,
    pcm16_to_float32,
)

FRAMES_PER_WINDOW = 589
WINDOW_SEC = 10.0
FRAME_SEC = WINDOW_SEC / FRAMES_PER_WINDOW


def one_hot(class_index: int) -> list[float]:
    return [1.0 if index == class_index else 0.0 for index in range(len(POWERSET))]


SILENCE, SPK0, SPK1, SPK2, SPK0_1 = 0, 1, 2, 3, 4


def frames_from(spans: list[tuple[float, float, int]]) -> list[list[float]]:
    """Per-frame scores for a window described as (from_sec, to_sec, class)."""
    scores = [one_hot(SILENCE) for _ in range(FRAMES_PER_WINDOW)]
    for start, end, class_index in spans:
        for frame in range(int(start / FRAME_SEC), min(int(end / FRAME_SEC), FRAMES_PER_WINDOW)):
            scores[frame] = one_hot(class_index)
    return scores


class ScriptedBackend:
    def __init__(self, windows: list[list[list[float]]]) -> None:
        self.windows = windows
        self.calls = 0

    def run(self, samples):  # noqa: ANN001, ANN202 - test double
        self.lengths = getattr(self, "lengths", [])
        self.lengths.append(len(samples))
        window = self.windows[min(self.calls, len(self.windows) - 1)]
        self.calls += 1
        return window


def detector(windows: list[list[list[float]]], **kwargs) -> tuple[TurnDetector, ScriptedBackend]:
    backend = ScriptedBackend(windows)
    config = SegmentationConfig(window_sec=WINDOW_SEC, **kwargs)
    return TurnDetector(backend, config), backend


def samples_for(seconds: float, sample_rate: int = 16000) -> list[float]:
    return [0.0] * int(seconds * sample_rate)


def test_one_speaker_throughout_yields_no_boundary() -> None:
    turns, _ = detector([frames_from([(0.0, 8.0, SPK0)])])

    result = turns.detect_samples(samples_for(8.0))

    assert result.boundaries == ()
    assert result.has_change is False


def test_a_speaker_change_is_reported_between_the_two_turns() -> None:
    windows = [frames_from([(0.5, 3.0, SPK0), (3.4, 6.0, SPK1)])]
    turns, _ = detector(windows)

    result = turns.detect_samples(samples_for(6.5))

    assert len(result.boundaries) == 1
    # Cut in the middle of the gap, where the word boundary is.
    assert result.boundaries[0] == pytest.approx(3.2, abs=0.1)


def test_a_boundary_is_placed_immediately_when_speech_is_continuous() -> None:
    windows = [frames_from([(0.0, 3.0, SPK0), (3.0, 6.0, SPK1)])]
    turns, _ = detector(windows)

    result = turns.detect_samples(samples_for(6.0))

    assert len(result.boundaries) == 1
    assert result.boundaries[0] == pytest.approx(3.0, abs=0.05)


def test_a_short_back_channel_does_not_split_the_row() -> None:
    # 300 ms of the other speaker in the middle: "mhm". Splitting here would turn
    # one sentence into three rows.
    windows = [frames_from([(0.0, 3.0, SPK0), (3.0, 3.3, SPK1), (3.3, 6.0, SPK0)])]
    turns, _ = detector(windows, min_turn_ms=700)

    result = turns.detect_samples(samples_for(6.0))

    assert result.boundaries == ()


def test_a_sustained_interjection_does_split_the_row() -> None:
    windows = [frames_from([(0.0, 3.0, SPK0), (3.0, 5.0, SPK1), (5.0, 8.0, SPK0)])]
    turns, _ = detector(windows, min_turn_ms=700)

    result = turns.detect_samples(samples_for(8.0))

    assert len(result.boundaries) == 2
    assert result.boundaries[0] == pytest.approx(3.0, abs=0.05)
    assert result.boundaries[1] == pytest.approx(5.0, abs=0.05)


def test_returning_to_the_same_speaker_is_not_a_boundary() -> None:
    # Same speaker either side of a pause: the pause is endpointing's business, not
    # a speaker change.
    windows = [frames_from([(0.0, 3.0, SPK0), (4.0, 7.0, SPK0)])]
    turns, _ = detector(windows)

    result = turns.detect_samples(samples_for(7.0))

    assert result.boundaries == ()


def test_overlapping_speech_is_reported_as_a_span() -> None:
    windows = [frames_from([(0.0, 2.0, SPK0), (2.0, 3.0, SPK0_1), (3.0, 6.0, SPK1)])]
    turns, _ = detector(windows)

    result = turns.detect_samples(samples_for(6.0))

    assert len(result.overlaps) == 1
    start, end = result.overlaps[0]
    assert start == pytest.approx(2.0, abs=0.05)
    assert end == pytest.approx(3.0, abs=0.05)


def test_a_window_is_padded_to_the_model_input_length() -> None:
    turns, backend = detector([frames_from([(0.0, 2.0, SPK0)])])

    turns.detect_samples(samples_for(2.0))

    # pyannote is calibrated on its training window; a short segment is padded with
    # silence rather than fed at an arbitrary length.
    assert backend.lengths == [int(WINDOW_SEC * 16000)]


def test_audio_longer_than_one_window_is_judged_once_per_instant() -> None:
    # Two windows, 50% hop. The same continuous speaker is reported in both, and the
    # seam must not be mistaken for a turn change.
    windows = [frames_from([(0.0, 10.0, SPK0)]), frames_from([(0.0, 10.0, SPK0)])]
    turns, backend = detector(windows)

    result = turns.detect_samples(samples_for(12.0))

    assert backend.calls >= 2
    assert result.boundaries == ()


def test_a_change_in_the_second_window_is_found_at_absolute_time() -> None:
    windows = [
        frames_from([(0.0, 10.0, SPK0)]),
        # This window starts at 5.0s; the change at 3.0s into it is 8.0s absolute.
        frames_from([(0.0, 3.0, SPK0), (3.0, 8.0, SPK1)]),
    ]
    turns, _ = detector(windows)

    result = turns.detect_samples(samples_for(13.0))

    assert len(result.boundaries) == 1
    assert result.boundaries[0] == pytest.approx(8.0, abs=0.2)


def test_empty_audio_is_handled_without_calling_the_model() -> None:
    turns, backend = detector([frames_from([])])

    result = turns.detect_samples([])

    assert result.boundaries == ()
    assert result.frame_sec == 0.0
    assert backend.calls == 0


def test_pcm_conversion_scales_int16_to_unit_range() -> None:
    pcm = (16384).to_bytes(2, "little", signed=True) + (-16384).to_bytes(2, "little", signed=True)

    samples = pcm16_to_float32(pcm)

    assert samples[0] == pytest.approx(0.5)
    assert samples[1] == pytest.approx(-0.5)


def test_the_frame_rate_is_derived_from_the_model_output() -> None:
    turns, _ = detector([frames_from([(0.0, 5.0, SPK0)])])

    result = turns.detect_samples(samples_for(5.0))

    assert result.frame_sec == pytest.approx(FRAME_SEC, rel=1e-6)
