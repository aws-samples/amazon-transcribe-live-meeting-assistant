# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The browser extension a stack serves, signed in and streaming against that stack.

Loads the extension the stack serves into Chromium, signs in on the Cognito hosted
page as a throwaway user, captures a local stand-in meeting page (one channel of a
recorded call) and Chromium's fake microphone (the other channel), and checks the
meeting and its transcript on the stack. It then continues that meeting as the same
user with ``reopenMeeting`` and a second session. See browser_extension_probe.py for
what is real and what is stood in for.

Opt-in. It runs only with ``LMA_TEST_BROWSER_EXTENSION=1``, because it takes about
three minutes, downloads a browser, and creates a Cognito user (deleted afterwards):

    .venv/bin/pip install -r integ-tests/requirements.txt \\
        -r lma-browser-extension-stack/e2e/requirements.txt
    .venv/bin/python -m playwright install --no-shell chromium
    AWS_PROFILE=default LMA_TEST_BROWSER_EXTENSION=1 \\
        make integ-tests STACK=<stack>

Needs credentials with CloudFormation read, Cognito user administration on the stack's
pool (``scripts/ux_test_session.py``) and DynamoDB read and delete on its
EventSourcing table. The meeting is deleted afterwards unless
``LMA_TEST_KEEP_MEETING=1``. ``LMA_TEST_EXTENSION_BUILD`` tests a local build (with the
served configuration) instead of the served extension; ``LMA_TEST_HEADED=1`` shows the
browser.

Once opted in, a missing dependency is a failure, not a skip. The tests are marked
``optional_feature`` so that a run without the flag, the scheduled one included,
reports them as skipped.
"""

from __future__ import annotations

import importlib
import os
import tempfile
from pathlib import Path

import pytest

ENABLED = os.environ.get("LMA_TEST_BROWSER_EXTENSION", "") not in ("", "0", "false")
KEEP_MEETING = os.environ.get("LMA_TEST_KEEP_MEETING", "") not in ("", "0", "false")
HEADED = os.environ.get("LMA_TEST_HEADED", "") not in ("", "0", "false")

pytestmark = [
    pytest.mark.optional_feature,
    pytest.mark.skipif(not ENABLED, reason="opt-in: set LMA_TEST_BROWSER_EXTENSION=1"),
]


def _require_dependencies() -> None:
    missing = []
    for module, requirement in (
        ("pycognito", "integ-tests/requirements.txt"),
        ("websockets", "integ-tests/requirements.txt"),
        ("playwright", "lma-browser-extension-stack/e2e/requirements.txt"),
    ):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(f"{module} (pip install -r {requirement})")
    if missing:
        pytest.fail(
            "LMA_TEST_BROWSER_EXTENSION is set but these are missing: " + ", ".join(missing)
        )


@pytest.fixture(scope="module")
def stack(client):
    import browser_extension_probe as probe

    return probe.resolve_stack(client.stack_name, client.region)


@pytest.fixture(scope="module")
def user(stack):
    _require_dependencies()
    import browser_extension_probe as probe

    created = probe.create_test_user(stack)
    try:
        yield created
    finally:
        probe.delete_test_user(stack, created["email"])


@pytest.fixture(scope="module")
def run(stack, user):
    """Stream once from the extension; every test below reads this one meeting."""
    import browser_extension_probe as probe

    scratch = Path.home() / ".lma-scratch"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="extension-e2e-", dir=scratch) as workdir:
        result = probe.stream_from_extension(stack, user, Path(workdir), headless=not HEADED)
    print(f"browser extension meeting: {result.call_id!r}")
    try:
        yield result
    finally:
        if result.call_id and not KEEP_MEETING:
            probe.cleanup(stack, result.call_id)


def _value(item: dict, key: str) -> str | None:
    """A DynamoDB attribute's value as a string, whatever its type."""
    attribute = (item or {}).get(key)
    return str(next(iter(attribute.values()))) if attribute else None


def test_served_extension_has_a_version_chrome_accepts(run) -> None:
    import browser_extension_probe as probe

    assert probe.chrome_accepts_version(run.served_version), (
        f"the stack serves the extension with manifest version {run.served_version!r}, "
        "which Chrome refuses to load (it accepts 1-4 dot-separated integers); the rest "
        "of this run used a copy stamped with its release part"
    )


def test_extension_signs_in_on_the_hosted_page(run, user) -> None:
    import extension_browser as eb

    assert run.extension_id == eb.expected_extension_id()
    assert run.signed_in, "the panel never showed Start Listening after Login"
    # The name field is prefilled from the signed-in user's id token.
    assert run.panel_name == user["email"].split("@")[0]


def test_extension_streams_the_tab_and_the_microphone(run, user) -> None:
    assert run.transcribing, f"capture did not start: {run.notes}"
    assert run.start_frame.get("clientType") == "BROWSER_EXTENSION", run.start_frame
    assert run.start_frame.get("samplingRate") == 8000, run.start_frame
    assert run.audio_frames > 0, "no audio frame was sent"
    assert not run.page_errors, run.page_errors
    assert run.meeting, f"no meeting record for {run.call_id!r}"
    assert _value(run.meeting, "Owner") == user["email"]
    assert _value(run.meeting, "CaptureSource") == "BROWSER_EXTENSION"
    channels = {s["Channel"]["S"] for s in run.segments}
    assert channels == {"LOCAL", "REMOTE"}, (
        f"expected transcript from the microphone (LOCAL) and the meeting tab (REMOTE), "
        f"got {len(run.segments)} segments on {sorted(channels)}"
    )


def test_owner_continues_the_extension_meeting(run, user, stack) -> None:
    import browser_extension_probe as probe

    assert run.meeting, "no meeting to continue"
    tokens = probe.user_tokens(stack, user)
    answer = probe.reopen_meeting(stack, tokens["id"], run.call_id)
    errors = answer.get("errors") or []
    if any("reopenMeeting" in str(e.get("message", "")) and "Validation" in str(e) for e in errors):
        pytest.skip("this stack's API has no reopenMeeting")
    assert not errors, errors
    reopened = answer["data"]["reopenMeeting"]
    session, offset = reopened["SessionNumber"], float(reopened["SessionOffsetSeconds"])
    assert session == 2

    streamed = probe.stream_session(
        stack, tokens, run.call_id, session, user["email"].split("@")[0], probe.CONTINUE_SECONDS
    )
    refusals = [r for r in streamed["replies"] if '"captureAllowed":false' in r]
    assert streamed["state"] == "OPEN" and not refusals, streamed

    meeting, segments = probe.wait_for_transcript(stack, run.call_id, prefix=f"s{session}-")
    assert segments, f"no transcript segments for session {session} of {run.call_id!r}"
    starts = [float(s["StartTime"]["N"]) for s in segments]
    assert min(starts) >= offset, f"session {session} starts at {offset}s, segments at {starts}"
    assert _value(meeting, "SessionStartedNumber") == str(session)
