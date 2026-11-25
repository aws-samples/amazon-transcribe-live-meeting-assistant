# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the last sign-in time the user management resolver reports (no AWS).

The Cognito stack's PostAuthentication trigger writes each user's last sign-in
time to a table keyed by Cognito sub. ``listUsers`` joins those times onto the
users it lists, and these tests pin what an access review relies on:

* each user gets the time recorded under their own sub, and a user with no item
  gets null (the page shows "Not recorded");
* when the table cannot be read, or the stack has none, every time is null and
  ``lastSignInAvailable`` is false, so "not recorded" is never claimed for a time
  that was not read;
* the table is read in BatchGetItem's chunks of 100 keys, and keys DynamoDB
  leaves unprocessed are asked for again;
* deleting a user also deletes their sign-in item, and a failure there does not
  fail the deletion.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("USER_POOL_ID", "us-east-1_testpool")

TABLE = "LMA-COGNITOSTACK-UserSignInTable"


def _load_index():
    """This directory's index.py under a name of its own (every Lambda has an index.py)."""
    spec = importlib.util.spec_from_file_location(
        "user_management_index_last_sign_in", Path(__file__).resolve().parent / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


with mock.patch("boto3.client"):
    index = _load_index()


def _denied(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, operation)


class FakeDynamo:
    """The two calls the resolver makes on the sign-in table."""

    def __init__(self, times: dict[str, str]) -> None:
        self.times = times
        # Last-active times, by sub, written by the token and API key paths.
        self.active: dict[str, str] = {}
        self.batch_requests: list[dict] = []
        self.deleted: list[dict] = []
        self.error: Exception | None = None
        # How many times a request leaves its last key unprocessed.
        self.unprocessed_rounds = 0

    def batch_get_item(self, RequestItems):  # noqa: N803 - boto3's name
        self.batch_requests.append(RequestItems)
        if self.error is not None:
            raise self.error
        keys = RequestItems[TABLE]["Keys"]
        unprocessed = {}
        if self.unprocessed_rounds:
            self.unprocessed_rounds -= 1
            unprocessed = {TABLE: {**RequestItems[TABLE], "Keys": keys[-1:]}}
            keys = keys[:-1]
        items = []
        for key in keys:
            sub = key["UserSub"]["S"]
            if sub not in self.times and sub not in self.active:
                continue
            item = {"UserSub": {"S": sub}}
            if sub in self.times:
                item["LastSignInAt"] = {"S": self.times[sub]}
            if sub in self.active:
                item["LastActiveAt"] = {"S": self.active[sub]}
            items.append(item)
        return {"Responses": {TABLE: items}, "UnprocessedKeys": unprocessed}

    def delete_item(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.deleted.append(kwargs)
        return {}


class FakeCognito:
    """ListUsers pages, group lookups, AdminGetUser and AdminDeleteUser."""

    def __init__(self, users: list[dict]) -> None:
        self.users = users
        self.admins = {"admin@example.com", "other-admin@example.com"}
        self.deleted: list[str] = []
        self.exceptions = mock.Mock(UserNotFoundException=type("UNF", (ClientError,), {}))

    def get_paginator(self, name):
        cognito = self

        class _Pages:
            def paginate(self, **_kwargs):
                if name == "list_users":
                    return [{"Users": cognito.users}]
                return [{"Users": [{"Username": a} for a in cognito.admins]}]

        return _Pages()

    def admin_list_groups_for_user(self, Username, **_kwargs):  # noqa: N803
        return {"Groups": [{"GroupName": "Admin"}] if Username in self.admins else []}

    def admin_get_user(self, Username, **_kwargs):  # noqa: N803
        user = next(u for u in self.users if u["Username"] == Username)
        return {**user, "UserAttributes": user["Attributes"]}

    def admin_delete_user(self, Username, **_kwargs):  # noqa: N803
        self.deleted.append(Username)
        return {}


def user(name: str, sub: str) -> dict:
    return {
        "Username": name,
        "UserStatus": "CONFIRMED",
        "Enabled": True,
        "UserCreateDate": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "Attributes": [{"Name": "email", "Value": name}, {"Name": "sub", "Value": sub}],
    }


@pytest.fixture(name="env")
def env_fixture(monkeypatch: pytest.MonkeyPatch):
    users = [user("admin@example.com", "sub-admin"), user("new@example.com", "sub-new")]
    cognito = FakeCognito(users)
    dynamo = FakeDynamo({"sub-admin": "2026-11-20T09:30:00.000Z"})
    monkeypatch.setattr(index, "cognito", cognito)
    monkeypatch.setattr(index, "USER_SIGN_IN_TABLE", TABLE)
    monkeypatch.setattr(index.boto3, "client", lambda service, **_kw: dynamo)
    monkeypatch.setattr(index.time, "sleep", lambda _s: None)
    return cognito, dynamo


def admin_event(field: str, arguments: dict | None = None) -> dict:
    return {
        "info": {"fieldName": field},
        "identity": {
            "claims": {"cognito:username": "admin@example.com", "cognito:groups": ["Admin"]}
        },
        "arguments": arguments or {},
    }


def by_name(result: dict) -> dict:
    return {u["username"]: u for u in result["users"]}


def test_each_user_gets_the_time_recorded_under_their_own_sub(env) -> None:
    result = index.handler(admin_event("listUsers"), None)

    users = by_name(result)
    assert result["lastSignInAvailable"] is True
    assert users["admin@example.com"]["lastSignInAt"] == "2026-11-20T09:30:00.000Z"
    # Never signed in since the trigger was deployed: null, shown as "Not recorded".
    assert users["new@example.com"]["lastSignInAt"] is None


def test_each_user_gets_their_own_last_active_time(env) -> None:
    _cognito, dynamo = env
    dynamo.active = {"sub-admin": "2026-11-24T16:45:00.000Z"}

    users = by_name(index.handler(admin_event("listUsers"), None))

    assert users["admin@example.com"]["lastActiveAt"] == "2026-11-24T16:45:00.000Z"
    assert users["admin@example.com"]["lastSignInAt"] == "2026-11-20T09:30:00.000Z"
    assert users["new@example.com"]["lastActiveAt"] is None


def test_a_user_active_only_through_an_api_key_has_no_sign_in_time(env) -> None:
    """An item holding only LastActiveAt (the authorizer wrote it) is still read."""
    _cognito, dynamo = env
    dynamo.active = {"sub-new": "2026-11-24T17:00:00.000Z"}

    users = by_name(index.handler(admin_event("listUsers"), None))

    assert users["new@example.com"]["lastActiveAt"] == "2026-11-24T17:00:00.000Z"
    assert users["new@example.com"]["lastSignInAt"] is None


def test_both_times_are_read_in_one_projection(env) -> None:
    _cognito, dynamo = env

    index.handler(admin_event("listUsers"), None)

    (request,) = dynamo.batch_requests
    assert request[TABLE]["ProjectionExpression"] == "UserSub, LastSignInAt, LastActiveAt"


def test_the_table_is_asked_only_for_the_listed_users_subs(env) -> None:
    _cognito, dynamo = env

    index.handler(admin_event("listUsers"), None)

    (request,) = dynamo.batch_requests
    assert sorted(k["UserSub"]["S"] for k in request[TABLE]["Keys"]) == ["sub-admin", "sub-new"]


def test_times_are_unavailable_when_the_table_cannot_be_read(env) -> None:
    _cognito, dynamo = env
    dynamo.error = _denied("BatchGetItem")

    result = index.handler(admin_event("listUsers"), None)

    assert result["lastSignInAvailable"] is False
    assert [u["lastSignInAt"] for u in result["users"]] == [None, None]
    assert [u["lastActiveAt"] for u in result["users"]] == [None, None]
    # The listing itself still succeeds.
    assert len(result["users"]) == 2


def test_times_are_unavailable_when_the_stack_has_no_sign_in_table(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cognito, dynamo = env
    monkeypatch.setattr(index, "USER_SIGN_IN_TABLE", "")

    result = index.handler(admin_event("listUsers"), None)

    assert result["lastSignInAvailable"] is False
    assert dynamo.batch_requests == []


def test_keys_left_unprocessed_are_asked_for_again(env) -> None:
    _cognito, dynamo = env
    dynamo.times["sub-new"] = "2026-11-21T08:00:00.000Z"
    dynamo.unprocessed_rounds = 1

    result = index.handler(admin_event("listUsers"), None)

    assert len(dynamo.batch_requests) == 2
    assert by_name(result)["new@example.com"]["lastSignInAt"] == "2026-11-21T08:00:00.000Z"
    assert result["lastSignInAvailable"] is True


def test_a_table_that_keeps_leaving_keys_unprocessed_makes_the_times_unavailable(env) -> None:
    _cognito, dynamo = env
    dynamo.unprocessed_rounds = 100

    result = index.handler(admin_event("listUsers"), None)

    assert result["lastSignInAvailable"] is False
    assert len(dynamo.batch_requests) == index.UNPROCESSED_RETRIES + 1


def test_the_table_is_read_in_chunks_of_one_hundred_keys(env) -> None:
    cognito, dynamo = env
    cognito.users = [user(f"u{i}@example.com", f"sub-{i:03d}") for i in range(250)]
    dynamo.times = {"sub-249": "2026-11-22T10:00:00.000Z"}

    result = index.handler(admin_event("listUsers"), None)

    assert [len(r[TABLE]["Keys"]) for r in dynamo.batch_requests] == [100, 100, 50]
    assert by_name(result)["u249@example.com"]["lastSignInAt"] == "2026-11-22T10:00:00.000Z"


def test_a_created_user_has_no_last_sign_in(env) -> None:
    """createUser returns the same User shape, with nothing recorded yet."""
    created = index._format_cognito_user(user("x@example.com", "sub-x"))
    assert created["lastSignInAt"] is None
    assert created["lastActiveAt"] is None


def test_deleting_a_user_deletes_their_sign_in_item(env) -> None:
    cognito, dynamo = env

    result = index.handler(
        admin_event("deleteUser", {"input": {"username": "new@example.com"}}), None
    )

    assert result == {"username": "new@example.com", "success": True}
    assert cognito.deleted == ["new@example.com"]
    assert dynamo.deleted == [{"TableName": TABLE, "Key": {"UserSub": {"S": "sub-new"}}}]


def test_a_failed_sign_in_item_delete_does_not_fail_the_user_deletion(env) -> None:
    cognito, dynamo = env
    dynamo.error = _denied("DeleteItem")

    result = index.handler(
        admin_event("deleteUser", {"input": {"username": "new@example.com"}}), None
    )

    assert result["success"] is True
    assert cognito.deleted == ["new@example.com"]
