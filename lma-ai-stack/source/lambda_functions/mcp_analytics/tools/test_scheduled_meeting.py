# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Meeting preparation on a Virtual Participant: at creation and before it starts.

update_scheduled_meeting changes a VP that waits for its scheduled launch, for
its owner or an Admin only, and never within five minutes of the start;
schedule_meeting and start_meeting_now pass the same inputs at creation.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from tools import schedule_meeting, scheduled_meeting, start_meeting_now, vp_preparation
from tools._meeting_fakes import ADMIN, OTHER, OWNER, SHARED, caller

NOW = 1_900_000_000
LATER = NOW + 3600


def vp_row(vp_id="vp1", status="SCHEDULED", meeting_time=LATER, **extra):
    row = {
        "id": vp_id,
        "status": status,
        "isScheduled": True,
        "Owner": OWNER,
        "SharedWith": SHARED,
        "meetingName": "Weekly",
        "meetingId": "81234567890",
        "meetingTime": meeting_time,
    }
    row.update(extra)
    return row


def update(who=None, vp_id="vp1", **kwargs):
    return scheduled_meeting.execute(who or caller(), vp_id, now=NOW, **kwargs)


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── update_scheduled_meeting ────────────────────────────────────────────────


@pytest.mark.parametrize("who", [caller(OWNER), caller(ADMIN, admin=True)])
def test_the_owner_or_an_admin_changes_every_setting(vp_table, appsync, who):
    vp_table.put(vp_row())
    appsync.answers["updateScheduledVirtualParticipant"] = {
        "updateScheduledVirtualParticipant": {
            "id": "vp1",
            "meetingName": "Weekly sync",
            "agenda": "Intro",
            "labels": [{"Key": "project", "Value": "relaunch"}],
            "audience": "external",
            "meetingContext": "# Goals",
            "checklist": [{"ItemId": "q1", "Text": "Budget?", "Kind": "question"}],
            "meetingSharedWith": "bo@example.com",
            "status": "SCHEDULED",
        }
    }
    answer = update(
        who,
        scheduled_time=iso(LATER + 600),
        meeting_name=" Weekly sync ",
        agenda="Intro",
        labels={"project": "relaunch"},
        audience="external",
        context="# Goals",
        checklist=[{"itemId": "q1", "text": "Budget?", "kind": "question"}],
        share_with=["bo@example.com"],
    )
    query, variables = appsync.calls[0]
    assert "mutation UpdateScheduledVirtualParticipant" in query
    assert variables["input"] == {
        "id": "vp1",
        "meetingTime": LATER + 600,
        "meetingName": "Weekly sync",
        "agenda": "Intro",
        "labels": [{"Key": "project", "Value": "relaunch"}],
        "audience": "external",
        "meetingContext": "# Goals",
        "checklist": [
            {"ItemId": "q1", "Text": "Budget?", "Kind": "question", "Priority": "should"}
        ],
        "meetingSharedWith": ["bo@example.com"],
    }
    assert answer["virtualParticipantId"] == "vp1"
    assert answer["meetingId"] is None and answer["platformMeetingId"] == "81234567890"
    assert answer["labels"] == {"project": "relaunch"} and answer["audience"] == "external"
    assert answer["checklist"] == [
        {
            "itemId": "q1",
            "text": "Budget?",
            "kind": "question",
            "priority": "should",
            "source": None,
        }
    ]
    assert answer["shareWith"] == ["bo@example.com"] and answer["context"] == "# Goals"


def test_empty_values_remove_each_input(vp_table, appsync):
    vp_table.put(vp_row())
    update(agenda="", labels={}, audience="", context="", checklist=[], share_with=[])
    assert appsync.calls[0][1]["input"] == {
        "id": "vp1",
        "agenda": "",
        "labels": [],
        "audience": "",
        "meetingContext": "",
        "checklist": [],
        "meetingSharedWith": [],
    }


def test_a_shared_user_or_another_user_may_not_change_it(vp_table, appsync):
    vp_table.put(vp_row())
    with pytest.raises(PermissionError, match="owner or an Admin"):
        update(caller(SHARED), agenda="x")
    with pytest.raises(PermissionError, match="not found"):
        update(caller(OTHER), agenda="x")
    with pytest.raises(PermissionError, match="not found"):
        update(caller(), vp_id="missing", agenda="x")
    assert appsync.calls == []


@pytest.mark.parametrize("starts_in", [0, 60, 299])
def test_a_vp_starting_within_five_minutes_is_not_changed(vp_table, appsync, starts_in):
    vp_table.put(vp_row(meeting_time=NOW + starts_in))
    with pytest.raises(ValueError, match="starts in less than 5 minutes"):
        update(agenda="x")
    assert appsync.calls == []


def test_a_vp_five_minutes_away_is_changed(vp_table, appsync):
    vp_table.put(vp_row(meeting_time=NOW + 300))
    update(agenda="x")
    assert len(appsync.calls) == 1


def test_a_new_time_within_five_minutes_is_refused(vp_table, appsync):
    vp_table.put(vp_row())
    with pytest.raises(ValueError, match="at least 5 minutes from now"):
        update(scheduled_time=iso(NOW + 120))
    with pytest.raises(ValueError, match="ISO 8601"):
        update(scheduled_time="next tuesday")
    assert appsync.calls == []


def test_a_vp_that_has_started_is_not_changed_this_way(vp_table, appsync):
    vp_table.put(vp_row(status="ACTIVE", CallId="m1"))
    with pytest.raises(ValueError, match="not waiting for a scheduled launch"):
        update(agenda="x")
    assert appsync.calls == []


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({}, "Give at least one of"),
        ({"meeting_name": "  "}, "meetingName cannot be empty"),
        ({"labels": {"Bad": "x"}}, "label Key"),
        ({"audience": "all"}, "Audience must be one of"),
        ({"checklist": [{"itemId": "a", "text": "x"}]}, "Kind"),
        ({"share_with": ["not-an-address"]}, "not an email address"),
        ({"context": "x" * 20001}, "20000"),
    ],
)
def test_an_input_that_breaks_a_rule_is_refused_before_anything_is_read(
    vp_table, appsync, kwargs, message
):
    vp_table.put(vp_row())
    with pytest.raises(ValueError, match=message):
        update(**kwargs)
    assert appsync.calls == []


# ── at creation ─────────────────────────────────────────────────────────────


def test_creation_inputs_leave_out_what_is_empty():
    assert vp_preparation.vp_inputs() == {}
    assert (
        vp_preparation.vp_inputs(labels={}, audience="", context="", checklist=[], share_with=[])
        == {}
    )
    assert vp_preparation.vp_inputs(
        labels={"a": "b"},
        audience="internal",
        context="c",
        checklist=[{"itemId": "i", "text": "t", "kind": "risk", "priority": "could"}],
        share_with="bo@example.com, cy@example.com, bo@example.com",
    ) == {
        "labels": [{"Key": "a", "Value": "b"}],
        "audience": "internal",
        "meetingContext": "c",
        "checklist": [{"ItemId": "i", "Text": "t", "Kind": "risk", "Priority": "could"}],
        "meetingSharedWith": ["bo@example.com", "cy@example.com"],
    }
    with pytest.raises(ValueError, match="at most 50"):
        vp_preparation.vp_inputs(share_with=[f"u{n}@example.com" for n in range(51)])


@pytest.fixture
def created(monkeypatch):
    """createVirtualParticipant, for both creating tools: the inputs sent."""
    sent = []

    def post(url, headers=None, data=None, timeout=None):  # noqa: ARG001
        vp_input = json.loads(data)["variables"]["input"]
        sent.append(vp_input)
        response = MagicMock()
        response.json.return_value = {
            "data": {
                "createVirtualParticipant": {
                    "id": "vp-new",
                    "meetingName": vp_input["meetingName"],
                    "meetingPlatform": vp_input["meetingPlatform"],
                    "meetingId": vp_input["meetingId"],
                    "status": vp_input["status"],
                }
            }
        }
        return response

    import botocore.auth
    import requests

    monkeypatch.setenv("APPSYNC_GRAPHQL_URL", "https://example.invalid/graphql")
    monkeypatch.setenv("VP_STATE_MACHINE_ARN", "")
    for module in (schedule_meeting, start_meeting_now):
        monkeypatch.setattr(module, "resolve_user_sub", lambda _user: None)
        monkeypatch.setattr(module, "has_zoom_credentials", lambda _sub: False)
        monkeypatch.setattr(module.boto3, "Session", lambda: MagicMock())
    aws = MagicMock()
    aws.get_parameter.return_value = {
        "Parameter": {"Value": json.dumps({"LMAVirtualParticipantSchedulerStateMachine": "arn"})}
    }
    aws.start_sync_execution.return_value = {"status": "SUCCEEDED"}
    monkeypatch.setenv("LMA_SETTINGS_PARAMETER", "settings")
    monkeypatch.setattr(start_meeting_now.boto3, "client", lambda *_a, **_k: aws)
    monkeypatch.setattr(botocore.auth, "SigV4Auth", lambda *a, **k: MagicMock())
    monkeypatch.setattr(requests, "post", post)
    return sent


PREPARATION = {
    "labels": [{"Key": "project", "Value": "relaunch"}],
    "meetingSharedWith": ["bo@example.com"],
}


def test_schedule_meeting_stores_the_preparation_on_the_new_vp(vp_table, created):
    when = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    answer = schedule_meeting.execute(
        meeting_name="Weekly",
        meeting_platform="Zoom",
        meeting_id="81234567890",
        scheduled_time=when,
        user_id=OWNER,
        preparation=PREPARATION,
    )
    assert created[0]["labels"] == PREPARATION["labels"]
    assert created[0]["meetingSharedWith"] == ["bo@example.com"]
    assert answer["meetingId"] is None and answer["platformMeetingId"] == "81234567890"
    assert answer["callId"] is None


def test_start_meeting_now_stores_the_preparation_on_the_new_vp(created):
    answer = start_meeting_now.execute(
        meeting_name="Weekly",
        meeting_platform="Zoom",
        meeting_id="81234567890",
        user_id=OWNER,
        preparation=PREPARATION,
    )
    assert created[0]["labels"] == PREPARATION["labels"]
    assert created[0]["meetingSharedWith"] == ["bo@example.com"]
    assert answer["virtualParticipantId"] == "vp-new"
    assert answer["meetingId"] is None and answer["platformMeetingId"] == "81234567890"


def test_a_vp_created_without_preparation_is_created_as_before(created):
    start_meeting_now.execute(
        meeting_name="Weekly", meeting_platform="Zoom", meeting_id="81234567890", user_id=OWNER
    )
    assert not {"labels", "audience", "meetingContext", "checklist", "meetingSharedWith"} & set(
        created[0]
    )
