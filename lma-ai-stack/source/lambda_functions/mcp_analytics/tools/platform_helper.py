# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Meeting platform and meeting ID normalisation for the Virtual Participant tools."""

import re

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
    here, rather than launching a Virtual Participant that cannot find the meeting.
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
