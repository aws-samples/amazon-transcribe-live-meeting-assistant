# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""A meeting's stored transcript: its final speech segments and chat lines.

Read from the EventSourcing table (PK ``trs#<CallId>``), where the call event
processor writes each segment while the meeting runs, so the transcript can be
read before the meeting ends as well as after. Speakers are named as the
meeting page and the summaries name them, with the configuration layer's
``speaker_namer`` over the meeting's speaker identity map.
"""

from typing import Any, Callable, Dict, List

from boto3.dynamodb.conditions import Attr, Key

from tools.access import calls_table

# Upper bound on Query pages for one meeting, as fetch_transcript bounds it: far
# above any real meeting, so that a table which keeps returning a continuation
# key cannot hold the function for its whole timeout.
MAX_PAGES = 100

MEETING_CHAT = "MEETING_CHAT"
ASSISTANT_CHANNELS = ("AGENT_ASSISTANT", "CHAT_ASSISTANT")
# The speech channels, under their current and pre-0.4.0 names
# (lma_channels.STORED_SPEECH_CHANNELS).
SPEECH_CHANNELS = ("REMOTE", "LOCAL", "CALLER", "AGENT")
CANONICAL_CHANNEL = {"CALLER": "REMOTE", "AGENT": "LOCAL"}


def read_final_rows(call_id: str) -> List[Dict[str, Any]]:
    """Every final segment of the meeting, on every channel, across all pages."""
    table = calls_table()
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"trs#{call_id}"),
        "FilterExpression": Attr("IsPartial").eq(False),
    }
    rows: List[Dict[str, Any]] = []
    for _ in range(MAX_PAGES):
        response = table.query(**kwargs)
        rows.extend(response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return rows


def kind_of(channel: Any) -> str:
    """speech, chat or assistant: what a segment's channel holds."""
    if channel == MEETING_CHAT:
        return "chat"
    if channel in ASSISTANT_CHANNELS:
        return "assistant"
    return "speech"


def namer_for(rows: List[Dict[str, Any]], meeting: Dict[str, Any]) -> Callable[[Dict], str]:
    """A function naming each row's speaker as the meeting page does.

    The configuration layer's speaker_namer, over the meeting's speech rows
    (fetch_transcript.speaker_namer_for). Chat lines and the assistant's
    answers carry their speaker as written.
    """
    speech = [row for row in rows if kind_of(row.get("Channel")) == "speech"]
    try:
        # pylint: disable-next=import-error,import-outside-toplevel
        from lma_speakers import speaker_namer

        name_speech = speaker_namer(
            speech, meeting.get("SpeakerIdentities"), meeting.get("CustomerPhoneNumber")
        )
    except ImportError:  # the layer is absent only outside Lambda

        def name_speech(row):
            return str(row.get("Speaker") or "")

    def name(row: Dict[str, Any]) -> str:
        if kind_of(row.get("Channel")) == "speech":
            return name_speech(row)
        return str(row.get("Speaker") or "")

    return name


def seconds(value: Any) -> float:
    """A stored time as seconds; 0 when absent."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def end_of(row: Dict[str, Any]) -> float:
    """Where a row ends on the meeting's timeline (its start, when it has no end)."""
    return seconds(row.get("EndTime", row.get("StartTime")))


def as_line(row: Dict[str, Any], name: Callable[[Dict], str]) -> Dict[str, Any]:
    """One row as a tool returns it."""
    channel = row.get("Channel")
    return {
        "segmentId": row.get("SegmentId"),
        "type": kind_of(channel),
        "channel": CANONICAL_CHANNEL.get(str(channel), channel),
        "speaker": name(row),
        "text": row.get("Transcript", ""),
        "startTime": seconds(row.get("StartTime")),
        "endTime": end_of(row),
    }
