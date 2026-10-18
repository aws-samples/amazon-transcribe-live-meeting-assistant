# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Expire a voice-enrollment consent that nobody renewed.

Consent that never expires is not meaningfully informed, so one stands for
``VoiceEnrollmentRetentionDays`` and then lapses. When it does, this deletes the
voiceprint, marks the enrollment revoked, and appends an ``EXPIRED`` row to the
consent log -- the same three steps a withdrawal takes, with the deployment
rather than a person as the actor.

Run on a schedule, with no user identity at all. That is also why ``EXPIRED`` is
the one action ``pure.may_grant`` allows nobody to perform: it cannot be
triggered through the API, by an admin or anyone else.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import boto3
import pure

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ENROLLMENT_TABLE = os.environ["VOICE_ENROLLMENT_TABLE"]
CONSENT_LOG_TABLE = os.environ["VOICE_CONSENT_LOG_TABLE"]
TERMS_VERSION = os.environ.get("VOICE_ENROLLMENT_TERMS_VERSION", "1")

# Upper bound on pages, for the same reason FetchTranscript has one: a table
# that keeps handing back a continuation key must not spin this to its timeout.
MAX_PAGES = 100

dynamodb = boto3.client("dynamodb")

# The actor recorded when the deployment itself expires a consent. Not a user,
# and deliberately not an email, so a log row is never ambiguous about whether a
# person acted.
SYSTEM_ACTOR = "system:retention"


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_enrollments():
    """Every enrollment, paged.

    A Scan rather than a query: this asks "which consents have lapsed", which is
    a question about all of them. The table holds one row per enrolled person, so
    it is small by construction -- there is no meeting-scale data here.
    """
    items = []
    start_key = None
    pages = 0
    while pages < MAX_PAGES:
        kwargs = {"TableName": ENROLLMENT_TABLE}
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = dynamodb.scan(**kwargs)
        items.extend(response.get("Items", []))
        pages += 1
        next_key = response.get("LastEvaluatedKey")
        if not next_key or next_key == start_key:
            break
        start_key = next_key
    if pages >= MAX_PAGES:
        LOGGER.warning("Stopped reading enrollments at the %d page limit", MAX_PAGES)
    return items


def expire(enrollment, now_iso):
    """Destroy one voiceprint and record why.

    Ordered so the biometric goes first. If the log write fails afterwards the
    data is still gone, which is the right way round: the alternative leaves a
    voiceprint alive with a row saying it was deleted.
    """
    from index import _from_item, _to_item  # noqa: PLC0415 - shared serialisation

    record = _from_item(enrollment)
    subject_id = record["SubjectId"]

    dynamodb.put_item(TableName=ENROLLMENT_TABLE, Item=_to_item(pure.revoke(record)))
    dynamodb.put_item(
        TableName=CONSENT_LOG_TABLE,
        Item=_to_item(
            pure.consent_record(
                subject_id=subject_id,
                action=pure.ACTION_EXPIRED,
                terms_version=record.get("TermsVersion", TERMS_VERSION),
                # No terms are shown for an expiry -- the deployment performs it
                # with nobody present -- so the hash the consent itself recorded
                # is carried forward. It says which terms lapsed, where hashing
                # an empty string would have said only that none were shown.
                terms_text="",
                terms_sha256=record.get("TermsSha256") or None,
                actor_sub=SYSTEM_ACTOR,
                actor_email="",
                recorded_at=now_iso,
            )
        ),
        ConditionExpression="attribute_not_exists(SubjectId) AND attribute_not_exists(RecordedAt)",
    )
    LOGGER.info("Expired the voice enrollment for %s", subject_id)


def handler(event, context):  # pylint: disable=unused-argument
    """Scheduled entry point. Reports counts, never the people."""
    from index import _from_item  # noqa: PLC0415

    now_iso = _now()
    # Read once and keep the raw item beside its deserialised form: scanning
    # twice would both cost double and risk acting on a row that had changed
    # between the two passes.
    raw_items = read_enrollments()
    enrollments = [_from_item(item) for item in raw_items]
    lapsed = set(pure.expired_subjects(enrollments, now_iso))

    expired = 0
    failed = 0
    for item, record in zip(raw_items, enrollments, strict=True):
        if record.get("SubjectId") not in lapsed:
            continue
        try:
            expire(item, now_iso)
            expired += 1
        except Exception:  # noqa: BLE001 - one bad row must not stop the rest
            LOGGER.exception("Could not expire an enrollment")
            failed += 1

    LOGGER.info(
        "Retention pass: %d enrollments, %d lapsed, %d expired, %d failed",
        len(enrollments),
        len(lapsed),
        expired,
        failed,
    )
    return {"Enrollments": len(enrollments), "Expired": expired, "Failed": failed}
