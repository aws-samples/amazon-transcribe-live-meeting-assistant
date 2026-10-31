#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The meeting-chat tool scope of the Strands meeting-assist function.

A question typed in the meeting chat (the Virtual Participant's ``LMA ask``)
arrives with ``toolScope: 'meeting_chat'``. The scope itself decides that the
user-scoped tools are not offered, whatever user email the event carries.

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

import os
import sys
import types

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import strands_meeting_assist_function as handler_module  # noqa: E402

ALL_TOOLS = {
    "mcp_server",
    "web_search",
    "document_search",
    "recent_meetings_list",
    "meeting_history",
    "current_meeting_transcript",
    "get_meeting_agenda",
    "control_vnc_preview",
    "control_vp_browser",
}

# The meeting's agenda is offered in the chat scope too: guests in the meeting
# may read the agenda of the meeting they are in, as they read its transcript.
CHAT_TOOLS = {"web_search", "current_meeting_transcript", "get_meeting_agenda"}


def _named(name: str):
    def tool() -> str:
        return name

    tool.__name__ = name
    return tool


@pytest.fixture
def built(monkeypatch):
    """Run the handler for an event and return what its Agent was given."""
    constructed: list[dict] = []

    class FakeAgent:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            self.event_loop_metrics = None

        def __call__(self, _message):
            return types.SimpleNamespace(text="answer")

    strands = types.ModuleType("strands")
    strands.Agent = FakeAgent
    strands.tool = lambda fn: fn
    strands_models = types.ModuleType("strands.models")
    strands_models.BedrockModel = lambda **kwargs: kwargs
    strands.models = strands_models
    tavily = types.ModuleType("tavily")
    tavily.TavilyClient = object
    monkeypatch.setitem(sys.modules, "strands", strands)
    monkeypatch.setitem(sys.modules, "strands.models", strands_models)
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


def test_meeting_chat_question_is_given_only_the_transcript_agenda_and_web_search(built):
    agent = built({"toolScope": "meeting_chat", "userEmail": "owner@example.com"})
    assert agent["tools"] == CHAT_TOOLS


def test_meeting_chat_question_is_told_which_tools_it_has(built):
    agent = built({"toolScope": "meeting_chat", "userEmail": "owner@example.com"})
    assert agent["system_prompt"].endswith(handler_module.MEETING_CHAT_SCOPE_NOTE)
    assert "transcript, its agenda and web search" in handler_module.MEETING_CHAT_SCOPE_NOTE


def test_other_requests_are_given_every_configured_tool(built):
    # The control case: the same configuration, without the scope, offers
    # every tool -- so the test above is measuring the scope and not a
    # stand-in that builds nothing.
    agent = built({"userEmail": "owner@example.com"})
    assert agent["tools"] == ALL_TOOLS
    assert handler_module.MEETING_CHAT_SCOPE_NOTE not in agent["system_prompt"]


def test_meeting_chat_scope_clears_the_user_email():
    scope, user_email = handler_module.meeting_chat_tool_scope(
        {"toolScope": "meeting_chat", "userEmail": "owner@example.com"}
    )
    assert scope is True
    assert user_email == ""


def test_other_requests_keep_their_user_email():
    assert handler_module.meeting_chat_tool_scope({"userEmail": "owner@example.com"}) == (
        False,
        "owner@example.com",
    )
    assert handler_module.meeting_chat_tool_scope({"user_email": "owner@example.com"}) == (
        False,
        "owner@example.com",
    )
