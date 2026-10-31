#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The meeting assistant's get_meeting_agenda tool and agenda progress checks.

What has to hold: the tool reads only the current meeting's record and says so
plainly when there is no agenda (the agent must never invent one); it is offered
in every tool scope, the meeting chat's included (asserted with the other scope
tests, in test_meeting_chat_tool_scope.py); and the transcript can carry
the times a progress check cites, without changing the transcript the agent is
handed as context by default.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import meeting_agenda  # noqa: E402
import strands_meeting_assist_function as handler_module  # noqa: E402

# ── reading the agenda ──────────────────────────────────────────────────────


def test_items_are_numbered_as_the_meeting_page_lists_them():
    assert meeting_agenda.parse_agenda_items("1. Budget\n\n- Hiring\n* Risks\nA team update") == [
        "Budget",
        "Hiring",
        "Risks",
        "A team update",
    ]


def test_the_agenda_is_described_with_its_items_and_provenance():
    text = meeting_agenda.describe_agenda_for_agent(
        {
            "Agenda": "Introductions\nBudget\nHiring",
            "AgendaSource": "scheduled",
            "AgendaUpdatedBy": "alice@example.com",
            "AgendaUpdatedAt": "2026-10-31T10:00:00Z",
        }
    )
    assert text.splitlines()[1:] == ["1. Introductions", "2. Budget", "3. Hiring"]
    assert "3 items" in text
    assert "Virtual Participant" in text
    # Answers reach everyone in the meeting; the editor's username is not one
    # of the things they need.
    assert "alice@example.com" not in text


def test_a_meeting_without_an_agenda_is_said_to_have_none():
    """The agent is told not to invent one, whatever shape "none" arrives in."""
    for record in (None, {}, {"Agenda": ""}, {"Agenda": " \n "}):
        text = meeting_agenda.describe_agenda_for_agent(record)
        assert text == meeting_agenda.NO_AGENDA
        assert "do not make one up" in text.replace("--", "").replace("  ", " ")


def test_an_unknown_source_is_left_out_rather_than_guessed():
    text = meeting_agenda.describe_agenda_for_agent({"Agenda": "Budget", "AgendaSource": "x"})
    assert text == "Meeting agenda (1 item):\n1. Budget"


class FakeTable:
    def __init__(self, item: Dict[str, Any] | None) -> None:
        self.item = item
        self.calls: List[Dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return {"Item": self.item} if self.item is not None else {}

    def query(self, **_kwargs: Any) -> Dict[str, Any]:
        return {"Items": self.item or []}


def test_only_the_current_meetings_record_is_read():
    table = FakeTable({"Agenda": "Budget"})
    assert meeting_agenda.read_meeting_record(table, "call-1") == {"Agenda": "Budget"}
    (call,) = table.calls
    assert call["Key"] == {"PK": "c#call-1", "SK": "c#call-1"}
    # Only the agenda fields, not the whole meeting record.
    assert call["ExpressionAttributeNames"] == {"#agenda": "Agenda"}
    assert "Owner" not in call["ProjectionExpression"]
    assert meeting_agenda.read_meeting_record(FakeTable(None), "call-2") is None


# ── transcript times ────────────────────────────────────────────────────────


def test_elapsed_times_read_as_the_meeting_page_shows_them():
    assert meeting_agenda.format_elapsed(0) == "[00:00]"
    assert meeting_agenda.format_elapsed(250.7) == "[04:10]"
    assert meeting_agenda.format_elapsed("61") == "[01:01]"
    assert meeting_agenda.format_elapsed(3725) == "[1:02:05]"
    assert meeting_agenda.format_elapsed(None) == "[--:--]"
    assert meeting_agenda.format_elapsed(-5) == "[00:00]"


def test_the_transcript_carries_times_only_when_asked(monkeypatch):
    segments = [
        {"Speaker": "Bob", "Transcript": "Budget next.", "Channel": "CALLER", "StartTime": 250, "EndTime": 252},
        {"Speaker": "Ann", "Transcript": "Hello all.", "Channel": "AGENT", "StartTime": 3, "EndTime": 5},
    ]

    class FakeResource:
        @staticmethod
        def Table(_name):  # noqa: N802 - the boto3 resource's own method name
            return FakeTable(segments)

    monkeypatch.setattr(handler_module, "dynamodb", FakeResource)
    plain = handler_module.fetch_meeting_transcript("call-1", "events")
    timed = handler_module.fetch_meeting_transcript("call-1", "events", with_timestamps=True)
    assert plain == "Ann: Hello all.\nBob: Budget next."
    assert timed == "[00:03] Ann: Hello all.\n[04:10] Bob: Budget next."


# ── where the tool is offered, and what the agent is told ───────────────────


def test_the_meeting_chat_is_told_the_agenda_is_available():
    assert "its agenda" in handler_module.MEETING_CHAT_SCOPE_NOTE


def test_the_prompt_guides_a_progress_check_with_transcript_times():
    prompt = handler_module.get_meeting_assistant_prompt_with_tools()
    assert "get_meeting_agenda" in prompt
    assert 'current_meeting_transcript(mode="full", with_timestamps=True)' in prompt
    for status in ("Covered", "In progress", "Remaining"):
        assert status in prompt
    assert "Never invent an agenda" in prompt


def test_items_split_on_line_breaks_only_as_the_page_does():
    assert meeting_agenda.parse_agenda_items("Budget\u2028review\r\nHiring") == [
        "Budget\u2028review",
        "Hiring",
    ]


def _agenda_tool(monkeypatch, table):
    import sys  # noqa: PLC0415
    import types  # noqa: PLC0415

    strands = types.ModuleType("strands")
    strands.tool = lambda fn: fn
    monkeypatch.setitem(sys.modules, "strands", strands)

    class FakeResource:
        @staticmethod
        def Table(_name):  # noqa: N802 - the boto3 resource's own method name
            return table

    monkeypatch.setattr(handler_module, "dynamodb", FakeResource)
    monkeypatch.setattr(handler_module, "APPSYNC_GRAPHQL_URL", "")
    return handler_module.create_meeting_agenda_tool(call_id="call-1", dynamodb_table_name="events")


def test_the_tool_reads_the_meeting_and_never_names_the_last_editor(monkeypatch):
    record = {"Agenda": "Budget", "AgendaUpdatedBy": "alice@example.com", "AgendaUpdatedAt": "t"}
    text = _agenda_tool(monkeypatch, FakeTable(record))()
    assert "1. Budget" in text
    assert "alice@example.com" not in text


def test_a_failed_read_is_reported_to_the_agent_not_raised(monkeypatch):
    class BrokenTable:
        @staticmethod
        def get_item(**_kwargs):
            raise RuntimeError("throttled")

    assert _agenda_tool(monkeypatch, BrokenTable())() == (
        "The meeting agenda could not be read just now."
    )
