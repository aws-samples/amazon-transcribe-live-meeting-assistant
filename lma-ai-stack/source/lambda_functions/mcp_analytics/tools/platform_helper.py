# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Meeting platform and meeting ID normalisation for the Virtual Participant tools."""

import re
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlsplit

# The platform names a caller may pass, folded to lower case with spaces,
# hyphens and underscores removed, and the value the Virtual Participant expects.
_PLATFORMS = {
    "zoom": "ZOOM",
    "teams": "TEAMS",
    "microsoftteams": "TEAMS",
    "chime": "CHIME",
    "amazonchime": "CHIME",
    "webex": "WEBEX",
    "googlemeet": "GOOGLE_MEET",
    "meet": "GOOGLE_MEET",
}

PLATFORM_CHOICES = "Zoom, Teams, Chime, Webex, Google Meet"

_MEET_CODE = re.compile(r"^([a-z]{3})-?([a-z]{4})-?([a-z]{3})$")
_MEET_LINK = re.compile(
    r"^(?:https?://)?meet\.google\.com/(?:meet/)?([a-z]{3}-?[a-z]{4}-?[a-z]{3})/?(?:[?#].*)?$"
)


def normalize_platform(meeting_platform: str) -> str:
    """The Virtual Participant's name for a platform; ValueError if it is not one."""
    key = re.sub(r"[\s_-]+", "", (meeting_platform or "").lower())
    if key not in _PLATFORMS:
        raise ValueError(f"Invalid platform. Must be one of: {PLATFORM_CHOICES}")
    return _PLATFORMS[key]


def normalize_meeting_id(platform: str, meeting_id: str) -> str:
    """The meeting ID with all whitespace removed, and a Meet link reduced to its code.

    A Google Meet meeting is identified by its code ("abc-defg-hij"), which is
    also what the create form stores. Anything else for Google Meet is refused
    here, rather than launching a Virtual Participant that cannot find the meeting;
    that includes a meet.google.com/lookup/ nickname link, which only signed-in
    members of the organiser's Workspace can open. The Virtual Participant, the
    create form, the invitation parser and the SDK apply the same rule, and each
    runs the shared table in test-fixtures/google-meet-inputs.json.
    """
    compact = "".join((meeting_id or "").split())
    if platform != "GOOGLE_MEET":
        return compact
    lowered = compact.lower()
    link = _MEET_LINK.match(lowered)
    code = _MEET_CODE.match(link.group(1) if link else lowered)
    if not code:
        raise ValueError(
            "For Google Meet, meetingId must be the meeting code (for example "
            "abc-defg-hij) or a https://meet.google.com/ link to it"
        )
    return f"{code.group(1)}-{code.group(2)}-{code.group(3)}"


# The longest passcode Zoom lets a host set. Zoom's Meetings API reference
# (developers.zoom.us/docs/api/meetings/, "Create a meeting", `password`): "By
# default, a passcode can only have a maximum length of 10 characters and only
# contain alphanumeric characters and the @, -, _, and * characters." The
# Virtual Participant uses the same limit (meeting-password.ts).
ZOOM_PASSCODE_MAX_LENGTH = 10

# The pwd value of a Zoom join link, as far as one has been seen whole: 30
# letters and digits followed by ".1" (32 characters). A copy cut off at 20
# characters had no ".1". So a value counts as complete only when it ends in a
# period and digits after at least 30 letters and digits; anything shorter, or
# without the ending, may have been cut off.
_ZOOM_LINK_TOKEN = re.compile(r"^[A-Za-z0-9]{30,}\.\d+$")

_ZOOM_HOST = re.compile(r"^([a-z0-9-]+\.)*zoom\.us$", re.IGNORECASE)
# /j/<number> (the invitation link), and /wc/<number>/join and
# /wc/join/<number> (the web client). The Virtual Participant reads the same
# forms (zoom-join-link.ts).
_ZOOM_PATHS = (
    re.compile(r"^/j/(\d+)/?$"),
    re.compile(r"^/wc/(\d+)(?:/join)?/?$"),
    re.compile(r"^/wc/join/(\d+)/?$"),
)
# /my/<name>, a personal meeting link. It carries no meeting number; the
# Virtual Participant finds it from the redirect Zoom answers the link with
# (zoom-join-link.ts parseZoomPersonalLink).
_ZOOM_PERSONAL_PATH = re.compile(r"^/my/[^/]+/?$")

ZOOM_PASSWORD_LOOKS_LIKE_LINK_VALUE = (
    "This looks like the pwd value from a Zoom join link, possibly cut off. Pass the "
    "passcode shown in the invitation text, or the full join link as meetingId."
)
ZOOM_LINK_NOT_READ = (
    "For Zoom, meetingId must be the numeric meeting ID, a full zoom.us join link "
    "(https://<name>.zoom.us/j/<number>?pwd=...), or a personal meeting link "
    "(https://<name>.zoom.us/my/<room>); otherwise pass the meeting ID and passcode "
    "from the invitation."
)


def is_complete_zoom_link_token(value: Optional[str]) -> bool:
    """Whether a value has the shape of a whole Zoom join-link pwd value (see _ZOOM_LINK_TOKEN)."""
    return bool(_ZOOM_LINK_TOKEN.match((value or "").strip()))


def zoom_link_parts(meeting_id: Optional[str]) -> Optional[Tuple[str, Optional[str]]]:
    """The meeting number and pwd value of a Zoom join link, or None if it is not one."""
    text = (meeting_id or "").strip()
    if not re.match(r"^https?://", text, re.IGNORECASE):
        return None
    parts = urlsplit(text)
    if not _ZOOM_HOST.match(parts.hostname or ""):
        return None
    for pattern in _ZOOM_PATHS:
        match = pattern.match(parts.path)
        if match:
            pwd = (parse_qs(parts.query).get("pwd") or [""])[0].strip()
            return match.group(1), (pwd or None)
    return None


def zoom_personal_link_pwd(meeting_id: Optional[str]) -> Optional[Tuple[str, Optional[str]]]:
    """For a Zoom personal meeting link, ("", its pwd value or None); None if it is not one."""
    text = (meeting_id or "").strip()
    if not re.match(r"^https?://", text, re.IGNORECASE):
        return None
    parts = urlsplit(text)
    if not _ZOOM_HOST.match(parts.hostname or "") or not _ZOOM_PERSONAL_PATH.match(parts.path):
        return None
    pwd = (parse_qs(parts.query).get("pwd") or [""])[0].strip()
    return "", (pwd or None)


def check_zoom_meeting(meeting_id: str, meeting_password: Optional[str]) -> None:
    """Refuse a Zoom meeting ID or password the Virtual Participant cannot join with.

    A meeting ID that is a link must be a zoom.us join link the Virtual
    Participant reads, or a personal meeting link, which it resolves to the
    meeting number before joining. A password is accepted when it is a passcode (at most
    ZOOM_PASSCODE_MAX_LENGTH characters, measured as the Virtual Participant
    enters it, without the grouping spaces of a numeric passcode) or a complete
    link pwd value, which the Virtual Participant puts on the join URL. Any
    other password is refused, unless the meeting ID is a link with its own pwd
    value, which the Virtual Participant then uses instead.
    """
    link = None
    if re.match(r"^https?://", meeting_id or "", re.IGNORECASE):
        link = zoom_link_parts(meeting_id) or zoom_personal_link_pwd(meeting_id)
        if link is None:
            raise ValueError(ZOOM_LINK_NOT_READ)
    password = (meeting_password or "").strip()
    if re.fullmatch(r"[\d\s]+", password):
        password = "".join(password.split())
    if not password or len(password) <= ZOOM_PASSCODE_MAX_LENGTH:
        return
    if is_complete_zoom_link_token(password) or (link and link[1]):
        return
    raise ValueError(ZOOM_PASSWORD_LOOKS_LIKE_LINK_VALUE)
