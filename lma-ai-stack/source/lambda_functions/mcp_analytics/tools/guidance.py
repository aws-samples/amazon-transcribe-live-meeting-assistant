# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tools: push_meeting_guidance and list_meeting_guidance.

Guidance is a short note an external agent leaves on a meeting for the people
following it in LMA: "the customer asked about pricing twice; the 2026 price
sheet is in the deal folder". It is a meeting notification of category
``guidance`` (docs/meeting-notifications.md): shown at once in the meeting
page's notifications panel to the meeting's owner and the people it is shared
with, and never posted to the meeting's own chat, so the meeting's other
participants do not see it. Guidance may quote the meeting, so it is deleted
with the meeting's transcript.

push publishes through lma_notifications.publish_meeting_notification, whose
resolver puts the meeting's Owner and SharedWith on the published message.
list reads the meeting's guidance, newest first, with the same query as
list_meeting_notifications, so it includes guidance from other publishers.
"""

from typing import Any, Dict, Optional

# pylint: disable-next=import-error
from lma_notifications import GUIDANCE, publish_meeting_notification

from tools.access import Caller, require_meeting
from tools.appsync_client import graphql
from tools.notifications import list_limit, read_items

MAX_TEXT_CHARS = 2000
MAX_TITLE_CHARS = 200
LEVELS = ("INFO", "WARNING", "URGENT")
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
# Recorded on each item, so a reader can tell where guidance came from.
SOURCE = "mcp"
# The type within the guidance category of a note pushed over MCP.
NOTE_TYPE = "note"


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

    stored = publish_meeting_notification(
        call_id,
        category=GUIDANCE,
        type=NOTE_TYPE,
        title=heading,
        body=body,
        severity=severity,
        source=SOURCE,
        created_by=caller.user_id,
        graphql=graphql,
    )
    return {
        "meetingId": call_id,
        "guidanceId": stored.get("NotificationId"),
        "createdAt": stored.get("CreatedAt"),
        "level": severity,
        "title": heading,
        "text": body,
        "delivered": "Shown on the meeting page to its owner and the people it is shared with.",
    }


def list_items(caller: Caller, meeting_id: str, limit: Any = None) -> Dict[str, Any]:
    """The guidance a meeting has been given, newest first."""
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or meeting_id.strip()
    page = read_items(
        call_id,
        category=GUIDANCE,
        limit=min(list_limit(limit, DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT),
        newest_first=True,
    )
    items = [
        {
            "guidanceId": row.get("NotificationId"),
            "createdAt": row.get("CreatedAt"),
            "createdBy": row.get("CreatedBy"),
            "source": row.get("Source"),
            "type": row.get("Type"),
            "level": row.get("Severity"),
            "title": row.get("Title"),
            "text": row.get("Body"),
        }
        for row in page.get("Items") or []
        if row
    ]
    # Set when more of the meeting's notifications were not read; the newest
    # guidance is first, so the items returned are always the most recent.
    return {
        "meetingId": call_id,
        "guidance": items,
        "count": len(items),
        "hasMore": bool(page.get("NextToken")),
    }
