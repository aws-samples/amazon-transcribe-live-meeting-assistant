#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The meeting-chat tool scope of the Strands meeting-assist function.

A question typed in the meeting chat (the Virtual Participant's ``LMA ask``)
arrives with ``toolScope: 'meeting_chat'``. The scope itself decides that the
user-scoped tools are not offered, whatever user email the event carries.
"""

from __future__ import annotations

import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import strands_meeting_assist_function as handler_module  # noqa: E402
from conftest import SOURCE_DIR  # noqa: E402


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


def test_meeting_chat_scope_gates_the_mcp_and_control_tools():
    # The three tool groups the scope leaves out are each guarded by it.
    source = (SOURCE_DIR / "strands_meeting_assist_function.py").read_text()
    for guard in (
        "if MCP_LOADER_AVAILABLE and not meeting_chat_scope:",
        "if APPSYNC_GRAPHQL_URL and not meeting_chat_scope:",
        "if EVENT_API_HTTP_URL and not meeting_chat_scope:",
    ):
        assert guard in source, guard
