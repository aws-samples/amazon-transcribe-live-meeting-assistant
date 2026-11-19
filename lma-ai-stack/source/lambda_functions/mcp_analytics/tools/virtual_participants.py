# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tools: list_virtual_participants and end_virtual_participant.

A Virtual Participant is read on getCall's rule against its own row (its owner,
the people it is shared with, Admins), as listVirtualParticipants and
getVirtualParticipant answer a signed-in user, and changed by its owner or an
Admin, as the Virtual Participant Manager's ``caller_may_change`` decides.
Starting one is start_meeting_now and schedule_meeting.

Ending one is the endVirtualParticipant mutation the VP page's End button
sends: the Virtual Participant Manager stops its task, sends the meeting's END
event and, for a scheduled one, cancels its launch; the change is published to
the VP list through onUpdateVirtualParticipant.

The rows are read from the Virtual Participant table directly. The
listVirtualParticipants resolver filters on the signed-in user, which a request
signed with this function's IAM role is not.
"""

import os
from typing import Any, Dict, List, Optional, Tuple

import boto3
from boto3.dynamodb.conditions import Attr

from tools.access import (
    VP_FINISHED_STATUSES,
    Caller,
    can_manage,
    can_read,
    not_found_or_denied,
)
from tools.appsync_client import graphql
from tools.url_helper import get_meeting_url, get_virtual_participant_url

# Bounds one scan of the table, so a large table cannot hold the function.
MAX_SCAN_PAGES = 20
DEFAULT_LIMIT = 25
MAX_LIMIT = 100

END_VIRTUAL_PARTICIPANT = """
mutation EndVirtualParticipant($input: EndVirtualParticipantInput!) {
  endVirtualParticipant(input: $input) {
    id
    status
    CallId
    Owner
    SharedWith
  }
}
"""


def vp_table():
    """The Virtual Participant table."""
    return boto3.resource("dynamodb").Table(os.environ.get("VP_TABLE_NAME", ""))


def get_vp(vp_id: Any) -> Optional[Dict[str, Any]]:
    """The Virtual Participant's row, or None."""
    if not isinstance(vp_id, str) or not vp_id.strip():
        raise ValueError("virtual_participant_id is required")
    return vp_table().get_item(Key={"id": vp_id.strip()}).get("Item")


def _scan(filter_expression=None) -> Tuple[List[Dict[str, Any]], bool]:
    """The rows read, and whether the whole table was read (False at MAX_SCAN_PAGES)."""
    kwargs: Dict[str, Any] = {}
    if filter_expression is not None:
        kwargs["FilterExpression"] = filter_expression
    rows: List[Dict[str, Any]] = []
    for _ in range(MAX_SCAN_PAGES):
        response = vp_table().scan(**kwargs)
        rows.extend(response.get("Items", []))
        last = response.get("LastEvaluatedKey")
        if not last:
            return rows, True
        kwargs["ExclusiveStartKey"] = last
    return rows, False


def active_vps_of_meeting(call_id: str) -> Tuple[List[Dict[str, Any]], bool]:
    """The meeting's Virtual Participants that have not finished, and whether all were read."""
    rows, complete = _scan(Attr("CallId").eq(call_id))
    return [row for row in rows if row.get("status") not in VP_FINISHED_STATUSES], complete


def summary(vp: Dict[str, Any]) -> Dict[str, Any]:
    """A Virtual Participant as these tools return it; never its meeting password."""
    out = {
        "virtualParticipantId": vp.get("id"),
        "meetingName": vp.get("meetingName"),
        "meetingPlatform": vp.get("meetingPlatform"),
        "status": vp.get("status"),
        "isScheduled": bool(vp.get("isScheduled")),
        "scheduledFor": vp.get("scheduledFor"),
        "owner": vp.get("Owner"),
        "createdAt": vp.get("createdAt"),
        "updatedAt": vp.get("updatedAt"),
        "virtualParticipantUrl": get_virtual_participant_url(str(vp.get("id") or "")),
    }
    if vp.get("CallId"):
        out["meetingId"] = vp["CallId"]
        out["meetingUrl"] = get_meeting_url(vp["CallId"])
    return out


def list_vps(caller: Caller, status: Optional[str] = None, limit: Any = None) -> Dict[str, Any]:
    """The Virtual Participants the caller may see, newest first."""
    try:
        count = DEFAULT_LIMIT if limit is None else max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError) as error:
        raise ValueError("limit must be a whole number") from error
    scanned, complete = _scan()
    rows = [row for row in scanned if can_read(row, caller)]
    if status:
        rows = [row for row in rows if str(row.get("status", "")).upper() == status.upper()]
    rows.sort(key=lambda row: str(row.get("createdAt") or ""), reverse=True)
    answer: Dict[str, Any] = {
        "virtualParticipants": [summary(row) for row in rows[:count]],
        "count": len(rows[:count]),
        "truncated": not complete,
    }
    if not complete:
        answer["note"] = (
            "The Virtual Participant table is larger than one read covers, so this list may "
            "leave some out, and is ordered newest first among those read."
        )
    return answer


def end_vp(caller: Caller, vp: Dict[str, Any], reason: Optional[str] = None) -> Dict[str, Any]:
    """End (or, while it waits for its launch, cancel) a Virtual Participant."""
    if not can_manage(vp, caller):
        raise PermissionError("Only the Virtual Participant's owner or an Admin may end it")
    if vp.get("status") in VP_FINISHED_STATUSES:
        raise ValueError(
            f"Virtual Participant {vp.get('id')} has already finished ({vp['status']})"
        )
    scheduled = vp.get("status") == "SCHEDULED"
    end_reason = (reason or "").strip()[:200] or (
        "Cancelled from an MCP client" if scheduled else "Ended from an MCP client"
    )
    ended = (
        graphql(
            END_VIRTUAL_PARTICIPANT,
            {"input": {"id": vp["id"], "endReason": end_reason, "endedBy": caller.user_id}},
        ).get("endVirtualParticipant")
        or {}
    )
    return {**summary(vp), "status": ended.get("status") or "ENDED", "endReason": end_reason}


def end_by_id(caller: Caller, virtual_participant_id: Any, reason: Optional[str] = None):
    """end_virtual_participant: a VP named by its id."""
    vp = get_vp(virtual_participant_id)
    if vp is None or not can_read(vp, caller):
        raise not_found_or_denied("Virtual Participant", str(virtual_participant_id))
    return end_vp(caller, vp, reason)
