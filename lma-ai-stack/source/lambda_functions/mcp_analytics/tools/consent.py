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
source. The record holds display names only, so an event has no email address
and no transcript speaker. The shaping is lma_consent.summary in the
configuration layer.
"""

from typing import Any, Dict

# pylint: disable-next=import-error
from lma_consent.summary import meeting_consent_output

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
      Notices {
        Channel
        PostedAt
      }
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
    return meeting_consent_output(call_id, state)
