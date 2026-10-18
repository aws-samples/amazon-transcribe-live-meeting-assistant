# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The voice-enrollment portal's backend.

Handles the whole lifecycle of a voiceprint: the enrollee's own consent, the
samples that become the voiceprint, withdrawal, and the retention pass that
expires a consent nobody renewed.

Two things about the shape of this module are deliberate.

**Consent is checked before anything else happens, on every call.** Not once at
the start of a flow -- each operation re-derives who is asking and what they are
allowed to do, from `pure.may_grant`. The asymmetry that matters is that only the
enrollee can grant: an admin can revoke, never grant, because a consent somebody
else asserts on another person's behalf is worthless.

**Every write to the consent log is a PutItem of a new row.** Nothing here
updates or deletes a log row, and the function's IAM role is not granted the
permission to -- so the application cannot rewrite its own history even if this
code is wrong. A withdrawal appends a `REVOKED` row and strips the voiceprint
from the mutable record; the log keeps no biometric data at any point.

All of the judgement lives in pure.py; this is the AWS plumbing.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import pure
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

# Standard logging rather than Powertools, which the repository otherwise
# prefers: the layer carrying Powertools is built for arm64 only, and these
# functions have no other reason to be pinned to an architecture. Same choice
# and same reason as FetchTranscript and the speaker-name resolver.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ENROLLMENT_TABLE = os.environ["VOICE_ENROLLMENT_TABLE"]
CONSENT_LOG_TABLE = os.environ["VOICE_CONSENT_LOG_TABLE"]
TERMS_VERSION = os.environ.get("VOICE_ENROLLMENT_TERMS_VERSION", "1")
RETENTION_DAYS = int(os.environ.get("VOICE_ENROLLMENT_RETENTION_DAYS", "365"))

SERIALIZER = TypeSerializer()
DESERIALIZER = TypeDeserializer()

dynamodb = boto3.client("dynamodb")

# The exact words an enrollee agrees to. Stored by hash with every consent, so a
# consent given today still proves what it was given to after this text is
# edited -- and editing it requires bumping VoiceEnrollmentTermsVersion, which
# asks everybody to agree again.
CONSENT_TERMS = """\
You are being asked to let this Live Meeting Assistant deployment recognise your \
voice, so that meeting transcripts can label what you say with your name.

What is stored. A "voiceprint": a set of numbers derived from your voice \
recording. It is enough to recognise your voice and not enough to reconstruct \
your speech. The recordings you make here are used to compute it and are then \
discarded -- they are never saved.

What it is used for. Labelling you by name in transcripts of meetings recorded \
by this deployment. Nothing else.

What is never done. Your voiceprint is never created from meeting audio -- only \
from recordings you make here, after agreeing to this. It is never shared outside \
this deployment.

How long. Your consent lasts {retention_days} days, after which your voiceprint \
is deleted and you will be asked again if you want to continue.

Withdrawing. You can withdraw at any time, from this page, without asking \
anyone. Your voiceprint is deleted immediately and no later meeting can \
recognise your voice. Names already written into past transcripts stay there as \
ordinary text, and can be edited on the meeting page.

Declining. If you do not agree, nothing is stored and meetings will label you the \
way they do today.\
"""


def terms_text():
    return CONSENT_TERMS.format(retention_days=RETENTION_DAYS)


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_dynamo_number(value):
    """Floats to Decimal, recursively.

    DynamoDB's serializer refuses Python floats outright, and a voiceprint is a
    list of them -- so without this the first real enrollment would fail at the
    write. Converted via str() rather than Decimal(float), which would otherwise
    store the full binary expansion of each component and overflow DynamoDB's
    38-digit limit.
    """
    if isinstance(value, float):
        return Decimal(str(round(value, 9)))
    if isinstance(value, list):
        return [_as_dynamo_number(item) for item in value]
    if isinstance(value, dict):
        return {key: _as_dynamo_number(item) for key, item in value.items()}
    return value


def _to_item(record):
    return {key: SERIALIZER.serialize(_as_dynamo_number(value)) for key, value in record.items()}


def _from_item(item):
    return {key: DESERIALIZER.deserialize(value) for key, value in item.items()}


def caller(event):
    """Who is asking, from the AppSync identity.

    An invocation with no identity is the retention pass, which runs with no user
    at all; it is allowed to expire a consent and nothing else.
    """
    identity = event.get("identity") or {}
    return {
        "sub": identity.get("sub") or identity.get("username") or "",
        "email": (identity.get("claims") or {}).get("email") or identity.get("username") or "",
        "groups": identity.get("groups") or [],
        "source_ip": (identity.get("sourceIp") or [None])[0]
        if isinstance(identity.get("sourceIp"), list)
        else identity.get("sourceIp"),
        "is_user": bool(identity),
    }


def read_enrollment(subject_id):
    response = dynamodb.get_item(TableName=ENROLLMENT_TABLE, Key={"SubjectId": {"S": subject_id}})
    item = response.get("Item")
    return _from_item(item) if item else None


def append_consent(record):
    """Append one row to the consent log.

    PutItem only, and the role has no UpdateItem or DeleteItem on this table, so
    the history cannot be rewritten by this code. The condition makes a repeated
    write at the same instant a no-op rather than an overwrite.
    """
    try:
        dynamodb.put_item(
            TableName=CONSENT_LOG_TABLE,
            Item=_to_item(record),
            ConditionExpression="attribute_not_exists(SubjectId) AND attribute_not_exists(RecordedAt)",
        )
    except dynamodb.exceptions.ConditionalCheckFailedException:
        LOGGER.warning(
            "A consent row already exists for %s at %s; not overwriting it",
            record.get("SubjectId"),
            record.get("RecordedAt"),
        )


def put_enrollment(record):
    dynamodb.put_item(TableName=ENROLLMENT_TABLE, Item=_to_item(record))


def asr_embedding_available():
    """Whether there is an engine that can compute a voiceprint.

    Read from the environment rather than attempted and caught, so somebody is
    told the deployment cannot enrol voices instead of being told their
    recordings were unusable.
    """
    return bool(
        os.environ.get("ASR_LAUNCHER_FUNCTION_ARN") or os.environ.get("ASR_DIRECT_ENDPOINT")
    )


def embed_samples(samples):
    """Turn enrollment recordings into one voiceprint.

    The embedding model lives in the ASR MicroVM image, not here, and the ASR
    protocol already has an `embed` mode that returns one vector per utterance --
    so this streams the samples to a session rather than carrying a model.

    The audio is held for the length of this call and never written anywhere.
    What persists is the centroid, which is a one-way summary.
    """
    # Imported lazily so the rest of this module stays usable, and testable,
    # without the ASR client's dependencies.
    from asr_enrollment_client import embed_utterances  # noqa: PLC0415

    return embed_utterances(samples)


def grant_consent(event, who):
    """Record that the enrollee themselves agreed to the current terms."""
    subject_id = who["sub"]
    if not pure.may_grant(pure.ACTION_GRANTED, subject_id, who["sub"], who["groups"]):
        return {"Success": False, "Message": "only you can give your own consent"}

    existing = read_enrollment(subject_id)
    action = (
        pure.ACTION_RE_CONSENTED
        if existing and existing.get("Status") != pure.STATUS_PENDING
        else pure.ACTION_GRANTED
    )
    append_consent(
        pure.consent_record(
            subject_id=subject_id,
            action=action,
            terms_version=TERMS_VERSION,
            terms_text=terms_text(),
            actor_sub=who["sub"],
            actor_email=who["email"],
            recorded_at=_now(),
            source_ip=who["source_ip"],
            user_agent=(event.get("request") or {}).get("headers", {}).get("user-agent"),
        )
    )
    # Agreeing to new terms replaces the stored voiceprint rather than carrying
    # it over, because it was computed under the terms that have been
    # superseded. That destroys a biometric, so -- unlike the row write itself
    # -- it is an event the log has to carry: the whole purpose of the
    # append-only table is that no centroid disappears without a row saying so.
    if (existing or {}).get("Centroid"):
        append_consent(
            pure.consent_record(
                subject_id=subject_id,
                action=pure.ACTION_DELETED,
                terms_version=(existing or {}).get("TermsVersion", TERMS_VERSION),
                terms_text="",
                actor_sub=who["sub"],
                actor_email=who["email"],
                recorded_at=_now(),
            )
        )
    # Consent is recorded BEFORE any audio is captured, so a record exists even
    # if the person abandons the recording step. Declining is a normal outcome
    # and leaves nothing stored.
    put_enrollment(
        {
            "SubjectId": subject_id,
            "DisplayName": (existing or {}).get("DisplayName", ""),
            "Status": pure.STATUS_PENDING,
            "TermsVersion": str(TERMS_VERSION),
            # The words agreed to, recorded HERE rather than when the
            # voiceprint is stored. Somebody can agree, walk away, and come
            # back after the terms have changed -- which this flow explicitly
            # allows, since a pending row has no expiry. Hashing at the later
            # point would assert they agreed to whatever the terms say then.
            "TermsSha256": pure.terms_fingerprint(terms_text()),
            "ConsentedAt": _now(),
        }
    )
    return {"Success": True, "Message": "consent recorded", "Status": pure.STATUS_PENDING}


def store_voiceprint(event, who):
    """Compute and store the voiceprint, once consent is on record."""
    args = (event.get("arguments") or {}).get("input") or {}
    subject_id = who["sub"]
    if not pure.may_grant(pure.ACTION_GRANTED, subject_id, who["sub"], who["groups"]):
        return {"Success": False, "Message": "only you can enrol your own voice"}

    existing = read_enrollment(subject_id)
    if not pure.may_store_voiceprint(
        existing, TERMS_VERSION, _now(), pure.terms_fingerprint(terms_text())
    ):
        # No standing consent to the terms in force, so there is nothing that
        # permits storing a voiceprint. A withdrawn or lapsed consent reaches
        # here too: the row survives a withdrawal, deliberately, so that the
        # fact of it is not lost -- which means its status has to be read, not
        # just its terms version. Agreeing again is the way back, and that
        # appends a consent row.
        return {"Success": False, "Message": "please agree to the terms first"}

    display_name = pure.clean_display_name(args.get("DisplayName"))
    if not display_name:
        return {"Success": False, "Message": "please give the name to label you with"}

    samples = args.get("Samples") or []
    total_seconds = float(args.get("TotalSeconds") or 0)
    if not pure.enrollment_is_sufficient(total_seconds, len(samples)):
        return {
            "Success": False,
            "Message": (
                f"please record at least {pure.MIN_ENROLLMENT_SECONDS:.0f} seconds "
                f"across {pure.MIN_ENROLLMENT_UTTERANCES} recordings"
            ),
        }

    if not asr_embedding_available():
        # Enrolling needs the on-demand engine, which computes the embeddings.
        # Saying so is better than "could not use those recordings", which
        # sends somebody off to re-record audio that was never the problem.
        return {
            "Success": False,
            "Message": "voice enrolment needs the on-demand speech engine, which is not deployed",
        }

    try:
        centroid = pure.centroid_from_embeddings(embed_samples(samples))
    except (ValueError, RuntimeError):
        LOGGER.exception("Could not build a voiceprint for %s", subject_id)
        return {"Success": False, "Message": "could not use those recordings"}

    enrolled_at = _now()
    put_enrollment(
        pure.enrollment_record(
            subject_id=subject_id,
            display_name=display_name,
            centroid=centroid,
            terms_version=TERMS_VERSION,
            # The hash the CONSENT recorded, not one recomputed now. They are
            # the same whenever the terms have not moved, and when they have
            # moved the gate above has already refused -- so recomputing could
            # only ever overwrite the truth with something more convenient.
            terms_sha256=existing.get("TermsSha256") or pure.terms_fingerprint(terms_text()),
            sample_count=len(samples),
            total_seconds=total_seconds,
            enrolled_at=enrolled_at,
            retention_days=RETENTION_DAYS,
        )
    )
    LOGGER.info(
        "Stored a voiceprint for %s (%d samples, %.1fs)",
        subject_id,
        len(samples),
        total_seconds,
    )
    return {"Success": True, "Message": "your voice is enrolled", "Status": pure.STATUS_ACTIVE}


def revoke_consent(event, who):
    """Withdraw, immediately, and destroy the voiceprint."""
    args = (event.get("arguments") or {}).get("input") or {}
    subject_id = args.get("SubjectId") or who["sub"]
    if not pure.may_grant(pure.ACTION_REVOKED, subject_id, who["sub"], who["groups"]):
        return {"Success": False, "Message": "not permitted"}

    existing = read_enrollment(subject_id)
    if existing:
        # The voiceprint is removed from the record, not blanked, so a revoked
        # row holds no biometric data whatsoever.
        put_enrollment(pure.revoke(existing))
    append_consent(
        pure.consent_record(
            subject_id=subject_id,
            action=pure.ACTION_REVOKED,
            terms_version=(existing or {}).get("TermsVersion", TERMS_VERSION),
            terms_text=terms_text(),
            actor_sub=who["sub"],
            actor_email=who["email"],
            recorded_at=_now(),
            source_ip=who["source_ip"],
        )
    )
    LOGGER.info("Withdrew voice enrollment for %s (by %s)", subject_id, who["sub"])
    return {"Success": True, "Message": "your voiceprint has been deleted"}


def my_enrollment(event, who):
    """What the portal shows its visitor about their own enrollment."""
    existing = read_enrollment(who["sub"]) or {}
    return {
        "Success": True,
        "Status": existing.get("Status", "none"),
        "DisplayName": existing.get("DisplayName", ""),
        "ConsentExpiresAt": existing.get("ConsentExpiresAt", ""),
        "NeedsReconsent": pure.needs_reconsent(
            existing, TERMS_VERSION, pure.terms_fingerprint(terms_text()), _now()
        ),
        "TermsVersion": str(TERMS_VERSION),
        "Terms": terms_text(),
    }


HANDLERS = {
    "voiceEnrollmentTerms": my_enrollment,
    "grantVoiceConsent": grant_consent,
    "enrolMyVoice": store_voiceprint,
    "revokeVoiceConsent": revoke_consent,
}


def handler(event, context):  # pylint: disable=unused-argument
    """AppSync resolver for every voice-enrollment operation."""
    field = (event.get("info") or {}).get("fieldName", "")
    LOGGER.debug("Voice enrollment request: %s", field)

    operation = HANDLERS.get(field)
    if operation is None:
        raise ValueError(f"unknown voice enrollment operation: {field!r}")

    who = caller(event)
    if not who["is_user"]:
        # Everything here is a decision only a person can make about their own
        # biometric data. The retention pass has its own entry point.
        return {"Success": False, "Message": "not permitted"}

    try:
        return operation(event, who)
    except Exception:  # noqa: BLE001 - reported, never leaked
        LOGGER.exception("Voice enrollment operation %s failed", field)
        return {"Success": False, "Message": "could not complete that just now"}
