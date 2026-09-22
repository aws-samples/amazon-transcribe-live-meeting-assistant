#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Exchange a CI OIDC token for AWS credentials, written to a credentials file.

The scheduled integration pipeline needs AWS credentials for the account its
target stack lives in. A long-lived access key stored as a CI variable would do
it, and is what this exists to avoid: the pipeline presents the OIDC token its CI
system already mints for the job, and AWS STS returns credentials that expire
with the job.

The credentials are written to an AWS shared-credentials file under a named
profile rather than printed, so nothing secret reaches stdout, a shell variable
or a job log. The caller sets ``AWS_PROFILE`` to that profile and every later
boto3 call — the LMA SDK's, the tests' — picks it up with no further plumbing.

Usage (see the ``nightly_integ_tests`` job in .gitlab-ci.yml)::

    python integ-tests/ci_assume_role.py \\
        --role-arn "$LMA_INTEG_ROLE_ARN" \\
        --token "$GITLAB_OIDC_TOKEN" \\
        --profile lma-ci
    export AWS_PROFILE=lma-ci

Writes to $AWS_SHARED_CREDENTIALS_FILE if set, else ~/.aws/credentials. The path
is deliberately outside the checkout so it cannot be collected as a job artifact.
"""

from __future__ import annotations

import argparse
import configparser
import os
import stat
import sys
from pathlib import Path

import boto3

_DEFAULT_SESSION_DURATION = 3600


def _credentials_path() -> Path:
    configured = os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
    if configured:
        return Path(configured)
    return Path.home() / ".aws" / "credentials"


def _assume(role_arn: str, token: str, session_name: str, duration: int) -> dict[str, str]:
    # No credentials are needed to call AssumeRoleWithWebIdentity — the OIDC
    # token is the proof of identity — so the client is built unsigned-capable by
    # simply not having any configured. A region is still required for endpoint
    # resolution; STS is global but boto3 wants one.
    sts = boto3.client(
        "sts",
        region_name=os.environ.get("AWS_DEFAULT_REGION") or "us-east-1",
    )
    response = sts.assume_role_with_web_identity(
        RoleArn=role_arn,
        RoleSessionName=session_name[:64],
        WebIdentityToken=token,
        DurationSeconds=duration,
    )
    creds = response["Credentials"]
    return {
        "aws_access_key_id": creds["AccessKeyId"],
        "aws_secret_access_key": creds["SecretAccessKey"],
        "aws_session_token": creds["SessionToken"],
    }


def _write_profile(path: Path, profile: str, creds: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parser = configparser.ConfigParser()
    if path.exists():
        parser.read(path)
    parser[profile] = creds
    owner_only = stat.S_IRUSR | stat.S_IWUSR
    # O_CREAT's mode applies only when the file is created, so an existing
    # credentials file would keep whatever mode it already had. chmod as well,
    # so the result is owner-only either way, and before the write so the
    # credentials are never briefly readable by others on a shared runner.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, owner_only)
    try:
        os.fchmod(handle, owner_only)
        stream = os.fdopen(handle, "w", encoding="utf-8")
    except BaseException:
        os.close(handle)
        raise
    # From here the file object owns the descriptor and closes it.
    with stream:
        parser.write(stream)


def main(argv: list[str] | None = None) -> int:
    """Assume the role and write its credentials; returns a process exit code."""
    args_parser = argparse.ArgumentParser(description=__doc__)
    args_parser.add_argument("--role-arn", required=True, help="IAM role to assume.")
    args_parser.add_argument(
        "--token",
        required=True,
        help="OIDC ID token minted for this CI job (e.g. $GITLAB_OIDC_TOKEN).",
    )
    args_parser.add_argument(
        "--profile",
        default="lma-ci",
        help="Profile name to write (default: lma-ci). Set AWS_PROFILE to this.",
    )
    args_parser.add_argument(
        "--session-name",
        default=os.environ.get("CI_JOB_ID") or "lma-integ-tests",
        help="RoleSessionName, for CloudTrail attribution (default: the CI job id).",
    )
    args_parser.add_argument(
        "--duration-seconds",
        type=int,
        default=_DEFAULT_SESSION_DURATION,
        help=f"Session lifetime (default: {_DEFAULT_SESSION_DURATION}).",
    )
    args = args_parser.parse_args(argv)

    if not args.token.strip():
        # An unset id_tokens block yields an empty variable rather than an error,
        # which would otherwise reach STS as a confusing InvalidIdentityToken.
        print(
            "ERROR: the OIDC token is empty. Check the job's `id_tokens:` block.",
            file=sys.stderr,
        )
        return 2

    creds = _assume(
        args.role_arn, args.token.strip(), args.session_name, args.duration_seconds
    )
    path = _credentials_path()
    _write_profile(path, args.profile, creds)
    # Safe to print: the path and profile name, never the credentials.
    print(f"wrote profile {args.profile!r} to {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
