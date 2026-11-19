# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Rejoining a Virtual Participant to its meeting (rejoin.py).

The two tables and the launcher state machine
are in-memory fakes, so a rejoin runs end to end without AWS.
"""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest import mock

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import index  # noqa: E402
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

CALL_ID = "Weekly sync-2026-11-18-09-00-00-000"
STATE_MACHINE = (
    "arn:aws:states:us-east-1:123456789012:stateMachine:LMA-LMAVirtualParticipantScheduler"
)


def _conflict() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}}, "UpdateItem"
    )


def ended_vp(**overrides: Any) -> Dict[str, Any]:
    row = {
        "id": "vp-1",
        "Owner": "owner@example.com",
        "owner": "owner@example.com",
        "status": "ENDED",
        "CallId": CALL_ID,
        "meetingPlatform": "ZOOM",
        "meetingId": "123456789",
        "meetingPassword": "pw-on-the-row",
        "meetingName": "Weekly sync",
        "enableVideoRecording": True,
        "endReason": "User requested termination",
        "endedAt": "2026-11-18T09:30:00+00:00",
        "vncEndpoint": "wss://old",
    }
    row.update(overrides)
    return row


class VpTable:
    """The Virtual Participant table: one row, conditional writes evaluated."""

    def __init__(self, row: Dict[str, Any]) -> None:
        self.row = dict(row)
        self.updates: List[Dict[str, Any]] = []

    def update_item(self, **kwargs: Any) -> Dict[str, Any]:
        self.updates.append(kwargs)
        values = kwargs["ExpressionAttributeValues"]
        if ":seen" in values:
            if (
                self.row.get("status") != values[":seen"]
                or self.row.get("CallId") != values[":callId"]
            ):
                raise _conflict()
            names = kwargs["ExpressionAttributeNames"]
            for placeholder, attribute in names.items():
                if placeholder.startswith("#r"):
                    self.row.pop(attribute, None)
            self.row.update(
                status=values[":initializing"],
                sessionNumber=Decimal(values[":session"]),
                sessionTimeOffset=values[":offset"],
                rejoinedBy=values[":by"],
                rejoinedAt=values[":now"],
                vncReady=False,
            )
            return {"Attributes": dict(self.row)}
        if ":failed" in values:
            if self.row.get("status") != values[":initializing"]:
                raise _conflict()
            self.row.update(status="FAILED", errorMessage=values[":reason"])
            return {}
        raise AssertionError(f"unexpected update {kwargs}")


class EventsTable:
    """The event sourcing table: the meeting record and its transcript."""

    def __init__(self, meeting: Dict[str, Any] | None, end_times: List[Any]) -> None:
        self.meeting = dict(meeting) if meeting is not None else None
        self.end_times = end_times
        self.updates: List[Dict[str, Any]] = []

    def get_item(self, Key: Dict[str, str], **_: Any) -> Dict[str, Any]:
        assert Key == {"PK": f"c#{CALL_ID}", "SK": f"c#{CALL_ID}"}
        return {"Item": dict(self.meeting)} if self.meeting is not None else {}

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        assert kwargs["ExpressionAttributeValues"] == {":pk": f"trs#{CALL_ID}"}
        # Two pages, to prove every page is read.
        if "ExclusiveStartKey" not in kwargs:
            half = len(self.end_times) // 2
            return {
                "Items": [{"EndTime": end} for end in self.end_times[:half]],
                "LastEvaluatedKey": {"PK": "next"},
            }
        half = len(self.end_times) // 2
        return {"Items": [{"EndTime": end} for end in self.end_times[half:]]}

    def update_item(self, **kwargs: Any) -> Dict[str, Any]:
        self.updates.append(kwargs)
        values = kwargs["ExpressionAttributeValues"]
        if self.meeting is None or self.meeting.get("TranscriptDeletedAt"):
            raise _conflict()
        if int(self.meeting.get("SessionNumber", 1)) >= values[":session"]:
            raise _conflict()
        self.meeting.update(
            SessionNumber=values[":session"], SessionOffsetSeconds=values[":offset"]
        )
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
    """A Virtual Participant that has left its meeting, owned by OWNER."""

    def build(
        vp: Dict[str, Any] | None = None,
        meeting: Dict[str, Any] | None = None,
        end_times: List[Any] | None = None,
        launch_fails: bool = False,
        host_running: bool = False,
    ) -> SimpleNamespace:
        vp_row = vp if vp is not None else ended_vp()
        meeting_row = (
            meeting
            if meeting is not None
            else {
                "PK": f"c#{CALL_ID}",
                "SK": f"c#{CALL_ID}",
                "Owner": "owner@example.com",
                "Status": "ENDED",
                "TotalConversationDurationMillis": Decimal("1800250"),
                "TranscriptDeletionScheduledFor": "2026-11-18T11:00:00+00:00",
            }
        )
        vp_table = VpTable(vp_row)
        events = EventsTable(
            meeting_row, end_times if end_times is not None else [Decimal("1700.5"), 1800.25]
        )
        state_machine = StateMachine(fail=launch_fails)
        monkeypatch.setattr(
            rejoin,
            "_clients",
            {"events": events, "stepfunctions": state_machine},
        )
        monkeypatch.setenv("VP_STATE_MACHINE_ARN", STATE_MACHINE)
        checked: List[str] = []
        manager = SimpleNamespace(
            table=vp_table,
            get_virtual_participant=lambda vp_id: dict(vp_table.row) if vp_id == "vp-1" else None,
            registered_host_running=lambda vp_id: checked.append(vp_id) or host_running,
        )
        return SimpleNamespace(
            manager=manager,
            vp_table=vp_table,
            events=events,
            state_machine=state_machine,
            checked=checked,
        )

    return build


def test_the_owner_rejoins_an_ended_virtual_participant_into_the_same_meeting(world):
    w = world()
    answer = rejoin.rejoin_virtual_participant(w.manager, "vp-1", OWNER)

    # The row is ready for the new session: its end and old host are gone.
    assert answer["status"] == "INITIALIZING"
    assert answer["CallId"] == CALL_ID
    assert answer["sessionNumber"] == 2
    for cleared in ("endReason", "endedAt", "vncEndpoint"):
        assert cleared not in w.vp_table.row
    assert "meetingPassword" not in answer, "no answer carries the meeting password"

    # The meeting allows session 2, after everything in its transcript.
    assert w.events.meeting["SessionNumber"] == 2
    assert w.events.meeting["SessionOffsetSeconds"] == Decimal(1802)
    assert w.events.meeting["Status"] == "ENDED", "the session's START reopens it, not this"

    # The pending transcript deletion stays until the session starts; the call
    # event processor cancels it then.
    assert w.events.meeting["TranscriptDeletionScheduledFor"] == "2026-11-18T11:00:00+00:00"

    # A launch, as the Create button starts one, with a token of its own.
    (execution,) = w.state_machine.executions
    assert execution["stateMachineArn"] == STATE_MACHINE
    data = execution["input"]["data"]
    assert execution["input"]["apiInfo"] == {"httpMethod": "POST"}
    assert data["virtualParticipantId"] == "vp-1"
    assert data["launchToken"] == "vp-1-s2"
    assert data["meetingTime"] == ""
    assert data["meetingID"] == "123456789"
    assert data["meetingPassword"] == "pw-on-the-row"
    assert data["userSub"] == "sub-owner"
    assert data["userZoomSub"] == ""
    assert data["enableVideoRecording"] == "true"
    assert w.checked == ["vp-1"], "the registry is checked for a host still running"


def test_each_rejoin_is_a_new_session_with_a_new_launch_token(world):
    w = world(
        vp=ended_vp(status="FAILED", sessionNumber=Decimal(2)),
        meeting={
            "Owner": "owner@example.com",
            "SessionNumber": Decimal(2),
            "SessionStartedNumber": Decimal(2),
            "SessionOffsetSeconds": Decimal(1802),
        },
        end_times=[Decimal("2400")],
    )
    rejoin.rejoin_virtual_participant(w.manager, "vp-1", OWNER)
    assert w.events.meeting["SessionNumber"] == 3
    assert w.events.meeting["SessionOffsetSeconds"] == Decimal(2401)
    assert w.state_machine.executions[0]["input"]["data"]["launchToken"] == "vp-1-s3"


def test_an_admin_rejoins_without_the_owners_browser_profile(world):
    w = world()
    rejoin.rejoin_virtual_participant(w.manager, "vp-1", ADMIN)
    data = w.state_machine.executions[0]["input"]["data"]
    assert data["userSub"] == ""
    assert data["userName"] == "owner@example.com"


def test_a_scheduled_virtual_participant_rejoins_with_its_stored_zoom_account(world):
    w = world(vp=ended_vp(userSub="sub-owner", userZoomSub="sub-owner", isScheduled=True))
    rejoin.rejoin_virtual_participant(w.manager, "vp-1", ADMIN)
    data = w.state_machine.executions[0]["input"]["data"]
    assert data["userSub"] == "sub-owner"
    assert data["userZoomSub"] == "sub-owner"


def test_a_user_who_does_not_own_the_virtual_participant_may_not_rejoin_it(world):
    w = world()
    with pytest.raises(NotAuthorizedError):
        rejoin.rejoin_virtual_participant(w.manager, "vp-1", OTHER)
    assert w.vp_table.updates == [] and w.events.updates == []
    assert w.state_machine.executions == []


def test_only_the_meetings_owner_or_an_admin_may_reopen_it(world):
    w = world(meeting={"Owner": "carol@example.com", "Status": "ENDED"})
    with pytest.raises(NotAuthorizedError):
        rejoin.rejoin_virtual_participant(w.manager, "vp-1", OWNER)
    assert w.vp_table.updates == [] and w.state_machine.executions == []

    rejoin.rejoin_virtual_participant(w.manager, "vp-1", ADMIN)
    assert len(w.state_machine.executions) == 1


def test_a_backend_role_rejoins_any_virtual_participant_as_a_guest(world):
    """The lma CLI and SDK sign with IAM (`lma vp rejoin`)."""
    w = world(meeting={"Owner": "carol@example.com", "Status": "ENDED"})
    role = "arn:aws:sts::123456789012:assumed-role/LMA-Operator/session"
    answer = rejoin.rejoin_virtual_participant(
        w.manager, "vp-1", {"userArn": role, "username": "AROAEXAMPLE:session"}
    )
    assert answer["sessionNumber"] == 2
    assert w.vp_table.row["rejoinedBy"] == role
    data = w.state_machine.executions[0]["input"]["data"]
    assert data["userSub"] == "", "no user's browser profile"


def test_an_iam_user_may_not_rejoin_a_virtual_participant(world):
    w = world()
    with pytest.raises(NotAuthorizedError):
        rejoin.rejoin_virtual_participant(
            w.manager,
            "vp-1",
            {"userArn": "arn:aws:iam::123456789012:user/someone", "username": "AIDAEXAMPLE"},
        )
    assert w.state_machine.executions == []


@pytest.mark.parametrize("status", ["ACTIVE", "JOINING", "INITIALIZING", "SCHEDULED", "CANCELLED"])
def test_a_virtual_participant_that_has_not_left_is_not_rejoined(world, status):
    w = world(vp=ended_vp(status=status))
    with pytest.raises(rejoin.RejoinRefused):
        rejoin.rejoin_virtual_participant(w.manager, "vp-1", OWNER)
    assert w.vp_table.updates == [] and w.state_machine.executions == []


def test_a_virtual_participant_that_never_started_a_meeting_is_not_rejoined(world):
    vp = ended_vp()
    del vp["CallId"]
    w = world(vp=vp)
    with pytest.raises(rejoin.RejoinRefused, match="no meeting to rejoin"):
        rejoin.rejoin_virtual_participant(w.manager, "vp-1", OWNER)


def test_a_meeting_whose_transcript_was_deleted_is_not_reopened(world):
    w = world(meeting={"Owner": "owner@example.com", "TranscriptDeletedAt": "2026-11-18T10:00:00Z"})
    with pytest.raises(rejoin.RejoinRefused, match="transcript has been deleted"):
        rejoin.rejoin_virtual_participant(w.manager, "vp-1", OWNER)
    assert w.vp_table.updates == [] and w.state_machine.executions == []


def test_a_deletion_that_ran_after_the_check_refuses_the_rejoin_and_releases_the_row(
    world, monkeypatch
):
    w = world()
    real_read = rejoin.read_meeting

    def read_then_delete(call_id):
        meeting = real_read(call_id)
        w.events.meeting["TranscriptDeletedAt"] = "2026-11-18T10:00:00+00:00"
        return meeting

    monkeypatch.setattr(rejoin, "read_meeting", read_then_delete)
    with pytest.raises(rejoin.RejoinRefused, match="meeting changed"):
        rejoin.rejoin_virtual_participant(w.manager, "vp-1", OWNER)
    assert w.vp_table.row["status"] == "FAILED"
    assert w.state_machine.executions == []


def test_a_launch_that_cannot_be_started_marks_the_row_failed(world):
    w = world(launch_fails=True)
    with pytest.raises(RuntimeError):
        rejoin.rejoin_virtual_participant(w.manager, "vp-1", OWNER)
    assert w.vp_table.row["status"] == "FAILED"
    assert w.vp_table.row["errorMessage"].startswith("Could not rejoin the meeting")


def test_a_launch_that_cannot_be_started_leaves_the_transcript_deletion_in_place(world):
    """No session starts, so the meeting stays ended with its deletion scheduled."""
    w = world(launch_fails=True)
    with pytest.raises(RuntimeError):
        rejoin.rejoin_virtual_participant(w.manager, "vp-1", OWNER)
    assert w.events.meeting["Status"] == "ENDED"
    assert w.events.meeting["TranscriptDeletionScheduledFor"] == "2026-11-18T11:00:00+00:00"
    (update,) = w.events.updates
    assert "TranscriptDeletion" not in update["UpdateExpression"]
    assert "lambda" not in rejoin._clients, "no function is asked to cancel the deletion"


def test_a_second_rejoin_of_the_same_row_is_refused(world):
    w = world()
    stale = dict(w.vp_table.row)
    rejoin.rejoin_virtual_participant(w.manager, "vp-1", OWNER)
    w.manager.get_virtual_participant = lambda _vp_id: dict(stale)
    with pytest.raises(rejoin.RejoinRefused, match="changed while it was being rejoined"):
        rejoin.rejoin_virtual_participant(w.manager, "vp-1", OWNER)
    assert len(w.state_machine.executions) == 1


def test_a_virtual_participant_whose_meeting_was_never_created_is_not_rejoined(world):
    w = world()
    w.events.meeting = None
    for caller in (OWNER, ADMIN):
        with pytest.raises(rejoin.RejoinRefused, match="no LMA meeting to reopen"):
            rejoin.rejoin_virtual_participant(w.manager, "vp-1", caller)
    assert w.vp_table.updates == [] and w.state_machine.executions == []


@pytest.mark.parametrize(
    "end_times,meeting,expected",
    [
        # A chat message after the last speech still comes first.
        ([Decimal("400"), Decimal("512.2")], {"TotalConversationDurationMillis": 400000}, 514),
        ([], {"TotalConversationDurationMillis": Decimal("90000")}, 91),
        ([], {"SessionOffsetSeconds": Decimal("300")}, 301),
        ([None, "x", float("nan")], {}, 0),
        ([], {}, 0),
    ],
)
def test_a_new_session_starts_after_everything_already_on_the_timeline(
    end_times, meeting, expected
):
    assert rejoin.next_session_offset(end_times, meeting) == expected


def test_the_handler_answers_a_refusal_as_a_bad_request(world, monkeypatch):
    w = world(vp=ended_vp(status="ACTIVE"))
    with mock.patch.object(index, "VirtualParticipantManager", return_value=w.manager):
        response = index.lambda_handler(
            {
                "operation": "rejoinVirtualParticipant",
                "arguments": {"input": {"id": "vp-1"}},
                "identity": OWNER,
            },
            None,
        )
    assert response["statusCode"] == 400
    assert response["body"]["errorType"] == "BadRequest"


def test_the_handler_answers_a_caller_who_may_not_as_an_authorization_error(world):
    w = world()
    with mock.patch.object(index, "VirtualParticipantManager", return_value=w.manager):
        response = index.lambda_handler(
            {
                "operation": "rejoinVirtualParticipant",
                "arguments": {"input": {"id": "vp-1"}},
                "identity": OTHER,
            },
            None,
        )
    assert response["statusCode"] == 403
    assert response["body"]["errorType"] == "AuthorizationError"


def test_the_end_of_a_rejoined_virtual_participant_names_its_session(monkeypatch):
    monkeypatch.setenv("KINESIS_STREAM_NAME", "stream")
    kinesis = mock.Mock()
    with (
        mock.patch.object(index.boto3, "client", return_value=kinesis),
        mock.patch.object(index.dynamodb, "Table"),
        mock.patch.object(index, "ECSTaskManager"),
        mock.patch.object(index, "ALBCleanupManager"),
        mock.patch.object(index, "MicrovmManager"),
    ):
        manager = index.VirtualParticipantManager("table")
        assert manager.send_end_meeting_event(CALL_ID, {"sessionNumber": Decimal(2)})
        assert manager.send_end_meeting_event(CALL_ID, {})
    first, second = (json.loads(c.kwargs["Data"]) for c in kinesis.put_record.call_args_list)
    assert first["SessionNumber"] == 2
    assert second["SessionNumber"] == 1, "the first session's END names it too"


def test_a_virtual_participant_still_shutting_down_is_not_rejoined(world):
    """Its host would record its own end on the new run if it were stopped now."""
    w = world(host_running=True)
    with pytest.raises(rejoin.RejoinRefused, match="still shutting down"):
        rejoin.rejoin_virtual_participant(w.manager, "vp-1", OWNER)
    assert w.vp_table.updates == [] and w.events.updates == []
    assert w.state_machine.executions == []


# ── whether a registered host may still be running ──────────────────────────


@pytest.fixture
def host_manager():
    with (
        mock.patch.object(index.dynamodb, "Table"),
        mock.patch.object(index, "ECSTaskManager") as ecs,
        mock.patch.object(index, "ALBCleanupManager"),
        mock.patch.object(index, "MicrovmManager") as microvm,
    ):
        manager = index.VirtualParticipantManager("table")
        manager.cleaned = []
        manager.cleanup_registry_entry = lambda vp_id: manager.cleaned.append(vp_id) or True
        manager.ecs_client = ecs.return_value.ecs_client
        manager.microvm_client = microvm.return_value.client
        yield manager


ECS_ENTRY = {"vpId": "vp-1", "taskArn": "arn:task", "clusterArn": "arn:cluster"}


@pytest.mark.parametrize(
    "described,running",
    [
        ({"tasks": [{"lastStatus": "RUNNING"}]}, True),
        ({"tasks": [{"lastStatus": "DEPROVISIONING"}]}, True),
        ({"tasks": [{"lastStatus": "STOPPED"}]}, False),
        ({"tasks": [], "failures": [{"reason": "MISSING"}]}, False),
    ],
)
def test_an_ecs_host_runs_until_it_has_stopped(host_manager, described, running):
    host_manager.get_task_details_from_registry = lambda vp_id: dict(ECS_ENTRY)
    host_manager.ecs_client.describe_tasks.return_value = described
    assert host_manager.registered_host_running("vp-1") is running
    assert host_manager.cleaned == ([] if running else ["vp-1"]), "a stale entry is removed"


@pytest.mark.parametrize(
    "answer,running",
    [
        ({"state": "RUNNING"}, True),
        ({"state": "TERMINATING"}, True),
        ({"state": "TERMINATED"}, False),
        ({"state": "FAILED"}, False),
        (index.MicrovmError("not found", status=404), False),
    ],
)
def test_a_microvm_host_runs_until_it_has_terminated(host_manager, answer, running):
    host_manager.get_task_details_from_registry = lambda vp_id: {"vpId": "vp-1", "microvmId": "m-1"}
    if isinstance(answer, Exception):
        host_manager.microvm_client.get_microvm.side_effect = answer
    else:
        host_manager.microvm_client.get_microvm.return_value = answer
    assert host_manager.registered_host_running("vp-1") is running


def test_a_host_whose_state_cannot_be_read_is_taken_to_be_running(host_manager):
    host_manager.get_task_details_from_registry = lambda vp_id: dict(ECS_ENTRY)
    host_manager.ecs_client.describe_tasks.side_effect = RuntimeError("throttled")
    assert host_manager.registered_host_running("vp-1") is True
    assert host_manager.cleaned == []


@pytest.mark.parametrize("entry", [None, {"vpId": "vp-1", "vpConfig": "{}"}])
def test_no_registered_host_is_not_a_running_one(host_manager, entry):
    host_manager.get_task_details_from_registry = lambda vp_id: entry
    assert host_manager.registered_host_running("vp-1") is False
