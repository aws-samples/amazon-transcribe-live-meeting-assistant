# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Meeting whiteboards on a live stack.

One probe run (meeting_whiteboards_probe.py) creates a meeting with the
assistant's whiteboard updates on and a control meeting with them off, sends a
recorded planning conversation to both in three waves, edits one panel as the
Cognito test user between the assistant's first and second runs, and observes what the assistant did, what
MCP and the export return, and what the transcript deletion removes. Each test
asserts one property. The model's wording varies, so the assertions about it are
loose: a panel the assistant filled, not its text. What the whiteboards held is
printed, so a reader can judge their quality.

Runs whenever a Cognito test user is configured, and skips without one. Takes
about twelve minutes: the transcript is sent in three waves an interval apart.
Self-cleaning: both meetings are removed by their CallIds, and the API keys are
revoked.
"""

from __future__ import annotations

import json

import cognito_test_user
import pytest
from meeting_whiteboards_probe import GANTT_CHART, USER_DECISION


@pytest.fixture(scope="module")
def observed(client):
    if not cognito_test_user.available(client.region):
        pytest.skip(f"no Cognito test user — {cognito_test_user.describe_sources()}")
    try:
        import pycognito  # noqa: F401
    except ImportError:
        pytest.skip(
            "pycognito not installed (pip install -r integ-tests/requirements.txt)"
        )

    from meeting_whiteboards_probe import run_probe

    result = run_probe(client.stack_name, region=client.region)
    print(json.dumps(result, indent=1, default=str))
    return result


def _decisions(board: dict) -> dict:
    return next(p for p in board["Panels"] if p["Title"] == "Decisions")


def test_the_meetings_start_with_their_whiteboards_choice(observed) -> None:
    assert observed["assisted_enabled"] is True
    assert observed["control_enabled"] is False


def test_a_whiteboard_made_by_hand_is_the_users_and_costs_no_model_call(
    observed,
) -> None:
    manual = observed["manual"]
    assert manual["Version"] == 2 and manual["Panels"][0]["Source"] == "user"
    assert (
        observed["control_metering"] == []
        or "MeetingWhiteboards" not in observed["control_metering"]
    )
    assert observed["control_state"] == {}


def test_a_project_plan_whiteboard_holds_a_gantt_chart(observed) -> None:
    created = observed["gantt_created"]
    assert created["Template"] == "gantt"
    assert created["Title"] == "Project plan (Gantt chart)"
    assert [(p["Title"], p["Kind"]) for p in created["Panels"]] == [
        ("Plan", "mermaid"),
        ("Dependencies and risks", "markdown"),
        ("Notes", "markdown"),
    ]
    written = observed["gantt_written"]
    assert written["Version"] == created["Version"] + 1
    assert written["Panels"][0]["Content"] == GANTT_CHART
    assert written["Panels"][0]["Source"] == "user"


def test_a_diagram_style_that_fetches_anything_is_refused(observed) -> None:
    assert observed["fetching_style_refused"] is True


def test_the_assistant_makes_and_fills_the_default_whiteboard(observed) -> None:
    boards = observed["after_first"]
    assert boards, observed["first_state"]
    board = boards[0]
    assert board["Template"] == "decisions" and board["CreatedBy"] == "LMA assistant"
    filled = [
        p
        for p in board["Panels"]
        if p["Source"] == "assistant" and p["Content"].strip()
    ]
    assert filled, board
    assert "MeetingWhiteboards" in observed["assisted_metering"]


def test_a_change_from_a_stale_version_is_refused(observed) -> None:
    assert observed["stale_refused"] is True


def test_a_panel_the_user_wrote_is_not_changed_by_a_later_run(observed) -> None:
    assert int(observed["second_state"]["RunCount"] or 0) >= 2, observed["second_state"]
    decisions = _decisions(observed["after_second"][0])
    assert decisions["Content"] == USER_DECISION
    assert decisions["Source"] == "user"


def test_each_change_is_a_version_with_its_source(observed) -> None:
    versions = observed["versions"]
    assert [v["Version"] for v in versions] == sorted(
        (v["Version"] for v in versions), reverse=True
    )
    assert {"user", "assistant"} <= {v["Source"] for v in versions}


def test_only_a_read_write_key_is_offered_the_whiteboard_writes(observed) -> None:
    assert observed["read_key_tools"] == [
        "get_meeting_whiteboard",
        "list_meeting_whiteboards",
    ]
    assert observed["rw_key_tools"] == [
        "create_meeting_whiteboard",
        "get_meeting_whiteboard",
        "list_meeting_whiteboards",
        "update_meeting_whiteboard",
    ]


def test_mcp_reads_the_whiteboards_and_polls_without_a_change(observed) -> None:
    assert observed["mcp_list"]["assistantUpdates"] is True
    assert observed["mcp_list"]["whiteboards"]
    assert observed["mcp_get"]["panels"]
    assert observed["mcp_not_modified"]["notModified"] is True


def test_the_export_holds_the_whiteboards(observed) -> None:
    assert observed.get("export_error") is None, observed.get("export_error")
    exported = observed["export_whiteboards"]
    assert exported and exported[0]["panels"]
    assert any(p["content"] == USER_DECISION for p in exported[0]["panels"])


def test_the_whiteboards_are_deleted_with_the_transcript(observed) -> None:
    assert observed["deletion_error"] is None
    assert observed["rows_after_deletion"] == 0
