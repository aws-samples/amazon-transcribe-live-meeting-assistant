# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Who the meeting tools answer: getCall's rule, and owner-or-Admin where it narrows."""

import importlib

import pytest

from tools import access
from tools._meeting_fakes import ADMIN, OTHER, OWNER, SHARED, FakeTable, caller, meeting_row


@pytest.mark.parametrize(
    "shared_with",
    [[SHARED], {SHARED}, f"[{SHARED}]", f"{OTHER}, {SHARED}", f"[{OTHER}, {SHARED}]"],
)
def test_a_user_the_meeting_is_shared_with_reads_it_in_every_stored_shape(shared_with):
    row = meeting_row(SharedWith=shared_with)
    assert access.can_read(row, caller(SHARED))
    assert not access.can_manage(row, caller(SHARED))


def test_the_owner_and_admins_read_and_manage_and_nobody_else_does():
    row = meeting_row()
    assert access.can_read(row, caller(OWNER)) and access.can_manage(row, caller(OWNER))
    assert access.can_read(row, caller(ADMIN, admin=True))
    assert access.can_manage(row, caller(ADMIN, admin=True))
    assert not access.can_read(row, caller(OTHER))
    assert not access.can_manage(row, caller(OTHER))
    assert not access.can_read(row, caller(""))


def test_a_shared_entry_is_matched_whole():
    row = meeting_row(SharedWith=f"[x{SHARED}]")
    assert not access.can_read(row, caller(SHARED))


def test_a_missing_meeting_and_a_hidden_one_are_refused_alike(calls_table):
    calls_table.put(meeting_row("m1"))
    with pytest.raises(PermissionError) as hidden:
        access.require_meeting("m1", caller(OTHER))
    with pytest.raises(PermissionError) as missing:
        access.require_meeting("m2", caller(OTHER))
    assert str(hidden.value).replace("m1", "X") == str(missing.value).replace("m2", "X")


def test_a_meeting_id_is_required(calls_table):
    for value in (None, "", "  ", 7):
        with pytest.raises(ValueError):
            access.require_meeting(value, caller())


def test_list_keys_are_the_ones_create_call_wrote():
    keys = access.list_keys(meeting_row("abc", CreatedAt="2026-11-19T13:45:00.000Z"))
    assert keys == {
        "ListPK": "cls#2026-11-19#s#03",
        "ListSK": "ts#2026-11-19T13:45:00.000Z#id#abc",
    }
    assert access.list_keys(meeting_row(CreatedAt="2026-11-19T03:00:00Z"))["ListPK"].endswith(
        "#s#00"
    )
    assert access.list_keys(meeting_row(CreatedAt="2026-11-19T23:59:00Z"))["ListPK"].endswith(
        "#s#05"
    )


@pytest.mark.parametrize(
    "stored,expected",
    [
        ("read-write", "read-write"),
        ("read", "read"),
        (None, "legacy"),
        ("admin", "read"),
        ("", "legacy"),
    ],
)
def test_a_key_without_a_scope_is_legacy_and_an_unknown_one_read(stored, expected):
    assert access.normalize_scope(stored) == expected


def test_a_resolver_is_given_the_caller_as_a_cognito_identity():
    assert access.resolver_identity(caller(OWNER)) == {"username": OWNER, "groups": []}
    assert access.resolver_identity(caller(ADMIN, admin=True))["groups"] == ["Admin"]


@pytest.mark.parametrize("module_name", ["get_summary", "get_transcript"])
def test_the_summary_and_transcript_tools_answer_shared_users(monkeypatch, module_name):
    module = importlib.import_module(f"tools.{module_name}")
    table = FakeTable()
    table.put(meeting_row())

    class Resource:
        def Table(self, _name):  # noqa: N802 - boto3's method name
            return table

    monkeypatch.setattr(module.boto3, "resource", lambda *_a, **_k: Resource())
    assert module.can_access_meeting("m1", SHARED, False)
    assert module.can_access_meeting("m1", OWNER, False)
    assert not module.can_access_meeting("m1", OTHER, False)


def test_list_meetings_answers_shared_users(monkeypatch):
    from tools import list_meetings

    rows = [meeting_row("m1"), meeting_row("m2", Owner=OTHER, SharedWith=[])]
    # The walk decides who may see a row; this stand-in applies the test list_meetings gives it.
    monkeypatch.setattr(
        list_meetings,
        "walk_date_range",
        lambda _table, _start, _end, _limit, include=None, _projection=None: (
            list_meetings.WalkResult(
                [r for r in rows if include is None or include(r)], 0, False, False
            )
        ),
    )
    monkeypatch.setattr(list_meetings.boto3, "resource", lambda *_a, **_k: Resource_any())
    listed = list_meetings.execute(user_id=SHARED)
    assert [m["meetingId"] for m in listed["meetings"]] == ["m1"]


class Resource_any:  # noqa: N801 - a stand-in for boto3's resource
    def Table(self, _name):  # noqa: N802 - boto3's method name
        return FakeTable()
