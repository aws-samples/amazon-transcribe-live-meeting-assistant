# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the voice-enrollment portal's backend.

The consent rules themselves are tested in test_pure.py. What is tested here is
that the plumbing cannot be talked out of them: that nobody can consent on
somebody else's behalf through the API, that a voiceprint cannot be stored
without a consent on record for the terms in force, that withdrawing really
removes the biometric and appends to the log, and that the log is only ever
appended to.

No AWS: the module builds its DynamoDB client at import time and it is replaced.
"""

from __future__ import annotations

import base64
import importlib.util
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("VOICE_ENROLLMENT_TABLE", "enrollments")
os.environ.setdefault("VOICE_CONSENT_LOG_TABLE", "consent-log")
os.environ.setdefault("VOICE_ENROLLMENT_TERMS_VERSION", "1")
os.environ.setdefault("VOICE_ENROLLMENT_RETENTION_DAYS", "365")
# Enrolling needs an engine that computes embeddings. These tests stub the
# embedding call itself, but the handler checks the deployment has one before
# asking, so the variable has to be present for the enrolment path to run.
os.environ.setdefault(
    "ASR_LAUNCHER_FUNCTION_ARN", "arn:aws:lambda:us-east-1:123456789012:function:asr-launcher"
)

HERE = Path(__file__).resolve().parent


def _load():
    sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location("voice_enrollment_index", HERE / "index.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()

ENROLLEE = "sub-enrollee"
ADMIN = "sub-admin"


class FakeDynamo:
    """Records every write, so "append only" is checkable."""

    class exceptions:  # noqa: N801 - mirrors botocore's shape
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self, enrollment=None):
        self.enrollment = enrollment
        self.puts = []
        self.updates = 0
        self.deletes = 0

    def get_item(self, **kwargs):
        # Key-aware on purpose: a fake that returns the same row whatever is
        # asked for would hide the fact that an operation looked up the wrong
        # subject.
        wanted = kwargs["Key"]["SubjectId"]["S"]
        if self.enrollment and self.enrollment["SubjectId"]["S"] == wanted:
            return {"Item": self.enrollment}
        return {}

    def put_item(self, **kwargs):
        self.puts.append(kwargs)

    def update_item(self, **kwargs):  # pragma: no cover - must never be called
        self.updates += 1
        raise AssertionError("the consent log must never be updated")

    def delete_item(self, **kwargs):  # pragma: no cover - must never be called
        self.deletes += 1
        raise AssertionError("the consent log must never be deleted from")

    def consent_rows(self):
        return [p["Item"] for p in self.puts if p["TableName"] == "consent-log"]

    def enrollment_rows(self):
        return [p["Item"] for p in self.puts if p["TableName"] == "enrollments"]


def install(monkeypatch, enrollment=None, embeddings=None, embed_error=None):
    ddb = FakeDynamo(enrollment=enrollment)
    monkeypatch.setattr(index, "dynamodb", ddb)

    def fake_embed(samples):
        if embed_error:
            raise embed_error
        return embeddings if embeddings is not None else [[1.0, 0.0], [0.9, 0.1], [1.0, 0.1]]

    monkeypatch.setattr(index, "embed_samples", fake_embed)
    return ddb


def event(field, *, sub=ENROLLEE, groups=(), **args):
    return {
        "info": {"fieldName": field},
        "identity": {"sub": sub, "groups": list(groups), "claims": {"email": f"{sub}@x.com"}},
        "arguments": {"input": args} if args else {},
    }


def consented(terms="1", status="pending", **extra):
    """An enrollment row shaped the way the handlers actually write one.

    The shape matters: a fixture that carries fields grant_consent never
    writes, or omits fields enrollment_record always writes, tests a row the
    product cannot produce. An `active` row therefore gets an expiry, because
    enrollment_record always sets one and an active row without one is refused.
    """
    row = {
        "SubjectId": {"S": ENROLLEE},
        "Status": {"S": status},
        "TermsVersion": {"S": terms},
        # The hash of the terms in force. An enrollment carrying a different
        # one agreed to different words, which is treated as a version change.
        "TermsSha256": {"S": index.pure.terms_fingerprint(index.terms_text())},
        "DisplayName": {"S": "Sarah Lee"},
    }
    if status == "active":
        row["ConsentExpiresAt"] = {"S": "2099-01-01T00:00:00Z"}
        row["Centroid"] = {"L": [{"N": "0.1"}, {"N": "0.2"}]}
    row.update(extra)
    return row


SAMPLES = [base64.b64encode(b"\x01\x00" * 16000).decode()] * 3


# --------------------------------------------------------------------------
# Only the person themselves
# --------------------------------------------------------------------------


def test_the_enrollee_can_record_their_own_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    ddb = install(monkeypatch)
    result = index.handler(event("grantVoiceConsent"), None)
    assert result["Success"] is True
    assert len(ddb.consent_rows()) == 1
    assert ddb.consent_rows()[0]["Action"]["S"] == "GRANTED"


def test_consent_is_recorded_before_any_audio_is_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So a record exists even if the person abandons the recording step."""
    ddb = install(monkeypatch)
    index.handler(event("grantVoiceConsent"), None)
    assert ddb.consent_rows()
    enrollment = ddb.enrollment_rows()[0]
    assert enrollment["Status"]["S"] == "pending"
    assert "Centroid" not in enrollment


def test_an_admin_cannot_enrol_somebody_elses_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole design rests on this: a consent the host asserts on somebody's
    behalf is worthless, and the person being enrolled is frequently not an LMA
    user at all."""
    ddb = install(monkeypatch, enrollment=consented())
    result = index.handler(
        {
            "info": {"fieldName": "enrolMyVoice"},
            "identity": {"sub": ADMIN, "groups": ["Admin"], "claims": {"email": "a@x.com"}},
            "arguments": {
                "input": {"DisplayName": "Sarah Lee", "Samples": SAMPLES, "TotalSeconds": 30}
            },
        },
        None,
    )
    # store_voiceprint only ever reads the CALLER's own sub, so an admin cannot
    # aim it at somebody else -- and their own sub has no consent on record.
    assert result["Success"] is False
    assert ddb.enrollment_rows() == []


def test_an_anonymous_call_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    ddb = install(monkeypatch)
    result = index.handler({"info": {"fieldName": "grantVoiceConsent"}}, None)
    assert result["Success"] is False
    assert ddb.puts == []


# --------------------------------------------------------------------------
# A voiceprint needs a consent on record, for the terms in force
# --------------------------------------------------------------------------


def test_a_voiceprint_is_stored_once_consent_is_on_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = install(monkeypatch, enrollment=consented())
    result = index.handler(
        event("enrolMyVoice", DisplayName="Sarah Lee", Samples=SAMPLES, TotalSeconds=30), None
    )
    assert result["Success"] is True
    stored = ddb.enrollment_rows()[0]
    assert stored["Status"]["S"] == "active"
    assert stored["DisplayName"]["S"] == "Sarah Lee"
    assert stored["Centroid"]["L"]


def test_no_voiceprint_without_a_consent_on_record(monkeypatch: pytest.MonkeyPatch) -> None:
    ddb = install(monkeypatch, enrollment=None)
    result = index.handler(
        event("enrolMyVoice", DisplayName="Sarah Lee", Samples=SAMPLES, TotalSeconds=30), None
    )
    assert result["Success"] is False
    assert "agree to the terms" in result["Message"]
    assert ddb.enrollment_rows() == []


def test_no_voiceprint_when_the_consent_was_to_superseded_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The terms changed, so what they agreed to is no longer what we do."""
    ddb = install(monkeypatch, enrollment=consented(terms="0"))
    result = index.handler(
        event("enrolMyVoice", DisplayName="Sarah Lee", Samples=SAMPLES, TotalSeconds=30), None
    )
    assert result["Success"] is False
    assert ddb.enrollment_rows() == []


def test_too_little_speech_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A weak voiceprint produces confident wrong matches and has taken somebody's
    biometric data for no benefit."""
    ddb = install(monkeypatch, enrollment=consented())
    result = index.handler(
        event("enrolMyVoice", DisplayName="Sarah Lee", Samples=SAMPLES[:1], TotalSeconds=4), None
    )
    assert result["Success"] is False
    assert ddb.enrollment_rows() == []


def test_a_name_that_could_be_read_as_markup_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = install(monkeypatch, enrollment=consented())
    result = index.handler(
        event("enrolMyVoice", DisplayName="<script>x</script>", Samples=SAMPLES, TotalSeconds=30),
        None,
    )
    assert result["Success"] is False
    assert ddb.enrollment_rows() == []


def test_recordings_that_cannot_be_embedded_store_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = install(monkeypatch, enrollment=consented(), embed_error=RuntimeError("no session"))
    result = index.handler(
        event("enrolMyVoice", DisplayName="Sarah Lee", Samples=SAMPLES, TotalSeconds=30), None
    )
    assert result["Success"] is False
    # The service's own error text is not handed to the browser.
    assert "session" not in result["Message"]
    assert ddb.enrollment_rows() == []


# --------------------------------------------------------------------------
# Withdrawal
# --------------------------------------------------------------------------


def test_withdrawing_removes_the_voiceprint_and_appends_to_the_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = consented(status="active", Centroid={"L": [{"N": "1.0"}]}, Dim={"N": "2"})
    ddb = install(monkeypatch, enrollment=active)
    result = index.handler(event("revokeVoiceConsent"), None)
    assert result["Success"] is True
    stored = ddb.enrollment_rows()[0]
    assert "Centroid" not in stored
    assert stored["Status"]["S"] == "revoked"
    assert ddb.consent_rows()[0]["Action"]["S"] == "REVOKED"


def test_an_admin_may_withdraw_on_somebody_elses_behalf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For a person who has left -- and the log records which of them did it."""
    active = consented(status="active", Centroid={"L": [{"N": "1.0"}]})
    ddb = install(monkeypatch, enrollment=active)
    result = index.handler(
        {
            "info": {"fieldName": "revokeVoiceConsent"},
            "identity": {"sub": ADMIN, "groups": ["Admin"], "claims": {"email": "a@x.com"}},
            "arguments": {"input": {"SubjectId": ENROLLEE}},
        },
        None,
    )
    assert result["Success"] is True
    assert ddb.consent_rows()[0]["ActorSub"]["S"] == ADMIN
    assert ddb.consent_rows()[0]["SubjectId"]["S"] == ENROLLEE


def test_a_stranger_cannot_withdraw_somebody_elses_enrollment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = install(monkeypatch, enrollment=consented(status="active"))
    result = index.handler(
        {
            "info": {"fieldName": "revokeVoiceConsent"},
            "identity": {"sub": "sub-stranger", "groups": [], "claims": {}},
            "arguments": {"input": {"SubjectId": ENROLLEE}},
        },
        None,
    )
    assert result["Success"] is False
    assert ddb.puts == []


def test_withdrawing_with_nothing_enrolled_still_records_the_withdrawal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = install(monkeypatch, enrollment=None)
    assert index.handler(event("revokeVoiceConsent"), None)["Success"] is True
    assert ddb.consent_rows()[0]["Action"]["S"] == "REVOKED"


# --------------------------------------------------------------------------
# The log is append-only
# --------------------------------------------------------------------------


def test_nothing_ever_updates_or_deletes_a_consent_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The role is not granted UpdateItem or DeleteItem on the log either, so
    this is belt and braces -- but the code must not even try."""
    ddb = install(monkeypatch, enrollment=consented(status="active"))
    for field in ("grantVoiceConsent", "revokeVoiceConsent", "voiceEnrollmentTerms"):
        index.handler(event(field), None)
    assert ddb.updates == 0
    assert ddb.deletes == 0
    for row in ddb.consent_rows():
        assert "Centroid" not in row, "the log must never carry a voiceprint"


def test_every_consent_row_is_written_with_an_existence_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So a repeated write at the same instant is a no-op, not an overwrite."""
    ddb = install(monkeypatch)
    index.handler(event("grantVoiceConsent"), None)
    log_writes = [p for p in ddb.puts if p["TableName"] == "consent-log"]
    assert all("attribute_not_exists" in p["ConditionExpression"] for p in log_writes)


def test_a_consent_row_pins_the_terms_by_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    ddb = install(monkeypatch)
    index.handler(event("grantVoiceConsent"), None)
    row = ddb.consent_rows()[0]
    assert row["TermsSha256"]["S"] == index.pure.terms_fingerprint(index.terms_text())


def test_re_agreeing_is_recorded_as_a_separate_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = install(monkeypatch, enrollment=consented(status="active"))
    index.handler(event("grantVoiceConsent"), None)
    assert ddb.consent_rows()[0]["Action"]["S"] == "RE_CONSENTED"


# --------------------------------------------------------------------------
# What the portal shows
# --------------------------------------------------------------------------


def test_the_portal_shows_the_terms_and_the_current_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, enrollment=consented(status="active"))
    result = index.handler(event("voiceEnrollmentTerms"), None)
    assert result["Status"] == "active"
    assert result["DisplayName"] == "Sarah Lee"
    assert "withdraw at any time" in result["Terms"]
    assert result["NeedsReconsent"] is False


def test_the_portal_asks_again_when_the_terms_have_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, enrollment=consented(status="active", terms="0"))
    assert index.handler(event("voiceEnrollmentTerms"), None)["NeedsReconsent"] is True


def test_a_visitor_with_no_enrollment_sees_no_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, enrollment=None)
    result = index.handler(event("voiceEnrollmentTerms"), None)
    assert result["Status"] == "none"
    assert result["Terms"]


def test_the_terms_say_what_is_stored_what_it_is_for_and_how_to_withdraw() -> None:
    """These are the claims the consent record attests to, so they have to be in
    the text that gets hashed."""
    terms = index.terms_text()
    for required in ("voiceprint", "discarded", "never", "withdraw", "365 days"):
        assert required in terms, required


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------


def test_an_unknown_operation_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    with pytest.raises(ValueError):
        index.handler(event("somethingElse"), None)


def test_an_unexpected_failure_is_reported_not_leaked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)

    def explode(*_args, **_kwargs):
        raise RuntimeError("dynamodb says no")

    monkeypatch.setattr(index, "read_enrollment", explode)
    result = index.handler(event("voiceEnrollmentTerms"), None)
    assert result["Success"] is False
    assert "dynamodb" not in result["Message"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------------------------
# A withdrawal or an expiry has to STAY withdrawn
#
# Both leave the row in place -- deliberately, so the fact of the consent is
# not lost when the biometric is destroyed. That means the row's STATUS is
# load-bearing: a gate that reads only the terms version accepts a row whose
# last recorded action was REVOKED, and a voiceprint would exist again while
# the append-only log still said consent had been withdrawn.
# --------------------------------------------------------------------------


def test_a_withdrawn_consent_does_not_permit_storing_a_voiceprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = install(monkeypatch, enrollment=consented(status="revoked"))
    result = index.handler(
        event("enrolMyVoice", DisplayName="Sarah Lee", Samples=SAMPLES, TotalSeconds=30.0),
        None,
    )
    assert result["Success"] is False
    assert ddb.enrollment_rows() == []


def test_a_lapsed_consent_does_not_permit_storing_a_voiceprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = install(
        monkeypatch,
        enrollment=consented(status="active", ConsentExpiresAt={"S": "2000-01-01T00:00:00Z"}),
    )
    result = index.handler(
        event("enrolMyVoice", DisplayName="Sarah Lee", Samples=SAMPLES, TotalSeconds=30.0),
        None,
    )
    assert result["Success"] is False
    assert ddb.enrollment_rows() == []


def test_agreeing_again_is_the_way_back_after_a_withdrawal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing the enrolment above must not leave somebody permanently stuck."""
    ddb = install(monkeypatch, enrollment=consented(status="revoked"))
    granted = index.handler(event("grantVoiceConsent"), None)
    assert granted["Success"] is True
    assert ddb.consent_rows()[0]["Action"]["S"] == "RE_CONSENTED"
    assert ddb.enrollment_rows()[0]["Status"]["S"] == "pending"


def test_destroying_a_voiceprint_to_re_consent_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agreeing to new terms replaces the stored voiceprint, so the log says so.

    The whole purpose of the append-only table is that no centroid disappears
    without a row attesting to it.
    """
    ddb = install(
        monkeypatch,
        enrollment=consented(
            status="active", terms="0", Centroid={"L": [{"N": "0.1"}, {"N": "0.2"}]}
        ),
    )
    index.handler(event("grantVoiceConsent"), None)
    actions = [row["Action"]["S"] for row in ddb.consent_rows()]
    # RE_CONSENTED first: agreeing is what causes the replacement, and the row
    # write that actually destroys the centroid comes after both.
    assert actions == ["RE_CONSENTED", "DELETED"]
    assert "Centroid" not in ddb.enrollment_rows()[0]


def test_re_consenting_with_no_voiceprint_records_no_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing was destroyed, so claiming it was would be a false audit row."""
    row = consented(status="active", terms="0")
    # An active row with no voiceprint: unusual, but a revoked-then-reactivated
    # record looks like this, and the DELETED row must be about an actual
    # deletion rather than about the re-consent having happened.
    del row["Centroid"]
    ddb = install(monkeypatch, enrollment=row)
    index.handler(event("grantVoiceConsent"), None)
    assert [row["Action"]["S"] for row in ddb.consent_rows()] == ["RE_CONSENTED"]


def test_enrolling_says_so_when_no_engine_can_compute_a_voiceprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Better than "could not use those recordings", which blames the audio.

    A deployment can have enrollment switched on without the on-demand engine,
    and somebody who records four clips perfectly well should be told what is
    actually missing.
    """
    monkeypatch.delenv("ASR_LAUNCHER_FUNCTION_ARN", raising=False)
    monkeypatch.delenv("ASR_DIRECT_ENDPOINT", raising=False)
    ddb = install(monkeypatch, enrollment=consented())
    result = index.handler(
        event("enrolMyVoice", DisplayName="Sarah Lee", Samples=SAMPLES, TotalSeconds=30.0),
        None,
    )
    assert result["Success"] is False
    assert "engine" in result["Message"]
    assert ddb.enrollment_rows() == []


# --------------------------------------------------------------------------
# The terms are pinned by hash, and the hash is COMPARED
#
# The retention period appears in the terms text, so changing
# VoiceEnrollmentRetentionDays changes what somebody agreed to without changing
# TermsVersion. Recording the hash and never comparing it would note the
# difference and then act on the superseded consent anyway.
# --------------------------------------------------------------------------


def test_the_portal_asks_again_when_the_words_changed_under_the_same_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch,
        enrollment=consented(status="active", TermsSha256={"S": "a-hash-of-older-words"}),
    )
    result = index.handler(event("voiceEnrollmentTerms"), None)
    assert result["NeedsReconsent"] is True


def test_the_portal_does_not_ask_again_when_the_words_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, enrollment=consented(status="active"))
    result = index.handler(event("voiceEnrollmentTerms"), None)
    assert result["NeedsReconsent"] is False


def test_a_stored_voiceprint_records_the_words_that_were_agreed_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the enrollment, not only in the log: the matching path reads this row.

    Note what this test can and cannot show. Whether the value was taken from
    the consent row or recomputed at store time is no longer observable from
    outside, because the gate above now refuses the case where those two
    differ -- which is a stronger guarantee than any assertion here could
    make. The two tests that follow cover that gate. This one only pins that
    the field is written at all, since the matching path is useless without it.
    """
    ddb = install(monkeypatch, enrollment=consented())
    index.handler(
        event("enrolMyVoice", DisplayName="Sarah Lee", Samples=SAMPLES, TotalSeconds=30.0),
        None,
    )
    stored = ddb.enrollment_rows()[0]
    assert stored["TermsSha256"]["S"] == index.pure.terms_fingerprint(index.terms_text())


def test_consent_records_the_terms_hash_when_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So there is something to compare against later.

    Somebody can agree, walk away, and return after the terms have moved -- a
    pending row has no expiry, so it waits indefinitely. Without a hash on that
    row there is no evidence of which words they agreed to.
    """
    ddb = install(monkeypatch)
    index.handler(event("grantVoiceConsent"), None)
    stored = ddb.enrollment_rows()[0]
    assert stored["TermsSha256"]["S"] == index.pure.terms_fingerprint(index.terms_text())


def test_a_consent_to_superseded_words_cannot_store_a_voiceprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agreed, walked away, terms changed, came back and recorded.

    The version is untouched -- only the retention period moved, which the
    terms text states -- so a version check alone lets this through.
    """
    ddb = install(
        monkeypatch,
        enrollment=consented(TermsSha256={"S": "a-hash-of-the-older-words"}),
    )
    result = index.handler(
        event("enrolMyVoice", DisplayName="Sarah Lee", Samples=SAMPLES, TotalSeconds=30.0),
        None,
    )
    assert result["Success"] is False
    assert ddb.enrollment_rows() == []


def test_a_consent_with_no_recorded_words_cannot_store_a_voiceprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row that does not say what was agreed to cannot show it is current."""
    row = consented()
    del row["TermsSha256"]
    ddb = install(monkeypatch, enrollment=row)
    result = index.handler(
        event("enrolMyVoice", DisplayName="Sarah Lee", Samples=SAMPLES, TotalSeconds=30.0),
        None,
    )
    assert result["Success"] is False
    assert ddb.enrollment_rows() == []


def test_the_portal_asks_again_when_a_consent_has_aged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Between lapsing and the nightly sweep, the row is still `active`.

    The voiceprint has already stopped being used, so telling the person they
    are enrolled -- with an expiry date in the past and no way to renew -- is
    both wrong and a dead end.
    """
    install(
        monkeypatch,
        enrollment=consented(status="active", ConsentExpiresAt={"S": "2000-01-01T00:00:00Z"}),
    )
    result = index.handler(event("voiceEnrollmentTerms"), None)
    assert result["NeedsReconsent"] is True


def test_a_withdrawal_records_the_terms_that_were_withdrawn_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not the terms in force now.

    A row saying somebody withdrew consent to terms they never saw is worse
    than one saying nothing about the text.
    """
    ddb = install(
        monkeypatch,
        enrollment=consented(status="active", TermsSha256={"S": "what-they-agreed-to"}),
    )
    index.handler(
        {
            "info": {"fieldName": "revokeVoiceConsent"},
            "identity": {"sub": ENROLLEE, "groups": [], "claims": {"email": "e@x.com"}},
            "arguments": {"input": {}},
        },
        None,
    )
    revoked = [r for r in ddb.consent_rows() if r["Action"]["S"] == "REVOKED"]
    assert revoked, [r["Action"]["S"] for r in ddb.consent_rows()]
    assert revoked[0]["TermsSha256"]["S"] == "what-they-agreed-to"


def test_destroying_a_voiceprint_records_the_terms_it_was_created_under(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ddb = install(
        monkeypatch,
        enrollment=consented(status="active", terms="0", TermsSha256={"S": "the-older-words"}),
    )
    index.handler(event("grantVoiceConsent"), None)
    deleted = [r for r in ddb.consent_rows() if r["Action"]["S"] == "DELETED"]
    assert deleted, [r["Action"]["S"] for r in ddb.consent_rows()]
    assert deleted[0]["TermsSha256"]["S"] == "the-older-words"
    assert deleted[0]["TermsSha256"]["S"] != index.pure.terms_fingerprint("")
