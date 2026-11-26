#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Tests for the names an account goes by in meetings (no AWS).

What matters: the names are kept within their limits and without characters a
participant list entry cannot hold; they count only for the account they were
kept for; and the search entries follow every change, so a name removed is no
longer found.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from lma_consent import account_names as names

TABLE = "events"


def test_names_are_trimmed_and_repeats_kept_once():
    display, shorts = names.clean_names(
        "  Robert   Chen ", ["Bob", " bob ", "ROBERT chen", "Bobby"]
    )
    assert display == "Robert Chen"
    assert shorts == ["Bob", "Bobby"]


@pytest.mark.parametrize(
    "display,shorts",
    [
        ("a" * 101, []),
        ("Ok", ["b" * 51]),
        ("Ok", ["a1", "b2", "c3", "d4", "e5", "f6"]),
        ("Robert <bob@example.com>", []),
        ("Ok", ["line\nbreak"]),
        ("tab\there", []),
        (5, []),
        ("Ok", "Bob"),
    ],
)
def test_names_outside_the_limits_are_refused(display, shorts):
    with pytest.raises(names.InvalidNames):
        names.clean_names(display, shorts)


def test_five_different_short_names_are_kept():
    assert len(names.clean_names("", ["a1", "b2", "c3", "d4", "e5", "A1"])[1]) == 5


def test_a_search_needs_three_characters_and_no_filter_characters():
    assert names.search_text("  Rob ") == "rob"
    for text in ("ro", "  r ", None, 'ro"b', "ro\\b", "ro<b"):
        with pytest.raises(names.InvalidNames):
            names.search_text(text)


def test_search_entries_are_keyed_by_the_first_three_characters():
    entries = names.index_entries("Bo@Example.com", "Robert Chen", ["Bob", "Bo"])
    keys = {(e["PK"]["S"], e["SK"]["S"]) for e in entries}
    assert keys == {("acn#rob", "robert chen\tbo@example.com"), ("acn#bob", "bob\tbo@example.com")}
    assert all(e["DisplayName"]["S"] == "Robert Chen" for e in entries)


def test_names_count_only_for_the_account_they_were_kept_for():
    profile = {"DisplayName": "Robert Chen", "ShortNames": ["Bob"], "Subject": "sub-1"}
    assert names.account_names(profile, {"sub": "sub-1", "name": "R. Chen"}) == {
        "display_name": "Robert Chen",
        "short_names": ["Bob"],
    }
    assert names.account_names(profile, {"sub": "sub-2", "name": "Someone"}) == {
        "display_name": "Someone",
        "short_names": [],
    }
    assert names.account_names(profile, None) is None


def test_the_user_pool_name_stands_in_for_a_missing_display_name():
    profile = {"DisplayName": "", "ShortNames": ["Bob"], "Subject": "sub-1"}
    answer = names.account_names(profile, {"sub": "sub-1", "name": "Robert Chen"})
    assert answer == {"display_name": "Robert Chen", "short_names": ["Bob"]}


class FakeClient:
    """The table as a dict of (PK, SK) to item, with the calls these functions make."""

    class exceptions:  # noqa: N801 - mirrors botocore's client.exceptions
        class TransactionCanceledException(Exception):
            pass

    def __init__(self) -> None:
        self.items: Dict[tuple, Dict[str, Any]] = {}
        self.cancel_next = 0
        self.cancel_code = "ConditionalCheckFailed"
        self.writes = 0

    @staticmethod
    def _key(key):
        return (key["PK"]["S"], key["SK"]["S"])

    def get_item(self, TableName, Key, **_kw):  # noqa: N803
        item = self.items.get(self._key(Key))
        return {"Item": item} if item else {}

    def delete_item(self, TableName, Key):  # noqa: N803
        self.items.pop(self._key(Key), None)

    def transact_write_items(self, TransactItems):  # noqa: N803
        if self.cancel_next:
            self.cancel_next -= 1
            error = self.exceptions.TransactionCanceledException()
            error.response = {"CancellationReasons": [{"Code": self.cancel_code}]}
            raise error
        self.writes += 1
        for entry in TransactItems:
            if "Put" in entry:
                item = entry["Put"]["Item"]
                self.items[self._key(item)] = item
            else:
                self.items.pop(self._key(entry["Delete"]["Key"]), None)

    def batch_get_item(self, RequestItems):  # noqa: N803
        keys = RequestItems[TABLE]["Keys"]
        found = [self.items[self._key(k)] for k in keys if self._key(k) in self.items]
        return {"Responses": {TABLE: found}}

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues, Limit):  # noqa: N803
        pk = ExpressionAttributeValues[":pk"]["S"]
        prefix = ExpressionAttributeValues[":prefix"]["S"]
        rows = sorted(k for k in self.items if k[0] == pk and k[1].startswith(prefix))
        return {"Items": [self.items[k] for k in rows][:Limit]}


def save(client, email="bo@example.com", display="Robert Chen", shorts=("Bob",), sub="sub-1"):
    return names.save_profile(
        client,
        TABLE,
        email=email,
        display_name=display,
        short_names=list(shorts),
        subject=sub,
        updated_by="admin@example.com",
        now=f"2026-11-26T10:00:{client.writes:02d}Z",
    )


def test_a_name_removed_is_no_longer_found():
    client = FakeClient()
    save(client)
    assert names.search_index(client, TABLE, "bob", 10) == [
        {"email": "bo@example.com", "name": "Robert Chen"}
    ]
    save(client, display="Rob Chen", shorts=())
    assert names.search_index(client, TABLE, "bob", 10) == []
    assert names.search_index(client, TABLE, "rob", 10) == [
        {"email": "bo@example.com", "name": "Rob Chen"}
    ]
    profiles = names.read_profiles(client, TABLE, ["BO@example.com"])
    assert profiles["bo@example.com"]["ShortNames"] == []


def test_clearing_every_name_removes_the_names_and_their_entries():
    client = FakeClient()
    save(client)
    save(client, display="", shorts=())
    assert client.items == {}


def test_deleting_the_names_removes_their_search_entries():
    client = FakeClient()
    save(client)
    names.delete_profile(client, TABLE, "Bo@example.com")
    assert client.items == {}


def test_a_search_answers_each_address_once():
    client = FakeClient()
    save(client, display="Robert Chen", shorts=["Robbie"])
    save(client, email="ro@example.com", display="Rowan Diaz", shorts=())
    assert names.search_index(client, TABLE, "rob", 10) == [
        {"email": "bo@example.com", "name": "Robert Chen"}
    ]


def test_a_conflicting_change_is_retried_then_reported():
    client = FakeClient()
    client.cancel_next = 1
    assert save(client)["DisplayName"] == "Robert Chen"
    client.cancel_next = names.WRITE_ATTEMPTS
    with pytest.raises(names.NamesChanged):
        save(client, display="Other")


def test_a_write_cancelled_for_another_reason_is_not_reported_as_a_conflict():
    client = FakeClient()
    client.cancel_next, client.cancel_code = 1, "ThrottlingError"
    with pytest.raises(names.NamesNotSaved, match="could not be saved"):
        save(client)
