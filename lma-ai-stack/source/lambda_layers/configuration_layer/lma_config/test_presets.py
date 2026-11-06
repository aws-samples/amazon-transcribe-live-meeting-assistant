#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the configuration library of presets.

A malformed preset would fail in front of an administrator, so every shipped
one is held to the shape the editor applies and to the section schemas, and the
healthcare preset to the prompts the healthcare domain ships. Each organization
preset is held to the values its description and the documentation give, so
none of them can change without a test saying so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import lma_config.presets as library
from lma_config.errors import InvalidConfiguration
from lma_config.parameters import shipped_setting_defaults

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
        ({"title": "t", "description": "d", "sections": {}}, "exactly title"),
        ({"title": "t", "description": "d", "kind": "add-on", "sections": {}}, "at least one"),
        (
            {
                "title": "t",
                "description": "d",
                "kind": "add-on",
                "sections": {"nope": {"mode": "merge", "values": {}}},
            },
            "not a configuration",
        ),
        (
            {
                "title": "t",
                "description": "d",
                "kind": "add-on",
                "sections": {"asr_engine": {"mode": "swap", "values": {}}},
            },
            "mode must",
        ),
        (
            {
                "title": "t",
                "description": "d",
                "kind": "add-on",
                "sections": {"asr_engine": {"values": {}}},
            },
            "exactly mode",
        ),
        (
            {
                "title": "t",
                "description": "d",
                "kind": "feature",
                "sections": {"asr_engine": {"mode": "merge", "values": {}}},
            },
            "kind must",
        ),
        (
            {
                "title": "t",
                "description": "d",
                "kind": "add-on",
                "order": "first",
                "sections": {"asr_engine": {"mode": "merge", "values": {}}},
            },
            "order must",
        ),
        (
            {
                "title": "t",
                "description": "d",
                "kind": "organization",
                "sections": {"retention": {"mode": "replace", "values": {}}},
            },
            "only merges",
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
                "kind": "add-on",
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


# ── organization presets and add-ons ────────────────────────────────────────

#: Every setting each organization preset sets, as the Configuration Guide's
#: table of organization presets lists them. A change to a preset has to change
#: this, and that table, too.
ORGANIZATION_VALUES = {
    "standard": {
        "meeting_consent": {
            "mode": "off",
            "consentRule": "each_participant",
            "allowPerMeetingOverride": True,
            "keepEvidenceScreenshots": True,
            "clientsWithoutConsent": "accept_with_warning",
            "leaveOnObjection": False,
        },
        "retention": {"transcriptDeletionAfterSummary": "off"},
        "recording": {"recordAudio": True},
        "virtual_participant": {"captureMeetingChat": True},
        "transcription": {"contentRedactionEnabled": False},
        "language_models": {"inferenceGeography": "global"},
    },
    "internal-meetings-notify": {
        "meeting_consent": {
            "mode": "notify",
            "consentRule": "each_participant",
            "allowPerMeetingOverride": True,
            "keepEvidenceScreenshots": True,
            "clientsWithoutConsent": "accept_with_warning",
            "leaveOnObjection": False,
        },
        "retention": {"transcriptDeletionAfterSummary": "off"},
        "recording": {"recordAudio": True},
        "virtual_participant": {"captureMeetingChat": True},
        "transcription": {"contentRedactionEnabled": False},
        "language_models": {"inferenceGeography": "global"},
    },
    "regulated-consent-required": {
        "meeting_consent": {
            "mode": "opt_in",
            "consentRule": "each_participant",
            "allowPerMeetingOverride": False,
            "keepEvidenceScreenshots": True,
            "clientsWithoutConsent": "reject",
            "leaveOnObjection": True,
        },
        "retention": {"transcriptDeletionAfterSummary": "after_15_minutes"},
        "recording": {"recordAudio": False},
        "virtual_participant": {"captureMeetingChat": False},
        "transcription": {"contentRedactionEnabled": True},
        "language_models": {"inferenceGeography": "geography"},
    },
}

ADD_ONS = {
    "healthcare-summary",
    "on-demand-asr-with-voice-separation",
    "translator-voice-assistant",
}


def test_the_organization_presets_are_standard_internal_and_regulated_in_that_order():
    assert [preset["Id"] for preset in library.organization_presets()] == list(ORGANIZATION_VALUES)


def test_every_other_shipped_preset_is_an_add_on():
    kinds = {preset["Id"]: preset["Kind"] for preset in library.shipped_presets()}
    assert {name for name, kind in kinds.items() if kind == library.ADD_ON} == ADD_ONS
    assert set(kinds) == ADD_ONS | set(ORGANIZATION_VALUES)


@pytest.mark.parametrize("preset_id", list(ORGANIZATION_VALUES))
def test_each_organization_preset_sets_exactly_its_documented_values(preset_id):
    sections = preset_by_id(preset_id)["Sections"]
    assert {name: change["mode"] for name, change in sections.items()} == dict.fromkeys(
        ORGANIZATION_VALUES[preset_id], "merge"
    )
    assert {name: change["values"] for name, change in sections.items()} == (
        ORGANIZATION_VALUES[preset_id]
    )


def test_the_standard_preset_holds_the_shipped_defaults():
    """Applied in the editor, it puts back what another organization preset changed."""
    shipped = shipped_setting_defaults()
    for name, file_name in (("meeting_consent", "MeetingConsent"), ("retention", "Retention")):
        shipped[name] = json.loads((DEFAULTS / f"{file_name}.json").read_text(encoding="utf-8"))
    for name, values in ORGANIZATION_VALUES["standard"].items():
        assert values == {key: shipped[name][key] for key in values}, name


def test_the_regulated_preset_names_the_controls_set_at_deploy_time():
    description = preset_by_id("regulated-consent-required")["Description"]
    for control in (
        "LogLevel",
        "MeetingRecordExpirationInDays",
        "TranscriptionExpirationInDays",
        "AudioRecordingExpirationInDays",
        "CloudWatchLogsExpirationInDays",
        "EnableVideoRecording",
        "docs/infrastructure-and-security.md",
    ):
        assert control in description, control


def test_organization_presets_that_set_different_settings_are_refused():
    standard = preset_by_id("standard")
    partial = json.loads(json.dumps(standard))
    partial["Id"] = "partial"
    del partial["Sections"]["recording"]
    with pytest.raises(library.InvalidPreset, match="same settings"):
        library._check_organization_presets([standard, partial])  # noqa: SLF001


def test_one_organization_preset_over_another_leaves_nothing_of_the_first():
    body = {"meeting_consent": {"mode": "off", "verifierModelId": "amazon.nova-pro-v1:0"}}
    regulated = apply_preset(body, preset_by_id("regulated-consent-required"))
    back = apply_preset(regulated, preset_by_id("standard"))
    for name, values in ORGANIZATION_VALUES["standard"].items():
        assert {key: back[name][key] for key in values} == values
    # A setting no organization preset names stays as it was.
    assert back["meeting_consent"]["verifierModelId"] == "amazon.nova-pro-v1:0"
