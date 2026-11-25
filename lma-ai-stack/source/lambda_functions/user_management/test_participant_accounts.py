# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for getMeetingParticipantAccounts and createParticipantAccounts (no AWS).

These two operations are the only ones in this resolver a User may call, so what
is pinned here is what keeps them narrow:

* the caller must own the meeting (an Admin may name any), whether the meeting is
  named by its CallId or by the Virtual Participant that will create it, and a
  meeting that does not exist reads like one the caller does not own;
* only addresses on that meeting's participant list are looked up or created;
* only missing accounts are created, always in the Participant group, never in
  another, and an existing account is never changed;
* the answer about an account is "none" or its role;
* a User's daily limit and the per-request limit, and AllowedSignUpEmailDomain.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("USER_POOL_ID", "us-east-1_testpool")


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "user_management_index_participants", Path(__file__).resolve().parent / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


with mock.patch("boto3.client"):
    index = _load_index()

OWNER = "owner@example.com"
OTHER = "other@example.com"
CALL_ID = "call-1"
VP_ID = "vp-1"
LISTED = [
    {"name": "Ana Lima", "email": "ana@example.com"},
    {"name": "", "email": "Bo@Example.org"},
    {"name": "Cy", "email": "cy@example.net"},
    {"name": "Di", "email": "di@example.com"},
]


class _CognitoError(ClientError):
    def __init__(self, code: str = "InternalErrorException") -> None:
        super().__init__({"Error": {"Code": code, "Message": code}}, "CognitoOperation")


class UsernameExistsException(_CognitoError):
    pass


class UserNotFoundException(_CognitoError):
    pass


class FakeCognito:
    """A user pool whose usernames compare ignoring case, as LMA's does."""

    def __init__(self) -> None:
        # username -> {"email", "groups"}
        self.users: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.add_to_group_error: ClientError | None = None
        self.exceptions = SimpleNamespace(
            UsernameExistsException=UsernameExistsException,
            UserNotFoundException=UserNotFoundException,
        )

    def add(self, username: str, *, email: str | None = None, groups=()) -> None:
        self.users[username] = {"email": email or username, "groups": list(groups)}

    def _key(self, username: str) -> str | None:
        return next((u for u in self.users if u.casefold() == username.casefold()), None)

    def names_called(self) -> list[str]:
        return [name for name, _ in self.calls]

    def admin_get_user(self, **kwargs):
        self.calls.append(("admin_get_user", kwargs))
        key = self._key(kwargs["Username"])
        if key is None:
            raise UserNotFoundException("UserNotFoundException")
        return {"Username": key}

    def list_users(self, **kwargs):
        self.calls.append(("list_users", kwargs))
        wanted = kwargs["Filter"].split('"')[1]
        return {"Users": [{"Username": u} for u, v in self.users.items() if v["email"] == wanted]}

    def admin_list_groups_for_user(self, **kwargs):
        self.calls.append(("admin_list_groups_for_user", kwargs))
        groups = self.users[self._key(kwargs["Username"])]["groups"]
        return {"Groups": [{"GroupName": g} for g in groups]}

    def admin_create_user(self, **kwargs):
        self.calls.append(("admin_create_user", kwargs))
        if self._key(kwargs["Username"]) is not None:
            raise UsernameExistsException("UsernameExistsException")
        self.add(kwargs["Username"])
        return {}

    def admin_add_user_to_group(self, **kwargs):
        self.calls.append(("admin_add_user_to_group", kwargs))
        if self.add_to_group_error is not None:
            raise self.add_to_group_error
        self.users[self._key(kwargs["Username"])]["groups"].append(kwargs["GroupName"])
        return {}

    def admin_delete_user(self, **kwargs):
        self.calls.append(("admin_delete_user", kwargs))
        self.users.pop(self._key(kwargs["Username"]), None)
        return {}


def _participants_attr(people):
    return {"L": [{"M": {"name": {"S": p["name"]}, "email": {"S": p["email"]}}} for p in people]}


class FakeDynamo:
    def __init__(self) -> None:
        self.meetings: dict[str, dict] = {}
        self.vps: dict[str, dict] = {}
        self.counts: dict[tuple, int] = {}
        self.updates: list[dict] = []

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        if kwargs["TableName"] == index.EVENT_SOURCING_TABLE and key["PK"]["S"].startswith("c#"):
            item = self.meetings.get(key["PK"]["S"][2:])
        elif kwargs["TableName"] == index.EVENT_SOURCING_TABLE:
            count = self.counts.get((key["PK"]["S"], key["SK"]["S"]))
            item = None if count is None else {"CreatedCount": {"N": str(count)}}
        else:
            item = self.vps.get(key["id"]["S"])
        return {"Item": item} if item else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        key = (kwargs["Key"]["PK"]["S"], kwargs["Key"]["SK"]["S"])
        values = kwargs["ExpressionAttributeValues"]
        current = self.counts.get(key)
        if "ConditionExpression" in kwargs:
            limit = int(values[":limit"]["N"])
            if current is not None and current >= limit:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
            self.counts[key] = (current or 0) + 1
        else:
            self.counts[key] = (current or 0) + int(values[":minus"]["N"])
        return {}


@pytest.fixture(name="pool")
def pool_fixture(monkeypatch):
    fake = FakeCognito()
    monkeypatch.setattr(index, "cognito", fake)
    return fake


@pytest.fixture(name="ddb")
def ddb_fixture(monkeypatch):
    fake = FakeDynamo()
    fake.meetings[CALL_ID] = {"Owner": {"S": OWNER}, "Participants": _participants_attr(LISTED)}
    fake.vps[VP_ID] = {"Owner": {"S": OWNER}, "participants": _participants_attr(LISTED[:2])}
    monkeypatch.setattr(index, "EVENT_SOURCING_TABLE", "events")
    monkeypatch.setattr(index, "VIRTUAL_PARTICIPANT_TABLE", "vps")
    monkeypatch.setattr(index.boto3, "client", lambda *_a, **_k: fake)
    monkeypatch.setattr(index, "ALLOWED_SIGNUP_EMAIL_DOMAINS", "")
    return fake


def event(field, arguments, *, username=OWNER, groups=("User",)):
    return {
        "info": {"fieldName": field},
        "identity": {"claims": {"cognito:username": username, "cognito:groups": list(groups)}},
        "arguments": arguments,
    }


def accounts(arguments, **caller):
    return index.handler(event("getMeetingParticipantAccounts", arguments, **caller), None)


def create(payload, **caller):
    return index.handler(event("createParticipantAccounts", {"input": payload}, **caller), None)


# ── who may ask ──────────────────────────────────────────────────────────────


def test_the_owner_reads_each_listed_persons_account(pool, ddb):
    pool.add("ana@example.com", groups=["Participant"])
    pool.add("legacy-name", email="bo@example.org")  # username is not the address
    pool.add("cy@example.net", groups=["Admin", "Participant"])
    answer = accounts({"CallId": CALL_ID})
    assert answer == [
        {"name": "Ana Lima", "email": "ana@example.com", "account": "Participant"},
        {"name": "", "email": "Bo@Example.org", "account": "User"},
        {"name": "Cy", "email": "cy@example.net", "account": "Admin"},
        {"name": "Di", "email": "di@example.com", "account": "none"},
    ]


def test_the_answer_carries_nothing_else_about_an_account(pool, ddb):
    pool.add("ana@example.com", groups=["Participant"])
    for entry in accounts({"CallId": CALL_ID}):
        assert set(entry) == {"name", "email", "account"}


def test_a_virtual_participant_s_list_is_read_before_it_has_a_meeting(pool, ddb):
    answer = accounts({"VirtualParticipantId": VP_ID})
    assert [entry["email"] for entry in answer] == ["ana@example.com", "Bo@Example.org"]


@pytest.mark.parametrize("arguments", [{"CallId": CALL_ID}, {"VirtualParticipantId": VP_ID}])
def test_a_user_who_does_not_own_the_meeting_is_refused(pool, ddb, arguments):
    with pytest.raises(Exception, match="Unauthorized"):
        accounts(arguments, username=OTHER)
    with pytest.raises(Exception, match="Unauthorized"):
        create(arguments, username=OTHER)
    assert pool.calls == []


def test_a_missing_meeting_reads_to_a_user_like_one_they_do_not_own(pool, ddb):
    with pytest.raises(Exception, match="Unauthorized"):
        accounts({"CallId": "no-such-meeting"})
    with pytest.raises(Exception, match="no such meeting"):
        accounts({"CallId": "no-such-meeting"}, username="admin@example.com", groups=["Admin"])


def test_an_admin_may_read_and_create_for_any_meeting(pool, ddb):
    admin = {"username": "admin@example.com", "groups": ["Admin"]}
    assert len(accounts({"CallId": CALL_ID}, **admin)) == 4
    result = create({"CallId": CALL_ID, "emails": ["di@example.com"]}, **admin)
    assert result["created"] == 1 and result["remainingToday"] is None
    assert ddb.updates == [], "an Admin is not counted against a daily limit"


def test_a_participant_is_refused_before_anything_is_read(pool, ddb):
    for field, arguments in (
        ("getMeetingParticipantAccounts", {"CallId": CALL_ID}),
        ("createParticipantAccounts", {"input": {"CallId": CALL_ID}}),
    ):
        with pytest.raises(Exception, match="Unauthorized"):
            index.handler(event(field, arguments, username=OWNER, groups=["Participant"]), None)
    assert pool.calls == []


@pytest.mark.parametrize(
    "arguments", [{}, {"CallId": CALL_ID, "VirtualParticipantId": VP_ID}, {"CallId": " "}]
)
def test_exactly_one_meeting_must_be_named(pool, ddb, arguments):
    with pytest.raises(Exception, match="exactly one"):
        accounts(arguments)


# ── what is created ──────────────────────────────────────────────────────────


def test_accounts_are_created_in_the_participant_group_only_for_those_without_one(pool, ddb):
    pool.add("ana@example.com", groups=["Admin"])
    pool.add("cy@example.net")
    result = create({"CallId": CALL_ID})
    outcomes = {r["email"]: r["outcome"] for r in result["results"]}
    assert outcomes == {
        "ana@example.com": "exists",
        "Bo@Example.org": "created",
        "cy@example.net": "exists",
        "di@example.com": "created",
    }
    assert result["created"] == 2
    groups = [kw["GroupName"] for name, kw in pool.calls if name == "admin_add_user_to_group"]
    assert groups == ["Participant", "Participant"]
    # Existing accounts keep exactly their groups.
    assert pool.users["ana@example.com"]["groups"] == ["Admin"]
    assert pool.users["cy@example.net"]["groups"] == []
    created = [kw for name, kw in pool.calls if name == "admin_create_user"]
    assert created[0]["DesiredDeliveryMediums"] == ["EMAIL"]
    assert {"Name": "email", "Value": "Bo@Example.org"} in created[0]["UserAttributes"]


def test_only_listed_addresses_may_be_named(pool, ddb):
    with pytest.raises(Exception, match="participant list"):
        create({"CallId": CALL_ID, "emails": ["ana@example.com", "stranger@example.com"]})
    assert "admin_create_user" not in pool.names_called()


def test_a_listed_address_is_matched_ignoring_case(pool, ddb):
    result = create({"CallId": CALL_ID, "emails": ["bo@example.ORG", "BO@example.org"]})
    assert [(r["email"], r["outcome"]) for r in result["results"]] == [
        ("Bo@Example.org", "created")
    ]


def test_an_account_made_between_the_lookup_and_the_create_is_reported_as_existing(
    pool, ddb, monkeypatch
):
    monkeypatch.setattr(index, "_accounts_of", lambda emails: {e: "none" for e in emails})
    pool.add("ana@example.com", groups=["Admin"])
    result = create({"CallId": CALL_ID, "emails": ["ana@example.com"]})
    assert result["results"][0]["outcome"] == "exists"
    assert pool.users["ana@example.com"]["groups"] == ["Admin"]
    assert sum(ddb.counts.values()) == 0, "the slot reserved for it is given back"


def test_a_new_account_whose_group_cannot_be_set_is_removed(pool, ddb):
    pool.add_to_group_error = _CognitoError()
    result = create({"CallId": CALL_ID, "emails": ["di@example.com"]})
    assert result["results"][0]["outcome"] == "failed"
    assert "di@example.com" not in pool.users
    assert result["created"] == 0


def test_addresses_outside_the_allowed_domains_are_not_created(pool, ddb, monkeypatch):
    monkeypatch.setattr(index, "ALLOWED_SIGNUP_EMAIL_DOMAINS", "example.com")
    result = create({"CallId": CALL_ID})
    outcomes = {r["email"]: r["outcome"] for r in result["results"]}
    assert outcomes["Bo@Example.org"] == "domain_not_allowed"
    assert outcomes["cy@example.net"] == "domain_not_allowed"
    assert outcomes["ana@example.com"] == "created"
    assert set(pool.users) == {"ana@example.com", "di@example.com"}


# ── limits ───────────────────────────────────────────────────────────────────


def test_a_user_s_daily_limit_stops_creation_and_is_reported(pool, ddb, monkeypatch):
    monkeypatch.setattr(index, "PARTICIPANT_ACCOUNTS_PER_DAY", 2)
    result = create({"CallId": CALL_ID})
    assert [r["outcome"] for r in result["results"]] == [
        "created",
        "created",
        "limit_reached",
        "limit_reached",
    ]
    assert result["remainingToday"] == 0
    assert len(pool.users) == 2
    # The count is the caller's, for today, and expires.
    (update, *_rest) = ddb.updates
    assert update["Key"]["PK"]["S"] == f"usr#{OWNER}"
    assert update["Key"]["SK"]["S"].startswith("participant-accounts#")
    assert ":ttl" in update["ExpressionAttributeValues"]


def test_one_request_creates_at_most_the_per_request_limit(pool, ddb, monkeypatch):
    monkeypatch.setattr(index, "PARTICIPANT_ACCOUNTS_PER_REQUEST", 1)
    result = create({"CallId": CALL_ID}, username="admin@example.com", groups=["Admin"])
    assert [r["outcome"] for r in result["results"]] == [
        "created",
        "limit_reached",
        "limit_reached",
        "limit_reached",
    ]


def test_the_remaining_daily_count_is_returned_to_a_user(pool, ddb):
    result = create({"CallId": CALL_ID, "emails": ["di@example.com"]})
    assert result["remainingToday"] == index.PARTICIPANT_ACCOUNTS_PER_DAY - 1
