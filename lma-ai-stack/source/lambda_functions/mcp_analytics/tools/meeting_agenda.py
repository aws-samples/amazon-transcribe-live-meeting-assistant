# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tools: get_meeting_agenda and update_meeting_agenda -- a meeting's agenda.

The agenda is read from the meeting's row and replaced with updateCallAgenda,
the write the meeting page's Agenda panel makes, recorded with source "mcp" and
attributed to the MCP caller. Both answer the callers getCall answers (an Admin,
the meeting's owner, a user it is shared with), as updateCallAgenda does, and,
as on the meeting page, the agenda can be changed while the meeting runs and
after it has ended. The agenda of a Virtual Participant that has not started
yet is changed with update_scheduled_meeting.
"""

from typing import Any, Dict, Mapping

from tools.access import Caller, require_meeting
from tools.agenda_helper import normalize_agenda
from tools.appsync_client import graphql
from tools.call_fields import with_call_fields

UPDATE_CALL_AGENDA = with_call_fields(
    """
mutation UpdateCallAgenda($input: UpdateCallAgendaInput!) {
  updateCallAgenda(input: $input) { __SUBSCRIBED_CALL_FIELDS__ }
}
"""
)


def agenda_text(agenda: Any) -> str:
    """The agenda as stored text; an empty string or list clears it."""
    if agenda == "" or agenda == []:
        return ""
    if agenda is None:
        raise ValueError("agenda is required; pass an empty string to clear it")
    return normalize_agenda(agenda) or ""


def _output(call_id: str, call: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "meetingId": call_id,
        "agenda": call.get("Agenda") or "",
        "agendaSource": call.get("AgendaSource"),
        "updatedBy": call.get("AgendaUpdatedBy"),
        "updatedAt": call.get("AgendaUpdatedAt"),
    }


def get_agenda(caller: Caller, meeting_id: Any) -> Dict[str, Any]:
    """get_meeting_agenda."""
    meeting = require_meeting(meeting_id, caller)
    return _output(meeting.get("CallId") or str(meeting_id).strip(), meeting)


def update_agenda(caller: Caller, meeting_id: Any, agenda: Any) -> Dict[str, Any]:
    """update_meeting_agenda: replace the meeting's agenda; an empty string clears it."""
    text = agenda_text(agenda)
    meeting = require_meeting(meeting_id, caller)
    call_id = meeting.get("CallId") or str(meeting_id).strip()
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
    return _output(call_id, updated)
