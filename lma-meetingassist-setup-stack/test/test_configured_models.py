#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The meeting assistant and the troubleshooting agent read their models per request.

Each prefers the Amazon Bedrock Models section of the Configuration page, whose
defaults are the MeetingAssistServiceBedrockModelID and
MeetingAssistKnowledgeBaseModelID stack parameters, and falls back to those
parameters' environment variables when the section does not name a model.
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

    monkeypatch.setenv("MODEL_ID", "env.agent-model")
    monkeypatch.setenv("KB_MODEL_ID", "env.kb-model")
    monkeypatch.setenv("TROUBLESHOOTING_MODEL_ID", "env.troubleshooting-model")
    return install


def test_the_assistant_prefers_the_configured_models(configured) -> None:
    configured(
        {
            "meetingAssistModelId": "us.anthropic.claude-sonnet-4-6",
            "knowledgeBaseModelId": "global.anthropic.claude-sonnet-4-6",
        }
    )
    assert assistant.configured_agent_model_id() == "us.anthropic.claude-sonnet-4-6"
    assert assistant.configured_kb_model_id() == "global.anthropic.claude-sonnet-4-6"


def test_the_assistant_falls_back_to_its_environment(configured) -> None:
    configured({})
    assert assistant.configured_agent_model_id() == "env.agent-model"
    assert assistant.configured_kb_model_id() == "env.kb-model"


def test_the_troubleshooting_agent_runs_on_the_assistants_configured_model(configured) -> None:
    configured({"meetingAssistModelId": "us.anthropic.claude-sonnet-4-6"})
    assert troubleshooting.configured_model_id() == "us.anthropic.claude-sonnet-4-6"


def test_the_troubleshooting_agent_falls_back_to_its_environment(configured) -> None:
    configured({})
    assert troubleshooting.configured_model_id() == "env.troubleshooting-model"


def test_with_neither_the_shipped_default_applies(monkeypatch) -> None:
    monkeypatch.setattr(parameters, "_SHARED_STORE", None)
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME", raising=False)
    monkeypatch.delenv("MODEL_ID", raising=False)
    monkeypatch.delenv("TROUBLESHOOTING_MODEL_ID", raising=False)
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
