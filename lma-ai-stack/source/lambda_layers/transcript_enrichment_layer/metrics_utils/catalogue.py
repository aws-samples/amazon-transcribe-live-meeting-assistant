# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The metrics LMA publishes, as data rather than as scattered string literals.

This module is the single source of truth for the metric names, and it is
imported by three different readers:

* ``metrics_utils.emf``, which refuses to emit a name that is not listed here,
  so a typo at a call site fails in the emitter rather than creating a second,
  near-identical series nobody is watching;
* the Lambda functions that emit, which name a metric by its constant;
* a static gate under ``lma-ai-stack/test/`` which checks every entry against
  the dashboard and the alarms in ``lma-main.yaml``.

It therefore imports nothing outside the standard library — see the package
docstring for why that constraint exists.

The metrics the WebSocket transcriber emits are listed here too, even though
nothing in Python emits them. They belong in the same catalogue because they
share the namespace and because the gate that checks a metric reaches a widget
should not have a hole in it wherever the emitter happens to be TypeScript. The
transcriber's own copy of these names lives in
``lma-websocket-transcriber-stack/source/app/src/metrics/catalogue.ts`` and a
gate asserts the two agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# The environment variable every emitter reads to learn its namespace, and the
# name the templates set. Deliberately one name across both languages and every
# component: the namespace is how a dashboard, an alarm and (later) an operator
# asking "what is this stack doing" all find the same series, and a second name
# for it is the start of two half-populated namespaces.
NAMESPACE_ENV_VAR = "METRIC_NAMESPACE"

# The unit strings CloudWatch accepts. Spelled out so a typo in the catalogue
# fails a unit test rather than being rejected per-record at ingestion time,
# where the only symptom is a series that never appears.
CLOUDWATCH_UNITS = frozenset(
    {
        "Seconds",
        "Microseconds",
        "Milliseconds",
        "Bytes",
        "Kilobytes",
        "Megabytes",
        "Gigabytes",
        "Terabytes",
        "Bits",
        "Kilobits",
        "Megabits",
        "Gigabits",
        "Terabits",
        "Percent",
        "Count",
        "Bytes/Second",
        "Kilobytes/Second",
        "Megabytes/Second",
        "Gigabytes/Second",
        "Terabytes/Second",
        "Bits/Second",
        "Kilobits/Second",
        "Megabits/Second",
        "Gigabits/Second",
        "Terabits/Second",
        "Count/Second",
        "None",
    }
)


class Component(str, Enum):
    """Which part of the solution publishes a metric.

    Not a metric dimension — it is how the catalogue records where to look for
    the emitting code, and it lets the gate report a missing widget against the
    component whose author has to fix it.
    """

    CALL_EVENT_PROCESSOR = "call-event-processor"
    WEBSOCKET_TRANSCRIBER = "websocket-transcriber"


@dataclass(frozen=True)
class MetricDefinition:
    """One metric name, with everything the gates need to check it."""

    name: str
    unit: str
    component: Component
    #: True when the condition this metric records is one the code otherwise
    #: swallows: an exception that is caught and logged, or a path that reports
    #: success having done nothing. Those are the metrics that must have an
    #: alarm and not merely a widget, because the number is the only notice
    #: anybody gets. The gate enforces it.
    records_swallowed_failure: bool
    #: What the number means, in a sentence an operator can act on. Used in the
    #: docs and worth keeping next to the name so the two cannot drift.
    meaning: str


# ── meeting lifecycle ───────────────────────────────────────────────────────
#
# One group answering one question: a meeting was asked for — did a transcript
# come out of it, how long did that take, and did anything get dropped on the
# way? The counts come from the Call Event Processor, which is the system of
# record for a meeting and sees every source including uploads. The latency and
# the session counts come from the WebSocket transcriber, which is the only
# place that holds per-meeting state for a live audio session and can therefore
# tell a meeting's first transcript segment from its hundredth.

MEETINGS_STARTED = "MeetingsStarted"
MEETINGS_ENDED = "MeetingsEnded"
TRANSCRIPT_SEGMENTS_WRITTEN = "TranscriptSegmentsWritten"
TRANSCRIPT_SEGMENT_WRITE_FAILED = "TranscriptSegmentWriteFailed"
STREAMING_SESSIONS_STARTED = "StreamingSessionsStarted"
TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS = "TimeToFirstTranscriptSegmentMs"
STREAMING_SESSIONS_WITHOUT_TRANSCRIPT = "StreamingSessionsWithoutTranscript"


CATALOGUE: tuple[MetricDefinition, ...] = (
    MetricDefinition(
        name=MEETINGS_STARTED,
        unit="Count",
        component=Component.CALL_EVENT_PROCESSOR,
        records_swallowed_failure=False,
        meaning=(
            "A meeting record was created. Counted once per meeting: a "
            "redelivered start event finds the record already there and is not "
            "counted again."
        ),
    ),
    MetricDefinition(
        name=MEETINGS_ENDED,
        unit="Count",
        component=Component.CALL_EVENT_PROCESSOR,
        records_swallowed_failure=False,
        meaning=(
            "A meeting was marked ended. Counted once per meeting. Read against "
            "MeetingsStarted over a long window rather than a short one: a live "
            "meeting is legitimately started and not yet ended."
        ),
    ),
    MetricDefinition(
        name=TRANSCRIPT_SEGMENTS_WRITTEN,
        unit="Count",
        component=Component.CALL_EVENT_PROCESSOR,
        records_swallowed_failure=False,
        meaning=(
            "Transcript segment writes that AppSync accepted. Counts partial "
            "segments as well as final ones, so it tracks pipeline throughput "
            "rather than the number of things said."
        ),
    ),
    MetricDefinition(
        name=TRANSCRIPT_SEGMENT_WRITE_FAILED,
        unit="Count",
        component=Component.CALL_EVENT_PROCESSOR,
        records_swallowed_failure=True,
        meaning=(
            "Transcript segment writes that failed. These are gathered with "
            "return_exceptions=True and collected into the invocation's result, "
            "so the invocation succeeds, the Kinesis record is not retried and "
            "AWS/Lambda Errors stays flat. This metric is the only notice that "
            "a transcript line was lost, which is why it is alarmed."
        ),
    ),
    MetricDefinition(
        name=STREAMING_SESSIONS_STARTED,
        unit="Count",
        component=Component.WEBSOCKET_TRANSCRIBER,
        records_swallowed_failure=False,
        meaning=(
            "A live audio session opened for a meeting, and the denominator for "
            "StreamingSessionsWithoutTranscript. One per audio session, not per "
            "meeting: a client that reconnects for the same meeting opens a "
            "second session and is counted again, where MeetingsStarted is not."
        ),
    ),
    MetricDefinition(
        name=TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS,
        unit="Milliseconds",
        component=Component.WEBSOCKET_TRANSCRIBER,
        records_swallowed_failure=False,
        meaning=(
            "Milliseconds from a meeting's audio session opening to its first "
            "transcript segment reaching Kinesis. Emitted once per session. "
            "Dominated by when somebody first speaks, not by pipeline latency, "
            "so read the shape of the distribution and not a single value, and "
            "do not alarm on it — a quiet meeting is not a broken one. The "
            "alarm for a meeting that produced nothing at all is on "
            "StreamingSessionsWithoutTranscript."
        ),
    ),
    MetricDefinition(
        name=STREAMING_SESSIONS_WITHOUT_TRANSCRIPT,
        unit="Count",
        component=Component.WEBSOCKET_TRANSCRIBER,
        records_swallowed_failure=True,
        meaning=(
            "A meeting's audio session closed having produced no transcript "
            "segment at all, after running long enough that silence is a fault "
            "rather than somebody changing their mind. Nothing else reports "
            "this: the session closes normally, the meeting record reaches "
            "ENDED, and the meeting page shows an empty transcript with no "
            "error anywhere. Alarmed for that reason."
        ),
    ),
)


BY_NAME: dict[str, MetricDefinition] = {metric.name: metric for metric in CATALOGUE}


def names_for(component: Component) -> tuple[str, ...]:
    """Every metric name the given component publishes."""
    return tuple(metric.name for metric in CATALOGUE if metric.component is component)


def must_be_alarmed() -> tuple[str, ...]:
    """Metric names whose condition is invisible unless an alarm notifies.

    A number nobody is paged on, recording an error nobody saw, is not
    observability — so the gate uses this to require an alarm, not a widget.
    """
    return tuple(metric.name for metric in CATALOGUE if metric.records_swallowed_failure)
