# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the key frame text extractor.

Asserted here: a frame's text is read once (a frame that already has it, or
that no longer exists, costs no model call); only an image in the meeting's own
folder of this deployment's bucket is read; what the model answers is stored
trimmed, with "no text" as empty; every model call is metered against the
meeting; and a failure ends the invocation without raising.

No AWS: the module's clients are built at import time and replaced here.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["EVENT_SOURCING_TABLE"] = "calls"
os.environ["RECORDINGS_BUCKET"] = "rec-bucket"
os.environ["KEYFRAME_TEXT_MODEL_ID"] = "amazon.nova-lite-v1:0"

HERE = Path(__file__).resolve().parent


def _load():
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))
    spec = importlib.util.spec_from_file_location(
        "keyframe_text_extractor_index", HERE / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()
import pure  # noqa: E402

CALL = "Weekly sync - 2026-11-01"
FRAME = "0000030000-ab12cd"
IMAGE_URL = (
    "https://rec-bucket.s3.us-east-1.amazonaws.com/"
    "lma-video-keyframes%2FWeekly_sync___2026_11_01%2F0000030000-ab12cd.jpg"
)
EVENT = {"CallId": CALL, "FrameId": FRAME, "ImageUrl": IMAGE_URL}


class Body:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self, _limit=None) -> bytes:
        return self.data


class FakeDynamo:
    def __init__(self, item):
        self.item = item
        self.updates: List[Dict[str, Any]] = []

    def get_item(self, **_kwargs):
        return {"Item": self.item} if self.item is not None else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


class FakeS3:
    def __init__(self, data=b"\xff\xd8jpeg\xff\xd9", error=None):
        self.data, self.error, self.keys = data, error, []

    def get_object(self, Bucket, Key):  # noqa: N803
        self.keys.append(Key)
        if self.error:
            raise self.error
        return {"Body": Body(self.data)}


class FakeBedrock:
    def __init__(self, answer="Agenda item 2: Support\n- pipeline budget", content=None):
        self.answer, self.calls = answer, []
        self.content = [{"text": answer}] if content is None else content

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": {"message": {"content": self.content}},
            "usage": {"inputTokens": 2144, "outputTokens": 30},
            "stopReason": "end_turn",
        }


@pytest.fixture
def fakes(monkeypatch):
    metered: List[Any] = []

    def install(item=None, s3=None, bedrock=None):
        dynamo = FakeDynamo({"PK": {"S": f"kf#{CALL}"}} if item is None else item)
        s3 = s3 or FakeS3()
        bedrock = bedrock or FakeBedrock()
        monkeypatch.setattr(index, "DYNAMODB", dynamo)
        monkeypatch.setattr(index, "S3", s3)
        monkeypatch.setattr(index, "BEDROCK", bedrock)
        monkeypatch.setattr(index.METERING, "record", lambda *a, **kw: metered.append(a) or True)
        return dynamo, s3, bedrock, metered

    return install


def test_a_frames_text_is_read_stored_and_metered(fakes):
    dynamo, s3, bedrock, metered = fakes()
    assert index.handler(dict(EVENT), None)["status"] == "read"
    assert s3.keys == ["lma-video-keyframes/Weekly_sync___2026_11_01/0000030000-ab12cd.jpg"]
    sent = bedrock.calls[0]["messages"][0]["content"]
    assert sent[0]["image"]["source"]["bytes"].startswith(b"\xff\xd8")
    assert "Do not describe or name people" in sent[1]["text"]
    update = dynamo.updates[0]
    assert update["Key"] == {"PK": {"S": f"kf#{CALL}"}, "SK": {"S": f"f#{FRAME}"}}
    assert update["ExpressionAttributeValues"][":t"] == {
        "S": "Agenda item 2: Support\n- pipeline budget"
    }
    assert update["ConditionExpression"] == "attribute_exists(PK)"
    assert metered == [
        (
            CALL,
            "SlideText",
            "bedrock/amazon.nova-lite-v1:0",
            {"inputTokens": 2144, "outputTokens": 30, "requests": 1},
        )
    ]


def test_a_frame_that_already_has_its_text_costs_nothing(fakes):
    _, s3, bedrock, metered = fakes(item={"PK": {"S": f"kf#{CALL}"}, "SlideText": {"S": "x"}})
    assert index.handler(dict(EVENT), None) == {"status": "skipped", "reason": "already read"}
    assert s3.keys == [] and bedrock.calls == [] and metered == []


def test_a_frame_that_is_not_stored_costs_nothing(fakes):
    dynamo, s3, bedrock, _ = fakes(item={})
    dynamo.item = None
    assert index.handler(dict(EVENT), None)["reason"] == "no frame"
    assert bedrock.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"ImageUrl": IMAGE_URL.replace("rec-bucket", "other-bucket")},
        {"ImageUrl": IMAGE_URL.replace("Weekly_sync___2026_11_01", "Another_meeting")},
        {"ImageUrl": "s3://rec-bucket/lma-video-keyframes/Weekly_sync___2026_11_01/f.jpg"},
        {"FrameId": "f#bad"},
        {"CallId": ""},
    ],
)
def test_only_the_meetings_own_images_are_read(fakes, change):
    _, s3, bedrock, _ = fakes()
    assert index.handler({**EVENT, **change}, None)["status"] == "skipped"
    assert s3.keys == [] and bedrock.calls == []


def test_a_picture_with_no_text_is_stored_as_empty(fakes):
    dynamo, *_ = fakes(bedrock=FakeBedrock(answer="NO TEXT."))
    index.handler(dict(EVENT), None)
    assert dynamo.updates[0]["ExpressionAttributeValues"][":t"] == {"S": ""}


def test_failures_end_the_invocation_without_raising(fakes):
    err = ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetObject")
    dynamo, *_ = fakes(s3=FakeS3(error=err))
    assert index.handler(dict(EVENT), None) == {"status": "failed", "reason": "image unreadable"}
    assert dynamo.updates == []


def test_an_answer_with_no_text_ends_the_invocation_without_raising(fakes):
    dynamo, *_ = fakes(bedrock=FakeBedrock(content=[{"reasoningContent": {"reasoningText": {}}}]))
    assert index.handler(dict(EVENT), None) == {"status": "failed", "reason": "no answer"}
    assert dynamo.updates == []


def test_table_errors_end_the_invocation_without_raising(fakes):
    dynamo, *_ = fakes()
    throttled = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "x"}}, "UpdateItem"
    )

    def slow(**_kwargs):
        raise throttled

    dynamo.update_item = slow
    assert index.handler(dict(EVENT), None) == {"status": "failed", "reason": "not stored"}
    dynamo.get_item = slow
    assert index.handler(dict(EVENT), None) == {"status": "failed", "reason": "frame unreadable"}


def test_the_function_is_not_retried_after_a_failure():
    template = (HERE.parents[2] / "deployment" / "lma-ai-stack.yaml").read_text(encoding="utf-8")
    block = template.split("  KeyframeTextExtractorFunction:\n", 1)[1].split(
        "\n  KeyframeTextExtractorFunctionLogGroup:", 1
    )[0]
    assert "EventInvokeConfig:\n        MaximumRetryAttempts: 0" in block


def test_a_frame_deleted_meanwhile_is_left_deleted(fakes):
    dynamo, *_ = fakes()

    def gone(**_kwargs):
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "UpdateItem"
        )

    dynamo.update_item = gone
    assert index.handler(dict(EVENT), None) == {"status": "skipped", "reason": "frame deleted"}


# ── pure ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "answer, stored",
    [
        ("  Title\n- one  ", "Title\n- one"),
        ("```\nTitle\n```", "Title"),
        ("NO TEXT", ""),
        ("no text.", ""),
        (None, ""),
    ],
)
def test_the_answer_is_trimmed_before_it_is_stored(answer, stored):
    assert pure.clean_text(answer) == stored


def test_a_runaway_answer_is_cut_at_a_line():
    text = pure.clean_text("line of text\n" * 2000)
    assert len(text) <= pure.MAX_TEXT_CHARS
    assert text.endswith("line of text")
