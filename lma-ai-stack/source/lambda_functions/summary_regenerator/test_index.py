# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for on-demand summary regeneration.

What a caller depends on, and what is tested here: that a user who may not edit
a meeting is refused, that a failing run leaves the stored summary standing,
that the run goes through the configured summary function with no prompt
override (which is what refreshes the Knowledge Base snapshot), and that the
result is written through AppSync rather than straight to the table -- which is
what makes an open meeting page show the new summary without being reloaded.

No AWS: the module's clients are built at import time and replaced here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from botocore.credentials import Credentials

TABLE = "lma-call-events"
APPSYNC_URL = "https://example.appsync-api.us-east-1.amazonaws.com/graphql"
SUMMARY_ARN = "arn:aws:lambda:us-east-1:123456789012:function:test-BedrockSummaryLambda"
SELF_ARN = "arn:aws:lambda:us-east-1:123456789012:function:test-SummaryRegenerator"

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("EVENT_SOURCING_TABLE", TABLE)
os.environ.setdefault("APPSYNC_GRAPHQL_URL", APPSYNC_URL)
os.environ.setdefault("TRANSCRIPT_SUMMARY_FUNCTION_ARN", SUMMARY_ARN)

HERE = Path(__file__).resolve().parent


def _load():
    """Import this directory's index.py under a name of its own.

    Several Lambda source directories in this tree have an ``index.py``, so a
    plain ``import index`` resolves to whichever directory comes first on
    sys.path.
    """
    sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location("summary_regenerator_index", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()

# Captured before any test replaces it, for the one test that wants the real
# write under a stubbed transport rather than the stub install() puts in.
REAL_WRITE_SUMMARY = index.write_summary

CALL_ID = "meeting-1"
OWNER = "owner@example.com"
NEW_SUMMARY = '{"Summary": "Sarah Lee walked through the migration plan."}'

MEETING_ROW = {
    "PK": {"S": f"c#{CALL_ID}"},
    "SK": {"S": f"c#{CALL_ID}"},
    "CallId": {"S": CALL_ID},
    "Owner": {"S": OWNER},
}


class FakeContext:
    invoked_function_arn = SELF_ARN


class FakeDynamo:
    def __init__(self, meeting=None, then=None):
        self.meeting = MEETING_ROW if meeting is None else meeting
        # What a read after the first one returns, for the case where the
        # meeting changes while a run is going. None means it does not change.
        self.then = then
        self.reads = 0

    def get_item(self, **kwargs):  # noqa: D102
        self.reads += 1
        row = self.then if self.then is not None and self.reads > 1 else self.meeting
        return {"Item": row} if row else {}


class FakeLambda:
    """Stands in for the invoke of the configured summary function."""

    def __init__(self, summary=NEW_SUMMARY, error=None, body=None):
        self.summary = summary
        self.error = error
        self.body = body
        self.payloads = []

    def invoke(self, **kwargs):  # noqa: D102
        self.payloads.append(json.loads(kwargs["Payload"]))
        if self.error:
            raise self.error
        body = self.body if self.body is not None else {"summary": self.summary}

        class _Payload:
            @staticmethod
            def read():
                return json.dumps(body).encode("utf-8")

        return {"Payload": _Payload()}


class FakeStarter:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def invoke(self, **kwargs):  # noqa: D102
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"StatusCode": 202}


def install(
    monkeypatch, meeting=None, summary=NEW_SUMMARY, error=None, body=None, starter=None, then=None
):
    ddb = FakeDynamo(meeting=meeting, then=then)
    fake_lambda = FakeLambda(summary=summary, error=error, body=body)
    fake_starter = starter or FakeStarter()
    written = []
    monkeypatch.setattr(index, "dynamodb", ddb)
    monkeypatch.setattr(index, "lambda_client", fake_lambda)
    monkeypatch.setattr(index, "starter_client", fake_starter)
    monkeypatch.setattr(
        index,
        "write_summary",
        lambda call_id, text=None, state=None: written.append(
            {"call_id": call_id, "text": text, "state": state}
        ),
    )
    return ddb, fake_lambda, fake_starter, written


def row_with_state(state, **extra):
    return {**MEETING_ROW, "CallSummaryState": {"S": json.dumps(state)}, **extra}


def start_event(identity=None, call_id=CALL_ID):
    return {"arguments": {"input": {"CallId": call_id}}, "identity": identity}


RUN_EVENT = {"CallId": CALL_ID, "Trigger": "regenerate"}


# ── access ──────────────────────────────────────────────────────────────────


def test_the_owner_may_regenerate(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, starter, written = install(monkeypatch)
    result = index.handler(start_event({"username": OWNER}), FakeContext())
    assert result["Success"] is True
    assert starter.calls
    assert written


def test_a_user_who_does_not_own_the_meeting_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Lambda resolver has no DynamoDB condition to lean on, so the rule the
    direct summary write enforces has to be applied here instead."""
    _, fake_lambda, starter, written = install(monkeypatch)
    result = index.handler(
        start_event({"username": "stranger@example.com", "groups": []}), FakeContext()
    )
    assert result["Success"] is False
    assert result["Message"] == "not permitted"
    assert starter.calls == []
    assert fake_lambda.payloads == []
    assert written == []


def test_an_admin_may_regenerate_any_meeting(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, starter, _ = install(monkeypatch)
    result = index.handler(
        start_event({"username": "admin@example.com", "groups": ["Admin"]}), FakeContext()
    )
    assert result["Success"] is True
    assert starter.calls


def test_a_user_the_meeting_is_shared_with_may_regenerate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both shapes SharedWith is stored in: a DynamoDB List from shareCall, and
    the older comma-separated String."""
    for shared_with in ({"L": [{"S": "friend@example.com"}]}, {"S": "a@b.com,friend@example.com"}):
        _, _, starter, _ = install(monkeypatch, meeting={**MEETING_ROW, "SharedWith": shared_with})
        result = index.handler(
            start_event({"username": "friend@example.com", "groups": []}), FakeContext()
        )
        assert result["Success"] is True, shared_with
        assert starter.calls


def test_a_user_a_different_meeting_is_shared_with_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, starter, written = install(
        monkeypatch,
        meeting={**MEETING_ROW, "SharedWith": {"L": [{"S": "friend@example.com"}]}},
    )
    result = index.handler(
        start_event({"username": "stranger@example.com", "groups": []}), FakeContext()
    )
    assert result["Success"] is False
    assert result["Message"] == "not permitted"
    assert starter.calls == []
    assert written == []


def test_an_identity_with_no_username_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An identity dict is present for every AppSync caller. One without a
    username is not the backend -- the backend's own leg sends no identity at
    all -- so it must not inherit the backend's access."""
    _, _, starter, written = install(monkeypatch)
    result = index.handler(start_event({"accountId": "123456789012"}), FakeContext())
    assert result["Success"] is False
    assert result["Message"] == "not permitted"
    assert starter.calls == []
    assert written == []


def test_an_identity_with_no_username_is_refused_on_a_meeting_with_no_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A meeting row carries no Owner attribute until somebody owns it, and an
    absent attribute reads as None. Access is a match between two names, so a
    caller with no name matches nothing -- including a row with no owner."""
    ownerless = {key: value for key, value in MEETING_ROW.items() if key != "Owner"}
    _, _, starter, written = install(monkeypatch, meeting=ownerless)
    result = index.handler(start_event({"accountId": "123456789012"}), FakeContext())
    assert result["Success"] is False
    assert result["Message"] == "not permitted"
    assert starter.calls == []
    assert written == []


# Every shape `SharedWith` is stored in, against one caller, with the answer
# each of the two write paths gives for it.
#
# `by_condition` is what `contains(#SharedWith, :caller)` evaluates to in the
# DynamoDB condition on `updateCallSummaryText`; `by_lambda` is what
# `caller_may_edit` returns. They agree on the List form, which is what
# `shareCall` writes and so the shape any meeting shared through this product
# has. They differ on one case -- a caller whose name is a substring of an entry
# in the legacy comma-separated String -- because a condition expression cannot
# split a string and this path can. Written down because the templates, the
# docstrings and the docs all describe the two paths as applying one rule, and
# this table is the extent to which that is true.
SHARED_WITH_CASES = [
    # (stored SharedWith, caller, by_condition, by_lambda)
    ({"L": [{"S": "friend@example.com"}]}, "friend@example.com", True, True),
    ({"L": [{"S": "friend@example.com"}]}, "friend@example.co", False, False),
    ({"L": [{"S": "a@b.com"}, {"S": "friend@example.com"}]}, "friend@example.com", True, True),
    ({"S": "friend@example.com"}, "friend@example.com", True, True),
    ({"S": "a@b.com,friend@example.com"}, "friend@example.com", True, True),
    ({"S": "a@b.com, friend@example.com"}, "friend@example.com", True, True),
    ({"S": "friend@example.com,other@example.com"}, "friend@example.co", True, False),
]


@pytest.mark.parametrize("stored,caller,by_condition,by_lambda", SHARED_WITH_CASES)
def test_the_share_list_is_read_entry_by_entry(
    monkeypatch: pytest.MonkeyPatch,
    stored: dict,
    caller: str,
    by_condition: bool,
    by_lambda: bool,
) -> None:
    """The Python half of the table above, through the handler.

    The `by_condition` column is not executed: a DynamoDB condition expression
    cannot be evaluated locally, so it records what the template's `contains`
    means for each stored shape. The assertion is on this path, which is the one
    with no condition behind it.
    """
    _, _, starter, _ = install(monkeypatch, meeting={**MEETING_ROW, "SharedWith": stored})
    result = index.handler(start_event({"username": caller, "groups": []}), FakeContext())
    assert result["Success"] is by_lambda, (stored, caller)
    assert bool(starter.calls) is by_lambda
    # The one disagreement is the documented one, and only there.
    if by_condition != by_lambda:
        assert "S" in stored and "," in stored["S"], (stored, caller)


def test_the_second_leg_has_no_identity_and_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This function invoking itself is the backend calling itself."""
    _, fake_lambda, _, written = install(monkeypatch)
    assert index.handler(RUN_EVENT, FakeContext())["ok"] is True
    assert fake_lambda.payloads == [{"CallId": CALL_ID}]
    assert written[-1]["text"] == NEW_SUMMARY


def test_a_meeting_that_does_not_exist_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, starter, written = install(monkeypatch, meeting={})
    result = index.handler(start_event({"username": OWNER}), FakeContext())
    assert result["Success"] is False
    assert result["Message"] == "no such meeting"
    assert starter.calls == []
    assert written == []


def test_a_request_with_no_call_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    with pytest.raises(ValueError):
        index.handler({"arguments": {"input": {}}, "identity": {"username": OWNER}}, FakeContext())


# ── starting a run ──────────────────────────────────────────────────────────


def test_starting_a_run_queues_the_work_and_marks_the_meeting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, fake_lambda, starter, written = install(monkeypatch)
    index.handler(start_event({"username": OWNER}), FakeContext())
    # The first leg must not summarise: AppSync gives a resolver 30 seconds and
    # a model call per prompt template routinely outlasts that.
    assert fake_lambda.payloads == []
    assert starter.calls[0]["FunctionName"] == SELF_ARN
    assert starter.calls[0]["InvocationType"] == "Event"
    assert json.loads(starter.calls[0]["Payload"]) == {
        "CallId": CALL_ID,
        "Trigger": "regenerate",
    }
    assert written[0]["text"] is None
    assert written[0]["state"]["status"] == index.STATUS_RUNNING
    assert written[0]["state"]["startedAt"]


def test_the_work_is_queued_before_the_meeting_is_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marking first and then failing to queue would leave the meeting looking
    busy, and the button disabled in every open page, until the mark went
    stale."""
    starter = FakeStarter(error=RuntimeError("throttled"))
    _, _, _, written = install(monkeypatch, starter=starter)
    result = index.handler(start_event({"username": OWNER}), FakeContext())
    assert result["Success"] is False
    assert written == []


def test_a_hand_edited_summary_still_reads_as_edited_while_a_run_is_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So that a run which then fails does not quietly relabel somebody's own
    text as generated."""
    _, _, _, written = install(monkeypatch, meeting=row_with_state({"source": index.SOURCE_MANUAL}))
    index.handler(start_event({"username": OWNER}), FakeContext())
    assert written[0]["state"]["source"] == index.SOURCE_MANUAL


def test_a_second_request_while_one_is_running_is_declined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = datetime.now(timezone.utc).isoformat()
    _, _, starter, written = install(
        monkeypatch,
        meeting=row_with_state({"status": index.STATUS_RUNNING, "startedAt": started}),
    )
    result = index.handler(start_event({"username": OWNER}), FakeContext())
    assert result["Success"] is False
    assert result["Message"] == "a summary is already being regenerated"
    assert starter.calls == []
    assert written == []


def test_a_run_whose_mark_went_stale_does_not_block_a_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run whose Lambda was killed would otherwise disable the button for
    ever."""
    started = (
        datetime.now(timezone.utc) - timedelta(seconds=index.STALE_RUN_SECONDS + 60)
    ).isoformat()
    _, _, starter, _ = install(
        monkeypatch,
        meeting=row_with_state({"status": index.STATUS_RUNNING, "startedAt": started}),
    )
    assert index.handler(start_event({"username": OWNER}), FakeContext())["Success"] is True
    assert starter.calls


def _ahead(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


@pytest.mark.parametrize(
    "stamp",
    [
        None,
        "",
        "yesterday",
        # Dated further ahead than the clock-skew allowance: nothing here wrote
        # it, and the window could not be read its way out of.
        _ahead(index.CLOCK_SKEW_ALLOWANCE_SECONDS + 600),
        _ahead(86400),
    ],
)
def test_a_mark_with_an_unusable_stamp_does_not_block_a_new_run(
    monkeypatch: pytest.MonkeyPatch, stamp: str | None
) -> None:
    """The in-flight window is measured from the stamp, so a running mark whose
    stamp falls outside what this can read would block the meeting with nothing
    able to clear it. `updateCallSummaryText` stores whatever state string it is
    given, so a reader cannot take the shape of this field for granted."""
    state = {"status": index.STATUS_RUNNING}
    if stamp is not None:
        state["startedAt"] = stamp
    _, _, starter, _ = install(monkeypatch, meeting=row_with_state(state))
    assert index.handler(start_event({"username": OWNER}), FakeContext())["Success"] is True
    assert starter.calls


def test_a_stamp_inside_the_clock_skew_allowance_is_still_believed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stamp a little ahead of this clock is two clocks disagreeing, not a
    value to throw away: a run really is in flight."""
    _, _, starter, written = install(
        monkeypatch,
        meeting=row_with_state(
            {
                "status": index.STATUS_RUNNING,
                "startedAt": _ahead(index.CLOCK_SKEW_ALLOWANCE_SECONDS - 10),
            }
        ),
    )
    result = index.handler(start_event({"username": OWNER}), FakeContext())
    assert result["Success"] is False
    assert result["Message"] == "a summary is already being regenerated"
    assert starter.calls == []
    assert written == []


def test_a_failed_previous_run_does_not_block_a_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, starter, _ = install(monkeypatch, meeting=row_with_state({"status": index.STATUS_FAILED}))
    assert index.handler(start_event({"username": OWNER}), FakeContext())["Success"] is True
    assert starter.calls


def test_a_deployment_with_no_summariser_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _, _, starter, written = install(monkeypatch)
    monkeypatch.setattr(index, "TRANSCRIPT_SUMMARY_FUNCTION_ARN", "")
    result = index.handler(start_event({"username": OWNER}), FakeContext())
    assert result["Success"] is False
    assert "not configured" in result["Message"]
    assert starter.calls == []
    assert written == []


def test_an_unparseable_stored_state_is_treated_as_nothing_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, starter, _ = install(
        monkeypatch, meeting={**MEETING_ROW, "CallSummaryState": {"S": "not json"}}
    )
    assert index.handler(start_event({"username": OWNER}), FakeContext())["Success"] is True
    assert starter.calls


# ── doing the run ───────────────────────────────────────────────────────────


def test_the_run_passes_no_prompt_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A prompt override would bypass the stored prompt templates and, in the
    Bedrock summariser, skip the snapshot of the transcript and summary written
    to S3 for the Bedrock Knowledge Base. Refreshing that snapshot is one of the
    reasons to regenerate."""
    _, fake_lambda, _, _ = install(monkeypatch)
    index.handler(RUN_EVENT, FakeContext())
    assert fake_lambda.payloads == [{"CallId": CALL_ID}]
    assert "Prompt" not in fake_lambda.payloads[0]


def test_the_run_waits_for_the_summariser(monkeypatch: pytest.MonkeyPatch) -> None:
    _, fake_lambda, _, _ = install(monkeypatch)
    index.handler(RUN_EVENT, FakeContext())
    assert fake_lambda.payloads


def test_a_successful_run_stores_the_text_and_marks_it_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, written = install(monkeypatch)
    index.handler(RUN_EVENT, FakeContext())
    assert written[-1]["text"] == NEW_SUMMARY
    assert written[-1]["state"]["source"] == index.SOURCE_GENERATED
    assert written[-1]["state"]["status"] == index.STATUS_IDLE


def test_a_run_the_meeting_outlived_is_discarded_rather_than_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run started while a meeting was going summarised part of a transcript
    the end-of-meeting summary covers in full, so its result is dropped: the two
    writes are unordered, and the fuller summary is the one wanted."""
    live = {**MEETING_ROW, "Status": {"S": "STARTED"}}
    ended = {**MEETING_ROW, "Status": {"S": "ENDED"}}
    _, _, _, written = install(monkeypatch, meeting=live, then=ended)
    result = index.handler(RUN_EVENT, FakeContext())
    assert result == {"ok": False, "reason": "superseded"}
    # The state is written, so the panel stops showing a run in progress, but no
    # text: the end-of-meeting summary is what the meeting keeps.
    assert written[-1]["text"] is None
    assert written[-1]["state"]["status"] == index.STATUS_IDLE
    assert all(entry["text"] is None for entry in written)


def test_a_run_on_a_meeting_that_was_already_over_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordinary case: regenerating a finished meeting. Nothing follows it
    from the pipeline, so there is nothing for it to lose a race with."""
    ended = {**MEETING_ROW, "Status": {"S": "ENDED"}}
    _, _, _, written = install(monkeypatch, meeting=ended)
    assert index.handler(RUN_EVENT, FakeContext())["ok"] is True
    assert written[-1]["text"] == NEW_SUMMARY


def test_a_run_that_finishes_while_the_meeting_is_still_going_is_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regenerating during a meeting is one of the documented reasons to press
    the button, so a run that outlives nothing is stored as usual."""
    live = {**MEETING_ROW, "Status": {"S": "STARTED"}}
    _, _, _, written = install(monkeypatch, meeting=live)
    assert index.handler(RUN_EVENT, FakeContext())["ok"] is True
    assert written[-1]["text"] == NEW_SUMMARY


def test_a_successful_run_clears_a_manual_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regenerating replaces the text, so the meeting is no longer hand-edited."""
    _, _, _, written = install(monkeypatch, meeting=row_with_state({"source": index.SOURCE_MANUAL}))
    index.handler(RUN_EVENT, FakeContext())
    assert written[-1]["state"]["source"] == index.SOURCE_GENERATED


@pytest.mark.parametrize(
    "summary",
    ["An error occurred.", "an error occurred", "No summary available", "", "   ", None, 7],
)
def test_a_run_that_produced_nothing_leaves_the_stored_summary_alone(
    monkeypatch: pytest.MonkeyPatch, summary
) -> None:
    """A summary that was there and is now "An error occurred." is worse than a
    stale one. The configured summariser never raises -- it catches everything
    and returns a fixed string -- so that string is the only signal there is."""
    _, _, _, written = install(monkeypatch, summary=summary)
    assert index.handler(RUN_EVENT, FakeContext())["ok"] is False
    assert len(written) == 1
    assert written[0]["text"] is None
    assert written[0]["state"]["status"] == index.STATUS_FAILED
    assert written[0]["state"]["message"]


def test_a_summariser_that_raises_leaves_the_stored_summary_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, written = install(monkeypatch, error=RuntimeError("timed out"))
    assert index.handler(RUN_EVENT, FakeContext())["ok"] is False
    assert written[0]["text"] is None
    assert written[0]["state"]["status"] == index.STATUS_FAILED


def test_a_summariser_returning_something_that_is_not_an_object_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, written = install(monkeypatch, body=["not", "an", "object"])
    assert index.handler(RUN_EVENT, FakeContext())["ok"] is False
    assert written[0]["text"] is None


def test_a_failed_run_keeps_a_manual_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    """The text that survived the failure is still the text somebody wrote."""
    _, _, _, written = install(
        monkeypatch,
        meeting=row_with_state({"source": index.SOURCE_MANUAL}),
        summary="An error occurred.",
    )
    index.handler(RUN_EVENT, FakeContext())
    assert written[0]["state"]["source"] == index.SOURCE_MANUAL


# ── the write goes through AppSync ──────────────────────────────────────────
#
# These exercise the real SigV4 signing, so they need credentials. Static ones
# are injected here rather than left to botocore's search of the environment:
# that search succeeds on a developer machine that happens to be logged in and
# raises NoCredentialsError on a build runner, so a test relying on it is
# testing the developer's shell. `make test-lambdas` runs with no AWS and no
# network, which is the contract these have to meet.


class StaticCredentialSession:
    """A boto3 Session stand-in with fixed credentials and no config to read."""

    region_name = "us-east-1"

    @staticmethod
    def get_credentials():
        return Credentials(
            access_key="test-access-key-id",
            secret_key="test-secret-access-key",
            token="test-session-token",
        )


@pytest.fixture(autouse=True)
def static_signing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sign every write in this module with known credentials.

    Autouse so a write reached from any test in the file -- not only the ones
    below -- is signed deterministically.
    """
    monkeypatch.setattr(index, "signing_session", StaticCredentialSession())
    # The backoff is real time, and these tests drive it to its limit.
    monkeypatch.setattr(index, "APPSYNC_WRITE_BACKOFF_SECONDS", 0)


def test_signing_needs_no_ambient_credentials() -> None:
    """The guard on the fixture above.

    If the module ever resolves credentials somewhere other than
    `signing_session`, the fixture stops covering it and the tests here go back
    to depending on whichever credentials the process happens to have.
    """
    source = (Path(__file__).resolve().parent / "index.py").read_text(encoding="utf-8")
    assert source.count("boto3.Session()") == 1
    assert "signing_session = boto3.Session()" in source
    assert "signing_session.get_credentials()" in source


def test_the_result_is_written_through_appsync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing to DynamoDB directly would skip the onUpdateCall publish, and
    every open meeting page would keep showing the old summary until it was
    reloaded."""
    posted = {}

    class FakeResponse:
        @staticmethod
        def json():
            return {"data": {"updateCallSummaryText": {"CallId": CALL_ID}}}

    def fake_post(url, data=None, headers=None, timeout=None):  # noqa: D103
        posted["url"] = url
        posted["body"] = json.loads(data)
        return FakeResponse()

    monkeypatch.setattr(index.requests, "post", fake_post)
    index.write_summary(CALL_ID, text="text", state={"status": index.STATUS_IDLE})

    assert posted["url"] == APPSYNC_URL
    assert "updateCallSummaryText" in posted["body"]["query"]
    assert posted["body"]["variables"]["input"]["CallSummaryText"] == "text"
    assert json.loads(posted["body"]["variables"]["input"]["CallSummaryState"]) == {
        "status": index.STATUS_IDLE
    }


def test_a_state_only_write_sends_no_summary_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending an empty string instead would blank the stored summary, which is
    exactly what a failed run must not do."""
    posted = {}

    class FakeResponse:
        @staticmethod
        def json():
            return {"data": {}}

    monkeypatch.setattr(
        index.requests,
        "post",
        lambda url, data=None, headers=None, timeout=None: (
            posted.update(body=json.loads(data)) or FakeResponse()
        ),
    )
    index.write_summary(CALL_ID, state={"status": index.STATUS_FAILED})
    assert "CallSummaryText" not in posted["body"]["variables"]["input"]


def test_the_write_selects_every_field_the_subscription_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AppSync publishes a mutation's own response to onUpdateCall's
    subscribers, and a field the subscription selects that the mutation did not
    arrives as null -- so a narrow payload would blank the meeting's status,
    timestamps and names in every open page."""
    subscription = (
        Path(__file__).resolve().parents[2]
        / "ui"
        / "src"
        / "graphql"
        / "queries"
        / "onUpdateCall.js"
    ).read_text(encoding="utf-8")
    selected = {
        line.strip()
        for line in subscription.splitlines()
        if line.strip() and line.strip()[0].isupper()
    }
    missing = sorted(field for field in selected if field not in index._CALL_FIELDS)
    assert missing == []


def test_an_appsync_error_is_raised_rather_than_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        @staticmethod
        def json():
            return {"errors": [{"message": "item put condition failure"}]}

    monkeypatch.setattr(index.requests, "post", lambda *args, **kwargs: FakeResponse())
    with pytest.raises(RuntimeError):
        index.write_summary(CALL_ID, text="text")


def test_a_refused_write_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused write would be refused the same way again; only an unreachable
    endpoint is worth another attempt."""
    attempts = []

    class FakeResponse:
        @staticmethod
        def json():
            return {"errors": [{"message": "item put condition failure"}]}

    def fake_post(*args, **kwargs):
        attempts.append(1)
        return FakeResponse()

    monkeypatch.setattr(index.requests, "post", fake_post)
    with pytest.raises(RuntimeError):
        index.write_summary(CALL_ID, text="text")
    assert len(attempts) == 1


def test_an_unreachable_endpoint_is_retried_before_the_summary_is_given_up_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retried here rather than by letting the asynchronous invoke fail, which
    would re-run the whole summarisation for an answer already in hand."""
    attempts = []

    class FakeResponse:
        @staticmethod
        def json():
            return {"data": {}}

    def fake_post(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < index.APPSYNC_WRITE_ATTEMPTS:
            raise OSError("connection reset")
        return FakeResponse()

    monkeypatch.setattr(index.requests, "post", fake_post)
    index.write_summary(CALL_ID, text="text")
    assert len(attempts) == index.APPSYNC_WRITE_ATTEMPTS


def test_every_attempt_is_signed_afresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry seconds after the first attempt must not carry the first
    attempt's signature, which SigV4 dates."""
    signatures = []

    class FakeResponse:
        @staticmethod
        def json():
            return {"data": {}}

    def fake_post(url, data=None, headers=None, timeout=None):
        signatures.append(headers["Authorization"])
        if len(signatures) < index.APPSYNC_WRITE_ATTEMPTS:
            raise OSError("connection reset")
        return FakeResponse()

    monkeypatch.setattr(index.requests, "post", fake_post)
    index.write_summary(CALL_ID, text="text")
    assert len(signatures) == index.APPSYNC_WRITE_ATTEMPTS
    # Signed, and signed with the credentials injected here rather than
    # whatever the process could find.
    assert all("test-access-key-id" in signature for signature in signatures)


def test_each_attempt_waits_only_as_long_as_the_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The number the worst-case budget is calculated from, so it has to be the
    number actually passed to the request."""
    timeouts = []

    def fake_post(url, data=None, headers=None, timeout=None):
        timeouts.append(timeout)
        raise OSError("connection reset")

    monkeypatch.setattr(index.requests, "post", fake_post)
    with pytest.raises(RuntimeError):
        index.write_summary(CALL_ID, text="text")
    assert timeouts == [index.APPSYNC_WRITE_TIMEOUT_SECONDS] * index.APPSYNC_WRITE_ATTEMPTS


def test_the_backoff_doubles_and_does_not_follow_the_last_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Waiting after the final attempt would spend the budget on nothing."""
    slept = []

    def never_answers(*args, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(index, "APPSYNC_WRITE_BACKOFF_SECONDS", 1)
    monkeypatch.setattr(index.time, "sleep", slept.append)
    monkeypatch.setattr(index.requests, "post", never_answers)
    with pytest.raises(RuntimeError):
        index.write_summary(CALL_ID, text="text")
    assert slept == [1, 2]


def test_an_endpoint_that_never_answers_eventually_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = []

    def fake_post(*args, **kwargs):
        attempts.append(1)
        raise OSError("connection reset")

    monkeypatch.setattr(index.requests, "post", fake_post)
    with pytest.raises(RuntimeError) as raised:
        index.write_summary(CALL_ID, text="text")
    assert len(attempts) == index.APPSYNC_WRITE_ATTEMPTS
    # The last transport error is carried out, so the log says what went wrong
    # rather than only that something did.
    assert "connection reset" in str(raised.value)


def test_a_failed_run_still_records_the_failure_when_the_first_write_is_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole budget, end to end: a summariser that never answers and a
    first write attempt that has to be retried still leaves the meeting with a
    recorded failure rather than a run that vanished."""
    posted = []

    class FakeResponse:
        @staticmethod
        def json():
            return {"data": {}}

    def fake_post(url, data=None, headers=None, timeout=None):
        posted.append(json.loads(data))
        if len(posted) < 2:
            raise OSError("connection reset")
        return FakeResponse()

    install(monkeypatch, error=RuntimeError("timed out waiting for the summariser"))
    # install() stubs write_summary; this test wants the real one, captured at
    # import before anything replaced it.
    monkeypatch.setattr(index, "write_summary", REAL_WRITE_SUMMARY)
    monkeypatch.setattr(index.requests, "post", fake_post)
    assert index.handler(RUN_EVENT, FakeContext())["ok"] is False
    assert len(posted) == 2
    state = json.loads(posted[-1]["variables"]["input"]["CallSummaryState"])
    assert state["status"] == index.STATUS_FAILED
    assert "CallSummaryText" not in posted[-1]["variables"]["input"]


def test_the_run_is_queued_with_a_client_that_cannot_duplicate_it() -> None:
    """A socket timeout on an Event invoke that did in fact succeed would queue
    the run a second time, and the second leg has no single-flight check of its
    own."""
    source = (Path(__file__).resolve().parent / "index.py").read_text(encoding="utf-8")
    config = re.search(r"starter_client = boto3\.client\((.*?)\n\)", source, re.S)
    assert config, "the dedicated client config is gone"
    assert '"max_attempts": 1' in config.group(1)


# ── failures while starting ─────────────────────────────────────────────────


def test_a_failure_while_starting_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser gets a sentence rather than DynamoDB internals."""
    _, _, _, _ = install(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("table unavailable")

    monkeypatch.setattr(index, "read_meeting", boom)
    result = index.handler(start_event({"username": OWNER}), FakeContext())
    assert result["Success"] is False
    assert result["Message"] == "could not start regenerating the summary"


def test_a_failure_to_mark_the_meeting_does_not_fail_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run is already queued by then, so reporting a failure would be
    wrong."""
    ddb = FakeDynamo()
    starter = FakeStarter()
    monkeypatch.setattr(index, "dynamodb", ddb)
    monkeypatch.setattr(index, "starter_client", starter)

    def boom(*args, **kwargs):
        raise RuntimeError("appsync unavailable")

    monkeypatch.setattr(index, "write_summary", boom)
    result = index.handler(start_event({"username": OWNER}), FakeContext())
    assert result["Success"] is True
    assert starter.calls
