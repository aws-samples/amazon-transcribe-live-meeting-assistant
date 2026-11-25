#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""A Participant's question on the page of a meeting shared with them.

It arrives with toolScope ``participant_page`` (the agent assist orchestrator
sets it for a question from strands_chat_interface by a Participant) and is
answered under the same Participant use settings as a request made in the
meeting: inMeetingRequestTools, its switches and the per-item flags decide the
tools, and inMeetingRequesters who may ask. On top of that it is never offered
the meeting context or checklist, nor the Virtual Participant controls, its
transcript leaves out the meeting assistant's earlier answers, and frames are
not shown inline.

The handler is driven with the stand-ins of test_in_meeting_request_scope.py.
"""

from __future__ import annotations

import pytest
import strands_meeting_assist_function as handler_module
from test_in_meeting_request_scope import (  # noqa: F401 - fixtures
    ALL_TOOLS,
    CONTEXT_TOOLS,
    CURRENT_MEETING_TOOLS,
    _named,
    built,
    configured,
)

PARTICIPANT_PAGE = {
    "toolScope": "participant_page",
    "userEmail": "owner@example.com",
    "transcript_segment_args": {"MessageId": "chat-1"},
}
VP_CONTROLS = {"control_vnc_preview", "control_vp_browser"}


def test_it_is_answered_from_the_current_meeting_only_by_default(built) -> None:  # noqa: F811
    assert built(PARTICIPANT_PAGE)["tools"] == CURRENT_MEETING_TOOLS


def test_unrestricted_offers_every_tool_but_the_virtual_participant_controls(built, configured) -> None:  # noqa: F811
    configured({"inMeetingRequestTools": "unrestricted"})
    assert built(PARTICIPANT_PAGE)["tools"] == ALL_TOOLS - VP_CONTROLS


def test_selected_tools_offer_what_is_switched_on_for_participant_use(built, configured) -> None:  # noqa: F811
    configured({"inMeetingRequestTools": "selected", "inMeetingWebSearch": True})
    assert built(PARTICIPANT_PAGE)["tools"] == CURRENT_MEETING_TOOLS | {"web_search"}
    configured({"inMeetingRequestTools": "selected", "inMeetingVpControls": True})
    assert built(PARTICIPANT_PAGE)["tools"] == CURRENT_MEETING_TOOLS


def test_an_mcp_server_not_approved_for_participant_use_is_not_offered(built, configured) -> None:  # noqa: F811
    configured(
        {
            "inMeetingRequestTools": "unrestricted",
            "toolSelection": {"mcpServers": {"server-1": {"inMeetingRequests": False}}, "skills": {}},
        }
    )
    assert "mcp_server" not in built(PARTICIPANT_PAGE)["tools"]


@pytest.mark.parametrize("mode", ["current_meeting_only", "unrestricted", "selected"])
def test_the_meeting_context_and_checklist_are_never_offered(built, configured, mode) -> None:  # noqa: F811
    configured({"inMeetingRequestTools": mode, "inMeetingContextAccess": "allowed"})
    agent = built(PARTICIPANT_PAGE)
    assert not agent["tools"] & CONTEXT_TOOLS
    assert handler_module.IN_MEETING_CONTEXT_NOTE not in agent["system_prompt"]


def test_the_transcript_leaves_out_the_assistants_answers_and_frames_are_not_inline(
    built, monkeypatch  # noqa: F811
) -> None:
    seen = {}

    def transcript_factory(*_args, **kwargs):
        seen["transcript"] = kwargs["include_assistant"]
        return _named("current_meeting_transcript")

    def frames_factory(*_args, **kwargs):
        seen["frames"] = (kwargs["include_assistant"], kwargs["show_inline"])
        return _named("get_meeting_frames")

    fetched = []
    monkeypatch.setattr(handler_module, "create_current_meeting_transcript_tool", transcript_factory)
    monkeypatch.setattr(handler_module, "create_meeting_frames_tool", frames_factory)
    monkeypatch.setattr(
        handler_module, "fetch_meeting_transcript", lambda *_a, **kw: fetched.append(kw) or ""
    )
    built(PARTICIPANT_PAGE)
    assert seen == {"transcript": False, "frames": (False, False)}
    assert fetched == [{"include_assistant": False}]
    # The owner's own chat keeps them.
    built({"userEmail": "owner@example.com", "transcript_segment_args": {"MessageId": "chat-2"}})
    assert seen == {"transcript": True, "frames": (True, True)}


def test_the_query_leaves_the_assistant_channel_out_only_when_asked() -> None:
    queries = []

    class Table:
        def query(self, **kwargs):
            queries.append(kwargs)
            return {"Items": []}

    original = handler_module.dynamodb
    handler_module.dynamodb = type("Resource", (), {"Table": lambda self, _name: Table()})()
    try:
        handler_module.query_transcript_items("m1", "t", include_chat=True, include_assistant=False)
        handler_module.query_transcript_items("m1", "t", include_chat=True)
    finally:
        handler_module.dynamodb = original

    def channels(query):
        return query["FilterExpression"].get_expression()["values"][0].get_expression()["values"][1]

    assert "AGENT_ASSISTANT" not in channels(queries[0])
    assert "AGENT_ASSISTANT" in channels(queries[1])
    assert "MEETING_CHAT" in channels(queries[0])


@pytest.mark.parametrize("value", ["owner_only", "off"])
def test_who_may_ask_applies_to_it_as_to_the_meeting_chat(configured, value) -> None:  # noqa: F811
    configured({"inMeetingRequesters": value})
    assert (
        handler_module.in_meeting_request_refusal(PARTICIPANT_PAGE)
        == handler_module.PARTICIPANT_PAGE_REFUSAL_TEXT
    )


def test_anyone_lets_a_participant_ask(configured) -> None:  # noqa: F811
    configured({"inMeetingRequesters": "anyone"})
    assert handler_module.in_meeting_request_refusal(PARTICIPANT_PAGE) == ""
