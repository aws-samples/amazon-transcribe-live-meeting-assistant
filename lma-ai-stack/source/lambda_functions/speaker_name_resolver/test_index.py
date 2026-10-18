# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the speaker-name resolver's plumbing.

The judgement is tested in test_pure.py. What is tested here is the behaviour a
caller depends on: that a user who may not edit a meeting is refused, that a
failing model call cannot stop a meeting from ending, that nothing is written
when nothing was inferred, and that the names are written through AppSync rather
than straight to the table (which is what makes an open meeting page re-label
without being reloaded).

No AWS: the module's clients are built at import time and replaced here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class StubLogger:
    """Powertools' Logger, minus Powertools -- it ships in a Lambda layer that
    is not on the path for a unit test."""

    def __init__(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass

    def inject_lambda_context(self, func=None, **kwargs):
        """Identity decorator, so the handler stays callable as written."""
        if func is None:
            return lambda inner: inner
        return func


# Each dotted name needs its own entry: `from a.b.c import d` imports a, then
# a.b, then a.b.c, and a stub for `a` alone does not satisfy the later steps.
sys.modules["aws_lambda_powertools"] = MagicMock(Logger=StubLogger)
sys.modules["aws_lambda_powertools.utilities"] = MagicMock()
sys.modules["aws_lambda_powertools.utilities.typing"] = MagicMock(LambdaContext=object)

TABLE = "lma-call-events"
APPSYNC_URL = "https://example.appsync-api.us-east-1.amazonaws.com/graphql"

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("EVENT_SOURCING_TABLE", TABLE)
os.environ.setdefault("APPSYNC_GRAPHQL_URL", APPSYNC_URL)
os.environ.setdefault("BEDROCK_MODEL_ID", "test.model")

HERE = Path(__file__).resolve().parent


def _load():
    """Import this directory's index.py under a name of its own.

    Every Lambda source directory in this tree has an ``index.py``, so a plain
    ``import index`` resolves to whichever directory comes first on sys.path.
    """
    sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location("speaker_name_resolver_index", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()

CALL_ID = "meeting-1"
OWNER = "owner@example.com"
SPK0 = "CALLER#Other Participant (spk_0)"
SPK1 = "CALLER#Other Participant (spk_1)"

MEETING_ROW = {
    "PK": {"S": f"c#{CALL_ID}"},
    "SK": {"S": f"c#{CALL_ID}"},
    "CallId": {"S": CALL_ID},
    "Owner": {"S": OWNER},
}


def segment_row(speaker, start, text, channel="CALLER", segment_id=None):
    return {
        "PK": {"S": f"trs#{CALL_ID}"},
        "SK": {"S": f"{start}#{channel}"},
        "SegmentId": {"S": segment_id or f"s{start}"},
        "Channel": {"S": channel},
        "Speaker": {"S": speaker},
        "Transcript": {"S": text},
        "StartTime": {"N": str(start)},
        "EndTime": {"N": str(start + 3)},
        "IsPartial": {"BOOL": False},
    }


SEGMENTS = [
    segment_row("Other Participant (spk_0)", 0, "Hi everyone, I'm Sarah Lee, I run the platform team"),
    segment_row("Other Participant (spk_1)", 5, "And I'm Priya Raman, I own data ingestion"),
]

MODEL_ANSWER = json.dumps(
    [
        {"label": SPK0, "name": "Sarah Lee", "basis": "self-introduction", "turn": 1, "quote": "I'm Sarah Lee"},
        {"label": SPK1, "name": "Priya Raman", "basis": "self-introduction", "turn": 2, "quote": "I'm Priya Raman"},
    ]
)


class FakeDynamo:
    """Stands in for the call-events table."""

    def __init__(self, meeting=None, segments=None, pages=None):
        self.meeting = MEETING_ROW if meeting is None else meeting
        self.segments = SEGMENTS if segments is None else segments
        self.pages = pages
        self.queries = []

    def get_item(self, **kwargs):  # noqa: D102
        return {"Item": self.meeting} if self.meeting else {}

    def query(self, **kwargs):  # noqa: D102
        self.queries.append(kwargs)
        rows = [dict(r) for r in self.segments]
        if self.pages is None:
            return {"Items": rows}
        start = 0
        if "ExclusiveStartKey" in kwargs:
            resume = kwargs["ExclusiveStartKey"]["SK"]["S"]
            start = next(i for i, r in enumerate(rows) if r["SK"]["S"] == resume) + 1
        page = rows[start : start + self.pages]
        out = {"Items": page}
        if start + self.pages < len(rows):
            out["LastEvaluatedKey"] = {"PK": page[-1]["PK"], "SK": page[-1]["SK"]}
        return out


class FakeBedrock:
    def __init__(self, answer=MODEL_ANSWER, error=None):
        self.answer = answer
        self.error = error
        self.prompts = []

    def converse(self, **kwargs):  # noqa: D102
        self.prompts.append(kwargs["messages"][0]["content"][0]["text"])
        if self.error:
            raise self.error
        return {"output": {"message": {"content": [{"text": self.answer}]}}}


def install(monkeypatch, meeting=None, segments=None, answer=MODEL_ANSWER, error=None, pages=None):
    ddb = FakeDynamo(meeting=meeting, segments=segments, pages=pages)
    bedrock = FakeBedrock(answer=answer, error=error)
    written = []
    monkeypatch.setattr(index, "dynamodb", ddb)
    monkeypatch.setattr(index, "bedrock", bedrock)
    monkeypatch.setattr(
        index, "write_identities", lambda call_id, identities: written.append((call_id, identities))
    )
    return ddb, bedrock, written


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


def test_self_introductions_are_named_and_written(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, written = install(monkeypatch)
    result = index.handler({"CallId": CALL_ID}, None)
    assert result == {"Success": True, "Message": "ok", "NamesApplied": 2}
    (call_id, identities) = written[0]
    assert call_id == CALL_ID
    assert identities["identities"][SPK0]["name"] == "Sarah Lee"
    assert identities["identities"][SPK0]["status"] == "confirmed"
    assert identities["identities"][SPK1]["name"] == "Priya Raman"


def test_the_prompt_carries_the_transcript_and_the_open_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, bedrock, _ = install(monkeypatch)
    index.handler({"CallId": CALL_ID}, None)
    prompt = bedrock.prompts[0]
    assert "I'm Sarah Lee" in prompt
    assert SPK0 in prompt


def test_segments_are_read_from_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long meeting spreads over several 1 MB pages; stopping at the first
    would hide the introductions if they were not on it."""
    many = [segment_row("Other Participant (spk_0)", i * 5, f"turn {i}") for i in range(6)]
    many[0] = segment_row("Other Participant (spk_0)", 0, "Hi, I'm Sarah Lee")
    ddb, _, _ = install(monkeypatch, segments=many, pages=2)
    index.handler({"CallId": CALL_ID}, None)
    assert len(ddb.queries) == 3


# --------------------------------------------------------------------------
# When nothing should happen
# --------------------------------------------------------------------------


def test_nothing_is_written_when_the_model_names_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass that infers nothing must not write, or it would publish an
    onUpdateCall to every client for no change."""
    _, _, written = install(monkeypatch, answer="[]")
    result = index.handler({"CallId": CALL_ID}, None)
    assert result["NamesApplied"] == 0
    assert written == []


def test_a_transcript_with_no_naming_language_does_not_call_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quiet = [
        segment_row("Other Participant (spk_0)", 0, "the numbers moved three percent"),
        segment_row("Other Participant (spk_1)", 5, "look at the second chart"),
    ]
    _, bedrock, written = install(monkeypatch, segments=quiet)
    result = index.handler({"CallId": CALL_ID}, None)
    assert bedrock.prompts == []
    assert written == []
    assert result["Message"] == "nothing to name"


def test_an_already_named_meeting_does_not_call_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    named = {
        **MEETING_ROW,
        "SpeakerIdentities": {
            "S": json.dumps(
                {
                    "identities": {
                        SPK0: {"name": "Sarah Lee", "status": "confirmed"},
                        SPK1: {"name": "Priya Raman", "status": "confirmed"},
                    }
                }
            )
        },
    }
    _, bedrock, _ = install(monkeypatch, meeting=named)
    index.handler({"CallId": CALL_ID}, None)
    assert bedrock.prompts == []


def test_a_meeting_that_does_not_exist_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, meeting={})
    result = index.handler({"CallId": CALL_ID}, None)
    assert result["Success"] is False
    assert "no such meeting" in result["Message"]


def test_an_event_with_no_call_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    with pytest.raises(ValueError):
        index.handler({}, None)


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------


def test_the_owner_may_run_a_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, written = install(monkeypatch)
    event = {"arguments": {"input": {"CallId": CALL_ID}}, "identity": {"username": OWNER}}
    assert index.handler(event, None)["Success"] is True
    assert written


def test_a_user_who_does_not_own_the_meeting_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Lambda resolver has no DynamoDB condition to lean on, so the rule the
    direct-write resolver enforces has to be applied here instead."""
    _, bedrock, written = install(monkeypatch)
    event = {
        "arguments": {"input": {"CallId": CALL_ID}},
        "identity": {"username": "stranger@example.com", "groups": []},
    }
    result = index.handler(event, None)
    assert result["Success"] is False
    assert result["Message"] == "not permitted"
    assert written == []
    assert bedrock.prompts == []


def test_an_admin_may_run_a_pass_on_any_meeting(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, written = install(monkeypatch)
    event = {
        "arguments": {"input": {"CallId": CALL_ID}},
        "identity": {"username": "admin@example.com", "groups": ["Admin"]},
    }
    assert index.handler(event, None)["Success"] is True
    assert written


def test_a_user_the_meeting_is_shared_with_may_run_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for shared_with in ({"L": [{"S": "friend@example.com"}]}, {"S": "a@b.com,friend@example.com"}):
        shared = {**MEETING_ROW, "SharedWith": shared_with}
        _, _, written = install(monkeypatch, meeting=shared)
        event = {
            "arguments": {"input": {"CallId": CALL_ID}},
            "identity": {"username": "friend@example.com", "groups": []},
        }
        assert index.handler(event, None)["Success"] is True, shared_with
        assert written


def test_a_backend_invoke_has_no_identity_and_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-of-meeting pass is the backend calling itself."""
    _, _, written = install(monkeypatch)
    assert index.handler({"CallId": CALL_ID}, None)["Success"] is True
    assert written


# --------------------------------------------------------------------------
# Failure containment
# --------------------------------------------------------------------------


def test_a_failing_model_call_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """This runs just before a meeting is closed out. A meeting must not fail to
    end because a model call did -- naming is an enhancement, ending is not.
    """
    _, _, written = install(monkeypatch, error=RuntimeError("bedrock unavailable"))
    result = index.handler({"CallId": CALL_ID}, None)
    assert result["Success"] is False
    assert "bedrock unavailable" in result["Message"]
    assert written == []


def test_a_failing_write_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)

    def explode(*_args, **_kwargs):
        raise RuntimeError("appsync rejected the write")

    monkeypatch.setattr(index, "write_identities", explode)
    result = index.handler({"CallId": CALL_ID}, None)
    assert result["Success"] is False
    assert "appsync" in result["Message"]


def test_a_model_answer_that_is_not_json_names_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, written = install(monkeypatch, answer="I could not work out who anyone is.")
    result = index.handler({"CallId": CALL_ID}, None)
    assert result["NamesApplied"] == 0
    assert written == []


def test_a_model_naming_a_label_that_does_not_exist_names_nobody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = json.dumps(
        [{"label": "CALLER#Ghost (spk_9)", "name": "Ghost", "basis": "self-introduction", "turn": 1}]
    )
    _, _, written = install(monkeypatch, answer=hostile)
    assert index.handler({"CallId": CALL_ID}, None)["NamesApplied"] == 0
    assert written == []


# --------------------------------------------------------------------------
# On-demand behaviour
# --------------------------------------------------------------------------


def test_the_button_overrides_the_shortcut_but_an_automatic_pass_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quiet = [
        segment_row("Other Participant (spk_0)", 0, "the numbers moved three percent"),
        segment_row("Other Participant (spk_1)", 5, "look at the second chart"),
    ]
    _, bedrock, _ = install(monkeypatch, segments=quiet, answer="[]")
    index.handler({"CallId": CALL_ID, "Force": True}, None)
    assert bedrock.prompts == [], "a backend invoke must not be able to force a model call"

    _, bedrock2, _ = install(monkeypatch, segments=quiet, answer="[]")
    event = {
        "arguments": {"input": {"CallId": CALL_ID, "Force": True}},
        "identity": {"username": OWNER},
    }
    index.handler(event, None)
    assert len(bedrock2.prompts) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
