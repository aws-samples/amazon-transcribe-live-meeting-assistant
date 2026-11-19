# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Join again: a new Virtual Participant, and a new LMA meeting, from an existing one.

The Join again form on a Virtual Participant's page offers two choices. Continuing
the meeting is a rejoin (rejoin.py). Starting a new meeting is this module's
`joinAgainVirtualParticipant` operation: it writes a new Virtual Participant row
with the form's values, as createVirtualParticipant does, and launches it.

What it adds to a create is the meeting password. The password is stored on the
source row and no read returns it, so the form cannot show it. When the new
Virtual Participant joins the same meeting as the source (the same platform and
the same meeting ID) and the form leaves the password empty, the stored
password is copied onto the new row. A password the user typed is used instead.
For another meeting nothing is copied. Only the source's owner or an Admin may
join again from it.

An immediate launch is started here, with the launcher state machine input that
rejoin builds (rejoin.start_launch), rather than from the browser. Like every
launch it carries no meeting password: the new Virtual Participant reads the
password from its own row when it starts. A scheduled one is launched at its
time by the scheduler, as for any scheduled Virtual Participant. See
docs/virtual-participant.md#join-again.
"""

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from access import NotAuthorizedError, caller_may_change
from botocore.exceptions import ClientError
from rejoin import start_launch, without_password

logger = logging.getLogger(__name__)

#: The bounds the createVirtualParticipant resolver holds.
MAX_AGENDA_CHARS = 10000
MAX_TIME_ZONE_CHARS = 64
CONSENT_MODES = ("off", "notify", "opt_in")
CONSENT_RULES = ("each_participant", "owner_attests")

_URL = re.compile(r"^\s*https?://", re.IGNORECASE)


class JoinAgainRefused(Exception):
    """The request cannot be carried out as given; the message says why."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _meeting_id(value: Any) -> str:
    """A meeting ID as the form submits it: without spaces."""
    return _text(value).replace(" ", "")


def takes_password(platform: str, meeting_id: str) -> bool:
    """Whether a meeting is joined with a separate password.

    A join link carries its own, and Google Meet has none; the form hides the
    password field for both and never submits one.
    """
    return platform != "GOOGLE_MEET" and not _URL.match(meeting_id or "")


def same_meeting(source: Dict[str, Any], request: Dict[str, Any]) -> bool:
    """Whether the request names the meeting the source Virtual Participant joined."""
    return _text(source.get("meetingPlatform")) == _text(request.get("meetingPlatform")) and (
        _meeting_id(source.get("meetingId")) == _meeting_id(request.get("meetingId"))
    )


def meeting_password_for(source: Dict[str, Any], request: Dict[str, Any]) -> Optional[str]:
    """The password the new Virtual Participant joins with, or None.

    A typed password always wins. With none typed, the source's stored password
    is used when the request names the same meeting. A meeting that takes no
    separate password gets none.
    """
    platform = _text(request.get("meetingPlatform"))
    if not takes_password(platform, _meeting_id(request.get("meetingId"))):
        return None
    typed = _text(request.get("meetingPassword"))
    if typed:
        return typed
    if same_meeting(source, request):
        return _text(source.get("meetingPassword")) or None
    return None


def _retention_seconds() -> int:
    """MeetingRecordExpirationInDays, in whole seconds (the days may be a fraction)."""
    try:
        days = float(os.environ.get("MEETING_RECORD_EXPIRATION_IN_DAYS", "90"))
    except ValueError:
        days = 90.0
    return int(days * 86400)


def _meeting_time(value: Any, now: datetime) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        seconds = int(value)
    except (TypeError, ValueError) as error:
        raise JoinAgainRefused("meetingTime must be a time in epoch seconds") from error
    if seconds <= int(now.timestamp()):
        raise JoinAgainRefused("The meeting time must be in the future.")
    return seconds


def new_row(
    request: Dict[str, Any], identity: Dict[str, Any], password: Optional[str], now: datetime
) -> Dict[str, Any]:
    """The new Virtual Participant's row, as createVirtualParticipant writes one.

    The caller owns it: Join again starts a Virtual Participant of one's own.
    """
    name = _text(request.get("meetingName")).strip()
    platform = _text(request.get("meetingPlatform")).strip()
    meeting_id = _meeting_id(request.get("meetingId"))
    if not name or not platform or not meeting_id:
        raise JoinAgainRefused("A meeting name, platform and meeting ID are required.")

    owner = identity.get("username")
    if not owner:
        raise NotAuthorizedError("Join again needs a signed-in user")
    meeting_time = _meeting_time(request.get("meetingTime"), now)
    stamp = now.isoformat()
    row: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "meetingName": name,
        "meetingPlatform": platform,
        "meetingId": meeting_id,
        "status": "SCHEDULED" if meeting_time else "INITIALIZING",
        "owner": owner,
        "Owner": owner,
        "SharedWith": "",
        "createdAt": stamp,
        "updatedAt": stamp,
    }
    if password:
        row["meetingPassword"] = password

    use_zoom_account = platform == "ZOOM" and request.get("useStoredZoomAccount") is True
    if platform == "ZOOM" and isinstance(request.get("useStoredZoomAccount"), bool):
        row["useStoredZoomAccount"] = use_zoom_account
    if meeting_time:
        row["meetingTime"] = meeting_time
        row["scheduledFor"] = (
            datetime.fromtimestamp(meeting_time, timezone.utc).isoformat().replace("+00:00", "Z")
        )
        row["isScheduled"] = True
        # Read by the scheduler at launch time, as for a scheduled create.
        sub = identity.get("sub")
        if sub:
            row["userSub"] = sub
            if use_zoom_account:
                row["userZoomSub"] = sub
        time_zone = _text(request.get("scheduledTimeZone"))
        if len(time_zone) > MAX_TIME_ZONE_CHARS:
            raise JoinAgainRefused(
                f"scheduledTimeZone is longer than {MAX_TIME_ZONE_CHARS} characters"
            )
        if time_zone:
            row["scheduledTimeZone"] = time_zone

    if isinstance(request.get("enableVideoRecording"), bool):
        row["enableVideoRecording"] = request["enableVideoRecording"]

    agenda = _text(request.get("agenda"))
    if agenda.strip():
        if len(agenda) > MAX_AGENDA_CHARS:
            raise JoinAgainRefused(f"The agenda is longer than {MAX_AGENDA_CHARS} characters")
        row["agenda"] = agenda
        row["agendaSource"] = "scheduled"

    mode = _text(request.get("consentMode"))
    rule = _text(request.get("consentRule"))
    if mode:
        if mode not in CONSENT_MODES:
            raise JoinAgainRefused("consentMode must be off, notify or opt_in")
        row["consentMode"] = mode
    if rule:
        if rule not in CONSENT_RULES:
            raise JoinAgainRefused("consentRule must be each_participant or owner_attests")
        if mode != "opt_in":
            raise JoinAgainRefused("consentRule is only used with consentMode opt_in")
        row["consentRule"] = rule

    # The table's time to live: the meeting record's retention, from the later
    # of now and the scheduled time.
    row["ExpiresAfter"] = max(int(now.timestamp()), meeting_time or 0) + _retention_seconds()
    return row


def _mark_failed(table: Any, vp_id: str) -> None:
    try:
        table.update_item(
            Key={"id": vp_id},
            UpdateExpression="SET #status = :failed, errorMessage = :reason, updatedAt = :now",
            ConditionExpression="#status = :initializing",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":failed": "FAILED",
                ":reason": "The Virtual Participant could not be launched.",
                ":now": _now().isoformat(),
                ":initializing": "INITIALIZING",
            },
        )
    except ClientError:
        logger.exception(f"VP {vp_id}: could not record that its launch did not start")


def join_again_virtual_participant(
    manager: Any, request: Dict[str, Any], identity: Dict[str, Any]
) -> Dict[str, Any]:
    """Create and launch a new Virtual Participant from the source one; answers with its row."""
    source_id = _text(request.get("sourceVpId"))
    source = manager.get_virtual_participant(source_id) if source_id else None
    if not caller_may_change(identity, source):
        # Told the same way whether or not the row exists.
        raise NotAuthorizedError(
            f"User {identity.get('username')} may not join again from "
            f"Virtual Participant {source_id}"
        )
    if not source:
        raise ValueError(f"Virtual Participant {source_id} not found")

    now = _now()
    password = meeting_password_for(source, request)
    row = new_row(request, identity, password, now)
    copied = bool(password) and not _text(request.get("meetingPassword"))
    logger.info(
        f"VP {row['id']}: joining again from VP {source_id} "
        f"({'scheduled' if row.get('isScheduled') else 'now'}; "
        f"stored meeting password used: {copied})"
    )

    manager.table.put_item(Item=row, ConditionExpression="attribute_not_exists(id)")
    if not row.get("isScheduled"):
        try:
            # The row's first launch, so its id is the launch token.
            start_launch(row, identity, row["id"])
        except Exception as error:  # noqa: BLE001 - recorded on the row, then raised
            logger.exception(f"VP {row['id']}: launch did not start")
            _mark_failed(manager.table, row["id"])
            raise RuntimeError("The Virtual Participant could not be launched.") from error
    return without_password(row)
