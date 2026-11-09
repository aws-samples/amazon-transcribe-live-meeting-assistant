#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The in-meeting tool scope of the Strands meeting-assist function.

A request made in the meeting by whoever is in it arrives with a toolScope
naming its caller: ``meeting_chat`` (the Virtual Participant's ``LMA ask``),
``voice_assistant`` (a voice assistant lookup) or ``wake_phrase`` (the wake
phrase, from the agent assist orchestrator). The Meeting Assistant section's
``inMeetingRequestTools`` setting decides what it is answered with: by default
the current meeting only (its transcript with its chat, agenda and shared
screen), with no user email whatever the event carries; ``unrestricted``
offers every tool. The meeting page's own chat sends no scope and is offered
every tool whatever the setting.

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
VOICE = {"toolScope": "voice_assistant", "userEmail": "owner@example.com"}
WAKE = {
    "toolScope": "wake_phrase",
    "userEmail": "owner@example.com",
    "transcript_segment_args": {"SegmentId": "seg-1"},
}
IN_MEETING = [CHAT, VOICE, WAKE]

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


@pytest.mark.parametrize("event", IN_MEETING, ids=["chat", "voice", "wake-phrase"])
def test_an_in_meeting_request_uses_current_meeting_content_only_by_default(built, event):
    assert built(event)["tools"] == CURRENT_MEETING_TOOLS


def test_a_chat_question_from_a_participant_is_answered_with_current_meeting_tools_only_by_default(built):
    assert built(CHAT)["tools"] == CURRENT_MEETING_TOOLS


def test_a_spoken_request_to_the_voice_assistant_uses_current_meeting_content_only_by_default(built):
    assert built(VOICE)["tools"] == CURRENT_MEETING_TOOLS


def test_the_shipped_in_meeting_request_tools_are_the_current_meeting_only(configured):
    configured({})
    shipped = parameters.shipped_setting_defaults()["meeting_assist"]["inMeetingRequestTools"]
    assert shipped == handler_module.DEFAULT_IN_MEETING_TOOLS == "current_meeting_only"
    assert handler_module.configured_in_meeting_request_tools() == "current_meeting_only"


@pytest.mark.parametrize("value", ["", "everything", "owner_only_all_tools", None, True])
def test_an_unrecognised_setting_is_answered_from_the_current_meeting_only(built, configured, value):
    configured({"inMeetingRequestTools": value})
    assert handler_module.configured_in_meeting_request_tools() == "current_meeting_only"
    assert built(VOICE)["tools"] == CURRENT_MEETING_TOOLS


@pytest.mark.parametrize("event", IN_MEETING, ids=["chat", "voice", "wake-phrase"])
def test_a_current_meeting_request_is_told_what_the_current_meeting_holds(built, event):
    prompt = built(event)["system_prompt"]
    assert prompt.endswith(handler_module.CURRENT_MEETING_SCOPE_NOTE)
    note = handler_module.CURRENT_MEETING_SCOPE_NOTE
    for available in ("transcript", "meeting chat", "[chat]", "agenda", "shared screen", "title", "text"):
        assert available in note
    assert "do not search the web" in note


def test_only_a_chat_question_is_told_its_answer_is_posted_in_the_chat(built):
    assert handler_module.MEETING_CHAT_NOTE in built(CHAT)["system_prompt"]
    assert handler_module.MEETING_CHAT_NOTE not in built(VOICE)["system_prompt"]
    assert handler_module.MEETING_CHAT_NOTE not in built(WAKE)["system_prompt"]


@pytest.mark.parametrize("event", IN_MEETING, ids=["chat", "voice", "wake-phrase"])
def test_a_current_meeting_request_has_no_user_email(event):
    scope = handler_module.in_meeting_scope(event)
    assert scope.current_meeting_only is True
    assert scope.user_email == ""


# ── unrestricted ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("event", IN_MEETING, ids=["chat", "voice", "wake-phrase"])
def test_unrestricted_lets_in_meeting_requests_use_all_tools(built, configured, event):
    configured({"inMeetingRequestTools": "unrestricted"})
    agent = built(event)
    assert agent["tools"] == ALL_TOOLS
    assert handler_module.CURRENT_MEETING_SCOPE_NOTE not in agent["system_prompt"]


@pytest.mark.parametrize("event", IN_MEETING, ids=["chat", "voice", "wake-phrase"])
def test_an_unrestricted_in_meeting_request_acts_for_the_user_it_names(
    built, configured, monkeypatch, event
):
    configured({"inMeetingRequestTools": "unrestricted"})
    seen = []

    def factory(*_args, **kwargs):
        seen.append(kwargs["user_email"])
        return _named("meeting_history")

    monkeypatch.setattr(handler_module, "create_meeting_history_tool", factory)
    built(event)
    assert seen == ["owner@example.com"]


def test_an_unrestricted_chat_question_is_still_told_its_answer_is_posted_in_the_chat(
    built, configured
):
    configured({"inMeetingRequestTools": "unrestricted"})
    assert built(CHAT)["system_prompt"].endswith(handler_module.MEETING_CHAT_NOTE)


# ── the meeting page's own chat ────────────────────────────────────────────


@pytest.mark.parametrize("value", ["current_meeting_only", "unrestricted"])
def test_the_meeting_pages_assistant_is_given_every_configured_tool_whatever_the_setting(
    built, configured, value
):
    # The control case: the same configuration, without an in-meeting scope,
    # offers every tool -- so the tests above are measuring the scope and not
    # a stand-in that builds nothing.
    configured({"inMeetingRequestTools": value})
    agent = built({"userEmail": "owner@example.com"})
    assert agent["tools"] == ALL_TOOLS
    assert handler_module.MEETING_CHAT_NOTE not in agent["system_prompt"]
    assert handler_module.CURRENT_MEETING_SCOPE_NOTE not in agent["system_prompt"]


def test_an_unknown_tool_scope_is_answered_as_the_meeting_page_is():
    # Only the named in-meeting callers are restricted; the in-app chat sends
    # no scope at all.
    assert handler_module.in_meeting_scope({"userEmail": "owner@example.com"}) == (
        "",
        False,
        "owner@example.com",
    )
    assert handler_module.in_meeting_scope({"user_email": "owner@example.com"}) == (
        "",
        False,
        "owner@example.com",
    )


def test_every_offered_choice_is_one_the_assistant_recognises():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))["properties"]["inMeetingRequestTools"]
    assert tuple(schema["enum"]) == handler_module.IN_MEETING_TOOLS_VALUES
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
