#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the section schemas and the validator that applies them.

What is asserted, most consequential first:

* **Every registered section ships a schema, and every schema names a
  registered section.** A section with no schema could not be saved at all;
  a schema with no section would be an editor form for nothing.
* **What the release ships is valid against what the release checks.** The
  shipped baseline is built by the seeder from its ``defaults/`` files; if any of
  it failed its own schema, a Reset followed by a save of the defaults would be
  refused.
* **No schema uses a keyword the validator does not implement.** An ignored
  keyword is a constraint that looks enforced and is not.
* **The validator reports exactly what the shared fixture says**, the same file
  the web UI's copy is tested against.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

from lma_config import errors, schema, sections

HERE = Path(__file__).resolve().parent
AI_STACK = HERE.parents[3]
CASES = AI_STACK / "test" / "fixtures" / "configuration_schema_cases.json"
SEEDER = AI_STACK / "source" / "lambda_functions" / "configuration_seeder"


def _seeder():
    """The configuration seeder's module, for the baseline it builds."""
    spec = importlib.util.spec_from_file_location(
        "seeder_for_schema_tests", SEEDER / "lambda_function.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SEEDER))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SEEDER))
    return module


def test_every_registered_section_ships_a_schema_and_nothing_else_does():
    shipped = {path.stem for path in schema.SCHEMA_DIR.glob("*.json")}
    assert shipped == set(sections.registered_sections())


@pytest.mark.parametrize("name", sorted(sections.registered_sections()))
def test_every_schema_uses_only_keywords_the_validator_implements(name):
    assert schema.unknown_keywords(schema.section_schema(name)) == []


@pytest.mark.parametrize("name", sorted(sections.registered_sections()))
def test_every_schema_describes_an_object_and_declares_how_it_merges(name):
    document = schema.section_schema(name)
    assert document["type"] == "object"
    assert document.get("mergeStrategy") in ("merge", "replace")
    # A section is a delta over the baseline, so nothing at its top level can be
    # required: a profile that changes one setting stores that one setting.
    assert "required" not in document
    assert document.get("sectionLabel"), "the editor heads each section with this"


@pytest.mark.parametrize(
    "fragment",
    [{"propertyNames": {"maxLength": 40}}, {"items": [{"type": "string"}]}, {"maximun": 3}],
    ids=["names-max-length", "items-as-a-list", "misspelt"],
)
def test_a_constraint_the_validator_would_ignore_is_reported(fragment):
    assert schema.unknown_keywords({"type": "object", **fragment})


@pytest.mark.parametrize("name", sorted(sections.registered_sections()))
def test_no_pattern_uses_a_class_escape_the_two_languages_read_differently(name):
    """Python's \\d, \\w, \\s and \\b match non-ASCII characters; JavaScript's do not.

    The patterns spell their classes out, such as [0-9], so the server and the
    editor agree on what they accept.
    """
    text = json.dumps(schema.section_schema(name))
    for escape in ("\\\\d", "\\\\D", "\\\\w", "\\\\W", "\\\\s", "\\\\S", "\\\\b", "\\\\B"):
        assert escape not in text, f"{name} uses {escape}"


def test_the_chat_buttons_are_the_one_section_a_profile_replaces_whole():
    replaced = {
        name
        for name in sections.registered_sections()
        if schema.section_schema(name)["mergeStrategy"] == "replace"
    }
    assert replaced == {sections.CHAT_BUTTONS.name}


@pytest.mark.parametrize("domain", ["default", "healthcare"])
def test_the_shipped_baseline_is_valid_against_the_schemas(domain):
    body = _seeder().shipped_default_body(domain)
    assert set(body) == set(sections.registered_sections())
    for name, values in body.items():
        schema.validate_section(name, values)


def test_an_empty_section_is_valid_for_every_section():
    """What Reset to Defaults stores."""
    for name in sections.registered_sections():
        schema.validate_section(name, {})


def test_validate_section_names_the_section_in_every_problem():
    with pytest.raises(errors.InvalidConfiguration) as raised:
        schema.validate_section(
            "nova_sonic_config", {"promptMode": "sometimes", "voiceId": "", "bogus": 1}
        )
    assert raised.value.problems == [
        'nova_sonic_config › promptMode: must be one of "base", "inject", "replace"; '
        'got "sometimes"',
        "nova_sonic_config › voiceId: must not be empty",
        "nova_sonic_config › bogus: is not a setting this section has",
    ]
    assert str(raised.value).startswith("the configuration was not saved: 3 problems: ")


def test_a_section_that_is_not_an_object_is_refused():
    with pytest.raises(errors.InvalidConfiguration) as raised:
        schema.validate_section("asr_engine", ["streamingEngineMicrovm"])
    assert raised.value.problems == ["asr_engine: must be an object; got a list"]


def test_an_unregistered_section_is_refused():
    with pytest.raises(errors.UnknownConfigurationSection):
        schema.validate_section("not_a_section", {})


def test_the_message_spells_out_the_first_ten_problems_and_counts_the_rest():
    values = {"regions": {"default": {f"svc/{n}": {"units": -1} for n in range(12)}}}
    with pytest.raises(errors.InvalidConfiguration) as raised:
        schema.validate_section("call_pricing", values)
    assert len(raised.value.problems) == 12
    assert str(raised.value).endswith("; and 2 more")


@pytest.mark.parametrize("price", [math.inf, -math.inf, math.nan])
def test_a_price_that_is_not_a_finite_number_is_refused(price):
    """Python's json module reads NaN and Infinity, which JSON itself has no form for."""
    with pytest.raises(errors.InvalidConfiguration):
        schema.validate_section(
            "call_pricing", {"regions": {"default": {"transcribe/streaming": {"seconds": price}}}}
        )


@pytest.mark.parametrize(
    "values",
    [
        {"regions": {"default": {"transcribe/streaming": {"seconds": "0.1"}}}},
        {"regions": {"default": {"transcribe/streaming": {"seconds": True}}}},
        {"regions": {"Mars-1": {}}},
        {"pricedRegions": "us-east-1"},
        {"notes": {"transcribe/streaming": 3}},
        {"currency": "EUR"},
        {"discount": 0.1},
    ],
    ids=[
        "text-price",
        "boolean-price",
        "bad-region",
        "list-as-text",
        "note-not-text",
        "currency",
        "unknown",
    ],
)
def test_a_price_table_the_estimator_could_not_use_is_refused(values):
    with pytest.raises(errors.InvalidConfiguration):
        schema.validate_section("call_pricing", values)


def test_a_price_override_for_one_unit_is_valid():
    schema.validate_section(
        "call_pricing",
        {"regions": {"us-west-2": {"bedrock/amazon.nova-lite-v1:0": {"inputTokens": 7e-08}}}},
    )


def test_a_summary_prompt_turned_off_is_valid():
    schema.validate_section(
        "summary_prompt_templates", {"2#DETAILS": "NONE", "9#MINE": "Prompt {transcript}"}
    )


def test_a_summary_prompt_key_must_carry_its_number():
    with pytest.raises(errors.InvalidConfiguration):
        schema.validate_section("summary_prompt_templates", {"SUMMARY": "text"})


def test_a_chat_button_prompt_must_be_text():
    with pytest.raises(errors.InvalidConfiguration):
        schema.validate_section("chat_buttons", {"1#Summarize": {"prompt": "x"}})


_FIXTURE = json.loads(CASES.read_text(encoding="utf-8"))["cases"]


def test_the_shared_fixture_has_cases():
    assert len(_FIXTURE) >= 20


@pytest.mark.parametrize("case", _FIXTURE, ids=[case["name"] for case in _FIXTURE])
def test_the_validator_reports_what_the_shared_fixture_expects(case):
    assert schema.check(case["schema"], case["value"]) == case["problems"]


def test_a_copy_is_served_so_a_caller_cannot_change_the_schema():
    first = schema.section_schema("asr_engine")
    first["properties"].clear()
    assert schema.section_schema("asr_engine")["properties"]
