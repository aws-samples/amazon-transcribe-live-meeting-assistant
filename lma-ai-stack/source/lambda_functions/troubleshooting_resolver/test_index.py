# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the troubleshooting resolver (no AWS calls).

Asserted here: every diagnosis -- of the whole deployment, a meeting, a Virtual
Participant or both -- is accepted from the Admin group only, and one naming a
meeting or Virtual Participant that does not exist is refused; a job is answered
only to the user who started it; and the worker records the agent's outcome
without retrying it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["TROUBLESHOOTING_JOBS_TABLE"] = "jobs"
os.environ["EVENT_SOURCING_TABLE"] = "calls"
os.environ["VIRTUAL_PARTICIPANT_TABLE"] = "vps"
os.environ["TROUBLESHOOTING_AGENT_FUNCTION_ARN"] = (
    "arn:aws:lambda:us-east-1:123456789012:function:STRANDS-LMA-Troubleshooting"
)
os.environ["AWS_LAMBDA_FUNCTION_NAME"] = "LMA-AISTACK-ABC123-Troubleshoot"

_SPEC = importlib.util.spec_from_file_location("troubleshooting_resolver_index", HERE / "index.py")
index = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index)

ADMIN = {"claims": {"cognito:groups": ["Admin"], "sub": "a"}, "username": "admin@example.com"}
OWNER = {"claims": {"sub": "o"}, "username": "owner@example.com"}
FRIEND = {"claims": {"sub": "f"}, "username": "friend@example.com"}
STRANGER = {"claims": {"sub": "x"}, "username": "x@example.com"}
OTHER_ADMIN = {"claims": {"cognito:groups": ["Admin"], "sub": "b"}, "username": "b@example.com"}


def _conditional_failure(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}, operation
    )


class FakeDynamoDB:
    """The three tables the resolver reads, keyed as DynamoDB keys them."""

    def __init__(self) -> None:
        self.calls: Dict[str, Dict[str, Any]] = {}
        self.vps: Dict[str, Dict[str, Any]] = {}
        self.jobs: Dict[str, Dict[str, Any]] = {}

    def get_item(self, **request: Any) -> Dict[str, Any]:
        table = request["TableName"]
        if table == "calls":
            item = self.calls.get(request["Key"]["PK"]["S"])
        elif table == "vps":
            item = self.vps.get(request["Key"]["id"]["S"])
        else:
            item = self.jobs.get(request["Key"]["JobId"]["S"])
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, **request: Any) -> Dict[str, Any]:
        assert request["TableName"] == "jobs"
        item = request["Item"]
        if item["JobId"]["S"] in self.jobs:
            raise _conditional_failure("PutItem")
        self.jobs[item["JobId"]["S"]] = dict(item)
        return {}

    def update_item(self, **request: Any) -> Dict[str, Any]:
        assert request["TableName"] == "jobs"
        job = self.jobs[request["Key"]["JobId"]["S"]]
        names = request["ExpressionAttributeNames"]
        values = request["ExpressionAttributeValues"]
        condition = request.get("ConditionExpression") or ""
        if condition.startswith("#s = :pending"):
            if job["Status"]["S"] != values[":pending"]["S"]:
                raise _conditional_failure("UpdateItem")
            if ":fresh" in condition and not job["UpdatedAt"]["S"] > values[":fresh"]["S"]:
                raise _conditional_failure("UpdateItem")
        assignments = request["UpdateExpression"][len("SET ") :].split(", ")
        for assignment in assignments:
            name, value = (part.strip() for part in assignment.split("="))
            job[names[name]] = values[value]
        return {"Attributes": dict(job)}


class FakeLambda:
    def __init__(self, response: Dict[str, Any] | None = None) -> None:
        self.invocations: List[Dict[str, Any]] = []
        self.response = response

    def invoke(self, **request: Any) -> Dict[str, Any]:
        self.invocations.append(request)
        if self.response is None:
            return {"StatusCode": 202}
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _agent_response(status: int, body: Dict[str, Any], function_error: str = "") -> Dict:
    payload = json.dumps({"statusCode": status, "body": json.dumps(body)}).encode()
    response: Dict[str, Any] = {"StatusCode": 200, "Payload": io.BytesIO(payload)}
    if function_error:
        response["FunctionError"] = function_error
    return response


@pytest.fixture
def ddb(monkeypatch: pytest.MonkeyPatch) -> FakeDynamoDB:
    fake = FakeDynamoDB()
    fake.calls["c#meeting-1"] = {
        "PK": {"S": "c#meeting-1"},
        "Owner": {"S": "owner@example.com"},
        "SharedWith": {"L": [{"S": "friend@example.com"}]},
    }
    fake.vps["vp-1"] = {
        "id": {"S": "vp-1"},
        "Owner": {"S": "owner@example.com"},
        "SharedWith": {"S": "friend@example.com"},
    }
    monkeypatch.setattr(index, "DYNAMODB", fake)
    return fake


@pytest.fixture
def lam(monkeypatch: pytest.MonkeyPatch) -> FakeLambda:
    fake = FakeLambda()
    monkeypatch.setattr(index, "LAMBDA", fake)
    return fake


def _start(identity: Dict[str, Any], **input_data: Any) -> Dict[str, Any]:
    return index.handler(
        {
            "arguments": {"input": input_data},
            "identity": identity,
            "info": {"fieldName": "startTroubleshooting"},
        },
        None,
    )


def _get(identity: Dict[str, Any], job_id: str) -> Dict[str, Any]:
    return index.handler(
        {
            "arguments": {"JobId": job_id},
            "identity": identity,
            "info": {"fieldName": "getTroubleshootingJob"},
        },
        None,
    )


# ── every diagnosis is for the Admin group ──────────────────────────────────


def test_admin_can_ask_about_the_whole_deployment(ddb: FakeDynamoDB, lam: FakeLambda) -> None:
    job = _start(ADMIN, Question="Are there transcriber errors in the last hour?")
    assert job["Status"] == "PENDING"
    assert job["Question"] == "Are there transcriber errors in the last hour?"
    assert job["CallId"] is None and job["VirtualParticipantId"] is None
    stored = ddb.jobs[job["JobId"]]
    assert stored["Requester"]["S"] == "admin@example.com"
    assert int(stored["ExpiresAt"]["N"]) > datetime.now(timezone.utc).timestamp()


def test_an_admin_stack_wide_question_defaults_when_blank(
    ddb: FakeDynamoDB, lam: FakeLambda
) -> None:
    job = _start(ADMIN, Question="   ")
    assert job["Question"] == index.STACK_WIDE_DEFAULT_QUESTION


NON_ADMINS = pytest.mark.parametrize(
    "identity", [OWNER, FRIEND, STRANGER], ids=["owner", "shared", "other"]
)


@NON_ADMINS
def test_a_stack_wide_question_requires_the_admin_group(
    identity: Dict[str, Any], ddb: FakeDynamoDB, lam: FakeLambda
) -> None:
    with pytest.raises(index.ForbiddenError, match="Admin"):
        _start(identity, Question="Anything wrong?")
    assert ddb.jobs == {} and lam.invocations == []


@NON_ADMINS
def test_a_meeting_diagnosis_requires_the_admin_group(
    identity: Dict[str, Any], ddb: FakeDynamoDB, lam: FakeLambda
) -> None:
    with pytest.raises(index.ForbiddenError, match="Admin"):
        _start(identity, CallId="meeting-1")
    assert ddb.jobs == {} and lam.invocations == []


@NON_ADMINS
def test_a_vp_diagnosis_requires_the_admin_group(
    identity: Dict[str, Any], ddb: FakeDynamoDB, lam: FakeLambda
) -> None:
    with pytest.raises(index.ForbiddenError, match="Admin"):
        _start(identity, VirtualParticipantId="vp-1")
    assert ddb.jobs == {} and lam.invocations == []


@NON_ADMINS
def test_a_diagnosis_naming_both_requires_the_admin_group(
    identity: Dict[str, Any], ddb: FakeDynamoDB, lam: FakeLambda
) -> None:
    with pytest.raises(index.ForbiddenError, match="Admin"):
        _start(identity, CallId="meeting-1", VirtualParticipantId="vp-1")
    assert ddb.jobs == {} and lam.invocations == []


def test_a_non_admin_is_refused_before_any_record_is_read(
    ddb: FakeDynamoDB, lam: FakeLambda, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: List[Dict[str, Any]] = []
    original = ddb.get_item
    monkeypatch.setattr(ddb, "get_item", lambda **r: reads.append(r) or original(**r))
    with pytest.raises(index.ForbiddenError):
        _start(OWNER, CallId="meeting-1", VirtualParticipantId="vp-1")
    assert reads == []


def test_admin_can_diagnose_a_meeting(ddb: FakeDynamoDB, lam: FakeLambda) -> None:
    job = _start(ADMIN, CallId="meeting-1")
    assert job["Status"] == "PENDING"
    assert job["CallId"] == "meeting-1"
    assert job["Question"] == index.scoped_question("meeting-1", "")


def test_admin_can_diagnose_a_vp(ddb: FakeDynamoDB, lam: FakeLambda) -> None:
    job = _start(ADMIN, VirtualParticipantId="vp-1")
    assert job["VirtualParticipantId"] == "vp-1"
    assert job["Question"] == index.scoped_question("", "vp-1")


def test_admin_can_diagnose_a_meeting_and_vp_together(ddb: FakeDynamoDB, lam: FakeLambda) -> None:
    job = _start(ADMIN, CallId="meeting-1", VirtualParticipantId="vp-1")
    assert (job["CallId"], job["VirtualParticipantId"]) == ("meeting-1", "vp-1")


def test_an_admin_may_ask_their_own_question_about_a_resource(
    ddb: FakeDynamoDB, lam: FakeLambda
) -> None:
    job = _start(ADMIN, CallId="meeting-1", Question="Why is the summary missing?")
    assert job["Question"] == "Why is the summary missing?"


def test_a_diagnosis_of_an_unknown_meeting_is_refused(ddb: FakeDynamoDB, lam: FakeLambda) -> None:
    with pytest.raises(index.ForbiddenError, match="meeting"):
        _start(ADMIN, CallId="no-such-meeting")
    assert ddb.jobs == {} and lam.invocations == []


def test_a_diagnosis_of_an_unknown_vp_is_refused(ddb: FakeDynamoDB, lam: FakeLambda) -> None:
    with pytest.raises(index.ForbiddenError, match="Virtual Participant"):
        _start(ADMIN, VirtualParticipantId="no-such-vp")
    with pytest.raises(index.ForbiddenError):
        _start(ADMIN, CallId="meeting-1", VirtualParticipantId="no-such-vp")
    assert ddb.jobs == {} and lam.invocations == []


def test_a_caller_with_no_username_is_refused(ddb: FakeDynamoDB, lam: FakeLambda) -> None:
    with pytest.raises(index.ForbiddenError):
        _start({"claims": {"cognito:groups": ["Admin"]}}, Question="x")


def test_an_overlong_question_is_refused(ddb: FakeDynamoDB, lam: FakeLambda) -> None:
    with pytest.raises(ValueError):
        _start(ADMIN, Question="x" * (index.MAX_QUESTION_LENGTH + 1))


# ── starting the worker ─────────────────────────────────────────────────────


def test_start_invokes_this_function_asynchronously_with_the_job_id_only(
    ddb: FakeDynamoDB, lam: FakeLambda
) -> None:
    job = _start(ADMIN, CallId="meeting-1")
    assert len(lam.invocations) == 1
    invocation = lam.invocations[0]
    assert invocation["FunctionName"] == "LMA-AISTACK-ABC123-Troubleshoot"
    assert invocation["InvocationType"] == "Event"
    assert json.loads(invocation["Payload"]) == {"troubleshootingJob": {"JobId": job["JobId"]}}


def test_a_worker_that_cannot_be_started_fails_the_job(
    ddb: FakeDynamoDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = FakeLambda(
        ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "x"}}, "Invoke")
    )
    monkeypatch.setattr(index, "LAMBDA", failing)
    with pytest.raises(ClientError):
        _start(ADMIN, Question="x")
    (stored,) = ddb.jobs.values()
    assert stored["Status"]["S"] == "FAILED"


def test_start_is_refused_when_the_agent_is_not_deployed(
    ddb: FakeDynamoDB, lam: FakeLambda, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index, "AGENT_FUNCTION_ARN", "")
    with pytest.raises(ValueError, match="not deployed"):
        _start(ADMIN, Question="x")
    assert ddb.jobs == {}


# ── reading a job ───────────────────────────────────────────────────────────


def test_a_job_is_answered_to_the_user_who_started_it(ddb: FakeDynamoDB, lam: FakeLambda) -> None:
    job = _start(ADMIN, CallId="meeting-1")
    assert _get(ADMIN, job["JobId"])["JobId"] == job["JobId"]


@pytest.mark.parametrize(
    "identity", [OTHER_ADMIN, OWNER, STRANGER], ids=["another-admin", "owner", "other"]
)
def test_a_job_is_not_answered_to_anyone_else(
    identity: Dict[str, Any], ddb: FakeDynamoDB, lam: FakeLambda
) -> None:
    job = _start(ADMIN, CallId="meeting-1")
    with pytest.raises(index.ForbiddenError):
        _get(identity, job["JobId"])


def test_an_unknown_job_is_refused(ddb: FakeDynamoDB, lam: FakeLambda) -> None:
    with pytest.raises(index.ForbiddenError):
        _get(ADMIN, "no-such-job")


def test_a_job_whose_worker_stopped_reads_as_failed(ddb: FakeDynamoDB, lam: FakeLambda) -> None:
    job = _start(ADMIN, CallId="meeting-1")
    long_ago = datetime.now(timezone.utc) - timedelta(seconds=index.STALE_AFTER_SECONDS + 5)
    ddb.jobs[job["JobId"]]["UpdatedAt"] = {"S": index._iso(long_ago)}
    read = _get(ADMIN, job["JobId"])
    assert read["Status"] == "FAILED"
    assert read["Error"]


# ── the worker ──────────────────────────────────────────────────────────────


def _run_worker(job_id: str) -> None:
    index.handler({"troubleshootingJob": {"JobId": job_id}}, None)


def test_the_worker_records_the_agents_answer(
    ddb: FakeDynamoDB, lam: FakeLambda, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _start(ADMIN, VirtualParticipantId="vp-1", CallId="meeting-1")
    agent = FakeLambda(
        _agent_response(200, {"answer": "## Root Cause\nNot determined", "truncated": True})
    )
    monkeypatch.setattr(index, "AGENT_LAMBDA", agent)
    _run_worker(job["JobId"])

    (invocation,) = agent.invocations
    assert invocation["InvocationType"] == "RequestResponse"
    sent = json.loads(invocation["Payload"])
    assert sent == {"query": job["Question"], "callId": "meeting-1", "vpId": "vp-1"}
    read = _get(ADMIN, job["JobId"])
    assert read["Status"] == "COMPLETED"
    assert read["Answer"].startswith("## Root Cause")
    assert read["Truncated"] is True


def test_the_worker_records_the_agents_fixed_error_message(
    ddb: FakeDynamoDB, lam: FakeLambda, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _start(ADMIN, Question="x")
    monkeypatch.setattr(
        index,
        "AGENT_LAMBDA",
        FakeLambda(
            _agent_response(400, {"error": "The prompt is not available.", "errorType": "E"})
        ),
    )
    _run_worker(job["JobId"])
    read = _get(ADMIN, job["JobId"])
    assert read["Status"] == "FAILED"
    assert read["Error"] == "The prompt is not available."


def test_a_function_error_is_reported_with_a_fixed_message(
    ddb: FakeDynamoDB, lam: FakeLambda, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _start(ADMIN, Question="x")
    response = {
        "StatusCode": 200,
        "FunctionError": "Unhandled",
        "Payload": io.BytesIO(b'{"errorMessage": "arn:aws:iam::123456789012:role/x"}'),
    }
    monkeypatch.setattr(index, "AGENT_LAMBDA", FakeLambda(response))
    _run_worker(job["JobId"])
    read = _get(ADMIN, job["JobId"])
    assert read["Status"] == "FAILED"
    assert "arn:" not in read["Error"]


def test_an_invocation_failure_still_ends_the_job(
    ddb: FakeDynamoDB, lam: FakeLambda, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _start(ADMIN, Question="x")
    monkeypatch.setattr(index, "AGENT_LAMBDA", FakeLambda(TimeoutError("read timed out")))
    _run_worker(job["JobId"])
    assert _get(ADMIN, job["JobId"])["Status"] == "FAILED"


def test_the_worker_runs_a_job_once(
    ddb: FakeDynamoDB, lam: FakeLambda, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _start(ADMIN, Question="x")
    agent = FakeLambda(_agent_response(200, {"answer": "## Root Cause\nx"}))
    monkeypatch.setattr(index, "AGENT_LAMBDA", agent)
    _run_worker(job["JobId"])
    _run_worker(job["JobId"])
    assert len(agent.invocations) == 1


def test_the_agent_invocation_is_not_retried() -> None:
    assert index.AGENT_INVOKE_CONFIG.retries["total_max_attempts"] == 1
    assert index.AGENT_INVOKE_CONFIG.read_timeout > 300


def test_an_appsync_request_is_never_treated_as_a_worker(
    ddb: FakeDynamoDB, lam: FakeLambda, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _start(ADMIN, Question="x")
    agent = FakeLambda(_agent_response(200, {"answer": "## Root Cause\nx"}))
    monkeypatch.setattr(index, "AGENT_LAMBDA", agent)
    with pytest.raises(ValueError):
        index.handler(
            {
                "troubleshootingJob": {"JobId": job["JobId"]},
                "arguments": {},
                "identity": STRANGER,
                "info": {"fieldName": "somethingElse"},
            },
            None,
        )
    assert agent.invocations == []


def test_a_worker_event_for_a_stale_job_does_not_run_it(
    ddb: FakeDynamoDB, lam: FakeLambda, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued worker event delivered after the page reads the job as failed is dropped."""
    job = _start(ADMIN, Question="x")
    old = datetime.now(timezone.utc) - timedelta(seconds=index.STALE_AFTER_SECONDS + 60)
    ddb.jobs[job["JobId"]]["UpdatedAt"] = {"S": index._iso(old)}
    agent = FakeLambda(_agent_response(200, {"answer": "## Root Cause\nx"}))
    monkeypatch.setattr(index, "AGENT_LAMBDA", agent)
    _run_worker(job["JobId"])
    assert agent.invocations == []
