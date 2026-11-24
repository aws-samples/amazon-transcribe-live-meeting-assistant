# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Which transcript segments a periodic pass over a live meeting reads next.

Live guidance and the whiteboards' assistant each keep, on their state row, the
largest segment end time a run read (``LastSegmentEndTime``, the marker) and the
ids of the segments it read near that marker (``ShownSegmentIds``). A run reads
the segments that ended after the marker, and also those that ended within
:data:`OVERLAP_SECONDS` before it and are not among the recorded ids: a segment
on another channel can be finalized after a run read the transcript although it
ended before that run's marker (a long utterance), and without the overlap no
run would read it.

A segment here is anything with ``segment_id`` (str) and ``end`` (seconds from
the start of the meeting); each function keeps its own segment type.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, List, Optional, Protocol, Sequence, TypeVar

#: How far before the last run's marker a run looks again.
OVERLAP_SECONDS = 120.0


class Segment(Protocol):
    """The two fields of a transcript segment that the selection reads."""

    segment_id: str
    end: float


S = TypeVar("S", bound=Segment)


def seconds(value: Any) -> float:
    """A stored time as seconds; 0.0 for a value that is not a finite number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def new_segments(
    segments: Sequence[S], marker: Any, shown: Optional[Iterable[str]] = None
) -> List[S]:
    """The segments the last run did not read.

    Those that ended after the last run's marker and, when the run recorded the
    segments it read near its marker (``shown``), those that ended within
    OVERLAP_SECONDS before it and are not among them. A state row written before
    ``shown`` was recorded is read by the marker alone, as it was.
    """
    if marker is None:
        return list(segments)
    after = seconds(marker)
    if shown is None:
        return [s for s in segments if s.end > after]
    seen = set(shown)
    return [
        s
        for s in segments
        if s.end > after or (s.end > after - OVERLAP_SECONDS and s.segment_id not in seen)
    ]


def next_marker(fresh: Sequence[Segment], marker: Any) -> float:
    """The marker after a run that read ``fresh``: never earlier than the last one."""
    return max([s.end for s in fresh] + [seconds(marker)])


def shown_after_run(
    segments: Sequence[Segment],
    marker: Any,
    shown: Optional[Iterable[str]],
    fresh: Sequence[Segment],
) -> List[str]:
    """The ShownSegmentIds to record after a run that read ``fresh``.

    For a state row without them (``shown`` None, a row from before they were
    recorded), the earlier runs are taken to have read every segment up to
    their marker, as the marker-only reading did, so those are not read again.
    """
    if shown is None:
        old = seconds(marker) if marker is not None else None
        shown = [s.segment_id for s in segments if old is not None and s.end <= old]
    every = [*shown, *(s.segment_id for s in fresh)]
    return shown_near(segments, every, next_marker(fresh, marker))


def shown_near(segments: Sequence[Segment], shown: Iterable[str], marker: float) -> List[str]:
    """The ids of the segments read so far that end within OVERLAP_SECONDS of ``marker``.

    ``shown`` is every id the runs have read that may still matter: the last
    record plus this run's segments. Older ones are dropped, so the list stays
    as long as OVERLAP_SECONDS of transcript.
    """
    wanted = set(shown)
    return sorted(
        s.segment_id
        for s in segments
        if s.segment_id in wanted and s.end > marker - OVERLAP_SECONDS
    )
