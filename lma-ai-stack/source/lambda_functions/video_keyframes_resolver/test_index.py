# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the key frame resolver (no AWS calls).

Asserted here: key frames are answered to exactly the callers ``getCall``
answers; an image link is signed only for an object in the meeting's own
key-frame folder in this deployment's bucket; pages are in time order and a
page token cannot be carried to another meeting.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

HERE = Path(__file__).resolve().parent
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["EVENT_SOURCING_TABLE"] = "calls"
os.environ["RECORDINGS_BUCKET"] = "rec-bucket"

_SPEC = importlib.util.spec_from_file_location("video_keyframes_resolver_index", HERE / "index.py")
index = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index)

CALL = "Weekly sync - 2026-11-01"
FOLDER = "lma-video-keyframes/Weekly_sync___2026_11_01/"
ADMIN = {"claims": {"cognito:groups": ["Admin"], "sub": "a"}, "username": "admin@example.com"}
OWNER = {"claims": {"sub": "o"}, "username": "owner@example.com"}
FRIEND = {"claims": {"sub": "f"}, "username": "friend@example.com"}
STRANGER = {"claims": {"sub": "x"}, "username": "x@example.com"}
BACKEND = {"userArn": "arn:aws:sts::123456789012:assumed-role/role/session", "username": "AROA"}


def url(key: str, bucket: str = "rec-bucket") -> str:
    from urllib.parse import quote

    return f"https://{bucket}.s3.us-east-1.amazonaws.com/{quote(key, safe='')}"


def item(ms: int, image: str | None = None, thumb: str | None = None) -> Dict[str, Any]:
    frame_id = f"{ms:010d}-ab12cd"
    return {
        "PK": {"S": f"kf#{CALL}"},
        "SK": {"S": f"f#{frame_id}"},
        "CallId": {"S": CALL},
        "FrameId": {"S": frame_id},
        "CreatedAt": {"S": "2026-11-01T10:00:00Z"},
        "Timestamp": {"N": str(ms / 1000)},
        "VideoOffset": {"N": "12.5"},
        "Width": {"N": "1568"},
        "Height": {"N": "882"},
        "ShareDetected": {"BOOL": True},
        "ImageUrl": {"S": url(f"{FOLDER}{frame_id}.jpg") if image is None else image},
        "ThumbnailUrl": {"S": url(f"{FOLDER}{frame_id}_thumb.jpg") if thumb is None else thumb},
        "Owner": {"S": "owner@example.com"},
        "SharedWith": {"L": [{"S": "friend@example.com"}]},
    }


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
        return {"Item": self.call} if self.call else {}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        rows = [i for i in self.items if i["PK"] == kwargs["ExpressionAttributeValues"][":pk"]]
        after = kwargs["ExpressionAttributeValues"].get(":after")
        if after:
            rows = [i for i in rows if i["SK"]["S"] > after["S"]]
        start = kwargs.get("ExclusiveStartKey")
        if start:
            rows = [i for i in rows if i["SK"]["S"] > start["SK"]["S"]]
        page = rows[: kwargs["Limit"]]
        out: Dict[str, Any] = {"Items": page}
        if len(rows) > kwargs["Limit"]:
            out["LastEvaluatedKey"] = {"PK": page[-1]["PK"], "SK": page[-1]["SK"]}
        return out


class FakeS3:
    def __init__(self) -> None:
        self.signed: List[str] = []

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
        self.signed.append(Params["Key"])
        return f"https://signed.example/{Params['Key']}?X-Amz-Expires={ExpiresIn}"


@pytest.fixture
def fakes(monkeypatch):
    def install(items, call=None):
        dynamo, s3 = FakeDynamo(items, call), FakeS3()
        monkeypatch.setattr(index, "DYNAMODB", dynamo)
        monkeypatch.setattr(index, "S3", s3)
        return dynamo, s3

    return install


def call(identity, **arguments):
    return index.handler(
        {
            "identity": identity,
            "arguments": {"callId": CALL, **arguments},
            "info": {"fieldName": "getVideoKeyframes"},
        },
        None,
    )


@pytest.mark.parametrize("identity", [ADMIN, OWNER, FRIEND, BACKEND])
def test_the_callers_getcall_answers_see_the_frames(fakes, identity):
    fakes([item(1000)])
    result = call(identity)
    assert [f["FrameId"] for f in result["VideoKeyframes"]] == ["0000001000-ab12cd"]


def test_anyone_else_is_refused_and_nothing_is_read(fakes):
    dynamo, s3 = fakes([item(1000)])
    with pytest.raises(index.ForbiddenError):
        call(STRANGER)
    assert dynamo.queries == [] and s3.signed == []


def test_a_meeting_that_does_not_exist_is_refused_like_one_not_shared(fakes):
    fakes([item(1000)], call={})
    with pytest.raises(index.ForbiddenError):
        call(OWNER)


def test_both_images_get_signed_links_and_the_fields_keep_their_types(fakes):
    _, s3 = fakes([item(1000)])
    frame = call(OWNER)["VideoKeyframes"][0]
    assert frame["ImageSignedUrl"].startswith(
        f"https://signed.example/{FOLDER}0000001000-ab12cd.jpg"
    )
    assert frame["ThumbnailSignedUrl"].endswith("_thumb.jpg?X-Amz-Expires=3600")
    assert frame["Timestamp"] == 1.0 and frame["VideoOffset"] == 12.5
    assert frame["Width"] == 1568 and isinstance(frame["Width"], int)
    assert frame["ShareDetected"] is True
    assert frame["SharedWith"] == "friend@example.com"
    assert sorted(s3.signed) == [
        f"{FOLDER}0000001000-ab12cd.jpg",
        f"{FOLDER}0000001000-ab12cd_thumb.jpg",
    ]


@pytest.mark.parametrize(
    "image",
    [
        url("lma-video-keyframes/Another_meeting/f.jpg"),  # another meeting's folder
        url("lma-audio-recordings/Weekly_sync___2026_11_01.wav"),  # another prefix
        url(f"{FOLDER}f.jpg", bucket="someone-elses-bucket"),  # another bucket
        url(f"{FOLDER}../../lma-audio-recordings/x.wav"),
        "http://rec-bucket.s3.us-east-1.amazonaws.com/" + FOLDER + "f.jpg",
        "s3://rec-bucket/" + FOLDER + "f.jpg",
        "",
    ],
)
def test_a_link_is_signed_only_inside_the_meetings_own_folder(fakes, image):
    _, s3 = fakes([item(1000, image=image)])
    frame = call(OWNER)["VideoKeyframes"][0]
    assert frame["ImageSignedUrl"] is None
    assert frame["ThumbnailSignedUrl"] is not None
    assert s3.signed == [f"{FOLDER}0000001000-ab12cd_thumb.jpg"]


def test_pages_follow_in_time_order_and_new_frames_come_after_a_given_one(fakes):
    fakes([item(ms) for ms in (1000, 2000, 3000, 4000, 5000)])
    first = call(OWNER, limit=2)
    assert [f["Timestamp"] for f in first["VideoKeyframes"]] == [1.0, 2.0]
    second = call(OWNER, limit=2, nextToken=first["nextToken"])
    assert [f["Timestamp"] for f in second["VideoKeyframes"]] == [3.0, 4.0]
    newer = call(OWNER, afterFrameId="0000003000-ab12cd")
    assert [f["Timestamp"] for f in newer["VideoKeyframes"]] == [4.0, 5.0]
    assert newer["nextToken"] is None


def test_a_page_token_cannot_be_used_for_another_meeting(fakes):
    fakes([item(ms) for ms in (1000, 2000, 3000)])
    other = {"PK": {"S": "kf#Another meeting"}, "SK": {"S": "f#0000000001-aa"}}
    token = base64.urlsafe_b64encode(json.dumps(other).encode()).decode()
    with pytest.raises(index.BadRequestError):
        call(OWNER, nextToken=token)
    with pytest.raises(index.BadRequestError):
        call(OWNER, nextToken="not base64 at all!")


@pytest.mark.parametrize(
    "arguments", [{"limit": "10"}, {"afterFrameId": "f#x"}, {"afterFrameId": "x" * 65}]
)
def test_arguments_that_cannot_be_used_are_refused(fakes, arguments):
    fakes([item(1000)])
    with pytest.raises(index.BadRequestError):
        call(OWNER, **arguments)


def test_the_limit_is_capped(fakes):
    dynamo, _ = fakes([item(1000)])
    call(OWNER, limit=100000)
    assert dynamo.queries[0]["Limit"] == index.MAX_LIMIT
