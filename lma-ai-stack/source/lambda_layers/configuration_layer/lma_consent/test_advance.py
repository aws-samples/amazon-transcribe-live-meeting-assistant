#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Tests for the rules of consent given in advance.

What matters: a consent never counts beyond a year, after it is withdrawn, or
for a meeting it was not given for; and the text a person agreed to is kept
exactly, with a hash that changes with any change of wording.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lma_consent import advance, rules

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def consent(**fields):
    base = {
        "ConsentId": "20261125120000-aaaaaaaaaaaa",
        "Scope": "meeting",
        "CallId": "Weekly sync",
        "Organizer": "owner@example.com",
        "GrantedAt": "2026-11-25T11:00:00Z",
        "ValidUntil": "2027-11-25T11:00:00Z",
    }
    base.update(fields)
    return base


MEETING = {
    "CallId": "Weekly sync",
    "VirtualParticipantId": "vp-1",
    "Owner": "owner@example.com",
    "Labels": [{"Key": "engagement", "Value": "acme"}],
}


def test_the_default_text_is_the_settings_default():
    assert advance.DEFAULT_TEXT == rules.DEFAULT_SETTINGS["advanceConsentText"]
    assert rules.effective_settings({})["advanceConsentMaxDays"] == 365
    assert rules.effective_settings({})["keepAdvanceConsentDeviceDetails"] is False


@pytest.mark.parametrize("setting,days", [(30, 30), (0, 1), (900, 365), (True, 365), ("x", 365)])
def test_the_longest_period_is_held_to_a_year(setting, days):
    assert advance.max_days(setting) == days


def test_a_standing_consent_runs_to_the_end_of_its_day():
    until = advance.valid_until("organizer", "2027-01-31", NOW, 365)
    assert advance.iso(until) == "2027-01-31T23:59:59Z"
    assert advance.valid_until("organizer", "2026-11-25", NOW, 365) > NOW


@pytest.mark.parametrize(
    "requested", [None, "", "2027-02-30", "31/01/2027", "2026-11-24", "2027-11-26"]
)
def test_a_standing_consent_needs_a_date_within_the_year(requested):
    with pytest.raises(advance.InvalidConsent):
        advance.valid_until("organizer", requested, NOW, 365)


def test_the_last_day_allowed_is_a_year_ahead():
    assert advance.iso(advance.valid_until("organizer", "2027-11-25", NOW, 365)).startswith(
        "2027-11-25"
    )
    with pytest.raises(advance.InvalidConsent, match="30 days"):
        advance.valid_until("organizer", "2026-12-26", NOW, 30)


def test_a_meeting_consent_lasts_at_most_the_period():
    assert advance.valid_until("meeting", None, NOW, 365) == NOW + timedelta(days=365)
    # A sooner date asked for is kept; a later one is refused.
    assert advance.iso(advance.valid_until("meeting", "2026-12-01", NOW, 365)).startswith(
        "2026-12-01"
    )


def test_the_text_fills_its_placeholders_and_keeps_others():
    text = advance.render_text(
        "{organizer} may record {scope} ({meeting}) {other}",
        organizer="owner@example.com",
        meeting_name="Weekly sync",
        scope=advance.scope_phrase("meeting", meeting_name="Weekly sync"),
    )
    assert text == 'owner@example.com may record the meeting "Weekly sync" (Weekly sync) {other}'


def test_the_scope_phrase_names_the_label_and_date():
    phrase = advance.scope_phrase(
        "organizer",
        until=datetime(2027, 1, 31, 23, 59, 59, tzinfo=timezone.utc),
        label=("engagement", "acme"),
    )
    assert phrase == (
        "every meeting they organize that is labelled engagement: acme, "
        "until the end of 2027-01-31 (UTC)"
    )


def test_the_hash_and_version_change_with_the_wording():
    assert advance.text_hash("a") != advance.text_hash("a ")
    assert (
        advance.text_version(None)
        == advance.text_version("")
        == advance.text_version(advance.DEFAULT_TEXT)
    )
    assert advance.text_version("other") != advance.text_version(None)


def test_status():
    assert advance.status_of(consent(), NOW) == "active"
    assert advance.status_of(consent(WithdrawnAt="2026-11-25T11:30:00Z"), NOW) == "withdrawn"
    assert advance.status_of(consent(ValidUntil="2026-11-25T11:59:59Z"), NOW) == "expired"
    assert advance.status_of(consent(ValidUntil=None), NOW) == "expired"


def test_a_meeting_consent_covers_its_meeting_only():
    assert advance.covers(consent(), MEETING, NOW)
    assert not advance.covers(consent(), {**MEETING, "CallId": "Other"}, NOW)
    by_vp = consent(CallId=None, VirtualParticipantId="vp-1")
    assert advance.covers(by_vp, MEETING, NOW)
    assert not advance.covers(by_vp, {**MEETING, "VirtualParticipantId": "vp-2"}, NOW)


def test_a_standing_consent_covers_the_organizers_meetings_until_its_date():
    standing = consent(Scope="organizer", CallId=None)
    assert advance.covers(standing, {**MEETING, "CallId": "Any"}, NOW)
    assert not advance.covers(standing, {**MEETING, "Owner": "someone@example.com"}, NOW)
    assert not advance.covers(standing, MEETING, datetime(2027, 11, 26, tzinfo=timezone.utc))


def test_a_labelled_standing_consent_needs_the_label():
    labelled = consent(Scope="organizer", LabelKey="engagement", LabelValue="acme")
    assert advance.covers(labelled, MEETING, NOW)
    assert not advance.covers(labelled, {**MEETING, "Labels": []}, NOW)
    assert not advance.covers(
        labelled, {**MEETING, "Labels": [{"Key": "engagement", "Value": "other"}]}, NOW
    )


def test_withdrawn_or_future_consent_covers_nothing():
    assert not advance.covers(consent(WithdrawnAt="2026-11-25T11:30:00Z"), MEETING, NOW)
    assert not advance.covers(consent(GrantedAt="2026-11-25T13:00:00Z"), MEETING, NOW)
    assert not advance.covers(consent(Scope="unknown"), MEETING, NOW)


def test_the_newest_covering_consent_is_the_one_used():
    older = consent(ConsentId="a", GrantedAt="2026-11-01T00:00:00Z")
    newer = consent(ConsentId="b", Scope="organizer", GrantedAt="2026-11-20T00:00:00Z")
    assert advance.covering([older, newer], MEETING, NOW)["ConsentId"] == "b"
    assert advance.covering([], MEETING, NOW) is None


def test_same_scope():
    assert advance.same_scope(consent(), consent(ConsentId="x"))
    assert not advance.same_scope(consent(), consent(CallId="Other"))
    standing = consent(Scope="organizer", CallId=None)
    assert advance.same_scope(standing, dict(standing))
    assert not advance.same_scope(standing, {**standing, "LabelKey": "engagement"})


# ── matching attendees to the participant list ──────────────────────────────

LISTED = [
    {"name": "Ana Silva", "email": "ana@example.com"},
    {"name": "Bo Chen", "email": "bo@example.com"},
    {"name": "", "email": "nameless@example.com"},
]


def test_attendees_are_matched_by_display_name_ignoring_case_and_spacing():
    matches = advance.match_listed(["ana  SILVA", "Bo Chen", "Cy Diaz"], LISTED)
    assert matches == {
        "ana  SILVA": {"name": "Ana Silva", "email": "ana@example.com"},
        "Bo Chen": {"name": "Bo Chen", "email": "bo@example.com"},
    }


def test_a_name_two_listed_people_share_matches_nobody():
    listed = LISTED + [{"name": "bo chen", "email": "other@example.com"}]
    assert "Bo Chen" not in advance.match_listed(["Bo Chen"], listed)


def test_a_name_two_attendees_show_matches_nobody():
    assert advance.match_listed(["Bo Chen", "BO CHEN"], LISTED) == {}


def test_an_empty_name_matches_nobody():
    assert advance.match_listed(["", "  "], LISTED) == {}
