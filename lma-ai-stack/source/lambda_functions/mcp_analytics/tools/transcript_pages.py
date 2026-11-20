# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""get_meeting_transcript with ``format: json`` -- a meeting's transcript, page by page.

The MCP server is a stateless Lambda behind a request/response endpoint, so it
cannot push lines to a client as they are spoken. Instead a client reads page
by page: each answer carries the segments after the ``nextToken`` it was given
and a new ``nextToken`` for the next call. While the meeting is being captured
a token is always returned, so a client can keep polling from it; once the
meeting has ended and the last segment has been read, it is null.

The segments are read from the EventSourcing table (transcript_rows), where the
call event processor writes each one while the meeting runs, so the transcript
can be read before the meeting ends as well as after.

The token is opaque to the client. It holds the latest end time returned (on
the meeting's transcript timeline) and the ids of the segments that end within
``OVERLAP_SECONDS`` of it. The two speech channels are finalized separately, so
a segment can be stored after a later one on the other channel; every call
therefore looks back over that window and leaves out only the ids it has
already returned. A segment finalized more than the window behind the newest
one returned is not returned to a client that has already read past it.
"""

import base64
import binascii
import json
from typing import Any, Dict, List, Optional

from tools.access import Caller, is_live, require_meeting
from tools.transcript_rows import as_line, end_of, kind_of, namer_for, read_final_rows

OVERLAP_SECONDS = 60.0
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000
# How often a client should poll a live meeting.
POLL_SECONDS = 5
CURSOR_VERSION = 1


def encode_cursor(call_id: str, watermark: float, seen: List[str]) -> str:
    """An opaque token for the next call."""
    raw = json.dumps({"v": CURSOR_VERSION, "m": call_id, "t": watermark, "ids": sorted(seen)})
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: Any, call_id: str) -> Optional[Dict[str, Any]]:
    """The token's content, or None when the client passed none."""
    if cursor in (None, ""):
        return None
    try:
        state = json.loads(base64.urlsafe_b64decode(str(cursor).encode("ascii")))
        ok = (
            isinstance(state, dict)
            and state.get("v") == CURSOR_VERSION
            and isinstance(state.get("t"), (int, float))
            and isinstance(state.get("ids"), list)
        )
    except (ValueError, binascii.Error, UnicodeError):
        ok = False
    if not ok:
        raise ValueError(
            "nextToken is not a token this tool returned; omit it to start from the beginning"
        )
    if state.get("m") != call_id:
        raise ValueError("nextToken is a token for a different meeting")
    return state


def _limit(value: Any) -> int:
    if value is None:
        return DEFAULT_LIMIT
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("maxSegments must be a whole number") from error
    return max(1, min(number, MAX_LIMIT))


def execute(
    caller: Caller,
    meeting_id: str,
    next_token: Optional[str] = None,
    max_segments: Any = None,
    include_assistant: bool = False,
) -> Dict[str, Any]:
    """The segments after ``next_token``, and the token to read on from."""
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or str(meeting_id).strip()
    state = decode_cursor(next_token, call_id)
    count = _limit(max_segments)

    rows = read_final_rows(call_id)
    wanted = ("speech", "chat", "assistant") if include_assistant else ("speech", "chat")
    rows = [row for row in rows if kind_of(row.get("Channel")) in wanted]
    name = namer_for(rows, meeting)

    watermark = float(state["t"]) if state else float("-inf")
    seen = set(state["ids"]) if state else set()
    fresh = [
        row
        for row in rows
        if end_of(row) > watermark - OVERLAP_SECONDS and row.get("SegmentId") not in seen
    ]
    # In order of where each segment ends, so that cutting the page short never
    # skips one: everything left over ends at or after the last one returned.
    fresh.sort(key=lambda row: (end_of(row), str(row.get("SegmentId"))))
    returned = fresh[:count]

    if returned:
        watermark = max(watermark, end_of(returned[-1]))
    elif not state:
        watermark = 0.0
    # Every id already returned that is still inside the look-back window.
    returned_ids = {sid for sid in seen if sid}
    returned_ids.update(str(row.get("SegmentId")) for row in returned)
    ends = {str(row.get("SegmentId")): end_of(row) for row in rows}
    window_ids = [
        sid for sid in returned_ids if ends.get(sid, float("-inf")) > watermark - OVERLAP_SECONDS
    ]

    live = is_live(meeting)
    more = len(fresh) > len(returned)
    answer: Dict[str, Any] = {
        "meetingId": call_id,
        "format": "json",
        "meetingStatus": meeting.get("Status", "UNKNOWN"),
        "segments": [as_line(row, name) for row in returned],
        # Null only once nothing more can come: the meeting has ended and the
        # last segment has been read.
        "nextToken": encode_cursor(call_id, watermark, window_ids) if live or more else None,
        "hasMore": more,
        "pollAfterSeconds": POLL_SECONDS if live and not more else None,
    }
    if meeting.get("TranscriptDeletedAt"):
        answer["transcriptDeleted"] = True
    return answer
