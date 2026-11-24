# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The deployed Stream Audio/Video page records screen video, in Chromium.

Signs in to the stack's web UI as the Cognito test user, switches on **Also
record screen video**, and streams a canvas slideshow tab for about 40 seconds
through the page's own capture (getDisplayMedia and MediaRecorder; see
stream_page_probe.py). Then checks the meeting: its capture source, a video
recording, and key frames under ``Source`` ``STREAM_AUDIO``, about one per
slide. ``test_browser_video.py`` covers what the transcriber does with such a
stream at the protocol; this file covers the page producing it.

Opt-in. It runs only with ``LMA_TEST_STREAM_PAGE_VIDEO=1``, because it needs
Playwright with Chromium (``requirements-video.txt``, then ``python -m
playwright install chromium``) and a display (run under ``xvfb-run`` on a
machine without one), turns on Meeting Recording › Record screen
video from Stream Audio/Video and the browser extension for the run (and puts
it back), and needs a stack with ``EnableVideoRecording=true``:

    AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user xvfb-run -a \\
      make integ-tests STACK=lma-integtest1 LMA_TEST_STREAM_PAGE_VIDEO=1

Chromium on Linux encodes in software; Windows and macOS hardware encoders, and
sharing a real meeting tab, still need a headed check on real hardware.
"""

from __future__ import annotations

import importlib
import os
import time
from datetime import UTC, datetime

import pytest

import cognito_test_user

ENABLED = os.environ.get("LMA_TEST_STREAM_PAGE_VIDEO", "") not in ("", "0", "false")
STREAM_SECONDS = 60

pytestmark = [
    pytest.mark.optional_feature,
    pytest.mark.skipif(not ENABLED, reason="opt-in: set LMA_TEST_STREAM_PAGE_VIDEO=1"),
]


@pytest.fixture(scope="module")
def page_run(client):
    for module in ("playwright", "websockets", "pycognito"):
        try:
            importlib.import_module(module)
        except ImportError:
            pytest.fail(f"LMA_TEST_STREAM_PAGE_VIDEO is set but {module} is missing "
                        "(pip install -r integ-tests/requirements.txt -r integ-tests/requirements-video.txt)")
    if not os.environ.get("DISPLAY"):
        pytest.fail("LMA_TEST_STREAM_PAGE_VIDEO is set but there is no display; run the tests under xvfb-run -a")
    credentials = cognito_test_user.resolve(client.region)
    if credentials is None:
        pytest.fail(f"LMA_TEST_STREAM_PAGE_VIDEO is set but there is no Cognito test user — "
                    f"{cognito_test_user.describe_sources()}")
    from meeting_consent_probe import ConsentSettingsOverride, Stack, read_meeting
    from stream_page_probe import SLIDE_SECONDS, run_stream_page

    stack = Stack.find(client.stack_name, client.region)
    url = stack.outputs.get("ApplicationCloudfrontEndpoint")
    if not url:
        pytest.fail(f"{client.stack_name} has no ApplicationCloudfrontEndpoint output")
    recording = ConsentSettingsOverride(stack, section="recording")
    recording.save()
    consent = ConsentSettingsOverride(store=recording.store, section="meeting_consent")
    record = recording.store.read_profile(recording.profile, consistent_read=True)
    consent.profile = recording.profile
    consent.had_section = "meeting_consent" in record.body
    consent.saved = dict(record.body.get("meeting_consent") or {}) if consent.had_section else None
    try:
        recording.apply(recordBrowserVideo=True)
        consent.apply(mode="off")
        time.sleep(35)  # Meeting Consent is read on a cached tier
        username, password = credentials
        topic = f"stream-page-video-probe {datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
        run = run_stream_page(url, username, password, seconds=STREAM_SECONDS, topic=topic)
        print(f"stream page run: call {run.call_id}, warnings {run.warnings}, errors {run.errors}")
        frames, meeting = [], {}
        if run.call_id:
            from desktop_video_probe import read_keyframes

            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                frames = read_keyframes(stack.session.client("dynamodb"), stack.event_table, run.call_id)
                meeting = read_meeting(stack, run.call_id) or {}
                if meeting.get("VideoRecordingUrl") and frames:
                    break
                time.sleep(5)
        yield {"run": run, "frames": frames, "meeting": meeting,
               "slides": STREAM_SECONDS // SLIDE_SECONDS}
    finally:
        consent.restore()
        recording.restore()


def test_the_page_records_the_shared_tab_as_video(page_run) -> None:
    run = page_run["run"]
    assert run.video_on_shown, f"{run.errors}; last console lines: {run.console[-15:]}"
    assert run.surface_text.startswith("Recording a browser tab"), run.surface_text
    assert "Screen video stopped" not in run.warnings, run.warnings
    assert run.call_id, "the page offered no recorded meeting to open"


def test_the_page_video_is_recorded_with_the_meeting(page_run) -> None:
    meeting = page_run["meeting"]
    assert meeting.get("CaptureSource") == "STREAM_AUDIO", meeting.get("CaptureSource")
    assert meeting.get("VideoRecordingUrl"), f"no video recording on {page_run['run'].call_id}"


def test_the_page_video_keeps_key_frames_under_its_source(page_run) -> None:
    frames = page_run["frames"]
    assert {f.get("Source") for f in frames} == {"STREAM_AUDIO"}, frames
    # One per slide, allowing the last slide, which may be cut short, to be missed.
    assert len(frames) >= page_run["slides"] - 1, [f.get("Timestamp") for f in frames]
    assert "Some slides may be missed" not in page_run["run"].warnings
