"""Adapter from recognizer ``Event`` values to wire messages.

The streaming recogniser (:mod:`asr_server.recognizer`) emits engine-internal
:class:`~asr_server.recognizer.Event` objects — growing ``partial`` hypotheses
for the active segment and a ``final`` when the segment is finalized at an
endpoint (or on flush). This module maps those onto the ``asr_protocol`` wire
messages the client actually receives, implementing the contract in
``design.md`` §5.3:

1. Each engine ``partial`` for segment *N* becomes a
   :class:`asr_protocol.Partial` (``type="partial"``). **Identical consecutive
   partial text is deduped** so the same interim hypothesis is never sent twice
   in a row.
2. Each engine ``final`` for segment *N* becomes a :class:`asr_protocol.Final`
   (``type="final"``) carrying ``start``/``end``; per-word timings are attached
   as :class:`asr_protocol.Word` entries **only when word timestamps are enabled
   and the engine provided them** (the streaming path leaves them empty).
3. A **monotonic** ``segment`` counter is preserved across the session and a
   segment, once finalized, is **never re-emitted as a partial** — a late or
   stray partial for a finalized (or earlier) segment is dropped.

The adapter is deliberately defensive: it re-enforces the dedupe / monotonicity
invariants itself rather than trusting the engine, so the wire contract holds
even if an engine variant is looser about them.
It is the single seam where engine events cross into the wire schema; the
recogniser never imports ``asr_protocol`` and the two evolve independently.
"""

from __future__ import annotations

from collections.abc import Iterable

from asr_protocol import Final, Partial, Word

from asr_server.recognizer import Event

__all__ = [
    "WireMessage",
    "ProtocolAdapter",
]

# The subset of server->client messages this adapter produces from engine events.
# (``ready``/``termination``/``error`` are owned by the WebSocket server layer.)
WireMessage = Partial | Final


def _coerce_time(value: float | None) -> float:
    """Wire timestamps are non-optional floats; treat a missing time as 0.0.

    Engine ``partial``/``final`` events carry a real ``start`` (and ``end`` on a
    final) in practice, but the fields are typed optional; anchoring an absent
    time to 0.0 keeps the wire model valid rather than raising mid-stream.
    """
    return value if value is not None else 0.0


class ProtocolAdapter:
    """Maps engine :class:`Event` objects onto ``Partial``/``Final`` wire messages.

    One instance per session (it carries per-session state: the last emitted
    partial and the set of finalized segments). Feed it engine events in arrival
    order via :meth:`adapt_event` (one event) or :meth:`adapt` (a batch); it
    returns the wire messages to send, dropping anything the §5.3 contract says
    must not go on the wire (deduped partials, partials for finalized segments).

    ``word_timestamps`` mirrors the session ``Config.word_timestamps`` flag: when
    ``False`` a ``final`` is emitted with an empty ``words`` list even if the
    engine supplied timings.
    """

    def __init__(self, *, word_timestamps: bool = True) -> None:
        self._word_timestamps = word_timestamps
        # (segment, text) of the last partial actually emitted; used to dedupe
        # identical consecutive partials. Keyed by segment too, so the first
        # partial of a new segment is never mistaken for a repeat of the last.
        self._last_partial: tuple[int, str, str | None] | None = None
        # Segments already finalized: a partial for any of these is dropped.
        self._finalized: set[int] = set()
        # Highest segment finalized so far; partials at or below it are stale.
        self._last_final_segment = -1
        # Count of finals emitted this session (feeds ``termination.segments``).
        self._segments = 0

    @property
    def segments(self) -> int:
        """Number of segments finalized this session (for ``termination``)."""
        return self._segments

    def adapt(self, events: Iterable[Event]) -> list[WireMessage]:
        """Map a batch of engine events to the wire messages to send, in order."""
        messages: list[WireMessage] = []
        for event in events:
            message = self.adapt_event(event)
            if message is not None:
                messages.append(message)
        return messages

    def adapt_event(self, event: Event) -> WireMessage | None:
        """Map one engine event to a wire message, or ``None`` if it is dropped.

        Returns ``None`` when a partial is a duplicate of the previous one or
        targets an already-finalized/earlier segment (§5.3 rules 1 and 3), or
        when a final carries no transcript (empty-final suppression, see
        :meth:`_adapt_final`).
        """
        if event.kind == "final":
            return self._adapt_final(event)
        return self._adapt_partial(event)

    def _adapt_partial(self, event: Event) -> Partial | None:
        # Rule 3: never re-emit a finalized segment (or an earlier one) as a
        # partial — the client has already seen its final.
        if event.segment in self._finalized or event.segment <= self._last_final_segment:
            return None
        # Rule 1: dedupe identical consecutive partial text for this segment.
        # The speaker is part of the key: the same text re-attributed to a
        # different speaker is new information, not a duplicate.
        key = (event.segment, event.text, event.speaker)
        if key == self._last_partial:
            return None
        self._last_partial = key
        return Partial(
            segment=event.segment,
            text=event.text,
            start=_coerce_time(event.start),
            speaker=event.speaker,
        )

    def _adapt_final(self, event: Event) -> Final | None:
        # Empty-final suppression: an endpoint firing on pure trailing silence
        # (endpointing rule1) can yield a final with no hypothesis. There was no
        # real utterance, so emit nothing and — deliberately — touch NO state:
        #   * no Final goes on the wire (never emit ``text=""``);
        #   * ``_segments`` is not incremented (it counts real, non-empty finals
        #     that feed ``termination.segments``);
        #   * the segment is NOT recorded finalized and ``_last_final_segment``
        #     is NOT advanced, so this empty final does not consume a segment
        #     number — a later real hypothesis simply continues the current
        #     segment rather than skipping one;
        #   * ``_last_partial`` is left intact so dedupe still holds across the
        #     no-op (the segment genuinely continues).
        # This never emits an empty final and never breaks the monotonic-segment
        # invariant: because nothing was finalized, a subsequent partial for the
        # same segment remains valid and monotonic.
        if event.text.strip() == "":
            return None
        # Rule 2 + 3: emit the final, record the segment as finalized, and clear
        # the partial-dedupe memory so the next segment starts fresh.
        self._finalized.add(event.segment)
        self._last_final_segment = max(self._last_final_segment, event.segment)
        self._last_partial = None
        self._segments += 1
        words = self._wire_words(event)
        return Final(
            segment=event.segment,
            text=event.text,
            start=_coerce_time(event.start),
            end=_coerce_time(event.end),
            words=words,
            speaker=event.speaker,
        )

    def _wire_words(self, event: Event) -> list[Word]:
        """Convert engine word timings to wire ``Word``s when enabled/present."""
        if not self._word_timestamps or not event.words:
            return []
        return [Word(w=wt.w, s=wt.s, e=wt.e) for wt in event.words]
