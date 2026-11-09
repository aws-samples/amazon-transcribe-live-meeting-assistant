#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The meeting-chat tool scope of the Strands meeting-assist function.

A question typed in the meeting chat (the Virtual Participant's ``LMA ask``)
arrives with ``toolScope: 'meeting_chat'``. The Meeting Assistant section's
``chatQuestionTools`` setting decides what it is answered with: by default the
current meeting only (transcript, agenda, shared-screen pictures), with no
user email whatever the event carries; the setting's other choices offer every
tool to the owner's questions or to everyone's.

The handler is driven end to end with every tool group's precondition met --
an MCP server installed, both knowledge bases, web search, AppSync and the
Event API configured, and a user email on the event -- and the Strands
``Agent`` it constructs is a stand-in that records the tools and system prompt
it was given. So these assert which tools the handler actually builds, rather
than that particular lines are present in its source. The tool factories are
stand-ins too: what each tool does is not under test here, only whether it is
offered.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import strands_meeting_assist_function as handler_module  # noqa: E402
import strands_stand_in  # noqa: E402 -- scripts/testing, on the path via conftest.py
from lma_config import parameters  # noqa: E402

ALL_TOOLS = {
    "mcp_server",
    "web_search",
    "document_search",
    "recent_meetings_list",
    "meeting_history",
    "current_meeting_transcript",
    "get_meeting_agenda",
    "get_meeting_frames",
    "control_vnc_preview",
    "control_vp_browser",
}

# The current meeting: its transcript, its agenda and its shared screen. Guests
# in the meeting may read the agenda of the meeting they are in, and see again
# what was shared with them, as they read its transcript.
CURRENT_MEETING_TOOLS = {"current_meeting_transcript", "get_meeting_agenda", "get_meeting_frames"}

CHAT = {"toolScope": "meeting_chat", "userEmail": "owner@example.com"}

SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/schemas/meeting_assist.json"
)


def _named(name: str):
    def tool() -> str:
        return name

    tool.__name__ = name
    return tool


class _Store:
    def __init__(self, values):
        self.values = values

    def get_section(self, name):
        return types.SimpleNamespace(values=self.values if name == "meeting_assist" else {})


@pytest.fixture
def configured(monkeypatch):
    """Set the Meeting Assistant section's saved values."""

    def install(values):
        monkeypatch.setattr(parameters, "_SHARED_STORE", _Store(values))

    install({})
    return install


@pytest.fixture
def built(monkeypatch, configured):  # pylint: disable=unused-argument,redefined-outer-name
    """Run the handler for an event and return what its Agent was given."""
    constructed: list[dict] = []

    class FakeAgent:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            self.event_loop_metrics = None

        def __call__(self, _message):
            return types.SimpleNamespace(text="answer")

    strands = strands_stand_in.install(monkeypatch)
    monkeypatch.setattr(strands, "Agent", FakeAgent)
    monkeypatch.setattr(strands.models, "BedrockModel", lambda **kwargs: kwargs)
    tavily = types.ModuleType("tavily")
    tavily.TavilyClient = object
    monkeypatch.setitem(sys.modules, "tavily", tavily)

    # Every tool group's precondition, met.
    monkeypatch.setenv("KB_ID", "kb-documents")
    monkeypatch.setenv("TRANSCRIPT_KB_ID", "kb-transcripts")
    monkeypatch.setenv("KB_ACCOUNT_ID", "123456789012")
    monkeypatch.setattr(handler_module, "MCP_LOADER_AVAILABLE", True)
    monkeypatch.setattr(
        handler_module,
        "load_account_mcp_servers",
        lambda: ([_named("mcp_server")], True),
        raising=False,
    )
    monkeypatch.setattr(handler_module.tavily_secret, "is_configured", lambda: True)
    monkeypatch.setattr(
        handler_module, "APPSYNC_GRAPHQL_URL", "https://appsync.example.com/graphql"
    )
    monkeypatch.setattr(handler_module, "EVENT_API_HTTP_URL", "https://events.example.com/event")
    monkeypatch.setattr(handler_module, "ENABLE_STREAMING", False)

    # Stand-ins for everything that would reach AWS.
    monkeypatch.setattr(handler_module, "fetch_meeting_transcript", lambda *_args: "")
    monkeypatch.setattr(handler_module, "send_thinking_step_async", lambda *_args, **_kw: None)
    monkeypatch.setattr(handler_module.call_metering, "record", lambda *_args, **_kw: False)
    for factory, name in (
        ("create_document_search_tool", "document_search"),
        ("create_recent_meetings_tool", "recent_meetings_list"),
        ("create_meeting_history_tool", "meeting_history"),
        ("create_current_meeting_transcript_tool", "current_meeting_transcript"),
        ("create_meeting_agenda_tool", "get_meeting_agenda"),
        ("create_meeting_frames_tool", "get_meeting_frames"),
        ("create_vnc_preview_control_tool", "control_vnc_preview"),
        ("create_vp_browser_control_tool", "control_vp_browser"),
    ):
        monkeypatch.setattr(
            handler_module, factory, lambda *_args, _name=name, **_kw: _named(_name)
        )

    def run(event: dict) -> dict:
        constructed.clear()
        result = handler_module.handler(
            {
                "text": "what did we decide about the launch date?",
                "call_id": "call-1",
                "dynamodb_table_name": "EventSourcingTable",
                **event,
            },
            None,
        )
        assert result.get("message") == "answer", result
        assert len(constructed) == 1, "the handler built no agent"
        agent = constructed[0]
        return {
            "tools": {tool.__name__ for tool in agent["tools"] or []},
            "system_prompt": agent["system_prompt"],
        }

    return run



# ── the default: the current meeting only ──────────────────────────────────


def test_a_chat_question_is_answered_with_current_meeting_tools_only_by_default(built):
    agent = built(CHAT)
    assert agent["tools"] == CURRENT_MEETING_TOOLS


def test_a_chat_question_from_the_owner_is_answered_with_current_meeting_tools_by_default(built):
    # Even an event that says the owner asked is answered from the current
    # meeting only unless the setting allows more.
    agent = built({**CHAT, "askedByMeetingOwner": True})
    assert agent["tools"] == CURRENT_MEETING_TOOLS


def test_the_shipped_chat_question_tools_are_the_current_meeting_only(configured):
    configured({})
    shipped = parameters.shipped_setting_defaults()["meeting_assist"]["chatQuestionTools"]
    assert shipped == handler_module.DEFAULT_CHAT_QUESTION_TOOLS == "current_meeting_only"
    assert handler_module.configured_chat_question_tools() == "current_meeting_only"


@pytest.mark.parametrize("value", ["", "everything", None, True])
def test_an_unrecognised_setting_is_answered_from_the_current_meeting_only(built, configured, value):
    configured({"chatQuestionTools": value})
    assert handler_module.configured_chat_question_tools() == "current_meeting_only"
    assert built(CHAT)["tools"] == CURRENT_MEETING_TOOLS


def test_a_current_meeting_chat_question_is_told_which_tools_it_has(built):
    agent = built(CHAT)
    assert agent["system_prompt"].endswith(
        handler_module.MEETING_CHAT_NOTE + handler_module.MEETING_CHAT_SCOPE_NOTE
    )
    assert "its transcript, its agenda and the pictures of its shared screen" in (
        handler_module.MEETING_CHAT_SCOPE_NOTE
    )
    assert "do not search the web" in handler_module.MEETING_CHAT_SCOPE_NOTE


def test_a_current_meeting_chat_question_has_no_user_email():
    scope = handler_module.chat_question_scope(CHAT)
    assert scope == (True, True, "")


# ── owner_only_all_tools ───────────────────────────────────────────────────


def test_the_owners_chat_question_may_use_all_tools_when_the_setting_allows(built, configured):
    configured({"chatQuestionTools": "owner_only_all_tools"})
    agent = built({**CHAT, "askedByMeetingOwner": True})
    assert agent["tools"] == ALL_TOOLS
    assert handler_module.MEETING_CHAT_SCOPE_NOTE not in agent["system_prompt"]
    assert agent["system_prompt"].endswith(handler_module.MEETING_CHAT_NOTE)


@pytest.mark.parametrize("flag", [False, None, "true", 1])
def test_a_participants_chat_question_is_answered_from_the_current_meeting_when_only_the_owner_may_use_all_tools(
    built, configured, flag
):
    # Only an explicit True says the owner asked; a missing, false or
    # merely truthy value does not.
    configured({"chatQuestionTools": "owner_only_all_tools"})
    event = {**CHAT} if flag is None else {**CHAT, "askedByMeetingOwner": flag}
    assert built(event)["tools"] == CURRENT_MEETING_TOOLS
    assert handler_module.chat_question_scope(event).user_email == ""


# ── all_participants_all_tools ─────────────────────────────────────────────


def test_every_chat_question_may_use_all_tools_when_the_setting_allows(built, configured):
    configured({"chatQuestionTools": "all_participants_all_tools"})
    agent = built({**CHAT, "askedByMeetingOwner": False})
    assert agent["tools"] == ALL_TOOLS
    assert agent["system_prompt"].endswith(handler_module.MEETING_CHAT_NOTE)


def test_an_all_tools_chat_question_acts_for_the_meetings_user(built, configured, monkeypatch):
    configured({"chatQuestionTools": "all_participants_all_tools"})
    seen = []

    def factory(*_args, **kwargs):
        seen.append(kwargs["user_email"])
        return _named("meeting_history")

    monkeypatch.setattr(handler_module, "create_meeting_history_tool", factory)
    built(CHAT)
    assert seen == ["owner@example.com"]


# ── every other request ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value", ["current_meeting_only", "owner_only_all_tools", "all_participants_all_tools"]
)
def test_other_requests_are_given_every_configured_tool_whatever_the_setting(built, configured, value):
    # The control case: the same configuration, without the scope, offers
    # every tool -- so the tests above are measuring the scope and not a
    # stand-in that builds nothing. The meeting page's assistant, the wake
    # phrase and the voice assistant are not affected by the setting.
    configured({"chatQuestionTools": value})
    agent = built({"userEmail": "owner@example.com"})
    assert agent["tools"] == ALL_TOOLS
    assert handler_module.MEETING_CHAT_NOTE not in agent["system_prompt"]
    assert handler_module.MEETING_CHAT_SCOPE_NOTE not in agent["system_prompt"]


def test_other_requests_keep_their_user_email():
    assert handler_module.chat_question_scope({"userEmail": "owner@example.com"}) == (
        False,
        False,
        "owner@example.com",
    )
    assert handler_module.chat_question_scope({"user_email": "owner@example.com"}) == (
        False,
        False,
        "owner@example.com",
    )


def test_every_offered_choice_is_one_the_assistant_recognises():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))["properties"]["chatQuestionTools"]
    assert tuple(schema["enum"]) == handler_module.CHAT_QUESTION_TOOLS_VALUES
    assert set(schema["enumLabels"]) == set(schema["enum"])


@pytest.mark.parametrize(
    "event, inline",
    [
        ({"transcript_segment_args": {"MessageId": "chat-1"}}, True),  # the chat panel
        ({"transcript_segment_args": {"SegmentId": "seg-1"}}, False),  # the wake phrase
        ({}, False),  # the voice assistant
        ({"toolScope": "meeting_chat", "transcript_segment_args": {"MessageId": "chat-1"}}, False),
    ],
)
def test_frames_are_shown_inline_only_in_the_meeting_pages_chat(built, monkeypatch, event, inline):
    seen = []

    def factory(*_args, **kwargs):
        seen.append(kwargs["show_inline"])
        return _named("get_meeting_frames")

    monkeypatch.setattr(handler_module, "create_meeting_frames_tool", factory)
    built({"userEmail": "owner@example.com", **event})
    assert seen == [inline]
