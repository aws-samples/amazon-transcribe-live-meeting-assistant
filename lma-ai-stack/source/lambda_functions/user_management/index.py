# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AppSync Lambda resolver for user management operations.

Handles the admin-only `listUsers`, `createUser`, and `deleteUser` fields.
Cognito is the single source of truth. Users have one of two roles:
  - Admin: member of the "Admin" Cognito group
  - User:  authenticated but not in "Admin"

Authorization is enforced at three layers:
  1. AppSync schema `@aws_auth(cognito_groups: ["Admin"])` (primary)
  2. This handler re-verifies the caller's `cognito:groups` claim
     (defense-in-depth, in case the resolver is wired to a field without
     the directive by mistake)
  3. The UI hides the page for non-admins

Guard rails:
  - A caller cannot delete their own account
  - Cannot delete the last remaining Admin (lock-out protection)
  - Email must be well-formed and (optionally) in the allowed-domain list

Last sign-in: `listUsers` adds each user's last interactive sign-in time, which
the Cognito stack's PostAuthentication trigger writes to its sign-in table
(USER_SIGN_IN_TABLE, keyed by Cognito sub). A user with no item there has not
signed in since the trigger was deployed. When the table cannot be read the
times are left null and `lastSignInAvailable` is false, so the page does not
report "not recorded" for a time it could not read.
"""

import logging
import os
import re
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

cognito = boto3.client("cognito-idp")

USER_POOL_ID = os.environ["USER_POOL_ID"]
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
ALLOWED_SIGNUP_EMAIL_DOMAINS = os.environ.get("ALLOWED_SIGNUP_EMAIL_DOMAINS", "")

VALID_ROLES = ("Admin", "User")
# The table that holds each user's meeting notification preferences
# (PK "usr#<username>", SK "notification-prefs"), deleted with the user.
EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
# The Cognito stack's table of last sign-in times (key "UserSub"); empty when the
# Cognito stack has none.
USER_SIGN_IN_TABLE = os.environ.get("USER_SIGN_IN_TABLE", "")
# BatchGetItem's limit on keys per request, and how often keys it leaves
# unprocessed (throttling) are retried.
BATCH_GET_MAX = 100
UNPROCESSED_RETRIES = 5
EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class ForbiddenError(Exception):
    """Raised when the caller is not authorized."""


class ValidationError(Exception):
    """Raised when input validation fails."""


def _get_caller_identity(event):
    """Extract caller's Cognito groups / username from AppSync event identity."""
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    username = claims.get("cognito:username") or identity.get("username") or claims.get("sub") or ""
    return {
        "username": username,
        "groups": groups,
        "is_admin": ADMIN_GROUP in groups,
    }


def _require_admin(caller):
    """Defense-in-depth: enforce admin role in the Lambda even if the AppSync
    @aws_auth directive is already in place."""
    if not caller["is_admin"]:
        logger.warning(
            "Non-admin caller '%s' attempted admin-only operation (groups=%s)",
            caller["username"],
            caller["groups"],
        )
        raise ForbiddenError("Only Admin users can perform this operation")


def _validate_email(email):
    if not email or not EMAIL_PATTERN.match(email):
        raise ValidationError(f"Invalid email format: {email!r}")

    if ALLOWED_SIGNUP_EMAIL_DOMAINS.strip():
        allowed = [d.strip().lower() for d in ALLOWED_SIGNUP_EMAIL_DOMAINS.split(",") if d.strip()]
        if allowed:
            domain = email.split("@", 1)[1].lower()
            if domain not in allowed:
                raise ValidationError(
                    f"Email domain '{domain}' is not allowed. Allowed domains: {', '.join(allowed)}"
                )


def _validate_role(role):
    if role not in VALID_ROLES:
        raise ValidationError(f"Invalid role '{role}'. Must be one of: {', '.join(VALID_ROLES)}")


def _get_role_for_user(username):
    """Look up a user's role by inspecting their Cognito group membership."""
    try:
        resp = cognito.admin_list_groups_for_user(Username=username, UserPoolId=USER_POOL_ID)
    except ClientError as exc:
        logger.warning("Could not list groups for %s: %s", username, exc)
        return "User"
    groups = [g.get("GroupName") for g in resp.get("Groups", [])]
    return "Admin" if ADMIN_GROUP in groups else "User"


def _attr(user, name, default=None):
    for a in user.get("Attributes") or user.get("UserAttributes") or []:
        if a.get("Name") == name:
            return a.get("Value")
    return default


def _format_cognito_user(user):
    """Shape a Cognito user dict into the GraphQL `User` type."""
    username = user.get("Username")
    email = _attr(user, "email") or username
    created = user.get("UserCreateDate")
    created_iso = None
    if created is not None:
        # AppSync AWSDateTime must include a timezone designator
        created_iso = created.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "username": username,
        "email": email,
        "role": _get_role_for_user(username),
        "status": user.get("UserStatus"),
        "enabled": user.get("Enabled", True),
        "createdAt": created_iso,
        "lastSignInAt": None,
    }


def _last_sign_ins(subs):
    """Map each Cognito sub in ``subs`` to its recorded last sign-in time.

    Returns None when the times cannot be read (no table configured, or
    DynamoDB refused), so a caller can tell "not recorded" from "unknown".
    """
    if not USER_SIGN_IN_TABLE:
        return None
    found = {}
    dynamodb = boto3.client("dynamodb")
    unique = sorted({sub for sub in subs if sub})
    try:
        for start in range(0, len(unique), BATCH_GET_MAX):
            chunk = unique[start : start + BATCH_GET_MAX]
            request = {
                USER_SIGN_IN_TABLE: {
                    "Keys": [{"UserSub": {"S": sub}} for sub in chunk],
                    "ProjectionExpression": "UserSub, LastSignInAt",
                }
            }
            attempt = 0
            while request:
                response = dynamodb.batch_get_item(RequestItems=request)
                for item in response.get("Responses", {}).get(USER_SIGN_IN_TABLE, []):
                    at = (item.get("LastSignInAt") or {}).get("S")
                    if at:
                        found[item["UserSub"]["S"]] = at
                request = response.get("UnprocessedKeys") or {}
                if request:
                    attempt += 1
                    if attempt > UNPROCESSED_RETRIES:
                        logger.warning("The sign-in table kept leaving keys unprocessed")
                        return None
                    time.sleep(min(0.05 * 2**attempt, 1.0))
    except (ClientError, BotoCoreError):
        logger.warning("Could not read the last sign-in times", exc_info=True)
        return None
    return found


def _count_admins():
    count = 0
    paginator = cognito.get_paginator("list_users_in_group")
    for page in paginator.paginate(UserPoolId=USER_POOL_ID, GroupName=ADMIN_GROUP):
        count += len(page.get("Users", []))
    return count


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def list_users(_event, _args):
    """Return all users in the Cognito user pool with their role and last sign-in."""
    users = []
    subs = []
    paginator = cognito.get_paginator("list_users")
    for page in paginator.paginate(UserPoolId=USER_POOL_ID, Limit=60):
        for user in page.get("Users", []):
            users.append(_format_cognito_user(user))
            subs.append(_attr(user, "sub"))
    sign_ins = _last_sign_ins(subs)
    if sign_ins is not None:
        for user, sub in zip(users, subs):
            user["lastSignInAt"] = sign_ins.get(sub)
    # Sort newest first
    users.sort(key=lambda u: u.get("createdAt") or "", reverse=True)
    logger.info("Listed %d users", len(users))
    return {"users": users, "lastSignInAvailable": sign_ins is not None}


def create_user(_event, args):
    """Create a new user in Cognito and (if Admin) add them to the Admin group."""
    payload = args.get("input") or {}
    email = (payload.get("email") or "").strip()
    role = (payload.get("role") or "").strip()

    _validate_email(email)
    _validate_role(role)

    logger.info("Creating user email=%s role=%s", email, role)

    try:
        cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
    except cognito.exceptions.UsernameExistsException as exc:
        raise ValidationError(f"User with email {email} already exists") from exc

    if role == "Admin":
        try:
            cognito.admin_add_user_to_group(
                UserPoolId=USER_POOL_ID,
                Username=email,
                GroupName=ADMIN_GROUP,
            )
        except ClientError:
            # Roll back the user creation if we couldn't set the group
            logger.exception("Failed to add %s to %s; rolling back", email, ADMIN_GROUP)
            try:
                cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=email)
            except ClientError:
                logger.exception("Rollback delete also failed for %s", email)
            raise

    resp = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=email)
    return _format_cognito_user(resp)


def delete_user(event, args):
    """Delete a user from Cognito with guard rails."""
    caller = _get_caller_identity(event)
    payload = args.get("input") or {}
    username = (payload.get("username") or "").strip()

    if not username:
        raise ValidationError("username is required")

    if username == caller["username"]:
        raise ValidationError("You cannot delete your own account")

    # Prevent deleting the last Admin (lock-out protection)
    target_role = _get_role_for_user(username)
    if target_role == "Admin" and _count_admins() <= 1:
        raise ValidationError(
            "Cannot delete the last remaining Admin user. Create another Admin first."
        )

    sub = _sub_of(username)
    logger.info("Deleting user %s (role=%s)", username, target_role)
    try:
        cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=username)
    except cognito.exceptions.UserNotFoundException as exc:
        raise ValidationError(f"User {username} not found") from exc

    _delete_notification_preferences(username)
    _delete_last_sign_in(sub)
    return {"username": username, "success": True}


def _sub_of(username):
    """The user's Cognito sub, read before the user is deleted; "" if it cannot be read."""
    if not USER_SIGN_IN_TABLE:
        return ""
    try:
        user = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=username)
    except ClientError:
        # A missing user is reported by the delete itself.
        return ""
    return _attr(user, "sub") or ""


def _delete_last_sign_in(sub):
    """Delete the user's last sign-in time. Best effort: the user is gone."""
    if not USER_SIGN_IN_TABLE or not sub:
        return
    try:
        boto3.client("dynamodb").delete_item(
            TableName=USER_SIGN_IN_TABLE, Key={"UserSub": {"S": sub}}
        )
    except (ClientError, BotoCoreError):
        logger.warning("Could not delete the last sign-in time of %s", sub, exc_info=True)


def _delete_notification_preferences(username):
    """Delete the user's categories muted on every meeting. Best effort: the user is gone."""
    if not EVENT_SOURCING_TABLE:
        return
    try:
        boto3.resource("dynamodb").Table(EVENT_SOURCING_TABLE).delete_item(
            Key={"PK": f"usr#{username}", "SK": "notification-prefs"}
        )
    except ClientError:
        logger.warning(
            "Could not delete the notification preferences of %s", username, exc_info=True
        )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


OPERATIONS = {
    "listUsers": list_users,
    "createUser": create_user,
    "deleteUser": delete_user,
}


def handler(event, _context):
    """AppSync direct-Lambda resolver entry point."""
    logger.info("User management event: %s", {k: event.get(k) for k in ("info",)})

    field = (event.get("info") or {}).get("fieldName", "")
    op = OPERATIONS.get(field)
    if op is None:
        raise ValueError(f"Unknown operation: {field}")

    caller = _get_caller_identity(event)
    # All current operations are Admin-only
    try:
        _require_admin(caller)
    except ForbiddenError as exc:
        # Return as an AppSync error so the client sees a 401-style message
        raise Exception(f"Unauthorized: {exc}") from exc

    try:
        return op(event, event.get("arguments") or {})
    except ValidationError as exc:
        # Surface a clean error message to the caller
        raise Exception(str(exc)) from exc
