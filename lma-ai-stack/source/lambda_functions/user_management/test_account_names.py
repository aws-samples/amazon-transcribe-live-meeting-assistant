# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the names accounts go by in meetings, and the account search (no AWS).

What is pinned here:

* an Admin sets any account's display name and short names, when creating it or
  later, and they are kept with that account's sub under its address; invalid
  names are refused before anything is created;
* a new Participant account made from a meeting's list goes by the name the
  list gives it;
* the names of a deleted account are removed with it;
* the account search answers only Admins and Users, needs three characters,
  matches the start of a display name, short name or address, answers at most
  ten accounts with their display name and address only, leaves out disabled
  accounts, and counts a User's searches a day.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("USER_POOL_ID", "us-east-1_testpool")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))


def _load_index():
    spec = importlib.util.spec_from_file_location("user_management_index_names", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


with mock.patch("boto3.client"):
    index = _load_index()

ADMIN = "admin@example.com"
USER = "user@example.com"


class _CognitoError(ClientError):
    def __init__(self, code: str = "InternalErrorException") -> None:
        super().__init__({"Error": {"Code": code, "Message": code}}, "CognitoOperation")


class UserNotFoundException(_CognitoError):
    pass


class UsernameExistsException(_CognitoError):
    pass


class FakeCognito:
    """Accounts by username, each with its attributes; ListUsers filters by = and ^=."""

    def __init__(self) -> None:
        self.users: dict[str, dict] = {}
        self.filters: list[str] = []
        self.exceptions = SimpleNamespace(
            UserNotFoundException=UserNotFoundException,
            UsernameExistsException=UsernameExistsException,
        )

    def add(self, email, name="", enabled=True, verified=True):
        self.users[email] = {
            "Username": email,
            "Enabled": enabled,
            "UserStatus": "CONFIRMED",
            "Attributes": [
                {"Name": "sub", "Value": f"sub-{email}"},
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true" if verified else "false"},
            ]
            + ([{"Name": "name", "Value": name}] if name else []),
        }

    def _as_get(self, user):
        out = copy.deepcopy(user)
        out["UserAttributes"] = out.pop("Attributes")
        return out

    def admin_get_user(self, UserPoolId, Username):  # noqa: N803
        assert UserPoolId == index.USER_POOL_ID
        if Username not in self.users:
            raise UserNotFoundException()
        return self._as_get(self.users[Username])

    def list_users(self, UserPoolId, Filter, Limit):  # noqa: N803
        self.filters.append(Filter)
        attribute, operator, value = re.fullmatch(r'(\w+) (\^?=) "(.*)"', Filter).groups()
        found = []
        for user in self.users.values():
            values = {a["Name"]: a["Value"] for a in user["Attributes"]}
            have = values.get(attribute) or ""
            if have == value if operator == "=" else have.startswith(value):
                found.append(copy.deepcopy(user))
        return {"Users": found[:Limit]}

    def admin_create_user(self, **kwargs):
        if kwargs.get("MessageAction") == "RESEND":
            return {}
        if kwargs["Username"] in self.users:
            raise UsernameExistsException()
        self.add(kwargs["Username"])
        return {}

    def admin_add_user_to_group(self, **_kwargs):
        return {}

    def admin_list_groups_for_user(self, **_kwargs):
        return {"Groups": []}

    def admin_delete_user(self, Username, **_kwargs):  # noqa: N803
        self.users.pop(Username, None)

    def get_paginator(self, name):
        if name == "list_users":
            return SimpleNamespace(
                paginate=lambda **_k: [{"Users": [copy.deepcopy(u) for u in self.users.values()]}]
            )
        return SimpleNamespace(
            paginate=lambda **_k: [{"Users": [{"Username": ADMIN}, {"Username": "x"}]}]
        )


class FakeDynamo:
    """The meetings table as a dict of (PK, SK) to item, with the calls made on it."""

    class exceptions:  # noqa: N801
        class TransactionCanceledException(Exception):
            pass

    def __init__(self) -> None:
        self.items: dict[tuple, dict] = {}

    @staticmethod
    def _key(key):
        return (key["PK"]["S"], key["SK"]["S"])

    def get_item(self, TableName, Key, **_kwargs):  # noqa: N803
        item = self.items.get(self._key(Key))
        return {"Item": copy.deepcopy(item)} if item else {}

    def delete_item(self, TableName, Key):  # noqa: N803
        self.items.pop(self._key(Key), None)

    def transact_write_items(self, TransactItems):  # noqa: N803
        for entry in TransactItems:
            if "Put" in entry:
                self.items[self._key(entry["Put"]["Item"])] = copy.deepcopy(entry["Put"]["Item"])
            else:
                self.items.pop(self._key(entry["Delete"]["Key"]), None)

    def batch_get_item(self, RequestItems):  # noqa: N803
        [(table, request)] = RequestItems.items()
        found = [self.items[self._key(k)] for k in request["Keys"] if self._key(k) in self.items]
        return {"Responses": {table: copy.deepcopy(found)}}

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues, **_kw):  # noqa: N803
        pk = ExpressionAttributeValues[":pk"]["S"]
        prefix = ExpressionAttributeValues[":prefix"]["S"]
        rows = sorted(k for k in self.items if k[0] == pk and k[1].startswith(prefix))
        return {"Items": [copy.deepcopy(self.items[k]) for k in rows]}

    def update_item(self, TableName, Key, ExpressionAttributeValues, **_kwargs):  # noqa: N803
        key = self._key(Key)
        item = self.items.setdefault(key, {"PK": Key["PK"], "SK": Key["SK"]})
        count = int(item.get("SearchCount", {"N": "0"})["N"])
        if count >= int(ExpressionAttributeValues[":limit"]["N"]):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        item["SearchCount"] = {"N": str(count + 1)}
        return {}


@pytest.fixture(name="aws")
def aws_fixture(monkeypatch):
    cognito, dynamo = FakeCognito(), FakeDynamo()
    for email in (ADMIN, USER):
        cognito.add(email)
    monkeypatch.setattr(index, "cognito", cognito)
    monkeypatch.setattr(index, "EVENT_SOURCING_TABLE", "events")
    monkeypatch.setattr(index, "USER_SIGN_IN_TABLE", "")
    monkeypatch.setattr(index.boto3, "client", lambda *_a, **_k: dynamo)
    # The deleted user's notification preferences and consents, which other tests cover.
    table = SimpleNamespace(delete_item=lambda **_k: None, query=lambda **_k: {"Items": []})
    resource = SimpleNamespace(Table=lambda _name: table)
    monkeypatch.setattr(index.boto3, "resource", lambda *_a, **_k: resource)
    monkeypatch.setattr(index, "_advance_consent_enabled", lambda: False)
    return SimpleNamespace(cognito=cognito, dynamo=dynamo)


def event(field, arguments=None, *, username=ADMIN, groups=("Admin",)):
    return {
        "info": {"fieldName": field},
        "identity": {"claims": {"cognito:username": username, "cognito:groups": list(groups)}},
        "arguments": arguments or {},
    }


def profile(aws, email):
    return aws.dynamo.items.get((f"pmc#{email}", "account-names"))


def search(text, **kwargs):
    return index.handler(event("searchAccounts", {"text": text}, **kwargs), None)


# ── an Admin sets the names ─────────────────────────────────────────────────


def test_an_admin_creates_a_user_with_names_kept_for_the_new_account(aws) -> None:
    created = index.handler(
        event(
            "createUser",
            {
                "input": {
                    "email": "new@example.com",
                    "role": "Participant",
                    "displayName": "Nia Okafor",
                    "shortNames": ["Nia", "nia"],
                }
            },
        ),
        None,
    )
    assert (created["displayName"], created["shortNames"]) == ("Nia Okafor", ["Nia"])
    kept = profile(aws, "new@example.com")
    assert kept["Subject"]["S"] == "sub-new@example.com" and kept["UpdatedBy"]["S"] == ADMIN


def test_invalid_names_are_refused_before_the_user_is_created(aws) -> None:
    with pytest.raises(Exception, match="At most 5"):
        index.handler(
            event(
                "createUser",
                {
                    "input": {
                        "email": "new@example.com",
                        "role": "User",
                        "shortNames": list("abcdef"),
                    }
                },
            ),
            None,
        )
    assert "new@example.com" not in aws.cognito.users


def test_an_admin_sets_any_users_names(aws) -> None:
    answer = index.handler(
        event("setUserAccountNames", {"input": {"username": USER, "displayName": "Uma Patel"}}),
        None,
    )
    assert answer["displayName"] == "Uma Patel" and answer["email"] == USER
    assert profile(aws, USER)["Subject"]["S"] == f"sub-{USER}"
    listed = {u["email"]: u for u in index.handler(event("listUsers"), None)["users"]}
    assert listed[USER]["displayName"] == "Uma Patel"
    assert listed[ADMIN]["displayName"] is None


@pytest.mark.parametrize("groups", [(), ("Participant",)])
def test_only_an_admin_sets_another_users_names(aws, groups) -> None:
    with pytest.raises(Exception, match="Unauthorized"):
        index.handler(
            event(
                "setUserAccountNames",
                {"input": {"username": ADMIN, "displayName": "Someone Else"}},
                username=USER,
                groups=groups,
            ),
            None,
        )
    assert profile(aws, ADMIN) is None


def test_names_kept_for_an_earlier_account_with_the_address_are_not_listed(aws) -> None:
    index.handler(
        event("setUserAccountNames", {"input": {"username": USER, "displayName": "Uma Patel"}}),
        None,
    )
    aws.cognito.users[USER]["Attributes"][0]["Value"] = "sub-of-a-new-account"
    listed = {u["email"]: u for u in index.handler(event("listUsers"), None)["users"]}
    assert listed[USER]["displayName"] is None


def test_a_deleted_users_names_are_removed_with_them(aws) -> None:
    index.handler(
        event(
            "setUserAccountNames",
            {"input": {"username": USER, "displayName": "Uma Patel", "shortNames": ["Uma"]}},
        ),
        None,
    )
    index.handler(event("deleteUser", {"input": {"username": USER}}), None)
    assert not [k for k in aws.dynamo.items if USER in k[0] or USER in k[1]]


def test_a_participant_account_made_from_the_list_goes_by_the_lists_name(aws, monkeypatch) -> None:
    meeting = {
        "owner": ADMIN,
        "participants": [{"name": "Lee Wong", "email": "lee@example.com"}],
        "name": "Weekly",
        "listed_on": ("call", "call-1"),
    }
    monkeypatch.setattr(index, "_meeting_for_caller", lambda caller, payload: meeting)
    monkeypatch.setattr(index, "_reserve_lookups", lambda *a: None)
    answer = index.handler(
        event("createParticipantAccounts", {"input": {"CallId": "call-1"}}), None
    )
    assert answer["created"] == 1
    assert profile(aws, "lee@example.com")["DisplayName"]["S"] == "Lee Wong"


# ── the account search ──────────────────────────────────────────────────────


@pytest.fixture(name="directory")
def directory_fixture(aws):
    for email, name in (
        ("robert.chen@example.com", ""),
        ("rosa@example.com", "Rosa Diaz"),
        ("off@example.com", "Robin Off"),
        ("unverified@example.com", "Robin Unverified"),
    ):
        aws.cognito.add(email, name=name)
    aws.cognito.users["off@example.com"]["Enabled"] = False
    aws.cognito.users["unverified@example.com"]["Attributes"][2]["Value"] = "false"
    index.handler(
        event(
            "setUserAccountNames",
            {
                "input": {
                    "username": "robert.chen@example.com",
                    "displayName": "Robert Chen",
                    "shortNames": ["Bobby"],
                }
            },
        ),
        None,
    )
    return aws


def test_an_account_is_found_by_the_start_of_its_display_name(directory) -> None:
    assert search("rob") == [{"name": "Robert Chen", "email": "robert.chen@example.com"}]


def test_an_account_is_found_by_the_start_of_a_short_name(directory) -> None:
    assert search("BOBB") == [{"name": "Robert Chen", "email": "robert.chen@example.com"}]


def test_an_account_is_found_by_the_start_of_its_address(directory) -> None:
    assert search("rosa@") == [{"name": "Rosa Diaz", "email": "rosa@example.com"}]


def test_the_middle_of_a_name_finds_nothing(directory) -> None:
    assert search("chen") == []


def test_disabled_and_unverified_accounts_are_not_found(directory) -> None:
    assert search("robin") == []


def test_a_search_answers_at_most_ten_accounts_with_their_name_and_address(aws) -> None:
    for n in range(15):
        aws.cognito.add(f"team{n:02d}@example.com", name=f"Team Member {n:02d}")
    found = search("team")
    assert len(found) == 10
    assert all(set(entry) == {"name", "email"} for entry in found)


@pytest.mark.parametrize("text", ["ro", "  r  ", 'ro"b', ""])
def test_a_search_needs_three_characters(aws, text) -> None:
    with pytest.raises(Exception):
        search(text)
    assert aws.cognito.filters == []


def test_a_participant_cannot_search_accounts(aws) -> None:
    with pytest.raises(Exception, match="Unauthorized"):
        search("rob", username="guest@example.com", groups=("Participant",))
    assert aws.cognito.filters == []


def test_a_user_searches_a_limited_number_of_times_a_day(aws, monkeypatch) -> None:
    monkeypatch.setattr(index, "ACCOUNT_SEARCHES_PER_DAY", 2)
    search("rob", username=USER, groups=())
    search("ros", username=USER, groups=())
    with pytest.raises(Exception, match="the most a day"):
        search("rob", username=USER, groups=())
    # An Admin's searches are not counted.
    for _ in range(3):
        search("rob")
