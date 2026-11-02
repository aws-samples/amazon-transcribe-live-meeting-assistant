#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the consent evidence verifier's plumbing.

An image is read only from the meeting's own evidence folder and only when it
is small enough for the model; the model is sent the image in its own format;
every call is metered against the meeting; and a failure is answered as a
message rather than raised.

No AWS: the module's clients are built at import time and replaced here.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("RECORDINGS_BUCKET", "lma-recordings")

HERE = Path(__file__).resolve().parent


def _load():
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))
    spec = importlib.util.spec_from_file_location("consent_verifier_index", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()

CALL_ID = "Weekly sync 2026-11-02"
KEY = "lma-consent-evidence/Weekly_sync_2026_11_02/chat.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


class FakeS3:
    def __init__(self, data=PNG, size=None, missing=False):
        self.data, self.size, self.missing = data, size, missing

    def head_object(self, Bucket, Key):  # noqa: N803
        if self.missing:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": self.size if self.size is not None else len(self.data)}

    def get_object(self, Bucket, Key):  # noqa: N803
        return {"Body": io.BytesIO(self.data)}


class FakeBedrock:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": {"message": {"content": [{"text": self.text}]}},
            "usage": {"inputTokens": 800, "outputTokens": 120, "totalTokens": 920},
        }


class FakeMetering:
    def __init__(self):
        self.records = []

    def record(self, call_id, context, service, usage, **kwargs):
        self.records.append((call_id, context, service, usage))
        return True


ANSWER = json.dumps(
    {
        "noticeVisible": True,
        "noticeMatches": True,
        "replies": [{"name": "Bo Chen", "text": "I object", "decision": "objection"}],
        "anyObjection": True,
        "confidence": 0.9,
        "summary": "Bo Chen objects.",
    }
)


@pytest.fixture
def fakes(monkeypatch):
    s3, bedrock, metering = FakeS3(), FakeBedrock(ANSWER), FakeMetering()
    monkeypatch.setattr(index, "S3", s3)
    monkeypatch.setattr(index, "BEDROCK", bedrock)
    monkeypatch.setattr(index, "METERING", metering)
    return s3, bedrock, metering


def event(**overrides):
    base = {
        "CallId": CALL_ID,
        "S3Key": KEY,
        "NoticeText": "Reply I consent or I object",
        "Mode": "notify",
        "Participants": ["Bo Chen"],
        "ConsentPhrases": ["I consent"],
        "ObjectionPhrases": ["I object"],
        "ModelId": "amazon.nova-lite-v1:0",
    }
    base.update(overrides)
    return base


def test_a_check_returns_the_verdict_and_meters_the_call(fakes) -> None:
    _s3, bedrock, metering = fakes
    result = index.handler(event(), None)
    assert result["Ok"] is True
    verification = result["Verification"]
    assert verification["Verdict"] == "objection_found"
    assert verification["ModelId"] == "amazon.nova-lite-v1:0"
    assert (verification["InputTokens"], verification["OutputTokens"]) == (800, 120)
    image = bedrock.calls[0]["messages"][0]["content"][0]["image"]
    assert image["format"] == "png" and image["source"]["bytes"] == PNG
    assert metering.records == [
        (
            CALL_ID,
            "ConsentVerification",
            "bedrock/amazon.nova-lite-v1:0",
            {"inputTokens": 800, "outputTokens": 120, "requests": 1},
        )
    ]


def test_an_image_outside_the_meetings_folder_is_not_read(fakes) -> None:
    _s3, bedrock, _metering = fakes
    for key in ("lma-video-keyframes/x.png", "lma-consent-evidence/other/x.png", None):
        result = index.handler(event(S3Key=key), None)
        assert result == {
            "Ok": False,
            "Message": "the evidence is not in this meeting's evidence folder",
        }
    assert bedrock.calls == []


def test_a_large_or_missing_or_unknown_image_is_refused(monkeypatch, fakes) -> None:
    monkeypatch.setattr(index, "S3", FakeS3(size=index.MAX_IMAGE_BYTES + 1))
    assert "larger" in index.handler(event(), None)["Message"]
    monkeypatch.setattr(index, "S3", FakeS3(missing=True))
    assert "could not be read" in index.handler(event(), None)["Message"]
    monkeypatch.setattr(index, "S3", FakeS3(data=b"%PDF-1.7"))
    assert "not a JPEG" in index.handler(event(), None)["Message"]


def test_a_model_failure_is_answered_as_a_message(monkeypatch, fakes) -> None:
    class Failing:
        def converse(self, **_kwargs):
            raise RuntimeError("throttled")

    monkeypatch.setattr(index, "BEDROCK", Failing())
    assert index.handler(event(), None) == {
        "Ok": False,
        "Message": "the evidence could not be checked just now; try again",
    }


def test_the_default_model_is_the_settings_default(fakes) -> None:
    _s3, bedrock, _metering = fakes
    index.handler(event(ModelId=None), None)
    assert bedrock.calls[0]["modelId"] == index.FALLBACK_MODEL_ID == "amazon.nova-lite-v1:0"
