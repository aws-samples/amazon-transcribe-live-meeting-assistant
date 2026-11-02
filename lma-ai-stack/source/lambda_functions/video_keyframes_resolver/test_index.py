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
        self.batches: List[Dict[str, Any]] = []
        self.unprocessed_rounds = 0

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        if key["PK"]["S"].startswith("kf#"):
            found = [i for i in self.items if i["PK"] == key["PK"] and i["SK"] == key["SK"]]
            return {"Item": found[0]} if found else {}
        return {"Item": self.call} if self.call else {}

    def batch_get_item(self, RequestItems):  # noqa: N803
        self.batches.append(RequestItems)
        ((table, request),) = RequestItems.items()
        keys = request["Keys"]
        # The first round leaves the last key unprocessed when asked to, as
        # DynamoDB does under load.
        held = keys[-1:] if self.unprocessed_rounds > 0 and len(keys) > 1 else []
        self.unprocessed_rounds -= 1
        answered = [k for k in keys if k not in held]
        found = [
            i for i in self.items for k in answered if i["PK"] == k["PK"] and i["SK"] == k["SK"]
        ]
        out: Dict[str, Any] = {"Responses": {table: found}}
        if held:
            out["UnprocessedKeys"] = {table: {"Keys": held}}
        return out

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


#: Every shape the meeting's SharedWith is stored in; the same table as in
#: call_cost_resolver's tests, whose check is a copy of this one.
SHARED_WITH_SHAPES = {
    "list": {"L": [{"S": "friend@example.com"}, {"S": "other@example.com"}]},
    "string set": {"SS": ["friend@example.com", "other@example.com"]},
    "string": {"S": "friend@example.com, other@example.com"},
    "bracketed string": {"S": "[other@example.com, friend@example.com]"},
    "single string": {"S": "friend@example.com"},
}


@pytest.mark.parametrize("shape", sorted(SHARED_WITH_SHAPES))
def test_every_stored_shared_with_shape_answers_the_same_callers(fakes, shape):
    meeting = {"Owner": {"S": "owner@example.com"}, "SharedWith": SHARED_WITH_SHAPES[shape]}
    for identity in (OWNER, FRIEND, ADMIN, BACKEND):
        fakes([item(1000)], call=meeting)
        assert len(call(identity)["VideoKeyframes"]) == 1
    for identity in (STRANGER, {"claims": {"sub": "p"}, "username": "friend@example"}):
        fakes([item(1000)], call=meeting)
        with pytest.raises(index.ForbiddenError):
            call(identity)


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
        url(f"{FOLDER}f.jpg", bucket="rec-bucket-2"),  # a bucket whose name begins the same
        url(f"{FOLDER}f.jpg", bucket="rec"),  # a prefix of this bucket's name
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
    call(OWNER, limit=0)
    call(OWNER)
    assert [q["Limit"] for q in dynamo.queries] == [index.MAX_LIMIT, 1, index.DEFAULT_LIMIT]


def test_a_bucket_whose_name_has_dots_is_recognised(fakes, monkeypatch):
    monkeypatch.setattr(index, "RECORDINGS_BUCKET", "lma.recordings.example")
    dotted = url(f"{FOLDER}0000001000-ab12cd.jpg", bucket="lma.recordings.example")
    _, s3 = fakes([item(1000, image=dotted)])
    frame = call(OWNER)["VideoKeyframes"][0]
    assert frame["ImageSignedUrl"] is not None
    assert s3.signed[0] == f"{FOLDER}0000001000-ab12cd.jpg"


def test_one_frame_can_be_read_by_its_id_with_its_links(fakes):
    fakes([item(ms) for ms in (1000, 2000, 3000)])
    result = call(OWNER, frameId="0000002000-ab12cd")
    assert [f["Timestamp"] for f in result["VideoKeyframes"]] == [2.0]
    assert result["VideoKeyframes"][0]["ImageSignedUrl"]
    assert call(OWNER, frameId="0000009999-ab12cd")["VideoKeyframes"] == []
    with pytest.raises(index.BadRequestError):
        call(OWNER, frameId="f#0000002000-ab12cd")


def test_a_frame_id_is_still_refused_to_anyone_else(fakes):
    fakes([item(1000)])
    with pytest.raises(index.ForbiddenError):
        call(STRANGER, frameId="0000001000-ab12cd")


def test_the_frames_a_summary_names_are_read_in_one_request_in_the_order_named(fakes):
    dynamo, _ = fakes([item(ms) for ms in (1000, 2000, 3000)])
    result = call(
        OWNER,
        frameIds=[
            "0000003000-ab12cd",
            "0000009999-ab12cd",
            "0000001000-ab12cd",
            "0000003000-ab12cd",
        ],
    )
    assert [f["FrameId"] for f in result["VideoKeyframes"]] == [
        "0000003000-ab12cd",
        "0000001000-ab12cd",
    ]
    assert all(f["ThumbnailSignedUrl"] and f["ImageSignedUrl"] for f in result["VideoKeyframes"])
    assert result["nextToken"] is None
    assert len(dynamo.batches) == 1
    # Each frame asked for once, under this meeting only.
    keys = dynamo.batches[0]["calls"]["Keys"]
    assert len(keys) == 3
    assert {k["PK"]["S"] for k in keys} == {f"kf#{CALL}"}
    assert dynamo.queries == []


def test_keys_left_unprocessed_are_asked_for_again(fakes):
    dynamo, _ = fakes([item(ms) for ms in (1000, 2000)])
    dynamo.unprocessed_rounds = 1
    result = call(OWNER, frameIds=["0000001000-ab12cd", "0000002000-ab12cd"])
    assert [f["FrameId"] for f in result["VideoKeyframes"]] == [
        "0000001000-ab12cd",
        "0000002000-ab12cd",
    ]
    assert len(dynamo.batches) == 2


def test_an_empty_list_reads_nothing(fakes):
    dynamo, _ = fakes([item(1000)])
    assert call(OWNER, frameIds=[])["VideoKeyframes"] == []
    assert dynamo.batches == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"frameIds": ["f#0000001000-ab12cd"]},
        {"frameIds": "0000001000-ab12cd"},
        {"frameIds": [f"{n:010d}-ab12cd" for n in range(101)]},
        {"frameIds": ["0000001000-ab12cd"], "frameId": "0000001000-ab12cd"},
    ],
)
def test_a_malformed_or_oversized_list_is_refused(fakes, arguments):
    fakes([item(1000)])
    with pytest.raises(index.BadRequestError):
        call(OWNER, **arguments)


def test_the_frames_a_summary_names_are_still_refused_to_anyone_else(fakes):
    dynamo, _ = fakes([item(1000)])
    with pytest.raises(index.ForbiddenError):
        call(STRANGER, frameIds=["0000001000-ab12cd"])
    assert dynamo.batches == []
