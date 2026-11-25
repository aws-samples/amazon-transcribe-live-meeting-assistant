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

Because it runs on every sign-in and every token renewal (about hourly while a
client is open), the trigger also records when the user was last active: it
sets LastActiveAt on the user's item in the Cognito stack's sign-in table
(USER_SIGN_IN_TABLE, keyed by Cognito sub), at most once per 15 minutes per user.
The write is one bounded attempt made after the response is built, and never
changes it: any failure is logged and the token is issued as it would be without
it.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ADMIN = "Admin"
PARTICIPANT = "Participant"
USER = "User"
# Highest precedence first; a user in none of these is a User.
ROLE_PRECEDENCE = (ADMIN, PARTICIPANT)
ROLE_GROUPS = frozenset((ADMIN, PARTICIPANT, USER))
ROLE_CLAIM = "lma_role"

TABLE = os.environ.get("USER_SIGN_IN_TABLE", "")
# A user's last-active time is written at most once in this many seconds.
ACTIVE_INTERVAL_SECONDS = 15 * 60
# One attempt of at most 1.5 seconds, so the trigger answers well inside the five
# seconds Cognito waits even on a cold start. A missed write is only logged.
DYNAMODB_CONFIG = Config(
    connect_timeout=0.5,
    read_timeout=1,
    retries={"mode": "standard", "total_max_attempts": 1},
)
# Tokens are issued about hourly per open client, so every issue makes the one
# conditional write; the condition, not a cache here, keeps it to one per interval.
_dynamodb: Any = None


def _client() -> Any:
    global _dynamodb  # pylint: disable=global-statement
    if _dynamodb is None:
        _dynamodb = boto3.client("dynamodb", config=DYNAMODB_CONFIG)
    return _dynamodb


def iso_utc(moment: datetime) -> str:
    """A time as the sign-in table holds it (UTC, in AppSync's AWSDateTime form)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def record_last_active(event: Dict[str, Any], now: Optional[datetime] = None) -> None:
    """Set the user's LastActiveAt unless it was set within the interval. Never raises."""
    try:
        attributes = (event.get("request") or {}).get("userAttributes") or {}
        sub = str(attributes.get("sub") or "")
        if not TABLE or not sub:
            return
        now = now or datetime.now(timezone.utc)
        at = iso_utc(now)
        cutoff = iso_utc(now - timedelta(seconds=ACTIVE_INTERVAL_SECONDS))
        try:
            _client().update_item(
                TableName=TABLE,
                Key={"UserSub": {"S": sub}},
                UpdateExpression="SET LastActiveAt = :at",
                # At most one write per interval, and never a time moved backwards.
                ConditionExpression="attribute_not_exists(LastActiveAt) OR LastActiveAt < :cutoff",
                ExpressionAttributeValues={":at": {"S": at}, ":cutoff": {"S": cutoff}},
            )
            logger.info("Recorded the last activity of sub %s at %s", sub, at)
        except ClientError as error:
            # Recorded within the interval already.
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
    except Exception:  # pylint: disable=broad-except
        logger.exception("Could not record the last activity of a user")


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
    record_last_active(event)
    return event
