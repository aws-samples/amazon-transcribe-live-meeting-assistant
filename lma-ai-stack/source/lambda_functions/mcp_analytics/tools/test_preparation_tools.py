# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting preparation tools: context, checklist, checklist items, labels and audience.

Each is answered for the meeting's owner, a user it is shared with and an Admin,
refused for anyone else before anything is sent, and checked with the meeting
preparation rules before the write.
"""

import pytest

from tools import preparation
from tools._meeting_fakes import ADMIN, OTHER, OWNER, SHARED, caller, meeting_row

CHECKLIST = [
    {
        "ItemId": "q1",
        "Text": "Who owns the budget?",
        "Kind": "question",
        "Priority": "must",
        "Source": "ref-1",
        "Status": "covered",
        "StatusSource": "assistant",
        "Evidence": [{"SegmentId": "s1", "StartTime": 64, "Quote": "Dana owns it"}],
        "Note": "confirmed",
        "UpdatedBy": "LMA",
        "UpdatedAt": "2026-11-19T14:00:00Z",
    }
]

CALLERS = [caller(OWNER), caller(SHARED), caller(ADMIN, admin=True)]


@pytest.fixture
def meeting(calls_table):
    calls_table.put(
        meeting_row(
            MeetingContext="# Goals",
            MeetingContextUpdatedBy=OWNER,
            MeetingContextUpdatedAt="2026-11-19T09:00:00Z",
            Checklist=CHECKLIST,
            ChecklistUpdatedAt="2026-11-19T14:00:00Z",
            Labels=[{"Key": "project", "Value": "relaunch"}],
            Audience="external",
        )
    )
    return calls_table


# ── reads ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", CALLERS)
def test_owner_shared_user_and_admin_read_the_preparation(meeting, who):
    assert preparation.get_context(who, "m1") == {
        "meetingId": "m1",
        "context": "# Goals",
        "updatedBy": OWNER,
        "updatedAt": "2026-11-19T09:00:00Z",
    }
    checklist = preparation.get_checklist(who, "m1")
    assert checklist["updatedAt"] == "2026-11-19T14:00:00Z"
    assert checklist["items"] == [
        {
            "itemId": "q1",
            "text": "Who owns the budget?",
            "kind": "question",
            "priority": "must",
            "source": "ref-1",
            "status": "covered",
            "statusSource": "assistant",
            "evidence": [{"segmentId": "s1", "startTime": 64.0, "quote": "Dana owns it"}],
            "note": "confirmed",
            "updatedBy": "LMA",
            "updatedAt": "2026-11-19T14:00:00Z",
        }
    ]


def test_another_user_is_refused_every_tool_and_nothing_is_sent(meeting, appsync):
    other = caller(OTHER)
    for call in (
        lambda: preparation.get_context(other, "m1"),
        lambda: preparation.set_context(other, "m1", "x"),
        lambda: preparation.get_checklist(other, "m1"),
        lambda: preparation.set_checklist(other, "m1", []),
        lambda: preparation.update_item(other, "m1", "q1", status="open"),
        lambda: preparation.update_metadata(other, "m1", audience="internal"),
    ):
        with pytest.raises(PermissionError, match="not found, or you do not have access"):
            call()
    assert appsync.calls == []


def test_a_meeting_without_preparation_reads_empty(calls_table):
    calls_table.put(meeting_row())
    assert preparation.get_context(caller(), "m1")["context"] == ""
    assert preparation.get_checklist(caller(), "m1") == {
        "meetingId": "m1",
        "items": [],
        "updatedAt": None,
    }


# ── set_meeting_context ─────────────────────────────────────────────────────


@pytest.mark.parametrize("who", CALLERS)
def test_the_context_is_written_for_the_caller(meeting, appsync, who):
    appsync.answers["updateCallMeetingContext"] = {
        "updateCallMeetingContext": {
            "MeetingContext": "New",
            "MeetingContextUpdatedBy": who.user_id,
            "MeetingContextUpdatedAt": "2026-11-20T10:00:00Z",
        }
    }
    answer = preparation.set_context(who, "m1", "New")
    query, variables = appsync.calls[0]
    assert "mutation UpdateCallMeetingContext" in query
    assert variables["input"] == {"CallId": "m1", "MeetingContext": "New", "UpdatedBy": who.user_id}
    assert answer == {
        "meetingId": "m1",
        "context": "New",
        "updatedBy": who.user_id,
        "updatedAt": "2026-11-20T10:00:00Z",
    }
    # The write selects what onUpdateCall subscribers need.
    assert "Owner" in query and "SharedWith" in query


def test_an_empty_context_clears_it_and_a_long_or_missing_one_is_refused(meeting, appsync):
    preparation.set_context(caller(), "m1", "")
    assert appsync.calls[0][1]["input"]["MeetingContext"] == ""
    with pytest.raises(ValueError, match="20000"):
        preparation.set_context(caller(), "m1", "x" * 20001)
    with pytest.raises(ValueError, match="context must be a string"):
        preparation.set_context(caller(), "m1", None)
    assert len(appsync.calls) == 1


# ── set_meeting_checklist ───────────────────────────────────────────────────


def test_the_checklist_is_replaced_as_the_users_with_camel_case_items(meeting, appsync):
    appsync.answers["updateCallChecklist"] = {
        "updateCallChecklist": {"Checklist": CHECKLIST, "ChecklistUpdatedAt": "2026-11-20T10:00Z"}
    }
    answer = preparation.set_checklist(
        caller(SHARED),
        "m1",
        [
            {
                "itemId": "q1",
                "text": "Who owns the budget?",
                "kind": "question",
                "priority": "must",
            },
            {"itemId": "t1", "text": "Hiring", "kind": "topic", "source": "doc#3"},
        ],
    )
    query, variables = appsync.calls[0]
    assert "mutation UpdateCallChecklist" in query
    assert variables["input"] == {
        "CallId": "m1",
        "Checklist": [
            {
                "ItemId": "q1",
                "Text": "Who owns the budget?",
                "Kind": "question",
                "Priority": "must",
            },
            {
                "ItemId": "t1",
                "Text": "Hiring",
                "Kind": "topic",
                "Priority": "should",
                "Source": "doc#3",
            },
        ],
        "ResetStatuses": False,
        "StatusSource": "user",
        "UpdatedBy": SHARED,
    }
    assert answer["items"][0]["itemId"] == "q1" and answer["updatedAt"] == "2026-11-20T10:00Z"


def test_reset_statuses_is_passed_through(meeting, appsync):
    preparation.set_checklist(caller(), "m1", [], reset_statuses=True)
    assert appsync.calls[0][1]["input"]["ResetStatuses"] is True


@pytest.mark.parametrize(
    "items,message",
    [
        (None, "items is required"),
        ("q1", "must be a list"),
        ([{"itemId": "bad id", "text": "x", "kind": "topic"}], "ItemId"),
        ([{"itemId": "a", "text": "", "kind": "topic"}], "Text is required"),
        ([{"itemId": "a", "text": "x", "kind": "chat"}], "Kind must be one of"),
        ([{"itemId": "a", "text": "x", "kind": "topic", "priority": "high"}], "Priority"),
        ([{"itemId": "a", "text": "x" * 501, "kind": "topic"}], "500"),
        ([{"itemId": "a", "text": "x", "kind": "topic", "status": "covered"}], "unknown fields"),
        ([{"itemId": "a", "text": "x", "kind": "topic"}] * 2, "more than one item"),
        ([{"itemId": f"i{n}", "text": "x", "kind": "topic"} for n in range(101)], "100"),
    ],
)
def test_a_checklist_that_breaks_a_limit_is_refused_before_anything_is_sent(
    meeting, appsync, items, message
):
    with pytest.raises(ValueError, match=message):
        preparation.set_checklist(caller(), "m1", items)
    assert appsync.calls == []


# ── update_checklist_item ───────────────────────────────────────────────────


def test_an_item_status_is_set_as_the_users(meeting, appsync):
    changed = dict(CHECKLIST[0], Status="partial", StatusSource="user", UpdatedBy=SHARED)
    appsync.answers["updateCallChecklistItem"] = {
        "updateCallChecklistItem": {"Checklist": [changed]}
    }
    answer = preparation.update_item(caller(SHARED), "m1", "q1", status="partial", note="")
    query, variables = appsync.calls[0]
    assert "mutation UpdateCallChecklistItem" in query
    assert variables["input"] == {
        "CallId": "m1",
        "ItemId": "q1",
        "Status": "partial",
        "Note": "",
        "StatusSource": "user",
        "UpdatedBy": SHARED,
    }
    assert answer["itemId"] == "q1" and answer["status"] == "partial"
    assert answer["statusSource"] == "user"


@pytest.mark.parametrize(
    "item_id,kwargs,message",
    [
        ("q1", {}, "status or a note"),
        ("q1", {"status": "done"}, "status must be one of"),
        ("q1", {"status": ""}, "status must be one of"),
        ("q1", {"note": "x" * 1001}, "1000"),
        ("q1", {"note": 5}, "note must be a string"),
        ("", {"status": "open"}, "itemId is required"),
        ("zz", {"status": "open"}, "has no item zz"),
    ],
)
def test_an_item_update_that_breaks_a_rule_is_refused(meeting, appsync, item_id, kwargs, message):
    with pytest.raises(ValueError, match=message):
        preparation.update_item(caller(), "m1", item_id, **kwargs)
    assert appsync.calls == []


# ── update_meeting_metadata ─────────────────────────────────────────────────


def test_labels_and_audience_are_written_with_their_own_mutations(meeting, appsync):
    appsync.answers["updateCallLabels"] = {
        "updateCallLabels": {"Labels": [{"Key": "team", "Value": "web"}], "Audience": "external"}
    }
    appsync.answers["updateCallAudience"] = {
        "updateCallAudience": {
            "Labels": [{"Key": "team", "Value": "web"}],
            "Audience": "internal",
            "MeetingMetadataUpdatedAt": "2026-11-20T10:00:00Z",
        }
    }
    answer = preparation.update_metadata(
        caller(SHARED), "m1", labels={"team": "web"}, audience="Internal"
    )
    (labels_query, labels_vars), (audience_query, audience_vars) = appsync.calls
    assert "mutation UpdateCallLabels" in labels_query
    assert labels_vars["input"] == {"CallId": "m1", "Labels": [{"Key": "team", "Value": "web"}]}
    assert "mutation UpdateCallAudience" in audience_query
    assert audience_vars["input"] == {"CallId": "m1", "Audience": "internal"}
    assert answer == {
        "meetingId": "m1",
        "labels": {"team": "web"},
        "audience": "internal",
        "updatedAt": "2026-11-20T10:00:00Z",
    }


def test_only_the_input_given_is_written_and_empty_values_clear(meeting, appsync):
    preparation.update_metadata(caller(), "m1", labels={})
    assert [q for q, _ in appsync.calls if "UpdateCallAudience" in q] == []
    assert appsync.calls[0][1]["input"]["Labels"] == []
    preparation.update_metadata(caller(), "m1", audience="")
    assert appsync.calls[1][1]["input"] == {"CallId": "m1", "Audience": None}


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({}, "Give labels, audience or both"),
        ({"labels": [{"Key": "a", "Value": "b"}]}, "object"),
        ({"labels": {"Bad Key": "x"}}, "label Key"),
        ({"labels": {"a": 5}}, "must have a Value"),
        ({"labels": {"a": "x" * 257}}, "256"),
        ({"labels": {f"k{n}": "v" for n in range(21)}}, "20 labels"),
        ({"audience": "everyone"}, "Audience must be one of"),
    ],
)
def test_metadata_that_breaks_a_limit_is_refused(meeting, appsync, kwargs, message):
    with pytest.raises(ValueError, match=message):
        preparation.update_metadata(caller(), "m1", **kwargs)
    assert appsync.calls == []


def test_a_failed_audience_write_after_the_labels_says_the_labels_changed(meeting, appsync):
    appsync.answers["updateCallAudience"] = ValueError("throttled")
    with pytest.raises(ValueError, match="labels were changed"):
        preparation.update_metadata(caller(), "m1", labels={"a": "b"}, audience="internal")
