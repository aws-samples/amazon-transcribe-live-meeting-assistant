#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The meeting assistant searches only its user's own meetings in the transcript knowledge base.

The summary step writes each meeting's ``Owner`` into the metadata of its
knowledge-base documents (bedrock_summary_lambda getKBMetadata). The
``meeting_history`` tool filters every search on that key, so a user's
assistant, and an in-meeting request acting for the owner, reaches only that
owner's meetings, never another user's. docs/hosting-models.md relies on this.
"""

from __future__ import annotations

import os
import types

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import strands_meeting_assist_function as handler_module  # noqa: E402
import strands_stand_in  # noqa: E402 -- scripts/testing, on the path via conftest.py

KB_MODEL = "global.anthropic.claude-sonnet-4-6"


def _search(monkeypatch, user_email: str) -> list:
    strands_stand_in.install(monkeypatch)
    calls = []

    def retrieve_and_generate(**kwargs):
        calls.append(kwargs)
        return {"output": {"text": "found"}}

    monkeypatch.setattr(
        handler_module,
        "bedrock_agent_runtime",
        types.SimpleNamespace(retrieve_and_generate=retrieve_and_generate),
    )
    monkeypatch.setattr(handler_module, "send_thinking_step_to_appsync", lambda *_a, **_k: None)
    monkeypatch.setattr(handler_module, "apply_kb_guardrail", lambda _input: None)
    tool = handler_module.create_meeting_history_tool(
        transcript_kb_id="kb-transcripts",
        kb_region="us-east-1",
        kb_account_id="123456789012",
        kb_model_id=KB_MODEL,
        user_email=user_email,
    )
    assert tool is not None
    tool(query="the launch date")
    return calls


def test_every_search_is_filtered_on_the_users_own_meetings(monkeypatch):
    calls = _search(monkeypatch, "owner@example.com")
    assert len(calls) == 1
    retrieval = calls[0]["retrieveAndGenerateConfiguration"]["knowledgeBaseConfiguration"][
        "retrievalConfiguration"
    ]
    assert retrieval["vectorSearchConfiguration"]["filter"] == {
        "equals": {"key": "Owner", "value": "owner@example.com"}
    }


def test_with_no_user_nothing_is_searched(monkeypatch):
    assert _search(monkeypatch, "") == []


def test_the_filter_key_is_the_one_the_summary_step_writes():
    from pathlib import Path

    summary = (
        Path(__file__).resolve().parents[2]
        / "lma-ai-stack/source/lambda_functions/bedrock_summary_lambda/index.py"
    ).read_text(encoding="utf-8")
    keys = summary.split("def getKBMetadata", 1)[1].split("]", 1)[0]
    assert '"Owner"' in keys
