#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Virtual Participant Status Changed: one event per status change, ids and states only."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from boto3.dynamodb.types import TypeSerializer

HERE = Path(__file__).resolve().parent
LAYER = HERE.parents[1] / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

_spec = importlib.util.spec_from_file_location(
    "meeting_events_vp_publisher_index", HERE / "index.py"
)
index = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = index
_spec.loader.exec_module(index)

BUS = "lma-test-meeting-events"
_SERIALIZER = TypeSerializer()

ROW = {
    "id": "vp-1",
    "CallId": "call-1",
    "owner": "user@example.com",
    "Owner": "user@example.com",
    "status": "JOINING",
    "updatedAt": "2026-11-19T15:58:00.000Z",
    "meetingName": "Quarterly review",
    "meetingId": "123 456 789",
    "meetingPassword": "secret",
    "agenda": "1. Budget",
    "meetingContext": "Background briefing",
    "checklist": [{"Text": "Ask about budget"}],
    "labels": [{"Key": "project", "Value": "acme-claims"}],
    "audience": "external",
    "summaryProfile": "discovery-session",
    "errorMessage": "Waiting in the lobby",
    "manualActionMessage": "Solve the puzzle",
    "vncPassword": "vnc-secret",
    "meetingTime": 1795622400,
}

CONTENT = (
    "Quarterly review",
    "123 456 789",
    "secret",
    "Budget",
    "Background briefing",
    "lobby",
    "puzzle",
    "vnc-secret",
)


def image(row):
    return {key: _SERIALIZER.serialize(value) for key, value in row.items()}


def record(event_name, new=None, old=None, approximate=1795622400):
    data = {"ApproximateCreationDateTime": approximate}
    if new is not None:
        data["NewImage"] = image(new)
    if old is not None:
        data["OldImage"] = image(old)
    return {"eventName": event_name, "dynamodb": data}


class RecordingEvents:
    def __init__(self, error=None):
        self.entries = []
        self.error = error

    def put_events(self, Entries):  # noqa: N803 - the boto3 argument name
        if self.error:
            raise self.error
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "e-1"}]}

    def details(self):
        return [json.loads(entry["Detail"]) for entry in self.entries]


@pytest.fixture
def events(monkeypatch):
    recorder = RecordingEvents()
    monkeypatch.setenv(index.lma_events.BUS_NAME_ENV_VAR, BUS)
    monkeypatch.setattr(index.lma_events, "_CLIENT", recorder)
    return recorder


def test_a_status_change_publishes_one_event_with_ids_and_states_only(events):
    old = {**ROW, "status": "CONNECTING"}
    result = index.handler({"Records": [record("MODIFY", ROW, old)]}, None)
    assert result == {"published": 1}
    (entry,) = events.entries
    assert entry["Source"] == "lma"
    assert entry["DetailType"] == "Virtual Participant Status Changed"
    assert entry["EventBusName"] == BUS
    assert json.loads(entry["Detail"]) == {
        "version": 1,
        "meetingId": "call-1",
        "virtualParticipantId": "vp-1",
        "owner": "user@example.com",
        "labels": {"project": "acme-claims"},
        "audience": "external",
        "summaryProfile": "discovery-session",
        "vpStatus": "JOINING",
        "occurredAt": "2026-11-19T15:58:00.000Z",
    }
    for fragment in CONTENT:
        assert fragment not in entry["Detail"]


def test_a_write_that_leaves_the_status_alone_publishes_nothing(events):
    old = {**ROW, "errorMessage": "Opening the meeting link"}
    assert index.handler({"Records": [record("MODIFY", ROW, old)]}, None) == {"published": 0}
    assert events.entries == []


def test_a_new_row_is_a_change_from_no_status(events):
    new = {**ROW, "status": "SCHEDULED"}
    del new["CallId"]
    index.handler({"Records": [record("INSERT", new)]}, None)
    (detail,) = events.details()
    assert detail["vpStatus"] == "SCHEDULED"
    assert "meetingId" not in detail, "a scheduled VP has not created its meeting yet"


def test_a_person_needed_carries_the_kind_of_action(events):
    old = {**ROW, "status": "JOINING"}
    new = {**ROW, "status": "MANUAL_ACTION_REQUIRED", "manualActionType": "CAPTCHA"}
    index.handler({"Records": [record("MODIFY", new, old)]}, None)
    (detail,) = events.details()
    assert detail["vpStatus"] == "MANUAL_ACTION_REQUIRED"
    assert detail["manualActionType"] == "CAPTCHA"


def test_the_action_kind_is_left_out_once_the_prompt_is_cleared(events):
    old = {**ROW, "status": "MANUAL_ACTION_REQUIRED", "manualActionType": "CAPTCHA"}
    new = {**ROW, "status": "JOINING", "manualActionType": "CAPTCHA"}
    index.handler({"Records": [record("MODIFY", new, old)]}, None)
    (detail,) = events.details()
    assert "manualActionType" not in detail


def test_a_removed_row_publishes_nothing(events):
    assert index.handler({"Records": [record("REMOVE", old=ROW)]}, None) == {"published": 0}


def test_a_row_without_its_own_time_uses_the_streams(events):
    new = {key: value for key, value in ROW.items() if key != "updatedAt"}
    index.handler({"Records": [record("INSERT", new, approximate=1795622400)]}, None)
    (detail,) = events.details()
    assert detail["occurredAt"] == "2026-11-25T16:00:00.000Z"


def test_each_change_in_a_batch_is_published(events):
    records = [
        record("MODIFY", {**ROW, "status": "JOINING"}, {**ROW, "status": "CONNECTING"}),
        record("MODIFY", {**ROW, "status": "JOINING", "vncReady": True}, {**ROW}),
        record("MODIFY", {**ROW, "status": "JOINED"}, {**ROW, "status": "JOINING"}),
    ]
    assert index.handler({"Records": records}, None) == {"published": 2}
    assert [d["vpStatus"] for d in events.details()] == ["JOINING", "JOINED"]


def test_an_unreachable_bus_does_not_fail_the_batch(monkeypatch):
    monkeypatch.setenv(index.lma_events.BUS_NAME_ENV_VAR, BUS)
    monkeypatch.setattr(index.lma_events, "_CLIENT", RecordingEvents(error=RuntimeError("timeout")))
    old = {**ROW, "status": "CONNECTING"}
    assert index.handler({"Records": [record("MODIFY", ROW, old)]}, None) == {"published": 0}


def test_an_unreadable_record_is_skipped_not_raised(events):
    broken = {"eventName": "MODIFY", "dynamodb": {"NewImage": {"status": {"BAD": "x"}}}}
    good = record("MODIFY", ROW, {**ROW, "status": "CONNECTING"})
    assert index.handler({"Records": [broken, good]}, None) == {"published": 1}


def test_without_a_bus_nothing_is_published(monkeypatch):
    monkeypatch.delenv(index.lma_events.BUS_NAME_ENV_VAR, raising=False)
    recorder = RecordingEvents()
    monkeypatch.setattr(index.lma_events, "_CLIENT", recorder)
    index.handler({"Records": [record("INSERT", ROW)]}, None)
    assert recorder.entries == []
