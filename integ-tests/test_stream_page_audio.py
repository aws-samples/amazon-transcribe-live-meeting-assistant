# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The deployed Stream Audio/Video page streams a meeting's audio, in Chromium.

Signs in to the stack's web UI as a throwaway Cognito user (made with
``scripts/ux_test_session.py`` and deleted afterwards), leaves **Also record
screen video** off, and streams for about 30 seconds (``STREAM_SECONDS``)
through the page's own capture: the page shares a stand-in meeting tab that
plays one channel of a recorded call, and Chromium's fake microphone plays the
other (``stream_page_probe.run_stream_page_audio``). Then checks the meeting:
transcript segments on both channels, ended, with an audio recording and no
video. A second meeting runs with Meeting Consent in Notify mode: the page
shows the notice, the notice is confirmed as posted in the meeting chat, and
the meeting's consent record keeps the notice the page showed.
``test_stream_page_video.py`` covers the page with screen video on.

Opt-in. It runs only with ``LMA_TEST_STREAM_PAGE_AUDIO=1``, because it needs
Playwright with Chromium (``requirements-video.txt``, then ``python -m
playwright install chromium``) and a display (run under ``xvfb-run`` on a
machine without one), creates a Cognito user, and for the run turns Meeting
Recording › Record meeting audio on and sets the Meeting Consent mode (off,
then Notify), putting both sections back afterwards:

    AWS_PROFILE=default xvfb-run -a \\
      make integ-tests STACK=lma-integtest1 LMA_TEST_STREAM_PAGE_AUDIO=1

Needs credentials with CloudFormation read, Cognito user administration on the
stack's pool, configuration table read and write, and DynamoDB read and delete
on its EventSourcing table. The meetings are deleted afterwards unless
``LMA_TEST_KEEP_MEETING=1``.
"""

from __future__ import annotations

import importlib
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

ENABLED = os.environ.get("LMA_TEST_STREAM_PAGE_AUDIO", "") not in ("", "0", "false")
KEEP_MEETING = os.environ.get("LMA_TEST_KEEP_MEETING", "") not in ("", "0", "false")
STREAM_SECONDS = 30
#: How long the Meeting Consent section may be served from cache after a change.
CONSENT_CACHE_SECONDS = 35

pytestmark = [
    pytest.mark.optional_feature,
    pytest.mark.skipif(not ENABLED, reason="opt-in: set LMA_TEST_STREAM_PAGE_AUDIO=1"),
]


def _require_dependencies() -> None:
    for module in ("playwright", "websockets", "pycognito"):
        try:
            importlib.import_module(module)
        except ImportError:
            pytest.fail(f"LMA_TEST_STREAM_PAGE_AUDIO is set but {module} is missing "
                        "(pip install -r integ-tests/requirements.txt -r integ-tests/requirements-video.txt)")
    if not os.environ.get("DISPLAY"):
        pytest.fail("LMA_TEST_STREAM_PAGE_AUDIO is set but there is no display; run the tests under xvfb-run -a")


@pytest.fixture(scope="module")
def setup(client):
    """The stack, a throwaway user, the stand-in meeting tab, and the settings for the run."""
    _require_dependencies()
    import browser_extension_probe as extension
    from meeting_consent_probe import ConsentSettingsOverride, Stack
    from stream_page_probe import meeting_tab_html

    stack = Stack.find(client.stack_name, client.region)
    url = stack.outputs.get("ApplicationCloudfrontEndpoint")
    if not url:
        pytest.fail(f"{client.stack_name} has no ApplicationCloudfrontEndpoint output")
    user_stack = extension.resolve_stack(client.stack_name, client.region)
    user = extension.create_test_user(user_stack)
    scratch = Path.home() / ".lma-scratch"
    scratch.mkdir(exist_ok=True)
    workdir = tempfile.TemporaryDirectory(prefix="stream-page-audio-", dir=scratch)
    media = Path(workdir.name)
    # A minute of each channel: the fake microphone and the tab both loop.
    remote, local = extension.split_channels(extension.CALL_WAV, media, 60)
    (media / "meeting.html").write_text(meeting_tab_html(remote.name))
    origin, server = extension.serve(media)
    recording = ConsentSettingsOverride(stack, section="recording")
    consent = ConsentSettingsOverride(store=recording.store, section="meeting_consent")
    try:
        recording.save()
        record = recording.store.read_profile(recording.profile, consistent_read=True)
        consent.profile = recording.profile
        consent.had_section = "meeting_consent" in record.body
        consent.saved = dict(record.body.get("meeting_consent") or {}) if consent.had_section else None
        recording.apply(recordAudio=True)
        yield {"stack": stack, "url": url, "user": user, "consent": consent,
               "meeting_url": f"{origin}/meeting.html", "microphone": str(local)}
    finally:
        consent.restore()
        recording.restore()
        server.shutdown()
        workdir.cleanup()
        extension.delete_test_user(user_stack, user["email"])


def _stream(setup: dict, label: str, *, confirm_notice: bool = False):
    from meeting_consent_probe import read_meeting, segments, wait_for
    from stream_page_probe import run_stream_page_audio

    topic = f"stream-page-audio-{label} {datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    run = run_stream_page_audio(
        setup["url"], setup["user"]["email"], setup["user"]["password"], seconds=STREAM_SECONDS,
        topic=topic, meeting_url=setup["meeting_url"], microphone_wav=setup["microphone"],
        confirm_notice=confirm_notice,
    )
    print(f"stream page audio run ({label}): call {run.call_id}, {run.audio_frames} audio frames, "
          f"errors {run.errors}")
    meeting, found = {}, []
    if run.call_id:
        stack = setup["stack"]
        meeting = wait_for(
            lambda: (m := read_meeting(stack, run.call_id)) and m.get("Status") == "ENDED"
            and m.get("RecordingUrl") and m,
            timeout=120,
        ) or read_meeting(stack, run.call_id) or {}
        found = wait_for(lambda: (s := segments(stack, run.call_id))
                         and {x.get("Channel") for x in s} >= {"LOCAL", "REMOTE"} and s,
                         timeout=60) or segments(stack, run.call_id)
    return {"run": run, "meeting": meeting, "segments": found}


def _cleanup(setup: dict, call_id: str | None) -> None:
    if call_id and not KEEP_MEETING:
        import browser_extension_probe as extension

        extension.cleanup(extension.resolve_stack(setup["stack"].name, setup["stack"].region), call_id)


@pytest.fixture(scope="module")
def audio_only(setup):
    """One meeting with Meeting Consent off."""
    setup["consent"].apply(mode="off")
    time.sleep(CONSENT_CACHE_SECONDS)
    result = _stream(setup, "off")
    try:
        yield result
    finally:
        _cleanup(setup, result["run"].call_id)


@pytest.fixture(scope="module")
def notified(setup, audio_only):
    """One meeting in Notify mode. Depends on ``audio_only`` so that the mode changes after it."""
    setup["consent"].apply(mode="notify")
    time.sleep(CONSENT_CACHE_SECONDS)
    result = _stream(setup, "notify", confirm_notice=True)
    try:
        yield result
    finally:
        _cleanup(setup, result["run"].call_id)


def test_the_page_streams_audio_without_video(audio_only) -> None:
    run = audio_only["run"]
    assert run.streaming_shown, f"{run.errors}; last console lines: {run.console[-15:]}"
    assert run.call_id, f"the page started no meeting: {run.errors}"
    assert run.start_frame.get("clientType") == "STREAM_AUDIO", run.start_frame
    assert run.audio_frames > 0, "the page sent no audio"
    assert not run.start_video_sent, "the page sent START_VIDEO with screen video off"


def test_the_page_transcribes_the_tab_and_the_microphone(audio_only) -> None:
    found = audio_only["segments"]
    channels = {s.get("Channel") for s in found}
    assert {"LOCAL", "REMOTE"} <= channels, (
        f"expected transcript from the microphone (LOCAL) and the shared tab (REMOTE), "
        f"got {len(found)} segments on {sorted(c for c in channels if c)}"
    )


def test_the_page_meeting_ends_with_an_audio_recording(audio_only) -> None:
    meeting = audio_only["meeting"]
    assert meeting, f"no meeting row for {audio_only['run'].call_id}"
    assert meeting.get("CaptureSource") == "STREAM_AUDIO", meeting.get("CaptureSource")
    assert meeting.get("Status") == "ENDED", f"the meeting's status is {meeting.get('Status')!r}"
    assert meeting.get("RecordingUrl"), f"no audio recording on {audio_only['run'].call_id}"
    assert not meeting.get("VideoRecordingUrl"), "a meeting streamed with video off has a video recording"


def test_the_page_shows_the_notice_and_the_meeting_records_it(notified) -> None:
    run = notified["run"]
    assert run.notice_shown, f"the page showed no consent notice in Notify mode: {run.errors}"
    assert run.streaming_shown and run.call_id, f"{run.errors}; last console lines: {run.console[-15:]}"
    sent = run.start_frame.get("consent") or {}
    # Compared without surrounding space, which the page's rendering drops.
    assert sent.get("mode") == "notify" and (sent.get("noticeText") or "").strip() == run.notice_shown, sent
    consent = notified["meeting"].get("Consent") or {}
    assert consent.get("Mode") == "notify", consent
    assert consent.get("CaptureAllowed") is True, consent
    notices = consent.get("Notices") or []
    assert notices and (notices[0].get("Text") or "").strip() == run.notice_shown, notices
    assert notified["segments"], f"no transcript segments for {run.call_id}"
