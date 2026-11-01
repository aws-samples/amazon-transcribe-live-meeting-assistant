# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the agenda deriver's plumbing.

The judgement is tested in test_pure.py. What is tested here is what a caller
depends on: a user who may not edit the meeting is refused; the automatic pass
runs at most once per meeting and never over an agenda somebody set; nothing is
written when no agenda was stated; what is found is written through AppSync, so
open meeting pages update; and every model call is metered against the meeting.

No AWS: the module's clients are built at import time and replaced here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

TABLE = "lma-call-events"
APPSYNC_URL = "https://example.appsync-api.us-east-1.amazonaws.com/graphql"

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("EVENT_SOURCING_TABLE", TABLE)
os.environ.setdefault("APPSYNC_GRAPHQL_URL", APPSYNC_URL)
os.environ.setdefault("BEDROCK_MODEL_ID", "test.model")

HERE = Path(__file__).resolve().parent


def _load():
    """Import this directory's index.py under a name of its own (every Lambda
    directory here has an index.py)."""
    sys.path.insert(0, str(HERE))
    sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))
    spec = importlib.util.spec_from_file_location("agenda_deriver_index", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()

CALL_ID = "meeting-1"
OWNER = "owner@example.com"
EVIDENCE = "Today we have two things to get through: the budget and hiring."


def meeting_row(**extra):
    row = {
        "PK": {"S": f"c#{CALL_ID}"},
        "SK": {"S": f"c#{CALL_ID}"},
        "CallId": {"S": CALL_ID},
        "Owner": {"S": OWNER},
    }
    row.update({key: {"S": value} for key, value in extra.items()})
    return row


def segment_row(start, text):
    return {
        "PK": {"S": f"trs#{CALL_ID}"},
        "SK": {"S": f"{start}#AGENT"},
        "Channel": {"S": "AGENT"},
        "Speaker": {"S": "Ann"},
        "Transcript": {"S": text},
        "StartTime": {"N": str(start)},
        "EndTime": {"N": str(start + 3)},
        "IsPartial": {"BOOL": False},
    }


class FakeDynamo:
    def __init__(self, meeting, segments, claim_taken=False):
        self.meeting = meeting
        self.segments = segments
        self.claim_taken = claim_taken
        self.updates = []
        self.puts = []

    def get_item(self, **_kwargs):
        return {"Item": self.meeting} if self.meeting else {}

    def query(self, **_kwargs):
        return {"Items": self.segments}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        if self.claim_taken:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}},
                "UpdateItem",
            )
        return {}

    def put_item(self, **kwargs):
        self.puts.append(kwargs)
        return {}


class FakeBedrock:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def converse(self, **_kwargs):
        self.calls += 1
        return {
            "output": {"message": {"content": [{"text": self.text}]}},
            "usage": {"inputTokens": 100, "outputTokens": 20},
        }


@pytest.fixture
def world(monkeypatch):
    """Install fakes; returns a function that configures them for one test."""
    state = {}

    def setup(meeting=None, segments=None, model_text=None, claim_taken=False, appsync_errors=None):
        dynamo = FakeDynamo(
            meeting if meeting is not None else meeting_row(),
            segments if segments is not None else [segment_row(2, EVIDENCE)],
            claim_taken=claim_taken,
        )
        bedrock = FakeBedrock(
            model_text
            if model_text is not None
            else json.dumps(
                {"agenda_stated": True, "items": ["the budget", "hiring"], "evidence": EVIDENCE}
            )
        )
        posted = []

        class Response:
            @staticmethod
            def json():
                return {"errors": appsync_errors} if appsync_errors else {"data": {}}

        def post(url, data=None, headers=None, timeout=None):  # noqa: ARG001
            posted.append(json.loads(data))
            return Response()

        metered = []
        monkeypatch.setattr(index, "dynamodb", dynamo)
        monkeypatch.setattr(index, "bedrock", bedrock)
        monkeypatch.setattr(index.requests, "post", post)
        monkeypatch.setattr(index.METERING, "record", lambda *args, **kw: metered.append(args))
        monkeypatch.setattr(index.boto3, "Session", lambda: _FakeSession())
        monkeypatch.setattr(index, "SigV4Auth", lambda *a, **k: _NoAuth())
        state.update(dynamo=dynamo, bedrock=bedrock, posted=posted, metered=metered)
        return state

    return setup


class _FakeSession:
    region_name = "us-east-1"

    @staticmethod
    def get_credentials():
        return None


class _NoAuth:
    @staticmethod
    def add_auth(_request):
        return None


OWNER_IDENTITY = {"username": OWNER, "groups": []}


def press(identity=OWNER_IDENTITY):
    return index.handler({"arguments": {"input": {"CallId": CALL_ID}}, "identity": identity}, None)


def automatic():
    return index.handler({"CallId": CALL_ID, "Trigger": "auto"}, None)


# ── on demand ───────────────────────────────────────────────────────────────


def test_a_stated_agenda_is_written_through_appsync_as_derived(world):
    state = world()
    result = press()
    assert result == {
        "Success": True,
        "Message": "found a stated agenda of 2 items",
        "Derived": True,
    }
    (posted,) = state["posted"]
    assert "updateCallAgenda" in posted["query"]
    variables = posted["variables"]["input"]
    assert variables == {
        "CallId": CALL_ID,
        "Agenda": "the budget\nhiring",
        "AgendaSource": "derived",
        "AgendaUpdatedBy": OWNER,
        "OnlyIfUnset": False,
    }
    # The full field set, Owner and SharedWith included, so subscribers update.
    for field in ("Owner", "SharedWith", "Agenda", "Status", "CallSummaryText"):
        assert f"\n      {field}\n" in posted["query"]


def test_every_model_call_is_metered_against_the_meeting(world):
    state = world()
    press()
    (record,) = state["metered"]
    assert record[0] == CALL_ID
    assert record[1] == "AgendaDerivation"


def test_nothing_is_written_when_no_agenda_was_stated(world):
    state = world(model_text=json.dumps({"agenda_stated": False, "items": [], "evidence": ""}))
    result = press()
    assert result == {
        "Success": True,
        "Message": "no agenda was stated in the meeting",
        "Derived": False,
    }
    assert state["posted"] == []
    assert len(state["metered"]) == 1


def test_an_invented_agenda_is_not_written(world):
    state = world(
        model_text=json.dumps(
            {"agenda_stated": True, "items": ["Budget"], "evidence": "Our agenda: budget review."}
        )
    )
    assert press()["Derived"] is False
    assert state["posted"] == []


def test_a_user_without_access_is_refused_before_any_model_call(world):
    state = world()
    result = press({"username": "stranger@example.com", "groups": []})
    assert result == {"Success": False, "Message": "not permitted", "Derived": False}
    assert state["bedrock"].calls == 0


def test_an_admin_and_a_shared_user_may_derive(world):
    world(meeting=meeting_row(SharedWith="a@example.com, b@example.com"))
    assert press({"username": "b@example.com", "groups": []})["Success"] is True
    assert press({"username": "x@example.com", "groups": ["Admin"]})["Success"] is True


def test_an_empty_transcript_costs_nothing(world):
    state = world(segments=[])
    assert press()["Message"] == "nothing has been said in the meeting yet"
    assert state["bedrock"].calls == 0


def test_a_failure_is_reported_as_a_message_not_an_error(world, monkeypatch):
    world()

    def boom(**_kwargs):
        raise RuntimeError("bedrock unavailable")

    monkeypatch.setattr(index.bedrock, "converse", boom)
    assert press() == {
        "Success": False,
        "Message": "could not derive an agenda just now",
        "Derived": False,
    }


# ── automatic ───────────────────────────────────────────────────────────────


def test_the_automatic_pass_claims_the_meeting_and_writes_only_if_unset(world):
    state = world()
    assert automatic()["Derived"] is True
    (claim,) = state["dynamo"].updates
    assert claim["ExpressionAttributeNames"]["#claim"] == index.CLAIM_ATTRIBUTE
    assert "attribute_not_exists(#claim)" in claim["ConditionExpression"]
    assert "attribute_not_exists(#agenda) OR #agenda = :empty" in claim["ConditionExpression"]
    variables = state["posted"][0]["variables"]["input"]
    assert variables["OnlyIfUnset"] is True
    assert variables["AgendaUpdatedBy"] == index.AUTOMATIC_ATTRIBUTION


def test_the_automatic_pass_runs_at_most_once(world):
    state = world(claim_taken=True)
    assert automatic() == {"Success": True, "Message": "already attempted", "Derived": False}
    assert state["bedrock"].calls == 0


def test_the_automatic_pass_skips_a_meeting_that_has_an_agenda(world):
    state = world(meeting=meeting_row(Agenda="Mine"))
    assert automatic()["Message"] == "the meeting already has an agenda"
    assert state["dynamo"].updates == []
    assert state["bedrock"].calls == 0


def test_an_agenda_set_while_the_automatic_pass_ran_is_kept(world):
    world(appsync_errors=[{"message": "item put condition failure"}])
    assert automatic() == {
        "Success": True,
        "Message": "an agenda was set while this ran",
        "Derived": False,
    }


def test_only_the_backend_can_ask_for_the_automatic_pass(world):
    """A browser caller saying Trigger=auto still gets the on-demand pass."""
    state = world()
    index.handler(
        {
            "arguments": {"input": {"CallId": CALL_ID}},
            "identity": OWNER_IDENTITY,
            "Trigger": "auto",
        },
        None,
    )
    assert state["dynamo"].updates == []
    assert state["posted"][0]["variables"]["input"]["OnlyIfUnset"] is False


def test_the_write_selects_every_field_the_subscription_does():
    subscription = (
        HERE.parents[1] / "ui" / "src" / "graphql" / "queries" / "onUpdateCall.js"
    ).read_text(encoding="utf-8")
    selected = {
        line.strip()
        for line in subscription.splitlines()
        if line.strip() and line.strip()[0].isupper()
    }
    assert sorted(field for field in selected if field not in index._CALL_FIELDS) == []
