#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The meeting assistant and the troubleshooting agent read their models per request.

Each prefers the Amazon Bedrock Models section of the Configuration page, and
uses the setting's shipped default (setting_defaults.json) when the section does
not name a model.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

import strands_meeting_assist_function as assistant  # noqa: E402
import troubleshooting_agent_function as troubleshooting  # noqa: E402
from lma_config import parameters  # noqa: E402


class _Store:
    def __init__(self, values):
        self.values = values

    def get_section(self, _name):
        return SimpleNamespace(values=self.values)


@pytest.fixture()
def configured(monkeypatch):
    def install(values):
        monkeypatch.setattr(parameters, "_SHARED_STORE", _Store(values))

    return install


SHIPPED = parameters.shipped_setting_defaults()["language_models"]


def test_the_assistant_prefers_the_configured_models(configured) -> None:
    configured(
        {
            "meetingAssistModelId": "us.anthropic.claude-sonnet-4-6",
            "knowledgeBaseModelId": "global.anthropic.claude-sonnet-4-6",
        }
    )
    assert assistant.configured_agent_model_id() == "us.anthropic.claude-sonnet-4-6"
    assert assistant.configured_kb_model_id() == "global.anthropic.claude-sonnet-4-6"


def test_without_a_saved_model_the_assistant_uses_the_shipped_defaults(configured) -> None:
    configured({})
    assert assistant.configured_agent_model_id() == SHIPPED["meetingAssistModelId"]
    assert assistant.configured_kb_model_id() == SHIPPED["knowledgeBaseModelId"]


def test_the_troubleshooting_agent_runs_on_the_assistants_configured_model(configured) -> None:
    configured({"meetingAssistModelId": "us.anthropic.claude-sonnet-4-6"})
    assert troubleshooting.configured_model_id() == "us.anthropic.claude-sonnet-4-6"


def test_without_a_saved_model_the_troubleshooting_agent_uses_the_shipped_default(
    configured,
) -> None:
    configured({})
    assert troubleshooting.configured_model_id() == SHIPPED["meetingAssistModelId"]


def test_with_neither_the_shipped_default_applies(monkeypatch) -> None:
    monkeypatch.setattr(parameters, "_SHARED_STORE", None)
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    assert assistant.configured_agent_model_id() == assistant.DEFAULT_AGENT_MODEL_ID
    assert troubleshooting.configured_model_id() == troubleshooting.FALLBACK_MODEL_ID


def test_without_the_configuration_layer_the_environment_is_read(monkeypatch) -> None:
    """A standalone deployment of the stack carries no layer, and still runs."""
    import importlib
    import sys

    monkeypatch.setitem(sys.modules, "lma_config", None)  # makes the import fail
    monkeypatch.delitem(sys.modules, "configuration_settings", raising=False)
    fallback = importlib.import_module("configuration_settings")
    monkeypatch.setenv("MODEL_ID", "env.agent-model")
    assert (
        fallback.read_setting("language_models", "meetingAssistModelId", env_var="MODEL_ID", default="d")
        == "env.agent-model"
    )
    monkeypatch.delenv("MODEL_ID")
    assert (
        fallback.read_setting("language_models", "meetingAssistModelId", env_var="MODEL_ID", default="d")
        == "d"
    )
    monkeypatch.delitem(sys.modules, "configuration_settings")


def test_the_models_run_within_the_configured_geography(configured, monkeypatch) -> None:
    configured(
        {
            "inferenceGeography": "geography",
            "meetingAssistModelId": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
            "knowledgeBaseModelId": "global.anthropic.claude-sonnet-4-6",
        }
    )
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.delenv("KB_REGION", raising=False)
    assert assistant.configured_agent_model_id() == "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert assistant.configured_kb_model_id() == "eu.anthropic.claude-sonnet-4-6"
    assert troubleshooting.configured_model_id() == "eu.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_the_knowledge_base_model_check_ignores_the_profile_prefix() -> None:
    for prefix in ("us.", "global.", "eu.", ""):
        model_id = f"{prefix}anthropic.claude-opus-4-1-20250805-v1:0"
        assert not assistant.kb_model_supports_retrieve_and_generate(model_id)
    assert assistant.build_kb_model_arn("jp.anthropic.claude-sonnet-4-6", "ap-northeast-1", "123456789012") == (
        "arn:aws:bedrock:ap-northeast-1:123456789012:inference-profile/jp.anthropic.claude-sonnet-4-6"
    )


def test_without_the_configuration_layer_model_ids_are_used_as_given(monkeypatch) -> None:
    import importlib
    import sys

    monkeypatch.setitem(sys.modules, "lma_config", None)  # makes the import fail
    monkeypatch.delitem(sys.modules, "configuration_settings", raising=False)
    fallback = importlib.import_module("configuration_settings")
    assert fallback.resolve_model_id("global.anthropic.claude-sonnet-5-5", region="eu-west-1") == (
        "global.anthropic.claude-sonnet-5-5"
    )
    assert fallback.base_model_id("au.anthropic.claude-sonnet-5-5") == "anthropic.claude-sonnet-5-5"
    assert fallback.model_arn("amazon.nova-lite-v1:0", "us-east-1", "1") == (
        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0"
    )
    monkeypatch.delitem(sys.modules, "configuration_settings")
