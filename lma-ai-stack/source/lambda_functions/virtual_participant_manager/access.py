# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Who may change a Virtual Participant, and the meeting it captures.

Shared by the end, delete and rejoin operations of this Lambda (index.py and
rejoin.py).
"""

from typing import Any, Dict, Optional

# Who may reopen a meeting is shared with the meeting reopen resolver
# (lma_sessions, in the configuration layer).
from lma_sessions import caller_groups, caller_owns_meeting  # noqa: F401 - re-exported


class NotAuthorizedError(Exception):
    """The caller may not change this Virtual Participant."""


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
