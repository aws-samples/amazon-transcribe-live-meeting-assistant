# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""reopenMeeting: allow one more capture session on an existing meeting.

A streaming client (the Desktop Capture App) that is to continue an existing
meeting calls this first, then streams to the WebSocket transcriber with the
meeting's CallId and the session number answered here. See
docs/desktop-capture-app.md#continue-an-existing-meeting and
docs/websocket-streaming-api.md.

What it does, in order:

1. Reads the meeting record. The caller must be the meeting's owner or an Admin;
   being one of the people the meeting is shared with is not enough.
2. Refuses a meeting whose transcript has been deleted, and one that is still
   being captured (Status STARTED or TRANSCRIBING).
3. Works out the new session's number and where it starts on the transcript
   timeline, as a Virtual Participant rejoin does (lma_sessions).
4. Allows the session on the meeting record with a conditional write.

The meeting itself is not changed otherwise: it stays as it is until the
session's START event arrives, and the call event processor then makes it live
again (startCallSession), once. A session that never starts leaves the meeting,
and any transcript deletion scheduled for it, as they were.

A refusal is answered as ``{"errorType": ..., "message": ...}``, which the
resolver's response template raises as a GraphQL error of that type, so a
client can tell the refusals apart:

* ``MeetingNotFound`` -- no meeting has this CallId.
* ``Unauthorized`` -- the caller is not the meeting's owner or an Admin.
* ``TranscriptDeleted`` -- the meeting's transcript has been deleted.
* ``MeetingInProgress`` -- the meeting is still being captured, or a session
  allowed on it in the last PENDING_SESSION_SECONDS (a Virtual Participant
  rejoin that is still joining, for example) has not started yet.
* ``MeetingChanged`` -- the record changed during the call; try again.
* ``BadRequest`` -- the CallId is missing or not usable.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import boto3
import lma_sessions

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

#: Statuses of a meeting that is still being captured.
LIVE_STATUSES = ("STARTED", "TRANSCRIBING")

#: How long a session allowed on the meeting (by a rejoin or an earlier
#: reopenMeeting) is waited for before another may be allowed: long enough for
#: a Virtual Participant to launch and be admitted. A session that has not
#: started by then is taken to have been abandoned.
PENDING_SESSION_SECONDS = 15 * 60


def pending_session(meeting: Mapping[str, Any], now: Optional[datetime] = None) -> bool:
    """Whether a session allowed on the meeting recently has not started yet."""
    allowed = lma_sessions.session_number(meeting.get("SessionNumber"))
    started = lma_sessions.session_number(meeting.get("SessionStartedNumber"))
    if allowed <= started:
        return False
    try:
        reopened_at = datetime.fromisoformat(str(meeting.get("ReopenedAt")).replace("Z", "+00:00"))
    except ValueError:
        return False
    if reopened_at.tzinfo is None:
        reopened_at = reopened_at.replace(tzinfo=timezone.utc)
    elapsed = ((now or datetime.now(timezone.utc)) - reopened_at).total_seconds()
    return 0 <= elapsed < PENDING_SESSION_SECONDS


#: The longest CallId accepted; LMA's own are a meeting name and a timestamp.
MAX_CALL_ID_LENGTH = 1024

_TABLE: Optional[Any] = None


def events_table() -> Any:
    global _TABLE  # pylint: disable=global-statement
    if _TABLE is None:
        _TABLE = boto3.resource("dynamodb").Table(os.environ["EVENT_SOURCING_TABLE"])
    return _TABLE


def refusal(error_type: str, message: str) -> Dict[str, str]:
    return {"errorType": error_type, "message": message}


def caller_identity(event: Mapping[str, Any]) -> Dict[str, Any]:
    """The signed-in caller, or an empty identity for anything else.

    The field is for Cognito users only (the schema says so); a request without
    a user-pool username is treated as a caller who owns nothing.
    """
    identity = event.get("identity") or {}
    if not identity.get("username") or not (identity.get("claims") or identity.get("sub")):
        return {}
    return dict(identity)


def reopen_meeting(identity: Mapping[str, Any], call_id: Any) -> Dict[str, Any]:
    """Allow the next capture session on the meeting; answers with its number and offset."""
    if not isinstance(call_id, str) or not call_id.strip():
        return refusal("BadRequest", "A meeting ID is required.")
    call_id = call_id.strip()
    if len(call_id) > MAX_CALL_ID_LENGTH:
        return refusal("BadRequest", "That is not a meeting ID.")

    table = events_table()
    meeting = lma_sessions.read_meeting(table, call_id)
    if not meeting:
        LOGGER.info("reopenMeeting: no meeting with the CallId given")
        return refusal(
            "MeetingNotFound",
            "There is no meeting with this ID. Copy the meeting ID from the meeting's page in LMA.",
        )
    if not lma_sessions.caller_owns_meeting(identity, meeting):
        LOGGER.info("reopenMeeting: refused to a caller who is not the owner or an Admin")
        return refusal(
            "Unauthorized",
            "Only the meeting's owner or an Admin can continue this meeting.",
        )
    if meeting.get("TranscriptDeletedAt"):
        return refusal(
            "TranscriptDeleted",
            "This meeting's transcript has been deleted, so the meeting cannot be continued.",
        )
    if meeting.get("Status") in LIVE_STATUSES:
        return refusal(
            "MeetingInProgress",
            "This meeting is still being captured. Continue it once it shows as Done in LMA.",
        )
    if pending_session(meeting):
        return refusal(
            "MeetingInProgress",
            "This meeting was reopened a few minutes ago and that capture has not started yet. "
            "Try again once it has started and ended, or in a few minutes.",
        )

    session = lma_sessions.next_session_number(
        meeting.get("SessionNumber"), meeting.get("SessionStartedNumber")
    )
    offset = lma_sessions.next_session_offset(
        lma_sessions.transcript_end_times(table, call_id), meeting
    )
    reopened_at = lma_sessions.now_iso()
    try:
        lma_sessions.allow_session(
            table, call_id, session, offset, lma_sessions.who(identity), now=reopened_at
        )
    except lma_sessions.SessionNotAllowed:
        return refusal(
            "MeetingChanged",
            "The meeting changed while it was being reopened. Try again.",
        )
    LOGGER.info(
        "reopenMeeting: allowed capture session %s, at %ss on the meeting's timeline",
        session,
        offset,
    )
    return {
        "CallId": call_id,
        "SessionNumber": session,
        "SessionOffsetSeconds": offset,
        "ReopenedAt": reopened_at,
    }


def handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """AppSync Lambda resolver entry point (reopenMeeting.request.vtl payload)."""
    field = (event.get("info") or {}).get("fieldName")
    if field != "reopenMeeting":
        return refusal("BadRequest", f"unsupported field {field}")
    data = (event.get("arguments") or {}).get("input") or {}
    return reopen_meeting(caller_identity(event), data.get("CallId"))
