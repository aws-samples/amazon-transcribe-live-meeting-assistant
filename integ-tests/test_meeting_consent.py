# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Meeting consent on the Stream Audio path: the transcriber's gate, end to end.

Sets the deployment's Meeting Consent section for the length of the module,
streams three meetings as Stream Audio does, and reads back what the stack
stored. See docs/meeting-consent.md ("Stream Audio and the Desktop Capture App")
for the behaviour asserted, and meeting_consent_probe.py for how each step is
done.

1. **Opt-in, no confirmation.** Mode ``opt_in``, rule ``owner_attests``. A
   ``START`` whose consent step is shown for opt-in but not confirmed is
   refused: a ``CONSENT_STATE`` frame with ``CONSENT_NOT_CONFIRMED`` and
   ``captureAllowed: false``, the connection closed with 1008, and no meeting
   row or transcript segment for the call.
2. **Opt-in, attested.** The same mode, with the step confirmed by the method
   ``verbal`` and a synthetic evidence screenshot sent straight after
   ``START``. The meeting is transcribed; its consent record is ``granted``,
   with the attestation and the notice as posted; the image is stored under
   ``lma-consent-evidence/<meeting>/`` in the recordings bucket; and the
   vision model's check of it is stored on the record (any verdict).
3. **Notify, objection.** Mode ``notify``. A confirmed step, then
   ``CONSENT_OBJECTION`` part-way through. The server stops capture and closes
   the connection; the transcript stops growing; the record is ``objected``
   with the objection; and the meeting is ended.

Opt-in. It runs only with ``LMA_TEST_CONSENT=1``, because it changes a
deployment-wide setting while it runs, takes about three minutes, and calls
Amazon Transcribe and Bedrock:

    AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \\
      make integ-tests STACK=lma-integtest1 LMA_TEST_CONSENT=1

Once opted in, a missing dependency, test user, or a transcriber that does not
answer ``CONSENT_SETTINGS`` is a failure, not a skip. The tests are marked
``optional_feature`` so that a run without the flag, the scheduled one
included, reports them as skipped rather than failed.

The setting is written through the configuration store with the caller's IAM
credentials, into the active profile, and is put back in the module's
teardown whatever the tests did. Meetings are left to expire with the
deployment's retention.
"""

from __future__ import annotations

import base64
import importlib
import os
import time

import cognito_test_user
import pytest

ENABLED = os.environ.get("LMA_TEST_CONSENT", "") not in ("", "0", "false")

pytestmark = [
    pytest.mark.optional_feature,
    pytest.mark.skipif(not ENABLED, reason="opt-in: set LMA_TEST_CONSENT=1"),
]

LMA_USER = "Integration Test"
MEETING_NAME = "Consent probe"
OBJECTOR = "Bo Chen"
NOTICE = (
    f"{LMA_USER} would like to record and transcribe this meeting ({MEETING_NAME}) with Live "
    "Meeting Assistant. Nothing is recorded or transcribed until everyone has agreed."
)
VERDICTS = {"supports_consent", "objection_found", "inconclusive"}
# Seconds into the notify meeting's audio at which the objection is sent, and
# the audio streamed in all. Long enough before it for Transcribe to have
# returned segments, and long enough after it that a gate that did not close
# would show as more of them.
OBJECTION_AFTER_SECONDS = 18.0
NOTIFY_SECONDS = 36.0
# Audio already inside Transcribe when the objection lands can still come back
# as a segment ending shortly after it.
OBJECTION_TOLERANCE_SECONDS = 4.0


def _require_dependencies() -> None:
    missing = []
    for module, requirement in (("pycognito", "requirements.txt"), ("websockets", "requirements.txt"),
                                ("PIL", "requirements-video.txt")):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(f"{module} (pip install -r integ-tests/{requirement})")
    if missing:
        pytest.fail("LMA_TEST_CONSENT is set but these are missing: " + ", ".join(missing))


@pytest.fixture(scope="module")
def stack(client):
    _require_dependencies()
    if not cognito_test_user.available(client.region):
        pytest.fail(f"LMA_TEST_CONSENT is set but there is no Cognito test user — "
                    f"{cognito_test_user.describe_sources()}")
    from meeting_consent_probe import Stack

    found = Stack.find(client.stack_name, client.region)
    if not found.has_consent_api():
        pytest.fail(f"{client.stack_name} has no meeting consent resolver; deploy a version with meeting consent")
    return found


@pytest.fixture(scope="module")
def user(stack):
    from meeting_consent_probe import tokens_for_test_user

    username, tokens = tokens_for_test_user(stack)
    return {"username": username, "tokens": tokens}


@pytest.fixture(scope="module")
def consent_config(stack, user):
    """The Meeting Consent section, saved first and always put back.

    Checks first that the transcriber has the consent step, so that a stack
    without it fails here with its setting untouched.
    """
    from meeting_consent_probe import ConsentSettingsOverride, consent_settings

    if consent_settings(stack, user["tokens"]) is None:
        pytest.fail(
            "the deployed transcriber did not answer CONSENT_SETTINGS, so it predates the Stream "
            "Audio consent step; deploy a version with it (nothing was changed)"
        )
    override = ConsentSettingsOverride(stack)
    override.save()
    print(f"meeting consent: saved the {override.profile!r} profile's section "
          f"({'present' if override.had_section else 'absent'}): {override.saved}")
    try:
        yield override
    finally:
        override.restore()
        print(f"meeting consent: restored; the deployment resolves mode {override.resolved().get('mode')!r}")


def _set_mode(consent_config, stack, user, **values) -> dict:
    """Write the section and wait until the transcriber reports it."""
    from meeting_consent_probe import consent_settings

    consent_config.apply(**values)
    answer = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        answer = consent_settings(stack, user["tokens"])
        if answer is None:
            pytest.fail(
                "the deployed transcriber did not answer CONSENT_SETTINGS, so it predates the Stream "
                "Audio consent step; deploy a version with it"
            )
        if answer.get("available") and answer.get("mode") == values["mode"]:
            return answer
        time.sleep(2)
    pytest.fail(f"saved meeting consent {values} but the transcriber reports {answer}")


def _stream(stack, user, **kwargs):
    from meeting_consent_probe import stream_meeting

    session = stream_meeting(stack, user["tokens"], user["username"], **kwargs)
    print(session.summary())
    return session


# ── 1. opt-in without a confirmation ───────────────────────────────────────


@pytest.fixture(scope="module")
def refused(stack, user, consent_config):
    from meeting_consent_probe import read_meeting, segments

    _set_mode(consent_config, stack, user, mode="opt_in", consentRule="owner_attests")
    session = _stream(stack, user, label="refused", seconds=20.0, linger_seconds=5.0, consent={
        "mode": "opt_in", "confirmed": False, "noticeText": NOTICE,
        "lmaUser": LMA_USER, "meetingName": MEETING_NAME, "client": "LMA integration test",
    })
    # Long enough for a START that had got through to have produced a meeting
    # row and its first segments.
    time.sleep(20)
    return {"session": session, "meeting": read_meeting(stack, session.call_id),
            "segments": segments(stack, session.call_id)}


def test_opt_in_meeting_without_confirmation_is_refused(refused) -> None:
    session = refused["session"]
    assert "CONSENT_NOT_CONFIRMED" in session.codes(), session.summary()
    refusal = next(f for f in session.consent_states() if f.get("code") == "CONSENT_NOT_CONFIRMED")
    assert refusal.get("captureAllowed") is False, refusal
    assert session.closed_by_server and session.close_code == 1008, session.summary()
    assert session.seconds_sent < 20.0, f"the server kept the connection open: {session.summary()}"


def test_refused_opt_in_meeting_captures_nothing(refused) -> None:
    call_id = refused["session"].call_id
    assert refused["meeting"] is None, f"a refused meeting has a row: {refused['meeting']}"
    assert refused["segments"] == [], f"{len(refused['segments'])} transcript segments for {call_id}"


# ── 2. opt-in, attested, with evidence ─────────────────────────────────────


@pytest.fixture(scope="module")
def granted(stack, user, consent_config, refused):
    """Depends on ``refused`` only so that the opt-in setting is written once."""
    from meeting_consent_probe import evidence_jpeg, read_meeting, segments, wait_for

    image = base64.b64encode(evidence_jpeg(NOTICE)).decode("ascii")
    session = _stream(stack, user, label="granted", seconds=20.0, linger_seconds=30.0, consent={
        "mode": "opt_in", "confirmed": True, "method": "verbal", "noticeText": NOTICE,
        "lmaUser": LMA_USER, "meetingName": MEETING_NAME,
        "statement": "Everyone agreed out loud at the start of the call.", "client": "LMA integration test",
    }, evidence={"image": f"data:image/jpeg;base64,{image}", "note": "Meeting chat after the request"})
    call_id = session.call_id
    found_segments = wait_for(lambda: segments(stack, call_id), timeout=60)

    def checked():
        meeting = read_meeting(stack, call_id) or {}
        evidence = (meeting.get("Consent") or {}).get("Evidence") or []
        return meeting if evidence and all(e.get("Verification") for e in evidence) else None

    meeting = wait_for(checked, timeout=120, interval=5) or read_meeting(stack, call_id)
    return {"session": session, "meeting": meeting, "segments": found_segments or []}


def test_attested_opt_in_meeting_is_transcribed(granted) -> None:
    session = granted["session"]
    assert not session.closed_by_server, f"the server ended an attested meeting: {session.summary()}"
    assert any(f.get("status") == "granted" and f.get("captureAllowed") is True
               for f in session.consent_states()), session.summary()
    assert granted["meeting"], f"no meeting row for {session.call_id}"
    assert granted["segments"], f"no transcript segments for {session.call_id}: {session.summary()}"


def test_attested_opt_in_meeting_records_the_attestation(granted, user) -> None:
    meeting = granted["meeting"]
    consent = meeting.get("Consent") or {}
    assert consent.get("Mode") == "opt_in" and consent.get("Rule") == "owner_attests", consent
    assert consent.get("Status") == "granted" and consent.get("CaptureAllowed") is True, consent
    attestation = consent.get("Attestation") or {}
    assert attestation.get("Method") == "verbal", attestation
    assert attestation.get("AttestedBy") == meeting.get("Owner") == user["username"], (
        f"attested by {attestation.get('AttestedBy')!r}, meeting owned by {meeting.get('Owner')!r}"
    )
    notices = consent.get("Notices") or []
    assert notices and notices[0].get("Channel") == "voice", notices
    assert notices[0].get("Text") == NOTICE, notices[0]


def test_attested_opt_in_meeting_stores_the_evidence_image(granted, stack) -> None:
    from meeting_consent_probe import evidence_folder, object_exists

    call_id = granted["session"].call_id
    evidence = (granted["meeting"].get("Consent") or {}).get("Evidence") or []
    assert len(evidence) == 1, f"expected the one image sent, got {evidence}: {granted['session'].summary()}"
    key = evidence[0].get("S3Key") or ""
    assert key.startswith(evidence_folder(call_id)), f"{key!r} is outside {evidence_folder(call_id)}"
    stored = object_exists(stack, key)
    assert stored, f"s3://{stack.recordings_bucket}/{key} does not exist"
    assert stored["content_type"] == "image/jpeg" and stored["bytes"] > 1000, stored


def test_attested_opt_in_meeting_evidence_is_checked(granted) -> None:
    evidence = (granted["meeting"].get("Consent") or {}).get("Evidence") or []
    assert evidence, "no evidence entry to check"
    verification = evidence[0].get("Verification")
    assert verification, (
        f"no stored check of {evidence[0].get('EvidenceId')} after two minutes; the verifier did not run "
        f"or failed (is the evidence check model enabled in Bedrock?): {granted['session'].summary()}"
    )
    assert verification.get("Verdict") in VERDICTS, verification
    assert verification.get("ModelId"), verification
    assert verification.get("VerifiedAt"), verification


# ── 3. notify, then an objection ───────────────────────────────────────────


@pytest.fixture(scope="module")
def objected(stack, user, consent_config, granted):
    """Depends on ``granted`` so that the opt-in meetings run before the mode changes."""
    from meeting_consent_probe import read_meeting, segments, wait_for

    _set_mode(consent_config, stack, user, mode="notify")
    session = _stream(
        stack, user, label="objected", seconds=NOTIFY_SECONDS, linger_seconds=10.0,
        consent={"mode": "notify", "confirmed": True, "method": "chat", "noticeText": NOTICE,
                 "lmaUser": LMA_USER, "meetingName": MEETING_NAME, "client": "LMA integration test"},
        objection={"participantName": OBJECTOR, "source": "chat", "text": "I object"},
        objection_after_seconds=OBJECTION_AFTER_SECONDS,
    )
    call_id = session.call_id
    # Segments for audio already sent before the objection finish arriving
    # within a few seconds; count them, then count again later.
    time.sleep(20)
    first = segments(stack, call_id)
    time.sleep(20)
    later = segments(stack, call_id)
    meeting = wait_for(lambda: (m := read_meeting(stack, call_id)) and m.get("Status") == "ENDED" and m,
                       timeout=60) or read_meeting(stack, call_id)
    return {"session": session, "first": first, "later": later, "meeting": meeting}


def test_objection_closes_the_connection(objected) -> None:
    session = objected["session"]
    assert session.objection_sent_at is not None, session.summary()
    assert "OBJECTION_RECEIVED" in session.codes(), session.summary()
    stop = next(f for f in session.consent_states() if f.get("code") == "OBJECTION_RECEIVED")
    assert stop.get("captureAllowed") is False, stop
    assert session.closed_by_server and session.close_code == 1008, session.summary()
    assert not session.end_sent, "the client sent END; the server should have closed the connection first"
    assert session.closed_at - session.objection_sent_at < 10, (
        f"the connection closed {session.closed_at - session.objection_sent_at:.1f}s after the objection"
    )


def test_objection_stops_the_transcript(objected) -> None:
    first, later = objected["first"], objected["later"]
    call_id = objected["session"].call_id
    assert first, f"no transcript segments from before the objection for {call_id}"
    assert len(later) == len(first), (
        f"transcript segments went on arriving after the objection: {len(first)}, then {len(later)}"
    )
    ends = [float(s["EndTime"]) for s in later if s.get("EndTime") is not None]
    if ends:
        assert max(ends) <= OBJECTION_AFTER_SECONDS + OBJECTION_TOLERANCE_SECONDS, (
            f"a segment ends {max(ends):.1f}s into the meeting; the objection was at {OBJECTION_AFTER_SECONDS}s"
        )


def test_objection_is_recorded_and_ends_the_meeting(objected) -> None:
    meeting = objected["meeting"]
    assert meeting, f"no meeting row for {objected['session'].call_id}"
    consent = meeting.get("Consent") or {}
    assert consent.get("Mode") == "notify", consent
    assert consent.get("Status") == "objected" and consent.get("CaptureAllowed") is False, consent
    objections = [e for e in consent.get("Events") or [] if e.get("Decision") == "objection"]
    assert [e.get("ParticipantName") for e in objections] == [OBJECTOR], consent.get("Events")
    assert meeting.get("Status") == "ENDED", f"the meeting's status is {meeting.get('Status')!r}"
