# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the pre token generation trigger (no AWS calls).

The trigger is what lets an AppSync field name the User role at all: without it
a user in no group carries no cognito:groups claim, and a field listing
["Admin", "User"] would refuse every existing User. These tests pin the role
rule to the role matrix's vectors and the response to the version 1 event shape
Cognito reads.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
ROLE_MATRIX = HERE.parents[2] / "lma-ai-stack" / "source" / "appsync" / "role-matrix.json"


def _load_index():
    spec = importlib.util.spec_from_file_location("pre_token_generation_index", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()


def _event(groups, trigger="TokenGeneration_Authentication", **configuration):
    return {
        "version": "1",
        "triggerSource": trigger,
        "userName": "someone@example.com",
        "request": {
            "userAttributes": {"sub": "a1b2", "email": "someone@example.com"},
            "groupConfiguration": {"groupsToOverride": groups, **configuration},
        },
        "response": {},
    }


def _vectors() -> list[dict]:
    return json.loads(ROLE_MATRIX.read_text())["effectiveRole"]["vectors"]


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: ",".join(v["groups"]) or "none")
def test_the_role_rule_matches_the_role_matrix(vector: dict) -> None:
    assert index.effective_role(vector["groups"]) == vector["role"]


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: ",".join(v["groups"]) or "none")
def test_every_token_carries_exactly_one_role_group(vector: dict) -> None:
    response = index.lambda_handler(_event(list(vector["groups"])), None)["response"]
    groups = response["claimsOverrideDetails"]["groupOverrideDetails"]["groupsToOverride"]

    assert [group for group in groups if group in ("Admin", "User", "Participant")] == [
        vector["role"]
    ]
    assert response["claimsOverrideDetails"]["claimsToAddOrOverride"] == {
        "lma_role": vector["role"]
    }


def test_a_user_in_no_group_carries_the_user_group() -> None:
    response = index.lambda_handler(_event([]), None)["response"]

    assert response["claimsOverrideDetails"]["groupOverrideDetails"]["groupsToOverride"] == ["User"]


def test_an_admin_who_is_also_a_participant_carries_admin_only() -> None:
    """Otherwise the identity pool's Participant rule would match an Admin."""
    response = index.lambda_handler(_event(["Participant", "Admin"]), None)["response"]

    assert response["claimsOverrideDetails"]["groupOverrideDetails"]["groupsToOverride"] == [
        "Admin"
    ]


def test_a_participant_in_a_group_called_user_carries_participant_only() -> None:
    """A group of that name must not lift a Participant to the User role."""
    response = index.lambda_handler(_event(["User", "Participant"]), None)["response"]

    assert response["claimsOverrideDetails"]["groupOverrideDetails"]["groupsToOverride"] == [
        "Participant"
    ]


def test_groups_other_than_the_role_groups_are_kept_in_order() -> None:
    response = index.lambda_handler(_event(["Sales", "Admin", "Emea"]), None)["response"]

    assert response["claimsOverrideDetails"]["groupOverrideDetails"]["groupsToOverride"] == [
        "Sales",
        "Emea",
        "Admin",
    ]


def test_the_group_iam_roles_are_passed_through() -> None:
    """The override replaces the group configuration, IAM roles included."""
    roles = ["example-group-role"]
    event = _event(["Sales"], iamRolesToOverride=roles, preferredRole=roles[0])

    details = index.lambda_handler(event, None)["response"]["claimsOverrideDetails"]

    assert details["groupOverrideDetails"]["iamRolesToOverride"] == roles
    assert details["groupOverrideDetails"]["preferredRole"] == roles[0]


def test_no_iam_roles_are_added_when_the_groups_have_none() -> None:
    details = index.lambda_handler(_event(["Admin"]), None)["response"]["claimsOverrideDetails"]

    assert set(details["groupOverrideDetails"]) == {"groupsToOverride"}


@pytest.mark.parametrize(
    "trigger",
    [
        "TokenGeneration_HostedAuth",
        "TokenGeneration_Authentication",
        "TokenGeneration_NewPasswordChallenge",
        "TokenGeneration_AuthenticateDevice",
        "TokenGeneration_RefreshTokens",
    ],
)
def test_every_sign_in_and_refresh_flow_gets_the_role(trigger: str) -> None:
    """The browser extension uses managed login, the web UI SRP, every client refresh."""
    details = index.lambda_handler(_event(["Participant"], trigger), None)["response"][
        "claimsOverrideDetails"
    ]

    assert details["groupOverrideDetails"]["groupsToOverride"] == ["Participant"]


def test_an_event_without_a_group_configuration_is_a_user() -> None:
    event = {"version": "1", "triggerSource": "TokenGeneration_Authentication", "request": {}}

    details = index.lambda_handler(event, None)["response"]["claimsOverrideDetails"]

    assert details["groupOverrideDetails"]["groupsToOverride"] == ["User"]
    assert details["claimsToAddOrOverride"] == {"lma_role": "User"}


# ---------------------------------------------------------------------------
# Last activity: one conditional write per user per 15 minutes, never in the way
# ---------------------------------------------------------------------------


class _FakeDynamoDB:
    """Records update_item calls; raises ``error`` from each when one is set."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def update_item(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "UpdateItem")


@pytest.fixture
def table(monkeypatch):
    """A sign-in table name and a fake client."""
    fake = _FakeDynamoDB()
    monkeypatch.setattr(index, "TABLE", "lma-UserSignInTable")
    monkeypatch.setattr(index, "_dynamodb", fake)
    return fake


def _role_response(groups):
    return index.lambda_handler(_event(list(groups)), None)["response"]


def test_a_token_issue_records_the_last_activity_with_a_15_minute_condition(
    table,
) -> None:
    now = datetime(2026, 11, 25, 10, 0, 0, tzinfo=timezone.utc)
    index.record_last_active(_event([], "TokenGeneration_RefreshTokens"), now=now)

    (call,) = table.calls
    assert call["TableName"] == "lma-UserSignInTable"
    assert call["Key"] == {"UserSub": {"S": "a1b2"}}
    assert call["UpdateExpression"] == "SET LastActiveAt = :at"
    assert call["ConditionExpression"] == (
        "attribute_not_exists(LastActiveAt) OR LastActiveAt < :cutoff"
    )
    assert call["ExpressionAttributeValues"] == {
        ":at": {"S": "2026-11-25T10:00:00.000Z"},
        ":cutoff": {"S": "2026-11-25T09:45:00.000Z"},
    }


def test_the_handler_records_the_last_activity_on_a_token_refresh(table) -> None:
    index.lambda_handler(_event(["Admin"], "TokenGeneration_RefreshTokens"), None)

    assert len(table.calls) == 1


class _ConditionalTable:
    """One item's LastActiveAt, updated only when the trigger's condition holds."""

    def __init__(self) -> None:
        self.value: str | None = None
        self.writes: list[str] = []

    def update_item(self, **kwargs):
        values = kwargs["ExpressionAttributeValues"]
        if self.value is not None and not self.value < values[":cutoff"]["S"]:
            raise _client_error("ConditionalCheckFailedException")
        self.value = values[":at"]["S"]
        self.writes.append(self.value)
        return {}


def test_tokens_within_15_minutes_write_the_time_once(monkeypatch) -> None:
    fake = _ConditionalTable()
    monkeypatch.setattr(index, "TABLE", "lma-UserSignInTable")
    monkeypatch.setattr(index, "_dynamodb", fake)
    start = datetime(2026, 11, 25, 10, 0, 0, tzinfo=timezone.utc)

    for minutes in (0, 5, 14, 16, 30, 32):
        index.record_last_active(_event([]), now=start + timedelta(minutes=minutes))

    assert fake.writes == [
        "2026-11-25T10:00:00.000Z",
        "2026-11-25T10:16:00.000Z",
        "2026-11-25T10:32:00.000Z",
    ]


def test_a_time_recorded_within_the_interval_is_left_quietly(table, caplog) -> None:
    table.error = _client_error("ConditionalCheckFailedException")

    response = _role_response(["Admin"])

    assert len(table.calls) == 1
    assert response["claimsOverrideDetails"]["claimsToAddOrOverride"] == {"lma_role": "Admin"}
    assert "Could not record" not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        _client_error("AccessDeniedException"),
        _client_error("ResourceNotFoundException"),
        RuntimeError("connect timeout"),
    ],
    ids=["denied", "no-table", "timeout"],
)
def test_a_failed_write_leaves_the_role_response_unchanged(table, error) -> None:
    for groups in (["Admin"], ["Participant"], [], ["Sales", "User"]):
        table.error = None
        expected = _role_response(groups)
        table.error = error

        assert _role_response(groups) == expected


def test_a_client_that_cannot_be_created_leaves_the_response_unchanged(
    monkeypatch,
) -> None:
    monkeypatch.setattr(index, "TABLE", "lma-UserSignInTable")
    monkeypatch.setattr(index, "_dynamodb", None)

    def broken(*_args, **_kwargs):
        raise RuntimeError("no region")

    monkeypatch.setattr(index.boto3, "client", broken)

    response = index.lambda_handler(_event(["Participant"]), None)["response"]

    assert response["claimsOverrideDetails"]["claimsToAddOrOverride"] == {"lma_role": "Participant"}


def test_no_table_or_no_sub_makes_no_call(table, monkeypatch) -> None:
    event = _event([])
    event["request"]["userAttributes"] = {}
    index.lambda_handler(event, None)
    monkeypatch.setattr(index, "TABLE", "")
    index.lambda_handler(_event([]), None)

    assert table.calls == []


def test_the_write_is_a_single_bounded_attempt() -> None:
    """One attempt of at most 1.5 seconds, well inside the five Cognito waits."""
    config = index.DYNAMODB_CONFIG

    assert config.retries["total_max_attempts"] == 1
    assert config.connect_timeout + config.read_timeout <= 1.5
