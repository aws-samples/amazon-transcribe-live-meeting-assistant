#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the configuration library of presets.

A malformed preset would fail in front of an administrator, so every shipped
one is held to the shape the editor applies and to the section schemas, and the
healthcare preset to the prompts the healthcare domain ships.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import lma_config.presets as library
from lma_config.errors import InvalidConfiguration

HERE = Path(__file__).resolve().parent
DEFAULTS = HERE.parents[2] / "lambda_functions" / "configuration_seeder" / "defaults"


def test_the_library_ships_presets_and_each_is_well_formed():
    shipped = library.shipped_presets()
    assert [preset["Id"] for preset in shipped] == sorted(
        path.stem for path in library.PRESET_DIR.glob("*.json")
    )
    assert len(shipped) >= 3
    for preset in shipped:
        assert preset["Title"] and preset["Description"]


def test_the_healthcare_preset_holds_exactly_the_prompts_that_domain_ships():
    """Two copies of the same prompts, so a change to one has to reach the other."""
    shipped = json.loads(
        (DEFAULTS / "LLMPromptHealthcareSummaryTemplate.json").read_text(encoding="utf-8")
    )
    numbered = {
        f"{index}#{title}": prompt for index, (title, prompt) in enumerate(shipped.items(), 1)
    }
    preset = {entry["Id"]: entry for entry in library.shipped_presets()}["healthcare-summary"]
    assert preset["Sections"]["summary_prompt_templates"] == {"mode": "replace", "values": numbered}


@pytest.mark.parametrize(
    "document, problem",
    [
        ({"title": "t", "description": "d"}, "exactly title"),
        ({"title": "t", "description": "d", "sections": {}}, "at least one"),
        (
            {
                "title": "t",
                "description": "d",
                "sections": {"nope": {"mode": "merge", "values": {}}},
            },
            "not a configuration",
        ),
        (
            {
                "title": "t",
                "description": "d",
                "sections": {"asr_engine": {"mode": "swap", "values": {}}},
            },
            "mode must",
        ),
        (
            {"title": "t", "description": "d", "sections": {"asr_engine": {"values": {}}}},
            "exactly mode",
        ),
    ],
)
def test_a_malformed_preset_is_refused(document, problem):
    with pytest.raises(library.InvalidPreset, match=problem):
        library._checked("a-preset", document)  # noqa: SLF001 -- the check under test


def test_a_preset_whose_values_the_schema_refuses_is_refused():
    with pytest.raises(InvalidConfiguration):
        library._checked(  # noqa: SLF001
            "a-preset",
            {
                "title": "t",
                "description": "d",
                "sections": {
                    "asr_engine": {"mode": "merge", "values": {"streamingEngineMicrovm": "yes"}}
                },
            },
        )


def test_a_preset_id_must_be_hyphenated_lower_case_words():
    with pytest.raises(library.InvalidPreset):
        library._checked("Bad_Name", {})  # noqa: SLF001


def test_a_copy_is_served_so_a_caller_cannot_change_the_library():
    library.shipped_presets()[0]["Sections"].clear()
    assert library.shipped_presets()[0]["Sections"]


# ── applying a preset at deploy time ────────────────────────────────────────

from lma_config.presets import apply_preset, preset_by_id, preset_offered  # noqa: E402


def test_a_merge_preset_changes_only_the_settings_it_names():
    body = {"nova_sonic_config": {"meetingMode": "normal", "voiceId": "tiffany"}}
    applied = apply_preset(body, preset_by_id("translator-voice-assistant"))
    assert applied["nova_sonic_config"]["meetingMode"] == "translator"
    assert applied["nova_sonic_config"]["voiceId"] == "tiffany"
    assert body["nova_sonic_config"]["meetingMode"] == "normal"  # not changed in place


def test_a_replace_preset_turns_off_the_shipped_entries_it_does_not_name():
    preset = preset_by_id("healthcare-summary")
    change = preset["Sections"]["summary_prompt_templates"]
    assert change["mode"] == "replace"
    body = {"summary_prompt_templates": {"99#NOT IN THE PRESET": "a shipped prompt"}}
    applied = apply_preset(body, preset)["summary_prompt_templates"]
    assert applied["99#NOT IN THE PRESET"] == "NONE"
    assert set(change["values"]) <= set(applied)


def test_a_preset_needing_the_asr_engine_is_offered_only_with_it():
    preset = preset_by_id("on-demand-asr-with-voice-separation")
    assert preset_offered(preset, {"AsrEngineAvailable": "true"}) is True
    assert preset_offered(preset, {}) is False
    assert preset_offered(preset_by_id("translator-voice-assistant"), {}) is True


def test_an_unknown_preset_id_is_refused():
    with pytest.raises(library.InvalidPreset):
        preset_by_id("no-such-preset")


def _shipped_summary_section():
    shipped = json.loads((DEFAULTS / "LLMPromptSummaryTemplate.json").read_text(encoding="utf-8"))
    return {f"{index}#{title}": prompt for index, (title, prompt) in enumerate(shipped.items(), 1)}


def test_the_healthcare_preset_turns_the_chat_section_off():
    """Its Managed profile is the preset over the shipped prompts, which include the chat."""
    body = {"summary_prompt_templates": _shipped_summary_section()}
    applied = apply_preset(body, preset_by_id("healthcare-summary"))["summary_prompt_templates"]
    assert applied["7#CHAT & SHARED LINKS"] == "NONE"


@pytest.mark.parametrize(
    "preset_id", ["translator-voice-assistant", "on-demand-asr-with-voice-separation"]
)
def test_presets_that_leave_the_summary_alone_keep_the_chat_section(preset_id):
    body = {"summary_prompt_templates": _shipped_summary_section()}
    applied = apply_preset(body, preset_by_id(preset_id))["summary_prompt_templates"]
    assert "{chat}" in applied["7#CHAT & SHARED LINKS"]
