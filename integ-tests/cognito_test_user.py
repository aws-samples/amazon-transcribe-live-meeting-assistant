# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Where the opt-in tests get a Cognito user's credentials.

Interactively you export ``LMA_TEST_USERNAME`` / ``LMA_TEST_PASSWORD`` and run
the suite, which is what `.claude/skills/integ-tests.md` documents and what this
module still does first.

That does not suit a scheduled pipeline: the password would have to be a CI
variable, which puts it in the CI configuration and one stray ``set -x`` away
from a job log. So a second source is supported — ``LMA_TEST_USER_SECRET_ID``
naming an AWS Secrets Manager secret, read with the same credentials the tests
already use for CloudFormation and DynamoDB. The value never passes through a
shell or an environment variable; it is read inside the test process and handed
straight to the Cognito SRP exchange.

The secret is a JSON object. ``username`` and ``password`` are the canonical
keys; ``email`` is accepted for the username because that is what a Cognito
user pool configured for email sign-in calls it.

This module deliberately has no ``test_`` functions. It is named
``test_user_credentials`` because it is about the *test user's* credentials, and
pytest collecting it as an empty module is harmless.
"""

from __future__ import annotations

import functools
import json
import os

_USERNAME_KEYS = ("username", "email", "user")
_PASSWORD_KEYS = ("password", "pass")


class CredentialsUnavailable(RuntimeError):
    """No credential source is configured, or the configured one did not work."""


def describe_sources() -> str:
    """A one-line reminder of how to supply credentials, for skip/failure text."""
    return (
        "set LMA_TEST_USERNAME / LMA_TEST_PASSWORD, or LMA_TEST_USER_SECRET_ID"
        ' naming a Secrets Manager secret holding {"username": ..., "password": ...}'
    )


def _from_environment() -> tuple[str, str] | None:
    username = os.environ.get("LMA_TEST_USERNAME")
    password = os.environ.get("LMA_TEST_PASSWORD")
    if username and password:
        return username, password
    return None


def _pick(
    payload: dict[str, object], keys: tuple[str, ...], secret_id: str, what: str
) -> str:
    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    raise CredentialsUnavailable(
        f"secret {secret_id!r} has no {what} — expected one of {list(keys)}, "
        f"found keys {sorted(payload)}"
    )


def _from_secrets_manager(region: str | None) -> tuple[str, str] | None:
    secret_id = os.environ.get("LMA_TEST_USER_SECRET_ID")
    if not secret_id:
        return None

    # Imported here, not at module scope, so the environment-variable path costs
    # nothing and a caller with no AWS credentials can still use it.
    import boto3  # pylint: disable=import-outside-toplevel

    client = boto3.Session(region_name=region).client("secretsmanager")
    try:
        raw = client.get_secret_value(SecretId=secret_id)["SecretString"]
    except Exception as err:  # noqa: BLE001 — surfaced verbatim, never swallowed
        raise CredentialsUnavailable(
            f"could not read Secrets Manager secret {secret_id!r}: {err}"
        ) from err

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as err:
        raise CredentialsUnavailable(
            f"secret {secret_id!r} is not JSON: {err}"
        ) from err
    if not isinstance(payload, dict):
        raise CredentialsUnavailable(f"secret {secret_id!r} is not a JSON object")

    return (
        _pick(payload, _USERNAME_KEYS, secret_id, "username"),
        _pick(payload, _PASSWORD_KEYS, secret_id, "password"),
    )


@functools.lru_cache(maxsize=None)
def _resolve_cached(region: str | None) -> tuple[str, str] | None:
    return _from_environment() or _from_secrets_manager(region)


def resolve(region: str | None = None) -> tuple[str, str] | None:
    """Return ``(username, password)``, or ``None`` if no source is configured.

    Raises ``CredentialsUnavailable`` when a source *is* configured but cannot be
    used, so a misspelled secret id fails the run instead of quietly skipping.
    Cached: the suite resolves this more than once and a Secrets Manager call per
    use is wasted work.
    """
    return _resolve_cached(region)


def available(region: str | None = None) -> bool:
    """Whether credentials can be obtained, without raising on a broken source."""
    try:
        return resolve(region) is not None
    except CredentialsUnavailable:
        return False
