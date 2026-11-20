#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The meeting event contract: what a detail carries, and that publishing never raises."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lma_events  # noqa: E402

BUS = "lma-test-meeting-events"

# Meeting content that a record carries and an event must never carry.
CONTENT_FIELDS = {
    "CallSummaryText": "We agreed to ship on Friday.",
    "Agenda": "1. Budget",
    "MeetingContext": "Background briefing",
    "Checklist": [{"ItemId": "1", "Text": "Ask about budget"}],
    "MeetingTopic": "Quarterly review",
    "Participants": ["Sarah Lee"],
    "SharedWith": "someone@example.com",
    "meetingName": "Quarterly review",
    "meetingContext": "Background briefing",
    "agenda": "1. Budget",
    "manualActionMessage": "Solve the puzzle",
    "errorMessage": "Asked to leave by Jeremy.",
    "meetingSharedWith": "someone@example.com",
}

MEETING = {
    "CallId": "call-1",
    "Owner": "user@example.com",
    "Status": "STARTED",
    "CreatedAt": "2026-11-19T15:00:00.000Z",
    "UpdatedAt": "2026-11-19T16:00:00.123Z",
    "Labels": [{"Key": "project", "Value": "acme-claims"}],
    "Audience": "external",
    "SummaryProfile": "discovery-session",
    **CONTENT_FIELDS,
}

VP_ROW = {
    "id": "vp-1",
    "CallId": "call-1",
    "owner": "user@example.com",
    "status": "MANUAL_ACTION_REQUIRED",
    "manualActionType": "CAPTCHA",
    "updatedAt": "2026-11-19T15:59:00.000Z",
    "labels": [{"Key": "project", "Value": "acme-claims"}],
    "audience": "internal",
    **CONTENT_FIELDS,
}


class RecordingEvents:
    def __init__(self, response=None, error=None):
        self.entries = []
        self.response = response or {"FailedEntryCount": 0, "Entries": [{"EventId": "e-1"}]}
        self.error = error

    def put_events(self, Entries):  # noqa: N803 - the boto3 argument name
        if self.error:
            raise self.error
        self.entries.extend(Entries)
        return self.response


@pytest.fixture
def bus(monkeypatch):
    monkeypatch.setenv(lma_events.BUS_NAME_ENV_VAR, BUS)
    monkeypatch.setenv(lma_events.NAMESPACE_ENV_VAR, "lma-test")


def metric_names(lines):
    return [name for line in lines for name in json.loads(line) if name != "_aws"]


# ── the detail ──────────────────────────────────────────────────────────────


def test_a_meeting_detail_carries_ids_and_states_only():
    detail = lma_events.meeting_detail(MEETING, session_number=1)
    assert detail == {
        "version": 1,
        "meetingId": "call-1",
        "owner": "user@example.com",
        "labels": {"project": "acme-claims"},
        "audience": "external",
        "status": "STARTED",
        "summaryProfile": "discovery-session",
        "sessionNumber": 1,
        "occurredAt": "2026-11-19T16:00:00.123Z",
    }
    assert set(detail) <= set(lma_events.DETAIL_FIELDS)


def test_a_vp_detail_carries_ids_and_states_only():
    detail = lma_events.vp_detail(VP_ROW)
    assert detail == {
        "version": 1,
        "meetingId": "call-1",
        "virtualParticipantId": "vp-1",
        "owner": "user@example.com",
        "labels": {"project": "acme-claims"},
        "audience": "internal",
        "vpStatus": "MANUAL_ACTION_REQUIRED",
        "manualActionType": "CAPTCHA",
        "occurredAt": "2026-11-19T15:59:00.000Z",
    }


@pytest.mark.parametrize(
    "detail", [lambda: lma_events.meeting_detail(MEETING), lambda: lma_events.vp_detail(VP_ROW)]
)
def test_no_detail_contains_meeting_content(detail):
    text = json.dumps(detail())
    for value in CONTENT_FIELDS.values():
        for fragment in value if isinstance(value, list) else [value]:
            assert json.dumps(fragment)[1:-1] not in text


def test_a_field_outside_the_contract_is_refused():
    with pytest.raises(ValueError, match="CallSummaryText"):
        lma_events.build_detail(meetingId="call-1", CallSummaryText="text")


def test_unknown_fields_are_left_out_rather_than_sent_empty():
    detail = lma_events.meeting_detail({"CallId": "call-1", "Labels": [], "Audience": ""})
    assert detail == {"version": 1, "meetingId": "call-1"}


def test_a_manual_action_type_is_sent_only_while_one_is_open():
    row = {**VP_ROW, "status": "JOINING"}
    detail = lma_events.vp_detail(row)
    assert detail["vpStatus"] == "JOINING"
    assert "manualActionType" not in detail


def test_a_vp_without_a_meeting_yet_has_no_meeting_id():
    row = {key: value for key, value in VP_ROW.items() if key != "CallId"}
    assert "meetingId" not in lma_events.vp_detail(row)


def test_the_owner_is_read_from_either_spelling_on_a_vp_row():
    row = {**VP_ROW, "Owner": "admin@example.com"}
    assert lma_events.vp_detail(row)["owner"] == "admin@example.com"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-11-19T16:00:00Z", "2026-11-19T16:00:00.000Z"),
        ("2026-11-19T16:00:00.5+00:00", "2026-11-19T16:00:00.500Z"),
        ("2026-11-19T11:00:00-05:00", "2026-11-19T16:00:00.000Z"),
        (datetime(2026, 11, 19, 16, tzinfo=timezone.utc), "2026-11-19T16:00:00.000Z"),
        (1795622400, "2026-11-25T16:00:00.000Z"),
        ("not a time", None),
        ("", None),
    ],
)
def test_occurred_at_is_utc_iso_8601(value, expected):
    assert lma_events.iso_utc(value) == expected


def test_labels_accept_the_stored_list_or_a_mapping():
    assert lma_events.labels_map([{"Key": "a", "Value": "1"}]) == {"a": "1"}
    assert lma_events.labels_map({"a": "1"}) == {"a": "1"}
    assert lma_events.labels_map([]) is None
    assert lma_events.labels_map(None) is None


# ── publishing ──────────────────────────────────────────────────────────────


def test_an_event_goes_to_the_stacks_bus_with_source_lma(bus):
    client = RecordingEvents()
    lines = []
    detail = lma_events.meeting_detail(MEETING)
    assert lma_events.publish(lma_events.MEETING_STARTED, detail, client=client, sink=lines.append)
    (entry,) = client.entries
    assert entry["Source"] == "lma"
    assert entry["DetailType"] == "Meeting Started"
    assert entry["EventBusName"] == BUS
    assert json.loads(entry["Detail"]) == detail
    assert entry["Time"] == datetime(2026, 11, 19, 16, 0, 0, 123000, tzinfo=timezone.utc)
    assert metric_names(lines) == [lma_events.PUBLISHED_METRIC]


def test_without_a_bus_nothing_is_published(monkeypatch):
    monkeypatch.delenv(lma_events.BUS_NAME_ENV_VAR, raising=False)
    client = RecordingEvents()
    assert lma_events.publish(lma_events.MEETING_ENDED, {"version": 1}, client=client) is False
    assert client.entries == []


def test_an_unreachable_bus_is_logged_and_counted_not_raised(bus, caplog):
    lines = []
    client = RecordingEvents(error=RuntimeError("connect timeout"))
    detail = lma_events.meeting_detail(MEETING)
    assert (
        lma_events.publish(lma_events.MEETING_ENDED, detail, client=client, sink=lines.append)
        is False
    )
    assert metric_names(lines) == [lma_events.PUBLISH_FAILED_METRIC]
    assert "call-1" in caplog.text and "Meeting Ended" in caplog.text


def test_an_entry_the_bus_rejects_is_counted_as_a_failure(bus):
    lines = []
    client = RecordingEvents(
        response={"FailedEntryCount": 1, "Entries": [{"ErrorCode": "InternalFailure"}]}
    )
    assert (
        lma_events.publish(
            lma_events.SUMMARY_READY, {"version": 1}, client=client, sink=lines.append
        )
        is False
    )
    assert metric_names(lines) == [lma_events.PUBLISH_FAILED_METRIC]


def test_an_unknown_detail_type_is_not_published(bus):
    client = RecordingEvents()
    assert (
        lma_events.publish("Meeting Paused", {"version": 1}, client=client, sink=lambda _: None)
        is False
    )
    assert client.entries == []


def test_the_metric_record_is_embedded_metric_format_without_dimensions(bus):
    lines = []
    lma_events.publish(
        lma_events.MEETING_STARTED, {"version": 1}, client=RecordingEvents(), sink=lines.append
    )
    record = json.loads(lines[0])
    (directive,) = record["_aws"]["CloudWatchMetrics"]
    assert directive["Namespace"] == "lma-test"
    assert directive["Dimensions"] == [[]]
    assert directive["Metrics"] == [{"Name": lma_events.PUBLISHED_METRIC, "Unit": "Count"}]


def test_the_four_detail_types_are_the_contracts():
    assert lma_events.DETAIL_TYPES == (
        "Meeting Started",
        "Meeting Ended",
        "Summary Ready",
        "Virtual Participant Status Changed",
    )


# ── the summary-ready notification ──────────────────────────────────────────


def test_the_summary_ready_notification_carries_no_summary_text():
    sent = []

    def graphql(_query, variables):
        sent.append(variables["input"])
        return {"publishMeetingNotification": {"Created": True}}

    assert lma_events.notify_summary_ready("call-1", graphql=graphql) is True
    (item,) = sent
    assert item["CallId"] == "call-1"
    assert item["Category"] == "meeting"
    assert item["Type"] == "summary_ready"
    assert item["DedupKey"] == "summary-ready"
    assert "Body" not in item


def test_a_failed_notification_is_not_raised():
    def graphql(_query, _variables):
        raise RuntimeError("unreachable")

    assert lma_events.notify_summary_ready("call-1", graphql=graphql) is False


def test_summary_ready_is_timed_by_when_the_summary_was_stored():
    meeting = {**MEETING, "CallSummaryState": json.dumps({"updatedAt": "2026-11-20T09:30:00Z"})}
    assert lma_events.summary_detail(meeting)["occurredAt"] == "2026-11-20T09:30:00.000Z"


@pytest.mark.parametrize("state", [None, "", "not json", json.dumps({"status": "idle"})])
def test_summary_ready_falls_back_to_the_meetings_time(state):
    meeting = {**MEETING, "CallSummaryState": state}
    assert lma_events.summary_detail(meeting)["occurredAt"] == "2026-11-19T16:00:00.123Z"
