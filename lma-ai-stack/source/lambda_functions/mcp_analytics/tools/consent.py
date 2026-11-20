# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tool: get_meeting_consent -- a meeting's consent record, without its images.

Read with getMeetingConsent, the query behind the meeting page's Consent
section, which is shown to everyone the transcript is shown to; so is this
answer: an Admin, the meeting's owner, a user the meeting is shared with. The
evidence images and their links are never selected: the answer says only
whether evidence exists, and how many entries there are. The images themselves
are in get_meeting_record's ``consent`` section, for the meeting's owner and
Admins. See docs/meeting-consent.md.

One row per participant, as the meeting page lists them: everyone listed as
present, then anybody else who was listed or replied, each with their latest
reply; the seats a Virtual Participant lists for people not yet seen by name
are left out unless a reply was recorded under one. The record holds display
names only, so a row has no email address.
"""

import re
from typing import Any, Dict, List, Mapping, Optional

from lma_consent.rules import normalize_name  # pylint: disable=import-error

from tools.access import Caller, require_meeting
from tools.appsync_client import graphql

GET_MEETING_CONSENT = """
query GetMeetingConsent($CallId: ID!) {
  getMeetingConsent(CallId: $CallId) {
    CallId
    CaptureAllowed
    Consent {
      Mode
      Rule
      Status
      CaptureAllowed
      NoticePostedAt
      Participants
      ParticipantsSeen
      StartedAt
      UpdatedAt
      Events {
        ParticipantName
        Decision
        Source
        OccurredAt
        RecordedAt
      }
      Attestation {
        AttestedBy
        Method
        AttestedAt
      }
      Evidence {
        EvidenceId
      }
    }
  }
}
"""

# The stored modes, as this tool names them.
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
    """One row per participant, as the meeting page's Consent section lists them."""
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
    """The answer for a getMeetingConsent result."""
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


def execute(caller: Caller, meeting_id: Any) -> Dict[str, Any]:
    """get_meeting_consent."""
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or str(meeting_id).strip()
    state = graphql(GET_MEETING_CONSENT, {"CallId": call_id}).get("getMeetingConsent")
    return consent_output(call_id, state)
