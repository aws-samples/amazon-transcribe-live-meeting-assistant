#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The meeting assistant's answer segments, as the orchestrator writes them.

An answer is a transcript segment: placed on the meeting's transcript timeline
(seconds from the start of the meeting) and stored under the assistant's name.
A wake-phrase answer takes the times of the segment that asked; a question
from the meeting page's chat arrives with a wall-clock time and is placed where
the meeting's transcript has reached. The Kinesis writer, the meeting-assist
Lambda and DynamoDB are stand-ins.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


class StubLogger:
    """Stands in for aws_lambda_powertools.Logger."""

    def __init__(self, *args, **kwargs):
        pass

    def inject_lambda_context(self, handler):
        return handler

    def debug(self, *args, **kwargs):
        pass

    info = warning = error = exception = debug


# Each dotted name needs its own entry, and the stubs are scoped to this
# import, so another test module in the same session that imports the real
# Powertools or the real layer is not handed them. The expiry is not under test
# here (it is the Retention section's), so the layer's helper is a stub too.
_STUBS = {
    "aws_lambda_powertools": mock.MagicMock(Logger=StubLogger),
    "aws_lambda_powertools.utilities": mock.MagicMock(),
    "aws_lambda_powertools.utilities.typing": mock.MagicMock(),
    "eventprocessor_utils": mock.MagicMock(get_transcription_ttl=lambda: 0),
}

with mock.patch.dict(sys.modules, _STUBS):
    import lambda_function as orchestrator  # noqa: E402

# A wall-clock time, as the meeting page's chat stamps its questions.
WALL_CLOCK = 1_794_240_852.0

# The requests the orchestrator sent the meeting-assist Lambda, in the last run.
REQUESTS: List[Dict[str, Any]] = []


class _Table:
    """A stand-in DynamoDB client holding meeting records by key."""

    def __init__(
        self, items: Dict[str, Dict[str, Any]] | None = None, error: Exception | None = None
    ):
        self.items = items or {}
        self.error = error
        self.requests: List[Dict[str, Any]] = []

    def get_item(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        item = self.items.get(kwargs["Key"]["PK"]["S"])
        return {"Item": item} if item is not None else {}


@pytest.fixture
def written(monkeypatch):
    """Run the orchestrator for a question; return the segments it wrote to Kinesis."""
    segments: List[Dict[str, Any]] = []

    def put_record(**kwargs):
        segments.append(json.loads(kwargs["Data"]))

    class _Lambda:
        @staticmethod
        def invoke(**kwargs):
            REQUESTS.append(json.loads(kwargs["Payload"]))
            return {
                "Payload": io.BytesIO(json.dumps({"message": "The launch is on Friday."}).encode())
            }

    monkeypatch.setattr(orchestrator.KINESIS_CLIENT, "put_record", put_record)
    monkeypatch.setattr(orchestrator, "LAMBDA_CLIENT", _Lambda())
    monkeypatch.setattr(orchestrator, "CALL_DATA_STREAM_NAME", "CallDataStream")
    monkeypatch.setattr(orchestrator, "DYNAMODB_TABLE_NAME", "EventSourcingTable")
    monkeypatch.setattr(orchestrator, "get_transcription_ttl", lambda: 0)

    def run(message: Dict[str, Any], table: _Table) -> List[Dict[str, Any]]:
        monkeypatch.setattr(orchestrator, "DYNAMODB_CLIENT", table)
        segments.clear()
        REQUESTS.clear()
        orchestrator.publish_lambda_agent_assist_transcript_segment(
            {
                "CallId": "call-1",
                "SegmentId": "q-1",
                "Transcript": "when is the launch?",
                "IsPartial": False,
                **message,
            }
        )
        return list(segments)

    return run


def _meeting(millis: float) -> _Table:
    return _Table({"c#call-1": {"TotalConversationDurationMillis": {"N": str(millis)}}})


CHAT_QUESTION = {
    "Channel": "CHAT_ASSISTANT",
    "StartTime": WALL_CLOCK,
    "EndTime": WALL_CLOCK + 1,
    "MessageId": "chat-1",
}


def test_an_assistant_answer_is_placed_at_its_meeting_time(written):
    checking, answer = written(CHAT_QUESTION, _meeting(754_500))
    for segment in (checking, answer):
        assert segment["Channel"] == "CHAT_ASSISTANT"
        assert segment["StartTime"] == pytest.approx(754.5)
        assert segment["EndTime"] == pytest.approx(754.501)
    assert answer["Transcript"] == "The launch is on Friday."


def test_an_assistant_answer_is_labelled_as_the_assistant(written):
    for message in (
        CHAT_QUESTION,
        {"Channel": "AGENT_ASSISTANT", "StartTime": 12.0, "EndTime": 14.0},
    ):
        for segment in written(message, _meeting(20_000)):
            assert segment["Speaker"] == orchestrator.ASSISTANT_SPEAKER == "Meeting Assistant"


def test_the_meeting_time_is_read_from_the_meeting_record(written):
    table = _meeting(1_000)
    written(CHAT_QUESTION, table)
    assert table.requests == [
        {
            "TableName": "EventSourcingTable",
            "Key": {"PK": {"S": "c#call-1"}, "SK": {"S": "c#call-1"}},
            "ProjectionExpression": "TotalConversationDurationMillis",
        }
    ]


@pytest.mark.parametrize(
    "table",
    [
        _Table(),  # no meeting record
        _Table({"c#call-1": {}}),  # no speech yet
        _Table(error=RuntimeError("throttled")),  # unreadable
    ],
)
def test_an_answer_with_no_meeting_time_is_placed_at_the_start_never_at_a_wall_clock_time(
    written, table
):
    for segment in written(CHAT_QUESTION, table):
        assert segment["StartTime"] == 0.0
        assert segment["EndTime"] == pytest.approx(0.001)


def test_a_wake_phrase_answer_keeps_the_time_of_the_segment_that_asked(written):
    table = _meeting(99_000)
    checking, answer = written(
        {"Channel": "AGENT_ASSISTANT", "StartTime": 12.0, "EndTime": 14.0}, table
    )
    for segment in (checking, answer):
        assert segment["Channel"] == "AGENT_ASSISTANT"
        assert segment["StartTime"] == 12.0
        assert segment["EndTime"] == pytest.approx(14.001)
    assert table.requests == [], "the meeting record is read only for a question from the chat"


def test_a_wake_phrase_request_is_sent_as_an_in_meeting_request(written):
    # Spoken in the meeting by whoever is in it, so the meeting assistant
    # answers it as the In-meeting requests setting allows.
    written({"Channel": "AGENT_ASSISTANT", "StartTime": 12.0, "EndTime": 14.0}, _meeting(0))
    assert [request.get("toolScope") for request in REQUESTS] == ["wake_phrase"]


def test_a_question_from_the_meeting_pages_chat_carries_no_in_meeting_scope(written):
    # The signed-in user's own question: answered with every tool, as before.
    written(CHAT_QUESTION, _meeting(0))
    assert len(REQUESTS) == 1
    assert "toolScope" not in REQUESTS[0]
