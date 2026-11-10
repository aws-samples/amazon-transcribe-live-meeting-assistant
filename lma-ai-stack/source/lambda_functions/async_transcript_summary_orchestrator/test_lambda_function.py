#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The end-of-meeting summary orchestrator always reports how the run went.

The call event processor marks a meeting's summary as in progress when the
meeting ends, and only the ADD_SUMMARY event this function writes clears that
mark -- as a summary, or as a failure the meeting page offers Regenerate for. So
whatever the summary function does, this function writes exactly one ADD_SUMMARY
event, and it never raises: a raise would leave the mark standing and have
Lambda run the whole function, speaker naming included, again.

Powertools and the transcript enrichment layer are stubbed; boto3 is real, with
its clients replaced per test.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

HERE = Path(__file__).parent
TEMPLATE = HERE.parents[2] / "deployment" / "lma-ai-stack.yaml"


class _Logger:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def inject_lambda_context(self, function):
        return function

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


_STUBS = {
    "aws_lambda_powertools": mock.MagicMock(Logger=_Logger),
    "aws_lambda_powertools.utilities": mock.MagicMock(),
    "aws_lambda_powertools.utilities.typing": mock.MagicMock(),
    "eventprocessor_utils": mock.MagicMock(get_meeting_ttl=lambda: 1234567890),
}

sys.path.insert(0, str(HERE))
with mock.patch.dict(sys.modules, _STUBS):
    with mock.patch("boto3.Session", return_value=mock.MagicMock()):
        import lambda_function as orchestrator  # noqa: E402


class FakeSummaryFunction:
    """Answers the summary invoke with each of `outcomes` in turn."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: List[Dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        payload = outcome if isinstance(outcome, bytes) else json.dumps(outcome).encode("utf-8")
        return {"Payload": io.BytesIO(payload)}


class FakeStream:
    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def put_record(self, **kwargs: Any) -> Dict[str, Any]:
        self.records.append(json.loads(kwargs["Data"]))
        return {}


def run_orchestrator(monkeypatch, summary_function: FakeSummaryFunction) -> List[Dict[str, Any]]:
    stream = FakeStream()
    monkeypatch.setattr(orchestrator, "SUMMARY_CLIENT", summary_function)
    monkeypatch.setattr(orchestrator, "KINESIS_CLIENT", stream)
    monkeypatch.setattr(orchestrator, "SPEAKER_NAME_RESOLVER_ARN", "")
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _seconds: None)
    orchestrator.handler({"CallId": "call-1", "EventType": "END"}, None)
    return stream.records


def _throttled() -> ClientError:
    return ClientError(
        {"Error": {"Code": "TooManyRequestsException", "Message": "Rate exceeded"}}, "Invoke"
    )


def test_a_summary_is_written_to_the_stream(monkeypatch) -> None:
    records = run_orchestrator(monkeypatch, FakeSummaryFunction({"summary": '{"SUMMARY": "ok"}'}))
    assert records == [
        {
            "CallId": "call-1",
            "EventType": "ADD_SUMMARY",
            "ExpiresAfter": 1234567890,
            "CallSummaryText": '{"SUMMARY": "ok"}',
        }
    ]


@pytest.mark.parametrize(
    "outcome",
    [
        RuntimeError("the summary function could not be reached"),
        ReadTimeoutError(endpoint_url="https://lambda"),
        ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "Invoke"),
        b"not json",
        {"no": "summary"},
        ["not", "an", "object"],
        {"summary": None},
    ],
)
def test_a_summary_function_that_fails_still_reports_the_outcome(monkeypatch, outcome) -> None:
    """One ADD_SUMMARY with the failure placeholder, which the event processor
    records as a failed summary, rather than a raise that leaves the meeting
    marked as in progress."""
    records = run_orchestrator(monkeypatch, FakeSummaryFunction(outcome))
    assert [r["CallSummaryText"] for r in records] == [orchestrator.FAILED_SUMMARY_TEXT]


def test_a_throttled_summary_function_is_tried_again(monkeypatch) -> None:
    function = FakeSummaryFunction(_throttled(), {"summary": "done"})
    records = run_orchestrator(monkeypatch, function)
    assert len(function.calls) == 2
    assert [r["CallSummaryText"] for r in records] == ["done"]


def test_a_summary_function_that_stays_throttled_is_reported_as_failed(monkeypatch) -> None:
    function = FakeSummaryFunction(_throttled())
    records = run_orchestrator(monkeypatch, function)
    assert len(function.calls) == orchestrator.SUMMARY_THROTTLE_ATTEMPTS
    assert [r["CallSummaryText"] for r in records] == [orchestrator.FAILED_SUMMARY_TEXT]


def test_a_summary_function_that_timed_out_is_not_started_again(monkeypatch) -> None:
    """It may still be running; a second start would pay for a second summary."""
    function = FakeSummaryFunction(ReadTimeoutError(endpoint_url="https://lambda"))
    run_orchestrator(monkeypatch, function)
    assert len(function.calls) == 1


def test_the_summary_client_cannot_retry_the_invoke() -> None:
    assert orchestrator.SUMMARY_CLIENT_CONFIG.retries["max_attempts"] == 1


def test_the_waits_fit_inside_the_functions_timeout() -> None:
    """Speaker naming, then the summary, must both be waited out with time left
    to write the outcome; a function killed mid-wait writes nothing."""
    template = TEMPLATE.read_text(encoding="utf-8")
    function = template[template.index("  AsyncTranscriptSummaryOrchestrator:\n") :]
    function = function[: function.index("\n  AsyncTranscriptSummaryOrchestratorLogGroup:")]
    timeout = int(re.search(r"\n      Timeout: (\d+)", function).group(1))
    naming = int(re.search(r"SPEAKER_NAME_READ_TIMEOUT: (\d+)", function).group(1))
    summary = int(re.search(r"SUMMARY_READ_TIMEOUT: (\d+)", function).group(1))
    assert naming + summary + 30 <= timeout, (naming, summary, timeout)
