# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tool: get_meeting_consent -- a meeting's consent record, without its images.

Read with getMeetingConsent, the query behind the meeting page's Consent
section, which is shown to everyone the transcript is shown to; so is this
answer: an Admin, the meeting's owner, a user the meeting is shared with. The
evidence images and their links are never selected: the answer says only
whether evidence exists. The images themselves are in get_meeting_record's
``consent`` section, for the meeting's owner and Admins. See
docs/meeting-consent.md.

The values are LMA's own, unchanged: the mode, the rule, the status, each
notice's channel, and one entry per consent event with its decision and
source. An event has no email address. A reply that was linked to the
transcript speaker who gave it, when it was recorded, gives that speaker's
``speakerId`` -- the participant id the meeting export gives the speaker -- and
how and when it was linked; the ids come from the meeting's final transcript
lines, read only when an event is linked and the transcript is still kept. A
name is never matched to a speaker. A chat reply linked by its sender's
platform participant id (``platform_identity``) is confirmed with that id,
which getMeetingConsent does not give: it is read from the meeting's stored
record, by the event's id, and is never part of the answer. The shaping is lma_consent.summary in the
configuration layer.
"""

from typing import Any, Dict

# pylint: disable-next=import-error
from lma_consent.summary import has_speaker_links, meeting_consent_output

# pylint: disable-next=import-error
from lma_speakers.participants import SPEAKER_FIELDS, Speakers, final_segments

from tools.access import Caller, require_meeting
from tools.appsync_client import graphql
from tools.transcript_rows import read_final_rows

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
      Notices {
        Channel
        PostedAt
      }
      StartedAt
      UpdatedAt
      Events {
        EventId
        ParticipantName
        Decision
        Source
        OccurredAt
        RecordedAt
        SpeakerLink {
          Basis
          SegmentId
          Channel
          Speaker
          LinkedAt
        }
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


def with_stored_platform_ids(state: Any, stored: Any) -> Any:
    """``state`` with each platform_identity link given its id from the stored record.

    ``stored`` is the meeting row's ``Consent``, read with the meeting; an
    event is matched by its ``EventId``. A link whose event is not found, or
    whose stored link holds no id (the transcript was deleted), stays without
    one and names no speaker.
    """
    consent_state = (state or {}).get("Consent")
    if not isinstance(consent_state, dict) or not isinstance(stored, dict):
        return state
    ids = {}
    for event in stored.get("Events") or []:
        link = event.get("SpeakerLink") if isinstance(event, dict) else None
        if isinstance(link, dict) and link.get("PlatformParticipantId") and event.get("EventId"):
            ids[event["EventId"]] = link["PlatformParticipantId"]
    events = []
    for event in consent_state.get("Events") or []:
        link = event.get("SpeakerLink") if isinstance(event, dict) else None
        if isinstance(link, dict) and link.get("Basis") == "platform_identity":
            platform_id = ids.get(event.get("EventId"))
            if platform_id:
                event = {**event, "SpeakerLink": {**link, "PlatformParticipantId": platform_id}}
        events.append(event)
    return {**state, "Consent": {**consent_state, "Events": events}}


def execute(caller: Caller, meeting_id: Any) -> Dict[str, Any]:
    """get_meeting_consent."""
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or str(meeting_id).strip()
    state = graphql(GET_MEETING_CONSENT, {"CallId": call_id}).get("getMeetingConsent")
    speakers = None
    if has_speaker_links((state or {}).get("Consent")) and not meeting.get("TranscriptDeletedAt"):
        # The ids the meeting export gives its speakers, from the same lines;
        # only the fields the ids are worked out from are read.
        rows = read_final_rows(call_id, finals_only=False, fields=SPEAKER_FIELDS)
        speakers = Speakers(final_segments(rows), meeting)
        # The meeting row was read before getMeetingConsent, so a chat reply
        # recorded between the two reads is given without a speaker this once.
        state = with_stored_platform_ids(state, meeting.get("Consent"))
    return meeting_consent_output(call_id, state, speakers)
