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
