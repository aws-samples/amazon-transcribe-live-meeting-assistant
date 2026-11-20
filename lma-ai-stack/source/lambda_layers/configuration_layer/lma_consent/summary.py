# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A meeting's consent record as the MCP get_meeting_consent tool and the meeting
export give it: camelCase, and no evidence images.

The record is the meeting's ``Consent`` attribute, the shape getMeetingConsent
answers with (without its signed links). See docs/meeting-consent.md.

:func:`meeting_consent_output` is the get_meeting_consent answer. It gives the
stored values unchanged -- ``mode``, ``rule``, ``status``, each event's
``decision`` and ``source`` are LMA's own names -- with one entry per consent
event, and says only whether evidence exists.

:func:`consent_output` is the export's ``consent`` part (schema version 1),
whose field names and values are fixed by that schema: one row per
participant, ``opt-in`` for ``opt_in``, ``consented`` for ``consent``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional

from .rules import normalize_name

#: The stored modes, as the export's consent part (schema version 1) names them.
MODES = {"off": "off", "notify": "notify", "opt_in": "opt-in"}
DECISIONS = {"consent": "consented", "objection": "objected"}
PLACEHOLDER_SEAT = re.compile(r"^unidentified participant \d+$")


def _latest_replies(events: List[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    """Each participant's latest reply, by normalised name; an attestation is not a reply."""
    latest: Dict[str, Mapping[str, Any]] = {}
    for event in events:
        if event.get("Source") == "attestation":
            continue
        key = normalize_name(event.get("ParticipantName"))
        if key and event.get("Decision") in DECISIONS:
            latest[key] = event
    return latest


def participant_rows(consent: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """One row per participant, as the meeting page's Consent section lists them.

    Everyone listed as present, then anybody else who was listed or replied,
    each with their latest reply; the seats a Virtual Participant lists for
    people not yet seen by name are left out unless a reply was recorded under
    one. The record holds display names only, so a row has no email address.
    """
    events = [event for event in consent.get("Events") or [] if isinstance(event, Mapping)]
    replies = _latest_replies(events)
    rows: List[Dict[str, Any]] = []
    seen = set()
    names = [
        *(consent.get("Participants") or []),
        *(consent.get("ParticipantsSeen") or []),
        *(event.get("ParticipantName") for event in events if event.get("Source") != "attestation"),
    ]
    for name in names:
        key = normalize_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        reply = replies.get(key)
        if reply is None and PLACEHOLDER_SEAT.match(key):
            continue
        rows.append(
            {
                "name": name,
                "status": DECISIONS[reply["Decision"]] if reply else "no_reply",
                "at": (reply.get("RecordedAt") or reply.get("OccurredAt")) if reply else None,
                "method": reply.get("Source") if reply else None,
            }
        )
    return rows


def consent_output(call_id: str, state: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The answer for a getMeetingConsent result, or ``{"Consent": <the stored record>}``."""
    consent = (state or {}).get("Consent") or {}
    mode = MODES.get(str(consent.get("Mode") or "off"), consent.get("Mode"))
    if not consent or mode == "off":
        return {"meetingId": call_id, "mode": "off"}
    attestation = consent.get("Attestation") or None
    evidence = [entry for entry in consent.get("Evidence") or [] if entry]
    return {
        "meetingId": call_id,
        "mode": mode,
        # How consent is given in an opt-in meeting: by each participant, or by the
        # meeting owner's attestation; null for notify.
        "consentGivenBy": consent.get("Rule") if mode == "opt-in" else None,
        "status": consent.get("Status"),
        "captureAllowed": consent.get("CaptureAllowed"),
        "noticePostedAt": consent.get("NoticePostedAt"),
        "startedAt": consent.get("StartedAt"),
        "updatedAt": consent.get("UpdatedAt"),
        "attestation": (
            {
                "attestedBy": attestation.get("AttestedBy"),
                "method": attestation.get("Method"),
                "at": attestation.get("AttestedAt"),
            }
            if attestation
            else None
        ),
        "participants": participant_rows(consent),
        "evidence": {"exists": bool(evidence), "count": len(evidence)},
    }


# ── get_meeting_consent ─────────────────────────────────────────────────────


def consent_events(consent: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """One entry per stored consent event, in the order recorded, values unchanged.

    ``at`` is when the reply or attestation happened, or when it was recorded
    if that is all the record holds. ``speakerId`` is the transcript speaker
    the event was recorded against. A consent event records the participant's
    display name only, never a transcript speaker, and LMA does not match a
    name to a speaker, so it is null for every event.
    """
    return [
        {
            "participantName": event.get("ParticipantName"),
            "decision": event.get("Decision"),
            "source": event.get("Source"),
            "at": event.get("OccurredAt") or event.get("RecordedAt"),
            "speakerId": None,
        }
        for event in consent.get("Events") or []
        if isinstance(event, Mapping)
    ]


#: A participant's consent in the export, from their latest reply.
PARTICIPANT_CONSENT = {"consent": "granted", "objection": "objected"}


def participant_consent(speaker_id: Any, events: List[Mapping[str, Any]]) -> str:
    """``granted``, ``objected`` or ``unknown`` for one transcript speaker.

    From the latest reply among ``events`` (as :func:`consent_events` gives
    them) recorded against ``speaker_id``; an attestation is not a reply.
    ``unknown`` when no reply is recorded against the speaker: a reply under a
    matching name is not counted, because a name does not identify a speaker.
    """
    decision = None
    for event in events:
        if (
            speaker_id is not None
            and event.get("speakerId") == speaker_id
            and event.get("source") != "attestation"
            and event.get("decision") in PARTICIPANT_CONSENT
        ):
            decision = event["decision"]
    return PARTICIPANT_CONSENT[decision] if decision else "unknown"


def meeting_consent_output(call_id: str, state: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The get_meeting_consent answer for a getMeetingConsent result.

    A meeting whose consent was never started has mode ``off`` and status
    ``not_required``, and ``captureAllowed`` is the meeting's capture gate.
    ``rule`` is given for an opt-in meeting only, as only opt-in uses one.
    """
    state = state or {}
    consent = state.get("Consent") or {}
    mode = consent.get("Mode") or "off"
    attestation = consent.get("Attestation") or None
    capture = consent.get("CaptureAllowed") if consent else state.get("CaptureAllowed")
    return {
        "meetingId": call_id,
        "mode": mode,
        "rule": (consent.get("Rule") or None) if mode == "opt_in" else None,
        "status": consent.get("Status") or "not_required",
        "captureAllowed": capture,
        "noticePostedAt": consent.get("NoticePostedAt"),
        "notices": [
            {"channel": notice.get("Channel"), "postedAt": notice.get("PostedAt")}
            for notice in consent.get("Notices") or []
            if isinstance(notice, Mapping)
        ],
        "events": consent_events(consent),
        "attestation": (
            {
                "attestedBy": attestation.get("AttestedBy"),
                "method": attestation.get("Method"),
                "at": attestation.get("AttestedAt"),
            }
            if attestation
            else None
        ),
        "hasEvidence": any(entry for entry in consent.get("Evidence") or []),
        "startedAt": consent.get("StartedAt"),
        "updatedAt": consent.get("UpdatedAt"),
    }
