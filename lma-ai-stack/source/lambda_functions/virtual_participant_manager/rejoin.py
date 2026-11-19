# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Rejoin: relaunch a Virtual Participant into the meeting it left.

The rejoined Virtual Participant joins the same online meeting and appends to
the same LMA meeting: the CallId already on its row. The meeting is captured in
*sessions*. The first is the one the Virtual Participant started; each rejoin
allows one more, numbered from 2, and the meeting record and the Virtual
Participant's row both carry the number. See docs/virtual-participant.md#rejoin.

What a rejoin does, in order:

1. Checks the caller: the Virtual Participant's owner or an Admin (the rule the
   end and delete operations hold), and the meeting's owner or an Admin.
2. Checks the Virtual Participant has left (ENDED, COMPLETED or FAILED) and has a
   meeting, and that the meeting's transcript has not been deleted.
3. Works out the new session's number and where it starts on the transcript
   timeline: after everything already on it, rounded up to a whole second, plus
   one second (next_session_offset).
   The container adds that offset to every segment it sends, so the new
   session's segments follow the earlier ones instead of starting again at 0.
4. Claims the row with a conditional write, which resets it to INITIALIZING and
   clears what the earlier run left on it (its end reason above all: the
   updateVirtualParticipant resolver refuses a progress status on a row that
   has one). A second rejoin of the same row fails the condition.
5. Allows the session on the meeting record. The meeting itself stays ENDED
   until the session's START event arrives; the call event processor then makes
   it live again (startCallSession) and only then cancels a pending transcript
   deletion. A rejoin whose session never starts -- the launch fails, or the
   Virtual Participant is not admitted -- leaves the meeting, and its deletion
   schedule, as they were.
6. Starts the launcher state machine, as the Create button does, with a launch
   token unique to the session: a launch is idempotent on its token, and the
   first launch used the Virtual Participant's id.

Before the claim, a rejoin is refused while a host the task registry holds for
the Virtual Participant may still be running (a failed run waiting to be
acknowledged, or one still shutting down). Stopping it under a row already
claimed would let its shutdown record its own end on the new run.

When a step after the claim fails, the row is marked FAILED with the reason, so
it does not wait at INITIALIZING for a launch that is not coming, and the user
can try again.
"""

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional

import boto3
import lma_sessions
from access import NotAuthorizedError, caller_may_change, caller_owns_meeting
from botocore.config import Config as BotoCoreConfig
from botocore.exceptions import ClientError

# The session rules shared with the meeting reopen resolver (lma_sessions, in
# the configuration layer).
from lma_sessions import (  # noqa: F401 - re-exported for this module's callers
    SESSION_GAP_SECONDS,
    meeting_key,
    next_session_offset,
    session_number,
    who,
)

logger = logging.getLogger(__name__)

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})

#: The statuses a Virtual Participant has left its meeting in. CANCELLED is a
#: scheduled one that never started, so it has no meeting to rejoin.
REJOINABLE_STATUSES = ("ENDED", "COMPLETED", "FAILED")

#: What the claim removes from the row: everything that describes how the
#: earlier run ended, and its host's connection details, which the new host
#: writes afresh.
CLEARED_ON_REJOIN = (
    "endReason",
    "endedAt",
    "endTrigger",
    "errorMessage",
    "userAcknowledgedFailure",
    "manualActionType",
    "manualActionMessage",
    "manualActionTimeoutSeconds",
    "manualActionStartTime",
    "statusBeforeManualAction",
    "vncEndpoint",
    "taskPrivateIp",
    "joinMethod",
    "ecsTerminated",
    "kinesisEndSent",
    "albCleanedUp",
)

_clients: Dict[str, Any] = {}


class RejoinRefused(Exception):
    """The Virtual Participant or its meeting cannot be rejoined; the message says why."""


def _client(name: str) -> Any:
    if name not in _clients:
        _clients[name] = boto3.client(name, config=CLIENT_CONFIG)
    return _clients[name]


def _events_table() -> Any:
    if "events" not in _clients:
        _clients["events"] = boto3.resource("dynamodb").Table(os.environ["EVENT_SOURCING_TABLE"])
    return _clients["events"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def next_session_number(vp: Dict[str, Any], meeting: Dict[str, Any]) -> int:
    """One more than any session the row or the meeting record has seen."""
    return lma_sessions.next_session_number(
        vp.get("sessionNumber"),
        meeting.get("SessionNumber"),
        meeting.get("SessionStartedNumber"),
    )


def read_meeting(call_id: str) -> Optional[Dict[str, Any]]:
    return lma_sessions.read_meeting(_events_table(), call_id)


def transcript_end_times(call_id: str) -> Iterable[Any]:
    """The EndTime of every item in the meeting's transcript, a page at a time."""
    return lma_sessions.transcript_end_times(_events_table(), call_id)


def claim(
    table: Any,
    vp: Dict[str, Any],
    session: int,
    offset: int,
    rejoined_by: str,
) -> Dict[str, Any]:
    """Reset the row for the new session, if it is still as it was read."""
    now = _now_iso()
    names = {"#status": "status", "#callId": "CallId"}
    removed = []
    for index, attribute in enumerate(CLEARED_ON_REJOIN):
        names[f"#r{index}"] = attribute
        removed.append(f"#r{index}")
    try:
        response = table.update_item(
            Key={"id": vp["id"]},
            UpdateExpression=(
                "SET #status = :initializing, updatedAt = :now, sessionNumber = :session, "
                "sessionTimeOffset = :offset, rejoinedAt = :now, rejoinedBy = :by, "
                "vncReady = :false REMOVE " + ", ".join(removed)
            ),
            ConditionExpression="attribute_exists(id) AND #status = :seen AND #callId = :callId",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={
                ":initializing": "INITIALIZING",
                ":now": now,
                ":session": session,
                ":offset": Decimal(offset),
                ":by": rejoined_by,
                ":false": False,
                ":seen": vp.get("status"),
                ":callId": vp["CallId"],
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as error:
        if _error_code(error) == "ConditionalCheckFailedException":
            raise RejoinRefused(
                "The Virtual Participant changed while it was being rejoined; "
                "refresh the page and try again."
            ) from error
        raise
    return response["Attributes"]


def release(table: Any, vp_id: str, reason: str) -> None:
    """Mark a claimed row FAILED with the reason the rejoin did not go ahead."""
    try:
        table.update_item(
            Key={"id": vp_id},
            UpdateExpression="SET #status = :failed, errorMessage = :reason, updatedAt = :now",
            ConditionExpression="#status = :initializing",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":failed": "FAILED",
                ":reason": f"Could not rejoin the meeting: {reason}",
                ":now": _now_iso(),
                ":initializing": "INITIALIZING",
            },
        )
    except ClientError:
        logger.exception(f"VP {vp_id}: could not record why the rejoin did not go ahead")


def allow_session(call_id: str, session: int, offset: int, rejoined_by: str) -> None:
    """Record on the meeting the session its next START may open."""
    try:
        lma_sessions.allow_session(
            _events_table(), call_id, session, offset, rejoined_by, via=lma_sessions.VIA_REJOIN
        )
    except lma_sessions.SessionNotAllowed as error:
        raise RejoinRefused(
            "The meeting changed while it was being reopened; refresh the page and try again."
        ) from error


def launch_input(vp: Dict[str, Any], identity: Dict[str, Any], launch_token: str) -> Dict[str, Any]:
    """The launcher state machine's input, as the Create button builds it.

    It carries no meeting password: the Virtual Participant reads that from its
    row when it starts, as every launch does. The launching user's
    Cognito sub (the persistent browser profile, and the stored Zoom account) is
    the row's for a scheduled Virtual Participant, and otherwise the caller's
    when the caller is its owner; an Admin rejoining someone else's Virtual
    Participant launches it without a profile, as a guest.
    """
    owner_sub = vp.get("userSub") or (
        identity.get("sub") if identity.get("username") == vp.get("Owner") else ""
    )
    use_zoom_account = bool(vp.get("useStoredZoomAccount") or vp.get("userZoomSub"))
    zoom_sub = (vp.get("userZoomSub") or owner_sub or "") if use_zoom_account else ""
    return {
        "apiInfo": {"httpMethod": "POST"},
        "data": {
            "meetingPlatform": vp.get("meetingPlatform", ""),
            "meetingID": vp.get("meetingId", ""),
            "meetingName": vp.get("meetingName", ""),
            # Empty: launch now, not on a schedule.
            "meetingTime": "",
            "userName": vp.get("owner") or vp.get("Owner") or "",
            "virtualParticipantId": vp["id"],
            "userSub": owner_sub or "",
            "userZoomSub": zoom_sub,
            "enableVideoRecording": "false" if vp.get("enableVideoRecording") is False else "true",
            "accessToken": "",
            "idToken": "",
            "rereshToken": "",
            # RunMicrovm is idempotent on its client token, so every launch of
            # a row carries a token of its own (see start_launch).
            "launchToken": launch_token,
        },
    }


def start_launch(vp: Dict[str, Any], identity: Dict[str, Any], launch_token: str) -> None:
    """Start the launcher state machine; its own failures mark the row FAILED.

    Used by rejoin and by Join again (join_again.py). `launch_token` is the
    Virtual Participant's id for its first launch, the token the MicroVM
    launcher falls back to when a launch from the browser names none, and
    `<id>-s<session>` for a rejoined session.
    """
    arn = os.environ.get("VP_STATE_MACHINE_ARN")
    if not arn:
        raise RuntimeError("the Virtual Participant launcher is not configured")
    _client("stepfunctions").start_execution(
        stateMachineArn=arn, input=json.dumps(launch_input(vp, identity, launch_token))
    )


def without_password(row: Dict[str, Any]) -> Dict[str, Any]:
    answer = {key: value for key, value in row.items() if key != "meetingPassword"}
    return json.loads(json.dumps(answer, default=_plain))


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def rejoin_virtual_participant(manager: Any, vp_id: str, identity: Dict[str, Any]) -> Dict:
    """Relaunch the Virtual Participant into its meeting; answers with its row."""
    vp = manager.get_virtual_participant(vp_id)
    if not caller_may_change(identity, vp):
        # Told the same way whether or not the row exists.
        raise NotAuthorizedError(
            f"User {identity.get('username')} is not authorized to change "
            f"Virtual Participant {vp_id}"
        )
    if not vp:
        raise ValueError(f"Virtual Participant {vp_id} not found")

    call_id = vp.get("CallId")
    if not call_id:
        raise RejoinRefused(
            "This Virtual Participant never started a meeting, so there is no meeting to rejoin."
        )
    status = vp.get("status")
    if status not in REJOINABLE_STATUSES:
        raise RejoinRefused(
            f"The Virtual Participant is {status}; it can rejoin only after it has left the meeting."
        )

    meeting = read_meeting(call_id)
    if not meeting:
        # A Virtual Participant links its CallId before it joins, so one that
        # was never admitted has a CallId and no meeting record.
        raise RejoinRefused(
            "There is no LMA meeting to reopen: the Virtual Participant never started "
            "capturing it, or it has been deleted."
        )
    if not caller_owns_meeting(identity, meeting):
        raise NotAuthorizedError(
            f"User {identity.get('username')} may not reopen the meeting of "
            f"Virtual Participant {vp_id}"
        )
    if meeting.get("TranscriptDeletedAt"):
        raise RejoinRefused("The meeting's transcript has been deleted, so it cannot be reopened.")

    if manager.registered_host_running(vp_id):
        raise RejoinRefused(
            "The Virtual Participant's earlier run is still shutting down; try again shortly. "
            "If it shows a failure, acknowledge it first."
        )

    session = next_session_number(vp, meeting)
    offset = next_session_offset(transcript_end_times(call_id), meeting)
    rejoined_by = who(identity)
    logger.info(
        f"VP {vp_id}: rejoining meeting as capture session {session}, at {offset}s on its timeline"
    )

    claimed = claim(manager.table, vp, session, offset, rejoined_by)
    try:
        allow_session(call_id, session, offset, rejoined_by)
        # Distinct per session: the row's first launch used its id.
        start_launch(vp, identity, f"{vp_id}-s{session}")
    except RejoinRefused as refused:
        release(manager.table, vp_id, str(refused))
        raise
    except Exception as error:  # noqa: BLE001 - recorded on the row, then raised
        logger.exception(f"VP {vp_id}: rejoin did not go ahead")
        release(manager.table, vp_id, "the launch could not be started")
        raise RuntimeError("The Virtual Participant could not be relaunched.") from error
    return without_password(claimed)
