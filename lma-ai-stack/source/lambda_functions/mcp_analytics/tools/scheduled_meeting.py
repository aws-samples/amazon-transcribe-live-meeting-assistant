# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tool: update_scheduled_meeting -- change a Virtual Participant before it starts.

The write is updateScheduledVirtualParticipant, the one the Virtual Participant
page's Edit form makes. Its resolver holds the change to the VP's owner, an
Admin or a backend role, to a VP that is still SCHEDULED, and to one that
starts at least five minutes from now, with a new start time as far away. This
function's IAM role is a backend role there, so the owner-or-Admin rule is
applied here first (virtual_participants: a VP is changed by its owner or an
Admin), and the five-minute rule too, so the client gets a plain answer.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from tools.access import Caller, can_manage, can_read, not_found_or_denied
from tools.meeting_agenda import agenda_text
from tools.virtual_participants import get_vp, summary, update_scheduled_vp
from tools.vp_preparation import platform_fields, vp_inputs, vp_preparation_output

# The resolver's edit cut-off (updateScheduledVirtualParticipant.request.vtl).
EDIT_CUTOFF_SECONDS = 300


def _epoch(scheduled_time: Any) -> int:
    if not isinstance(scheduled_time, str) or not scheduled_time.strip():
        raise ValueError("scheduledTime must be an ISO 8601 date and time")
    try:
        moment = datetime.fromisoformat(scheduled_time.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            "scheduledTime must be an ISO 8601 date and time, for example 2026-01-10T15:30:00Z"
        ) from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp())


def execute(  # pylint: disable=too-many-arguments
    caller: Caller,
    virtual_participant_id: Any,
    scheduled_time: Any = None,
    meeting_name: Any = None,
    agenda: Any = None,
    labels: Any = None,
    audience: Any = None,
    context: Any = None,
    checklist: Any = None,
    share_with: Any = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Change the scheduled VP's time, name, agenda or meeting preparation."""
    changes: Dict[str, Any] = vp_inputs(
        labels, audience, context, checklist, share_with, for_update=True
    )
    if meeting_name is not None:
        if not isinstance(meeting_name, str) or not meeting_name.strip():
            raise ValueError("meetingName cannot be empty")
        changes["meetingName"] = meeting_name.strip()
    if agenda is not None:
        changes["agenda"] = agenda_text(agenda)
    current = datetime.now(timezone.utc).timestamp() if now is None else now
    if scheduled_time is not None:
        changes["meetingTime"] = _epoch(scheduled_time)
    if not changes:
        raise ValueError(
            "Give at least one of scheduledTime, meetingName, agenda, labels, audience, "
            "context, checklist or shareWith"
        )

    vp = get_vp(virtual_participant_id)
    vp_id = str(virtual_participant_id).strip()
    if vp is None or not can_read(vp, caller):
        raise not_found_or_denied("Virtual Participant", vp_id)
    if not can_manage(vp, caller):
        raise PermissionError("Only the Virtual Participant's owner or an Admin may change it")
    if vp.get("status") != "SCHEDULED" or vp.get("isScheduled") is not True:
        raise ValueError(
            f"Virtual Participant {vp_id} is not waiting for a scheduled launch (status "
            f"{vp.get('status')}), so it can no longer be changed this way. Change a meeting "
            "that has started with the meeting tools (meetingId)."
        )
    try:
        starts_at = int(vp.get("meetingTime"))
    except (TypeError, ValueError):
        starts_at = None
    if starts_at is None or starts_at < current + EDIT_CUTOFF_SECONDS:
        raise ValueError(
            f"Virtual Participant {vp_id} starts in less than 5 minutes, so it can no longer "
            "be changed."
        )
    if "meetingTime" in changes and changes["meetingTime"] < current + EDIT_CUTOFF_SECONDS:
        raise ValueError("scheduledTime must be at least 5 minutes from now")

    updated = {**vp, **update_scheduled_vp(vp_id, changes)}
    return {
        **summary(updated),
        **platform_fields(updated.get("meetingId"), updated.get("CallId")),
        **vp_preparation_output(updated),
    }
