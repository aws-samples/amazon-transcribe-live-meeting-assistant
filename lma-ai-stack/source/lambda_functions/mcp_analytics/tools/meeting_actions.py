# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tools that act on one meeting through the code paths the meeting page uses.

* end_meeting -- endVirtualParticipant for each of the meeting's Virtual
  Participants that has not finished (virtual_participants.end_vp).
* delete_meeting and share_meeting / unshare_meeting -- the meeting controls
  Lambda behind deleteMeetings and shareMeetings, invoked directly with the MCP
  caller's identity, so its own owner-or-Admin check applies as well as the one
  here. Those two mutations accept signed-in users only, so they cannot be
  called with this function's IAM role.
* regenerate_meeting_summary -- the summary regenerator behind
  regenerateCallSummary, invoked the same way, so its own access rule applies.
* get_meeting_cost -- getCallCost, the meeting page's Cost panel.
"""

import os
import re
from typing import Any, Dict, List

from tools.access import (
    Caller,
    invoke_function,
    is_live,
    list_keys,
    require_meeting,
    require_meeting_manager,
    resolver_identity,
)
from tools.appsync_client import graphql
from tools.get_virtual_participant_status import shared_with_entries
from tools.url_helper import get_meeting_url
from tools.virtual_participants import active_vps_of_meeting, end_vp

# One recipient: an email address, as meeting_controls_resolver accepts it.
RECIPIENT_PATTERN = re.compile(r"[^@\[\]\s,]+@[^@\[\]\s,]+\.[^@\[\]\s,]+")
MAX_RECIPIENTS = 50
DELETED = "Meetings deleted successfully"
SHARED = "Meetings shared successfully"

GET_CALL_COST = """
query GetCallCost($CallId: ID!) {
  getCallCost(CallId: $CallId) {
    CallId
    Currency
    TotalCost
    UnpricedCount
    PriceProfile
    PricesAsOf
    Region
    NotIncluded
    ComputedAt
    ContextTotals {
      Context
      Cost
    }
    Items {
      Context
      Service
      Unit
      Quantity
      UnitPrice
      Cost
      Priced
      Note
    }
  }
}
"""


def end_meeting(caller: Caller, meeting_id: str, reason: Any = None) -> Dict[str, Any]:
    """End the meeting's Virtual Participants, which ends the meeting."""
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or meeting_id.strip()
    vps, complete = active_vps_of_meeting(call_id)
    if not vps:
        if not complete:
            raise ValueError(
                f"No unfinished Virtual Participant of meeting {call_id} was found, but the "
                "Virtual Participant table is larger than one lookup covers, so one may exist. "
                "Find it with list_virtual_participants and end it with end_virtual_participant."
            )
        if is_live(meeting):
            raise ValueError(
                f"Meeting {call_id} is not being captured by a Virtual Participant. It is "
                "streamed from a browser tab, the desktop capture app or the browser "
                "extension, and ends when the capture is stopped there."
            )
        raise ValueError(f"Meeting {call_id} has already ended (status {meeting.get('Status')})")
    ended = [end_vp(caller, vp, reason if isinstance(reason, str) else None) for vp in vps]
    return {
        "meetingId": call_id,
        "endedVirtualParticipants": ended,
        "message": "The Virtual Participant is leaving the meeting; the meeting ends and its "
        "summary is generated once it has left.",
        "meetingUrl": get_meeting_url(call_id),
    }


def _controls(field: str, caller: Caller, inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke the meeting controls Lambda as AppSync would for `field`."""
    event = {
        "identity": resolver_identity(caller),
        "arguments": {"input": inputs},
        "info": {"fieldName": field},
    }
    result = invoke_function(os.environ.get("MEETING_CONTROLS_FUNCTION", ""), event, 120)
    return result if isinstance(result, dict) else {}


def delete_meeting(caller: Caller, meeting_id: str, confirm: Any = False) -> Dict[str, Any]:
    """Delete the meeting and everything stored with it."""
    if confirm is not True:
        raise ValueError(
            "delete_meeting permanently deletes the meeting, its transcript, summary, "
            "recordings, key frames, chat attachments and consent record. Nothing is "
            "deleted until it is called again with confirm: true."
        )
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or meeting_id.strip()
    require_meeting_manager(meeting, caller, "delete it")
    outcome = _controls(
        "deleteMeetings", caller, {"Calls": [{"CallId": call_id, **list_keys(meeting)}]}
    )
    if outcome.get("Result") != DELETED:
        raise ValueError(outcome.get("Result") or "The meeting could not be deleted")
    return {"meetingId": call_id, "deleted": True}


def _recipients(value: Any) -> List[str]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("recipients must be a list of email addresses")
    found = []
    for item in value:
        address = item.strip()
        if not address:
            continue
        if not RECIPIENT_PATTERN.fullmatch(address):
            raise ValueError(f"{address!r} is not an email address")
        if address not in found:
            found.append(address)
    if not found:
        raise ValueError("recipients must name at least one email address")
    if len(found) > MAX_RECIPIENTS:
        raise ValueError(f"recipients can name at most {MAX_RECIPIENTS} addresses at a time")
    return found


def change_sharing(caller: Caller, meeting_id: str, recipients: Any, share: bool) -> Dict[str, Any]:
    """Add recipients to, or remove them from, the people the meeting is shared with."""
    addresses = _recipients(recipients)
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or meeting_id.strip()
    require_meeting_manager(meeting, caller, "change who it is shared with")
    current = shared_with_entries(meeting.get("SharedWith"))
    if share:
        wanted = current + [address for address in addresses if address not in current]
    else:
        wanted = [entry for entry in current if entry not in addresses]
    outcome = _controls(
        "shareMeetings",
        caller,
        {
            "Calls": [{"CallId": call_id, **list_keys(meeting)}],
            "MeetingRecipients": ",".join(wanted),
        },
    )
    if outcome.get("Result") != SHARED:
        raise ValueError(outcome.get("Result") or "The meeting's sharing could not be changed")
    return {"meetingId": call_id, "sharedWith": wanted}


def regenerate_summary(caller: Caller, meeting_id: str) -> Dict[str, Any]:
    """Start a new summary of the meeting; it replaces the stored one when it finishes."""
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or meeting_id.strip()
    event = {"arguments": {"input": {"CallId": call_id}}, "identity": resolver_identity(caller)}
    outcome = invoke_function(os.environ.get("SUMMARY_REGENERATOR_FUNCTION", ""), event, 60)
    if not isinstance(outcome, dict) or not outcome.get("Success"):
        message = outcome.get("Message") if isinstance(outcome, dict) else None
        raise ValueError(f"The summary was not regenerated: {message or 'unknown reason'}")
    return {
        "meetingId": call_id,
        "started": True,
        "message": "A new summary is being generated. It replaces the current one when it is "
        "done, usually within a few minutes; read it with get_meeting_summary.",
    }


def meeting_cost(caller: Caller, meeting_id: str) -> Dict[str, Any]:
    """The meeting's itemised cost estimate."""
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or meeting_id.strip()
    cost = graphql(GET_CALL_COST, {"CallId": call_id}).get("getCallCost") or {}
    return {"meetingId": call_id, **cost, "note": "An estimate computed from metering records."}
