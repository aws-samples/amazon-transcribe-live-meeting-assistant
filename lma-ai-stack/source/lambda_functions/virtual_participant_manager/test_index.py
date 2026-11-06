# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Who may end, cancel and delete a Virtual Participant through this Lambda.

endVirtualParticipant (which also cancels a scheduled one) and
deleteVirtualParticipants reach this Lambda through AppSync with the caller's
identity. Both hold the "Virtual Participant owner condition" of the AppSync
templates: the owner, Admins and backend roles may; nobody else may.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import index  # noqa: E402

OWNER = {"username": "owner@example.com", "groups": [], "claims": {"email": "owner@example.com"}}
ADMIN = {"username": "admin@example.com", "groups": ["Admin"]}
ADMIN_BY_CLAIM = {"username": "admin@example.com", "claims": {"cognito:groups": "Admin"}}
OTHER = {"username": "bob@example.com", "groups": [], "claims": {"email": "bob@example.com"}}
BACKEND = {"userArn": "arn:aws:sts::123456789012:assumed-role/LambdaRole/session"}
IAM_USER = {"userArn": "arn:aws:iam::123456789012:user/someone", "username": "AIDAEXAMPLE"}

SCHEDULED = {
    "id": "vp-1",
    "Owner": "owner@example.com",
    "owner": "owner@example.com",
    "SharedWith": "bob@example.com",
    "status": "SCHEDULED",
    "isScheduled": True,
}


@pytest.mark.parametrize(
    "identity,allowed",
    [
        (OWNER, True),
        (ADMIN, True),
        (ADMIN_BY_CLAIM, True),
        (BACKEND, True),
        (OTHER, False),
        (IAM_USER, False),
        ({}, False),
    ],
)
def test_the_owner_admins_and_backend_roles_may_change_a_virtual_participant(
    identity: dict, allowed: bool
) -> None:
    assert index.caller_may_change(identity, SCHEDULED) is allowed


def test_a_missing_row_is_changeable_only_by_admins_and_backend_roles() -> None:
    assert index.caller_may_change(OWNER, None) is False
    assert index.caller_may_change(ADMIN, None) is True
    assert index.caller_may_change(BACKEND, None) is True


@pytest.fixture
def table():
    rows = {"vp-1": dict(SCHEDULED), "vp-2": {**SCHEDULED, "id": "vp-2", "Owner": "carol@x.com"}}
    fake = mock.Mock()
    fake.get_item.side_effect = lambda Key: (
        {"Item": dict(rows[Key["id"]])} if Key["id"] in rows else {}
    )
    fake.update_item.side_effect = lambda **kwargs: {
        "Attributes": {**rows[kwargs["Key"]["id"]], "status": "CANCELLED"}
    }
    with (
        mock.patch.object(index.dynamodb, "Table", return_value=fake),
        mock.patch.object(index, "scheduler") as scheduler,
        mock.patch.object(index, "ECSTaskManager"),
        mock.patch.object(index, "ALBCleanupManager"),
        mock.patch.object(index, "MicrovmManager"),
    ):
        fake.scheduler = scheduler
        yield fake


def _end(identity: dict, vp_id: str = "vp-1") -> dict:
    return index.lambda_handler(
        {
            "operation": "endVirtualParticipant",
            "arguments": {"input": {"id": vp_id, "endReason": "Schedule cancelled by user"}},
            "identity": identity,
        },
        None,
    )


def _delete(identity: dict, ids: list) -> dict:
    return index.lambda_handler(
        {
            "operation": "deleteVirtualParticipants",
            "arguments": {"input": {"ids": ids}},
            "identity": identity,
        },
        None,
    )


@pytest.mark.parametrize("identity", [OWNER, ADMIN, BACKEND])
def test_cancelling_is_allowed_to_the_owner_admins_and_backend_roles(table, identity) -> None:
    result = _end(identity)
    assert result["statusCode"] == 200, result
    table.scheduler.delete_schedule.assert_called_once()
    table.update_item.assert_called_once()


@pytest.mark.parametrize("identity", [OTHER, IAM_USER, {}])
def test_cancelling_is_refused_to_other_callers_without_changing_anything(table, identity) -> None:
    result = _end(identity)
    assert result["statusCode"] == 403
    assert result["body"]["errorType"] == "AuthorizationError"
    assert "is not authorized to change Virtual Participant vp-1" in result["body"]["error"]
    table.scheduler.delete_schedule.assert_not_called()
    table.update_item.assert_not_called()


def test_a_refused_caller_is_not_told_whether_the_row_exists(table) -> None:
    assert _end(OTHER, "vp-missing")["body"]["errorType"] == "AuthorizationError"
    assert "not found" in _end(ADMIN, "vp-missing")["body"]["error"]


def test_deleting_removes_only_the_rows_the_caller_may_change(table) -> None:
    result = _delete(OWNER, ["vp-1", "vp-2"])
    assert result["statusCode"] == 200
    deleted = [c.kwargs["Key"]["id"] for c in table.delete_item.call_args_list]
    assert deleted == ["vp-1"]
    assert "1 failed" in result["body"]["Result"]
    assert "vp-2" in result["body"]["Result"]


@pytest.mark.parametrize("identity", [ADMIN, BACKEND])
def test_admins_and_backend_roles_delete_any_row(table, identity) -> None:
    _delete(identity, ["vp-1", "vp-2"])
    deleted = sorted(c.kwargs["Key"]["id"] for c in table.delete_item.call_args_list)
    assert deleted == ["vp-1", "vp-2"]


def test_another_user_deletes_nothing(table) -> None:
    result = _delete(OTHER, ["vp-1"])
    table.delete_item.assert_not_called()
    table.scheduler.delete_schedule.assert_not_called()
    assert "1 failed" in result["body"]["Result"]


def test_cancelling_a_schedule_logs_no_environment(table, caplog, monkeypatch) -> None:
    monkeypatch.setenv("VP_SCHEDULE_GROUP_NAME", "group")
    monkeypatch.setenv("SOME_SETTING", "value-that-stays-out-of-logs")
    with caplog.at_level(logging.INFO):
        _end(OWNER)
    assert "value-that-stays-out-of-logs" not in caplog.text
