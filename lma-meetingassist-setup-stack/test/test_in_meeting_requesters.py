#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Who may make a request in the meeting, and the tools turned off for every request.

The Meeting Assistant section's ``inMeetingRequesters`` setting decides whether a
request made in the meeting (``toolScope`` ``meeting_chat``, ``voice_assistant``
or ``wake_phrase``) is answered at all: ``anyone`` (the default) answers every
one; ``owner_only`` answers only a wake phrase spoken on the LOCAL channel of a
meeting captured on the owner's own device; ``off`` answers none. A refused
request gets a fixed answer and no agent is built. The meeting page's own chat
is never refused.

``webSearchEnabled`` and ``mcpServersEnabled``, on by default, take their tool
group out of every request's tools when they are saved as false, the meeting
page's chat included.

The handler is driven end to end with the fixtures of
test_in_meeting_request_scope.py.
"""

from __future__ import annotations

import pytest

import strands_meeting_assist_function as handler_module  # noqa: E402
from lma_config import parameters  # noqa: E402
from test_in_meeting_request_scope import (  # noqa: E402,F401 -- shared fixtures
    CHAT,
    CURRENT_MEETING_TOOLS,
    PAGE_TOOLS,
    VOICE,
    WAKE,
    built,
    configured,
)

PAGE = {"userEmail": "owner@example.com"}
OWNER_WAKE = {**WAKE, "requestChannel": "LOCAL", "requestCaptureSource": "STREAM_AUDIO"}
IN_MEETING = [CHAT, VOICE, WAKE, OWNER_WAKE]
IN_MEETING_IDS = ["chat", "voice", "wake-phrase", "owner-wake-phrase"]


@pytest.fixture
def answered(monkeypatch, built):  # pylint: disable=redefined-outer-name
    """Run the handler; return its answer and whether it built an agent."""
    del built  # installs the stand-ins; the handler is called directly here

    def run(event: dict) -> tuple:
        import strands  # the stand-in the fixture installed

        built_agents = []
        real_agent = strands.Agent

        def counting_agent(**kwargs):
            built_agents.append(kwargs)
            return real_agent(**kwargs)

        monkeypatch.setattr(strands, "Agent", counting_agent)
        result = handler_module.handler(
            {"text": "what did we decide?", "call_id": "call-1", **event}, None
        )
        return result, bool(built_agents)

    return run


# ── inMeetingRequesters ─────────────────────────────────────────────────────


def test_the_shipped_requesters_are_anyone(configured):  # pylint: disable=redefined-outer-name
    configured({})
    shipped = parameters.shipped_setting_defaults()["meeting_assist"]["inMeetingRequesters"]
    assert shipped == handler_module.DEFAULT_IN_MEETING_REQUESTERS == "anyone"
    assert handler_module.configured_in_meeting_requesters() == "anyone"


@pytest.mark.parametrize("event", IN_MEETING, ids=IN_MEETING_IDS)
def test_anyone_answers_every_in_meeting_request(built, configured, event):  # noqa: F811
    configured({"inMeetingRequesters": "anyone"})
    assert built(event)["tools"] == CURRENT_MEETING_TOOLS


@pytest.mark.parametrize("event", IN_MEETING, ids=IN_MEETING_IDS)
def test_off_answers_no_in_meeting_request_and_calls_no_model(answered, configured, event):  # noqa: F811
    configured({"inMeetingRequesters": "off"})
    result, built_an_agent = answered(event)
    assert result["message"] == handler_module.IN_MEETING_REQUESTS_OFF_TEXT
    assert built_an_agent is False


def test_owner_only_answers_a_wake_phrase_on_the_owners_microphone(built, configured):  # noqa: F811
    configured({"inMeetingRequesters": "owner_only"})
    for source in sorted(handler_module.OWNER_CAPTURE_SOURCES):
        event = {**OWNER_WAKE, "requestCaptureSource": source}
        assert built(event)["tools"] == CURRENT_MEETING_TOOLS, source


@pytest.mark.parametrize(
    "event",
    [
        CHAT,
        VOICE,
        WAKE,
        # The meeting audio, not the owner's microphone.
        {**OWNER_WAKE, "requestChannel": "REMOTE"},
        # On a Virtual Participant meeting LOCAL is the voice assistant's own speech.
        {**OWNER_WAKE, "requestCaptureSource": "VIRTUAL_PARTICIPANT"},
        # A client that did not say what it is, or a meeting from before CaptureSource.
        {**OWNER_WAKE, "requestCaptureSource": "API"},
        {**OWNER_WAKE, "requestCaptureSource": None},
        # The owner's channel claimed by a caller that is not the wake phrase.
        {**CHAT, "requestChannel": "LOCAL", "requestCaptureSource": "STREAM_AUDIO"},
        {**VOICE, "requestChannel": "LOCAL", "requestCaptureSource": "STREAM_AUDIO"},
    ],
    ids=["chat", "voice", "wake-no-channel", "remote", "vp", "api", "unknown", "chat-local", "voice-local"],
)
def test_owner_only_refuses_every_request_not_known_to_be_the_owners(answered, configured, event):  # noqa: F811
    configured({"inMeetingRequesters": "owner_only"})
    result, built_an_agent = answered(event)
    assert result["message"] == handler_module.IN_MEETING_REQUESTS_OWNER_ONLY_TEXT
    assert built_an_agent is False


@pytest.mark.parametrize("value", ["", "owner", "nobody", True])
def test_an_unrecognised_requesters_value_is_read_as_off(monkeypatch, value):
    # The store refuses such a value at the save, so only a reader without the
    # configuration layer could see one; it is read as the strictest choice.
    monkeypatch.setattr(handler_module, "read_setting", lambda *_args, **_kw: value)
    assert handler_module.configured_in_meeting_requesters() == "off"


@pytest.mark.parametrize("value", ["anyone", "owner_only", "off"])
def test_the_meeting_pages_chat_is_never_refused(built, configured, value):  # noqa: F811
    configured({"inMeetingRequesters": value})
    assert built(PAGE)["tools"] == PAGE_TOOLS


def test_every_requesters_value_the_schema_offers_is_one_the_assistant_recognises():
    import json

    from test_in_meeting_request_scope import SCHEMA

    offered = json.loads(SCHEMA.read_text(encoding="utf-8"))["properties"]["inMeetingRequesters"]
    assert tuple(offered["enum"]) == handler_module.IN_MEETING_REQUESTERS_VALUES


# ── webSearchEnabled and mcpServersEnabled ─────────────────────────────────


def test_web_search_and_mcp_servers_are_on_by_default(built, configured):  # noqa: F811
    configured({})
    shipped = parameters.shipped_setting_defaults()["meeting_assist"]
    assert shipped["webSearchEnabled"] is True and shipped["mcpServersEnabled"] is True
    assert built(PAGE)["tools"] == PAGE_TOOLS


def test_web_search_off_is_offered_to_no_request(built, configured):  # noqa: F811
    configured({"webSearchEnabled": False, "inMeetingRequestTools": "unrestricted"})
    for event in (PAGE, CHAT, VOICE, WAKE):
        agent = built(event)
        assert "web_search" not in agent["tools"], event
        assert "mcp_server" in agent["tools"], event
        assert "These are turned off on this deployment: web search." in agent["system_prompt"]


def test_mcp_servers_off_are_loaded_for_no_request(built, configured, monkeypatch):  # noqa: F811
    loaded = []
    monkeypatch.setattr(
        handler_module,
        "load_account_mcp_servers_by_id",
        lambda: loaded.append(True) or ({}, True),
        raising=False,
    )
    configured({"mcpServersEnabled": False, "inMeetingRequestTools": "unrestricted"})
    for event in (PAGE, CHAT):
        agent = built(event)
        assert "mcp_server" not in agent["tools"], event
        assert "web_search" in agent["tools"], event
        assert "the installed MCP servers" in agent["system_prompt"]
    assert loaded == []


def test_mcp_servers_off_overrides_a_servers_own_in_meeting_flag(built, configured):  # noqa: F811
    configured(
        {
            "mcpServersEnabled": False,
            "inMeetingRequestTools": "selected",
            "toolSelection": {"mcpServers": {"server-1": {"inMeetingRequests": True}}},
        }
    )
    assert built(CHAT)["tools"] == CURRENT_MEETING_TOOLS


def test_mcp_servers_off_skips_them_when_warming_up(configured, monkeypatch):  # noqa: F811
    loaded = []
    monkeypatch.setattr(handler_module, "MCP_LOADER_AVAILABLE", True)
    monkeypatch.setattr(
        handler_module, "load_account_mcp_servers", lambda: loaded.append(True) or ([], True)
    )
    monkeypatch.setattr(handler_module.meeting_skills, "load_catalog", lambda: [])
    configured({"mcpServersEnabled": False})
    assert handler_module.handler({"action": "warmup"}, None)["mcp_clients"] == 0
    assert loaded == []
    configured({})
    handler_module.handler({"action": "warmup"}, None)
    assert loaded == [True]


@pytest.mark.parametrize("value", [0, None, "off", "no"])
def test_a_switch_read_as_anything_but_false_leaves_its_tools_on(monkeypatch, value):
    monkeypatch.setattr(handler_module, "read_setting", lambda *_args, **_kw: value)
    assert handler_module.configured_turned_off_tool_groups() == frozenset()


def test_each_turned_off_switch_is_read_by_its_own_literal_call():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(handler_module.configured_turned_off_tool_groups))
    read = {
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "read_setting"
    }
    assert read == {"webSearchEnabled", "mcpServersEnabled"}
