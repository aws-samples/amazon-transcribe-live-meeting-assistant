#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Cognito pre token generation trigger (event version 1): every token names one role.

LMA has three roles (lma-ai-stack/source/appsync/role-matrix.json):

* Admin -- a member of the Admin group;
* Participant -- a member of the Participant group and not of Admin;
* User -- every other user, including one in no group at all.

AppSync admits a caller to a field when the token's cognito:groups claim holds
one of the groups the field names, and has no way to name "in no group". So
this trigger writes the caller's role into the claim: the role groups the user
is in (Admin, Participant, or a group called User) are replaced by the one role
the precedence above gives, and any other group is kept. A user in no group
therefore carries "User", and a field can name the roles it admits, User
included. Version 1 group overrides apply to the ID token and the access token
alike.

The ID token also gets an lma_role claim holding the same role, which the
identity pool's rule-based role mapping reads, as it matches on single-valued
claims.

Cognito invokes this for every token it issues to a user: managed login,
SRP and other API sign-in, the new-password challenge, device authentication and
token refresh. A group change therefore reaches a user's tokens at their next
refresh at the latest.
"""

from typing import Any, Dict, List

ADMIN = "Admin"
PARTICIPANT = "Participant"
USER = "User"
# Highest precedence first; a user in none of these is a User.
ROLE_PRECEDENCE = (ADMIN, PARTICIPANT)
ROLE_GROUPS = frozenset((ADMIN, PARTICIPANT, USER))
ROLE_CLAIM = "lma_role"


def effective_role(groups: List[str]) -> str:
    """The caller's role from their group names; names compare exactly."""
    for role in ROLE_PRECEDENCE:
        if role in groups:
            return role
    return USER


def token_groups(groups: List[str]) -> List[str]:
    """The groups a token carries: the non-role groups, in order, then the one role."""
    return [group for group in groups if group not in ROLE_GROUPS] + [effective_role(groups)]


def lambda_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    """Name the caller's role in cognito:groups and in the ID token's lma_role claim."""
    configuration = (event.get("request") or {}).get("groupConfiguration") or {}
    groups = [str(group) for group in configuration.get("groupsToOverride") or []]

    override: Dict[str, Any] = {"groupsToOverride": token_groups(groups)}
    # The override replaces the whole group configuration, so the group IAM
    # roles Cognito would otherwise have put in the ID token are passed through.
    if configuration.get("iamRolesToOverride"):
        override["iamRolesToOverride"] = configuration["iamRolesToOverride"]
    if configuration.get("preferredRole"):
        override["preferredRole"] = configuration["preferredRole"]

    event["response"] = {
        "claimsOverrideDetails": {
            "claimsToAddOrOverride": {ROLE_CLAIM: effective_role(groups)},
            "groupOverrideDetails": override,
        }
    }
    return event
