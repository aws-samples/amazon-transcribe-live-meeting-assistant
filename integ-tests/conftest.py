# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Shared fixtures for LMA integration tests.

Unlike the unit tests in ``lib/lma_sdk/tests`` (which mock boto3), these run
against a REAL deployed LMA stack using the caller's AWS credentials. They are
read-mostly and safe to repeat; the one mutating test (VP lifecycle) cleans up
after itself.

Target stack resolution (in priority order):
  1. ``--stack-name`` pytest CLI option
  2. ``LMA_STACK_NAME`` environment variable
  3. defaults to ``LMA``

Region resolution: ``LMA_REGION`` / ``AWS_DEFAULT_REGION`` / ``AWS_REGION``,
else the session default. Uses ``AWS_PROFILE`` like every other repo command.
"""

from __future__ import annotations

import os

import pytest

from lma_sdk import LMAClient


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--stack-name",
        action="store",
        default=None,
        help="LMA CloudFormation stack name (else $LMA_STACK_NAME, else 'LMA').",
    )
    parser.addoption(
        "--vp-platform",
        action="store",
        default="ZOOM",
        help="Platform for the VP lifecycle test (ZOOM|TEAMS|CHIME|WEBEX).",
    )
    parser.addoption(
        "--vp-meeting-id",
        action="store",
        default=os.environ.get("LMA_TEST_MEETING_ID", ""),
        help="Real meeting ID for the opt-in live VP join test (else skipped).",
    )
    parser.addoption(
        "--vp-meeting-password",
        action="store",
        default=os.environ.get("LMA_TEST_MEETING_PASSWORD", ""),
        help="Meeting password for the opt-in live VP join test.",
    )


@pytest.fixture(scope="session")
def stack_name(request: pytest.FixtureRequest) -> str:
    return (
        request.config.getoption("--stack-name")
        or os.environ.get("LMA_STACK_NAME")
        or "LMA"
    )


@pytest.fixture(scope="session")
def region() -> str | None:
    return (
        os.environ.get("LMA_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or os.environ.get("AWS_REGION")
    )


@pytest.fixture(scope="session")
def client(stack_name: str, region: str | None) -> LMAClient:
    """A real LMAClient bound to the target stack (session-scoped, cached)."""
    return LMAClient(stack_name=stack_name, region=region)


@pytest.fixture(scope="session")
def outputs(client: LMAClient) -> dict[str, str]:
    """CloudFormation stack outputs as a flat name->value dict.

    Fails the whole session fast if the stack doesn't exist, so every other
    test gets a clear reason rather than a cascade of confusing errors.
    """
    try:
        raw = client.stack.outputs()
    except Exception as err:  # noqa: BLE001
        pytest.fail(
            f"Could not read outputs for stack {client.stack_name!r} in "
            f"region {client.region!r}: {err}. Is the stack deployed and are "
            f"your AWS creds (AWS_PROFILE) correct?"
        )
    return {k: o.value for k, o in raw.items()}
