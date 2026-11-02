# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The wake phrase and the transcript-hook mode are read from the configuration.

Each prefers its Configuration page setting and falls back to the setting's
shipped default in setting_defaults.json. A wake phrase saved on the page that is
not a regular expression gives way to the shipped one rather than waking the
assistant on nothing.
"""

from __future__ import annotations

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

from lma_config import parameters, shipped_setting_defaults  # noqa: E402
from test_graphql_response_handling import cep  # noqa: E402

#: The shipped wake phrase: what applies when none is saved, or the saved one
#: does not compile.
SHIPPED = shipped_setting_defaults()["meeting_assist"]["wakePhraseRegex"]


class _Store:
    def __init__(self, sections):
        self.sections = sections

    def get_section(self, name):
        return SimpleNamespace(values=self.sections.get(name, {}))


@pytest.fixture()
def configured(monkeypatch):
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


def test_without_one_the_shipped_wake_phrase_applies(configured) -> None:
    configured({})
    assert cep.wake_phrase_pattern().pattern == SHIPPED


def test_a_wake_phrase_that_is_not_a_regular_expression_gives_way(configured) -> None:
    configured({"meeting_assist": {"wakePhraseRegex": "(unclosed"}})
    assert cep.wake_phrase_pattern().pattern == SHIPPED
    assert any("not a regular expression" in r["message"] for r in cep.LOGGER.records)


def test_the_hook_mode_comes_from_the_configuration(configured) -> None:
    configured({"meeting_processing": {"transcriptHookNonPartialOnly": False}})
    assert cep.transcript_hook_nonpartial_only() is False


def test_without_one_the_hook_mode_is_the_shipped_default(configured) -> None:
    configured({})
    assert cep.transcript_hook_nonpartial_only() is True


def test_the_agenda_threshold_comes_from_the_configuration(configured) -> None:
    configured({"meeting_agenda": {"autoDeriveAfterSeconds": 120}})
    assert cep.agenda_derivation_threshold() == 120.0


def test_an_agenda_threshold_of_zero_saved_on_the_page_turns_the_pass_off(configured) -> None:
    configured({"meeting_agenda": {"autoDeriveAfterSeconds": 0}})
    assert cep.agenda_derivation_threshold() == 0.0
    assert not cep.agenda_derivation_due("call-1", 9999.0, cep.agenda_derivation_threshold(), set())


def test_without_one_the_agenda_threshold_is_the_shipped_default(configured) -> None:
    configured({})
    assert cep.agenda_derivation_threshold() == 300.0
