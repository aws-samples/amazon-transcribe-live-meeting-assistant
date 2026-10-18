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
    row = {
        "SubjectId": {"S": ENROLLEE},
        "Status": {"S": status},
        "TermsVersion": {"S": terms},
        "DisplayName": {"S": "Sarah Lee"},
    }
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
    ddb = install(monkeypatch, enrollment=consented(status="active", terms="0"))
    index.handler(event("grantVoiceConsent"), None)
    assert [row["Action"]["S"] for row in ddb.consent_rows()] == ["RE_CONSENTED"]
