#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the meeting consent rules.

What matters is the gate: an opt-in meeting is never captured before its rule
is met or after anybody objects, a notify meeting is captured until somebody
objects, and a meeting with no record is judged by the deployment's mode.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lma_consent import rules

NOW = "2026-11-02T10:00:00+00:00"


def record(mode, rule=None, **extra):
    base = {"Mode": mode, "Rule": rule, "Events": [], "Participants": []}
    base.update(extra)
    return base


def reply(name, decision, source="chat"):
    return {"ParticipantName": name, "Decision": decision, "Source": source}


# ── the gate ───────────────────────────────────────────────────────────────


def test_off_is_never_gated() -> None:
    assert rules.evaluate(record("off"))[:2] == ("not_required", True)
    assert rules.capture_allowed(record("off"), "opt_in") is True


def test_a_meeting_without_a_record_follows_the_deployment_mode() -> None:
    assert rules.capture_allowed(None, "off") is True
    assert rules.capture_allowed(None, "notify") is True
    assert rules.capture_allowed(None, "opt_in") is False
    assert rules.capture_allowed({}, "opt_in") is False


def test_notify_is_captured_until_somebody_objects() -> None:
    meeting = record("notify")
    assert rules.evaluate(meeting)[:2] == ("pending", True)
    meeting["NoticePostedAt"] = NOW
    assert rules.evaluate(meeting)[:2] == ("granted", True)
    meeting["Events"].append(reply("Ana", "objection"))
    assert rules.evaluate(meeting)[:2] == ("objected", False)
    assert rules.capture_allowed(meeting, "off") is False


def test_each_participant_needs_every_listed_participant() -> None:
    meeting = record("opt_in", "each_participant")
    assert rules.evaluate(meeting)[:2] == ("pending", False)
    meeting["Participants"] = ["Ana Silva", "Bo Chen"]
    meeting["Events"].append(reply("ana silva", "consent"))
    status, allowed, reason = rules.evaluate(meeting)
    assert (status, allowed) == ("pending", False)
    assert "1 participant has not consented" in reason
    meeting["Events"].append(reply("BO  CHEN", "consent", source="voice"))
    assert rules.evaluate(meeting)[:2] == ("granted", True)


def test_a_participant_who_joins_later_withholds_capture_again() -> None:
    meeting = record(
        "opt_in",
        "each_participant",
        Participants=["Ana"],
        Events=[reply("Ana", "consent")],
    )
    assert rules.capture_allowed(meeting, "opt_in") is True
    meeting["Participants"].append("Cy")
    assert rules.capture_allowed(meeting, "opt_in") is False


def test_an_objection_outweighs_the_owners_attestation() -> None:
    meeting = record("opt_in", "owner_attests")
    assert rules.evaluate(meeting)[:2] == ("pending", False)
    meeting["Attestation"] = {"AttestedBy": "owner", "Method": "verbal"}
    assert rules.evaluate(meeting)[:2] == ("granted", True)
    meeting["Events"].append(reply("Bo", "objection"))
    assert rules.evaluate(meeting)[:2] == ("objected", False)


def test_an_objection_from_somebody_who_left_still_stands() -> None:
    meeting = record(
        "opt_in",
        "each_participant",
        Participants=["Ana"],
        Events=[reply("Ana", "consent"), reply("Bo", "objection")],
    )
    assert rules.evaluate(meeting)[:2] == ("objected", False)


def test_the_latest_reply_of_a_participant_counts() -> None:
    meeting = record(
        "opt_in",
        "each_participant",
        Participants=["Ana"],
        Events=[reply("Ana", "objection"), reply("Ana", "consent")],
    )
    assert rules.evaluate(meeting)[:2] == ("granted", True)
    meeting["Events"].append(reply("Ana", "objection"))
    assert rules.evaluate(meeting)[:2] == ("objected", False)


def test_an_attestation_entry_is_not_a_participants_reply() -> None:
    meeting = record(
        "opt_in",
        "each_participant",
        Participants=["Owner"],
        Events=[reply("Owner", "consent", source="attestation")],
    )
    assert rules.evaluate(meeting)[:2] == ("pending", False)


def test_an_unknown_mode_is_not_treated_as_consent_required() -> None:
    # A record written by a later release with a mode this one does not know
    # is read as not requiring consent rather than failing every reader; the
    # resolver never writes such a mode.
    assert rules.evaluate(record("something_new"))[0] == "not_required"


# ── status history ─────────────────────────────────────────────────────────


def test_a_change_of_status_is_logged_once() -> None:
    meeting = record("opt_in", "owner_attests")
    rules.apply_evaluation(meeting, NOW)
    rules.apply_evaluation(meeting, NOW)
    assert [entry["Status"] for entry in meeting["StatusHistory"]] == ["pending"]
    meeting["Attestation"] = {"AttestedBy": "owner"}
    rules.apply_evaluation(meeting, "2026-11-02T10:05:00+00:00")
    assert [entry["Status"] for entry in meeting["StatusHistory"]] == ["pending", "granted"]
    assert meeting["StatusHistory"][-1]["CaptureAllowed"] is True
    assert meeting["Status"] == "granted" and meeting["CaptureAllowed"] is True


def test_the_status_history_keeps_the_newest_entries() -> None:
    meeting = record("opt_in", "each_participant", Participants=["Ana"])
    for index in range(rules.MAX_STATUS_HISTORY + 10):
        decision = "consent" if index % 2 == 0 else "objection"
        meeting["Events"].append(reply("Ana", decision))
        rules.apply_evaluation(meeting, f"t{index:04d}")
    assert len(meeting["StatusHistory"]) == rules.MAX_STATUS_HISTORY
    assert meeting["StatusHistory"][-1]["At"] == f"t{rules.MAX_STATUS_HISTORY + 9:04d}"


def test_compacting_the_events_keeps_every_participants_latest_reply() -> None:
    events = [reply("Ana", "objection")] + [reply("Bo", "consent") for _ in range(40)]
    meeting = record("opt_in", "each_participant", Participants=["Bo"], Events=events)
    before = rules.evaluate(meeting)
    meeting["Events"] = rules.compact_events(events, 10)
    assert len(meeting["Events"]) == 10
    assert meeting["Events"][0] == reply("Ana", "objection")
    assert rules.evaluate(meeting) == before == ("objected", False, "1 participant objected")


def test_compacting_leaves_a_short_list_alone() -> None:
    events = [reply("Ana", "consent")]
    assert rules.compact_events(events, 10) == events


# ── replies ────────────────────────────────────────────────────────────────

CONSENT = rules.split_phrases(rules.DEFAULT_SETTINGS["consentPhrases"])
OBJECTION = rules.split_phrases(rules.DEFAULT_SETTINGS["objectionPhrases"])


@pytest.mark.parametrize(
    "text,expected",
    [
        ("I consent", "consent"),
        ("i CONSENT!", "consent"),
        ("Sure, I agree.", "consent"),
        ("I do not consent", "objection"),
        ("I don’t consent", "objection"),
        ("I object", "objection"),
        ("Please stop recording", "objection"),
        ("I agree with Ana but I object to recording", "objection"),
        ("consenting adults", None),
        ("I disagree", None),
        ("Good morning", None),
        ("", None),
        (None, None),
    ],
)
def test_replies_are_classified_with_objections_first(text, expected) -> None:
    assert rules.classify_reply(text, CONSENT, OBJECTION) == expected


def test_names_match_case_and_spacing_insensitively() -> None:
    assert rules.normalize_name("  Ana   SILVA ") == rules.normalize_name("ana silva")
    assert rules.normalize_name(None) == ""


# ── settings ───────────────────────────────────────────────────────────────


def test_settings_of_the_wrong_kind_fall_back_to_the_defaults() -> None:
    settings = rules.effective_settings(
        {"mode": "always", "consentRule": 3, "allowPerMeetingOverride": "no"}
    )
    assert settings["mode"] == "off"
    assert settings["consentRule"] == "each_participant"
    assert settings["allowPerMeetingOverride"] is True
    assert settings["consentPhrases"][0] == "I consent"


def test_a_saved_mode_is_used() -> None:
    settings = rules.effective_settings({"mode": "opt_in", "consentRule": "owner_attests"})
    assert (settings["mode"], settings["consentRule"]) == ("opt_in", "owner_attests")


@pytest.mark.parametrize(
    "mode,rule,allowed",
    [
        ("opt_in", "each_participant", True),
        ("opt_in", "owner_attests", True),
        ("notify", None, False),
        ("off", None, False),
    ],
)
def test_without_overrides_a_meeting_may_only_be_as_strict_or_stricter(mode, rule, allowed) -> None:
    settings = rules.effective_settings(
        {"mode": "opt_in", "consentRule": "owner_attests", "allowPerMeetingOverride": False}
    )
    assert rules.override_permitted(settings, mode, rule)[0] is allowed


def test_with_overrides_any_mode_is_permitted() -> None:
    settings = rules.effective_settings({"mode": "opt_in"})
    assert rules.override_permitted(settings, "off", None) == (True, "")


def test_the_notice_placeholders_are_filled() -> None:
    text = rules.render_notice(
        rules.DEFAULT_SETTINGS["optInNoticeText"],
        lma_user="Ana",
        meeting="Weekly sync",
        consent_phrase="I consent",
        objection_phrase="I object",
    )
    assert text.startswith("Ana would like to record and transcribe this meeting (Weekly sync)")
    assert '"I consent"' in text and '"I object"' in text
    assert "{" not in text
    assert rules.render_notice("{other} {LMA_USER}") == "{other} The meeting organizer"


def test_the_seeded_defaults_are_the_rules_defaults() -> None:
    shipped = (
        Path(__file__).resolve().parents[3]
        / "lambda_functions"
        / "configuration_seeder"
        / "defaults"
        / "MeetingConsent.json"
    )
    assert json.loads(shipped.read_text(encoding="utf-8")) == rules.DEFAULT_SETTINGS
