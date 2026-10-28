# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Virtual Participant data models."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VpStatus(str, Enum):
    """Virtual Participant lifecycle states.

    These match the status strings written by the LMA scribe container,
    the UI, and the scheduler Step Function.
    """

    INITIALIZING = "INITIALIZING"
    CONNECTING = "CONNECTING"
    JOINING = "JOINING"
    JOINED = "JOINED"
    ACTIVE = "ACTIVE"
    MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"
    COMPLETED = "COMPLETED"
    ENDED = "ENDED"
    FAILED = "FAILED"


TERMINAL_VP_STATES = {VpStatus.COMPLETED, VpStatus.ENDED, VpStatus.FAILED}
LAUNCHED_VP_STATES = {
    VpStatus.CONNECTING,
    VpStatus.JOINING,
    VpStatus.JOINED,
    VpStatus.ACTIVE,
    VpStatus.MANUAL_ACTION_REQUIRED,
    VpStatus.COMPLETED,
    VpStatus.ENDED,
}


class VpRow(BaseModel):
    """A single Virtual Participant row (AppSync ``VirtualParticipant`` type)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    meetingName: str | None = None
    meetingPlatform: str | None = None
    meetingId: str | None = None
    meetingPassword: str | None = None
    status: str | None = None
    CallId: str | None = None
    createdAt: str | None = None
    updatedAt: str | None = None
    owner: str | None = None
    Owner: str | None = None
    SharedWith: str | None = None
    vncEndpoint: str | None = None
    vncPort: int | None = None
    vncReady: bool | None = None

    # Catch-all for any extra fields returned by the API.
    extra: dict[str, Any] = Field(default_factory=dict)


class LmaUser(BaseModel):
    """An LMA (Cognito) user a Virtual Participant can be launched for.

    ``username`` is the Cognito user name, which is what the AppSync
    resolvers compare a VP row's ``Owner`` against; ``sub`` is the
    immutable Cognito user id that keys the user's stored Zoom
    credentials and saved browser profile.
    """

    username: str
    sub: str
    email: str | None = None


class VpLaunchResult(BaseModel):
    """Result of a ``client.vp.create(...)`` call."""

    id: str
    status: str
    call_id: str | None = None
    meeting_name: str | None = None
    meeting_platform: str | None = None
    meeting_id: str | None = None
    elapsed_ms: float = 0.0
    waited_for_launch: bool = False
    sfn_execution_arn: str | None = None
    sfn_status: str | None = None
    # Set when the VP was launched for an LMA user (``as_user=``).
    owner: str | None = None
    user_sub: str | None = None
    zoom_credentials_used: bool = False

    @property
    def launched(self) -> bool:
        """True when the VP transitioned past ``INITIALIZING``."""
        try:
            return VpStatus(self.status) in LAUNCHED_VP_STATES
        except ValueError:
            return False
