# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Consent and voiceprint state, without AWS.

A voiceprint is biometric data about a person, so the rules around it are not
ordinary product rules and are kept here, in one testable place, rather than
spread through handlers.

The shape of the problem: the person being enrolled is often not an LMA user at
all -- somebody from another company, a contractor in a meeting room -- so a
consent that the meeting host asserts on their behalf is worthless. Consent is
therefore first-party: the enrollee authenticates as themselves, is shown the
terms, and performs an explicit affirmative action. Everything below enforces
that, and records it in a form an auditor can rely on.

Two records, deliberately separate:

* the **enrollment**, which is mutable and is the only place a centroid (the
  biometric) lives;
* the **consent log**, which is append-only and holds no biometric at all.

That split is what lets a withdrawal destroy the voiceprint while preserving the
record that consent was given and then withdrawn -- which is what an auditor
needs, and what the person asking to be forgotten should not have to leave
behind.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

# Actions recorded in the append-only log. A withdrawal appends; it never edits.
ACTION_INVITED = "INVITED"
ACTION_GRANTED = "GRANTED"
ACTION_RE_CONSENTED = "RE_CONSENTED"
ACTION_REVOKED = "REVOKED"
ACTION_EXPIRED = "EXPIRED"
ACTION_DELETED = "DELETED"

CONSENT_ACTIONS = frozenset(
    {
        ACTION_INVITED,
        ACTION_GRANTED,
        ACTION_RE_CONSENTED,
        ACTION_REVOKED,
        ACTION_EXPIRED,
        ACTION_DELETED,
    }
)

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"

# Enough speech for a stable voiceprint. Measured guidance from the embedding
# model's own operating point: utterances under ~2.5s embed unreliably, so a
# handful of short clips is worse than a few longer ones.
MIN_ENROLLMENT_SECONDS = 20.0
MIN_ENROLLMENT_UTTERANCES = 3

# A display name is a label on a transcript, not free text to render as markup.
MAX_DISPLAY_NAME = 60
DISPLAY_NAME_DISALLOWED = re.compile(r"[<>{}\[\]\\\x00-\x1f\x7f]")


def terms_fingerprint(terms_text):
    """SHA-256 of the exact terms text an enrollee was shown.

    Pinning the text by hash is what makes a stored consent provable. If the
    terms are later edited, an older consent still says precisely what was
    agreed to, and the mismatch against the current text is detectable rather
    than silent.
    """
    return hashlib.sha256((terms_text or "").encode("utf-8")).hexdigest()


def clean_display_name(value):
    """The name to label transcripts with, or None if it is not usable."""
    if not isinstance(value, str):
        return None
    name = " ".join(value.split())
    if not name or len(name) > MAX_DISPLAY_NAME:
        return None
    if DISPLAY_NAME_DISALLOWED.search(name):
        return None
    if not re.search(r"[^\W\d_]", name, re.UNICODE):
        # Must contain at least one letter in some script -- digits and
        # punctuation alone are not a name.
        return None
    return name


def consent_record(
    *,
    subject_id,
    action,
    terms_version,
    terms_text,
    actor_sub,
    actor_email,
    recorded_at,
    terms_sha256=None,
    source_ip=None,
    user_agent=None,
    sample_count=None,
):
    """One row for the append-only consent log.

    Carries no biometric data by construction: there is no field for a centroid
    and none is accepted. A caller that wants to store a voiceprint uses
    :func:`enrollment_record` instead.

    ``terms_sha256`` is for rows where no terms were shown -- an expiry, which
    the deployment performs with nobody present. Those carry forward the hash
    the consent itself recorded, so the row says which terms lapsed. Hashing an
    empty string instead produced e3b0c442... on every such row, which is
    indistinguishable from "the terms shown were empty".
    """
    if action not in CONSENT_ACTIONS:
        raise ValueError(f"unknown consent action: {action!r}")
    if not subject_id:
        raise ValueError("a consent record must name its subject")
    if not actor_sub:
        raise ValueError("a consent record must name the identity that acted")

    record = {
        "SubjectId": subject_id,
        "RecordedAt": recorded_at,
        "Action": action,
        "TermsVersion": str(terms_version),
        "TermsSha256": terms_sha256 or terms_fingerprint(terms_text),
        "ActorSub": actor_sub,
        "ActorEmail": actor_email or "",
    }
    if source_ip:
        record["SourceIp"] = source_ip
    if user_agent:
        record["UserAgent"] = str(user_agent)[:400]
    if sample_count is not None:
        record["SampleCount"] = int(sample_count)
    return record


def may_grant(action, subject_id, actor_sub, actor_groups=()):
    """Who is allowed to perform which consent action.

    The asymmetry is the whole design: **only the enrollee may grant**, because a
    consent somebody else gave on their behalf is not consent. An admin may
    invite, and may revoke on somebody's behalf (for a person who has left), but
    may never grant.
    """
    is_self = bool(subject_id) and subject_id == actor_sub
    is_admin = "Admin" in (actor_groups or ())

    if action in (ACTION_GRANTED, ACTION_RE_CONSENTED):
        return is_self
    if action == ACTION_INVITED:
        return is_admin
    if action in (ACTION_REVOKED, ACTION_DELETED):
        return is_self or is_admin
    if action == ACTION_EXPIRED:
        # Only the retention pass, which runs with no user identity at all.
        return False
    return False


def centroid_from_embeddings(embeddings):
    """Average unit-normalised embeddings into one voiceprint.

    Normalising each embedding before averaging stops a single loud or long
    utterance dominating the result -- the assignment step compares by cosine
    similarity, so only direction carries meaning and magnitude is noise.
    """
    vectors = [v for v in (embeddings or []) if v]
    if not vectors:
        raise ValueError("no embeddings to build a voiceprint from")
    width = len(vectors[0])
    if any(len(v) != width for v in vectors):
        raise ValueError("embeddings differ in dimension")

    total = [0.0] * width
    used = 0
    for vector in vectors:
        norm = sum(component * component for component in vector) ** 0.5
        if norm == 0:
            # A silent or failed utterance carries no direction; including it
            # would pull the centroid toward the origin.
            continue
        for index, component in enumerate(vector):
            total[index] += component / norm
        used += 1
    if used == 0:
        raise ValueError("every embedding was empty")
    return [component / used for component in total]


def enrollment_is_sufficient(total_seconds, utterance_count):
    """Is there enough speech for a voiceprint worth storing?

    Storing a weak voiceprint is worse than storing none: it produces confident
    wrong matches, and it has taken somebody's biometric data for no benefit.
    """
    return total_seconds >= MIN_ENROLLMENT_SECONDS and utterance_count >= MIN_ENROLLMENT_UTTERANCES


def enrollment_record(
    *,
    subject_id,
    display_name,
    centroid,
    terms_version,
    terms_sha256,
    sample_count,
    total_seconds,
    enrolled_at,
    retention_days,
):
    """The mutable enrollment, including the voiceprint itself."""
    name = clean_display_name(display_name)
    if not name:
        raise ValueError("a usable display name is required")
    if not centroid:
        raise ValueError("a voiceprint is required")
    expires_at = _iso(_parse(enrolled_at) + timedelta(days=int(retention_days)))
    return {
        "SubjectId": subject_id,
        "DisplayName": name,
        "Centroid": [float(component) for component in centroid],
        "Dim": len(centroid),
        "Status": STATUS_ACTIVE,
        "TermsVersion": str(terms_version),
        # Carried on the enrollment as well as in the log, because this is the
        # record the matching path reads: a hash only an auditor can see cannot
        # stop a voiceprint being used under terms that have since changed.
        "TermsSha256": terms_sha256,
        "SampleCount": int(sample_count),
        "SampleSeconds": round(float(total_seconds), 2),
        "EnrolledAt": enrolled_at,
        "ConsentExpiresAt": expires_at,
    }


def revoke(enrollment):
    """Strip the voiceprint and mark the enrollment withdrawn.

    The centroid is removed rather than blanked, so a revoked row holds no
    biometric data whatsoever. The row itself stays, because "this person
    withdrew" is information the audit needs and is not itself biometric.
    """
    revoked = {
        key: value for key, value in (enrollment or {}).items() if key not in ("Centroid", "Dim")
    }
    revoked["Status"] = STATUS_REVOKED
    return revoked


def is_usable_for_matching(
    enrollment, current_terms_version, now_iso, current_terms_sha256=None
):
    """May this enrollment be used to label a meeting?

    Five things must all hold, and each of them is a promise made to the person
    who enrolled: they consented, they have not withdrawn, they consented to the
    terms that are current, the words of those terms have not changed under
    them, and their consent has not aged out.

    The fourth is why ``TermsSha256`` is stored rather than only the version
    number. The terms text states the retention period, so changing
    ``VoiceEnrollmentRetentionDays`` changes what somebody agreed to without
    changing ``TermsVersion``. Comparing the hash catches that, and catches any
    other edit to the text, without relying on an operator remembering to bump
    a version. Pinning the text and then never comparing it would record the
    difference and act on the old consent anyway.

    ``current_terms_sha256`` is optional so a caller that genuinely has no terms
    text to compare -- the retention pass, which only asks what has lapsed --
    does not have to invent one.
    """
    if not enrollment or enrollment.get("Status") != STATUS_ACTIVE:
        return False
    if not enrollment.get("Centroid"):
        return False
    if str(enrollment.get("TermsVersion")) != str(current_terms_version):
        # The terms changed, so what they agreed to is no longer what we do.
        return False
    if current_terms_sha256 and enrollment.get("TermsSha256") != current_terms_sha256:
        # Same version, different words. Treated exactly like a version change.
        return False
    expires_at = enrollment.get("ConsentExpiresAt")
    if not expires_at:
        # No recorded expiry, so there is nothing showing this consent is still
        # in date. enrollment_record always sets one, so an active row without
        # it was hand-written or migrated; "consent expires" has to hold for it
        # too, or it would stand for ever.
        return False
    if _parse(expires_at) <= _parse(now_iso):
        return False
    return True


def may_store_voiceprint(enrollment, current_terms_version, now_iso):
    """Is there a standing consent that permits storing a voiceprint?

    Separate from ``is_usable_for_matching`` because the two ask different
    questions. That one asks whether an existing voiceprint may be used; this
    asks whether one may be created, which is true in exactly one state: a
    consent to the terms in force that has not been withdrawn and has not
    lapsed.

    The status test is the part that is easy to leave out and expensive to get
    wrong. A withdrawal and an expiry both keep the row -- that is the whole
    point of the two-table split, so the fact of the consent is not lost when
    the biometric is destroyed -- so a check that looks only at the terms
    version accepts a row whose last recorded action was ``REVOKED``. That would
    let a voiceprint exist again while the append-only log still says consent
    was withdrawn, which is precisely the state the log exists to make
    impossible.
    """
    if not enrollment:
        return False
    if enrollment.get("Status") not in (STATUS_PENDING, STATUS_ACTIVE):
        return False
    if str(enrollment.get("TermsVersion")) != str(current_terms_version):
        return False
    expires_at = enrollment.get("ConsentExpiresAt")
    if expires_at and _parse(expires_at) <= _parse(now_iso):
        return False
    return True


def needs_reconsent(enrollment, current_terms_version, current_terms_sha256=None):
    """An active enrollment whose consent is to superseded terms.

    Superseded by either a version bump or an edit to the words, for the reason
    given in :func:`is_usable_for_matching`: the retention period appears in the
    text, so the two can diverge.
    """
    if not enrollment or enrollment.get("Status") != STATUS_ACTIVE:
        return False
    if str(enrollment.get("TermsVersion")) != str(current_terms_version):
        return True
    return bool(
        current_terms_sha256 and enrollment.get("TermsSha256") != current_terms_sha256
    )


def seedable_voices(enrollments, current_terms_version, now_iso, current_terms_sha256=None):
    """The labelled centroids a transcription session may be seeded with.

    Only usable enrollments, reduced to exactly what the engine needs. Two people
    who typed the same display name are both returned -- de-duplicating them
    would silently drop somebody's enrollment -- so the caller disambiguates.
    """
    out = []
    for enrollment in enrollments or []:
        if not is_usable_for_matching(
            enrollment, current_terms_version, now_iso, current_terms_sha256
        ):
            continue
        out.append(
            {
                "label": enrollment["DisplayName"],
                "subjectId": enrollment["SubjectId"],
                "vector": [float(c) for c in enrollment["Centroid"]],
            }
        )
    return out


def session_seeds(enrollments, current_terms_version, now_iso, limit, current_terms_sha256=None):
    """`seedable_voices`, ordered and bounded for one transcription session.

    The limit is a guard against a table that has grown past what session setup
    should spend time on, not a product rule -- a deployment with more enrolled
    people than this is served by the transcript miner for the rest.

    Which ones survive the cut therefore matters, and the order is by
    enrollment date, newest first. Nothing here knows who is in the room: a
    meeting recorded from a browser tab has no attendee list, which is the whole
    reason the Speakers panel takes a pasted one. Recency is the only signal
    available, and it is a weak proxy for somebody still being around to speak.
    Ties break on subject id so the set is stable rather than dependent on the
    order a Scan happened to return.
    """
    usable = [
        enrollment
        for enrollment in enrollments or []
        if is_usable_for_matching(
            enrollment, current_terms_version, now_iso, current_terms_sha256
        )
    ]
    usable.sort(
        key=lambda e: (
            # EnrolledAt is set when the voiceprint is stored; ConsentedAt only
            # exists on a row that never got that far, which cannot be seeded
            # anyway. The fallback is so a hand-written or migrated row still
            # sorts somewhere deterministic rather than all landing on "".
            str(e.get("EnrolledAt") or e.get("ConsentedAt") or ""),
            str(e.get("SubjectId") or ""),
        ),
        reverse=True,
    )
    bounded = usable[: max(0, int(limit))]
    return (
        seedable_voices(bounded, current_terms_version, now_iso, current_terms_sha256),
        len(usable),
    )


def expired_subjects(enrollments, now_iso):
    """Active enrollments whose consent has aged out, for the retention pass.

    An active row with no recorded expiry is collected too. It is not usable for
    matching either (see :func:`is_usable_for_matching`), and leaving it alone
    would keep a voiceprint alive for ever on a consent nothing can show is
    current -- which is the one outcome the retention period exists to prevent.
    """
    return [
        enrollment["SubjectId"]
        for enrollment in enrollments or []
        if enrollment.get("Status") == STATUS_ACTIVE
        and (
            not enrollment.get("ConsentExpiresAt")
            or _parse(enrollment["ConsentExpiresAt"]) <= _parse(now_iso)
        )
    ]


def _parse(value):
    """Parse an ISO-8601 timestamp, treating a bare one as UTC."""
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
