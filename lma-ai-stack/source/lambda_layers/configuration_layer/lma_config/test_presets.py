#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the configuration library of presets.

A malformed preset would fail in front of an administrator, so every shipped
one is held to the shape the editor applies and to the section schemas. Each
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
from lma_config.presets import apply_preset, preset_by_id, preset_offered

HERE = Path(__file__).resolve().parent
DEFAULTS = HERE.parents[2] / "lambda_functions" / "configuration_seeder" / "defaults"

#: Every setting each preset sets, as the Configuration Guide's table of
#: configuration presets lists them, in the order the presets are listed. A
#: change to a preset has to change this, and that table, too.
PRESET_VALUES = {
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
    "regulated-notify": {
        "meeting_consent": {
            "mode": "notify",
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


def test_the_presets_are_standard_internal_regulated_and_regulated_notify_in_that_order():
    assert [preset["Id"] for preset in library.shipped_presets()] == list(PRESET_VALUES)
    assert {path.stem for path in library.PRESET_DIR.glob("*.json")} == set(PRESET_VALUES)
    for preset in library.shipped_presets():
        assert preset["Title"] and preset["Description"]


@pytest.mark.parametrize("preset_id", list(PRESET_VALUES))
def test_each_preset_sets_exactly_its_documented_values(preset_id):
    sections = preset_by_id(preset_id)["Sections"]
    assert {name: change["mode"] for name, change in sections.items()} == dict.fromkeys(
        PRESET_VALUES[preset_id], "merge"
    )
    assert {name: change["values"] for name, change in sections.items()} == (
        PRESET_VALUES[preset_id]
    )


def test_the_standard_preset_holds_the_shipped_defaults():
    """Applied in the editor, it puts back what another preset changed."""
    shipped = shipped_setting_defaults()
    for name, file_name in (("meeting_consent", "MeetingConsent"), ("retention", "Retention")):
        shipped[name] = json.loads((DEFAULTS / f"{file_name}.json").read_text(encoding="utf-8"))
    for name, values in PRESET_VALUES["standard"].items():
        assert values == {key: shipped[name][key] for key in values}, name


@pytest.mark.parametrize("preset_id", ["regulated-consent-required", "regulated-notify"])
def test_the_regulated_presets_name_the_controls_set_at_deploy_time(preset_id):
    description = preset_by_id(preset_id)["Description"]
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


def test_regulated_notify_differs_from_regulated_only_in_the_consent_mode():
    """The two Regulated presets keep the same posture apart from when capture starts."""
    regulated = json.loads(json.dumps(PRESET_VALUES["regulated-consent-required"]))
    regulated["meeting_consent"]["mode"] = "notify"
    assert PRESET_VALUES["regulated-notify"] == regulated


@pytest.mark.parametrize(
    "document, problem",
    [
        ({"title": "t", "description": "d"}, "exactly title"),
        ({"title": "t", "description": "d", "kind": "x", "sections": {}}, "exactly title"),
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
                "sections": {"retention": {"mode": "replace", "values": {}}},
            },
            "mode must",
        ),
        (
            {"title": "t", "description": "d", "sections": {"retention": {"values": {}}}},
            "exactly mode",
        ),
        (
            {
                "title": "t",
                "description": "d",
                "order": "first",
                "sections": {"retention": {"mode": "merge", "values": {}}},
            },
            "order must",
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
                "sections": {"retention": {"mode": "merge", "values": {"mode": "soon"}}},
            },
        )


def test_a_preset_id_must_be_hyphenated_lower_case_words():
    with pytest.raises(library.InvalidPreset):
        library._checked("Bad_Name", {})  # noqa: SLF001


def test_presets_that_set_different_settings_are_refused():
    standard = preset_by_id("standard")
    partial = json.loads(json.dumps(standard))
    partial["Id"] = "partial"
    del partial["Sections"]["recording"]
    with pytest.raises(library.InvalidPreset, match="same settings"):
        library._check_same_settings([standard, partial])  # noqa: SLF001


def test_a_copy_is_served_so_a_caller_cannot_change_the_library():
    library.shipped_presets()[0]["Sections"].clear()
    assert library.shipped_presets()[0]["Sections"]


def test_an_unknown_preset_id_is_refused():
    with pytest.raises(library.InvalidPreset):
        preset_by_id("no-such-preset")


# ── applying a preset ───────────────────────────────────────────────────────


def test_a_preset_changes_only_the_settings_it_names():
    body = {"meeting_consent": {"mode": "off", "verifierModelId": "amazon.nova-pro-v1:0"}}
    applied = apply_preset(body, preset_by_id("internal-meetings-notify"))
    assert applied["meeting_consent"]["mode"] == "notify"
    assert applied["meeting_consent"]["verifierModelId"] == "amazon.nova-pro-v1:0"
    assert body["meeting_consent"]["mode"] == "off"  # not changed in place


def test_one_preset_over_another_leaves_nothing_of_the_first():
    body = {"meeting_consent": {"mode": "off", "verifierModelId": "amazon.nova-pro-v1:0"}}
    regulated = apply_preset(body, preset_by_id("regulated-consent-required"))
    back = apply_preset(regulated, preset_by_id("standard"))
    for name, values in PRESET_VALUES["standard"].items():
        assert {key: back[name][key] for key in values} == values
    # A setting no preset names stays as it was.
    assert back["meeting_consent"]["verifierModelId"] == "amazon.nova-pro-v1:0"


def test_a_preset_is_offered_only_where_every_setting_it_names_is():
    needs_asr = {
        "Sections": {"asr_engine": {"mode": "merge", "values": {"streamingEngineMicrovm": True}}}
    }
    assert preset_offered(needs_asr, {"AsrEngineAvailable": "true"}) is True
    assert preset_offered(needs_asr, {}) is False
    for preset in library.shipped_presets():
        assert preset_offered(preset, {}) is True, preset["Id"]
