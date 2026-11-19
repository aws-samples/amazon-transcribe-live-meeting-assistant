# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Join again as a new meeting (join_again.py).

The Virtual Participant table and the launcher state machine are in-memory
fakes, so a Join again runs end to end without AWS.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import index  # noqa: E402
import join_again  # noqa: E402
import rejoin  # noqa: E402
from access import NotAuthorizedError  # noqa: E402

OWNER = {
    "username": "owner@example.com",
    "sub": "sub-owner",
    "groups": [],
    "claims": {"email": "owner@example.com"},
}
ADMIN = {"username": "admin@example.com", "sub": "sub-admin", "groups": ["Admin"]}
OTHER = {"username": "bob@example.com", "sub": "sub-bob", "groups": []}

STORED_PASSWORD = "stored-pw-7731"
STATE_MACHINE = "arn:aws:states:us-east-1:000000000000:stateMachine:LMA-VPScheduler"


def source_vp(**overrides: Any) -> Dict[str, Any]:
    row = {
        "id": "vp-source",
        "Owner": "owner@example.com",
        "owner": "owner@example.com",
        "status": "ENDED",
        "CallId": "Weekly sync-2026-11-18",
        "meetingPlatform": "ZOOM",
        "meetingId": "123456789",
        "meetingPassword": STORED_PASSWORD,
        "meetingName": "Weekly sync",
    }
    row.update(overrides)
    return row


def form(**overrides: Any) -> Dict[str, Any]:
    """The form as Join again fills it in from the source, password left empty."""
    request = {
        "sourceVpId": "vp-source",
        "meetingName": "Weekly sync",
        "meetingPlatform": "ZOOM",
        "meetingId": "123456789",
        "enableVideoRecording": False,
        "agenda": "Budget\nHiring",
        "consentMode": "opt_in",
        "consentRule": "owner_attests",
        "useStoredZoomAccount": False,
    }
    request.update(overrides)
    return request


class VpTable:
    def __init__(self, rows: Dict[str, Dict[str, Any]]) -> None:
        self.rows = {key: dict(value) for key, value in rows.items()}
        self.puts: List[Dict[str, Any]] = []
        self.updates: List[Dict[str, Any]] = []

    def put_item(self, Item: Dict[str, Any], ConditionExpression: str) -> Dict[str, Any]:
        assert ConditionExpression == "attribute_not_exists(id)"
        assert Item["id"] not in self.rows
        self.puts.append(dict(Item))
        self.rows[Item["id"]] = dict(Item)
        return {}

    def update_item(self, **kwargs: Any) -> Dict[str, Any]:
        self.updates.append(kwargs)
        row = self.rows[kwargs["Key"]["id"]]
        values = kwargs["ExpressionAttributeValues"]
        if row.get("status") != values[":initializing"]:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "Update")
        row.update(status=values[":failed"], errorMessage=values[":reason"])
        return {}


class StateMachine:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.executions: List[Dict[str, Any]] = []

    def start_execution(self, **kwargs: Any) -> Dict[str, Any]:
        if self.fail:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "Start")
        self.executions.append({**kwargs, "input": json.loads(kwargs["input"])})
        return {"executionArn": "arn:exec"}


@pytest.fixture
def world(monkeypatch):
    def build(source: Dict[str, Any] | None = None, launch_fails: bool = False):
        table = VpTable({"vp-source": source if source is not None else source_vp()})
        state_machine = StateMachine(fail=launch_fails)
        monkeypatch.setattr(rejoin, "_clients", {"stepfunctions": state_machine})
        monkeypatch.setenv("VP_STATE_MACHINE_ARN", STATE_MACHINE)
        monkeypatch.setenv("MEETING_RECORD_EXPIRATION_IN_DAYS", "1.5")
        manager = SimpleNamespace(
            table=table,
            get_virtual_participant=lambda vp_id: (
                dict(table.rows[vp_id]) if vp_id in table.rows else None
            ),
        )
        return SimpleNamespace(manager=manager, table=table, state_machine=state_machine)

    return build


def _new_row(w) -> Dict[str, Any]:
    (row,) = w.table.puts
    return row


def _launched(w) -> Dict[str, Any]:
    (execution,) = w.state_machine.executions
    return execution["input"]["data"]


def _contains(value: Any, text: str) -> bool:
    return text in json.dumps(value, default=str)


# ── the stored password is copied for the same meeting ──────────────────────


def test_the_owner_joins_the_same_meeting_again_with_the_stored_password(world):
    w = world()
    answer = join_again.join_again_virtual_participant(w.manager, form(), OWNER)

    row = _new_row(w)
    assert row["id"] != "vp-source", "a new Virtual Participant"
    assert "CallId" not in row, "and a new meeting: the VP starts its own CallId"
    assert row["meetingPassword"] == STORED_PASSWORD
    assert row["Owner"] == row["owner"] == "owner@example.com"
    assert row["SharedWith"] == ""
    assert row["status"] == "INITIALIZING"
    assert row["enableVideoRecording"] is False
    assert row["agenda"] == "Budget\nHiring" and row["agendaSource"] == "scheduled"
    assert (row["consentMode"], row["consentRule"]) == ("opt_in", "owner_attests")

    # Launched from here, since only the server has the password.
    data = _launched(w)
    assert w.state_machine.executions[0]["stateMachineArn"] == STATE_MACHINE
    assert data["virtualParticipantId"] == row["id"]
    assert data["launchToken"] == row["id"], "the row's first launch"
    assert data["meetingID"] == "123456789"
    assert data["meetingPassword"] == STORED_PASSWORD
    assert data["meetingTime"] == ""
    assert data["userSub"] == "sub-owner", "the launching user's browser profile"
    assert data["enableVideoRecording"] == "false"

    assert answer["id"] == row["id"]
    assert answer["Owner"] == "owner@example.com"


def test_an_admin_joins_again_from_someone_elses_virtual_participant(world):
    w = world()
    join_again.join_again_virtual_participant(w.manager, form(), ADMIN)
    row = _new_row(w)
    assert row["meetingPassword"] == STORED_PASSWORD
    assert row["Owner"] == "admin@example.com", "the new one belongs to whoever starts it"
    assert _launched(w)["userSub"] == "sub-admin"


def test_the_meeting_id_is_compared_as_the_form_submits_it(world):
    w = world()
    join_again.join_again_virtual_participant(w.manager, form(meetingId="123 456 789"), OWNER)
    assert _new_row(w)["meetingPassword"] == STORED_PASSWORD


# ── who may ─────────────────────────────────────────────────────────────────


def test_a_user_who_does_not_own_the_source_may_not_join_again_from_it(world):
    w = world()
    with pytest.raises(NotAuthorizedError):
        join_again.join_again_virtual_participant(w.manager, form(), OTHER)
    assert w.table.puts == [] and w.state_machine.executions == []


def test_a_user_the_source_is_shared_with_may_not_join_again_from_it(world):
    w = world(source=source_vp(SharedWith="bob@example.com"))
    with pytest.raises(NotAuthorizedError):
        join_again.join_again_virtual_participant(w.manager, form(), OTHER)
    assert w.table.puts == []


def test_a_missing_source_is_refused_as_one_that_is_not_the_callers(world):
    w = world()
    with pytest.raises(NotAuthorizedError):
        join_again.join_again_virtual_participant(w.manager, form(sourceVpId="nope"), OTHER)
    with pytest.raises(ValueError):
        join_again.join_again_virtual_participant(w.manager, form(sourceVpId="nope"), ADMIN)


# ── when nothing is copied ──────────────────────────────────────────────────


def test_a_typed_password_wins_over_the_stored_one(world):
    w = world()
    join_again.join_again_virtual_participant(w.manager, form(meetingPassword="typed-pw"), OWNER)
    assert _new_row(w)["meetingPassword"] == "typed-pw"
    assert _launched(w)["meetingPassword"] == "typed-pw"


@pytest.mark.parametrize(
    "change",
    [
        {"meetingId": "987654321"},
        {"meetingPlatform": "TEAMS"},
        {"meetingId": "https://zoom.us/j/123456789?pwd=abc"},
    ],
)
def test_another_meeting_gets_no_stored_password(world, change):
    w = world()
    join_again.join_again_virtual_participant(w.manager, form(**change), OWNER)
    row = _new_row(w)
    assert "meetingPassword" not in row
    assert _launched(w)["meetingPassword"] == ""


def test_another_meeting_uses_a_typed_password(world):
    w = world()
    join_again.join_again_virtual_participant(
        w.manager, form(meetingId="987654321", meetingPassword="typed-pw"), OWNER
    )
    assert _new_row(w)["meetingPassword"] == "typed-pw"


def test_a_meeting_that_takes_no_password_gets_none(world):
    w = world(source=source_vp(meetingPlatform="GOOGLE_MEET", meetingId="abc-defg-hij"))
    join_again.join_again_virtual_participant(
        w.manager,
        form(meetingPlatform="GOOGLE_MEET", meetingId="abc-defg-hij", meetingPassword="x"),
        OWNER,
    )
    assert "meetingPassword" not in _new_row(w)


def test_a_source_without_a_password_gives_none(world):
    source = source_vp()
    del source["meetingPassword"]
    w = world(source=source)
    join_again.join_again_virtual_participant(w.manager, form(), OWNER)
    assert "meetingPassword" not in _new_row(w)


# ── the password never leaves the server ────────────────────────────────────


def test_no_answer_carries_the_meeting_password(world):
    w = world()
    answer = join_again.join_again_virtual_participant(w.manager, form(), OWNER)
    assert "meetingPassword" not in answer
    assert not _contains(answer, STORED_PASSWORD)


def test_the_handler_answer_carries_no_meeting_password(world, monkeypatch):
    w = world()
    monkeypatch.setattr(index, "VirtualParticipantManager", lambda _name: w.manager)
    response = index.lambda_handler(
        {
            "operation": "joinAgainVirtualParticipant",
            "arguments": {"input": form()},
            "identity": OWNER,
        },
        None,
    )
    assert response["statusCode"] == 200
    assert not _contains(response, STORED_PASSWORD)
    assert _new_row(w)["meetingPassword"] == STORED_PASSWORD


def test_the_password_is_not_logged(world, caplog):
    w = world()
    caplog.set_level("DEBUG")
    join_again.join_again_virtual_participant(w.manager, form(), OWNER)
    assert STORED_PASSWORD not in caplog.text


def test_a_refusal_carries_no_meeting_password(world, monkeypatch):
    w = world()
    monkeypatch.setattr(index, "VirtualParticipantManager", lambda _name: w.manager)
    for identity in (OTHER, OWNER):
        request = form() if identity is OTHER else form(agenda="x" * 10001)
        response = index.lambda_handler(
            {
                "operation": "joinAgainVirtualParticipant",
                "arguments": {"input": request},
                "identity": identity,
            },
            None,
        )
        assert response["statusCode"] in (400, 403)
        assert not _contains(response, STORED_PASSWORD)


# ── scheduling, failure and the create resolver's rules ─────────────────────


def test_a_scheduled_join_again_is_left_to_the_scheduler(world):
    w = world()
    meeting_time = int(datetime.now(timezone.utc).timestamp()) + 3600
    join_again.join_again_virtual_participant(
        w.manager,
        form(meetingTime=meeting_time, scheduledTimeZone="Europe/Paris", useStoredZoomAccount=True),
        OWNER,
    )
    row = _new_row(w)
    assert row["status"] == "SCHEDULED" and row["isScheduled"] is True
    assert row["meetingTime"] == meeting_time
    assert row["scheduledFor"].endswith("Z")
    assert row["meetingPassword"] == STORED_PASSWORD, "read by the scheduler at launch time"
    assert row["userSub"] == row["userZoomSub"] == "sub-owner"
    assert row["useStoredZoomAccount"] is True
    assert row["scheduledTimeZone"] == "Europe/Paris"
    assert row["ExpiresAfter"] == meeting_time + int(1.5 * 86400)
    assert w.state_machine.executions == [], "the stream scheduler launches it"


def test_a_meeting_time_in_the_past_is_refused(world):
    w = world()
    with pytest.raises(join_again.JoinAgainRefused):
        join_again.join_again_virtual_participant(w.manager, form(meetingTime=1000), OWNER)
    assert w.table.puts == []


def test_an_immediate_join_again_expires_after_the_retention(world):
    w = world()
    before = int(datetime.now(timezone.utc).timestamp())
    join_again.join_again_virtual_participant(w.manager, form(), OWNER)
    assert before + int(1.5 * 86400) <= _new_row(w)["ExpiresAfter"] <= before + 2 + 1.5 * 86400


def test_a_launch_that_cannot_be_started_marks_the_new_row_failed(world):
    w = world(launch_fails=True)
    with pytest.raises(RuntimeError):
        join_again.join_again_virtual_participant(w.manager, form(), OWNER)
    row = w.table.rows[_new_row(w)["id"]]
    assert row["status"] == "FAILED"
    assert STORED_PASSWORD not in row["errorMessage"]
    assert w.table.rows["vp-source"]["status"] == "ENDED", "the source is left as it was"


@pytest.mark.parametrize(
    "change",
    [
        {"meetingName": " "},
        {"meetingId": ""},
        {"consentMode": "always"},
        {"consentMode": "notify", "consentRule": "owner_attests"},
        {"agenda": "x" * 10001},
        {"meetingTime": 4102444800, "scheduledTimeZone": "x" * 65},
    ],
)
def test_the_create_resolvers_rules_hold(world, change):
    w = world()
    with pytest.raises(join_again.JoinAgainRefused):
        join_again.join_again_virtual_participant(w.manager, form(**change), OWNER)
    assert w.table.puts == []


def test_the_handler_answers_a_refusal_as_a_bad_request(world, monkeypatch):
    w = world()
    monkeypatch.setattr(index, "VirtualParticipantManager", lambda _name: w.manager)
    response = index.lambda_handler(
        {
            "operation": "joinAgainVirtualParticipant",
            "arguments": {"input": form(meetingName="")},
            "identity": OWNER,
        },
        None,
    )
    assert response["statusCode"] == 400
    assert response["body"]["errorType"] == "BadRequest"


def test_the_handler_answers_a_caller_who_may_not_as_an_authorization_error(world, monkeypatch):
    w = world()
    monkeypatch.setattr(index, "VirtualParticipantManager", lambda _name: w.manager)
    response = index.lambda_handler(
        {
            "operation": "joinAgainVirtualParticipant",
            "arguments": {"input": form()},
            "identity": OTHER,
        },
        None,
    )
    assert response["statusCode"] == 403
    assert response["body"]["errorType"] == "AuthorizationError"
