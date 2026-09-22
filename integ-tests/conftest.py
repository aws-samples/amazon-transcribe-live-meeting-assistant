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
    parser.addoption(
        "--no-skips",
        action="store_true",
        default=os.environ.get("LMA_INTEG_NO_SKIP", "") not in ("", "0", "false"),
        help=(
            "Treat a skipped test as a failure. For the scheduled pipeline, where "
            "the opt-in tests are the point of the run and a silent skip would "
            "report green having checked nothing."
        ),
    )


@pytest.hookimpl(hookwrapper=True)
# `call` is unused but its name and position are fixed by pytest's hookspec.
def pytest_runtest_makereport(  # pylint: disable=unused-argument
    item: pytest.Item, call: pytest.CallInfo[None]
):
    """Under ``--no-skips``, rewrite a skipped outcome into a failure.

    The opt-in tests skip themselves when their credentials or optional
    dependencies are absent, which is right for a local run and wrong for the
    scheduled pipeline: a missing secret or a dropped requirement would produce a
    green run that exercised none of the audio path. Rewriting the outcome here
    rather than in each test means a skip added later is covered too.

    ``pytest.importorskip`` and ``@pytest.mark.skipif`` raise at different points,
    so both the call and setup phases are handled.
    """
    outcome = yield
    if not item.config.getoption("--no-skips"):
        return
    report = outcome.get_result()
    if report.skipped and not _is_deselected_by_marker(item):
        reason = getattr(report, "longrepr", None)
        report.outcome = "failed"
        report.longrepr = (
            f"skipped under --no-skips, which this run forbids: {reason}"
        )


def _is_deselected_by_marker(item: pytest.Item) -> bool:
    """Whether the skip came from ``-m``/``--vp-meeting-id`` gating, not a gap.

    ``make integ-tests`` passes ``-m "not live"``, which *deselects* rather than
    skips, so it does not reach here. An explicit ``live`` marker still might if
    the suite is invoked another way, and a run that has deliberately excluded
    real-meeting tests should not fail because of them.
    """
    return item.get_closest_marker("live") is not None


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
