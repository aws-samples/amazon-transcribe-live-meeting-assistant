# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Computation from call_event_processor that touches nothing outside itself.

`call_event_processor.py` cannot be imported without aiohttp, aws_lambda_powertools
and a Lambda layer on the path, so none of the logic inside it was reachable from a
test. These functions are the parts that only transform their arguments, moved here
so they can be called directly. Nothing else changed: `call_event_processor`
imports them from here (as a sibling at the function root, the way lambda_function.py
imports batch_item_failures) and its behaviour is identical.

The same idea as `decideAttendeeAction` in the Virtual Participant's `teams.ts` —
the decision worth testing is separated from the I/O that surrounds it. Where a
function previously read module state (the wake-phrase pattern came from a global
`SETTINGS` dict), it now takes that state as an argument, which is what makes it
testable rather than merely importable.

No AWS imports here, deliberately. Anything needing a boto3 client or a GraphQL
session belongs in call_event_processor.py, not in this module.
"""

from __future__ import annotations

from statistics import fmean
from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, Pattern, TypedDict

SentimentLabelType = Literal["NEGATIVE", "MIXED", "NEUTRAL", "POSITIVE"]
# The speech channels sentiment is aggregated for (lma_channels.SPEECH_CHANNELS).
ChannelType = Literal["REMOTE", "LOCAL"]
SentimentPeriodType = Literal["QUARTER"]

# The meeting timeline is divided into this many equal periods.
QUARTER_COUNT = 4


class SentimentEntry(TypedDict):
    """Sentiment Shape

    Held in a list per channel
    """

    Id: str
    BeginOffsetMillis: float
    EndOffsetMillis: float
    Sentiment: SentimentLabelType
    Score: float


class SentimentPerChannel(TypedDict):
    """StatePerChannel Shape

    Holds state per channel under StatePerCallId. Use to keep values needed
    for statistics and aggregations.
    """

    SentimentList: List[SentimentEntry]


class SentimentByPeriodEntry(TypedDict):
    """Sentiment By Period Shape"""

    BeginOffsetMillis: float
    EndOffsetMillis: float
    Score: float


class Sentiment(TypedDict):
    """Sentiment Shape"""

    OverallSentiment: Dict[ChannelType, float]
    SentimentByPeriod: Dict[SentimentPeriodType, Dict[ChannelType, List[SentimentByPeriodEntry]]]


def get_sentiment_per_quarter(
    sentiment_list: List[SentimentEntry],
) -> List[SentimentByPeriodEntry]:
    """Average the sentiment scores into four equal periods of the meeting.

    Always returns exactly `QUARTER_COUNT` entries, so the chart has a fixed
    number of points whatever the input. A period containing no sentiment gets
    zeroes, which the UI renders as a gap rather than as neutral sentiment.

    Segments are assigned to a period by their END offset, and the boundaries are
    exclusive at the start and inclusive at the end — so a segment ending exactly
    on a boundary belongs to the earlier period and is never counted twice.
    """
    sorted_sentiment = sorted(sentiment_list, key=lambda i: i["BeginOffsetMillis"])
    min_begin_time: float = (
        min(
            sorted_sentiment,
            key=lambda i: i["BeginOffsetMillis"],
        ).get("BeginOffsetMillis", 0.0)
        if sorted_sentiment
        else 0.0
    )
    max_end_time: float = (
        max(sorted_sentiment, key=lambda i: i["EndOffsetMillis"]).get("EndOffsetMillis", 0.0)
        if sorted_sentiment
        else 0.0
    )
    time_range: float = max_end_time - min_begin_time
    time_ranges = (
        (
            max((min_begin_time + time_range * i / QUARTER_COUNT), min_begin_time),
            min((min_begin_time + time_range * (i + 1) / QUARTER_COUNT), max_end_time),
        )
        for i in range(QUARTER_COUNT)
    )
    quarters = (
        [
            s
            for s in sorted_sentiment
            if s["EndOffsetMillis"] > time_range[0] and s["EndOffsetMillis"] <= time_range[1]
        ]
        for time_range in time_ranges
    )
    sentiment_per_quarter = [
        SentimentByPeriodEntry(
            {
                "Score": fmean((i["Score"] for i in quarter)) if quarter else 0,
                "BeginOffsetMillis": (
                    min((i["BeginOffsetMillis"] for i in quarter)) if quarter else 0
                ),
                "EndOffsetMillis": (max((i["EndOffsetMillis"] for i in quarter)) if quarter else 0),
            }
        )
        for quarter in quarters
    ]

    return sentiment_per_quarter


def matches_wake_phrase(pattern: Optional[Pattern[str]], transcript: str) -> bool:
    """Whether a transcript should wake the Meeting Assistant.

    The pattern is passed in rather than read from module state, which is the only
    change from the original: the caller still supplies the compiled regex from
    the deployment's settings, so the decision is unchanged.

    Matched with `search`, not `fullmatch`, so the phrase can appear anywhere in
    the utterance. `None` means no pattern is configured and nothing wakes it.
    """
    if pattern is None:
        return False
    return pattern.search(transcript) is not None


class SegmentAnalyses(NamedTuple):
    """Which per-segment analyses a stored transcript segment is given."""

    sentiment: bool
    agent_assist: bool


def segment_analyses(
    *,
    is_speech: bool,
    is_partial: bool,
    sentiment_enabled: bool,
    agent_assist_enabled: bool,
    wake_pattern: Callable[[], Optional[Pattern[str]]],
    transcript: str,
) -> SegmentAnalyses:
    """Whether a segment gets Amazon Comprehend sentiment, and wakes the Meeting Assistant.

    Both read what was said, so both are for a final segment on a speech
    channel only. A message typed in the meeting chat (the MEETING_CHAT
    channel) is stored like a segment but gets neither: its sentiment would be
    counted with the speakers', and a wake phrase typed in the chat is not
    somebody asking the assistant aloud. ``is_speech`` is the caller's
    ``lma_channels.is_speech_channel(channel)``; it is passed in so this module
    keeps no imports outside the standard library. ``wake_pattern`` is called
    only for a segment that could wake the assistant, because reading it reads
    the configuration.
    """
    final_speech = is_speech and not is_partial
    return SegmentAnalyses(
        sentiment=final_speech and sentiment_enabled,
        agent_assist=final_speech
        and agent_assist_enabled
        and matches_wake_phrase(wake_pattern(), transcript),
    )


def convert_keys_to_uppercamelcase(d: Dict[str, Any]) -> Dict[str, Any]:
    """Upper-case the first letter of every key, recursively through nested dicts.

    Used to bring an incoming payload's camelCase keys into line with the
    UpperCamelCase the GraphQL schema uses. Only the first character changes; the
    rest of each key is left alone, so `callId` becomes `CallId` and `CallId`
    stays as it is.
    """
    new_dict: Dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            new_dict[k[0].upper() + k[1:]] = convert_keys_to_uppercamelcase(v)
        else:
            new_dict[k[0].upper() + k[1:]] = v
    return new_dict


def merge_dicts(d1: Dict[Any, Any], d2: Dict[Any, Any]) -> Dict[Any, Any]:
    """`d1` overlaid with `d2`, without modifying either.

    `d2` wins on a shared key. Returns a new dict, so a caller holding `d1` does
    not see it change underneath them.
    """
    new_dict = d1.copy()
    new_dict.update(d2)
    return new_dict


def agenda_derivation_due(
    call_id: Optional[str],
    end_time: Any,
    after_seconds: float,
    already_queued: "set[str]",
) -> bool:
    """Whether a final segment should queue the automatic agenda pass.

    True once the meeting's transcript reaches ``after_seconds`` from its start,
    the first time this execution environment sees that for the meeting. The
    environment-local set only keeps a busy meeting from queueing a pass per
    segment; it is not what makes the pass run once. Other environments queue it
    too, and the deriver's own conditional claim on the meeting record is what
    lets only one of them reach the model. ``after_seconds`` of 0 or less turns
    the automatic pass off.
    """
    if not call_id or after_seconds <= 0 or call_id in already_queued:
        return False
    try:
        return float(end_time) >= after_seconds
    except (TypeError, ValueError):
        return False


def apply_meeting_access(segment: Dict[str, Any], meeting: Dict[str, Any]) -> None:
    """Gives a transcript segment its meeting's Owner and SharedWith, in place.

    ``meeting`` is what ``get_call_details`` read from the meeting record, so the
    access fields come from the record rather than from the event. SharedWith is
    the meeting's, None included, as it has always been for a segment. Owner is
    set when the meeting has one; a meeting without one leaves the segment's
    Owner as normalization made it (from a verified access token, or None).

    Every write of a segment to ``addTranscriptSegment`` replaces the whole
    item, so each write of the same segment -- the transcript, then its
    sentiment -- has to carry these fields.
    """
    segment["SharedWith"] = meeting.get("SharedWith")
    owner = meeting.get("Owner")
    if owner is not None:
        segment["Owner"] = owner
