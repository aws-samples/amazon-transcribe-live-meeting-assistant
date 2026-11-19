# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Meeting models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class MeetingSession(BaseModel):
    """A capture session ``reopenMeeting`` allowed on an existing meeting.

    ``session_number`` is what a streaming client names in its START frame
    (``sessionNumber``) to continue the meeting; ``session_offset_seconds`` is
    where the session starts on the meeting's transcript timeline.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    call_id: str
    session_number: int
    session_offset_seconds: float
    reopened_at: str | None = None
