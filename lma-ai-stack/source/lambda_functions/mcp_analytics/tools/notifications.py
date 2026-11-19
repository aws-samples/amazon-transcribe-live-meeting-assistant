# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tool: list_meeting_notifications, and the read the guidance tools share.

A meeting's notifications are the items LMA showed on the meeting page to the
people who can see the meeting: guidance (category ``guidance``) and any other
category a feature publishes. This tool returns them as they are stored, in
the order they were first published, a page at a time, so an assistant can
learn after the meeting what it was told. It does not apply any user's
dismissals or mutes, which change only that user's view of the page.

The read is the listMeetingNotifications query, sent with this function's IAM
role after ``require_meeting`` has checked the MCP caller may see the meeting
now; the query applies the same rule for a signed-in user. See
docs/meeting-notifications.md.
"""

import json
from typing import Any, Dict, Optional

from tools.access import Caller, require_meeting
from tools.appsync_client import graphql

DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 500
MAX_CATEGORY = 32

LIST_MEETING_NOTIFICATIONS = """
query ListMeetingNotifications(
  $callId: ID!
  $category: String
  $since: AWSDateTime
  $limit: Int
  $nextToken: String
  $newestFirst: Boolean
) {
  listMeetingNotifications(
    CallId: $callId
    Category: $category
    Since: $since
    Limit: $limit
    NextToken: $nextToken
    NewestFirst: $newestFirst
  ) {
    Items {
      CallId
      NotificationId
      Category
      Type
      Severity
      Title
      Body
      Refs
      DedupKey
      Actions { Label Kind Payload }
      Source
      CreatedBy
      CreatedAt
      UpdatedAt
      Revision
      ExpiresAt
    }
    NextToken
  }
}
"""


def list_limit(value: Any, default: int = DEFAULT_LIST_LIMIT) -> int:
    """A page size between 1 and MAX_LIST_LIMIT."""
    if value is None:
        return default
    try:
        return max(1, min(int(value), MAX_LIST_LIMIT))
    except (TypeError, ValueError) as error:
        raise ValueError("limit must be a whole number") from error


def _optional_text(value: Any, field: str, limit: int) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value.strip()) > limit:
        raise ValueError(f"{field} must be a string of at most {limit} characters")
    return value.strip()


def read_page(
    call_id: str,
    *,
    category: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = DEFAULT_LIST_LIMIT,
    cursor: Optional[str] = None,
    newest_first: bool = False,
) -> Dict[str, Any]:
    """One page of the listMeetingNotifications query, for a meeting already checked."""
    variables: Dict[str, Any] = {"callId": call_id, "limit": limit, "newestFirst": newest_first}
    if category:
        variables["category"] = category
    if since:
        variables["since"] = since
    if cursor:
        variables["nextToken"] = cursor
    data = graphql(LIST_MEETING_NOTIFICATIONS, variables)
    return data.get("listMeetingNotifications") or {}


def _actions(values: Any) -> list:
    actions = []
    for action in values or []:
        try:
            payload = json.loads(action.get("Payload") or "{}")
        except ValueError:
            payload = None
        actions.append(
            {"label": action.get("Label"), "kind": action.get("Kind"), "payload": payload}
        )
    return actions


def as_tool_item(row: Dict[str, Any]) -> Dict[str, Any]:
    """A notification in the tool's camelCase form."""
    return {
        "notificationId": row.get("NotificationId"),
        "category": row.get("Category"),
        "type": row.get("Type"),
        "severity": (row.get("Severity") or "").lower() or None,
        "title": row.get("Title"),
        "body": row.get("Body"),
        "refs": row.get("Refs") or [],
        "dedupKey": row.get("DedupKey"),
        "actions": _actions(row.get("Actions")),
        "source": row.get("Source"),
        "createdBy": row.get("CreatedBy"),
        "createdAt": row.get("CreatedAt"),
        "updatedAt": row.get("UpdatedAt"),
        "revision": row.get("Revision"),
        "expiresAt": row.get("ExpiresAt"),
    }


def list_items(
    caller: Caller,
    meeting_id: str,
    category: Any = None,
    since: Any = None,
    limit: Any = None,
    cursor: Any = None,
) -> Dict[str, Any]:
    """The meeting's notifications, oldest first, with a cursor for the next page."""
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or meeting_id.strip()
    page = read_page(
        call_id,
        category=_optional_text(category, "category", MAX_CATEGORY),
        since=_optional_text(since, "since", 64),
        limit=list_limit(limit),
        cursor=_optional_text(cursor, "cursor", 4096),
    )
    items = [as_tool_item(row) for row in page.get("Items") or [] if row]
    return {
        "meetingId": call_id,
        "notifications": items,
        "count": len(items),
        "nextCursor": page.get("NextToken"),
    }
