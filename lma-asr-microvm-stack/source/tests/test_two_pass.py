# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the two-pass recogniser: streaming previews, offline finals."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from asr_server.recognizer import Event, Recognizer, RecognizerEngine, SessionConfig
from asr_server.two_pass import TwoPassEngine, TwoPassRecognizer

RATE = 16000
CHUNK = RATE * 320 // 1000 * 2  # 320 ms of 16-bit PCM


def _pcm(fill: int = 1) -> bytes:
    return bytes([fill]) * CHUNK


def _partial(segment: int, text: str, start: float, end: float) -> Event:
    return Event(kind="partial", segment=segment, text=text, start=start, end=end)


def _final(segment: int, text: str, start: float, end: float) -> Event:
    return Event(kind="final", segment=segment, text=text, start=start, end=end)


class ScriptedAuthority:
    """Offline stand-in: events per chunk, and the open utterance's start after each chunk."""

    def __init__(
        self,
        per_chunk: Sequence[Sequence[Event]],
        open_starts: Sequence[float | None],
        *,
        on_flush: Sequence[Event] = (),
    ) -> None:
        self._per_chunk = [list(events) for events in per_chunk]
        self._open_starts = list(open_starts)
        self._on_flush = list(on_flush)
        self._idx = -1
        self._segment = 0
        self.chunks: list[bytes] = []

    def accept_pcm(self, pcm: bytes) -> list[Event]:
        self.chunks.append(pcm)
        self._idx += 1
        events = list(self._per_chunk[self._idx]) if self._idx < len(self._per_chunk) else []
        self._segment += sum(1 for event in events if event.kind == "final")
        return events

    def flush(self) -> list[Event]:
        return list(self._on_flush)

    def current_segment(self) -> int:
        return self._segment

    def open_segment_start(self) -> float | None:
        if self._idx < 0 or self._idx >= len(self._open_starts):
            return None
        return self._open_starts[self._idx]


class ScriptedPreview(Recognizer):
    def __init__(self, per_chunk: Sequence[Sequence[Event]]) -> None:
        self._per_chunk = [list(events) for events in per_chunk]
        self._idx = -1
        self.chunks: list[bytes] = []

    def accept_pcm(self, pcm: bytes) -> list[Event]:
        self.chunks.append(pcm)
        self._idx += 1
        return list(self._per_chunk[self._idx]) if self._idx < len(self._per_chunk) else []

    def flush(self) -> list[Event]:
        return []


class PreviewFactory:
    def __init__(self, scripts: Sequence[Sequence[Sequence[Event]]]) -> None:
        self._scripts = list(scripts)
        self.created: list[ScriptedPreview] = []

    def __call__(self) -> Recognizer:
        script = self._scripts[len(self.created)] if len(self.created) < len(self._scripts) else []
        preview = ScriptedPreview(script)
        self.created.append(preview)
        return preview


def test_preview_partials_carry_the_open_utterance_and_grow() -> None:
    authority = ScriptedAuthority([[], []], [0.1, 0.1])
    factory = PreviewFactory(
        [[[_partial(0, "hello", 0.0, 0.3)], [_partial(0, "hello there", 0.0, 0.6)]]]
    )
    rec = TwoPassRecognizer(authority, factory, sample_rate=RATE)

    first = rec.accept_pcm(_pcm())
    second = rec.accept_pcm(_pcm())

    assert first == [_partial(0, "hello", 0.1, pytest.approx(0.32))]
    assert second == [_partial(0, "hello there", 0.1, pytest.approx(0.64))]
    assert len(factory.created) == 1
    assert factory.created[0].chunks[0] == _pcm()[round(0.1 * RATE) * 2 :]
    assert factory.created[0].chunks[1] == _pcm()


def test_the_offline_final_replaces_the_preview_and_the_next_utterance_starts_fresh() -> None:
    authority = ScriptedAuthority(
        [[], [_partial(0, "Hello.", 0.1, 0.6), _final(0, "Hello.", 0.1, 0.6)], []],
        [0.1, None, 0.9],
    )
    factory = PreviewFactory(
        [[[_partial(0, "hello", 0.0, 0.3)]], [[_partial(0, "next", 0.0, 0.1)]]]
    )
    rec = TwoPassRecognizer(authority, factory, sample_rate=RATE)

    rec.accept_pcm(_pcm(1))
    closing = rec.accept_pcm(_pcm(2))
    reopened = rec.accept_pcm(_pcm(3))

    assert closing == [_final(0, "Hello.", 0.1, 0.6)]
    assert reopened == [_partial(1, "next", 0.9, pytest.approx(0.96))]
    assert len(factory.created) == 2
    assert factory.created[1].chunks == [_pcm(3)[round(0.9 * RATE) * 2 - 2 * CHUNK :]]


def test_streaming_finals_inside_an_utterance_accumulate_into_the_preview() -> None:
    authority = ScriptedAuthority([[], [], []], [0.0, 0.0, 0.0])
    factory = PreviewFactory(
        [
            [
                [_partial(0, "one", 0.0, 0.3)],
                [_final(0, "one two", 0.0, 0.6)],
                [_partial(1, "three", 0.7, 0.9)],
            ]
        ]
    )
    rec = TwoPassRecognizer(authority, factory, sample_rate=RATE)

    texts = [event.text for _ in range(3) for event in rec.accept_pcm(_pcm())]

    assert texts == ["one", "one two", "one two three"]


def test_a_hard_cut_closes_one_utterance_and_previews_the_next_on_the_same_chunk() -> None:
    authority = ScriptedAuthority([[], [_final(0, "First part", 0.0, 0.64)]], [0.0, 0.6])
    factory = PreviewFactory(
        [[[_partial(0, "first", 0.0, 0.3)]], [[_partial(0, "second", 0.0, 0.1)]]]
    )
    rec = TwoPassRecognizer(authority, factory, sample_rate=RATE)

    rec.accept_pcm(_pcm(1))
    events = rec.accept_pcm(_pcm(2))

    assert events == [
        _final(0, "First part", 0.0, 0.64),
        _partial(1, "second", 0.6, pytest.approx(0.64)),
    ]
    assert len(factory.created) == 2
    assert factory.created[1].chunks == [_pcm(2)[round(0.6 * RATE) * 2 - CHUNK :]]


def test_flush_returns_only_the_offline_finals() -> None:
    authority = ScriptedAuthority(
        [[]], [0.1], on_flush=[_partial(0, "Bye.", 0.1, 0.3), _final(0, "Bye.", 0.1, 0.3)]
    )
    factory = PreviewFactory([[[_partial(0, "bye", 0.0, 0.2)]]])
    rec = TwoPassRecognizer(authority, factory, sample_rate=RATE)
    rec.accept_pcm(_pcm())

    assert rec.flush() == [_final(0, "Bye.", 0.1, 0.3)]
    assert rec.current_words() == []


class FakeEngine(RecognizerEngine):
    def __init__(self, make: object) -> None:
        self._make = make
        self.configs: list[SessionConfig | None] = []

    def new_session(self, config: SessionConfig | None = None) -> Recognizer:
        self.configs.append(config)
        return self._make()  # type: ignore[no-any-return,operator]


def test_the_engine_composes_an_offline_session_with_lazily_made_previews() -> None:
    authority_engine = FakeEngine(lambda: ScriptedAuthority([[], []], [0.0, 0.0]))
    preview_engine = FakeEngine(lambda: ScriptedPreview([[_partial(0, "hi", 0.0, 0.2)]]))
    engine = TwoPassEngine(authority_engine, preview_engine, sample_rate=RATE)
    negotiated = SessionConfig(sample_rate=RATE, endpointing_ms=1200, diarize=True)

    session = engine.new_session(negotiated)

    assert isinstance(session, TwoPassRecognizer)
    assert authority_engine.configs == [negotiated]
    assert preview_engine.configs == []
    assert session.accept_pcm(_pcm()) == [_partial(0, "hi", 0.0, pytest.approx(0.32))]
    assert preview_engine.configs == [SessionConfig(sample_rate=RATE)]


def test_a_new_utterance_without_a_final_for_the_last_one_starts_a_fresh_preview() -> None:
    authority = ScriptedAuthority([[], []], [0.0, 0.6])
    factory = PreviewFactory(
        [[[_partial(0, "first", 0.0, 0.3)]], [[_partial(0, "second", 0.0, 0.1)]]]
    )
    rec = TwoPassRecognizer(authority, factory, sample_rate=RATE)

    rec.accept_pcm(_pcm(1))
    events = rec.accept_pcm(_pcm(2))

    assert [e.text for e in events] == ["second"]
    assert events[0].start == pytest.approx(0.6)
    assert len(factory.created) == 2


class FailingPreview(Recognizer):
    def accept_pcm(self, pcm: bytes) -> list[Event]:
        raise RuntimeError("preview decode failed")

    def flush(self) -> list[Event]:
        return []


def test_a_preview_failure_keeps_the_offline_finals_and_stops_previews(
    caplog: pytest.LogCaptureFixture,
) -> None:
    authority = ScriptedAuthority([[], [_final(0, "Hello.", 0.0, 0.6)], []], [0.0, None, 0.9])
    made: list[Recognizer] = []

    def factory() -> Recognizer:
        preview = FailingPreview()
        made.append(preview)
        return preview

    rec = TwoPassRecognizer(authority, factory, sample_rate=RATE)
    with caplog.at_level("ERROR", logger="asr_server.two_pass"):
        first = rec.accept_pcm(_pcm(1))
        closing = rec.accept_pcm(_pcm(2))
        later = rec.accept_pcm(_pcm(3))

    assert first == []
    assert closing == [_final(0, "Hello.", 0.0, 0.6)]
    assert later == []
    assert len(made) == 1
    assert sum("live preview failed" in r.getMessage() for r in caplog.records) == 1
