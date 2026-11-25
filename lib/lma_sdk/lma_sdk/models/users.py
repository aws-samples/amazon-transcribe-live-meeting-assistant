# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""User models: the users of a deployment, for a user access review."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class UserRecord(BaseModel):
    """One user of the deployment's Cognito user pool."""

    username: str
    email: str = ""
    #: ``Admin`` for a member of the Admin group, else ``User``.
    role: str
    #: Cognito's user status, for example ``CONFIRMED`` or ``FORCE_CHANGE_PASSWORD``.
    status: str = ""
    enabled: bool = True
    #: When the user was created (ISO 8601, UTC).
    created_at: Optional[str] = None
    #: The user's last interactive sign-in (ISO 8601, UTC), or None when none is
    #: recorded or the times could not be read (see ``UserList.last_sign_in_available``).
    last_sign_in_at: Optional[str] = None
    #: The user's last activity (ISO 8601, UTC): the latest sign-in or token renewal,
    #: or use of their MCP API key, each recorded at most once per 15 minutes. None
    #: as for ``last_sign_in_at``.
    last_active_at: Optional[str] = None


class UserList(BaseModel):
    """What ``client.users.list`` found."""

    #: The stack, Region, AWS account and user pool the users were read from.
    stack: str = ""
    region: str = ""
    account: str = ""
    user_pool_id: str = ""
    #: When the list was read (ISO 8601, UTC).
    generated_at: str = ""
    #: False when the last sign-in and last active times could not be read, so every
    #: ``last_sign_in_at`` and ``last_active_at`` is None for that reason and not
    #: because none is recorded.
    last_sign_in_available: bool = True
    #: Why the times could not be read, when they could not.
    last_sign_in_unavailable_reason: str = ""
    users: list[UserRecord] = Field(default_factory=list)
