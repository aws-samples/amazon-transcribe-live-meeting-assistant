# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The meeting whiteboard tools.

Each is answered for the meeting's owner, a user it is shared with and an Admin,
refused for anyone else before anything is sent, and checked with the whiteboard
rules before a write. A write is recorded as the caller's (Source user).
"""

import pytest

from tools import whiteboards
from tools._meeting_fakes import ADMIN, OTHER, OWNER, SHARED, caller, meeting_row

CALLERS = [caller(OWNER), caller(SHARED), caller(ADMIN, admin=True)]

BOARD = {
    "CallId": "m1",
    "WhiteboardId": "wb1",
    "Title": "Plan",
    "Template": "decisions",
    "Instructions": "",
    "AssistantUpdates": True,
    "Version": 3,
    "CreatedBy": OWNER,
    "CreatedAt": "2026-11-20T10:00:00Z",
    "UpdatedBy": "LMA assistant",
    "UpdatedAt": "2026-11-20T10:05:00Z",
    "Panels": [
        {
            "PanelId": "decisions",
            "Title": "Decisions",
            "Kind": "markdown",
            "Content": "- March",
            "Source": "assistant",
            "UpdatedBy": "LMA assistant",
            "UpdatedAt": "2026-11-20T10:05:00Z",
        },
    ],
}


@pytest.fixture
def meeting(calls_table):
    calls_table.put(meeting_row())
    return calls_table


@pytest.mark.parametrize("who", CALLERS)
def test_owner_shared_user_and_admin_list_and_read_whiteboards(meeting, appsync, who):
    appsync.answers["listMeetingWhiteboards"] = {
        "listMeetingWhiteboards": {
            "WhiteboardsEnabled": True,
            "AssistantAvailable": True,
            "MaxWhiteboards": 5,
            "Writable": True,
            "Items": [BOARD],
        }
    }
    listed = whiteboards.list_whiteboards(who, "m1")
    assert listed["assistantUpdates"] is True and listed["maxWhiteboards"] == 5
    assert (
        listed["whiteboards"][0]["whiteboardId"] == "wb1"
        and "panels" not in listed["whiteboards"][0]
    )
    appsync.answers.clear()
    appsync.answers["getMeetingWhiteboard"] = {"getMeetingWhiteboard": BOARD}
    board = whiteboards.get_whiteboard(who, "m1", "wb1")
    assert board["version"] == 3 and board["panels"][0]["content"] == "- March"


def test_another_user_is_refused_every_tool_and_nothing_is_sent(meeting, appsync):
    other = caller(OTHER)
    for call in (
        lambda: whiteboards.list_whiteboards(other, "m1"),
        lambda: whiteboards.get_whiteboard(other, "m1", "wb1"),
        lambda: whiteboards.create_whiteboard(other, "m1", {}),
        lambda: whiteboards.update_whiteboard(
            other, "m1", {"whiteboardId": "wb1", "baseVersion": 1, "title": "x"}
        ),
    ):
        with pytest.raises(PermissionError):
            call()
    assert appsync.calls == []


def test_polling_with_the_known_version_answers_not_modified(meeting, appsync):
    appsync.answers["getMeetingWhiteboard"] = {"getMeetingWhiteboard": BOARD}
    assert whiteboards.get_whiteboard(caller(OWNER), "m1", "wb1", if_newer_than=3) == {
        "meetingId": "m1",
        "whiteboardId": "wb1",
        "version": 3,
        "notModified": True,
    }
    assert whiteboards.get_whiteboard(caller(OWNER), "m1", "wb1", if_newer_than=2)["version"] == 3


def test_the_markdown_format_gives_the_whiteboard_as_one_document(meeting, appsync):
    appsync.answers["getMeetingWhiteboard"] = {"getMeetingWhiteboard": BOARD}
    board = whiteboards.get_whiteboard(caller(OWNER), "m1", "wb1", fmt="markdown")
    assert board["markdown"].startswith("# Plan\n") and "panels" not in board
    with pytest.raises(ValueError, match="format"):
        whiteboards.get_whiteboard(caller(OWNER), "m1", "wb1", fmt="pdf")


def test_an_earlier_version_is_read_by_number(meeting, appsync):
    appsync.answers["getMeetingWhiteboard"] = {"getMeetingWhiteboard": {**BOARD, "Version": 1}}
    whiteboards.get_whiteboard(caller(OWNER), "m1", "wb1", version=1)
    assert appsync.calls[0][1] == {"CallId": "m1", "WhiteboardId": "wb1", "Version": 1}


def test_a_write_is_recorded_as_the_callers(meeting, appsync):
    appsync.answers["createMeetingWhiteboard"] = {"createMeetingWhiteboard": BOARD}
    whiteboards.create_whiteboard(
        caller(SHARED),
        "m1",
        {
            "title": "Plan",
            "template": "comparison",
            "panels": [{"title": "Options", "content": "| a |"}],
        },
    )
    sent = appsync.calls[0][1]["input"]
    assert sent["Source"] == "user" and sent["UpdatedBy"] == SHARED
    assert sent["Template"] == "comparison"
    # No kind given, so none is sent: the resolver keeps an existing panel's kind.
    assert sent["Panels"] == [{"Title": "Options", "Content": "| a |"}]


def test_an_update_names_its_base_version_and_can_hand_a_panel_back(meeting, appsync):
    appsync.answers["updateMeetingWhiteboard"] = {"updateMeetingWhiteboard": BOARD}
    whiteboards.update_whiteboard(
        caller(OWNER),
        "m1",
        {
            "whiteboardId": "wb1",
            "baseVersion": 3,
            "panels": [
                {
                    "panelId": "decisions",
                    "title": "Decisions",
                    "content": "- March",
                    "source": "assistant",
                }
            ],
        },
    )
    sent = appsync.calls[0][1]["input"]
    assert sent["BaseVersion"] == 3 and sent["Panels"][0]["Source"] == "assistant"


@pytest.mark.parametrize(
    "args, message",
    [
        ({"whiteboardId": "wb1", "baseVersion": 1}, "at least one"),
        ({"whiteboardId": "wb1", "title": "x"}, "baseVersion"),
        (
            {
                "whiteboardId": "wb1",
                "baseVersion": 1,
                "panels": [
                    {"title": "D", "kind": "mermaid", "content": "flowchart TD\n click a x()"}
                ],
            },
            "click",
        ),
        (
            {
                "whiteboardId": "wb1",
                "baseVersion": 1,
                "panels": [{"title": f"P{i}"} for i in range(13)],
            },
            "12",
        ),
    ],
)
def test_a_broken_rule_is_reported_before_anything_is_sent(meeting, appsync, args, message):
    with pytest.raises(ValueError, match=message):
        whiteboards.update_whiteboard(caller(OWNER), "m1", args)
    assert appsync.calls == []
