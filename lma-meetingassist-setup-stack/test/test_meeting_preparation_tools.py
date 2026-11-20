#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The meeting context and checklist tools of the Strands meeting-assist function.

What matters is who is offered them. The meeting page's own chat is; a request
made in the meeting -- a question typed in the meeting chat, a request to the
voice assistant, one made with the wake phrase -- is not, under every
inMeetingRequestTools value, unless the Meeting Assistant setting
inMeetingContextAccess is "allowed". The handler is driven end to end with the
fixtures of test_in_meeting_request_scope.py, so these assert which tools it
actually builds. Then: what the two tools return.
"""

from __future__ import annotations

import json
from pathlib import Path

import meeting_preparation
import pytest
import strands_meeting_assist_function as handler_module
from test_in_meeting_request_scope import (  # noqa: F401 -- fixtures
    CONTEXT_TOOLS,
    IN_MEETING,
    built,
    configured,
)

MODES = ["current_meeting_only", "unrestricted", "selected"]
SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/schemas/meeting_assist.json"
)
DEFAULTS = (
    Path(__file__).resolve().parents[2]
    / "lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/setting_defaults.json"
)


def _everything_on(mode: str) -> dict:
    switches = {
        name: True
        for name in (
            "inMeetingWebSearch",
            "inMeetingDocumentSearch",
            "inMeetingMeetingHistory",
            "inMeetingMcpServers",
            "inMeetingVpControls",
        )
    }
    return {"inMeetingRequestTools": mode, **switches}


# ── who is offered them ─────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("event", IN_MEETING, ids=["chat", "voice", "wake-phrase"])
def test_a_request_made_in_the_meeting_cannot_read_them_by_default(built, configured, event, mode):
    configured(_everything_on(mode))
    agent = built(event)
    assert not agent["tools"] & CONTEXT_TOOLS
    assert handler_module.IN_MEETING_CONTEXT_NOTE not in agent["system_prompt"]


@pytest.mark.parametrize("value", ["none", "", None, "yes", True])
@pytest.mark.parametrize("event", IN_MEETING, ids=["chat", "voice", "wake-phrase"])
def test_any_value_but_allowed_keeps_them_from_a_request_made_in_the_meeting(built, configured, event, value):
    configured({**_everything_on("unrestricted"), "inMeetingContextAccess": value})
    assert not built(event)["tools"] & CONTEXT_TOOLS


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("event", IN_MEETING, ids=["chat", "voice", "wake-phrase"])
def test_an_admin_can_allow_them_to_requests_made_in_the_meeting(built, configured, event, mode):
    configured({"inMeetingRequestTools": mode, "inMeetingContextAccess": "allowed"})
    agent = built(event)
    assert CONTEXT_TOOLS <= agent["tools"]
    assert handler_module.IN_MEETING_CONTEXT_NOTE in agent["system_prompt"]


@pytest.mark.parametrize("value", ["none", "allowed"])
@pytest.mark.parametrize("mode", MODES)
def test_the_meeting_pages_assistant_always_has_them(built, configured, mode, value):
    configured({"inMeetingRequestTools": mode, "inMeetingContextAccess": value})
    agent = built({"userEmail": "owner@example.com"})
    assert CONTEXT_TOOLS <= agent["tools"]
    # Not a request made in the meeting, so no in-meeting note.
    assert handler_module.IN_MEETING_CONTEXT_NOTE not in agent["system_prompt"]


def test_the_rule_itself():
    assert meeting_preparation.context_tools_allowed("", "none")
    assert meeting_preparation.context_tools_allowed("", None)
    for scope in ("meeting_chat", "voice_assistant", "wake_phrase", "anything_else"):
        assert not meeting_preparation.context_tools_allowed(scope, "none")
        assert not meeting_preparation.context_tools_allowed(scope, None)
        assert meeting_preparation.context_tools_allowed(scope, "allowed")


def test_the_setting_is_declared_with_none_as_its_default():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))["properties"]["inMeetingContextAccess"]
    assert schema["enum"] == ["none", "allowed"]
    defaults = json.loads(DEFAULTS.read_text(encoding="utf-8"))
    section = next(value for key, value in defaults.items() if "inMeetingContextAccess" in json.dumps(value))
    assert section["inMeetingContextAccess"]["default"] == "none"
    assert meeting_preparation.DEFAULT_IN_MEETING_CONTEXT_ACCESS == "none"


# ── what they return ────────────────────────────────────────────────────────


def test_the_context_tool_returns_the_context_as_quoted_data():
    text = meeting_preparation.describe_context_for_agent(
        {"MeetingContext": "# Goals\nAgree the budget", "MeetingContextUpdatedBy": "owner@example.com"}
    )
    assert "# Goals\nAgree the budget" in text
    assert "not instructions" in text
    # Who changed it is not part of the answer.
    assert "owner@example.com" not in text


@pytest.mark.parametrize("record", [None, {}, {"MeetingContext": "  "}])
def test_a_meeting_without_context_says_so(record):
    assert meeting_preparation.describe_context_for_agent(record) == meeting_preparation.NO_CONTEXT


def test_the_checklist_tool_lists_each_item_with_its_status_and_evidence_times():
    text = meeting_preparation.describe_checklist_for_agent(
        {
            "Checklist": [
                {"ItemId": "q1", "Text": "Who owns the budget?", "Kind": "question", "Priority": "must",
                 "Status": "covered", "StatusSource": "user", "UpdatedBy": "owner@example.com",
                 "Evidence": [{"SegmentId": "s1", "StartTime": 64.0, "Quote": "Dana owns it"}]},
                {"ItemId": "r1", "Text": "Vendor lock-in", "Kind": "risk", "Status": "open", "Evidence": []},
            ]
        }
    )
    lines = text.splitlines()
    assert lines[0].startswith("Meeting checklist (2 items: 1 open, 1 covered)")
    assert lines[1] == "1. [covered] (must, question) Who owns the budget? Evidence at [01:04]."
    assert lines[2] == "2. [open] (should, risk) Vendor lock-in"
    assert "owner@example.com" not in text


@pytest.mark.parametrize("record", [None, {}, {"Checklist": []}])
def test_a_meeting_without_a_checklist_says_so(record):
    assert meeting_preparation.describe_checklist_for_agent(record) == meeting_preparation.NO_CHECKLIST


def test_the_record_read_asks_for_only_the_two_fields():
    class Table:
        def get_item(self, **kwargs):
            self.kwargs = kwargs
            return {"Item": {"MeetingContext": "x"}}

    table = Table()
    assert meeting_preparation.read_meeting_record(table, "c1") == {"MeetingContext": "x"}
    assert table.kwargs["Key"] == {"PK": "c#c1", "SK": "c#c1"}
    assert table.kwargs["ProjectionExpression"] == "MeetingContext, Checklist"
