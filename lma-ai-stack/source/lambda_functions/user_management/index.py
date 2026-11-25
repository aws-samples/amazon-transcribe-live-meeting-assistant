# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AppSync Lambda resolver for user management operations.

Handles the admin-only `listUsers`, `createUser`, `setUserRole` and
`deleteUser` fields, and the two participant list operations a meeting's owner
may also call, `getMeetingParticipantAccounts` and `createParticipantAccounts`
(see "Participant accounts for a meeting" below for their own checks). Cognito
is the single source of truth. Users have one of
three roles (appsync/role-matrix.json, effectiveRole):
  - Admin:       member of the "Admin" Cognito group
  - Participant: member of the "Participant" Cognito group and not of "Admin"
  - User:        in neither group

Authorization is enforced at three layers:
  1. AppSync schema `@aws_auth(cognito_groups: ["Admin"])` (primary)
  2. This handler re-verifies the caller's `cognito:groups` claim
     (defense-in-depth, in case the resolver is wired to a field without
     the directive by mistake)
  3. The UI hides the page for non-admins

Guard rails:
  - A caller cannot delete their own account or change their own role
  - Cannot delete the last remaining Admin, or move them to another role
    (lock-out protection)
  - Email must be well-formed and (optionally) in the allowed-domain list

Last sign-in: `listUsers` adds each user's last interactive sign-in time, which
the Cognito stack's PostAuthentication trigger writes to its sign-in table
(USER_SIGN_IN_TABLE, keyed by Cognito sub). A user with no item there has not
signed in since the trigger was deployed. When the table cannot be read the
times are left null and `lastSignInAvailable` is false, so the page does not
report "not recorded" for a time it could not read.

Last active: `listUsers` also adds each user's last activity, LastActiveAt in
the same table, which the Cognito stack's pre token generation trigger writes on
sign-in and token renewal and the MCP API key authorizer writes on API key use,
each at most once per 15 minutes. It is read and reported as the last sign-in is.
"""

import hashlib
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

cognito = boto3.client("cognito-idp")

USER_POOL_ID = os.environ["USER_POOL_ID"]
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")
PARTICIPANT_GROUP = os.environ.get("PARTICIPANT_GROUP", "Participant")
ALLOWED_SIGNUP_EMAIL_DOMAINS = os.environ.get("ALLOWED_SIGNUP_EMAIL_DOMAINS", "")

ADMIN_ROLE = "Admin"
USER_ROLE = "User"
PARTICIPANT_ROLE = "Participant"
VALID_ROLES = (ADMIN_ROLE, USER_ROLE, PARTICIPANT_ROLE)
# The table that holds each user's meeting notification preferences
# (PK "usr#<username>", SK "notification-prefs"), deleted with the user.
EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
# The Virtual Participant table, read for the owner and participant list of a
# Virtual Participant that has not created its meeting yet.
VIRTUAL_PARTICIPANT_TABLE = os.environ.get("VIRTUAL_PARTICIPANT_TABLE", "")
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
        "role": effective_role(groups),
    }


def _require_role(caller, roles):
    """Defense-in-depth: enforce the operation's roles in the Lambda as well as
    in the AppSync directive on its field."""
    if caller["role"] not in roles:
        logger.warning(
            "Caller '%s' (role=%s) attempted an operation for %s",
            caller["username"],
            caller["role"],
            ", ".join(roles),
        )
        if roles == (ADMIN_ROLE,):
            raise ForbiddenError("Only Admin users can perform this operation")
        raise ForbiddenError("Your role cannot perform this operation")


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


def effective_role(groups):
    """A user's role from their group names: Admin, then Participant, then User.

    The rule of appsync/role-matrix.json (effectiveRole); test_index.py holds
    this copy to its vectors. Group names compare exactly.
    """
    if ADMIN_GROUP in groups:
        return ADMIN_ROLE
    if PARTICIPANT_GROUP in groups:
        return PARTICIPANT_ROLE
    return USER_ROLE


def _group_names(username):
    """The user's Cognito group names. Raises ClientError when Cognito cannot say."""
    resp = cognito.admin_list_groups_for_user(Username=username, UserPoolId=USER_POOL_ID)
    return [g.get("GroupName") for g in resp.get("Groups", [])]


def _get_role_for_user(username):
    """A user's role for display, read from their Cognito group membership.

    A failed lookup reads as User so one throttled call degrades a listing
    rather than failing it. Decisions that depend on the role use
    _current_groups, which raises instead.
    """
    try:
        groups = _group_names(username)
    except ClientError as exc:
        logger.warning("Could not list groups for %s: %s", username, exc)
        return USER_ROLE
    return effective_role(groups)


def _current_groups(username):
    """A user's group names, for a decision that depends on them.

    Raises when they cannot be read: guessing would let the last-Admin guard
    pass for an Admin whose lookup was throttled.
    """
    try:
        return _group_names(username)
    except cognito.exceptions.UserNotFoundException as exc:
        raise ValidationError(f"User {username} not found") from exc


# The group each role is held through. A User is in neither role group.
ROLE_GROUPS = {ADMIN_ROLE: ADMIN_GROUP, PARTICIPANT_ROLE: PARTICIPANT_GROUP}


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
        "lastActiveAt": None,
    }


def _last_sign_ins(subs):
    """Map each Cognito sub in ``subs`` to its recorded times.

    Each value is a dict with "lastSignInAt" and "lastActiveAt" (either may be
    None); a sub with neither recorded is left out.

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
                    "ProjectionExpression": "UserSub, LastSignInAt, LastActiveAt",
                }
            }
            attempt = 0
            while request:
                response = dynamodb.batch_get_item(RequestItems=request)
                for item in response.get("Responses", {}).get(USER_SIGN_IN_TABLE, []):
                    times = {
                        "lastSignInAt": (item.get("LastSignInAt") or {}).get("S") or None,
                        "lastActiveAt": (item.get("LastActiveAt") or {}).get("S") or None,
                    }
                    if times["lastSignInAt"] or times["lastActiveAt"]:
                        found[item["UserSub"]["S"]] = times
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
    """Return all users in the Cognito user pool with their role, last sign-in and last activity."""
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
            user.update(sign_ins.get(sub) or {})
    # Sort newest first
    users.sort(key=lambda u: u.get("createdAt") or "", reverse=True)
    logger.info("Listed %d users", len(users))
    return {"users": users, "lastSignInAvailable": sign_ins is not None}


def create_user(_event, args):
    """Create a new user in Cognito and add them to their role's group, if it has one."""
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

    group = ROLE_GROUPS.get(role)
    if group:
        try:
            cognito.admin_add_user_to_group(
                UserPoolId=USER_POOL_ID,
                Username=email,
                GroupName=group,
            )
        except ClientError:
            # Roll back the user creation if we couldn't set the group: a user
            # created for a Participant must not be left signing in as a User.
            logger.exception("Failed to add %s to %s; rolling back", email, group)
            try:
                cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=email)
            except ClientError:
                logger.exception("Rollback delete also failed for %s", email)
            raise

    resp = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=email)
    return _format_cognito_user(resp)


def set_user_role(event, args):
    """Move a user to another role by changing their role group membership.

    The new role's group is joined before the old one is left. Because Admin
    outranks Participant, a failure between the two steps leaves the user in
    either their old role or their new one, never a third; the error is
    reported, and repeating the change completes it.
    """
    caller = _get_caller_identity(event)
    payload = args.get("input") or {}
    username = (payload.get("username") or "").strip()
    role = (payload.get("role") or "").strip()

    if not username:
        raise ValidationError("username is required")
    _validate_role(role)
    if username.casefold() == (caller["username"] or "").casefold():
        raise ValidationError("You cannot change your own role. Ask another Admin.")

    groups = set(_current_groups(username))
    current = effective_role(groups)
    if current == ADMIN_ROLE and role != ADMIN_ROLE and _count_admins() <= 1:
        raise ValidationError(
            "Cannot change the role of the last remaining Admin user. "
            "Make another user an Admin first."
        )

    wanted = ROLE_GROUPS.get(role)
    logger.info("Changing the role of %s from %s to %s", username, current, role)
    if wanted and wanted not in groups:
        cognito.admin_add_user_to_group(
            UserPoolId=USER_POOL_ID, Username=username, GroupName=wanted
        )
    for group in ROLE_GROUPS.values():
        if group != wanted and group in groups:
            cognito.admin_remove_user_from_group(
                UserPoolId=USER_POOL_ID, Username=username, GroupName=group
            )

    resp = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=username)
    return _format_cognito_user(resp)


def delete_user(event, args):
    """Delete a user from Cognito with guard rails."""
    caller = _get_caller_identity(event)
    payload = args.get("input") or {}
    username = (payload.get("username") or "").strip()

    if not username:
        raise ValidationError("username is required")

    if username.casefold() == (caller["username"] or "").casefold():
        raise ValidationError("You cannot delete your own account")

    # Prevent deleting the last Admin (lock-out protection)
    target_role = effective_role(_current_groups(username))
    if target_role == ADMIN_ROLE and _count_admins() <= 1:
        raise ValidationError(
            "Cannot delete the last remaining Admin user. Create another Admin first."
        )

    sub, email = _sub_and_email_of(username)
    logger.info("Deleting user %s (role=%s)", username, target_role)
    try:
        cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=username)
    except cognito.exceptions.UserNotFoundException as exc:
        raise ValidationError(f"User {username} not found") from exc

    _delete_notification_preferences(username)
    _delete_last_sign_in(sub)
    _withdraw_advance_consents(email, sub)
    return {"username": username, "success": True}


def _sub_and_email_of(username):
    """The user's Cognito sub and email address, read before the user is deleted.

    Each is "" when it cannot be read.
    """
    try:
        user = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=username)
    except (ClientError, BotoCoreError):
        # A missing user is reported by the delete itself.
        return "", ""
    return _attr(user, "sub") or "", (_attr(user, "email") or "").strip()


def _withdraw_advance_consents(email, sub):
    """Withdraw the consents a deleted user gave in advance. Best effort: the user is gone.

    Consents are kept under the account's email address (PK pmc#<address>, see
    advance_consent_resolver); only those the deleted account gave (its sub)
    and still in force are withdrawn, with the reason account_deleted, so no
    later meeting counts them. The records stay until they expire, as
    withdrawn ones do.
    """
    if not EVENT_SOURCING_TABLE or not email or not sub:
        return
    table = boto3.resource("dynamodb").Table(EVENT_SOURCING_TABLE)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        items = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :ac)",
            ExpressionAttributeValues={":pk": f"pmc#{email.casefold()}", ":ac": "ac#"},
        ).get("Items", [])
        for item in items:
            if item.get("WithdrawnAt") or item.get("Subject") != sub:
                continue
            try:
                table.update_item(
                    Key={"PK": item["PK"], "SK": item["SK"]},
                    UpdateExpression="SET WithdrawnAt = :w, WithdrawnReason = :r",
                    ConditionExpression="attribute_not_exists(WithdrawnAt)",
                    ExpressionAttributeValues={":w": now, ":r": "account_deleted"},
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
    except (ClientError, BotoCoreError):
        logger.warning(
            "Could not withdraw the consents given in advance by a deleted user", exc_info=True
        )


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
# Participant accounts for a meeting
# ---------------------------------------------------------------------------
#
# getMeetingParticipantAccounts and createParticipantAccounts are callable by a
# User as well as an Admin, for one meeting at a time, named by its CallId or,
# before a Virtual Participant has created it, by the Virtual Participant's id.
# Their own checks, in order:
#   1. A User must own the meeting (its Owner); an Admin may name any. A meeting
#      that does not exist is refused to a User in the same words as one they do
#      not own.
#   2. Only addresses on that meeting's participant list are looked up or
#      created; a request naming any other address is refused whole.
#   3. Only accounts that do not exist are created, and only ever in the
#      Participant group. An existing account is never changed.
#   4. The answer about an account is "none" or its role, nothing else.
#   5. A User creates at most PARTICIPANT_ACCOUNTS_PER_DAY accounts a UTC day
#      (counted in the event-sourcing table under usr#<username>); every caller
#      at most PARTICIPANT_ACCOUNTS_PER_REQUEST in one request.
#   6. AllowedSignUpEmailDomain applies as it does to createUser.
#   7. A User looks up at most PARTICIPANT_LOOKUPS_PER_DAY different addresses a
#      UTC day, across both operations (each counted once, however often it is
#      read), so a list cannot be rewritten again and again to look up any
#      address in the user pool.
# The owner is compared exactly, as the meeting preparation resolver's write
# condition and the web UI compare it.

NO_ACCOUNT = "none"
PARTICIPANT_ACCOUNTS_PER_DAY = int(os.environ.get("PARTICIPANT_ACCOUNTS_PER_DAY", "25"))
PARTICIPANT_ACCOUNTS_PER_REQUEST = 25
PARTICIPANT_LOOKUPS_PER_DAY = int(os.environ.get("PARTICIPANT_LOOKUPS_PER_DAY", "1000"))
LOOKUP_WORKERS = 8
# The daily counts are kept for two days, then the table's time to live removes them.
DAILY_COUNT_TTL_SECONDS = 2 * 86400


def _meeting_ref(args):
    """(kind, id) for the meeting a request names: exactly one of the two ids."""
    call_id = (args.get("CallId") or "").strip()
    vp_id = (args.get("VirtualParticipantId") or "").strip()
    if bool(call_id) == bool(vp_id):
        raise ValidationError(
            "Name the meeting with exactly one of CallId and VirtualParticipantId"
        )
    return ("call", call_id) if call_id else ("vp", vp_id)


def _plain_participants(value):
    """The stored participant list as [{"name", "email"}], whatever DynamoDB shape it has."""
    if not isinstance(value, dict) or "L" not in value:
        return []
    participants = []
    for entry in value["L"]:
        fields = (entry or {}).get("M") or {}
        email = (fields.get("email") or {}).get("S")
        if isinstance(email, str) and email.strip():
            participants.append(
                {"name": (fields.get("name") or {}).get("S") or "", "email": email.strip()}
            )
    return participants


def _read_meeting_for_participants(kind, ref_id):
    """The owner and participant list of the meeting, or None when there is none."""
    dynamodb = boto3.client("dynamodb")
    if kind == "call":
        if not EVENT_SOURCING_TABLE:
            raise ValidationError("Meeting records are not available")
        item = dynamodb.get_item(
            TableName=EVENT_SOURCING_TABLE,
            Key={"PK": {"S": f"c#{ref_id}"}, "SK": {"S": f"c#{ref_id}"}},
            ProjectionExpression="#o, Participants",
            ExpressionAttributeNames={"#o": "Owner"},
        ).get("Item")
        participants_attr = "Participants"
    else:
        if not VIRTUAL_PARTICIPANT_TABLE:
            raise ValidationError("Virtual Participant records are not available")
        item = dynamodb.get_item(
            TableName=VIRTUAL_PARTICIPANT_TABLE,
            Key={"id": {"S": ref_id}},
            ProjectionExpression="#o, participants, meetingName",
            ExpressionAttributeNames={"#o": "Owner"},
        ).get("Item")
        participants_attr = "participants"
    if not item:
        return None
    return {
        "owner": (item.get("Owner") or {}).get("S") or "",
        "participants": _plain_participants(item.get(participants_attr)),
        "name": (item.get("meetingName") or {}).get("S") or (ref_id if kind == "call" else ""),
    }


def _meeting_for_caller(caller, args):
    """The meeting the request names, once the caller is known to own it or to be an Admin."""
    kind, ref_id = _meeting_ref(args)
    meeting = _read_meeting_for_participants(kind, ref_id)
    if caller["is_admin"]:
        if meeting is None:
            raise ValidationError("There is no such meeting")
        return meeting
    username = caller["username"] or ""
    if meeting is None or not username or meeting["owner"] != username:
        raise ForbiddenError("only the meeting's owner or an Admin may do this")
    return meeting


def _find_account(email):
    """The username of the account with this email address, or None.

    Accounts made in LMA have the address as their username, which the user pool
    compares ignoring case; an account whose username differs is found by its
    email attribute, as given and in lower case.
    """
    try:
        user = cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=email)
        return user.get("Username") or email
    except cognito.exceptions.UserNotFoundException:
        pass
    if '"' in email or "\\" in email:
        return None
    for candidate in dict.fromkeys((email, email.lower())):
        found = cognito.list_users(
            UserPoolId=USER_POOL_ID, Filter=f'email = "{candidate}"', Limit=2
        ).get("Users", [])
        if found:
            return found[0].get("Username")
    return None


def _account_of(email):
    """ "none" or the role of the account with this address."""
    username = _find_account(email)
    if username is None:
        return NO_ACCOUNT
    return effective_role(_group_names(username))


def _accounts_of(emails):
    """Each address's account answer, looked up a few at a time."""
    with ThreadPoolExecutor(max_workers=LOOKUP_WORKERS) as pool:
        return dict(zip(emails, pool.map(_account_of, emails)))


def _address_key(email):
    """An address as the daily lookup count records it: a hash, compared ignoring case."""
    return hashlib.sha256(email.strip().casefold().encode("utf-8")).hexdigest()[:32]


def _reserve_lookups(caller, emails, now):
    """Count a User's lookups of addresses not already looked up today; refuse past the limit.

    Each distinct address is counted once a UTC day, however often the same list
    is read again, so ordinary use of one meeting does not use up the day. The
    day's item keeps a hash of each address counted, never the address itself.
    """
    if caller["is_admin"] or not emails:
        return
    dynamodb = boto3.client("dynamodb")
    key = _daily_count_key(caller["username"], now, "participant-lookups")
    item = (
        dynamodb.get_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=key,
            ConsistentRead=True,
            ProjectionExpression="Seen",
        ).get("Item")
        or {}
    )
    seen = set((item.get("Seen") or {}).get("SS") or [])
    fresh = sorted({_address_key(email) for email in emails} - seen)
    if not fresh:
        return
    room = PARTICIPANT_LOOKUPS_PER_DAY - len(fresh)
    refused = (
        f"You have looked up the accounts of {PARTICIPANT_LOOKUPS_PER_DAY} different addresses "
        "today, the most a day; an Admin can look up the rest, or try again tomorrow (UTC)"
    )
    if room < 0:
        raise ValidationError(refused)
    try:
        dynamodb.update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=key,
            UpdateExpression="ADD LookupCount :count, Seen :fresh SET ExpiresAfter = :ttl",
            ConditionExpression="attribute_not_exists(LookupCount) OR LookupCount <= :room",
            ExpressionAttributeValues={
                ":count": {"N": str(len(fresh))},
                ":fresh": {"SS": fresh},
                ":room": {"N": str(room)},
                ":ttl": {"N": str(int(now) + DAILY_COUNT_TTL_SECONDS)},
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ValidationError(refused) from exc
        raise


def get_meeting_participant_accounts(event, args):
    """Each person on the meeting's list, as the list names them, with their account answer."""
    caller = _get_caller_identity(event)
    meeting = _meeting_for_caller(caller, args)
    participants = meeting["participants"]
    _reserve_lookups(caller, [p["email"] for p in participants], time.time())
    accounts = _accounts_of([p["email"] for p in participants])
    return [
        {"name": p["name"], "email": p["email"], "account": accounts[p["email"]]}
        for p in participants
    ]


def _domain_allowed(email):
    try:
        _validate_email(email)
    except ValidationError:
        return False
    return True


def _daily_count_key(username, now, kind="participant-accounts"):
    return {
        "PK": {"S": f"usr#{username}"},
        "SK": {"S": f"{kind}#{time.strftime('%Y-%m-%d', time.gmtime(now))}"},
    }


def _reserve_daily_slot(username, now):
    """Count one more account against the caller's day; False when the day is used up."""
    try:
        boto3.client("dynamodb").update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=_daily_count_key(username, now),
            UpdateExpression="ADD CreatedCount :one SET ExpiresAfter = :ttl",
            ConditionExpression="attribute_not_exists(CreatedCount) OR CreatedCount < :limit",
            ExpressionAttributeValues={
                ":one": {"N": "1"},
                ":limit": {"N": str(PARTICIPANT_ACCOUNTS_PER_DAY)},
                ":ttl": {"N": str(int(now) + DAILY_COUNT_TTL_SECONDS)},
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def _release_daily_slot(username, now):
    """Give back a slot reserved for an account that was not created. Best effort."""
    try:
        boto3.client("dynamodb").update_item(
            TableName=EVENT_SOURCING_TABLE,
            Key=_daily_count_key(username, now),
            UpdateExpression="ADD CreatedCount :minus",
            ExpressionAttributeValues={":minus": {"N": "-1"}},
        )
    except (ClientError, BotoCoreError):
        logger.warning("Could not give back a daily participant account slot", exc_info=True)


def _remaining_today(username, now):
    try:
        item = (
            boto3.client("dynamodb")
            .get_item(
                TableName=EVENT_SOURCING_TABLE,
                Key=_daily_count_key(username, now),
                ConsistentRead=True,
            )
            .get("Item")
            or {}
        )
    except (ClientError, BotoCoreError):
        return None
    used = int((item.get("CreatedCount") or {}).get("N") or 0)
    return max(PARTICIPANT_ACCOUNTS_PER_DAY - used, 0)


def _invitation_metadata(kind, ref_id, meeting):
    """The client metadata that has the invitation name the meeting and link its consent page.

    Read by the user pool's CustomMessage trigger
    (lma-cognito-stack/source/custom_message), which builds the link from the
    web UI's address and this query. Cognito passes client metadata to the
    trigger only; it is not stored.
    """
    return {
        "lmaInvitation": "meeting_participant",
        "meetingName": (meeting.get("name") or "")[:200],
        "consentQuery": f"{'meeting' if kind == 'call' else 'vp'}={quote(ref_id, safe='')}",
    }


def _create_participant_account(email, client_metadata=None):
    """Create the account in the Participant group; "created" or "exists".

    The account is created with its invitation suppressed, added to the
    Participant group, and only then sent its invitation (a resend of the
    message the suppressed create would have sent). Until the invitation is
    sent nobody holds its temporary password, and an account in that state
    cannot reset one, so no account outside the Participant group is ever
    usable. A failure after the create removes the account again.
    """
    try:
        cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )
    except cognito.exceptions.UsernameExistsException:
        return "exists"
    try:
        cognito.admin_add_user_to_group(
            UserPoolId=USER_POOL_ID, Username=email, GroupName=PARTICIPANT_GROUP
        )
        cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=email,
            MessageAction="RESEND",
            DesiredDeliveryMediums=["EMAIL"],
            # The invitation names the meeting and links to its consent page.
            **({"ClientMetadata": client_metadata} if client_metadata else {}),
        )
    except ClientError:
        logger.exception("Could not finish a new participant account; removing it")
        try:
            cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=email)
        except ClientError:
            logger.exception("Removing the new participant account also failed")
        raise
    return "created"


def create_participant_accounts(event, args):
    """Participant accounts for the listed people without one (see the rules above)."""
    caller = _get_caller_identity(event)
    payload = args.get("input") or {}
    meeting = _meeting_for_caller(caller, payload)
    invitation = _invitation_metadata(*_meeting_ref(payload), meeting)
    listed = {p["email"].casefold(): p["email"] for p in meeting["participants"]}

    requested = payload.get("emails")
    if requested is None:
        wanted = list(listed.values())
    else:
        if not isinstance(requested, list):
            raise ValidationError("emails must be a list of addresses")
        not_listed = [
            e for e in requested if not isinstance(e, str) or e.strip().casefold() not in listed
        ]
        if not_listed:
            raise ValidationError(
                "Accounts can be created only for people on the meeting's participant list"
            )
        wanted = list(dict.fromkeys(listed[e.strip().casefold()] for e in requested))

    now = time.time()
    _reserve_lookups(caller, wanted, now)
    accounts = _accounts_of(wanted)
    limited = not caller["is_admin"]
    results = []
    attempts = 0
    for email in wanted:
        if accounts[email] != NO_ACCOUNT:
            results.append({"email": email, "outcome": "exists", "message": None})
            continue
        if not _domain_allowed(email):
            results.append(
                {
                    "email": email,
                    "outcome": "domain_not_allowed",
                    "message": "This deployment creates accounts only for its allowed email domains",
                }
            )
            continue
        if attempts >= PARTICIPANT_ACCOUNTS_PER_REQUEST:
            results.append(
                {
                    "email": email,
                    "outcome": "limit_reached",
                    "message": f"At most {PARTICIPANT_ACCOUNTS_PER_REQUEST} accounts are created "
                    "at a time; run it again for the rest",
                }
            )
            continue
        if limited and not _reserve_daily_slot(caller["username"], now):
            results.append(
                {
                    "email": email,
                    "outcome": "limit_reached",
                    "message": f"You can create at most {PARTICIPANT_ACCOUNTS_PER_DAY} participant "
                    "accounts a day",
                }
            )
            continue
        attempts += 1
        try:
            outcome = _create_participant_account(email, invitation)
        except ClientError as exc:
            logger.warning("Could not create a participant account: %s", exc)
            outcome = "failed"
        if outcome != "created" and limited:
            _release_daily_slot(caller["username"], now)
        results.append(
            {
                "email": email,
                "outcome": outcome,
                "message": "The account could not be created" if outcome == "failed" else None,
            }
        )

    created = sum(1 for r in results if r["outcome"] == "created")
    logger.info(
        "%s created %d participant accounts (%d asked for)",
        caller["username"],
        created,
        len(wanted),
    )
    return {
        "results": results,
        "created": created,
        "remainingToday": _remaining_today(caller["username"], now) if limited else None,
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


# Each operation and the roles that may call it, as its field's directive in
# schema.graphql and the user-management capability of appsync/role-matrix.json
# name them. An operation for a narrower purpose and a wider role list (such as a
# meeting owner adding Participants for their own meeting) is a new entry with
# its own checks, never a widening of these.
OPERATIONS = {
    "listUsers": (list_users, (ADMIN_ROLE,)),
    "createUser": (create_user, (ADMIN_ROLE,)),
    "setUserRole": (set_user_role, (ADMIN_ROLE,)),
    "deleteUser": (delete_user, (ADMIN_ROLE,)),
    "getMeetingParticipantAccounts": (get_meeting_participant_accounts, (ADMIN_ROLE, USER_ROLE)),
    "createParticipantAccounts": (create_participant_accounts, (ADMIN_ROLE, USER_ROLE)),
}


def handler(event, _context):
    """AppSync direct-Lambda resolver entry point."""
    logger.info("User management event: %s", {k: event.get(k) for k in ("info",)})

    field = (event.get("info") or {}).get("fieldName", "")
    entry = OPERATIONS.get(field)
    if entry is None:
        raise ValueError(f"Unknown operation: {field}")
    op, roles = entry

    caller = _get_caller_identity(event)
    try:
        _require_role(caller, roles)
    except ForbiddenError as exc:
        # Return as an AppSync error so the client sees a 401-style message
        raise Exception(f"Unauthorized: {exc}") from exc

    try:
        return op(event, event.get("arguments") or {})
    except ForbiddenError as exc:
        # An operation's own check (a meeting the caller does not own).
        raise Exception(f"Unauthorized: {exc}") from exc
    except ValidationError as exc:
        # Surface a clean error message to the caller
        raise Exception(str(exc)) from exc
