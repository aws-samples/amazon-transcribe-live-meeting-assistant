# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""schedule_meeting called again for a meeting the caller has already scheduled.

A second call with the same meeting name and time updates the caller's waiting
Virtual Participant through updateScheduledVirtualParticipant instead of
creating another; anything else, or allowDuplicate, creates a new one.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from tools import schedule_meeting
from tools._meeting_fakes import ADMIN, OTHER, OWNER

WHEN = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(microsecond=0)
WHEN_EPOCH = int(WHEN.timestamp())
OLD_LINK = "https://us05web.zoom.us/j/81234567890?pwd=oldpwd"
NEW_LINK = "https://us05web.zoom.us/j/89876543210?pwd=newpwd"


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def scheduled_vp(vp_id="vp-old", owner=OWNER, status="SCHEDULED", **extra):
    row = {
        "id": vp_id,
        "Owner": owner,
        "status": status,
        "isScheduled": True,
        "meetingName": "DMN Review",
        "meetingPlatform": "ZOOM",
        "meetingId": OLD_LINK,
        "meetingTime": WHEN_EPOCH,
        "scheduledFor": iso(WHEN),
        "createdAt": "2026-11-19T15:40:00Z",
    }
    row.update(extra)
    return row


@pytest.fixture
def created(monkeypatch):
    """createVirtualParticipant: the inputs it is sent, answered with a new VP."""
    sent = []

    def post(url, headers=None, data=None, timeout=None):  # noqa: ARG001
        body = json.loads(data)
        sent.append(body["variables"]["input"])
        vp_input = body["variables"]["input"]
        response = MagicMock()
        response.json.return_value = {
            "data": {
                "createVirtualParticipant": {
                    "id": "vp-new",
                    "meetingName": vp_input["meetingName"],
                    "meetingPlatform": vp_input["meetingPlatform"],
                    "meetingId": vp_input["meetingId"],
                    "status": "SCHEDULED",
                    "owner": vp_input.get("owner"),
                }
            }
        }
        return response

    import botocore.auth
    import requests

    monkeypatch.setenv("APPSYNC_GRAPHQL_URL", "https://example.invalid/graphql")
    monkeypatch.setattr(schedule_meeting, "resolve_user_sub", lambda _user: None)
    monkeypatch.setattr(schedule_meeting, "has_zoom_credentials", lambda _sub: False)
    monkeypatch.setattr(schedule_meeting.boto3, "Session", lambda: MagicMock())
    monkeypatch.setattr(botocore.auth, "SigV4Auth", lambda *a, **k: MagicMock())
    monkeypatch.setattr(requests, "post", post)
    return sent


@pytest.fixture
def updates(appsync):
    """updateScheduledVirtualParticipant, answered with the row as written."""
    appsync.answers["updateScheduledVirtualParticipant"] = {
        "updateScheduledVirtualParticipant": {
            "id": "vp-old",
            "meetingName": "DMN Review",
            "meetingPlatform": "ZOOM",
            "meetingId": NEW_LINK,
            "scheduledFor": iso(WHEN),
            "status": "SCHEDULED",
        }
    }
    return appsync


def schedule(user=OWNER, name="DMN Review", when=WHEN, **extra):
    return schedule_meeting.execute(
        meeting_name=name,
        meeting_platform="Zoom",
        meeting_id=extra.pop("meeting_id", NEW_LINK),
        scheduled_time=iso(when),
        user_id=user,
        **extra,
    )


def test_a_second_call_for_the_same_meeting_updates_the_scheduled_vp(vp_table, updates, created):
    vp_table.put(scheduled_vp(meetingPassword="old-passcode"))
    answer = schedule(name="  dmn review ", agenda="Decisions\nNext steps")
    assert created == []
    assert answer["virtualParticipantId"] == "vp-old"
    assert answer["action"] == "updated"
    assert answer["meetingId"] == NEW_LINK
    (query, variables), *rest = updates.calls
    assert rest == []
    assert "mutation UpdateScheduledVirtualParticipant" in query
    assert variables["input"] == {
        "id": "vp-old",
        "meetingPlatform": "ZOOM",
        "meetingId": NEW_LINK,
        # The stored password was the replaced meeting's.
        "clearMeetingPassword": True,
        "agenda": "Decisions\nNext steps",
    }


def test_a_time_within_a_minute_is_the_same_meeting(vp_table, updates, created):
    vp_table.put(scheduled_vp())
    answer = schedule(when=WHEN + timedelta(seconds=45))
    assert answer["action"] == "updated" and created == []


def test_a_new_password_is_sent_and_an_unchanged_meeting_keeps_its_own(vp_table, updates, created):
    vp_table.put(scheduled_vp(meetingId="81234567890"))
    schedule(meeting_id="81234567890")
    schedule(meeting_id="81234567890", meeting_password="12345678")
    first, second = (variables["input"] for _, variables in updates.calls)
    assert "clearMeetingPassword" not in first and "meetingPassword" not in first
    assert "agenda" not in first
    assert second["meetingPassword"] == "12345678"


@pytest.mark.parametrize(
    "name,when",
    [
        ("DMN Review", WHEN + timedelta(minutes=2)),
        ("DMN Review", WHEN + timedelta(days=7)),
        ("DMN Retro", WHEN),
    ],
)
def test_a_different_time_or_name_schedules_a_new_vp(vp_table, updates, created, name, when):
    vp_table.put(scheduled_vp())
    answer = schedule(name=name, when=when)
    assert answer["virtualParticipantId"] == "vp-new"
    assert answer["action"] == "created"
    assert len(created) == 1 and updates.calls == []


def test_allow_duplicate_schedules_a_second_vp(vp_table, updates, created):
    vp_table.put(scheduled_vp())
    answer = schedule(allow_duplicate=True)
    assert answer["action"] == "created" and answer["virtualParticipantId"] == "vp-new"
    assert len(created) == 1 and updates.calls == []


@pytest.mark.parametrize("status", ["INITIALIZING", "ACTIVE", "COMPLETED", "ENDED", "CANCELLED"])
def test_a_vp_that_has_started_or_finished_is_not_updated(vp_table, updates, created, status):
    vp_table.put(scheduled_vp(status=status, isScheduled=status == "INITIALIZING"))
    answer = schedule()
    assert answer["action"] == "created"
    assert len(created) == 1 and updates.calls == []


@pytest.mark.parametrize("user", [OTHER, ADMIN])
def test_another_users_vp_is_never_matched(vp_table, updates, created, user):
    vp_table.put(scheduled_vp(owner=OWNER))
    answer = schedule(user=user)
    assert answer["action"] == "created"
    assert created[0]["owner"] == user
    assert updates.calls == []


def test_the_newest_matching_vp_is_the_one_updated(vp_table, updates, created):
    vp_table.put(scheduled_vp("vp-a", createdAt="2026-11-19T15:40:00Z"))
    vp_table.put(scheduled_vp("vp-b", createdAt="2026-11-19T17:26:00Z"))
    schedule()
    assert updates.calls[0][1]["input"]["id"] == "vp-b"


def test_a_refused_update_is_reported_and_nothing_is_created(vp_table, appsync, created):
    vp_table.put(scheduled_vp())
    appsync.answers["updateScheduledVirtualParticipant"] = ValueError(
        "Virtual Participant vp-old starts in less than 5 minutes, so its launch settings "
        "can no longer be changed"
    )
    with pytest.raises(ValueError, match="less than 5 minutes.*allowDuplicate"):
        schedule()
    assert created == []


def test_both_dispatchers_forward_allow_duplicate(monkeypatch):
    import index

    seen = []
    monkeypatch.setattr(
        index.schedule_meeting, "execute", lambda **kw: seen.append(kw) or {"ok": True}
    )
    arguments = {
        "meetingName": "W",
        "meetingPlatform": "Zoom",
        "meetingId": "1",
        "scheduledTime": "2099-01-01T10:00:00Z",
    }
    index.execute_tool_call(1, "schedule_meeting", arguments, "u", "u", False, "read-write")
    index.execute_tool_call(
        1, "schedule_meeting", {**arguments, "allowDuplicate": True}, "u", "u", False, "read-write"
    )
    assert [kw["allow_duplicate"] for kw in seen] == [False, True]
    schema = next(tool for tool in index.MCP_TOOLS if tool["name"] == "schedule_meeting")
    assert schema["inputSchema"]["properties"]["allowDuplicate"]["type"] == "boolean"
    assert "allowDuplicate" not in schema["inputSchema"]["required"]
