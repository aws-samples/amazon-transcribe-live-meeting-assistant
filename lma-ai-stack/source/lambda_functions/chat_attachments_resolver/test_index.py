# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the chat attachments resolver (no AWS calls).

Asserted here: attachments are answered to exactly the callers ``getCall``
answers; a link is signed only for an object in the meeting's own attachments
folder; every download link asks S3 for ``Content-Disposition: attachment``;
only a raster image gets an inline preview link; a page token cannot be carried
to another meeting.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

import pytest

HERE = Path(__file__).resolve().parent
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["EVENT_SOURCING_TABLE"] = "calls"
os.environ["RECORDINGS_BUCKET"] = "rec-bucket"
# Signing needs credentials of some kind; nothing is sent anywhere.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "example")

_SPEC = importlib.util.spec_from_file_location("chat_attachments_resolver_index", HERE / "index.py")
index = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index)

CALL = "Weekly sync - 2026-11-01"
FOLDER = "lma-chat-attachments/Weekly_sync___2026_11_01/"
ADMIN = {"claims": {"cognito:groups": ["Admin"], "sub": "a"}, "username": "admin@example.com"}
OWNER = {"claims": {"sub": "o"}, "username": "owner@example.com"}
FRIEND = {"claims": {"sub": "f"}, "username": "friend@example.com"}
STRANGER = {"claims": {"sub": "x"}, "username": "x@example.com"}
BACKEND = {"userArn": "arn:aws:sts::123456789012:assumed-role/role/session", "username": "AROA"}


def item(
    attachment_id: str, content_type: str | None, name: str, key: str | None = None, status="STORED"
):
    found: Dict[str, Any] = {
        "PK": {"S": f"att#{CALL}"},
        "SK": {"S": f"a#{attachment_id}"},
        "CallId": {"S": CALL},
        "AttachmentId": {"S": attachment_id},
        "SegmentId": {"S": "chat-zoom-m1"},
        "Timestamp": {"N": "12.5"},
        "Kind": {"S": "IMAGE" if (content_type or "").startswith("image/") else "DOCUMENT"},
        "FileName": {"S": name},
        "Status": {"S": status},
        "Owner": {"S": "owner@example.com"},
        "SharedWith": {"L": [{"S": "friend@example.com"}]},
        "Title": {"S": "Whiteboard sketch"},
    }
    if content_type:
        found["ContentType"] = {"S": content_type}
    if status == "STORED":
        found["S3Key"] = {"S": key if key is not None else f"{FOLDER}{attachment_id}/{name}"}
    return found


class FakeDynamo:
    def __init__(self, items: List[Dict[str, Any]], call: Dict[str, Any] | None = None) -> None:
        self.items = items
        self.call = (
            call
            if call is not None
            else {
                "Owner": {"S": "owner@example.com"},
                "SharedWith": {"S": "[friend@example.com, other@example.com]"},
            }
        )
        self.queries: List[Dict[str, Any]] = []

    def get_item(self, **kwargs):
        pk = kwargs["Key"]["PK"]["S"]
        if pk.startswith("c#"):
            return {"Item": self.call} if self.call else {}
        sk = kwargs["Key"]["SK"]["S"]
        for found in self.items:
            if found["PK"]["S"] == pk and found["SK"]["S"] == sk:
                return {"Item": found}
        return {}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        pk = kwargs["ExpressionAttributeValues"][":pk"]["S"]
        return {"Items": [i for i in self.items if i["PK"]["S"] == pk]}


@pytest.fixture
def dynamo(monkeypatch):
    fake = FakeDynamo(
        [
            item("0000012500-aa", "image/png", "whiteboard.png"),
            item("0000013000-bb", "application/pdf", 'Q3 "plan" é.pdf'),
            item("0000014000-cc", None, "Plan.docx", status="NOT_STORED"),
            item(
                "0000015000-dd",
                "image/png",
                "elsewhere.png",
                key="lma-chat-attachments/Another/x/elsewhere.png",
            ),
        ]
    )
    monkeypatch.setattr(index, "DYNAMODB", fake)
    monkeypatch.setattr(index, "RECORDINGS_BUCKET", "rec-bucket")
    return fake


def call(identity, **arguments):
    return index.handler(
        {
            "info": {"fieldName": "getChatAttachments"},
            "identity": identity,
            "arguments": {"callId": CALL, **arguments},
        },
        None,
    )


def params(link: str) -> Dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(link).query).items()}


@pytest.mark.parametrize("identity", [ADMIN, OWNER, FRIEND, BACKEND])
def test_attachments_are_answered_to_the_callers_get_call_answers(dynamo, identity):
    assert len(call(identity)["ChatAttachments"]) == 4


def test_attachments_are_refused_to_anyone_else(dynamo):
    with pytest.raises(index.ForbiddenError):
        call(STRANGER)


def test_a_meeting_that_does_not_exist_is_refused_like_one_not_shared(dynamo):
    dynamo.call = {}
    with pytest.raises(index.ForbiddenError):
        call(OWNER)


def test_every_download_link_is_served_as_a_download_with_the_stored_type(dynamo):
    found = {a["AttachmentId"]: a for a in call(OWNER)["ChatAttachments"]}
    image = params(found["0000012500-aa"]["DownloadUrl"])
    assert image["response-content-disposition"].startswith("attachment;")
    assert image["response-content-type"] == "image/png"
    pdf = params(found["0000013000-bb"]["DownloadUrl"])
    assert pdf["response-content-disposition"].startswith("attachment;")
    assert pdf["response-content-type"] == "application/pdf"
    assert '"Q3 _plan_ _.pdf"' in pdf["response-content-disposition"], (
        "quotes and non-ASCII replaced"
    )
    assert "filename*=UTF-8''Q3%20%22plan%22%20%C3%A9.pdf" in pdf["response-content-disposition"]


def test_only_a_raster_image_gets_an_inline_preview(dynamo):
    found = {a["AttachmentId"]: a for a in call(OWNER)["ChatAttachments"]}
    preview = params(found["0000012500-aa"]["PreviewUrl"])
    assert preview["response-content-disposition"] == "inline"
    assert preview["response-content-type"] == "image/png"
    assert found["0000013000-bb"]["PreviewUrl"] is None


def test_an_unexpected_stored_type_is_downloaded_as_bytes(dynamo):
    dynamo.items = [item("0000012500-aa", "image/svg+xml", "logo.svg")]
    [attachment] = call(OWNER)["ChatAttachments"]
    assert params(attachment["DownloadUrl"])["response-content-type"] == "application/octet-stream"
    assert attachment["PreviewUrl"] is None


def test_nothing_is_signed_for_an_attachment_not_stored_or_outside_the_meetings_folder(dynamo):
    found = {a["AttachmentId"]: a for a in call(OWNER)["ChatAttachments"]}
    assert found["0000014000-cc"]["DownloadUrl"] is None
    assert (
        found["0000015000-dd"]["DownloadUrl"] is None
        and found["0000015000-dd"]["PreviewUrl"] is None
    )


def test_one_attachment_is_read_by_its_id(dynamo):
    [attachment] = call(FRIEND, attachmentId="0000013000-bb")["ChatAttachments"]
    assert attachment["FileName"] == 'Q3 "plan" é.pdf'
    assert attachment["SharedWith"] == "friend@example.com"
    assert call(FRIEND, attachmentId="0000099999-zz")["ChatAttachments"] == []
    with pytest.raises(index.BadRequestError):
        call(FRIEND, attachmentId="../x")


def test_the_access_check_comes_before_reading_one_attachment(dynamo):
    with pytest.raises(index.ForbiddenError):
        call(STRANGER, attachmentId="0000013000-bb")


def test_a_page_token_cannot_be_carried_to_another_meeting(dynamo):
    other = base64.urlsafe_b64encode(
        json.dumps({"PK": {"S": "att#Another"}, "SK": {"S": "a#x"}}).encode()
    ).decode()
    with pytest.raises(index.BadRequestError):
        call(OWNER, nextToken=other)


def test_another_field_is_refused():
    with pytest.raises(index.BadRequestError):
        index.handler({"info": {"fieldName": "getCall"}, "arguments": {"callId": CALL}}, None)
