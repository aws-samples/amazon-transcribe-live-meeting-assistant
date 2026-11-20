# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The meeting preparation an MCP client gives a Virtual Participant.

schedule_meeting, start_meeting_now and update_scheduled_meeting take the same
optional arguments -- ``labels``, ``audience``, ``context``, ``checklist`` and
``shareWith`` -- and pass them to createVirtualParticipant or
updateScheduledVirtualParticipant as ``labels``, ``audience``,
``meetingContext``, ``checklist`` and ``meetingSharedWith``. The VP stores them
on its row and, when it starts, puts them on the meeting it creates; the
meeting is then shared with the ``shareWith`` addresses. Sharing the Virtual
Participant itself is a separate thing and is not done here.
"""

from typing import Any, Dict, Mapping, Optional

from tools.preparation import (
    audience_input,
    checklist_input,
    context_input,
    item_output,
    labels_input,
    labels_output,
)
from tools.recipients import recipients


def vp_inputs(
    labels: Any = None,
    audience: Any = None,
    context: Any = None,
    checklist: Any = None,
    share_with: Any = None,
    *,
    for_update: bool = False,
) -> Dict[str, Any]:
    """The mutation input fields for the arguments given, checked.

    For a new VP an empty argument is left out. For an update it is sent empty,
    which removes that input from the scheduled VP.
    """
    fields: Dict[str, Any] = {}
    if labels is not None:
        fields["labels"] = labels_input(labels)
    if audience is not None:
        fields["audience"] = audience_input(audience) or ""
    if context is not None:
        fields["meetingContext"] = context_input(context)
    if checklist is not None:
        fields["checklist"] = checklist_input(checklist)
    if share_with is not None:
        fields["meetingSharedWith"] = recipients(share_with, "shareWith", allow_empty=True)
    if not for_update:
        fields = {name: value for name, value in fields.items() if value not in ("", [], None)}
    return fields


def vp_preparation_output(vp: Mapping[str, Any]) -> Dict[str, Any]:
    """A VP row's agenda and meeting preparation, as the MCP tools name them."""
    shared = vp.get("meetingSharedWith")
    share_with = [entry.strip() for entry in str(shared or "").split(",") if entry.strip()]
    return {
        "agenda": vp.get("agenda") or "",
        "labels": labels_output(vp.get("labels")),
        "audience": vp.get("audience") or None,
        "context": vp.get("meetingContext") or "",
        "checklist": [
            {
                key: value
                for key, value in item_output(item).items()
                if key in ("itemId", "text", "kind", "priority", "source")
            }
            for item in vp.get("checklist") or []
            if isinstance(item, Mapping)
        ],
        "shareWith": share_with,
    }


def platform_fields(platform_meeting_id: Optional[str], call_id: Optional[str]) -> Dict[str, Any]:
    """The meeting identifiers a VP tool answers with.

    ``meetingId`` is the LMA meeting (the Call id), null until the VP has
    created it; ``platformMeetingId`` is the meeting platform's ID or join link.
    ``callId`` is the earlier name of ``meetingId``, kept for clients that read it.
    """
    return {
        "meetingId": call_id or None,
        "platformMeetingId": platform_meeting_id,
        "callId": call_id or None,
    }
