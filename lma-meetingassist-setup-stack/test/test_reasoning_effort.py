#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The meeting assistant's reasoning effort, from the setting to the Converse request.

Which models take an effort is bedrock_compat's decision (tested in
lma-ai-stack/test/test_bedrock_compat.py); these tests hold the assistant to it,
to the Meeting Assistant section's setting, and to the request the real Strands
SDK builds from it, including beside the prompt cache points.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

import strands_meeting_assist_function as assistant  # noqa: E402
from lma_config import parameters  # noqa: E402

HAIKU_5_5 = "global.anthropic.claude-haiku-5-5"
HAIKU_4_5 = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
NOVA = "amazon.nova-lite-v1:0"

SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/schemas/meeting_assist.json"
)


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


# ── the setting ─────────────────────────────────────────────────────────────


def test_the_shipped_effort_is_low(configured) -> None:
    configured({})
    shipped = parameters.shipped_setting_defaults()["meeting_assist"][
        "assistantReasoningEffort"
    ]
    assert shipped == assistant.DEFAULT_REASONING_EFFORT == "low"
    assert assistant.configured_reasoning_effort() == "low"


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_the_configured_effort_is_used(configured, effort) -> None:
    configured({"assistantReasoningEffort": effort})
    assert assistant.configured_reasoning_effort() == effort
    assert assistant.assistant_request_fields(HAIKU_5_5) == {
        "output_config": {"effort": effort}
    }


def test_every_offered_effort_is_one_bedrock_accepts() -> None:
    from bedrock_compat import REASONING_EFFORT_LEVELS  # pylint: disable=import-outside-toplevel

    offered = json.loads(SCHEMA.read_text(encoding="utf-8"))["properties"][
        "assistantReasoningEffort"
    ]["enum"]
    assert set(offered) <= set(REASONING_EFFORT_LEVELS)


# ── which models are sent it ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model_id",
    [
        HAIKU_5_5,
        # What the Model inference geography setting maps the default to.
        "us.anthropic.claude-haiku-5-5",
        "eu.anthropic.claude-haiku-5-5",
        "anthropic.claude-haiku-5-5",
        "global.anthropic.claude-sonnet-5-5",
    ],
)
def test_claude_5_models_are_sent_the_effort(configured, model_id) -> None:
    configured({})
    assert assistant.assistant_request_fields(model_id) == {
        "output_config": {"effort": "low"}
    }


@pytest.mark.parametrize(
    "model_id",
    [
        HAIKU_4_5,
        "global.anthropic.claude-sonnet-4-6",
        NOVA,
        "us.amazon.nova-2-lite-v1:0",
    ],
)
def test_other_models_ignore_the_setting(configured, model_id) -> None:
    configured({"assistantReasoningEffort": "high"})
    assert assistant.assistant_request_fields(model_id) is None


# ── the request the real SDK builds ─────────────────────────────────────────


def _request(model_id: str):
    strands_models = pytest.importorskip(
        "strands.models",
        reason="strands-agents is not installed (make setup-cli-dev installs the pinned version)",
    )
    import boto3  # pylint: disable=import-outside-toplevel

    kwargs, system = assistant.prompt_cache_plan(
        model_id, "You are a meeting assistant."
    )
    model_kwargs = {
        "model_id": model_id,
        "boto_session": boto3.Session(region_name="us-east-1"),
    }
    fields = assistant.assistant_request_fields(model_id)
    if fields:
        model_kwargs["additional_request_fields"] = fields
    if kwargs is not None:
        model_kwargs["cache_config"] = strands_models.CacheConfig(**kwargs)
    model = strands_models.BedrockModel(**model_kwargs)
    spec = {
        "name": "lookup",
        "description": "Look something up.",
        "inputSchema": {"json": {"type": "object", "properties": {}}},
    }
    system_blocks = system if isinstance(system, list) else [{"text": system}]
    return model.format_request(
        [{"role": "user", "content": [{"text": "What did we decide?"}]}],
        tool_specs=[spec],
        system_prompt_content=system_blocks,
    )


@pytest.mark.parametrize("model_id", [HAIKU_5_5, "us.anthropic.claude-haiku-5-5"])
def test_the_claude_5_request_carries_the_effort_and_the_cache_points(
    configured, model_id
) -> None:
    configured({})
    request = _request(model_id)
    assert request["additionalModelRequestFields"] == {
        "output_config": {"effort": "low"}
    }
    assert "thinking" not in request["additionalModelRequestFields"]
    assert any("cachePoint" in block for block in request["system"])
    assert any("cachePoint" in block for block in request["messages"][-1]["content"])


@pytest.mark.parametrize("model_id", [HAIKU_4_5, NOVA])
def test_an_earlier_model_request_carries_no_additional_fields(
    configured, model_id
) -> None:
    configured({})
    assert "additionalModelRequestFields" not in _request(model_id)
