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
names only, so a row has no email address. The shaping is lma_consent.summary
in the configuration layer, which the meeting export shares.
"""

from typing import Any, Dict

# pylint: disable-next=import-error
from lma_consent.summary import consent_output, participant_rows  # noqa: F401

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


def execute(caller: Caller, meeting_id: Any) -> Dict[str, Any]:
    """get_meeting_consent."""
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or str(meeting_id).strip()
    state = graphql(GET_MEETING_CONSENT, {"CallId": call_id}).get("getMeetingConsent")
    return consent_output(call_id, state)
