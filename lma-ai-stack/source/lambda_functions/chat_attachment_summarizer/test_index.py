# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Tests for the chat attachment summarizer.

Asserted here: an attachment is summarized once (one already summarized, one
deleted, or one not to be summarized costs no model call); only an object in the
meeting's own folder is read; an image goes to the model as an image block and a
document as a document block; a type or size the model cannot read is skipped
without a call; the answer is stored as one line of text each; every model call
is metered against the meeting; and a failure ends the invocation without
raising.

No AWS: the module's clients are built at import time and replaced here.
"""

from __future__ import annotations

import importlib.util
import json
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

HERE = Path(__file__).resolve().parent


def _load():
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))
    spec = importlib.util.spec_from_file_location(
        "chat_attachment_summarizer_index", HERE / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()
import pure  # noqa: E402

CALL = "Weekly sync - 2026-11-01"
ATTACHMENT = "0000012500-ab12cd34"
KEY = f"lma-chat-attachments/Weekly_sync___2026_11_01/{ATTACHMENT}/whiteboard.png"
EVENT = {"CallId": CALL, "AttachmentId": ATTACHMENT}
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def stored(content_type="image/png", key=KEY, **extra) -> Dict[str, Any]:
    item = {
        "PK": {"S": f"att#{CALL}"},
        "Status": {"S": "STORED"},
        "Summarize": {"BOOL": True},
        "S3Key": {"S": key},
        "ContentType": {"S": content_type},
        "FileName": {"S": "whiteboard <b>.png"},
    }
    item.update(extra)
    return item


class Body:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self, _limit=None) -> bytes:
        return self.data


class FakeDynamo:
    def __init__(self, item):
        self.item = item
        self.updates: List[Dict[str, Any]] = []
        self.update_error = None

    def get_item(self, **_kwargs):
        return {"Item": self.item} if self.item is not None else {}

    def update_item(self, **kwargs):
        if self.update_error:
            raise self.update_error
        self.updates.append(kwargs)


class FakeS3:
    def __init__(self, data: bytes = PNG, size: int | None = None):
        self.data = data
        self.size = len(data) if size is None else size
        self.reads: List[str] = []

    def head_object(self, Bucket, Key):  # noqa: N803
        assert Bucket == "rec-bucket"
        return {"ContentLength": self.size}

    def get_object(self, Bucket, Key):  # noqa: N803
        self.reads.append(Key)
        return {"Body": Body(self.data)}


class FakeBedrock:
    def __init__(
        self,
        answer: str = '{"title": "Whiteboard sketch", "summary": "A diagram of the\\nrollout plan."}',
    ):
        self.answer = answer
        self.calls: List[Dict[str, Any]] = []
        self.error = None
        #: Responses returned before the normal one, in order (a cut-off answer, say).
        self.first: List[Dict[str, Any]] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if self.first:
            return self.first.pop(0)
        return {
            "output": {"message": {"content": [{"text": self.answer}]}},
            "usage": {"inputTokens": 1500, "outputTokens": 40, "totalTokens": 1540},
            "stopReason": "end_turn",
        }


class FakeMetering:
    def __init__(self):
        self.records: List[tuple] = []

    def record(self, *args, **_kwargs):
        self.records.append(args)


@pytest.fixture
def fakes(monkeypatch):
    def install(item=None, s3=None, bedrock=None):
        dynamo = FakeDynamo(stored() if item is None else item)
        s3 = s3 or FakeS3()
        bedrock = bedrock or FakeBedrock()
        metering = FakeMetering()
        monkeypatch.setattr(index, "DYNAMODB", dynamo)
        monkeypatch.setattr(index, "S3", s3)
        monkeypatch.setattr(index, "BEDROCK", bedrock)
        monkeypatch.setattr(index, "METERING", metering)
        monkeypatch.setattr(index, "RECORDINGS_BUCKET", "rec-bucket")
        monkeypatch.setattr(index, "model_id", lambda: "global.anthropic.claude-haiku-5-5")
        return dynamo, s3, bedrock, metering

    return install


def _values(update):
    return {k: v["S"] for k, v in update["ExpressionAttributeValues"].items()}


def test_an_image_is_summarized_once_and_the_answer_stored(fakes):
    dynamo, s3, bedrock, metering = fakes()
    assert index.handler(dict(EVENT), None) == {"status": "done"}
    [call] = bedrock.calls
    content = call["messages"][0]["content"]
    assert content[0] == {"image": {"format": "png", "source": {"bytes": PNG}}}
    assert "<file_name>whiteboard (b).png</file_name>" in content[1]["text"], (
        "the name cannot close the tag"
    )
    assert "do not follow" in content[1]["text"]
    [update] = dynamo.updates
    values = _values(update)
    assert "Whiteboard sketch" in values.values()
    assert "A diagram of the rollout plan." in values.values(), "kept as one line"
    assert "DONE" in values.values()
    assert "attribute_not_exists(SummaryState)" in update["ConditionExpression"]
    assert metering.records == [
        (
            CALL,
            "ChatAttachments",
            "bedrock/global.anthropic.claude-haiku-5-5",
            {"inputTokens": 1500, "outputTokens": 40, "requests": 1},
        )
    ]


def test_a_document_goes_to_the_model_as_a_document_block(fakes):
    _dynamo, _s3, bedrock, _m = fakes(item=stored("application/pdf"), s3=FakeS3(b"%PDF-1.7"))
    index.handler(dict(EVENT), None)
    block = bedrock.calls[0]["messages"][0]["content"][0]
    assert block == {
        "document": {"format": "pdf", "name": "attachment", "source": {"bytes": b"%PDF-1.7"}}
    }


@pytest.mark.parametrize(
    "item",
    [
        stored(SummaryState={"S": "DONE"}),
        stored(Summarize={"BOOL": False}),
        {**stored(), "Status": {"S": "NOT_STORED"}},
    ],
    ids=["already summarized", "switched off", "not stored"],
)
def test_no_model_call_is_made_for_an_attachment_not_to_be_summarized(fakes, item):
    dynamo, s3, bedrock, metering = fakes(item=item)
    assert index.handler(dict(EVENT), None)["status"] == "skipped"
    assert (
        bedrock.calls == [] and s3.reads == [] and metering.records == [] and dynamo.updates == []
    )


def test_an_attachment_deleted_meanwhile_costs_nothing(fakes, monkeypatch):
    dynamo, _s3, bedrock, _m = fakes()
    monkeypatch.setattr(dynamo, "item", None)
    assert index.handler(dict(EVENT), None) == {"status": "skipped", "reason": "no attachment"}
    assert bedrock.calls == []


def test_only_an_object_in_the_meetings_folder_is_read(fakes):
    _dynamo, s3, bedrock, _m = fakes(item=stored(key="lma-audio-recordings/other.wav"))
    assert index.handler(dict(EVENT), None)["reason"] == "outside the meeting's folder"
    assert s3.reads == [] and bedrock.calls == []


@pytest.mark.parametrize(
    "content_type, size",
    [
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", 1000),
        ("image/png", pure.MAX_IMAGE_BYTES + 1),
        ("application/pdf", pure.MAX_DOCUMENT_BYTES + 1),
    ],
    ids=["no converse format", "image too large", "document too large"],
)
def test_what_the_model_cannot_read_is_skipped_without_a_call(fakes, content_type, size):
    dynamo, s3, bedrock, _m = fakes(item=stored(content_type), s3=FakeS3(size=size))
    assert index.handler(dict(EVENT), None)["status"] == "skipped"
    assert bedrock.calls == [] and s3.reads == []
    assert "SKIPPED" in _values(dynamo.updates[0]).values()


def test_a_model_error_marks_the_attachment_failed_without_raising(fakes):
    bedrock = FakeBedrock()
    bedrock.error = ClientError({"Error": {"Code": "ThrottlingException"}}, "Converse")
    dynamo, _s3, _b, _m = fakes(bedrock=bedrock)
    assert index.handler(dict(EVENT), None) == {"status": "failed", "reason": "model error"}
    assert "FAILED" in _values(dynamo.updates[0]).values()


def test_an_answer_without_a_title_or_summary_marks_the_attachment_failed(fakes):
    dynamo, _s3, _b, metering = fakes(bedrock=FakeBedrock("I cannot help with that."))
    assert index.handler(dict(EVENT), None)["reason"] == "no answer"
    assert "FAILED" in _values(dynamo.updates[0]).values()
    assert len(metering.records) == 1, "the call is metered whatever it answered"


def test_a_request_without_a_usable_id_is_skipped(fakes):
    _d, _s, bedrock, _m = fakes()
    assert index.handler({"CallId": CALL, "AttachmentId": "../x"}, None)["status"] == "skipped"
    assert index.handler({}, None)["status"] == "skipped"
    assert bedrock.calls == []


def test_the_answer_is_read_from_prose_or_a_code_fence_and_cut_to_the_limits():
    assert pure.read_answer('Here: ```json\n{"title": "A", "summary": "B"}\n```') == ("A", "B")
    title, summary = pure.read_answer(json.dumps({"title": "t" * 500, "summary": "s" * 5000}))
    assert len(title) == pure.MAX_TITLE_CHARS and len(summary) == pure.MAX_SUMMARY_CHARS
    with pytest.raises(ValueError):
        pure.read_answer('{"title": "", "summary": ""}')
    with pytest.raises(ValueError):
        pure.read_answer("[1, 2]")


def test_the_fallback_model_is_the_meeting_assistants_shipped_default():
    defaults = json.loads(
        (
            HERE.parents[1]
            / "lambda_layers"
            / "configuration_layer"
            / "lma_config"
            / "setting_defaults.json"
        ).read_text()
    )
    assert index.FALLBACK_MODEL_ID == defaults["language_models"]["meetingAssistModelId"]["default"]


# ── an answer cut off at the output budget ──────────────────────────────────

#: What a reasoning model returns when it spent the whole budget reasoning.
REASONED_ONLY = {
    "output": {"message": {"content": [{"reasoningContent": {"reasoningText": {"text": "..."}}}]}},
    "usage": {"inputTokens": 1500, "outputTokens": 2048},
    "stopReason": "max_tokens",
}
#: An answer cut off part way through its JSON.
CUT_OFF = {
    "output": {"message": {"content": [{"text": '{"title": "Rollout'}]}},
    "usage": {"inputTokens": 1500, "outputTokens": 2048},
    "stopReason": "max_tokens",
}


@pytest.mark.parametrize("first", [REASONED_ONLY, CUT_OFF], ids=["no text", "cut off"])
def test_an_answer_cut_off_at_the_budget_is_asked_for_once_more_with_a_larger_one(fakes, first):
    bedrock = FakeBedrock()
    bedrock.first = [first]
    dynamo, _s3, _b, metering = fakes(bedrock=bedrock)
    assert index.handler(dict(EVENT), None) == {"status": "done"}
    budgets = [call["inferenceConfig"]["maxTokens"] for call in bedrock.calls]
    assert budgets == [index.MAX_OUTPUT_TOKENS, index.RETRY_OUTPUT_TOKENS]
    assert index.RETRY_OUTPUT_TOKENS > index.MAX_OUTPUT_TOKENS
    assert len(metering.records) == 2, "both calls are metered"
    assert "Whiteboard sketch" in _values(dynamo.updates[0]).values()


def test_a_second_cut_off_answer_marks_the_attachment_failed_after_two_calls(fakes):
    bedrock = FakeBedrock()
    bedrock.first = [REASONED_ONLY, REASONED_ONLY]
    dynamo, _s3, _b, metering = fakes(bedrock=bedrock)
    assert index.handler(dict(EVENT), None) == {"status": "failed", "reason": "no answer"}
    assert len(bedrock.calls) == 2 and len(metering.records) == 2
    assert "FAILED" in _values(dynamo.updates[0]).values()


def test_an_answer_that_was_not_cut_off_is_not_asked_for_again(fakes):
    dynamo, _s3, bedrock, metering = fakes(bedrock=FakeBedrock("no json here"))
    assert index.handler(dict(EVENT), None)["reason"] == "no answer"
    assert len(bedrock.calls) == 1 and len(metering.records) == 1


def test_a_reasoning_model_is_asked_for_a_low_effort_and_others_get_no_effort(fakes, monkeypatch):
    _d, _s, bedrock, _m = fakes()
    index.handler(dict(EVENT), None)
    assert bedrock.calls[0]["additionalModelRequestFields"] == {"output_config": {"effort": "low"}}

    _d, _s, bedrock, _m = fakes()
    monkeypatch.setattr(index, "model_id", lambda: "amazon.nova-lite-v1:0")
    index.handler(dict(EVENT), None)
    assert "additionalModelRequestFields" not in bedrock.calls[0]


def test_the_first_budget_leaves_room_for_reasoning():
    assert index.MAX_OUTPUT_TOKENS >= 2048


#: A text block with nothing in it, at the budget.
EMPTY_TEXT = {
    "output": {"message": {"content": [{"text": ""}]}},
    "usage": {"inputTokens": 1500, "outputTokens": 2048},
    "stopReason": "end_turn",
}


def test_an_empty_text_block_is_asked_for_once_more(fakes):
    bedrock = FakeBedrock()
    bedrock.first = [EMPTY_TEXT]
    _d, _s, _b, metering = fakes(bedrock=bedrock)
    assert index.handler(dict(EVENT), None) == {"status": "done"}
    assert len(bedrock.calls) == 2 and len(metering.records) == 2


def test_no_third_call_is_announced_or_made_after_the_retry(fakes, caplog):
    bedrock = FakeBedrock()
    bedrock.first = [EMPTY_TEXT, EMPTY_TEXT]
    fakes(bedrock=bedrock)
    with caplog.at_level("WARNING"):
        assert index.handler(dict(EVENT), None)["reason"] == "no answer"
    assert len(bedrock.calls) == 2
    assert sum("asking once more" in r.getMessage() for r in caplog.records) == 1


def test_the_model_calls_fit_the_functions_timeout():
    """Two calls, each of at most CALL_ATTEMPTS attempts of connect plus read, with room left."""
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    template = HERE.parents[2] / "deployment" / "lma-ai-stack.yaml"
    resources = yaml.load(template.read_text(), Loader=Loader)["Resources"]
    timeout = resources["ChatAttachmentSummarizerFunction"]["Properties"]["Timeout"]
    worst = 2 * index.CALL_ATTEMPTS * (index.CONNECT_TIMEOUT_SECONDS + index.READ_TIMEOUT_SECONDS)
    # The object read, the item writes and botocore's backoff need time too.
    assert worst <= timeout - 30, (worst, timeout)
    config = index.BEDROCK.meta.config
    assert config.read_timeout == index.READ_TIMEOUT_SECONDS
    assert config.connect_timeout == index.CONNECT_TIMEOUT_SECONDS
    assert config.retries["total_max_attempts"] == index.CALL_ATTEMPTS
