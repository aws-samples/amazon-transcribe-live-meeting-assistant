# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Who may change a Virtual Participant, and the meeting it captures.

Shared by the end, delete and rejoin operations of this Lambda (index.py and
rejoin.py).
"""

from typing import Any, Dict, List, Optional


class NotAuthorizedError(Exception):
    """The caller may not change this Virtual Participant."""


def caller_groups(identity: Optional[Dict[str, Any]]) -> List[str]:
    """The caller's Cognito groups, from either place AppSync puts them."""
    caller: Dict[str, Any] = identity or {}
    groups = caller.get("groups") or (caller.get("claims") or {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [group.strip() for group in groups.split(",") if group.strip()]
    return list(groups)


def caller_may_change(identity: Optional[Dict[str, Any]], vp: Optional[Dict[str, Any]]) -> bool:
    """The "Virtual Participant owner condition" of the AppSync templates.

    A signed-in caller who is not an Admin may change a Virtual Participant only
    as its Owner. Admins, and backend roles with IAM credentials (the Virtual
    Participant task and the Lambdas that launch and manage it), may change any.
    A backend role is detected as the templates detect it: by an assumed-role ARN.
    """
    caller: Dict[str, Any] = identity or {}
    user_arn = caller.get("userArn") or ""
    if ":assumed-role/" in user_arn:
        return True
    if "Admin" in caller_groups(caller):
        return True
    username = caller.get("username")
    return bool(username) and vp is not None and vp.get("Owner") == username


def caller_owns_meeting(
    identity: Optional[Dict[str, Any]], meeting: Optional[Dict[str, Any]]
) -> bool:
    """Whether the caller is an Admin or the meeting's Owner.

    The rule for reopening a meeting. Narrower than reading one: a user the
    meeting is shared with may read it but not reopen it.
    """
    caller: Dict[str, Any] = identity or {}
    if "Admin" in caller_groups(caller):
        return True
    username = caller.get("username")
    return bool(username) and meeting is not None and meeting.get("Owner") == username
