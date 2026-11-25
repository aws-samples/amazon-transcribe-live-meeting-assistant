#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
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

#: The categories of sensitive personal information the minimised-content
#: summary profile tells the model to leave out. The meeting assistant's rule
#: (lma-meetingassist-setup-stack/test/test_sensitive_information_rule.py) names
#: the same ones.
SENSITIVE_CATEGORIES = (
    "health and medical information (conditions, symptoms, diagnoses, treatments, medication, "
    "disabilities, pregnancy and mental health), racial or ethnic origin, religious or "
    "philosophical beliefs, political opinions, trade union membership, sex life and sexual "
    "orientation, gender identity, genetic and biometric data, and criminal convictions or "
    "allegations"
)

#: What the minimised-content summary profile adds after each default section it uses.
SUMMARY_SENSITIVE_INFORMATION_RULE = (
    "<br><br>Leave out sensitive personal information about anyone, even where it was said in "
    "the meeting, typed in the chat or shown on the shared screen: " + SENSITIVE_CATEGORIES + ". "
    "Do not quote, paraphrase or hint at it. Where it bears on a decision or an action item, "
    "write only that a personal matter was raised, never what it was or whom it concerns."
)

#: The meeting assistant and summary settings at their shipped defaults, which
#: the four presets other than the two hosting presets set, and Minimised content
#: with the sensitive personal information switch on.
ASSISTANT_DEFAULTS = {
    "meeting_assist": {
        "inMeetingRequestTools": "current_meeting_only",
        "inMeetingContextAccess": "none",
        "inMeetingRequesters": "anyone",
        "webSearchEnabled": True,
        "mcpServersEnabled": True,
        "guidance": {"defaultEnabled": False},
        "whiteboards": {"defaultEnabled": False},
        "omitSensitivePersonalInformation": False,
    },
    "meeting_processing": {"defaultSummaryProfile": "default"},
}

#: Meeting Sharing, which every preset turns on: a Participant sees only the
#: meetings they are listed on, read-only and without the owner's own material.
SHARING_DEFAULTS = {"meeting_sharing": {"shareWithListedParticipants": True}}

#: The sentence every preset gives the consent notice when video is recorded.
VIDEO_NOTICE_TEXT = (
    "Video of the meeting, including shared screens and any visible camera feeds, is also recorded."
)

#: Every setting each preset sets, as the Configuration Guide's table of
#: configuration presets lists them, in the order the presets are listed. A
#: change to a preset has to change this, and that table, too.
PRESET_VALUES = {
    "standard": {
        "meeting_consent": {
            "videoNoticeText": VIDEO_NOTICE_TEXT,
            "mode": "off",
            "consentRule": "each_participant",
            "allowPerMeetingOverride": True,
            "keepEvidenceScreenshots": True,
            "clientsWithoutConsent": "accept_with_warning",
            "leaveOnObjection": False,
        },
        "retention": {
            "transcriptDeletionAfterSummary": "off",
            "recordingDeletionAfterSummary": "off",
        },
        "recording": {"recordAudio": True, "recordScreenVideo": True, "recordVpMeetingVideo": True},
        "virtual_participant": {"captureMeetingChat": True},
        "transcription": {"contentRedactionEnabled": False},
        "language_models": {"inferenceGeography": "global"},
        **ASSISTANT_DEFAULTS,
        **SHARING_DEFAULTS,
    },
    "internal-meetings-notify": {
        "meeting_consent": {
            "videoNoticeText": VIDEO_NOTICE_TEXT,
            "mode": "notify",
            "consentRule": "each_participant",
            "allowPerMeetingOverride": True,
            "keepEvidenceScreenshots": True,
            "clientsWithoutConsent": "accept_with_warning",
            "leaveOnObjection": False,
        },
        "retention": {
            "transcriptDeletionAfterSummary": "off",
            "recordingDeletionAfterSummary": "off",
        },
        "recording": {"recordAudio": True, "recordScreenVideo": True, "recordVpMeetingVideo": True},
        "virtual_participant": {"captureMeetingChat": True},
        "transcription": {"contentRedactionEnabled": False},
        "language_models": {"inferenceGeography": "global"},
        **ASSISTANT_DEFAULTS,
        **SHARING_DEFAULTS,
    },
    "regulated-consent-required": {
        "meeting_consent": {
            "videoNoticeText": VIDEO_NOTICE_TEXT,
            "mode": "opt_in",
            "consentRule": "each_participant",
            "allowPerMeetingOverride": False,
            "keepEvidenceScreenshots": True,
            "clientsWithoutConsent": "reject",
            "leaveOnObjection": True,
        },
        "retention": {
            "transcriptDeletionAfterSummary": "after_15_minutes",
            "recordingDeletionAfterSummary": "immediately",
        },
        "recording": {
            "recordAudio": False,
            "recordScreenVideo": False,
            "recordVpMeetingVideo": False,
        },
        "virtual_participant": {"captureMeetingChat": False},
        "transcription": {"contentRedactionEnabled": True},
        "language_models": {"inferenceGeography": "geography"},
        **ASSISTANT_DEFAULTS,
        **SHARING_DEFAULTS,
    },
    "regulated-notify": {
        "meeting_consent": {
            "videoNoticeText": VIDEO_NOTICE_TEXT,
            "mode": "notify",
            "consentRule": "each_participant",
            "allowPerMeetingOverride": False,
            "keepEvidenceScreenshots": True,
            "clientsWithoutConsent": "reject",
            "leaveOnObjection": True,
        },
        "retention": {
            "transcriptDeletionAfterSummary": "after_15_minutes",
            "recordingDeletionAfterSummary": "immediately",
        },
        "recording": {
            "recordAudio": False,
            "recordScreenVideo": False,
            "recordVpMeetingVideo": False,
        },
        "virtual_participant": {"captureMeetingChat": False},
        "transcription": {"contentRedactionEnabled": True},
        "language_models": {"inferenceGeography": "geography"},
        **ASSISTANT_DEFAULTS,
        **SHARING_DEFAULTS,
    },
    "team-hosted-external": {
        "meeting_consent": {
            "videoNoticeText": VIDEO_NOTICE_TEXT,
            "mode": "opt_in",
            "consentRule": "each_participant",
            "allowPerMeetingOverride": False,
            "keepEvidenceScreenshots": True,
            "clientsWithoutConsent": "reject",
            "leaveOnObjection": True,
        },
        "retention": {
            "transcriptDeletionAfterSummary": "off",
            "recordingDeletionAfterSummary": "off",
        },
        "recording": {
            "recordAudio": False,
            "recordScreenVideo": False,
            "recordVpMeetingVideo": False,
        },
        "virtual_participant": {"captureMeetingChat": True},
        "transcription": {"contentRedactionEnabled": False},
        "language_models": {"inferenceGeography": "global"},
        "meeting_assist": {
            "inMeetingRequestTools": "current_meeting_only",
            "inMeetingContextAccess": "none",
            "inMeetingRequesters": "off",
            "webSearchEnabled": False,
            "mcpServersEnabled": False,
            "guidance": {"defaultEnabled": True},
            "whiteboards": {"defaultEnabled": False},
            "omitSensitivePersonalInformation": False,
        },
        "meeting_processing": {"defaultSummaryProfile": "discovery-session"},
        **SHARING_DEFAULTS,
    },
    "customer-account": {
        "meeting_consent": {
            "videoNoticeText": VIDEO_NOTICE_TEXT,
            "mode": "opt_in",
            "consentRule": "each_participant",
            "allowPerMeetingOverride": True,
            "keepEvidenceScreenshots": True,
            "clientsWithoutConsent": "reject",
            "leaveOnObjection": False,
        },
        "retention": {
            "transcriptDeletionAfterSummary": "off",
            "recordingDeletionAfterSummary": "off",
        },
        "recording": {
            "recordAudio": False,
            "recordScreenVideo": True,
            "recordVpMeetingVideo": True,
        },
        "virtual_participant": {"captureMeetingChat": True},
        "transcription": {"contentRedactionEnabled": False},
        "language_models": {"inferenceGeography": "global"},
        "meeting_assist": {
            "inMeetingRequestTools": "current_meeting_only",
            "inMeetingContextAccess": "none",
            "inMeetingRequesters": "anyone",
            "webSearchEnabled": False,
            "mcpServersEnabled": True,
            "guidance": {"defaultEnabled": False},
            "whiteboards": {"defaultEnabled": False},
            "omitSensitivePersonalInformation": False,
        },
        "meeting_processing": {"defaultSummaryProfile": "default"},
        **SHARING_DEFAULTS,
    },
    "minimised-content": {
        "meeting_consent": {
            "videoNoticeText": VIDEO_NOTICE_TEXT,
            "mode": "opt_in",
            "consentRule": "each_participant",
            "allowPerMeetingOverride": False,
            "keepEvidenceScreenshots": True,
            "clientsWithoutConsent": "reject",
            "leaveOnObjection": True,
        },
        "retention": {
            "transcriptDeletionAfterSummary": "immediately",
            "recordingDeletionAfterSummary": "immediately",
        },
        "recording": {
            "recordAudio": False,
            "recordScreenVideo": False,
            "recordVpMeetingVideo": False,
        },
        "virtual_participant": {"captureMeetingChat": False},
        "transcription": {"contentRedactionEnabled": True},
        "language_models": {"inferenceGeography": "geography"},
        "meeting_assist": {
            **ASSISTANT_DEFAULTS["meeting_assist"],
            "omitSensitivePersonalInformation": True,
        },
        "meeting_processing": {"defaultSummaryProfile": "minimised-content"},
        **SHARING_DEFAULTS,
    },
}


def test_the_presets_are_listed_in_their_documented_order():
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


def _as_named(value, shipped):
    """The shipped value of what a preset names; of an object, only the keys it names."""
    if isinstance(value, dict) and isinstance(shipped, dict):
        return {key: _as_named(part, shipped.get(key)) for key, part in value.items()}
    return shipped


def test_the_standard_preset_holds_the_shipped_defaults():
    """Applied in the editor, it puts back what another preset changed."""
    shipped = shipped_setting_defaults()
    for name, file_name in (("meeting_consent", "MeetingConsent"), ("retention", "Retention")):
        shipped[name] = json.loads((DEFAULTS / f"{file_name}.json").read_text(encoding="utf-8"))
    for name, values in PRESET_VALUES["standard"].items():
        assert values == {key: _as_named(values[key], shipped[name][key]) for key in values}, name


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
    # PRESET_VALUES stands for the shipped JSON files only because
    # test_the_presets_are_listed_in_their_documented_order
    # holds each file to it; keep the two tests together.
    regulated = json.loads(json.dumps(PRESET_VALUES["regulated-consent-required"]))
    regulated["meeting_consent"]["mode"] = "notify"
    assert PRESET_VALUES["regulated-notify"] == regulated


def test_the_team_hosted_preset_answers_no_request_made_in_the_meeting():
    """Guests from several organizations share the deployment; none of them may ask."""
    assistant = preset_by_id("team-hosted-external")["Sections"]["meeting_assist"]["values"]
    assert assistant["inMeetingRequesters"] == "off"
    assert assistant["inMeetingRequestTools"] == "current_meeting_only"
    assert assistant["inMeetingContextAccess"] == "none"
    assert assistant["webSearchEnabled"] is False
    assert assistant["mcpServersEnabled"] is False


def test_the_team_hosted_preset_turns_live_guidance_on_for_new_meetings_and_keeps_its_cadence():
    """Guidance goes to the owner and shared users only, never to the meeting's guests."""
    assistant = preset_by_id("team-hosted-external")["Sections"]["meeting_assist"]["values"]
    assert assistant["guidance"] == {"defaultEnabled": True}
    shipped = {"guidance": {"defaultEnabled": False, "intervalSeconds": 120}}
    applied = apply_preset({"meeting_assist": shipped}, preset_by_id("team-hosted-external"))
    assert applied["meeting_assist"]["guidance"] == {"defaultEnabled": True, "intervalSeconds": 120}


def test_the_team_hosted_preset_requires_each_participants_consent():
    consent = preset_by_id("team-hosted-external")["Sections"]["meeting_consent"]["values"]
    assert consent["mode"] == "opt_in"
    assert consent["consentRule"] == "each_participant"
    # Off: a meeting may not switch to the owner attesting for everyone.
    assert consent["allowPerMeetingOverride"] is False


def test_the_team_hosted_preset_names_the_controls_set_at_deploy_time():
    description = preset_by_id("team-hosted-external")["Description"]
    for control in (
        "EnableVideoRecording",
        "TranscriptionExpirationInDays",
        "MeetingRecordExpirationInDays",
        "AllowedSignUpEmailDomain",
        "docs/hosting-models.md",
    ):
        assert control in description, control


def test_the_customer_account_preset_keeps_the_summary_profile_and_requesters_open():
    sections = preset_by_id("customer-account")["Sections"]
    assert sections["meeting_assist"]["values"]["inMeetingRequesters"] == "anyone"
    assert sections["meeting_assist"]["values"]["webSearchEnabled"] is False
    assert sections["meeting_processing"]["values"]["defaultSummaryProfile"] == "default"
    assert "docs/hosting-models.md" in preset_by_id("customer-account")["Description"]


def test_every_preset_sets_the_meeting_assistant_and_summary_settings():
    """Applying any preset after a hosting preset puts these settings back."""
    for preset_id, values in PRESET_VALUES.items():
        assert set(values["meeting_assist"]) == set(ASSISTANT_DEFAULTS["meeting_assist"]), preset_id
        assert set(values["meeting_processing"]) == {"defaultSummaryProfile"}, preset_id


def _shipped_summary_profiles():
    return json.loads((DEFAULTS / "SummaryProfiles.json").read_text(encoding="utf-8"))


def test_each_presets_default_summary_profile_ships_with_the_release():
    """A name that is not a shipped profile would summarize new meetings with the default."""
    shipped = set(_shipped_summary_profiles()) | {"default"}
    for preset_id, values in PRESET_VALUES.items():
        assert values["meeting_processing"]["defaultSummaryProfile"] in shipped, preset_id


def test_only_minimised_content_turns_the_assistants_sensitive_information_rule_on():
    """The rule is a switch, so no preset touches the Assistant instructions an Admin wrote."""
    for preset_id in PRESET_VALUES:
        values = preset_by_id(preset_id)["Sections"]["meeting_assist"]["values"]
        assert "systemPrompt" not in values, preset_id
        expected = preset_id == "minimised-content"
        assert values["omitSensitivePersonalInformation"] is expected, preset_id


def test_applying_a_preset_keeps_the_assistant_instructions_an_admin_wrote():
    own = {"meeting_assist": {"systemPrompt": "You are Pat."}}
    for preset in library.shipped_presets():
        applied = apply_preset(own, preset)
        assert applied["meeting_assist"]["systemPrompt"] == "You are Pat.", preset["Id"]


def test_the_minimised_content_summary_profile_carries_the_rule_in_every_section():
    profile = _shipped_summary_profiles()["minimised-content"]
    default = json.loads((DEFAULTS / "LLMPromptSummaryTemplate.json").read_text(encoding="utf-8"))
    assert profile["structuredActionItems"] is False
    assert list(profile["prompts"]) == ["SUMMARY", "ACTIONS", "AGENDA COVERAGE"]
    for name, prompt in profile["prompts"].items():
        # The default section's prompt, unchanged, with the rule after it.
        assert prompt == default[name] + SUMMARY_SENSITIVE_INFORMATION_RULE, name
        assert SENSITIVE_CATEGORIES in prompt, name
    assert "not guaranteed" in profile["description"]


def test_the_minimised_content_preset_selects_its_summary_profile_and_deletes_the_transcript():
    sections = preset_by_id("minimised-content")["Sections"]
    assert sections["meeting_processing"]["values"]["defaultSummaryProfile"] == "minimised-content"
    assert sections["retention"]["values"]["transcriptDeletionAfterSummary"] == "immediately"
    assert sections["retention"]["values"]["recordingDeletionAfterSummary"] == "immediately"
    # No audio, and no video from any client the settings govern.
    assert sections["recording"]["values"] == {
        "recordAudio": False,
        "recordScreenVideo": False,
        "recordVpMeetingVideo": False,
    }
    consent = sections["meeting_consent"]["values"]
    assert (consent["mode"], consent["consentRule"], consent["allowPerMeetingOverride"]) == (
        "opt_in",
        "each_participant",
        False,
    )
    assert consent["clientsWithoutConsent"] == "reject"


def test_the_minimised_content_preset_names_the_controls_set_at_deploy_time():
    description = preset_by_id("minimised-content")["Description"]
    for control in (
        "EnableVideoRecording",
        "TranscriptKnowledgeBase",
        "LogLevel",
        "MeetingRecordExpirationInDays",
        "TranscriptionExpirationInDays",
        "AudioRecordingExpirationInDays",
        "CloudWatchLogsExpirationInDays",
        "docs/privacy-and-data-handling.md",
    ):
        assert control in description, control
    # The omission is the model following an instruction, and the description says so.
    assert "not a filter" in description


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
