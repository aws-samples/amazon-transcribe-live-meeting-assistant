# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Desktop capture app video, end to end: a synthetic slideshow becomes key frames.

Streams five narrated slides as the desktop capture app would — audio over the
Stream Audio socket, H.264 fragmented MP4 over the app's second, video-only
socket — and then checks what the stack stored: one key frame per slide, in
order and at the slide's time on the transcript's clock, with both images in
the recordings bucket. When the deployed meeting assistant has the shared-screen
tool, it also asks "What did the last slide say?" and expects the answer to
quote the last slide, whose text is never spoken. See desktop_video_probe.py.

Opt-in. It runs only with ``LMA_TEST_DESKTOP_VIDEO=1``, because it takes about
two minutes, calls Polly and Bedrock, and needs a stack whose transcriber keeps
desktop key frames (``EnableVideoRecording=true``):

    AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \\
      make integ-tests STACK=lma-integtest1 LMA_TEST_DESKTOP_VIDEO=1

Once opted in, a missing dependency or test user is a failure, not a skip. The
tests are marked ``optional_feature`` so that a run without the flag, the
scheduled one included, reports them as skipped rather than failed.

The meeting is left in place: it expires with the deployment's retention, and
can be opened on the meeting page to see its Shared screen strip.
"""

from __future__ import annotations

import importlib
import os
import re

import cognito_test_user
import pytest

ENABLED = os.environ.get("LMA_TEST_DESKTOP_VIDEO", "") not in ("", "0", "false")
# A slide's frame is stamped when the picture first appeared, which is the
# key frame at the slide change; allow one key frame interval either way.
TIMESTAMP_TOLERANCE_SECONDS = 2.5
QUESTION = "What did the last slide say?"

pytestmark = [
    pytest.mark.optional_feature,
    pytest.mark.skipif(not ENABLED, reason="opt-in: set LMA_TEST_DESKTOP_VIDEO=1"),
]


def _require_dependencies() -> None:
    missing = []
    for module, requirement in (("pycognito", "requirements.txt"), ("websockets", "requirements.txt"),
                                ("PIL", "requirements-video.txt")):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(f"{module} (pip install -r integ-tests/{requirement})")
    if missing:
        pytest.fail("LMA_TEST_DESKTOP_VIDEO is set but these are missing: " + ", ".join(missing))
    from desktop_video_probe import find_ffmpeg

    try:
        find_ffmpeg()
    except FileNotFoundError as err:
        pytest.fail(str(err))


def _meeting_folder(call_id: str) -> str:
    """The per-meeting image folder name (docs/meeting-video.md, 'What is stored')."""
    return re.sub(r"[^A-Za-z0-9_.]", "_", call_id).strip("_")


@pytest.fixture(scope="module")
def run(client) -> dict:
    """Stream the slideshow once; every test below reads this one meeting."""
    _require_dependencies()
    if not cognito_test_user.available(client.region):
        pytest.fail(f"LMA_TEST_DESKTOP_VIDEO is set but there is no Cognito test user — "
                    f"{cognito_test_user.describe_sources()}")
    from desktop_video_probe import run_probe

    result = run_probe(client.stack_name, region=client.region)
    print(f"desktop video probe meeting: {result['call_id']}")
    return result


def _frame_summary(result: dict) -> str:
    return ", ".join(f"{f.get('FrameId')}@{f.get('Timestamp')}" for f in result["keyframes"]) or "none"


def test_desktop_video_streams_alongside_audio(run: dict) -> None:
    assert run["audio_state"] == "OPEN", f"audio socket ended {run['audio_state']}"
    assert run["video_state"] == "OPEN", (
        f"video socket ended {run['video_state']} (close code {run['video_close']}) before "
        f"END_VIDEO; a 1008 close means the transcriber did not accept the stream for "
        f"this call (it joins only the caller's own live audio session)"
    )
    assert run["fragments_sent"] == run["fragments"] > 0
    assert run["meeting_created"], f"no meeting row for {run['call_id']}"
    assert run["segment_count"] > 0, f"no transcript segments for {run['call_id']}"


def test_desktop_video_keeps_one_key_frame_per_slide(run: dict) -> None:
    frames = run["keyframes"]
    expected = run["expected_timestamps"]
    assert len(frames) == len(expected), (
        f"expected {len(expected)} key frames (one per slide, at {expected}), "
        f"got {len(frames)}: {_frame_summary(run)}. None at all usually means the "
        f"transcriber image predates desktop key frames or EnableVideoRecording=false."
    )
    sources = {f.get("Source") for f in frames}
    assert sources == {"DESKTOP_CAPTURE"}, sources
    stamps = [f["Timestamp"] for f in frames]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps), (
        f"key frame timestamps are not strictly increasing: {stamps}"
    )
    for title, want, got in zip(run["slides"], expected, stamps):
        assert abs(got - want) <= TIMESTAMP_TOLERANCE_SECONDS, (
            f"slide {title!r} went up at {want}s on the transcript's clock but its "
            f"key frame is stamped {got}s: {_frame_summary(run)}"
        )
    for f in frames:
        # The first video stream of a call has a position in the recording.
        assert f.get("VideoOffset") is not None, f"{f.get('FrameId')} has no VideoOffset"


def test_desktop_video_key_frame_images_are_stored(run: dict) -> None:
    images = run["images"]
    assert images, f"no key frames to check: {_frame_summary(run)}"
    folder = f"lma-video-keyframes/{_meeting_folder(run['call_id'])}/"
    for rec in images:
        where = f"{rec['which']} of {rec['frame']} (s3://{rec['bucket']}/{rec['key']})"
        assert rec["exists"], f"{where} is missing: {rec.get('error')}"
        assert rec["content_type"] == "image/jpeg", f"{where} is {rec['content_type']}"
        assert rec["bytes"] > 1000, f"{where} is only {rec['bytes']} bytes"
        assert rec["key"].startswith(folder), f"{where} is outside {folder}"
        if run["recordings_bucket"]:
            assert rec["bucket"] == run["recordings_bucket"], where


def test_assistant_reads_the_last_slide(run: dict, client) -> None:
    from desktop_video_probe import (
        LAST_SLIDE_MARKERS,
        ask_assistant,
        assistant_has_frames_tool,
    )

    if not assistant_has_frames_tool(client.stack_name, client.region):
        pytest.skip("the deployed meeting assistant has no shared-screen tool (meeting_frames.py)")
    assert len(run["keyframes"]) == len(run["slides"]), (
        f"not asking: the slides were not all kept ({_frame_summary(run)})"
    )
    answer = ask_assistant(client.stack_name, run["call_id"], run["username"], run["table"],
                           QUESTION, client.region)
    assert not answer["function_error"], answer["response"]
    text = answer["message"]
    assert text, f"the assistant returned no message: {answer['response']}"
    assert all(m in text.lower() for m in LAST_SLIDE_MARKERS), (
        f"asked {QUESTION!r}; the answer does not quote the last slide "
        f"({run['slides'][-1]!r}), which is never spoken: {text!r}"
    )
