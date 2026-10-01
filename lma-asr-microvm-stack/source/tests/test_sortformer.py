# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
from __future__ import annotations

import math
import struct

import numpy as np
import pytest
from asr_server.recognizer import Event, SessionConfig, WordTiming
from asr_server.sortformer import (
    FRAME_SEC,
    HIDDEN,
    N_MELS,
    NUM_SPEAKERS,
    SUBSAMPLING,
    LogMelFrontEnd,
    SortformerConfig,
    SortformerEngine,
    SortformerRecognizer,
    SortformerSession,
    SpeakerCache,
    SpeakerCacheConfig,
    slaney_mel_filters,
)

from tests.test_diarization import ScriptedEngine, ScriptedRecognizer

RATE = 16000


def _tone(seconds: float, amplitude: float, freq: float = 300.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (amplitude * np.sin(2 * math.pi * freq * t)).astype(np.float32)


def _pcm(samples: np.ndarray) -> bytes:
    ints = np.clip(samples * 32767, -32768, 32767).astype(np.int16)
    return struct.pack(f"<{len(ints)}h", *ints)


def _word(w: str, s: float, e: float) -> WordTiming:
    return WordTiming(w=w, s=s, e=e)


def test_mel_filters_cover_the_spectrum_with_slaney_normalisation() -> None:
    filters = slaney_mel_filters()
    assert filters.shape == (N_MELS, 257)
    assert (filters >= 0).all()
    assert (filters.sum(axis=1) > 0).all()


def test_front_end_output_does_not_depend_on_how_audio_is_chunked() -> None:
    audio = _tone(3.3, 0.3) + _tone(3.3, 0.05, freq=1200.0)
    whole = LogMelFrontEnd()
    expected = np.concatenate([whole.push(audio), whole.flush()])
    pieces = LogMelFrontEnd()
    parts, pos = [], 0
    for size in (1, 159, 161, 777, 4000, 16000):
        parts.append(pieces.push(audio[pos : pos + size]))
        pos += size
    parts.append(pieces.push(audio[pos:]))
    parts.append(pieces.flush())
    got = np.concatenate(parts)
    assert got.shape == expected.shape == (len(audio) // 160, N_MELS)
    np.testing.assert_allclose(got, expected, atol=1e-4)


def test_front_end_marks_digital_silence_at_the_log_guard() -> None:
    fe = LogMelFrontEnd()
    frames = np.concatenate([fe.push(np.zeros(RATE, dtype=np.float32)), fe.flush()])
    assert frames.shape == (100, N_MELS)
    np.testing.assert_allclose(frames, math.log(2.0**-24), atol=1e-4)


def _step_inputs(num_cached: int, num_chunk: int, active: int | None) -> tuple:
    step = np.random.default_rng(num_cached).standard_normal((num_cached + num_chunk, HIDDEN))
    logits = np.full(((num_cached + num_chunk) * SUBSAMPLING, NUM_SPEAKERS), -8.0)
    if active is not None:
        logits[:, active] = 8.0
    return step.astype(np.float32), logits.astype(np.float32)


def test_cache_holds_frames_in_the_fifo_until_it_overflows() -> None:
    cache = SpeakerCache(
        SpeakerCacheConfig(fifo_length=10, speaker_cache_length=16, speaker_cache_update_period=4)
    )
    silence = np.zeros(HIDDEN, dtype=np.float32)
    step, logits = _step_inputs(0, 6, 0)
    cache.update(step, logits, silence, 6)
    assert (len(cache.embeds), len(cache.fifo)) == (0, 6)
    step, logits = _step_inputs(6, 6, 0)
    cache.update(step, logits, silence, 6)
    assert (len(cache.embeds), len(cache.fifo)) == (4, 8)
    assert len(cache.cached()) == 12


def test_cache_compresses_to_its_length_with_a_silence_slot_per_speaker() -> None:
    cache = SpeakerCache(
        SpeakerCacheConfig(fifo_length=8, speaker_cache_length=16, speaker_cache_update_period=8)
    )
    silence = np.full(HIDDEN, 7.0, dtype=np.float32)
    for index in range(6):
        cached = len(cache.cached())
        step, logits = _step_inputs(cached, 8, index % 2)
        cache.update(step, logits, silence, 8)
    assert cache.compressed
    assert len(cache.embeds) == 16
    assert len(cache.fifo) == 8
    assert (cache.embeds == 7.0).all(axis=1).sum() >= 1


class EnergyBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def step(self, features: np.ndarray, cached: np.ndarray) -> tuple:
        self.calls.append((len(features), len(cached)))
        num_embeds = math.ceil(len(features) / SUBSAMPLING)
        energy = features.max(axis=1)
        frame_logits = np.full((len(features), NUM_SPEAKERS), -8.0, dtype=np.float32)
        frame_logits[energy > -2.0, 0] = 8.0
        frame_logits[(energy > -9.0) & (energy <= -2.0), 1] = 8.0
        padded = np.full((num_embeds * SUBSAMPLING, NUM_SPEAKERS), -8.0, dtype=np.float32)
        padded[: len(features)] = frame_logits
        cached_logits = np.full((len(cached) * SUBSAMPLING, NUM_SPEAKERS), -8.0, dtype=np.float32)
        logits = np.concatenate([cached_logits, padded])
        chunk_embeds = np.zeros((num_embeds, HIDDEN), dtype=np.float32)
        return logits, chunk_embeds, np.zeros(HIDDEN, dtype=np.float32)


def test_session_labels_each_stretch_of_audio_by_its_speaker() -> None:
    backend = EnergyBackend()
    session = SortformerSession(backend, SortformerConfig(idle_step_frames=50, max_chunk_frames=50))
    session.push(_tone(4.0, 0.5))
    session.push(np.zeros(RATE * 2, dtype=np.float32))
    session.push(_tone(4.0, 0.01))
    session.finish()
    assert session.speaker_between(0.5, 3.5) == 0
    assert session.speaker_between(6.5, 9.5) == 1
    assert session.speaker_between(4.5, 5.5) is None
    assert all(features <= (50 + 4) * SUBSAMPLING for features, _ in backend.calls)
    assert session.labelled_until == pytest.approx(10.0, abs=0.02)


def test_session_steps_on_its_own_through_long_stretches_without_finals() -> None:
    backend = EnergyBackend()
    session = SortformerSession(backend, SortformerConfig(idle_step_frames=125))
    session.push(np.zeros(RATE * 30, dtype=np.float32))
    assert len(backend.calls) == 2
    assert session.labelled_until == pytest.approx(20.0)


class ScriptedDiarizer:
    def __init__(self, turns: list[tuple[float, float, int]]) -> None:
        self._turns = turns
        self.pushed = 0
        self.processed = 0
        self.finished = False

    def push(self, samples: np.ndarray) -> None:
        self.pushed += len(samples)

    def process_available(self) -> None:
        self.processed += 1

    def finish(self) -> None:
        self.finished = True

    def speaker_between(self, start: float, end: float) -> int | None:
        best, speaker = 0.0, None
        for a, b, spk in self._turns:
            overlap = min(b, end) - max(a, start)
            if overlap > best:
                best, speaker = overlap, spk
        return speaker


def _final(segment: int, start: float, end: float, words: list[WordTiming] | None) -> Event:
    text = " ".join(w.w for w in words) if words else "no timings here"
    return Event(kind="final", segment=segment, text=text, start=start, end=end, words=words)


def test_a_final_is_split_where_the_speaker_changes_and_labels_follow_first_appearance() -> None:
    words = [
        _word("so", 0.1, 0.4),
        _word("what", 0.5, 0.8),
        _word("do", 0.9, 1.1),
        _word("you", 1.2, 1.4),
        _word("yeah", 2.0, 2.4),
        _word("ready", 2.5, 2.9),
        _word("when", 3.0, 3.3),
        _word("you", 3.4, 3.6),
    ]
    inner = ScriptedRecognizer([[_final(0, 0.0, 4.0, words)]])
    rec = SortformerRecognizer(inner, ScriptedDiarizer([(0.0, 1.8, 5), (1.8, 4.0, 2)]))

    rows = rec.accept_pcm(_pcm(np.zeros(1600, dtype=np.float32)))

    assert [(r.segment, r.text, r.speaker) for r in rows] == [
        (0, "so what do you", "spk_0"),
        (1, "yeah ready when you", "spk_1"),
    ]
    assert rows[0].start == 0.0 and rows[0].end == pytest.approx(1.4)
    assert rows[1].start == pytest.approx(2.0) and rows[1].end == 4.0
    assert rec.current_segment() == 1


def test_a_single_short_word_by_another_speaker_stays_in_its_row() -> None:
    words = [
        _word("one", 0.0, 0.4),
        _word("two", 0.5, 0.9),
        _word("mm", 1.0, 1.2),
        _word("three", 1.3, 1.7),
        _word("four", 1.8, 2.2),
    ]
    inner = ScriptedRecognizer([[_final(0, 0.0, 2.2, words)]])
    rec = SortformerRecognizer(
        inner, ScriptedDiarizer([(0.0, 0.95, 0), (0.95, 1.25, 1), (1.25, 2.2, 0)])
    )

    rows = rec.accept_pcm(_pcm(np.zeros(160, dtype=np.float32)))

    assert [(r.text, r.speaker) for r in rows] == [("one two mm three four", "spk_0")]


def test_later_partials_and_finals_are_renumbered_after_a_split() -> None:
    first = [_word("a", 0.0, 0.5), _word("b", 0.6, 1.0), _word("c", 2.0, 2.5), _word("d", 2.6, 3.0)]
    inner = ScriptedRecognizer(
        [
            [_final(0, 0.0, 3.0, first)],
            [Event(kind="partial", segment=1, text="next", start=3.5, end=4.0)],
            [_final(1, 3.5, 4.5, [_word("later", 3.6, 4.4)])],
        ]
    )
    rec = SortformerRecognizer(
        inner, ScriptedDiarizer([(0.0, 1.5, 0), (1.5, 3.2, 1), (3.2, 5.0, 0)])
    )
    silence = _pcm(np.zeros(160, dtype=np.float32))

    split = rec.accept_pcm(silence)
    partial = rec.accept_pcm(silence)
    later = rec.accept_pcm(silence)

    assert [r.segment for r in split] == [0, 1]
    assert [(r.kind, r.segment, r.speaker) for r in partial] == [("partial", 2, None)]
    assert [(r.segment, r.speaker) for r in later] == [(2, "spk_0")]


def test_a_final_without_word_timings_takes_the_dominant_speaker() -> None:
    inner = ScriptedRecognizer([[_final(0, 0.0, 10.0, None)]])
    rec = SortformerRecognizer(inner, ScriptedDiarizer([(0.0, 3.0, 4), (3.0, 10.0, 6)]))

    rows = rec.accept_pcm(_pcm(np.zeros(160, dtype=np.float32)))

    assert [(r.text, r.speaker) for r in rows] == [("no timings here", "spk_0")]


def test_the_diarizer_is_fed_the_audio_and_caught_up_before_each_final() -> None:
    diarizer = ScriptedDiarizer([(0.0, 5.0, 0)])
    inner = ScriptedRecognizer([[], [_final(0, 0.0, 1.0, [_word("hi", 0.1, 0.5)])]], on_flush=[])
    rec = SortformerRecognizer(inner, diarizer)

    rec.accept_pcm(_pcm(np.zeros(1600, dtype=np.float32)))
    rec.accept_pcm(_pcm(np.zeros(1600, dtype=np.float32)))
    rec.flush()

    assert diarizer.pushed == 3200
    assert diarizer.processed == 1
    assert diarizer.finished and inner.flushed


def test_a_session_without_speaker_labels_is_left_undiarized() -> None:
    inner_session = ScriptedRecognizer()
    engine = SortformerEngine(ScriptedEngine(inner_session), EnergyBackend())

    plain = engine.new_session(SessionConfig(sample_rate=RATE, diarize=False))
    labelled = engine.new_session(SessionConfig(sample_rate=RATE, diarize=True))

    assert plain is inner_session
    assert isinstance(labelled, SortformerRecognizer)


def test_the_recogniser_refuses_audio_that_is_not_16_khz() -> None:
    with pytest.raises(ValueError, match="16 kHz"):
        SortformerRecognizer(ScriptedRecognizer(), ScriptedDiarizer([]), sample_rate=8000)


def test_labels_land_on_the_frames_the_front_end_timestamps() -> None:
    backend = EnergyBackend()
    session = SortformerSession(backend)
    session.push(np.zeros(RATE, dtype=np.float32))
    session.push(_tone(1.0, 0.5))
    session.finish()
    active = session.activity(0.0, 2.0)[:, 0] > 0.5
    onset = int(np.argmax(active)) * FRAME_SEC
    assert onset == pytest.approx(1.0, abs=0.05)


def test_a_slow_step_is_logged_with_its_size(caplog: pytest.LogCaptureFixture) -> None:
    session = SortformerSession(EnergyBackend(), SortformerConfig(slow_step_seconds=0.0))
    with caplog.at_level("WARNING", logger="asr_server.sortformer"):
        session.push(np.zeros(RATE, dtype=np.float32))
        session.finish()
    assert any("sortformer step 1 took" in r.getMessage() for r in caplog.records)


class BrokenDiarizer(ScriptedDiarizer):
    def process_available(self) -> None:
        raise RuntimeError("onnxruntime failure")


def test_a_diarizer_failure_leaves_transcription_running_without_labels(
    caplog: pytest.LogCaptureFixture,
) -> None:
    words = [_word("hello", 0.0, 0.4), _word("there", 0.5, 0.9)]
    inner = ScriptedRecognizer(
        [[_final(0, 0.0, 1.0, words)], [_final(1, 1.5, 2.0, [_word("again", 1.6, 1.9)])]]
    )
    rec = SortformerRecognizer(inner, BrokenDiarizer([(0.0, 5.0, 0)]))
    silence = _pcm(np.zeros(160, dtype=np.float32))

    with caplog.at_level("ERROR", logger="asr_server.sortformer"):
        first = rec.accept_pcm(silence)
        second = rec.accept_pcm(silence)

    assert [(r.segment, r.text, r.speaker) for r in first + second] == [
        (0, "hello there", None),
        (1, "again", None),
    ]
    assert sum("sortformer diarizer failed" in r.getMessage() for r in caplog.records) == 1
    assert len(inner.chunks) == 2
