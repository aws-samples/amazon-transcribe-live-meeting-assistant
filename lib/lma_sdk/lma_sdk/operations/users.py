# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Users: list a deployment's users with their role and last sign-in, for a user
access review.

Available as ``client.users``::

    review = client.users.list()
    for user in review.users:
        print(user.email, user.role, user.status, user.enabled, user.last_sign_in_at,
              user.last_active_at)

Each user is read from the stack's Cognito user pool; the role is ``Admin`` for a
member of the Admin group, otherwise ``Participant`` for a member of the Participant
group, and ``User`` otherwise. The last sign-in time is the one
the Cognito stack's PostAuthentication trigger records in its sign-in table,
``UserSignInTable``, keyed by Cognito sub: the last interactive sign-in. Renewing
tokens with a refresh token does not run the trigger, so it is not a "last used"
time. A user with no item has not signed in since the trigger was deployed
(``last_sign_in_at`` is None). When the table cannot be read, for example on a
stack deployed before it existed or for want of a permission, every
``last_sign_in_at`` is None, ``last_sign_in_available`` is False and
``last_sign_in_unavailable_reason`` says why; the users are still listed.

The last active time is ``LastActiveAt`` in the same table: the Cognito stack's
pre token generation trigger sets it on every sign-in and token renewal (about
hourly while a client is open), and the MCP API key authorizer on each use of the
user's API key, each at most once per 15 minutes. It is None, and unavailable, in
the same cases as the last sign-in.

Every call uses the caller's AWS credentials: ``cloudformation:DescribeStacks``
and ``cloudformation:DescribeStackResource`` on the stack and its Cognito stack,
``cognito-idp:ListUsers`` and ``cognito-idp:ListUsersInGroup`` on the user pool,
and ``dynamodb:BatchGetItem`` on the sign-in table (with ``kms:Decrypt`` on the
stack's key through DynamoDB, where the key policy leaves that to IAM).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional

from lma_sdk._core.aws_errors import AWS_ERRORS, aws_call_error, error_code
from lma_sdk.models.users import UserList, UserRecord

if TYPE_CHECKING:
    from lma_sdk.client import LMAClient

#: The Cognito group whose members are Admins.
ADMIN_GROUP = "Admin"
#: The Cognito group whose members are Participants, unless they are also Admins.
PARTICIPANT_GROUP = "Participant"
#: The Cognito stack's logical id in the main stack, and the sign-in table's in it.
COGNITO_STACK_LOGICAL_ID = "COGNITOSTACK"
SIGN_IN_TABLE_LOGICAL_ID = "UserSignInTable"
#: BatchGetItem's limit on keys per request.
BATCH_GET_MAX = 100
#: How many times keys BatchGetItem leaves unprocessed are retried, with backoff.
UNPROCESSED_RETRIES = 8

_SEE = "see the permissions table of `lma users list` in docs/lma-cli.md"


def iso_utc(value: Any) -> Optional[str]:
    """A Cognito timestamp as ISO 8601 in UTC (``2026-11-20T09:30:00.000Z``), or None."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _attribute(user: Dict[str, Any], name: str) -> str:
    for attribute in user.get("Attributes") or []:
        if attribute.get("Name") == name:
            return str(attribute.get("Value") or "")
    return ""


def _role(username: str, admins: set, participants: set) -> str:
    """Admin, then Participant, then User: the rule of the role matrix's effectiveRole."""
    if username in admins:
        return "Admin"
    if username in participants:
        return "Participant"
    return "User"


class SignInTimesUnavailable(Exception):
    """The last sign-in times could not be read; the message says why."""


class UserOperations:
    """List the deployment's users for an access review, as ``client.users``."""

    def __init__(self, client: LMAClient) -> None:
        self._client = client
        self._sign_in_table: Optional[str] = None

    # ── where things are ──────────────────────────────────────────────────

    def sign_in_table(self) -> str:
        """The Cognito stack's sign-in table, found through the stack's resources.

        Raises:
            SignInTimesUnavailable: the stack has no such table (deployed before it
                existed), or its resources could not be read.
        """
        if self._sign_in_table:
            return self._sign_in_table
        cfn = self._client.session.client("cloudformation")
        stack = self._client._require_stack()  # noqa: SLF001 -- the client's own helper
        try:
            cognito_stack = cfn.describe_stack_resource(
                StackName=stack, LogicalResourceId=COGNITO_STACK_LOGICAL_ID
            )["StackResourceDetail"]["PhysicalResourceId"]
            table = cfn.describe_stack_resource(
                StackName=cognito_stack, LogicalResourceId=SIGN_IN_TABLE_LOGICAL_ID
            )["StackResourceDetail"]["PhysicalResourceId"]
        except AWS_ERRORS as err:
            if (
                error_code(err) == "ValidationError"
                and "does not exist" in str(err)
                and SIGN_IN_TABLE_LOGICAL_ID in str(err)
            ):
                raise SignInTimesUnavailable(
                    f"stack {stack} records no sign-in times (its Cognito stack has no "
                    f"{SIGN_IN_TABLE_LOGICAL_ID}; update the stack to record them)"
                ) from err
            raise SignInTimesUnavailable(
                str(
                    aws_call_error(
                        "cloudformation:DescribeStackResource",
                        f"find the sign-in table of stack {stack}",
                        err,
                        see=_SEE,
                    )
                )
            ) from err
        self._sign_in_table = table
        return table

    # ── reading ───────────────────────────────────────────────────────────

    def _cognito_users(self, pool_id: str) -> List[Dict[str, Any]]:
        cognito = self._client.session.client("cognito-idp")
        users: List[Dict[str, Any]] = []
        token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"UserPoolId": pool_id, "Limit": 60}
            if token:
                kwargs["PaginationToken"] = token
            try:
                response = cognito.list_users(**kwargs)
            except AWS_ERRORS as err:
                raise aws_call_error(
                    "cognito-idp:ListUsers", f"list the users of user pool {pool_id}", err, see=_SEE
                ) from err
            users += response.get("Users") or []
            token = response.get("PaginationToken")
            if not token:
                return users

    def _group_members(self, pool_id: str, group: str) -> set[str]:
        cognito = self._client.session.client("cognito-idp")
        admins: set[str] = set()
        token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"UserPoolId": pool_id, "GroupName": group}
            if token:
                kwargs["NextToken"] = token
            try:
                response = cognito.list_users_in_group(**kwargs)
            except AWS_ERRORS as err:
                raise aws_call_error(
                    "cognito-idp:ListUsersInGroup",
                    f"list the {group} group of user pool {pool_id}",
                    err,
                    see=_SEE,
                ) from err
            admins.update(str(u.get("Username")) for u in response.get("Users") or [])
            token = response.get("NextToken")
            if not token:
                return admins

    def last_sign_ins(self, subs: Iterable[str]) -> Dict[str, str]:
        """The recorded last sign-in time of each Cognito sub in ``subs`` that has one.

        Raises:
            SignInTimesUnavailable: the table could not be found or read.
        """
        return {
            sub: times["last_sign_in_at"]
            for sub, times in self.sign_in_times(subs).items()
            if times.get("last_sign_in_at")
        }

    def sign_in_times(self, subs: Iterable[str]) -> Dict[str, Dict[str, Optional[str]]]:
        """The recorded times of each Cognito sub in ``subs`` that has any.

        Each value has ``last_sign_in_at`` and ``last_active_at``, either of which
        may be None.

        Raises:
            SignInTimesUnavailable: the table could not be found or read.
        """
        table = self.sign_in_table()
        dynamodb = self._client.session.client("dynamodb")
        unique = sorted({sub for sub in subs if sub})
        found: Dict[str, Dict[str, Optional[str]]] = {}
        for start in range(0, len(unique), BATCH_GET_MAX):
            request: Dict[str, Any] = {
                table: {
                    "Keys": [{"UserSub": {"S": s}} for s in unique[start : start + BATCH_GET_MAX]],
                    "ProjectionExpression": "UserSub, LastSignInAt, LastActiveAt",
                }
            }
            attempt = 0
            while request:
                try:
                    response = dynamodb.batch_get_item(RequestItems=request)
                except AWS_ERRORS as err:
                    raise SignInTimesUnavailable(
                        str(
                            aws_call_error(
                                "dynamodb:BatchGetItem",
                                f"read the last sign-in times in {table}",
                                err,
                                see=_SEE,
                            )
                        )
                    ) from err
                for item in response.get("Responses", {}).get(table, []):
                    times = {
                        "last_sign_in_at": (item.get("LastSignInAt") or {}).get("S") or None,
                        "last_active_at": (item.get("LastActiveAt") or {}).get("S") or None,
                    }
                    if times["last_sign_in_at"] or times["last_active_at"]:
                        found[item["UserSub"]["S"]] = times
                request = response.get("UnprocessedKeys") or {}
                if request:
                    attempt += 1
                    if attempt > UNPROCESSED_RETRIES:
                        raise SignInTimesUnavailable(
                            f"{table} kept refusing reads; try again later"
                        )
                    time.sleep(min(0.1 * 2**attempt, 5.0))
        return found

    def list(self) -> UserList:
        """Every user of the deployment, by email, with role, status, last sign-in and last active."""
        stack = self._client._require_stack()  # noqa: SLF001
        pool_id = self._client.vp.user_pool_id
        cognito_users = self._cognito_users(pool_id)
        admins = self._group_members(pool_id, ADMIN_GROUP)
        participants = self._group_members(pool_id, PARTICIPANT_GROUP)
        subs = [_attribute(user, "sub") for user in cognito_users]

        available, reason = True, ""
        times: Dict[str, Dict[str, Optional[str]]] = {}
        try:
            times = self.sign_in_times(subs)
        except SignInTimesUnavailable as err:
            available, reason = False, str(err)

        users = [
            UserRecord(
                username=str(user.get("Username") or ""),
                email=_attribute(user, "email") or str(user.get("Username") or ""),
                role=_role(str(user.get("Username")), admins, participants),
                status=str(user.get("UserStatus") or ""),
                enabled=bool(user.get("Enabled", True)),
                created_at=iso_utc(user.get("UserCreateDate")),
                last_sign_in_at=(times.get(sub) or {}).get("last_sign_in_at"),
                last_active_at=(times.get(sub) or {}).get("last_active_at"),
            )
            for user, sub in zip(cognito_users, subs)
        ]
        users.sort(key=lambda u: (u.email.lower(), u.username))
        return UserList(
            stack=stack,
            region=self._client.region,
            account=self._account_id(),
            user_pool_id=pool_id,
            generated_at=iso_utc(datetime.now(timezone.utc)) or "",
            last_sign_in_available=available,
            last_sign_in_unavailable_reason=reason,
            users=users,
        )

    def _account_id(self) -> str:
        """The AWS account the caller's credentials are in, or "" when it cannot be read."""
        try:
            return str(self._client.session.client("sts").get_caller_identity().get("Account") or "")
        except Exception:  # noqa: BLE001
            return ""
