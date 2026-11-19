# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The addChatAttachment input for an ADD_CHAT_ATTACHMENT event.

A chat attachment is an image or file shared in the meeting chat, sent by the
Virtual Participant (chat-attachments.ts) after it has stored the content, or
with its name only when it could not; see docs/virtual-participant.md, "Chat
attachments".

Kept free of AWS imports, like pure.py, so the validation can be tested without
the Lambda layer on the path.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Mapping, Optional

#: The folder the Virtual Participant stores attachments under, one folder per
#: meeting, as meeting and transcript deletion find them.
ATTACHMENTS_PREFIX = "lma-chat-attachments/"

KINDS = frozenset({"IMAGE", "DOCUMENT"})
STATUSES = frozenset({"STORED", "NOT_STORED"})
NOT_STORED_REASONS = frozenset({"NOT_DOWNLOADABLE", "TOO_LARGE", "TYPE_NOT_ALLOWED", "UNAVAILABLE"})

#: The types the Virtual Participant stores (ALLOWED_CONTENT_TYPES in
#: chat-attachments.ts; test_attachment_input.py keeps the two the same).
ALLOWED_CONTENT_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "text/plain",
        "text/csv",
        "text/markdown",
    }
)

# Zero-padded milliseconds plus a suffix (attachmentIdFor in
# chat-attachments.ts); it becomes part of the item's sort key.
ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_SEGMENT_ID_LENGTH = 256
MAX_FILE_NAME_LENGTH = 255
MAX_PLATFORM_LENGTH = 32


class AttachmentEventError(TypeError):
    """An ADD_CHAT_ATTACHMENT event that cannot be stored."""


def posixify_filename(filename: str) -> str:
    """The folder name producers use for a meeting; the same rule as meeting deletion."""
    name = re.sub(r"[^a-zA-Z0-9_.]", "_", filename)
    name = re.sub(r"^_+", "", name)
    return re.sub(r"_+$", "", name)


def key_in_folder(key: Any, folder: str) -> bool:
    """True when ``key`` names an object inside ``folder``, a prefix ending in "/".

    The rule is applied to the key's "/"-separated segments: every segment must
    be non-empty and neither "." nor "..". A meeting's folder name is its CallId
    made safe as a file name, which keeps dots, so a run of dots inside a
    segment is part of a name and is accepted. The same helper is copied into
    each function that checks such a key; lma-ai-stack/test/
    test_key_in_folder_copies_agree.py keeps the copies identical.
    """
    if not isinstance(key, str) or not folder.endswith("/") or not key.startswith(folder):
        return False
    return all(segment not in ("", ".", "..") for segment in key.split("/"))


def attachment_folder(call_id: str) -> str:
    """The meeting's folder of attachments in the recordings bucket."""
    return f"{ATTACHMENTS_PREFIX}{posixify_filename(call_id)}/"


def _number(message: Mapping[str, Any], name: str, *, required: bool) -> Optional[float]:
    value = message.get(name)
    if value is None:
        if required:
            raise AttachmentEventError(f"ADD_CHAT_ATTACHMENT is missing {name}")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AttachmentEventError(f"ADD_CHAT_ATTACHMENT {name} is not a number")
    if value < 0:
        raise AttachmentEventError(f"ADD_CHAT_ATTACHMENT {name} is negative")
    return float(value)


def _text(message: Mapping[str, Any], name: str, limit: int, *, required: bool) -> Optional[str]:
    value = message.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AttachmentEventError(f"ADD_CHAT_ATTACHMENT {name} is missing or not text")
    return value.strip()[:limit]


def build_attachment_input(
    message: Mapping[str, Any],
    owner: Optional[str],
    shared_with: Any,
    expires_after: int,
) -> Dict[str, Any]:
    """The addChatAttachment input, or AttachmentEventError for an event that cannot be stored.

    Owner and SharedWith come from the meeting rather than from the event, so an
    attachment is visible to exactly the people who can see the meeting's
    transcript. ExpiresAfter is the transcript's retention, for the same reason.
    A stored attachment's key must be inside the meeting's own folder, the one
    meeting deletion removes.
    """
    call_id = message.get("CallId")
    if not isinstance(call_id, str) or not call_id:
        raise AttachmentEventError("ADD_CHAT_ATTACHMENT is missing CallId")
    attachment_id = message.get("AttachmentId")
    if not isinstance(attachment_id, str) or not ATTACHMENT_ID.match(attachment_id):
        raise AttachmentEventError("ADD_CHAT_ATTACHMENT AttachmentId is missing or not an id")
    segment_id = _text(message, "SegmentId", MAX_SEGMENT_ID_LENGTH, required=True)
    kind = message.get("Kind")
    if kind not in KINDS:
        raise AttachmentEventError("ADD_CHAT_ATTACHMENT Kind is not recognised")
    status = message.get("Status")
    if status not in STATUSES:
        raise AttachmentEventError("ADD_CHAT_ATTACHMENT Status is not recognised")

    item: Dict[str, Any] = {
        "CallId": call_id,
        "AttachmentId": attachment_id,
        "SegmentId": segment_id,
        "Timestamp": _number(message, "Timestamp", required=True),
        "Kind": kind,
        "FileName": _text(message, "FileName", MAX_FILE_NAME_LENGTH, required=True),
        "Platform": _text(message, "Platform", MAX_PLATFORM_LENGTH, required=False),
        "Status": status,
        "Size": _number(message, "Size", required=False),
        "ExpiresAfter": expires_after,
        "Owner": owner,
        "SharedWith": shared_with,
    }
    if status == "STORED":
        content_type = message.get("ContentType")
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise AttachmentEventError("ADD_CHAT_ATTACHMENT ContentType is not an allowed type")
        key = message.get("S3Key")
        if not key_in_folder(key, attachment_folder(call_id)):
            raise AttachmentEventError("ADD_CHAT_ATTACHMENT S3Key is not in the meeting's folder")
        summarize = message.get("Summarize")
        if summarize is not None and not isinstance(summarize, bool):
            raise AttachmentEventError("ADD_CHAT_ATTACHMENT Summarize is not a boolean")
        item.update({"ContentType": content_type, "S3Key": key, "Summarize": bool(summarize)})
    else:
        reason = message.get("NotStoredReason")
        if reason is not None and reason not in NOT_STORED_REASONS:
            raise AttachmentEventError("ADD_CHAT_ATTACHMENT NotStoredReason is not recognised")
        # Nothing was stored, so there is nothing to read or to sign.
        item.update({"NotStoredReason": reason, "Summarize": False})
    created_at = message.get("CreatedAt")
    if isinstance(created_at, str) and created_at:
        item["CreatedAt"] = created_at
    # Unset optional fields are left out rather than written as nulls.
    return {k: v for k, v in item.items() if v is not None}
