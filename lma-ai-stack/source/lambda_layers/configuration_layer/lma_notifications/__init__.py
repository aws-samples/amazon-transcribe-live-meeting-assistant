# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Publish a notification on a meeting, from a backend Lambda.

A meeting notification is a short item for the people who can see a meeting
in LMA -- its owner and the users it is shared with, and Admins -- shown live
on the meeting page and read back with the listMeetingNotifications query or
the MCP tool ``list_meeting_notifications``. It is never posted to the
meeting's chat or spoken. See docs/meeting-notifications.md.

``publish_meeting_notification`` sends the IAM-only ``publishMeetingNotification``
mutation, signed with the calling function's role; no user token is involved.
The function's role needs ``appsync:GraphQL`` on that field, and the function
needs ``APPSYNC_GRAPHQL_URL``. A function that publishes category ``guidance``
also needs ``CONFIGURATION_TABLE_NAME``, read access to the configuration
table, and ``TRANSCRIPTION_RECORD_EXPIRATION_IN_DAYS``, from which
``transcript_expires_after`` reads the transcript's retention; without them
the shipped retention is used. The resolver decides who receives it, from the
meeting: a publisher never names recipients. A publisher acting for a user
(the MCP server does) checks that user may see the meeting before calling.

A second publish with the same ``category`` and ``dedup_key`` updates the
notification the first one made rather than adding another. A meeting takes at
most 120 publishes a minute; beyond that ``NotificationRateLimited`` is raised.

Like the rest of this layer it uses only the standard library and the botocore
that ships with the Lambda runtime.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

LOGGER = logging.getLogger(__name__)

# The category of guidance for the people running a meeting. Guidance may
# quote the transcript, so it follows the transcript's retention and is deleted
# with it (TRANSCRIPT_BOUND_CATEGORIES).
GUIDANCE = "guidance"
TRANSCRIPT_BOUND_CATEGORIES = frozenset({GUIDANCE})

SEVERITIES = ("info", "suggestion", "warning", "urgent")
ACTION_KINDS = ("mutation", "copy")

# The resolver's limits (notificationPublishMeeting.js), checked here first so
# a publisher gets a clear error without a request.
MAX_CATEGORY = 32
MAX_TYPE = 64
MAX_TITLE = 200
MAX_BODY = 2000
MAX_REFS = 20
MAX_REF = 200
MAX_DEDUP_KEY = 200
MAX_SOURCE = 64
MAX_ACTIONS = 2
MAX_LABEL = 40
MAX_COPY_TEXT = 2000
RATE_PER_MINUTE = 120

_LOWER = "abcdefghijklmnopqrstuvwxyz"
_CATEGORY_CHARS = frozenset(_LOWER + "0123456789_-")
_NAME_CHARS = frozenset(_LOWER + _LOWER.upper() + "0123456789_.:/-")

PUBLISH_MEETING_NOTIFICATION = """
mutation PublishMeetingNotification($input: PublishMeetingNotificationInput!) {
  publishMeetingNotification(input: $input) {
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
    Created
    Owner
    SharedWith
  }
}
"""

GraphQL = Callable[[str, Mapping[str, Any]], Dict[str, Any]]


class NotificationError(ValueError):
    """A publish the API refused; ``error_type`` is its GraphQL errorType."""

    def __init__(self, message: str, error_type: Optional[str] = None):
        super().__init__(message)
        self.error_type = error_type


class NotificationRateLimited(NotificationError):
    """The meeting has had RATE_PER_MINUTE publishes this minute."""


class NotificationMeetingNotFound(NotificationError):
    """There is no such meeting."""


class NotificationTranscriptDeleted(NotificationError):
    """The meeting's transcript is deleted, so a transcript-bound category is not kept."""


def _text(value: Any, field: str, limit: int, required: bool) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{field} is longer than {limit} characters")
    return value


def _name(value: Any, field: str, limit: int, allowed: frozenset) -> str:
    text = _text(value, field, limit, required=True) or ""
    if not set(text) <= allowed:
        raise ValueError(f"{field} may hold only letters, digits and _ . : / -")
    return text


def _category(value: Any) -> str:
    category = _name(value, "category", MAX_CATEGORY, _CATEGORY_CHARS)
    if category[0] not in _LOWER:
        raise ValueError("category must start with a lower-case letter")
    return category


def _severity(value: Any) -> str:
    severity = str(value or "info").strip().lower()
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
    return severity.upper()


def _refs(values: Optional[Iterable[Any]]) -> Optional[List[str]]:
    if values is None:
        return None
    refs = list(values)
    if len(refs) > MAX_REFS:
        raise ValueError(f"at most {MAX_REFS} refs")
    for ref in refs:
        if not isinstance(ref, str) or not ref or len(ref) > MAX_REF:
            raise ValueError(f"each ref is a string of 1 to {MAX_REF} characters")
    return refs or None


def _iso(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if isinstance(value, str):
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("expires_at must be an ISO 8601 time") from error
        return value
    raise ValueError("expires_at must be a datetime or an ISO 8601 string")


def _action(action: Mapping[str, Any]) -> Dict[str, str]:
    if not isinstance(action, Mapping):
        raise ValueError("each action is a mapping of label, kind and payload")
    label = _text(action.get("label"), "action label", MAX_LABEL, required=True)
    kind = str(action.get("kind") or "").strip().lower()
    if kind not in ACTION_KINDS:
        raise ValueError(f"action kind must be one of {', '.join(ACTION_KINDS)}")
    payload = action.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as error:
            raise ValueError("action payload must be a JSON object") from error
    if not isinstance(payload, Mapping):
        raise ValueError("action payload must be a JSON object")
    if kind == "copy":
        if set(payload) != {"text"}:
            raise ValueError("a copy action payload holds text only")
        _text(payload.get("text"), "copy action text", MAX_COPY_TEXT, required=True)
    else:
        if not isinstance(payload.get("action"), str) or set(payload) - {"action", "args"}:
            raise ValueError("a mutation action payload holds action and args only")
    return {
        "Label": label or "",
        "Kind": kind.upper(),
        "Payload": json.dumps(payload, separators=(",", ":")),
    }


def _actions(values: Optional[Iterable[Mapping[str, Any]]]) -> Optional[List[Dict[str, str]]]:
    if values is None:
        return None
    actions = [_action(action) for action in values]
    if len(actions) > MAX_ACTIONS:
        raise ValueError(f"at most {MAX_ACTIONS} actions")
    return actions or None


def transcript_expires_after(now: Optional[float] = None) -> Optional[int]:
    """The expiry a record that quotes the transcript gets if written now.

    The transcript's retention (Retention › transcriptionExpirationInDays, at
    most the TranscriptionExpirationInDays parameter), shortened as a
    transcript segment's is while transcripts are deleted after the summary.
    Where the function has no configuration table or no
    TRANSCRIPTION_RECORD_EXPIRATION_IN_DAYS, read_setting answers with the
    shipped default. None only when the read fails or the retention is not
    above zero; the resolver then gives the row the meeting's expiry, and the
    transcript deletion still removes it.
    """
    # pylint: disable=import-outside-toplevel
    try:
        from lma_config import SEGMENT_EXPIRY_LIMIT_DAYS, deletion_delay_minutes, read_setting

        days = read_setting(
            "retention",
            "transcriptionExpirationInDays",
            env_var="TRANSCRIPTION_RECORD_EXPIRATION_IN_DAYS",
        )
        if days is None or float(days) <= 0:
            return None
        days = float(days)
        if deletion_delay_minutes() is not None:
            days = min(days, SEGMENT_EXPIRY_LIMIT_DAYS)
    # Any failure leaves the meeting's expiry, which still applies.
    except Exception:  # pylint: disable=broad-except
        LOGGER.warning("could not read the transcript retention", exc_info=True)
        return None
    return int((time.time() if now is None else now) + days * 86400)


def build_input(  # pylint: disable=too-many-arguments,too-many-locals,redefined-builtin
    call_id: str,
    *,
    category: str,
    type: str,
    title: Optional[str],
    source: str,
    body: Optional[str] = None,
    severity: str = "info",
    refs: Optional[Iterable[str]] = None,
    dedup_key: Optional[str] = None,
    expires_at: Any = None,
    actions: Optional[Iterable[Mapping[str, Any]]] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """The publishMeetingNotification input, or ValueError for one the API would refuse."""
    meeting = _text(call_id, "call_id", 256, required=True)
    item: Dict[str, Any] = {
        "CallId": meeting,
        "Category": _category(category),
        "Type": _name(type, "type", MAX_TYPE, _NAME_CHARS),
        "Severity": _severity(severity),
        "Source": _text(source, "source", MAX_SOURCE, required=True),
    }
    optional = {
        "Title": _text(title, "title", MAX_TITLE, required=False),
        "Body": _text(body, "body", MAX_BODY, required=False),
        "Refs": _refs(refs),
        "DedupKey": (
            _name(dedup_key, "dedup_key", MAX_DEDUP_KEY, _NAME_CHARS)
            if dedup_key not in (None, "")
            else None
        ),
        "Actions": _actions(actions),
        "CreatedBy": _text(created_by, "created_by", 256, required=False),
        "ExpiresAt": _iso(expires_at),
    }
    item.update({key: value for key, value in optional.items() if value is not None})
    if item["Category"] in TRANSCRIPT_BOUND_CATEGORIES:
        expires_after = transcript_expires_after()
        if expires_after is not None:
            item["ExpiresAfter"] = expires_after
    return item


def appsync_graphql(query: str, variables: Mapping[str, Any]) -> Dict[str, Any]:
    """One request to LMA's AppSync API, signed with this function's IAM role.

    Returns the response's ``data``; raises NotificationError for its first error.
    """
    # pylint: disable=import-outside-toplevel
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    url = os.environ.get("APPSYNC_GRAPHQL_URL", "")
    if not url.startswith("https://"):
        raise NotificationError("APPSYNC_GRAPHQL_URL is not set to the AppSync endpoint")
    session = boto3.Session()
    credentials = session.get_credentials()
    if credentials is None:
        raise NotificationError("No AWS credentials to sign the AppSync request with")
    region = session.region_name or os.environ.get("AWS_REGION", "")
    body = json.dumps({"query": query, "variables": dict(variables)})
    signed = AWSRequest(
        method="POST", url=url, data=body, headers={"Content-Type": "application/json"}
    )
    SigV4Auth(credentials, "appsync", region).add_auth(signed)
    request = urllib.request.Request(  # noqa: S310 -- an https URL checked above
        url, data=body.encode("utf-8"), headers=dict(signed.headers), method="POST"
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
        result = json.loads(response.read().decode("utf-8"))
    errors = result.get("errors") or []
    if errors:
        first = errors[0]
        error_type = first.get("errorType")
        message = first.get("message") or "The request failed"
        if error_type == "RateLimited":
            raise NotificationRateLimited(message, error_type)
        if error_type == "NotFound":
            raise NotificationMeetingNotFound(message, error_type)
        if error_type == "TranscriptDeleted":
            raise NotificationTranscriptDeleted(message, error_type)
        raise NotificationError(message, error_type)
    return result.get("data") or {}


def publish_meeting_notification(  # pylint: disable=too-many-arguments,too-many-locals,redefined-builtin
    call_id: str,
    *,
    category: str,
    type: str,
    title: Optional[str],
    source: str,
    body: Optional[str] = None,
    severity: str = "info",
    refs: Optional[Iterable[str]] = None,
    dedup_key: Optional[str] = None,
    expires_at: Any = None,
    actions: Optional[Iterable[Mapping[str, Any]]] = None,
    created_by: Optional[str] = None,
    graphql: Optional[GraphQL] = None,
) -> Dict[str, Any]:
    """Store a notification on a meeting and announce it to the people who can see it.

    Args:
        call_id: the meeting's CallId.
        category: ``guidance``, or another lower-case name of up to 32 characters.
        type: a name within the category, such as ``ask_question``.
        title: up to 200 characters, or None for the category's own heading.
        source: who is publishing, such as ``mcp``.
        body: Markdown, up to 2000 characters.
        severity: ``info``, ``suggestion``, ``warning`` or ``urgent``.
        refs: up to 20 opaque strings, stored as given.
        dedup_key: a later publish with the same category and key updates this one.
        expires_at: when the page stops showing it (a datetime or ISO 8601 string).
        actions: up to two ``{"label", "kind": "copy" | "mutation", "payload"}``.
        created_by: the user it is published on behalf of, if any.
        graphql: for tests, a stand-in for the signed AppSync request.

    Returns:
        The stored notification as the mutation answers it, with ``Created``
        False when the publish updated an earlier one.
    """
    item = build_input(
        call_id,
        category=category,
        type=type,
        title=title,
        source=source,
        body=body,
        severity=severity,
        refs=refs,
        dedup_key=dedup_key,
        expires_at=expires_at,
        actions=actions,
        created_by=created_by,
    )
    send = graphql or appsync_graphql
    data = send(PUBLISH_MEETING_NOTIFICATION, {"input": item})
    stored = data.get("publishMeetingNotification") or {}
    LOGGER.info(
        "published a meeting notification: callId=%s category=%s type=%s created=%s",
        item["CallId"],
        item["Category"],
        item["Type"],
        stored.get("Created"),
    )
    return stored
