# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Unit tests for client.users: the deployment's users for an access review.

The AWS clients are stand-ins: a user pool of three users across two ListUsers
pages, an Admin group, and the Cognito stack's sign-in table keyed by Cognito
sub. What is asserted is that every user is listed with their role, status,
enabled flag, creation time and recorded last sign-in; that a user with no
sign-in item has none; and that when the table cannot be found or read the users
are still listed, with the times marked unavailable and the reason given.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from botocore.exceptions import ClientError

from lma_sdk.exceptions import LMAConfigurationError
from lma_sdk.operations import users as user_ops

TABLE = "test-lma-stack-COGNITOSTACK-UserSignInTable"


def _user(name: str, sub: str, *, status="CONFIRMED", enabled=True, created=None) -> Dict[str, Any]:
    return {
        "Username": name,
        "UserStatus": status,
        "Enabled": enabled,
        "UserCreateDate": created or datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        "Attributes": [{"Name": "sub", "Value": sub}, {"Name": "email", "Value": name}],
    }


def _denied(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, operation)


class FakeCognito:
    def __init__(self) -> None:
        self.pages = [
            [_user("zed@example.com", "sub-zed"), _user("admin@example.com", "sub-admin")],
            [_user("new@example.com", "sub-new", status="FORCE_CHANGE_PASSWORD", enabled=False)],
        ]
        self.admins = ["admin@example.com"]
        self.participants: List[str] = []
        self.list_error: Exception | None = None

    def list_users(self, **kwargs):
        if self.list_error is not None:
            raise self.list_error
        index = int(kwargs.get("PaginationToken") or 0)
        response: Dict[str, Any] = {"Users": self.pages[index]}
        if index + 1 < len(self.pages):
            response["PaginationToken"] = str(index + 1)
        return response

    def list_users_in_group(self, **kwargs):
        members = {"Admin": self.admins, "Participant": self.participants}[kwargs["GroupName"]]
        return {"Users": [{"Username": name} for name in members]}


class FakeDynamo:
    def __init__(self) -> None:
        self.times = {"sub-admin": "2026-11-20T09:30:00.000Z", "sub-zed": "2026-10-01T08:00:00.000Z"}
        self.requests: List[Dict[str, Any]] = []
        self.error: Exception | None = None
        self.unprocessed_rounds = 0

    def batch_get_item(self, RequestItems):  # noqa: N803 - boto3's name
        self.requests.append(RequestItems)
        if self.error is not None:
            raise self.error
        keys = RequestItems[TABLE]["Keys"]
        unprocessed: Dict[str, Any] = {}
        if self.unprocessed_rounds:
            self.unprocessed_rounds -= 1
            unprocessed = {TABLE: {**RequestItems[TABLE], "Keys": keys[-1:]}}
            keys = keys[:-1]
        items = [
            {"UserSub": key["UserSub"], "LastSignInAt": {"S": self.times[key["UserSub"]["S"]]}}
            for key in keys
            if key["UserSub"]["S"] in self.times
        ]
        return {"Responses": {TABLE: items}, "UnprocessedKeys": unprocessed}


class FakeCfn:
    def __init__(self) -> None:
        self.has_table = True
        self.error: Exception | None = None

    def describe_stack_resource(self, StackName, LogicalResourceId):  # noqa: N803
        if self.error is not None:
            raise self.error
        if LogicalResourceId == "UserSignInTable" and not self.has_table:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationError",
                        "Message": f"Resource UserSignInTable does not exist for stack {StackName}",
                    }
                },
                "DescribeStackResource",
            )
        ids = {"COGNITOSTACK": "test-lma-stack-COGNITOSTACK-nested", "UserSignInTable": TABLE}
        return {"StackResourceDetail": {"PhysicalResourceId": ids[LogicalResourceId]}}


@pytest.fixture
def aws(client, monkeypatch):
    fakes = SimpleNamespace(cognito=FakeCognito(), dynamodb=FakeDynamo(), cloudformation=FakeCfn())
    services = {
        "cognito-idp": fakes.cognito,
        "dynamodb": fakes.dynamodb,
        "cloudformation": fakes.cloudformation,
        "sts": SimpleNamespace(get_caller_identity=lambda: {"Account": "111122223333"}),
    }
    client.session.client.side_effect = lambda name, **_kw: services[name]
    client._vp_ops = SimpleNamespace(user_pool_id="us-east-1_pool")
    monkeypatch.setattr(user_ops.time, "sleep", lambda _s: None)
    return fakes


def _by_email(result):
    return {user.email: user for user in result.users}


def test_every_user_is_listed_with_role_status_enabled_created_and_last_sign_in(client, aws):
    result = client.users.list()

    assert [u.email for u in result.users] == ["admin@example.com", "new@example.com", "zed@example.com"]
    users = _by_email(result)
    assert users["admin@example.com"].role == "Admin"
    assert users["admin@example.com"].last_sign_in_at == "2026-11-20T09:30:00.000Z"
    assert users["zed@example.com"].role == "User"
    assert users["new@example.com"].status == "FORCE_CHANGE_PASSWORD"
    assert users["new@example.com"].enabled is False
    assert users["new@example.com"].created_at == "2026-01-02T03:04:05.000Z"
    assert result.last_sign_in_available is True


def test_a_user_who_has_not_signed_in_since_recording_began_has_no_time(client, aws):
    users = _by_email(client.users.list())

    assert users["new@example.com"].last_sign_in_at is None


def test_the_result_names_where_it_was_read(client, aws):
    result = client.users.list()

    assert (result.stack, result.region, result.account, result.user_pool_id) == (
        "test-lma-stack",
        "us-east-1",
        "111122223333",
        "us-east-1_pool",
    )
    assert result.generated_at.endswith("Z")


def test_the_table_is_asked_for_each_users_sub_once(client, aws):
    client.users.list()

    (request,) = aws.dynamodb.requests
    assert sorted(k["UserSub"]["S"] for k in request[TABLE]["Keys"]) == ["sub-admin", "sub-new", "sub-zed"]


def test_keys_left_unprocessed_are_asked_for_again(client, aws):
    aws.dynamodb.unprocessed_rounds = 1

    users = _by_email(client.users.list())

    assert len(aws.dynamodb.requests) == 2
    assert users["zed@example.com"].last_sign_in_at == "2026-10-01T08:00:00.000Z"


def test_a_stack_without_the_sign_in_table_lists_users_with_times_unavailable(client, aws):
    aws.cloudformation.has_table = False

    result = client.users.list()

    assert len(result.users) == 3
    assert result.last_sign_in_available is False
    assert "records no sign-in times" in result.last_sign_in_unavailable_reason
    assert all(u.last_sign_in_at is None for u in result.users)


def test_a_refused_table_read_names_the_permission_and_still_lists_users(client, aws):
    aws.dynamodb.error = _denied("BatchGetItem")

    result = client.users.list()

    assert len(result.users) == 3
    assert result.last_sign_in_available is False
    assert "dynamodb:BatchGetItem" in result.last_sign_in_unavailable_reason
    assert all(u.last_sign_in_at is None for u in result.users)


def test_a_refused_user_listing_is_an_error_naming_the_permission(client, aws):
    aws.cognito.list_error = _denied("ListUsers")

    with pytest.raises(LMAConfigurationError, match="cognito-idp:ListUsers"):
        client.users.list()


def test_cognito_timestamps_are_given_in_utc():
    naive = datetime(2026, 5, 6, 7, 8, 9)
    assert user_ops.iso_utc(naive) == "2026-05-06T07:08:09.000Z"
    assert user_ops.iso_utc(None) is None


def test_a_participant_is_listed_as_a_participant_unless_also_an_admin(client, aws):
    aws.cognito.participants = ["zed@example.com", "admin@example.com"]

    roles = {user.email: user.role for user in client.users.list().users}

    assert roles == {
        "admin@example.com": "Admin",
        "new@example.com": "User",
        "zed@example.com": "Participant",
    }
