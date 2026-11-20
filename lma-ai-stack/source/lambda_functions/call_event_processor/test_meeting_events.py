#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Meeting Started, Meeting Ended and Summary Ready from the call event processor.

Each event is published once, on the write that made its transition, and
carries identifiers and states only. These drive the real dispatch with the
harness from ``test_graphql_response_handling`` (the real schema, only the
GraphQL transport replaced), and a recording EventBridge client in place of
boto3's.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

# The harness, not its tests: importing a test_* name, or a fixture, would
# collect it a second time here.
import test_graphql_response_handling as harness  # noqa: E402
from graphql import build_ast_schema, parse

cep = harness.cep
QueuedSession = harness.QueuedSession
TransportQueryError = harness.TransportQueryError
process_event = harness.process_event

# The module the processor itself imported. Not a fresh ``import lma_events``:
# the harness imports the processor inside ``mock.patch.dict(sys.modules)``,
# which drops what was imported there, so a second import is a second module.
lma_events = cep.meeting_event_publisher.lma_events


@pytest.fixture(scope="module")
def schema():
    """The schema the stack deploys, as the harness builds it."""
    return build_ast_schema(
        parse(harness.APPSYNC_PRELUDE + harness.SCHEMA_FILE.read_text(encoding="utf-8")),
        assume_valid_sdl=True,
    )


@pytest.fixture
def emitted(monkeypatch):
    recorder = harness.RecordingEmitter()
    monkeypatch.setattr(cep, "METRICS", recorder, raising=True)
    return recorder


BUS = "lma-test-meeting-events"
OWNER = "user@example.com"

MEETING = {
    "CallId": "call-1",
    "Owner": OWNER,
    "Status": "STARTED",
    "CreatedAt": "2026-11-19T15:00:00.000Z",
    "UpdatedAt": "2026-11-19T15:00:00.500Z",
    "Labels": [{"Key": "project", "Value": "acme-claims"}],
    "Audience": "external",
    "SummaryProfile": "discovery-session",
    "SessionStartedNumber": 1,
}

SUMMARY_TEXT = '{"SUMMARY": "We agreed to ship on Friday."}'


class RecordingEvents:
    def __init__(self, error: Exception | None = None) -> None:
        self.entries: List[Dict[str, Any]] = []
        self.error = error

    def put_events(self, Entries):  # noqa: N803 - the boto3 argument name
        if self.error:
            raise self.error
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "e-1"}]}

    def published(self) -> List[tuple]:
        return [(entry["DetailType"], json.loads(entry["Detail"])) for entry in self.entries]


@pytest.fixture
def events(monkeypatch) -> RecordingEvents:
    recorder = RecordingEvents()
    monkeypatch.setenv(lma_events.BUS_NAME_ENV_VAR, BUS)
    monkeypatch.setattr(lma_events, "_CLIENT", recorder)
    return recorder


@pytest.fixture(autouse=True)
def notifications(monkeypatch) -> List[str]:
    sent: List[str] = []
    monkeypatch.setattr(
        lma_events, "notify_summary_ready", lambda call_id, **_: sent.append(call_id) or True
    )
    monkeypatch.setattr(cep, "TRANSCRIPT_DELETION_FUNCTION_ARN", "", raising=False)
    monkeypatch.setattr(cep, "POST_CALL_SUMMARY_LAMBDA_HOOK_FUNCTION_ARN", "", raising=False)
    return sent


def already_exists() -> TransportQueryError:
    return TransportQueryError(
        "Item already exists",
        errors=[{"message": "Item already exists", "errorType": "DynamoDB"}],
    )


def start(message_extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {"EventType": "START", "CallId": "call-1", "AgentId": OWNER, **(message_extra or {})}


# ── Meeting Started ─────────────────────────────────────────────────────────


def test_a_created_meeting_publishes_meeting_started(schema, emitted, events, monkeypatch):
    session = QueuedSession(
        schema,
        [{"createCall": {"CallId": "call-1", "Owner": OWNER}}, {"getCall": MEETING}],
    )
    process_event(session, start({"VirtualParticipantId": "vp-1"}), monkeypatch)
    assert events.published() == [
        (
            "Meeting Started",
            {
                "version": 1,
                "meetingId": "call-1",
                "virtualParticipantId": "vp-1",
                "owner": OWNER,
                "labels": {"project": "acme-claims"},
                "audience": "external",
                "status": "STARTED",
                "summaryProfile": "discovery-session",
                "sessionNumber": 1,
                "occurredAt": "2026-11-19T15:00:00.000Z",
            },
        )
    ]
    assert events.entries[0]["Source"] == "lma"
    assert events.entries[0]["EventBusName"] == BUS


def test_a_redelivered_start_publishes_nothing(schema, emitted, events, monkeypatch):
    session = QueuedSession(schema, [already_exists()])
    process_event(session, start(), monkeypatch)
    assert events.published() == []


def test_a_reopened_meeting_publishes_meeting_started_for_its_session(
    schema, emitted, events, monkeypatch
):
    reopened = {
        **MEETING,
        "UpdatedAt": "2026-11-19T17:00:09.000Z",
        "SessionStartedNumber": 2,
        "Sessions": [
            {"SessionNumber": 1, "StartedAt": "2026-11-19T15:00:00.000Z"},
            {"SessionNumber": 2, "StartedAt": "2026-11-19T17:00:00.000Z"},
        ],
    }
    session = QueuedSession(
        schema,
        [
            already_exists(),
            {"startCallSession": {"CallId": "call-1", "SessionStartedNumber": 2}},
            {"getCall": reopened},
        ],
    )
    monkeypatch.setattr(cep, "TRANSCRIPT_DELETION_FUNCTION_ARN", "", raising=False)
    process_event(session, start({"SessionNumber": 2}), monkeypatch)
    ((detail_type, detail),) = events.published()
    assert detail_type == "Meeting Started"
    assert detail["sessionNumber"] == 2
    assert detail["occurredAt"] == "2026-11-19T17:00:00.000Z"


def test_an_unreachable_bus_does_not_fail_the_start(schema, emitted, monkeypatch):
    monkeypatch.setenv(lma_events.BUS_NAME_ENV_VAR, BUS)
    monkeypatch.setattr(lma_events, "_CLIENT", RecordingEvents(error=RuntimeError("timeout")))
    session = QueuedSession(
        schema, [{"createCall": {"CallId": "call-1", "Owner": OWNER}}, {"getCall": MEETING}]
    )
    process_event(session, start(), monkeypatch)  # does not raise


def test_an_unreadable_meeting_still_publishes_what_the_record_says(
    schema, emitted, events, monkeypatch
):
    session = QueuedSession(
        schema, [{"createCall": {"CallId": "call-1", "Owner": OWNER}}, {"getCall": None}]
    )
    process_event(session, start(), monkeypatch)
    ((_, detail),) = events.published()
    assert detail == {"version": 1, "meetingId": "call-1", "owner": OWNER, "sessionNumber": 1}


def test_without_a_bus_the_processor_publishes_nothing(schema, emitted, monkeypatch):
    monkeypatch.delenv(lma_events.BUS_NAME_ENV_VAR, raising=False)
    recorder = RecordingEvents()
    monkeypatch.setattr(lma_events, "_CLIENT", recorder)
    session = QueuedSession(schema, [{"createCall": {"CallId": "call-1", "Owner": OWNER}}])
    process_event(session, start(), monkeypatch)
    assert recorder.entries == []
    # And it does not read the meeting for an event it will not send.
    assert len(session.queries) == 1


# ── Meeting Ended ───────────────────────────────────────────────────────────


def ended_meeting() -> Dict[str, Any]:
    return {**MEETING, "Status": "ENDED", "UpdatedAt": "2026-11-19T16:00:00.000Z"}


def test_an_end_publishes_meeting_ended(schema, emitted, events, monkeypatch):
    session = QueuedSession(
        schema,
        [
            {"getCall": {"CallId": "call-1", "Status": "STARTED", "SessionStartedNumber": 1}},
            {"updateCallStatus": {"CallId": "call-1", "Status": "ENDED"}},
            {"getCall": ended_meeting()},
        ],
    )
    process_event(session, {"EventType": "END", "CallId": "call-1"}, monkeypatch)
    ((detail_type, detail),) = events.published()
    assert detail_type == "Meeting Ended"
    assert detail["status"] == "ENDED"
    assert detail["sessionNumber"] == 1
    assert detail["occurredAt"] == "2026-11-19T16:00:00.000Z"


def test_a_redelivered_end_publishes_nothing(schema, emitted, events, monkeypatch):
    session = QueuedSession(
        schema,
        [
            {"getCall": {"CallId": "call-1", "Status": "ENDED"}},
            {"updateCallStatus": {"CallId": "call-1", "Status": "ENDED"}},
        ],
    )
    process_event(session, {"EventType": "END", "CallId": "call-1"}, monkeypatch)
    assert events.published() == []


def test_the_end_of_an_earlier_session_publishes_nothing(schema, emitted, events, monkeypatch):
    session = QueuedSession(
        schema, [{"getCall": {"CallId": "call-1", "Status": "STARTED", "SessionStartedNumber": 2}}]
    )
    process_event(
        session, {"EventType": "END", "CallId": "call-1", "SessionNumber": 1}, monkeypatch
    )
    assert events.published() == []


def test_a_later_sessions_end_publishes_meeting_ended_with_its_number(
    schema, emitted, events, monkeypatch
):
    session = QueuedSession(
        schema,
        [
            {"getCall": {"CallId": "call-1", "Status": "STARTED", "SessionStartedNumber": 2}},
            {"updateCallStatus": {"CallId": "call-1", "Status": "ENDED"}},
            {"getCall": ended_meeting()},
        ],
    )
    process_event(
        session, {"EventType": "END", "CallId": "call-1", "SessionNumber": 2}, monkeypatch
    )
    ((_, detail),) = events.published()
    assert detail["sessionNumber"] == 2


def test_a_failed_status_write_publishes_no_meeting_ended(schema, emitted, events, monkeypatch):
    failure = TransportQueryError(
        "service unavailable",
        errors=[{"message": "service unavailable", "errorType": "ServiceUnavailable"}],
    )
    session = QueuedSession(
        schema, [{"getCall": {"CallId": "call-1", "Status": "STARTED"}}, failure]
    )
    with pytest.raises(Exception):  # noqa: B017 - the transport's own type
        process_event(session, {"EventType": "END", "CallId": "call-1"}, monkeypatch)
    assert events.published() == []


# ── Summary Ready ───────────────────────────────────────────────────────────


def test_a_stored_summary_publishes_summary_ready_and_notifies(
    schema, emitted, events, notifications, monkeypatch
):
    stored = {
        **ended_meeting(),
        "CallSummaryState": json.dumps({"status": "idle", "updatedAt": "2026-11-19T16:02:00Z"}),
    }
    session = QueuedSession(
        schema, [{"addCallSummaryText": {"CallId": "call-1"}}, {"getCall": stored}]
    )
    process_event(
        session,
        {"EventType": "ADD_SUMMARY", "CallId": "call-1", "CallSummaryText": SUMMARY_TEXT},
        monkeypatch,
    )
    ((detail_type, detail),) = events.published()
    assert detail_type == "Summary Ready"
    assert detail == {
        "version": 1,
        "meetingId": "call-1",
        "owner": OWNER,
        "labels": {"project": "acme-claims"},
        "audience": "external",
        "status": "ENDED",
        "summaryProfile": "discovery-session",
        "occurredAt": "2026-11-19T16:02:00.000Z",
    }
    assert "ship on Friday" not in events.entries[0]["Detail"]
    assert notifications == ["call-1"]


@pytest.mark.parametrize("text", ["An error occurred.", "", None])
def test_a_failed_summary_run_publishes_nothing(
    text, schema, emitted, events, notifications, monkeypatch
):
    session = QueuedSession(schema, [{"addCallSummaryText": {"CallId": "call-1"}}])
    process_event(
        session,
        {"EventType": "ADD_SUMMARY", "CallId": "call-1", "CallSummaryText": text},
        monkeypatch,
    )
    assert events.published() == []
    assert notifications == []


def test_without_a_bus_a_stored_summary_still_notifies(schema, emitted, notifications, monkeypatch):
    monkeypatch.delenv(lma_events.BUS_NAME_ENV_VAR, raising=False)
    session = QueuedSession(schema, [{"addCallSummaryText": {"CallId": "call-1"}}])
    process_event(
        session,
        {"EventType": "ADD_SUMMARY", "CallId": "call-1", "CallSummaryText": SUMMARY_TEXT},
        monkeypatch,
    )
    assert notifications == ["call-1"]
