# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The addChatAttachment input built from an ADD_CHAT_ATTACHMENT event (no AWS)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from attachment_input import (  # noqa: E402
    ALLOWED_CONTENT_TYPES,
    AttachmentEventError,
    build_attachment_input,
)

REPO = HERE.parents[3]
VP_ATTACHMENTS = REPO / "lma-virtual-participant-stack" / "backend" / "src" / "chat-attachments.ts"

EVENT = {
    "EventType": "ADD_CHAT_ATTACHMENT",
    "CallId": "Weekly sync-2026-11-19-10-00-00-000",
    "AttachmentId": "0000012500-ab12cd34",
    "SegmentId": "chat-zoom-m1",
    "Timestamp": 12.5,
    "Kind": "IMAGE",
    "FileName": "whiteboard.png",
    "Platform": "zoom",
    "Status": "STORED",
    "ContentType": "image/png",
    "Size": 1234,
    "S3Key": "lma-chat-attachments/Weekly_sync_2026_11_19_10_00_00_000/0000012500-ab12cd34/whiteboard.png",
    "Summarize": True,
    "CreatedAt": "2026-11-19T10:00:12.500Z",
    "AccessToken": "token",
    "Owner": "someone-else@example.com",
}


def build(**changes):
    message = {**EVENT, **changes}
    return build_attachment_input(message, "owner@example.com", "[a@example.com]", 1_900_000_000)


def test_a_stored_attachment_takes_the_meetings_access_and_retention():
    item = build()
    assert item["Owner"] == "owner@example.com", "the meeting's owner, not the event's"
    assert item["SharedWith"] == "[a@example.com]"
    assert item["ExpiresAfter"] == 1_900_000_000
    assert item["S3Key"] == EVENT["S3Key"]
    assert item["Summarize"] is True
    assert "AccessToken" not in item and "EventType" not in item


def test_a_stored_attachment_key_must_be_in_the_meetings_folder():
    for key in (
        "lma-chat-attachments/Another_meeting/0000012500-ab12cd34/whiteboard.png",
        "lma-chat-attachments/Weekly_sync_2026_11_19_10_00_00_000/../x/whiteboard.png",
        "lma-audio-recordings/Weekly_sync_2026_11_19_10_00_00_000/x.wav",
        None,
    ):
        with pytest.raises(AttachmentEventError):
            build(S3Key=key)


def test_a_stored_attachment_must_be_of_an_allowed_type():
    for content_type in ("image/svg+xml", "text/html", "application/octet-stream", None):
        with pytest.raises(AttachmentEventError):
            build(ContentType=content_type)


def test_an_attachment_not_stored_has_nothing_to_read_or_sign():
    item = build(
        Status="NOT_STORED",
        NotStoredReason="NOT_DOWNLOADABLE",
        Kind="DOCUMENT",
        FileName="Plan.docx",
    )
    assert "S3Key" not in item and "ContentType" not in item
    assert item["Summarize"] is False
    assert item["NotStoredReason"] == "NOT_DOWNLOADABLE"


@pytest.mark.parametrize(
    "changes",
    [
        {"CallId": ""},
        {"AttachmentId": "a/b"},
        {"AttachmentId": "x" * 65},
        {"SegmentId": ""},
        {"Kind": "VIDEO"},
        {"Status": "MAYBE"},
        {"Timestamp": -1},
        {"Timestamp": True},
        {"FileName": "  "},
        {"Summarize": "yes"},
        {"Status": "NOT_STORED", "NotStoredReason": "BECAUSE"},
    ],
)
def test_an_event_that_cannot_be_stored_is_refused(changes):
    with pytest.raises(AttachmentEventError):
        build(**changes)


def test_a_long_file_name_is_cut_to_the_limit():
    assert len(build(FileName="x" * 1000)["FileName"]) == 255


def test_the_allowed_types_are_the_virtual_participants():
    """The VP stores only these; the processor accepts only these. One list, in two languages."""
    text = VP_ATTACHMENTS.read_text(encoding="utf-8")
    declared = set(re.findall(r"'((?:image|application|text)/[A-Za-z0-9.+-]+)'", text))
    assert declared == set(ALLOWED_CONTENT_TYPES)
