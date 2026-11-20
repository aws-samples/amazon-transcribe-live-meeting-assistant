# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A meeting's consent record as the MCP get_meeting_consent tool and the meeting
export give it: camelCase, LMA's own values, and no evidence images.

The record is the meeting's ``Consent`` attribute, the shape getMeetingConsent
answers with (without its signed links). See docs/meeting-consent.md.

:func:`meeting_consent_output` is both the get_meeting_consent answer and the
export's ``consent`` part, so the two always give the record in one shape. It
gives the stored values unchanged -- ``mode``, ``rule``, ``status``, each
event's ``decision`` and ``source`` are LMA's own names -- with one entry per
consent event, and says only whether evidence exists.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

# ── the consent record ──────────────────────────────────────────────────────


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
    """The consent record for a getMeetingConsent result, or ``{"Consent": <the stored record>}``.

    The get_meeting_consent answer and the export's ``consent`` part. A meeting
    whose consent was never started has mode ``off`` and status
    ``not_required``, and ``captureAllowed`` is the meeting's capture gate when
    ``state`` gives one (getMeetingConsent does; the export does not, so it is
    null there).
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
