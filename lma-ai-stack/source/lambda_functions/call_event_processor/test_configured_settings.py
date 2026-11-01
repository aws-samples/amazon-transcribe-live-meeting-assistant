# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The wake phrase and the transcript-hook mode are read from the configuration.

Each prefers its Configuration page setting (whose default is the stack
parameter) and falls back to what the processor read before: the deployment
settings' compiled wake phrase, and the hook-mode environment variable. A wake
phrase saved on the page that is not a regular expression gives way to the
deployment's own rather than waking the assistant on nothing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
# The configuration layer the deployed function carries, for lma_config.
CONFIG_LAYER = HERE.parents[1] / "lambda_layers" / "configuration_layer"
for path in (str(HERE), str(CONFIG_LAYER)):
    if path not in sys.path:
        sys.path.insert(0, path)

from lma_config import parameters  # noqa: E402
from test_graphql_response_handling import cep  # noqa: E402

DEPLOYED = re.compile(r"(OK|Okay)[.,! ]*[Aa]ssistant")


class _Store:
    def __init__(self, sections):
        self.sections = sections

    def get_section(self, name):
        return SimpleNamespace(values=self.sections.get(name, {}))


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(cep, "SETTINGS", {"AssistantWakePhraseRegEx": DEPLOYED}, raising=False)
    monkeypatch.delenv("ASSISTANT_WAKE_PHRASE_REGEX", raising=False)
    cep._compiled_wake_phrase.cache_clear()

    def install(sections):
        monkeypatch.setattr(parameters, "_SHARED_STORE", _Store(sections))

    return install


def test_the_configured_wake_phrase_wakes_the_assistant(configured) -> None:
    configured({"meeting_assist": {"wakePhraseRegex": "hey lma"}})
    pattern = cep.wake_phrase_pattern()
    assert pattern.pattern == "hey lma"
    assert cep.matches_wake_phrase(pattern, "so, hey lma, what was decided?")
    assert not cep.matches_wake_phrase(pattern, "OK assistant")


def test_without_one_the_deployment_wake_phrase_applies(configured) -> None:
    configured({})
    assert cep.wake_phrase_pattern() is DEPLOYED


def test_a_wake_phrase_that_is_not_a_regular_expression_gives_way(configured) -> None:
    configured({"meeting_assist": {"wakePhraseRegex": "(unclosed"}})
    assert cep.wake_phrase_pattern() is DEPLOYED
    assert any("not a regular expression" in r["message"] for r in cep.LOGGER.records)


def test_the_hook_mode_comes_from_the_configuration(configured, monkeypatch) -> None:
    monkeypatch.setenv("TRANSCRIPT_LAMBDA_HOOK_FUNCTION_NONPARTIAL_ONLY", "true")
    configured({"meeting_processing": {"transcriptHookNonPartialOnly": False}})
    assert cep.transcript_hook_nonpartial_only() is False


def test_without_one_the_hook_mode_is_the_parameters(configured, monkeypatch) -> None:
    monkeypatch.setenv("TRANSCRIPT_LAMBDA_HOOK_FUNCTION_NONPARTIAL_ONLY", "false")
    configured({})
    assert cep.transcript_hook_nonpartial_only() is False
    monkeypatch.delenv("TRANSCRIPT_LAMBDA_HOOK_FUNCTION_NONPARTIAL_ONLY")
    assert cep.transcript_hook_nonpartial_only() is True
