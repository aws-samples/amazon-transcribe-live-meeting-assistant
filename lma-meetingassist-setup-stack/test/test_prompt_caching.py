#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The meeting assistant's Amazon Bedrock prompt caching, and how it is metered.

Which request sections a model takes a cache point in is bedrock_compat's
decision (tested in lma-ai-stack/test/test_bedrock_compat.py); these tests hold
the assistant to that decision, to the Meeting Assistant section's switch, and
to recording the cache token counts the SDK reports. The last group builds the
real Strands request, when strands-agents is installed, and checks where the
cache points land in it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

import call_metering  # noqa: E402
import strands_meeting_assist_function as assistant  # noqa: E402
from lma_config import parameters  # noqa: E402

PROMPT = "You are an AI assistant helping participants during a live meeting."

CLAUDE = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
NOVA = "amazon.nova-lite-v1:0"

LANGUAGE_MODELS_SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "lma-ai-stack"
    / "source"
    / "lambda_layers"
    / "configuration_layer"
    / "lma_config"
    / "schemas"
    / "language_models.json"
)

#: Meeting assistant models offered that Amazon Bedrock does not list for
#: explicit prompt caching. A model added to the list must either cache or be
#: named here, so the choice is made deliberately. Every model offered today
#: caches.
NOT_CACHED: set = set()


class _Store:
    def __init__(self, values):
        self.values = values

    def get_section(self, name):
        return SimpleNamespace(values=self.values if name == "meeting_assist" else {})


@pytest.fixture()
def configured(monkeypatch):
    def install(values):
        monkeypatch.setattr(parameters, "_SHARED_STORE", _Store(values))

    return install


# ── the plan for one answer ─────────────────────────────────────────────────


def test_claude_caches_tools_system_and_the_conversation() -> None:
    kwargs, system = assistant.prompt_cache_plan(CLAUDE, PROMPT)
    assert kwargs == {"strategy": "anthropic", "tools_ttl": True}
    # Strands appends the system cache point itself from cache_config.
    assert system == PROMPT


def test_nova_caches_the_system_prompt_only() -> None:
    """No cache_config, which would also put a cache point beside a toolResult."""
    kwargs, system = assistant.prompt_cache_plan(NOVA, PROMPT)
    assert kwargs is None
    assert system == [{"text": PROMPT}, {"cachePoint": {"type": "default"}}]


@pytest.mark.parametrize(
    "model_id", ["us.amazon.nova-premier-v1:0", "anthropic.claude-3-haiku-20240307-v1:0", ""]
)
def test_a_model_without_prompt_caching_is_sent_the_prompt_unchanged(model_id) -> None:
    assert assistant.prompt_cache_plan(model_id, PROMPT) == (None, PROMPT)


@pytest.mark.parametrize("model_id", [CLAUDE, NOVA])
def test_turning_the_switch_off_sends_the_prompt_unchanged(model_id) -> None:
    assert assistant.prompt_cache_plan(model_id, PROMPT, enabled=False) == (None, PROMPT)


def test_every_offered_meeting_assistant_model_has_a_caching_decision() -> None:
    offered = json.loads(LANGUAGE_MODELS_SCHEMA.read_text(encoding="utf-8"))["properties"][
        "meetingAssistModelId"
    ]["enum"]
    for model_id in offered:
        kwargs, system = assistant.prompt_cache_plan(model_id, PROMPT)
        caches = kwargs is not None or isinstance(system, list)
        assert caches is (model_id not in NOT_CACHED), model_id


# ── the Meeting Assistant section's switch ──────────────────────────────────


def test_prompt_caching_is_on_by_default(configured) -> None:
    configured({})
    assert parameters.shipped_setting_defaults()["meeting_assist"]["promptCachingEnabled"] is True
    assert assistant.prompt_caching_enabled() is True


def test_prompt_caching_follows_the_configured_switch(configured) -> None:
    configured({"promptCachingEnabled": False})
    assert assistant.prompt_caching_enabled() is False
    configured({"promptCachingEnabled": True})
    assert assistant.prompt_caching_enabled() is True


# ── metering the cache tokens ───────────────────────────────────────────────


def test_cache_reads_and_writes_reported_by_strands_are_metered() -> None:
    """The shape of Strands' accumulated_usage, as an answer that used a tool reports it."""
    accumulated = {
        "inputTokens": 614,
        "outputTokens": 88,
        "totalTokens": 5772,
        "cacheReadInputTokens": 1795,
        "cacheWriteInputTokens": 5070,
    }
    usage = call_metering.bedrock_usage(accumulated, requests=2)
    assert usage == {
        "inputTokens": 614,
        "outputTokens": 88,
        "cacheReadInputTokens": 1795,
        "cacheWriteInputTokens": 5070,
        "requests": 2,
    }
    item = call_metering.build_item(
        "call-1",
        "MeetingAssistant",
        f"bedrock/{CLAUDE}",
        usage,
        source="strands-meeting-assist",
        expiration_days=0,
        now=datetime(2026, 11, 6, tzinfo=timezone.utc),
    )
    assert item["Usage"]["M"]["cacheReadInputTokens"] == {"N": "1795"}
    assert item["Usage"]["M"]["cacheWriteInputTokens"] == {"N": "5070"}


def test_a_call_that_cached_nothing_records_no_cache_units() -> None:
    usage = call_metering.bedrock_usage(
        {
            "inputTokens": 3779,
            "outputTokens": 64,
            "cacheReadInputTokens": 0,
            "cacheWriteInputTokens": 0,
        }
    )
    assert usage == {"inputTokens": 3779, "outputTokens": 64, "requests": 1}


# ── the request the real SDK builds ─────────────────────────────────────────


def _request(model_id: str, tool_result: bool):
    strands_models = pytest.importorskip(
        "strands.models",
        reason="strands-agents is not installed (make setup-cli-dev installs the pinned version)",
    )
    import boto3  # pylint: disable=import-outside-toplevel
    import strands  # pylint: disable=import-outside-toplevel

    kwargs, system = assistant.prompt_cache_plan(model_id, PROMPT)
    model_kwargs = {"model_id": model_id, "boto_session": boto3.Session(region_name="us-east-1")}
    if kwargs is not None:
        model_kwargs["cache_config"] = strands_models.CacheConfig(**kwargs)
    model = strands_models.BedrockModel(**model_kwargs)
    messages = [{"role": "user", "content": [{"text": "What did we decide?"}]}]
    if tool_result:
        messages += [
            {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": "t1", "name": "lookup", "input": {}}}],
            },
            {
                "role": "user",
                "content": [{"toolResult": {"toolUseId": "t1", "content": [{"text": "x"}]}}],
            },
        ]
    spec = {
        "name": "lookup",
        "description": "Look something up.",
        "inputSchema": {"json": {"type": "object", "properties": {}}},
    }
    # The system prompt goes through Agent(system_prompt=...) as it does in the
    # handler, so the test also covers the pinned SDK accepting a list of
    # system blocks and keeping the cache point in them.
    agent = strands.Agent(model=model, system_prompt=system, callback_handler=None)
    return model.format_request(
        messages, tool_specs=[spec], system_prompt_content=agent.system_prompt_content
    )


def _has_cache_point(blocks) -> bool:
    return any("cachePoint" in block for block in blocks)


@pytest.mark.parametrize("tool_result", [False, True])
def test_the_claude_request_carries_a_cache_point_in_each_section(tool_result) -> None:
    request = _request(CLAUDE, tool_result)
    assert _has_cache_point(request["toolConfig"]["tools"])
    assert _has_cache_point(request["system"])
    assert _has_cache_point(request["messages"][-1]["content"])


@pytest.mark.parametrize("tool_result", [False, True])
def test_the_nova_request_carries_a_cache_point_in_the_system_prompt_only(tool_result) -> None:
    request = _request(NOVA, tool_result)
    assert not _has_cache_point(request["toolConfig"]["tools"])
    assert _has_cache_point(request["system"])
    assert not any(_has_cache_point(message["content"]) for message in request["messages"])
