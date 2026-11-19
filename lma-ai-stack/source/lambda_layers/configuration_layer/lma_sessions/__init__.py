# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A meeting's capture sessions: who may reopen a meeting, and the next session.

A meeting is captured in *sessions*. The first is the capture that created it.
When its owner or an Admin reopens it -- by rejoining its Virtual Participant
(``rejoinVirtualParticipant``), or to continue it from a streaming client
(``reopenMeeting``) -- the meeting record is given the number of one more
session and where that session starts on the transcript timeline. The meeting
stays as it is until that session's START event arrives; the call event
processor then makes it live again with ``startCallSession``, which accepts only
the session allowed here. See docs/virtual-participant.md#rejoin and
docs/desktop-capture-app.md#continue-an-existing-meeting.

Shared by the Virtual Participant manager (rejoin.py) and the meeting reopen
resolver, so the two allow a session the same way. The functions that touch
DynamoDB take the event sourcing table as an argument.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional

#: What allowed a session, recorded on the meeting as ``ReopenedVia`` beside
#: ``ReopenedBy``: a Virtual Participant rejoin, or reopenMeeting for a
#: streaming client. reopenMeeting hands a still-pending session back only to
#: the caller who allowed it with reopenMeeting, never a rejoin's.
VIA_REJOIN = "rejoinVirtualParticipant"
VIA_REOPEN_MEETING = "reopenMeeting"

#: Seconds between the end of the earlier sessions and the start of the new one
#: on the transcript timeline.
SESSION_GAP_SECONDS = 1


class SessionNotAllowed(Exception):
    """The meeting record changed while a session was being allowed on it: it was
    deleted, its transcript was deleted, or a later session was allowed first."""


def meeting_key(call_id: str) -> Dict[str, str]:
    """The key of a meeting's record in the event sourcing table."""
    return {"PK": f"c#{call_id}", "SK": f"c#{call_id}"}


def session_number(value: Any) -> int:
    """A stored session number: absent, or anything below 2, is the first session."""
    if isinstance(value, bool):
        return 1
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return max(number, 1)


def next_session_number(*stored: Any) -> int:
    """One more than the highest of the stored session numbers given.

    Pass every number that may name a session already allowed or started: the
    meeting's ``SessionNumber`` and ``SessionStartedNumber``, and for a rejoin
    the Virtual Participant row's ``sessionNumber``.
    """
    return max([session_number(value) for value in stored], default=1) + 1


def _seconds(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 0.0
    return seconds if math.isfinite(seconds) and seconds > 0 else 0.0


def next_session_offset(transcript_end_times: Iterable[Any], meeting: Mapping[str, Any]) -> int:
    """Where the new session starts on the transcript timeline, in whole seconds.

    After the latest end time of anything in the transcript -- speech, chat
    messages and assistant answers alike -- after the meeting's recorded
    duration, and after the start of the latest earlier session; rounded up to a
    whole second, plus SESSION_GAP_SECONDS. With nothing to follow, the session
    starts at 0.
    """
    latest = max(
        [
            *(_seconds(end) for end in transcript_end_times),
            _seconds(meeting.get("TotalConversationDurationMillis")) / 1000.0,
            _seconds(meeting.get("SessionOffsetSeconds")),
        ],
        default=0.0,
    )
    if latest <= 0:
        return 0
    return math.ceil(latest) + SESSION_GAP_SECONDS


def read_meeting(table: Any, call_id: str) -> Optional[Dict[str, Any]]:
    """The meeting's record, read consistently, or None when there is none."""
    response = table.get_item(Key=meeting_key(call_id), ConsistentRead=True)
    return response.get("Item")


def transcript_end_times(table: Any, call_id: str) -> Iterator[Any]:
    """The EndTime of every item in the meeting's transcript, a page at a time."""
    query: Dict[str, Any] = {
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": f"trs#{call_id}"},
        "ProjectionExpression": "#end",
        "ExpressionAttributeNames": {"#end": "EndTime"},
    }
    while True:
        page = table.query(**query)
        for item in page.get("Items", []):
            yield item.get("EndTime")
        if not page.get("LastEvaluatedKey"):
            return
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def allow_session(
    table: Any,
    call_id: str,
    session: int,
    offset: int,
    reopened_by: str,
    *,
    via: str,
    now: str = "",
) -> None:
    """Record on the meeting the session its next START may open.

    Conditional: the record must exist, its transcript must not have been
    deleted, and no session as high as this one may have been allowed already.
    Raises SessionNotAllowed when the condition fails.
    """
    # Imported here so the pure helpers above load without botocore.
    from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

    try:
        table.update_item(
            Key=meeting_key(call_id),
            UpdateExpression=(
                "SET SessionNumber = :session, SessionOffsetSeconds = :offset, "
                "ReopenedAt = :now, ReopenedBy = :by, ReopenedVia = :via"
            ),
            ConditionExpression=(
                "attribute_exists(PK) AND attribute_not_exists(TranscriptDeletedAt) "
                "AND (attribute_not_exists(SessionNumber) OR SessionNumber < :session)"
            ),
            ExpressionAttributeValues={
                ":session": session,
                ":offset": Decimal(offset),
                ":now": now or now_iso(),
                ":by": reopened_by,
                ":via": via,
            },
        )
    except ClientError as error:
        if (
            str(error.response.get("Error", {}).get("Code", ""))
            == "ConditionalCheckFailedException"
        ):
            raise SessionNotAllowed(call_id) from error
        raise


def caller_groups(identity: Optional[Mapping[str, Any]]) -> List[str]:
    """The caller's Cognito groups, from either place AppSync puts them."""
    caller: Mapping[str, Any] = identity or {}
    groups = caller.get("groups") or (caller.get("claims") or {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [group.strip() for group in groups.split(",") if group.strip()]
    return list(groups)


def caller_owns_meeting(
    identity: Optional[Mapping[str, Any]], meeting: Optional[Mapping[str, Any]]
) -> bool:
    """Whether the caller is an Admin or the meeting's Owner.

    The rule for reopening a meeting. Narrower than reading one: a user the
    meeting is shared with may read it but not reopen it.
    """
    caller: Mapping[str, Any] = identity or {}
    if "Admin" in caller_groups(caller):
        return True
    username = caller.get("username")
    return bool(username) and meeting is not None and meeting.get("Owner") == username


def who(identity: Optional[Mapping[str, Any]]) -> str:
    """How the caller is recorded on the meeting: their email, else their username."""
    caller: Mapping[str, Any] = identity or {}
    claims = caller.get("claims") or {}
    return str(claims.get("email") or caller.get("username") or "unknown")
