#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Attendees matched by the names a listed person's LMA account goes by.

Besides the name a participant list entry gives a person, the account that
holds the listed address can carry a display name (or the user pool's standard
``name``) and short names. Each counts as one of that person's names, under the
same rule as the list's: only one listed person goes by it, and only one
attendee shows a name of that person. The record says which name matched,
never the address. No AWS.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_advance_consent as advance  # noqa: E402  pylint: disable=wrong-import-position

base = advance.base
index = advance.index
SER = advance.SER


class FakeCognito:
    """The user pool: an account per address, with its sub and standard name."""

    class exceptions:  # noqa: N801 - mirrors botocore's client.exceptions
        class UserNotFoundException(Exception):
            pass

    def __init__(self, accounts: Dict[str, Dict[str, str]]) -> None:
        self.accounts = accounts
        self.fail = False
        self.reads: List[str] = []

    def admin_get_user(self, UserPoolId, Username):  # noqa: N803
        self.reads.append(Username)
        if self.fail:
            raise base.ClientError({"Error": {"Code": "Throttling", "Message": "slow"}}, "Get")
        account = self.accounts.get(Username)
        if account is None:
            raise self.exceptions.UserNotFoundException()
        attributes = {"email": Username, "email_verified": "true", **account}
        return {
            "Username": Username,
            "Enabled": True,
            "UserAttributes": [{"Name": k, "Value": v} for k, v in attributes.items()],
        }

    def list_users(self, **_kw):
        return {"Users": []}


class NamedDynamo(advance.AdvanceDynamo):
    """And the names kept for each address (pmc#<address>, account-names)."""

    def __init__(self) -> None:
        super().__init__()
        self.profiles: Dict[str, Dict[str, Any]] = {}

    def name(self, email: str, display: str = "", shorts=(), subject: str = "") -> None:
        self.profiles[email] = {
            "PK": SER.serialize(f"pmc#{email}"),
            "SK": SER.serialize("account-names"),
            "DisplayName": SER.serialize(display),
            "ShortNames": SER.serialize(list(shorts)),
            "Subject": SER.serialize(subject or f"sub-{email}"),
            "UpdatedAt": SER.serialize(advance.iso(advance.NOW)),
        }

    def batch_get_item(self, RequestItems):  # noqa: N803
        [(table, request)] = RequestItems.items()
        found = [
            self.profiles[k["PK"]["S"][4:]]
            for k in request["Keys"]
            if k["PK"]["S"][4:] in self.profiles
        ]
        return {"Responses": {table: found}}


@pytest.fixture
def named(monkeypatch):
    fake = NamedDynamo()
    fake.put_meeting(
        Participants=[
            {"name": "", "email": "ana@example.com"},
            {"name": "Bo Chen", "email": "bo@example.com"},
            {"name": "", "email": "cy@example.com"},
        ],
        Status="TRANSCRIBING",
    )
    cognito = FakeCognito(
        {
            "ana@example.com": {"sub": "sub-ana@example.com"},
            "bo@example.com": {"sub": "sub-bo@example.com", "name": "Robert Chen"},
            "cy@example.com": {"sub": "sub-cy@example.com", "name": "Cy Diaz"},
        }
    )
    monkeypatch.setattr(index, "DYNAMODB", fake)
    monkeypatch.setattr(index, "COGNITO", cognito)
    monkeypatch.setattr(index, "USER_POOL_ID", "pool")
    monkeypatch.setattr(index, "_ACCOUNTS", {})
    monkeypatch.setattr(index, "VIRTUAL_PARTICIPANT_TABLE", "lma-vps")
    monkeypatch.setattr(index, "S3", base.FakeS3())
    monkeypatch.setattr(index, "_STORE", base.FakeStore())
    fake.cognito = cognito
    return fake


start = advance.start
present = advance.present


def test_a_list_entry_with_no_name_matches_by_the_accounts_display_name(named) -> None:
    given = named.give("ana@example.com")
    named.name("ana@example.com", display="Ana Silva")
    start()
    [event] = present("ana silva")["Events"]
    assert event["PreMeeting"]["ConsentId"] == given["ConsentId"]
    assert event["PreMeeting"]["MatchBasis"] == "display_name"
    assert event["PreMeeting"]["MatchedNameSource"] == "display_name"
    assert event["PreMeeting"]["ListedName"] is None
    assert "@" not in str(event)


def test_an_attendee_is_matched_by_a_short_name_of_the_account(named) -> None:
    named.give("ana@example.com")
    named.name("ana@example.com", display="Ana Silva", shorts=["Ani"])
    start()
    [event] = present("ANI")["Events"]
    assert event["PreMeeting"]["MatchedNameSource"] == "short_name"


def test_the_list_entrys_name_is_recorded_as_the_name_matched(named) -> None:
    named.give("bo@example.com")
    start()
    [event] = present("Bo Chen")["Events"]
    assert event["PreMeeting"]["MatchedNameSource"] == "list"


def test_the_user_pool_name_is_the_display_name_of_an_account_with_none(named) -> None:
    named.give("cy@example.com")
    start()
    [event] = present("Cy Diaz")["Events"]
    assert event["PreMeeting"]["MatchedNameSource"] == "display_name"


def test_names_kept_for_another_account_with_the_address_are_not_used(named) -> None:
    named.give("ana@example.com")
    named.name("ana@example.com", display="Ana Silva", subject="sub-of-an-earlier-account")
    start()
    assert present("Ana Silva")["Events"] == []


def test_a_name_two_listed_people_go_by_matches_nobody(named) -> None:
    named.give("ana@example.com")
    named.give("bo@example.com")
    named.name("ana@example.com", display="Ana Silva", shorts=["Bo Chen"])
    start()
    assert present("Bo Chen")["Events"] == []
    assert named.queries == []


def test_two_attendees_showing_two_names_of_one_person_match_nobody(named) -> None:
    named.give("bo@example.com")
    start()
    assert present("Bo Chen", "Robert Chen")["Events"] == []


def test_the_user_pool_is_asked_once_for_attendees_listed_again(named) -> None:
    named.give("bo@example.com")
    start()
    present("Somebody")
    present("Somebody", "Somebody Else")
    assert sorted(named.cognito.reads) == ["ana@example.com", "bo@example.com", "cy@example.com"]


def test_nothing_given_in_advance_is_counted_while_the_accounts_cannot_be_read(named) -> None:
    named.give("bo@example.com")
    named.cognito.fail = True
    start()
    assert present("Bo Chen")["Events"] == []
    # The next listing tries again.
    named.cognito.fail = False
    index._ACCOUNTS.clear()
    [event] = present("Bo Chen", "Somebody")["Events"]
    assert event["PreMeeting"]["MatchedNameSource"] == "list"


def test_a_second_name_of_a_person_already_counted_matches_nobody(named) -> None:
    named.give("bo@example.com")
    start()
    [first] = present("Bo Chen")["Events"]
    assert first["Source"] == "pre_meeting"
    # Later another attendee shows another name of the same listed person.
    record = present("Bo Chen", "Robert Chen")
    assert [e["ParticipantName"] for e in record["Events"]] == ["Bo Chen"]


def test_a_second_name_of_a_person_who_has_left_matches_nobody(named) -> None:
    named.give("bo@example.com")
    start()
    present("Bo Chen")
    record = present("Robert Chen")
    assert [e["ParticipantName"] for e in record["Events"]] == ["Bo Chen"]


def test_a_full_account_cache_is_pruned_safely_while_lookups_run_together(
    named, monkeypatch
) -> None:
    monkeypatch.setattr(index, "MAX_CACHED_ACCOUNTS", 50)
    for n in range(50):
        index._ACCOUNTS[f"old{n}@example.com"] = (0.0, None)
    emails = [f"p{n}@example.com" for n in range(40)] + ["bo@example.com"]
    answer = index.listed_account_names(emails)
    assert answer is not None and answer["bo@example.com"]["display_name"] == "Robert Chen"
    assert len(index._ACCOUNTS) <= 50
