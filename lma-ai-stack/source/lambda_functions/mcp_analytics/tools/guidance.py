# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tools: push_meeting_guidance and list_meeting_guidance.

Guidance is a short note an external agent leaves on a meeting for the people
following it in LMA: "the customer asked about pricing twice; the 2026 price
sheet is in the deal folder". It is stored under the meeting and announced to
the meeting page, which shows it as a dismissible notification to the
meeting's owner and the people it is shared with. It is never posted to the
meeting's own chat, so the meeting's other participants do not see it.

Each item is a row of the EventSourcing table under ``gd#<CallId>``, sorted by
when it was added (``g#<CreatedAt>#<GuidanceId>``), so anything that wants the
guidance a meeting has been given -- this tool's list, or an assistant reading
it as instructions -- queries that one partition. The row carries the meeting's
Owner, SharedWith and ExpiresAfter as they were when it was written, so it is
announced to the same people and expires with the meeting.

The write goes through the addMeetingGuidance mutation, which stores the row
and publishes it to the onAddMeetingGuidance subscription, filtered on Owner and
SharedWith like the meeting's other subscriptions (subscription.js).
"""

import uuid
from typing import Any, Dict, Optional

from boto3.dynamodb.conditions import Key

from tools.access import Caller, calls_table, require_meeting
from tools.appsync_client import graphql
from tools.get_virtual_participant_status import shared_with_entries

MAX_TEXT_CHARS = 2000
MAX_TITLE_CHARS = 200
LEVELS = ("INFO", "WARNING", "URGENT")
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
# Recorded on each item, so a reader can tell where guidance came from.
SOURCE = "mcp"

ADD_MEETING_GUIDANCE = """
mutation AddMeetingGuidance($input: AddMeetingGuidanceInput!) {
  addMeetingGuidance(input: $input) {
    CallId
    GuidanceId
    Title
    Text
    Level
    Source
    CreatedBy
    CreatedAt
    Owner
    SharedWith
  }
}
"""


def _text(value: Any, field: str, limit: int, required: bool) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{field} is longer than {limit} characters; shorten it")
    return value


def _level(value: Any) -> str:
    if value is None or value == "":
        return "INFO"
    level = str(value).strip().upper()
    if level not in LEVELS:
        raise ValueError("level must be one of info, warning or urgent")
    return level


def published_shared_with(value: Any) -> Optional[str]:
    """SharedWith as "[a, b]", the form every filtered subscription message carries."""
    entries = shared_with_entries(value)
    return f"[{', '.join(entries)}]" if entries else None


def push(
    caller: Caller,
    meeting_id: str,
    text: Any,
    title: Any = None,
    level: Any = None,
) -> Dict[str, Any]:
    """Store a guidance item on the meeting and announce it to the meeting page."""
    body = _text(text, "text", MAX_TEXT_CHARS, required=True)
    heading = _text(title, "title", MAX_TITLE_CHARS, required=False)
    severity = _level(level)
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or meeting_id.strip()

    item: Dict[str, Any] = {
        "CallId": call_id,
        "GuidanceId": str(uuid.uuid4()),
        "Text": body,
        "Level": severity,
        "Source": SOURCE,
        "CreatedBy": caller.user_id,
        "Owner": meeting.get("Owner"),
    }
    if heading:
        item["Title"] = heading
    shared_with = published_shared_with(meeting.get("SharedWith"))
    if shared_with:
        item["SharedWith"] = shared_with
    if meeting.get("ExpiresAfter") is not None:
        item["ExpiresAfter"] = int(meeting["ExpiresAfter"])

    stored = graphql(ADD_MEETING_GUIDANCE, {"input": item}).get("addMeetingGuidance") or {}
    return {
        "meetingId": call_id,
        "guidanceId": stored.get("GuidanceId", item["GuidanceId"]),
        "createdAt": stored.get("CreatedAt"),
        "level": severity,
        "title": heading,
        "text": body,
        "delivered": "Shown on the meeting page to its owner and the people it is shared with.",
    }


def _list_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_LIST_LIMIT
    try:
        return max(1, min(int(value), MAX_LIST_LIMIT))
    except (TypeError, ValueError) as error:
        raise ValueError("limit must be a whole number") from error


def list_items(caller: Caller, meeting_id: str, limit: Any = None) -> Dict[str, Any]:
    """The guidance a meeting has been given, newest first."""
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or meeting_id.strip()
    response = calls_table().query(
        KeyConditionExpression=Key("PK").eq(f"gd#{call_id}"),
        ScanIndexForward=False,
        Limit=_list_limit(limit),
    )
    items = [
        {
            "guidanceId": row.get("GuidanceId"),
            "createdAt": row.get("CreatedAt"),
            "createdBy": row.get("CreatedBy"),
            "source": row.get("Source"),
            "level": row.get("Level"),
            "title": row.get("Title"),
            "text": row.get("Text"),
        }
        for row in response.get("Items", [])
    ]
    return {"meetingId": call_id, "guidance": items, "count": len(items)}
