# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Synthetic Cognito user provisioning & JWT fetching.

We mint N load-test users in the target LMA User Pool using the
``+N`` email subaddressing trick so all Welcome / invite emails land in
one real inbox.

    email prefix = strahanr       domain = amazon.com
    →  strahanr+loadtest-<runId>-0001@amazon.com
    →  strahanr+loadtest-<runId>-0002@amazon.com
    ...

Each user is created via ``AdminCreateUser`` with ``MessageAction=SUPPRESS``
(so we don't actually send invite emails for throwaway users), then
``AdminSetUserPassword`` with ``Permanent=true`` sets a random strong password
so the first-login forced-reset flow is skipped. This means we can
``InitiateAuth`` with ``USER_PASSWORD_AUTH`` and get usable JWTs immediately.

Users are tagged with ``LoadTestRunId=<runId>`` (as a Cognito custom attribute
if available, otherwise embedded in the username) so cleanup is deterministic.
"""

from __future__ import annotations

import logging
import secrets
import string
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Username format: must match a Cognito username (we use the email itself,
# since LMA's Cognito pool is email-aliased).
# Cognito usernames allow +, but some internal logs are tidier if we keep
# them readable; subaddressed email addresses are unique and work fine.
_USERNAME_TEMPLATE = "{prefix}+loadtest-{run_id}-{idx:04d}@{domain}"


@dataclass
class CognitoUser:
    """A single provisioned synthetic user + cached tokens."""

    username: str        # Cognito username (== email)
    email: str
    password: str        # Random strong password; kept in memory only.
    is_admin: bool = False
    idx: int = 0
    run_id: str = ""

    # Filled in once a token is fetched.
    id_token: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    token_expiry: float = 0.0  # epoch seconds

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable snapshot (without tokens)."""
        return {
            "username": self.username,
            "email": self.email,
            "is_admin": self.is_admin,
            "idx": self.idx,
            "run_id": self.run_id,
        }


def _random_password() -> str:
    """Generate a Cognito-policy-compliant random password.

    Default LMA Cognito policy (inherited from Amplify defaults):
    minimum 8 chars, upper+lower+digit+symbol. We generate 24 chars.
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()_+-="
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(24))
        if (
            any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)
            and any(c in "!@#$%^&*()_+-=" for c in pw)
        ):
            return pw


class SyntheticUserPool:
    """Manages a group of N synthetic users for a single run-id."""

    ADMIN_GROUP = "Admin"

    def __init__(
        self,
        user_pool_id: str,
        user_pool_client_id: str,
        region: str,
        run_id: str,
        profile: str | None = None,
    ) -> None:
        self.user_pool_id = user_pool_id
        self.user_pool_client_id = user_pool_client_id
        self.region = region
        self.run_id = run_id

        session_kwargs: dict[str, Any] = {"region_name": region}
        if profile:
            session_kwargs["profile_name"] = profile
        self.session = boto3.Session(**session_kwargs)
        self.cognito = self.session.client("cognito-idp")

        self.users: list[CognitoUser] = []

    # ── Provisioning ──────────────────────────────────────────

    def provision(
        self,
        count: int,
        email_prefix: str,
        email_domain: str,
        admin_fraction: float = 0.2,
        parallel: int = 8,
    ) -> list[CognitoUser]:
        """Create ``count`` synthetic users. Returns them (also stored on self).

        Uses a thread-pool to parallelise the AdminCreateUser + AdminSetUserPassword
        pair, since Cognito admin APIs are latency-dominated.
        """
        if count <= 0:
            return []
        # First ``admin_count`` users get the Admin group.
        admin_count = max(0, int(count * admin_fraction))
        logger.info(
            "Provisioning %d synthetic users (%d admin, %d regular) into pool %s",
            count,
            admin_count,
            count - admin_count,
            self.user_pool_id,
        )

        def _create(idx: int) -> CognitoUser:
            email = _USERNAME_TEMPLATE.format(
                prefix=email_prefix,
                run_id=self.run_id,
                idx=idx,
                domain=email_domain,
            )
            pw = _random_password()
            user = CognitoUser(
                username=email,
                email=email,
                password=pw,
                is_admin=(idx <= admin_count),
                idx=idx,
                run_id=self.run_id,
            )
            self._admin_create(user)
            self._admin_set_permanent_password(user)
            if user.is_admin:
                self._admin_add_to_group(user, self.ADMIN_GROUP)
            return user

        users: list[CognitoUser] = []
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = [pool.submit(_create, i) for i in range(1, count + 1)]
            for fut in as_completed(futures):
                try:
                    users.append(fut.result())
                except Exception as err:  # noqa: BLE001
                    logger.error("User creation failed: %s", err)
        # Deterministic ordering (by idx) to make cleanup/report stable.
        users.sort(key=lambda u: u.idx)
        self.users = users
        logger.info("Provisioned %d users successfully", len(users))
        return users

    def _admin_create(self, user: CognitoUser) -> None:
        try:
            self.cognito.admin_create_user(
                UserPoolId=self.user_pool_id,
                Username=user.username,
                UserAttributes=[
                    {"Name": "email", "Value": user.email},
                    {"Name": "email_verified", "Value": "true"},
                ],
                MessageAction="SUPPRESS",  # don't email the invite
                TemporaryPassword=user.password,
            )
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code == "UsernameExistsException":
                logger.warning("User %s already exists — reusing", user.username)
            else:
                raise

    def _admin_set_permanent_password(self, user: CognitoUser) -> None:
        self.cognito.admin_set_user_password(
            UserPoolId=self.user_pool_id,
            Username=user.username,
            Password=user.password,
            Permanent=True,
        )

    def _admin_add_to_group(self, user: CognitoUser, group: str) -> None:
        try:
            self.cognito.admin_add_user_to_group(
                UserPoolId=self.user_pool_id,
                Username=user.username,
                GroupName=group,
            )
        except ClientError as err:
            logger.warning(
                "Could not add %s to group %s: %s", user.username, group, err
            )

    # ── Auth / JWT fetch ──────────────────────────────────────

    def authenticate(self, user: CognitoUser) -> CognitoUser:
        """Populate ``user`` with JWTs.

        LMA's UserPoolClient typically enables ``ALLOW_USER_SRP_AUTH`` only
        (no ``USER_PASSWORD_AUTH``), so we do the SRP handshake first via
        pycognito and fall back to USER_PASSWORD_AUTH only if SRP is
        explicitly disabled or pycognito isn't installed. Either way we
        end up with IdToken/AccessToken/RefreshToken on the user object.
        """
        # Try SRP first (matches what the real Web UI does) so we work out
        # of the box on stacks that have USER_PASSWORD_AUTH disabled for
        # security reasons.
        try:
            from pycognito import Cognito  # noqa: WPS433 — optional dep
        except ImportError:
            Cognito = None  # type: ignore[assignment]

        if Cognito is not None:
            try:
                client = Cognito(
                    user_pool_id=self.user_pool_id,
                    client_id=self.user_pool_client_id,
                    username=user.username,
                    user_pool_region=self.region,
                )
                client.authenticate(password=user.password)
                user.id_token = client.id_token
                user.access_token = client.access_token
                user.refresh_token = client.refresh_token
                # pycognito exposes token_expires as a datetime-like; fall
                # back to a 55-min assumption if it isn't populated.
                try:
                    user.token_expiry = float(client.token_expires)  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001
                    user.token_expiry = time.time() + 3300
                return user
            except ClientError as err:
                code = err.response.get("Error", {}).get("Code", "")
                # Only fall through to USER_PASSWORD_AUTH when the pool
                # genuinely doesn't support SRP; other errors (bad
                # password, user disabled, …) should still surface.
                if code not in (
                    "InvalidParameterException",
                    "NotAuthorizedException",
                ):
                    logger.error("SRP auth failed for %s: %s", user.username, err)
                    raise
                logger.debug(
                    "SRP auth rejected (%s); falling back to USER_PASSWORD_AUTH",
                    code,
                )
            except Exception as err:  # noqa: BLE001
                # pycognito can raise non-ClientError subclasses (e.g. its
                # own SoftwareTokenMFAChallengeException). Fall back rather
                # than crash.
                logger.debug("SRP auth failed (%s); trying USER_PASSWORD_AUTH", err)

        # Fallback path — only works if the UserPoolClient has
        # ALLOW_USER_PASSWORD_AUTH enabled.
        try:
            resp = self.cognito.initiate_auth(
                ClientId=self.user_pool_client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={
                    "USERNAME": user.username,
                    "PASSWORD": user.password,
                },
            )
        except ClientError as err:
            # Give the user a concrete remediation path — this is the
            # #1 source of confusion when load-testing a stock LMA stack.
            msg = str(err)
            if "USER_PASSWORD_AUTH flow not enabled" in msg:
                raise RuntimeError(
                    "Cognito auth failed: the UserPoolClient does not allow "
                    "USER_PASSWORD_AUTH, and pycognito (SRP) is either not "
                    "installed or could not authenticate. Install it with "
                    "`pip install pycognito` (it's a declared dep of "
                    "lma-load-simulator; you're probably running against a "
                    "stale install), or enable USER_PASSWORD_AUTH on the "
                    "UserPoolClient. Underlying error: " + msg
                ) from err
            logger.error("Auth failed for %s: %s", user.username, err)
            raise
        result = resp.get("AuthenticationResult") or {}
        user.id_token = result.get("IdToken")
        user.access_token = result.get("AccessToken")
        user.refresh_token = result.get("RefreshToken")
        # ExpiresIn is relative seconds; we cache the absolute deadline.
        user.token_expiry = time.time() + int(result.get("ExpiresIn", 3600)) - 60
        return user

    def authenticate_all(self, parallel: int = 8) -> None:
        """Fetch tokens for every user in the pool."""
        if not self.users:
            return
        logger.info("Authenticating %d users", len(self.users))
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            for fut in as_completed(pool.submit(self.authenticate, u) for u in self.users):
                try:
                    fut.result()
                except Exception as err:  # noqa: BLE001
                    logger.error("Auth failed: %s", err)

    # ── Cleanup ───────────────────────────────────────────────

    def list_synthetic_users(self, run_id: str | None = None) -> list[dict]:
        """List all users whose username contains the given run-id (or all
        run-ids if ``run_id`` is None — i.e. everything ever created by
        load-simulator).

        Cognito lower-cases email-format usernames on intake (so a user
        created with ``…lt-20260511T192735-62bb54…`` comes back as
        ``…lt-20260511t192735-62bb54…``). The run_id generator uses a
        mixed-case timestamp, so we match case-insensitively to avoid
        missing everything.
        """
        marker = (f"+loadtest-{run_id}-" if run_id else "+loadtest-").lower()
        paginator = self.cognito.get_paginator("list_users")
        matched: list[dict] = []
        for page in paginator.paginate(
            UserPoolId=self.user_pool_id, AttributesToGet=["email"], Limit=60
        ):
            for u in page.get("Users", []):
                if marker in u.get("Username", "").lower():
                    matched.append(u)
        return matched

    def delete_synthetic_users(self, run_id: str | None = None, parallel: int = 8) -> int:
        """Delete every synthetic user matching ``run_id`` (None = all)."""
        victims = self.list_synthetic_users(run_id)
        logger.info("Deleting %d synthetic Cognito users", len(victims))

        def _del(username: str) -> None:
            try:
                self.cognito.admin_delete_user(
                    UserPoolId=self.user_pool_id, Username=username
                )
            except ClientError as err:
                logger.warning("Could not delete %s: %s", username, err)

        with ThreadPoolExecutor(max_workers=parallel) as pool:
            list(pool.map(lambda u: _del(u["Username"]), victims))
        return len(victims)
