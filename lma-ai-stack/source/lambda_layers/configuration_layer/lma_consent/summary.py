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

from .rules import normalize_name

# ── the consent record ──────────────────────────────────────────────────────


def speaker_link(event: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The event's stored ``SpeakerLink``, when it has one in a readable shape."""
    link = event.get("SpeakerLink")
    if isinstance(link, Mapping) and link.get("Basis") and link.get("SegmentId"):
        return link
    return None


def has_speaker_links(consent: Optional[Mapping[str, Any]]) -> bool:
    """Whether any of the record's events is linked to a transcript speaker."""
    return any(
        isinstance(event, Mapping) and speaker_link(event) is not None
        for event in (consent or {}).get("Events") or []
    )


def consent_events(consent: Mapping[str, Any], speakers: Any = None) -> List[Dict[str, Any]]:
    """One entry per stored consent event, in the order recorded, values unchanged.

    ``at`` is when the reply or attestation happened, or when it was recorded
    if that is all the record holds.

    ``speakerLinkBasis`` and ``speakerLinkedAt`` say how and when the event was
    linked to the transcript speaker who gave it (``same_segment``: the reply
    is that speaker's own line of the transcript), and are null for an event
    that was never linked. ``speakerId`` is that speaker's participant id, from
    ``speakers`` (an ``lma_speakers.participants.Speakers`` over the meeting's
    final segments), when the transcript still confirms the link; null
    otherwise, and always null without ``speakers``. A name is never matched
    to a speaker.
    """
    out = []
    for event in consent.get("Events") or []:
        if not isinstance(event, Mapping):
            continue
        link = speaker_link(event)
        out.append(
            {
                "participantName": event.get("ParticipantName"),
                "decision": event.get("Decision"),
                "source": event.get("Source"),
                "at": event.get("OccurredAt") or event.get("RecordedAt"),
                "speakerId": speakers.linked_id(link) if link and speakers is not None else None,
                "speakerLinkBasis": link.get("Basis") if link else None,
                "speakerLinkedAt": link.get("LinkedAt") if link else None,
            }
        )
    return out


#: A participant's consent in the export, from their latest reply.
PARTICIPANT_CONSENT = {"consent": "granted", "objection": "objected"}


def participant_consent(speaker_id: Any, events: List[Mapping[str, Any]]) -> str:
    """``granted``, ``objected`` or ``unknown`` for one transcript speaker.

    From the latest reply among ``events`` (as :func:`consent_events` gives
    them) linked to ``speaker_id``; an attestation is not a reply. ``unknown``
    when no reply is linked to the speaker: a reply under a matching name is
    not counted, because a name does not identify a speaker.

    Also ``unknown`` when the participant who gave that reply (the record's
    own participant, by its display name as the record compares names)
    replied otherwise afterwards in a reply not linked to this speaker, such as
    a chat objection after a spoken consent: the later reply is the one the
    record counts, and nothing says which speaker it came from. A later reply
    can only take a speaker's consent away, never give one.
    """
    if speaker_id is None:
        return "unknown"
    decision: Optional[str] = None
    replier: Optional[str] = None
    for event in events:
        if event.get("source") == "attestation" or event.get("decision") not in PARTICIPANT_CONSENT:
            continue
        if event.get("speakerId") == speaker_id:
            decision = event["decision"]
            replier = normalize_name(event.get("participantName"))
        elif (
            decision is not None
            and replier
            and normalize_name(event.get("participantName")) == replier
            and event["decision"] != decision
        ):
            decision, replier = None, None
    return PARTICIPANT_CONSENT[decision] if decision else "unknown"


def meeting_consent_output(
    call_id: str, state: Optional[Mapping[str, Any]], speakers: Any = None
) -> Dict[str, Any]:
    """The consent record for a getMeetingConsent result, or ``{"Consent": <the stored record>}``.

    The get_meeting_consent answer and the export's ``consent`` part. A meeting
    whose consent was never started has mode ``off`` and status
    ``not_required``, and ``captureAllowed`` is the meeting's capture gate when
    ``state`` gives one (getMeetingConsent does; the export does not, so it is
    null there).
    ``speakers`` gives each linked event its ``speakerId``; see
    :func:`consent_events`.
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
        "events": consent_events(consent, speakers),
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
