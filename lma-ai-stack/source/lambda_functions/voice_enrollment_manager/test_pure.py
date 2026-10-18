# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for consent and voiceprint state.

These are not ordinary product tests. Each one pins a promise made to a person
who handed over biometric data, so the cases that matter most are the refusals:
that nobody can consent on somebody else's behalf, that a withdrawal actually
destroys the voiceprint, that a consent to superseded terms stops being usable,
and that the append-only log never carries a centroid.
"""

from __future__ import annotations

import pure
import pytest

TERMS = "You agree that your voice may be used to label you in meetings."
NOW = "2026-10-18T12:00:00Z"
ENROLLEE = "sub-enrollee"
ADMIN = "sub-admin"


def granted(**overrides):
    args = dict(
        subject_id=ENROLLEE,
        action=pure.ACTION_GRANTED,
        terms_version=1,
        terms_text=TERMS,
        actor_sub=ENROLLEE,
        actor_email="enrollee@example.com",
        recorded_at=NOW,
    )
    args.update(overrides)
    return pure.consent_record(**args)


def active(**overrides):
    record = pure.enrollment_record(
        subject_id=ENROLLEE,
        display_name="Sarah Lee",
        centroid=[0.6, 0.8],
        terms_version=1,
        terms_sha256="sha-of-the-terms",
        sample_count=4,
        total_seconds=26.0,
        enrolled_at=NOW,
        retention_days=365,
    )
    record.update(overrides)
    return record


# --------------------------------------------------------------------------
# Only the person themselves can consent
# --------------------------------------------------------------------------


def test_the_enrollee_may_grant_consent() -> None:
    assert pure.may_grant(pure.ACTION_GRANTED, ENROLLEE, ENROLLEE) is True


def test_an_admin_may_not_grant_consent_for_somebody_else() -> None:
    """The whole design rests on this. A consent somebody else gave on your
    behalf is not consent, however senior they are."""
    assert pure.may_grant(pure.ACTION_GRANTED, ENROLLEE, ADMIN, ["Admin"]) is False
    assert pure.may_grant(pure.ACTION_RE_CONSENTED, ENROLLEE, ADMIN, ["Admin"]) is False


def test_another_user_may_not_grant_consent_for_somebody_else() -> None:
    assert pure.may_grant(pure.ACTION_GRANTED, ENROLLEE, "sub-stranger", []) is False


def test_an_unknown_action_is_refused_to_everybody_including_an_admin() -> None:
    """The vocabulary is closed, so a typo or a removed action denies rather
    than defaults to allow."""
    assert pure.may_grant("INVITED", ENROLLEE, ADMIN, ["Admin"]) is False
    assert pure.may_grant("ANYTHING", ENROLLEE, ENROLLEE, ["Admin"]) is False


def test_either_the_person_or_an_admin_may_revoke() -> None:
    """Withdrawal must be easy. An admin can also revoke for somebody who has
    left, and the log records which of them did it."""
    assert pure.may_grant(pure.ACTION_REVOKED, ENROLLEE, ENROLLEE) is True
    assert pure.may_grant(pure.ACTION_REVOKED, ENROLLEE, ADMIN, ["Admin"]) is True
    assert pure.may_grant(pure.ACTION_REVOKED, ENROLLEE, "sub-stranger", []) is False


def test_expiry_cannot_be_performed_by_any_user() -> None:
    """Only the retention pass, which runs with no user identity."""
    for actor, groups in ((ENROLLEE, []), (ADMIN, ["Admin"])):
        assert pure.may_grant(pure.ACTION_EXPIRED, ENROLLEE, actor, groups) is False


def test_an_unknown_action_is_refused_rather_than_allowed() -> None:
    assert pure.may_grant("SOMETHING_ELSE", ENROLLEE, ENROLLEE) is False


# --------------------------------------------------------------------------
# The consent log
# --------------------------------------------------------------------------


def test_a_consent_record_carries_no_biometric_data() -> None:
    """The log is append-only and must survive a deletion request, so a
    voiceprint must never reach it."""
    record = granted()
    assert "Centroid" not in record
    assert not any("centroid" in key.lower() for key in record)
    assert not any("vector" in key.lower() for key in record)


def test_a_consent_record_pins_the_exact_terms_by_hash() -> None:
    """So an older consent still proves what was agreed to after the terms are
    edited."""
    record = granted()
    assert record["TermsSha256"] == pure.terms_fingerprint(TERMS)
    assert record["TermsSha256"] != pure.terms_fingerprint(TERMS + " And one more thing.")


def test_the_fingerprint_is_stable_and_differs_for_any_edit() -> None:
    assert pure.terms_fingerprint(TERMS) == pure.terms_fingerprint(TERMS)
    assert pure.terms_fingerprint("a") != pure.terms_fingerprint("a ")


def test_a_consent_record_names_the_identity_that_acted() -> None:
    record = granted(actor_sub=ADMIN, actor_email="admin@example.com", action=pure.ACTION_REVOKED)
    assert record["ActorSub"] == ADMIN
    assert record["ActorEmail"] == "admin@example.com"


def test_a_consent_record_without_a_subject_or_an_actor_is_refused() -> None:
    """An unattributable consent record is not an audit trail."""
    with pytest.raises(ValueError):
        granted(subject_id="")
    with pytest.raises(ValueError):
        granted(actor_sub="")


def test_an_unknown_action_cannot_be_written_to_the_log() -> None:
    with pytest.raises(ValueError):
        granted(action="BACKDATED")


def test_optional_context_is_recorded_when_present_and_omitted_when_not() -> None:
    bare = granted()
    assert "SourceIp" not in bare and "UserAgent" not in bare
    full = granted(source_ip="203.0.113.4", user_agent="Mozilla/5.0", sample_count=4)
    assert full["SourceIp"] == "203.0.113.4"
    assert full["SampleCount"] == 4


def test_a_long_user_agent_is_truncated_rather_than_rejected() -> None:
    record = granted(user_agent="x" * 2000)
    assert len(record["UserAgent"]) == 400


# --------------------------------------------------------------------------
# Withdrawal
# --------------------------------------------------------------------------


def test_revoking_destroys_the_voiceprint() -> None:
    """Not blanked -- removed. A revoked row must hold no biometric at all."""
    revoked = pure.revoke(active())
    assert "Centroid" not in revoked
    assert "Dim" not in revoked
    assert revoked["Status"] == pure.STATUS_REVOKED


def test_revoking_keeps_the_fact_that_the_person_enrolled() -> None:
    """ "This person withdrew" is what the audit needs, and is not biometric."""
    revoked = pure.revoke(active())
    assert revoked["SubjectId"] == ENROLLEE
    assert revoked["EnrolledAt"] == NOW


def test_a_revoked_enrollment_can_never_match_again() -> None:
    assert pure.is_usable_for_matching(pure.revoke(active()), 1, NOW) is False


def test_revoking_twice_is_harmless() -> None:
    assert pure.revoke(pure.revoke(active()))["Status"] == pure.STATUS_REVOKED


# --------------------------------------------------------------------------
# When a voiceprint may be used
# --------------------------------------------------------------------------


def test_an_active_current_unexpired_enrollment_may_match() -> None:
    assert pure.is_usable_for_matching(active(), 1, NOW) is True


def test_consent_to_superseded_terms_stops_being_usable() -> None:
    """The terms changed, so what they agreed to is no longer what we do."""
    assert pure.is_usable_for_matching(active(), 2, NOW) is False
    assert pure.needs_reconsent(active(), 2) is True
    assert pure.needs_reconsent(active(), 1, None, NOW) is False


def test_consent_that_has_aged_out_stops_being_usable() -> None:
    """Consent that never expires is not meaningfully informed."""
    assert pure.is_usable_for_matching(active(), 1, "2027-10-19T00:00:00Z") is False


def test_an_enrollment_with_no_voiceprint_is_not_usable() -> None:
    assert pure.is_usable_for_matching(active(Centroid=[]), 1, NOW) is False


def test_a_pending_enrollment_is_not_usable() -> None:
    """Consent given, but no voice recorded yet."""
    assert pure.is_usable_for_matching(active(Status=pure.STATUS_PENDING), 1, NOW) is False


def test_a_missing_enrollment_is_not_usable() -> None:
    assert pure.is_usable_for_matching(None, 1, NOW) is False
    assert pure.is_usable_for_matching({}, 1, NOW) is False


def test_only_usable_enrollments_are_offered_to_a_session() -> None:
    usable = active()
    revoked = pure.revoke(active(SubjectId="sub-b", DisplayName="Bob"))
    stale = active(SubjectId="sub-c", DisplayName="Priya", TermsVersion="0")
    seeds = pure.seedable_voices([usable, revoked, stale], 1, NOW)
    assert [s["label"] for s in seeds] == ["Sarah Lee"]
    assert seeds[0]["subjectId"] == ENROLLEE
    assert seeds[0]["vector"] == [0.6, 0.8]


def test_a_seed_carries_no_more_than_the_engine_needs() -> None:
    seed = pure.seedable_voices([active()], 1, NOW)[0]
    assert set(seed) == {"label", "subjectId", "vector"}


def test_two_people_with_the_same_display_name_are_both_offered() -> None:
    """De-duplicating by name would silently drop somebody's enrollment."""
    other = active(SubjectId="sub-b")
    assert len(pure.seedable_voices([active(), other], 1, NOW)) == 2


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------


def test_an_enrollment_expires_a_retention_period_after_consent() -> None:
    record = pure.enrollment_record(
        subject_id=ENROLLEE,
        display_name="Sarah Lee",
        centroid=[1.0],
        terms_version=1,
        terms_sha256="sha-of-the-terms",
        sample_count=3,
        total_seconds=22.0,
        enrolled_at="2026-01-01T00:00:00Z",
        retention_days=30,
    )
    assert record["ConsentExpiresAt"].startswith("2026-01-31")


def test_the_retention_pass_finds_only_aged_out_active_enrollments() -> None:
    aged = active(SubjectId="sub-old", ConsentExpiresAt="2026-01-01T00:00:00Z")
    fresh = active(SubjectId="sub-new")
    already_revoked = pure.revoke(
        active(SubjectId="sub-gone", ConsentExpiresAt="2026-01-01T00:00:00Z")
    )
    assert pure.expired_subjects([aged, fresh, already_revoked], NOW) == ["sub-old"]


def test_a_naive_timestamp_is_read_as_utc() -> None:
    """Mixing a bare timestamp with a Z-suffixed one must not shift a date."""
    assert (
        pure.is_usable_for_matching(active(ConsentExpiresAt="2027-10-18T12:00:00"), 1, NOW) is True
    )


# --------------------------------------------------------------------------
# Building the voiceprint
# --------------------------------------------------------------------------


def test_a_voiceprint_is_the_mean_direction_of_its_samples() -> None:
    centroid = pure.centroid_from_embeddings([[1.0, 0.0], [0.0, 1.0]])
    assert centroid == pytest.approx([0.5, 0.5])


def test_a_loud_sample_does_not_dominate_the_voiceprint() -> None:
    """Assignment compares by cosine similarity, so only direction carries
    meaning; magnitude is just recording level."""
    quiet_and_loud = pure.centroid_from_embeddings([[1.0, 0.0], [0.0, 50.0]])
    equal = pure.centroid_from_embeddings([[1.0, 0.0], [0.0, 1.0]])
    assert quiet_and_loud == pytest.approx(equal)


def test_a_silent_sample_is_skipped_rather_than_pulling_the_voiceprint_to_zero() -> None:
    centroid = pure.centroid_from_embeddings([[1.0, 0.0], [0.0, 0.0]])
    assert centroid == pytest.approx([1.0, 0.0])


def test_embeddings_of_different_sizes_are_refused() -> None:
    with pytest.raises(ValueError):
        pure.centroid_from_embeddings([[1.0, 0.0], [1.0]])


def test_no_usable_samples_is_an_error_not_an_empty_voiceprint() -> None:
    """An empty centroid stored as a voiceprint would match everybody."""
    for bad in ([], None, [[]], [[0.0, 0.0]]):
        with pytest.raises(ValueError):
            pure.centroid_from_embeddings(bad)


def test_enough_speech_is_required_before_a_voiceprint_is_stored() -> None:
    """A weak voiceprint is worse than none: it produces confident wrong matches
    and has taken somebody's biometric data for no benefit."""
    assert pure.enrollment_is_sufficient(26.0, 4) is True
    assert pure.enrollment_is_sufficient(8.0, 4) is False
    assert pure.enrollment_is_sufficient(26.0, 1) is False


# --------------------------------------------------------------------------
# The display name
# --------------------------------------------------------------------------


def test_a_display_name_is_tidied_but_kept() -> None:
    assert pure.clean_display_name("  Sarah   Lee ") == "Sarah Lee"
    assert pure.clean_display_name("Jean-Luc Picard") == "Jean-Luc Picard"
    assert pure.clean_display_name("O'Brien") == "O'Brien"


def test_a_display_name_in_a_non_latin_script_is_kept() -> None:
    """This labels people; refusing their name because of its script is not on."""
    for name in ("李雷", "Зоя", "أحمد", "Ólafur"):
        assert pure.clean_display_name(name) == name


def test_a_display_name_that_could_be_read_as_markup_is_refused() -> None:
    """It is rendered in a transcript and exported to documents."""
    for bad in ("<script>x</script>", "Bob<br>", "a{b}", "back\\slash", "null\x00byte"):
        assert pure.clean_display_name(bad) is None


def test_a_name_that_is_not_a_name_is_refused() -> None:
    for bad in ("", "   ", "123", "...", None, 42, "x" * 61):
        assert pure.clean_display_name(bad) is None


def test_an_enrollment_without_a_usable_name_or_voiceprint_is_refused() -> None:
    for kwargs in ({"display_name": "<bad>"}, {"centroid": []}):
        args = dict(
            subject_id=ENROLLEE,
            display_name="Sarah Lee",
            centroid=[1.0],
            terms_version=1,
        terms_sha256="sha-of-the-terms",
            sample_count=3,
            total_seconds=22.0,
            enrolled_at=NOW,
            retention_days=365,
        )
        args.update(kwargs)
        with pytest.raises(ValueError):
            pure.enrollment_record(**args)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------
# "Consent expires" has to hold for every active row, including odd ones
# --------------------------------------------------------------------------


def test_an_active_enrollment_with_no_recorded_expiry_is_not_usable() -> None:
    """Nothing shows the consent is in date, so it is not used.

    enrollment_record always writes one, so such a row was hand-written or
    migrated -- but without this it would stand for ever, which is exactly what
    the retention period exists to prevent.
    """
    record = active()
    del record["ConsentExpiresAt"]
    assert pure.is_usable_for_matching(record, 1, NOW) is False


def test_an_active_enrollment_with_no_recorded_expiry_is_collected() -> None:
    """And the retention pass takes it, so it does not linger unusable."""
    record = active()
    del record["ConsentExpiresAt"]
    assert pure.expired_subjects([record], NOW) == [ENROLLEE]


def test_a_pending_row_without_an_expiry_is_not_collected() -> None:
    """A consent recorded but not yet acted on is not an expiry candidate."""
    pending = {"SubjectId": ENROLLEE, "Status": pure.STATUS_PENDING, "TermsVersion": "1"}
    assert pure.expired_subjects([pending], NOW) == []


def test_an_expiry_row_can_carry_the_hash_the_consent_recorded() -> None:
    """No terms are shown for an expiry, so hashing "" would say only that.

    Carrying the original hash forward makes the row say which terms lapsed.
    """
    row = pure.consent_record(
        subject_id=ENROLLEE,
        action=pure.ACTION_EXPIRED,
        terms_version=1,
        terms_text="",
        terms_sha256="the-hash-that-was-agreed",
        actor_sub="system:retention",
        actor_email="",
        recorded_at=NOW,
    )
    assert row["TermsSha256"] == "the-hash-that-was-agreed"
    assert row["TermsSha256"] != pure.terms_fingerprint("")


def test_a_consent_row_still_hashes_the_text_when_one_is_shown() -> None:
    """The normal path is unchanged: the hash is of what the person read."""
    row = pure.consent_record(
        subject_id=ENROLLEE,
        action=pure.ACTION_GRANTED,
        terms_version=1,
        terms_text="the exact words shown",
        actor_sub=ENROLLEE,
        actor_email="e@x.com",
        recorded_at=NOW,
    )
    assert row["TermsSha256"] == pure.terms_fingerprint("the exact words shown")


# --------------------------------------------------------------------------
# Whenever storing is refused, the terms must be on offer
#
# The two are read by different layers -- may_store_voiceprint by the backend,
# needs_reconsent by the portal -- so a state where one refuses while the other
# reports nothing to do is a dead end with no way out of it. A pending row
# waits indefinitely, which makes it the row most likely to be sitting there
# when the terms move.
# --------------------------------------------------------------------------

CURRENT_WORDS = pure.terms_fingerprint("the words in force")


def _states():
    """Every state a record can be in, as the two layers see it."""
    rows = {
        "pending, terms moved": {"Status": "pending", "TermsSha256": "older"},
        "pending, terms current": {"Status": "pending", "TermsSha256": CURRENT_WORDS},
        # Reachable: this branch's own seeds diagnostic exists for enrollments
        # that predate the hash field. A one-sided leniency about an absent
        # hash -- strict when storing, lenient when deciding what to show --
        # recreates the dead end with every suite green.
        "pending, no recorded terms": {"Status": "pending"},
        "active, no recorded terms": {
            "Status": "active",
            "ConsentExpiresAt": "2099-01-01T00:00:00Z",
            "Centroid": [0.1],
        },
        "pending, version moved": {
            "Status": "pending",
            "TermsSha256": CURRENT_WORDS,
            "TermsVersion": "0",
        },
        "active, terms moved": {
            "Status": "active",
            "TermsSha256": "older",
            "ConsentExpiresAt": "2099-01-01T00:00:00Z",
            "Centroid": [0.1],
        },
        "active, current": {
            "Status": "active",
            "TermsSha256": CURRENT_WORDS,
            "ConsentExpiresAt": "2099-01-01T00:00:00Z",
            "Centroid": [0.1],
        },
        "active, lapsed": {
            "Status": "active",
            "TermsSha256": CURRENT_WORDS,
            "ConsentExpiresAt": "2000-01-01T00:00:00Z",
            "Centroid": [0.1],
        },
        "active, no expiry": {
            "Status": "active",
            "TermsSha256": CURRENT_WORDS,
            "Centroid": [0.1],
        },
        "revoked": {"Status": "revoked", "TermsSha256": CURRENT_WORDS},
    }
    for row in rows.values():
        row.setdefault("SubjectId", ENROLLEE)
        row.setdefault("TermsVersion", "1")
        row.setdefault("DisplayName", "Sarah Lee")
        # Deliberately NOT defaulting TermsSha256: its absence is one of the
        # states under test.
    return rows


@pytest.mark.parametrize("name", sorted(_states()))
def test_a_refusal_to_store_always_comes_with_a_way_to_consent_again(name) -> None:
    row = _states()[name]
    may_store = pure.may_store_voiceprint(row, 1, NOW, CURRENT_WORDS)
    # What the portal computes: a standing consent hides the terms.
    portal_hides_terms = row["Status"] in ("pending", "active") and not pure.needs_reconsent(
        row, 1, CURRENT_WORDS, NOW
    )
    assert not (portal_hides_terms and not may_store), (
        f"{name!r} is a dead end: the backend refuses to store a voiceprint and "
        f"the portal offers no way to consent again."
    )


def test_a_pending_consent_to_superseded_words_is_asked_again() -> None:
    """The row that waits indefinitely, so is likeliest to be stranded."""
    assert pure.needs_reconsent(_states()["pending, terms moved"], 1, CURRENT_WORDS, NOW) is True


def test_a_pending_consent_to_the_current_words_is_not_disturbed() -> None:
    """Somebody mid-enrolment must not be sent back to the terms for no reason."""
    assert (
        pure.needs_reconsent(_states()["pending, terms current"], 1, CURRENT_WORDS, NOW) is False
    )


def test_a_pending_row_is_not_treated_as_expired_for_having_no_expiry() -> None:
    """Nothing is stored yet, so there is nothing to have expired."""
    pending = _states()["pending, terms current"]
    assert "ConsentExpiresAt" not in pending
    assert pure.needs_reconsent(pending, 1, CURRENT_WORDS, NOW) is False


def test_a_withdrawn_record_is_not_asked_to_re_consent() -> None:
    """It already gets the terms, by virtue of having no standing consent."""
    assert pure.needs_reconsent(_states()["revoked"], 1, CURRENT_WORDS, NOW) is False


def test_an_active_record_with_no_recorded_expiry_is_asked_again() -> None:
    """Pins the rule the previous round added, whose line this round rewrote.

    An active row that records no expiry cannot be shown to be in date, so it
    is refused for matching; its owner therefore has to be asked again, or the
    voiceprint sits unusable with nothing offering a way to renew it. The
    property test above is one-directional and goes vacuous for this row.
    """
    row = _states()["active, no expiry"]
    assert "ConsentExpiresAt" not in row
    assert pure.needs_reconsent(row, 1, CURRENT_WORDS, NOW) is True
    assert pure.is_usable_for_matching(row, 1, NOW, CURRENT_WORDS) is False


def test_a_pending_record_with_no_recorded_terms_is_asked_again() -> None:
    """Written before the hash field existed, so what was agreed is unknown."""
    row = _states()["pending, no recorded terms"]
    assert pure.needs_reconsent(row, 1, CURRENT_WORDS, NOW) is True


def test_a_consent_row_omits_the_terms_hash_when_there_is_none_to_record() -> None:
    """Rather than recording the hash of the empty string.

    e3b0c442... reads as "the terms shown were empty" rather than "not
    recorded", and this table is append-only, so a wrong value cannot be
    corrected afterwards.
    """
    row = pure.consent_record(
        subject_id=ENROLLEE,
        action=pure.ACTION_REVOKED,
        terms_version=1,
        terms_text="",
        terms_sha256=None,
        actor_sub=ADMIN,
        actor_email="admin@example.com",
        recorded_at=NOW,
    )
    assert "TermsSha256" not in row
    assert pure.terms_fingerprint("") not in row.values()


def test_a_consent_row_still_records_a_hash_whenever_one_exists() -> None:
    """The omission must be narrow: either input is enough to record one."""
    carried = pure.consent_record(
        subject_id=ENROLLEE,
        action=pure.ACTION_EXPIRED,
        terms_version=1,
        terms_text="",
        terms_sha256="what-was-agreed",
        actor_sub="system:retention",
        actor_email="",
        recorded_at=NOW,
    )
    assert carried["TermsSha256"] == "what-was-agreed"

    shown = pure.consent_record(
        subject_id=ENROLLEE,
        action=pure.ACTION_GRANTED,
        terms_version=1,
        terms_text="the words on screen",
        actor_sub=ENROLLEE,
        actor_email="e@x.com",
        recorded_at=NOW,
    )
    assert shown["TermsSha256"] == pure.terms_fingerprint("the words on screen")


def test_a_consent_row_omits_the_terms_version_when_there_is_none_to_record() -> None:
    """The same rule as the hash, on the same rows.

    An admin may revoke for a subject who has no enrollment at all. A row
    asserting which version they agreed to would be inventing one, on a table
    that cannot be corrected afterwards.
    """
    row = pure.consent_record(
        subject_id=ENROLLEE,
        action=pure.ACTION_REVOKED,
        terms_version="",
        terms_text="",
        actor_sub=ADMIN,
        actor_email="admin@example.com",
        recorded_at=NOW,
    )
    assert "TermsVersion" not in row
    assert "TermsSha256" not in row
    # The row still says who did what, when, and to whom -- which is the point.
    assert row["Action"] == pure.ACTION_REVOKED
    assert row["SubjectId"] == ENROLLEE
    assert row["ActorSub"] == ADMIN


def test_a_consent_row_records_the_version_whenever_one_is_known() -> None:
    row = pure.consent_record(
        subject_id=ENROLLEE,
        action=pure.ACTION_REVOKED,
        terms_version=2,
        terms_text="",
        terms_sha256="what-was-agreed",
        actor_sub=ENROLLEE,
        actor_email="e@x.com",
        recorded_at=NOW,
    )
    assert row["TermsVersion"] == "2"
    assert row["TermsSha256"] == "what-was-agreed"
