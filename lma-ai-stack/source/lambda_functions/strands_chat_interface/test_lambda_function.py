#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Who may ask the meeting page's assistant about a meeting, and what is passed on.

No AWS: the meetings table and the orchestrator are stand-ins.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN"] = "orchestrator-function"
os.environ["EVENT_SOURCING_TABLE_NAME"] = "lma-call-events"

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "strands_chat_interface_fn", HERE / "lambda_function.py"
)
chat = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(chat)

OWNER = "owner@example.com"
PAT = "pat@example.com"
SHARED_USER = "sam@example.com"


class Table:
    def __init__(self, meeting):
        self.meeting = meeting

    def get_item(self, **kwargs):
        assert kwargs["Key"] == {"PK": "c#m1", "SK": "c#m1"}
        return {"Item": dict(self.meeting)} if self.meeting else {}


@pytest.fixture
def ask(monkeypatch):
    sent = []

    class Lambda:
        def invoke(self, **kwargs):
            sent.append(json.loads(kwargs["Payload"]))

    monkeypatch.setattr(chat, "lambda_client", Lambda())

    def run(username, groups, meeting):
        monkeypatch.setattr(
            chat, "dynamodb", type("R", (), {"Table": lambda self, _n: Table(meeting)})()
        )
        sent.clear()
        chat.lambda_handler(
            {
                "arguments": {"input": {"CallId": "m1", "Message": "what was decided?"}},
                "identity": {"username": username, "groups": groups},
            },
            None,
        )
        return sent[0]

    return run


MEETING = {"Owner": OWNER, "SharedWith": [PAT, SHARED_USER]}


def test_a_participant_the_meeting_is_shared_with_is_answered_under_participant_use(ask) -> None:
    payload = ask(PAT, ["Participant"], MEETING)
    assert payload["RequesterRole"] == "Participant"
    assert payload["MeetingOwner"] == OWNER
    assert payload["Channel"] == "CHAT_ASSISTANT"


@pytest.mark.parametrize(
    "username,groups",
    [(OWNER, ["User"]), (SHARED_USER, ["User"]), ("admin@example.com", ["Admin"])],
)
def test_the_owner_a_shared_user_and_an_admin_ask_as_before(ask, username, groups) -> None:
    payload = ask(username, groups, MEETING)
    assert "RequesterRole" not in payload and "MeetingOwner" not in payload


@pytest.mark.parametrize(
    "username,groups,meeting",
    [
        ("stranger@example.com", ["Participant"], MEETING),
        ("stranger@example.com", ["User"], MEETING),
        (PAT, ["Participant"], {"Owner": OWNER, "SharedWith": f"[x{PAT}]"}),
        # A Participant reads shared meetings only, not meetings they own.
        (PAT, ["Participant"], {"Owner": PAT, "SharedWith": []}),
        (PAT, ["Participant"], None),
        ("admin@example.com", ["Admin"], None),
    ],
)
def test_a_caller_who_may_not_read_the_meeting_is_refused(ask, username, groups, meeting) -> None:
    with pytest.raises(PermissionError, match="Unauthorized"):
        ask(username, groups, meeting)
