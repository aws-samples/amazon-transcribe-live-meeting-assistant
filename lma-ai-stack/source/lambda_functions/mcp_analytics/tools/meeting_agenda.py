# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tool: update_meeting_agenda -- the agenda of a meeting in progress or a scheduled VP.

Two existing writes, chosen by what the client names:

* a meeting (``meeting_id``) in progress: updateCallAgenda, the write the
  meeting page's Agenda panel makes, recorded with source "mcp" and attributed
  to the MCP caller. Its access rule is getCall's.
* a Virtual Participant waiting for its scheduled launch
  (``virtual_participant_id``): updateScheduledVirtualParticipant, the write the
  Edit form makes, which the VP carries onto the meeting record when it starts.
  Its rule is the VP's: its owner or an Admin. The resolver also refuses a VP
  that starts in less than five minutes.
"""

from typing import Any, Dict, Optional

from tools.access import (
    Caller,
    can_manage,
    can_read,
    is_live,
    not_found_or_denied,
    require_meeting,
)
from tools.agenda_helper import normalize_agenda
from tools.appsync_client import graphql
from tools.url_helper import get_meeting_url
from tools.virtual_participants import get_vp, update_scheduled_vp

UPDATE_CALL_AGENDA = """
mutation UpdateCallAgenda($input: UpdateCallAgendaInput!) {
  updateCallAgenda(input: $input) {
    CallId
    Agenda
    AgendaSource
    AgendaUpdatedBy
    AgendaUpdatedAt
    Owner
    SharedWith
  }
}
"""


def _agenda_text(agenda: Any) -> str:
    """The agenda as stored text; an empty string or list clears it."""
    if agenda == "" or agenda == []:
        return ""
    if agenda is None:
        raise ValueError("agenda is required; pass an empty string to clear it")
    return normalize_agenda(agenda) or ""


def execute(
    caller: Caller,
    agenda: Any,
    meeting_id: Optional[str] = None,
    virtual_participant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Replace the agenda of a meeting in progress or of a scheduled VP."""
    if bool(meeting_id) == bool(virtual_participant_id):
        raise ValueError("Pass exactly one of meeting_id or virtual_participant_id")
    text = _agenda_text(agenda)

    if virtual_participant_id:
        vp = get_vp(virtual_participant_id)
        if vp is None or not can_read(vp, caller):
            raise not_found_or_denied("Virtual Participant", virtual_participant_id)
        if not can_manage(vp, caller):
            raise PermissionError("Only the Virtual Participant's owner or an Admin may change it")
        if vp.get("status") != "SCHEDULED":
            raise ValueError(
                f"Virtual Participant {virtual_participant_id} is not waiting for a scheduled "
                "launch; once it has started, change the agenda on its meeting (meeting_id)"
            )
        updated = update_scheduled_vp(virtual_participant_id, {"agenda": text})
        return {
            "virtualParticipantId": virtual_participant_id,
            "agenda": updated.get("agenda", text) or "",
            "message": "The agenda is stored on the scheduled Virtual Participant and carried "
            "onto the meeting when it starts.",
        }

    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or (meeting_id or "").strip()
    if not is_live(meeting):
        raise ValueError(
            f"Meeting {call_id} is not in progress (status {meeting.get('Status')}); this tool "
            "changes the agenda of a meeting in progress or a scheduled Virtual Participant"
        )
    updated = (
        graphql(
            UPDATE_CALL_AGENDA,
            {
                "input": {
                    "CallId": call_id,
                    "Agenda": text,
                    "AgendaSource": "mcp",
                    "AgendaUpdatedBy": caller.user_id,
                }
            },
        ).get("updateCallAgenda")
        or {}
    )
    return {
        "meetingId": call_id,
        "agenda": updated.get("Agenda", text) or "",
        "agendaUpdatedAt": updated.get("AgendaUpdatedAt"),
        "meetingUrl": get_meeting_url(call_id),
    }
