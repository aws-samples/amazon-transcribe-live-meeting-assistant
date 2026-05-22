# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""URL helper for generating LMA web app deep links."""

import os
from typing import Optional
from urllib.parse import quote


def get_web_app_url() -> Optional[str]:
    """Get the LMA web app base URL from environment."""
    return os.environ.get("LMA_WEB_APP_URL", "").rstrip("/") or None


def get_meeting_url(meeting_id: str) -> Optional[str]:
    """Generate the URL to the meeting detail page."""
    base = get_web_app_url()
    if not base or not meeting_id:
        return None
    return f"{base}/#/calls/{quote(meeting_id, safe='')}"


def get_virtual_participant_url(meeting_id: str) -> Optional[str]:
    """Generate the URL to the virtual participant page for a meeting."""
    base = get_web_app_url()
    if not base or not meeting_id:
        return None
    return f"{base}/#/virtual-participant/{quote(meeting_id, safe='')}"
