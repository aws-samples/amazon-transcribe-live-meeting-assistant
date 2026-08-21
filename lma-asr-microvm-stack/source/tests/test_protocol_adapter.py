# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the engine-event to wire-envelope adapter.

The tests run a *recorded sherpa event sequence*
through :class:`ProtocolAdapter` and asserts the exact wire envelope schema
(``design.md`` §5.2) comes out, honouring the §5.3 contract: partial dedupe,
final-with-word-timestamps, and monotonic segments that never re-emit a
finalized segment as a partial.

These tests drive the adapter with hand-authored
:class:`~asr_server.recognizer.Event` trajectories — exactly the shape the
streaming recognizer emits, so no model weights are required.
"""

from __future__ import annotations

import pytest
from asr_protocol import Final, Partial, Word
from asr_server.protocol_adapter import ProtocolAdapter
from asr_server.recognizer import Event, WordTiming


def _partial(segment: int, text: str, start: float) -> Event:
    return Event(kind="partial", segment=segment, text=text, start=start)


def _final(
    segment: int,
    text: str,
    start: float,
    end: float,
    words: list[WordTiming] | None = None,
) -> Event:
    return Event(kind="final", segment=segment, text=text, start=start, end=end, words=words)


# --- Recorded sherpa event sequence -> exact envelope schema ----------------


def test_recorded_sequence_produces_exact_envelope_schema() -> None:
    """Core acceptance: a recorded event stream yields the exact §5.2 envelopes."""
    events = [
        _partial(0, "let", 12.44),
        _partial(0, "let's", 12.44),
        _partial(0, "let's meet", 12.44),
        _partial(0, "let's meet at noon", 12.44),
        _final(
            0,
            "Let's meet at noon.",
            12.44,
            13.98,
            words=[
                WordTiming(w="Let's", s=12.44, e=12.70),
                WordTiming(w="meet", s=12.70, e=12.98),
                WordTiming(w="at", s=12.98, e=13.20),
                WordTiming(w="noon", s=13.20, e=13.98),
            ],
        ),
    ]
    adapter = ProtocolAdapter(word_timestamps=True)

    messages = adapter.adapt(events)

    # The exact serialized envelopes on the wire (schema per design.md §5.2).
    # ``speaker`` is present but ``None``: these events carry no speaker because
    # diarization is not enabled here (the engine emits no label). It still appears
    # in the envelope so the wire schema is stable whether or not diarization is on.
    assert [m.model_dump() for m in messages] == [
        {"type": "partial", "segment": 0, "text": "let", "start": 12.44, "speaker": None},
        {"type": "partial", "segment": 0, "text": "let's", "start": 12.44, "speaker": None},
        {
            "type": "partial",
            "segment": 0,
            "text": "let's meet",
            "start": 12.44,
            "speaker": None,
        },
        {
            "type": "partial",
            "segment": 0,
            "text": "let's meet at noon",
            "start": 12.44,
            "speaker": None,
        },
        {
            "type": "final",
            "segment": 0,
            "text": "Let's meet at noon.",
            "start": 12.44,
            "end": 13.98,
            "words": [
                {"w": "Let's", "s": 12.44, "e": 12.70},
                {"w": "meet", "s": 12.70, "e": 12.98},
                {"w": "at", "s": 12.98, "e": 13.20},
                {"w": "noon", "s": 13.20, "e": 13.98},
            ],
            "speaker": None,
        },
    ]
    assert adapter.segments == 1


def test_partial_and_final_are_wire_models() -> None:
    """Adapter output are the ``asr_protocol`` wire models, not engine events."""
    adapter = ProtocolAdapter()
    (partial,) = adapter.adapt([_partial(0, "hi", 0.0)])
    (final,) = adapter.adapt([_final(0, "Hi.", 0.0, 1.0)])
    assert isinstance(partial, Partial)
    assert isinstance(final, Final)


# --- Rule 1: dedupe identical consecutive partial text ----------------------


def test_identical_consecutive_partials_are_deduped() -> None:
    """The same interim hypothesis is never emitted twice in a row (§5.3.1)."""
    events = [
        _partial(0, "hello", 0.0),
        _partial(0, "hello", 0.0),
        _partial(0, "hello world", 0.0),
        _partial(0, "hello world", 0.0),
    ]
    messages = ProtocolAdapter().adapt(events)
    assert [m.text for m in messages] == ["hello", "hello world"]


def test_partial_text_may_repeat_non_consecutively() -> None:
    """Dedupe is only for *consecutive* text; an oscillation re-emits (§5.3.1)."""
    events = [
        _partial(0, "the", 0.0),
        _partial(0, "the cat", 0.0),
        _partial(0, "the", 0.0),  # revised back down — not consecutive-identical
    ]
    messages = ProtocolAdapter().adapt(events)
    assert [m.text for m in messages] == ["the", "the cat", "the"]


def test_dedupe_memory_resets_between_segments() -> None:
    """Same text in a new segment is not treated as a duplicate of the last."""
    events = [
        _partial(0, "okay", 0.0),
        _final(0, "Okay.", 0.0, 1.0),
        _partial(1, "okay", 2.0),  # identical text, new segment -> must emit
    ]
    messages = ProtocolAdapter().adapt(events)
    assert [(type(m).__name__, m.segment, m.text) for m in messages] == [
        ("Partial", 0, "okay"),
        ("Final", 0, "Okay."),
        ("Partial", 1, "okay"),
    ]


# --- Rule 2: final with word timestamps -------------------------------------


def test_word_timestamps_omitted_when_disabled() -> None:
    """With ``word_timestamps=False`` a final carries an empty words list (§5.2)."""
    event = _final(
        0, "hi there", 0.0, 1.0, words=[WordTiming(w="hi", s=0.0, e=0.5)]
    )
    (final,) = ProtocolAdapter(word_timestamps=False).adapt([event])
    assert isinstance(final, Final)
    assert final.words == []


def test_word_timestamps_empty_when_engine_provides_none() -> None:
    """Even with the flag on, no engine timings -> empty words (streaming path)."""
    (final,) = ProtocolAdapter(word_timestamps=True).adapt([_final(0, "hi", 0.0, 1.0)])
    assert isinstance(final, Final)
    assert final.words == []


def test_word_timestamps_converted_to_wire_words() -> None:
    """Engine ``WordTiming`` maps 1:1 to the wire ``Word`` model."""
    event = _final(
        0,
        "hi there",
        0.0,
        1.0,
        words=[WordTiming(w="hi", s=0.0, e=0.5), WordTiming(w="there", s=0.5, e=1.0)],
    )
    (final,) = ProtocolAdapter(word_timestamps=True).adapt([event])
    assert isinstance(final, Final)
    assert final.words == [Word(w="hi", s=0.0, e=0.5), Word(w="there", s=0.5, e=1.0)]


# --- Rule 3: monotonic segments, never re-emit finalized as partial ---------


def test_monotonic_segments_across_utterances() -> None:
    """Each finalized segment advances; the counter is monotonic (§5.3.3)."""
    events = [
        _partial(0, "one", 0.0),
        _final(0, "One.", 0.0, 1.0),
        _partial(1, "two", 2.0),
        _final(1, "Two.", 2.0, 3.0),
        _partial(2, "three", 4.0),
        _final(2, "Three.", 4.0, 5.0),
    ]
    messages = ProtocolAdapter().adapt(events)
    finals = [m for m in messages if isinstance(m, Final)]
    assert [f.segment for f in finals] == [0, 1, 2]


def test_partial_for_finalized_segment_is_dropped() -> None:
    """A stray partial for an already-finalized segment never goes on the wire."""
    events = [
        _final(0, "Done.", 0.0, 1.0),
        _partial(0, "late partial", 0.5),  # same segment, after final -> drop
    ]
    messages = ProtocolAdapter().adapt(events)
    assert [type(m).__name__ for m in messages] == ["Final"]


def test_partial_for_earlier_segment_is_dropped() -> None:
    """A partial for a segment below the last finalized one is stale -> dropped."""
    events = [
        _final(1, "Second.", 2.0, 3.0),
        _partial(0, "stale earlier segment", 0.0),  # below last final -> drop
        _partial(2, "fresh", 4.0),  # ahead of last final -> emitted
    ]
    messages = ProtocolAdapter().adapt(events)
    assert [(type(m).__name__, m.segment) for m in messages] == [
        ("Final", 1),
        ("Partial", 2),
    ]


def test_segments_count_tracks_finals_only() -> None:
    """``segments`` counts finals (feeds ``termination.segments``), not partials."""
    adapter = ProtocolAdapter()
    adapter.adapt([_partial(0, "a", 0.0), _partial(0, "ab", 0.0)])
    assert adapter.segments == 0
    adapter.adapt([_final(0, "Ab.", 0.0, 1.0)])
    assert adapter.segments == 1
    adapter.adapt([_final(1, "Cd.", 2.0, 3.0)])
    assert adapter.segments == 2


# --- Empty-final suppression ------------------------------------------------


@pytest.mark.parametrize("empty_text", ["", "   ", "\t", "\n", " \t\n "])
def test_empty_final_emits_no_message(empty_text: str) -> None:
    """An endpoint on pure silence (empty/whitespace final) emits nothing (§5)."""
    dropped = ProtocolAdapter().adapt_event(_final(0, empty_text, 0.0, 1.0))
    assert dropped is None
    assert ProtocolAdapter().adapt([_final(0, empty_text, 0.0, 1.0)]) == []


def test_empty_final_does_not_increment_segments() -> None:
    """A suppressed empty final is not a real utterance -> ``segments`` unchanged."""
    adapter = ProtocolAdapter()
    adapter.adapt([_final(0, "", 0.0, 1.0)])
    assert adapter.segments == 0


def test_empty_final_does_not_consume_a_segment_number() -> None:
    """A later hypothesis continues the same segment, not the next one.

    A suppressed empty final touches no segment bookkeeping, so a real partial
    for the *same* segment is still emitted and later finalized normally.
    """
    events = [
        _final(0, "   ", 0.0, 1.0),  # silence endpoint -> suppressed, no state change
        _partial(0, "actual words", 1.0),  # same segment continues
        _final(0, "Actual words.", 1.0, 2.5),
    ]
    adapter = ProtocolAdapter()
    messages = adapter.adapt(events)
    assert [(type(m).__name__, m.segment, m.text) for m in messages] == [
        ("Partial", 0, "actual words"),
        ("Final", 0, "Actual words."),
    ]
    assert adapter.segments == 1


def test_empty_final_preserves_monotonic_invariant() -> None:
    """Real finals surrounding a suppressed empty one stay monotonic (§5.3.3)."""
    events = [
        _partial(0, "hello", 0.0),
        _final(0, "Hello.", 0.0, 1.0),
        _final(1, "", 1.0, 3.0),  # silence-only endpoint -> suppressed
        _partial(1, "world", 3.0),  # segment 1 still valid (not finalized)
        _final(1, "World.", 3.0, 4.0),
    ]
    adapter = ProtocolAdapter()
    messages = adapter.adapt(events)
    finals = [m for m in messages if isinstance(m, Final)]
    assert [f.segment for f in finals] == [0, 1]
    assert [f.text for f in finals] == ["Hello.", "World."]
    assert adapter.segments == 2


def test_empty_final_does_not_finalize_segment_for_dedupe() -> None:
    """After a suppressed empty final, dedupe memory for the segment is intact."""
    adapter = ProtocolAdapter()
    first = adapter.adapt_event(_partial(0, "same", 0.0))
    empty = adapter.adapt_event(_final(0, "", 0.0, 1.0))  # suppressed no-op
    dup = adapter.adapt_event(_partial(0, "same", 0.0))  # still a consecutive dup
    assert first is not None
    assert empty is None
    assert dup is None  # dedupe survived the no-op empty final


# --- Timestamp coercion -----------------------------------------------------


def test_missing_start_coerced_to_zero_on_partial() -> None:
    """An engine partial without a start still yields a valid wire float."""
    (partial,) = ProtocolAdapter().adapt(
        [Event(kind="partial", segment=0, text="x", start=None)]
    )
    assert isinstance(partial, Partial)
    assert partial.start == 0.0


def test_missing_times_coerced_to_zero_on_final() -> None:
    """An engine final without start/end still yields valid wire floats."""
    (final,) = ProtocolAdapter().adapt(
        [Event(kind="final", segment=0, text="x", start=None, end=None)]
    )
    assert isinstance(final, Final)
    assert final.start == 0.0
    assert final.end == 0.0


# --- Single-event convenience API -------------------------------------------


def test_adapt_event_returns_none_for_dropped_partial() -> None:
    """The single-event API returns ``None`` when a partial is deduped."""
    adapter = ProtocolAdapter()
    assert adapter.adapt_event(_partial(0, "hi", 0.0)) is not None
    assert adapter.adapt_event(_partial(0, "hi", 0.0)) is None


def test_end_to_end_multi_segment_envelope_trajectory() -> None:
    """A realistic two-utterance stream yields the full ordered envelope list."""
    events = [
        _partial(0, "schedule", 0.0),
        _partial(0, "schedule the", 0.0),
        _partial(0, "schedule the", 0.0),  # deduped
        _partial(0, "schedule the meeting", 0.0),
        _final(0, "Schedule the meeting.", 0.0, 2.0),
        _partial(1, "for friday", 2.5),
        _final(1, "For Friday.", 2.5, 3.5),
    ]
    messages = ProtocolAdapter(word_timestamps=True).adapt(events)
    assert [(type(m).__name__, m.segment, m.text) for m in messages] == [
        ("Partial", 0, "schedule"),
        ("Partial", 0, "schedule the"),
        ("Partial", 0, "schedule the meeting"),
        ("Final", 0, "Schedule the meeting."),
        ("Partial", 1, "for friday"),
        ("Final", 1, "For Friday."),
    ]
    assert messages[3].start == 0.0 and messages[3].end == 2.0  # type: ignore[union-attr]
    assert messages[-1].start == 2.5 and messages[-1].end == 3.5  # type: ignore[union-attr]
