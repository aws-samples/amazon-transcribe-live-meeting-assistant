# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Deleting a meeting's recordings once it is summarized, end to end.

Sets the deployment's Retention › "Delete the recordings after the summary" to
Immediately for the length of the module (and Meeting Recording's two switches
on, if they are not), streams one meeting with audio and screen video as the
Desktop Capture App does (desktop_video_probe.py), waits for its summary, and
reads back what the stack kept. See docs/transcript-deletion.md for the
behaviour asserted:

- the meeting record says when its recordings were deleted and how many files
  went, and no longer links to any recording;
- no version or delete marker of the meeting's audio or video recordings is
  left in the recordings bucket, including a recording stored after the
  deletion ran (the transcriber stores a session's recordings after its END);
- the shared screen's key frames, their images and the summary are kept, and
  the transcript too while its own setting is off.

Opt-in. It runs only with ``LMA_TEST_RECORDING_DELETION=1``, because it changes
a deployment-wide setting while it runs (every meeting summarized in that time
has its recordings deleted), takes about four minutes, calls Polly, Amazon
Transcribe and Bedrock, and needs ffmpeg, Pillow and a stack that records video
(``EnableVideoRecording=true``):

    AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \\
      make integ-tests STACK=lma-integtest1 LMA_TEST_RECORDING_DELETION=1

Each setting is written through the configuration store into the active
profile, as test_transcript_deletion.py writes its own, and put back in the
module's teardown whatever the tests did.
"""

from __future__ import annotations

import importlib
import os
import re
import time
from datetime import datetime, timezone

import cognito_test_user
import pytest

ENABLED = os.environ.get("LMA_TEST_RECORDING_DELETION", "") not in ("", "0", "false")

pytestmark = [
    pytest.mark.optional_feature,
    pytest.mark.skipif(not ENABLED, reason="opt-in: set LMA_TEST_RECORDING_DELETION=1"),
]

SETTING = "recordingDeletionAfterSummary"
SUMMARY_TIMEOUT_SECONDS = 300
DELETION_TIMEOUT_SECONDS = 120
# The transcriber stores the session's WAV, its raw audio and the screen video
# after the END; give the last of them this long to arrive and be deleted.
LATE_RECORDING_SECONDS = 180
# How long a saved Meeting Recording change can take to reach the transcriber.
SETTINGS_REACH_SECONDS = 330
LINK_FIELDS = (
    "RecordingUrl",
    "RecordingUrls",
    "RecordingUrlSessions",
    "VideoRecordingUrl",
    "VideoRecordingUrls",
    "VideoRecordingUrlSessions",
)


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _posix(name: str) -> str:
    return re.sub(r"_+$", "", re.sub(r"^_+", "", re.sub(r"[^a-zA-Z0-9_.]", "_", name)))


@pytest.fixture(scope="module")
def stack(client):
    for module in ("pycognito", "websockets", "PIL"):
        try:
            importlib.import_module(module)
        except ImportError:
            pytest.fail(f"LMA_TEST_RECORDING_DELETION is set but {module} is missing "
                        "(pip install -r integ-tests/requirements.txt -r integ-tests/requirements-video.txt)")
    from desktop_video_probe import find_ffmpeg

    try:
        find_ffmpeg()
    except FileNotFoundError as err:
        pytest.fail(str(err))
    if not cognito_test_user.available(client.region):
        pytest.fail("LMA_TEST_RECORDING_DELETION is set but there is no Cognito test user — "
                    f"{cognito_test_user.describe_sources()}")
    from meeting_consent_probe import Stack

    found = Stack.find(client.stack_name, client.region)
    if "TranscriptDeletionFunction" not in found.ai_resources:
        pytest.fail(f"{client.stack_name} has no deletion function; deploy a version with it")
    return found


def _video_allowed(stack, tokens) -> bool | None:
    from browser_video_probe import settings_answer

    answer = settings_answer(stack.outputs["LMAWebsocketEndpoint"], tokens, None) or {}
    return answer.get("videoAllowed")


@pytest.fixture(scope="module")
def settings(stack):
    """Recordings deleted Immediately, and audio and screen video recorded; always put back."""
    from meeting_consent_probe import ConsentSettingsOverride, tokens_for_test_user

    retention = ConsentSettingsOverride(
        stack, section="retention", updated_by="integ-tests/test_recording_deletion.py"
    )
    recording = ConsentSettingsOverride(
        stack, section="recording", updated_by="integ-tests/test_recording_deletion.py"
    )
    retention.save()
    recording.save()
    try:
        retention.apply(**{SETTING: "immediately"})
        assert retention.resolved().get(SETTING) == "immediately"
        transcript_kept = retention.resolved().get("transcriptDeletionAfterSummary", "off") == "off"
        current = recording.resolved()
        if not (current.get("recordAudio") and current.get("recordScreenVideo")):
            recording.apply(recordAudio=True, recordScreenVideo=True)
            _username, tokens = tokens_for_test_user(stack)
            deadline = time.monotonic() + SETTINGS_REACH_SECONDS
            while _video_allowed(stack, tokens) is not True and time.monotonic() < deadline:
                time.sleep(10)
            if _video_allowed(stack, tokens) is not True:
                pytest.fail("Meeting Recording was turned on but the transcriber still withholds video")
        yield {"transcript_kept": transcript_kept}
    finally:
        recording.restore()
        retention.restore()
        print(f"retention: restored; the deployment resolves {retention.resolved().get(SETTING)!r}")


@pytest.fixture(scope="module")
def deleted(stack, settings):
    """One recorded meeting, summarized, with its recordings deleted."""
    from desktop_video_probe import run_probe
    from meeting_consent_probe import read_meeting, wait_for

    run = run_probe(stack.name, region=stack.region, call_label="recdel-probe")
    call_id = run["call_id"]
    print(f"recording deletion probe meeting: {call_id}")
    assert run["keyframes"], f"{call_id} kept no key frames; is EnableVideoRecording true?"
    summarized = wait_for(
        lambda: (read_meeting(stack, call_id) or {}).get("CallSummaryText"), SUMMARY_TIMEOUT_SECONDS, 5
    )
    assert summarized, f"{call_id} has no summary {SUMMARY_TIMEOUT_SECONDS}s after it ended"
    def deletion_recorded():
        meeting = read_meeting(stack, call_id)
        return meeting if meeting and meeting.get("RecordingsDeletedAt") else None

    meeting = wait_for(deletion_recorded, DELETION_TIMEOUT_SECONDS, 3)
    assert meeting, f"{call_id} was summarized but its recordings were not deleted"
    return {"call_id": call_id, "run": run, **settings}


def _recording_versions(stack, call_id: str) -> list[dict]:
    """Every version and delete marker of this meeting's recordings: its id, as a file name, begins each."""
    s3 = stack.session.client("s3")
    found = []
    name = _posix(call_id)
    for prefix in (f"lma-audio-recordings/{name}", f"lma-video-recordings/{name}"):
        for page in s3.get_paginator("list_object_versions").paginate(
            Bucket=stack.recordings_bucket, Prefix=prefix
        ):
            found += page.get("Versions", []) + page.get("DeleteMarkers", [])
    return found


def test_the_recordings_are_gone_in_every_version_including_late_ones(deleted, stack) -> None:
    from meeting_consent_probe import read_meeting

    call_id = deleted["call_id"]
    # Poll: a recording stored after the deletion is deleted when its link arrives.
    deadline = time.monotonic() + LATE_RECORDING_SECONDS
    remaining = _recording_versions(stack, call_id)
    while remaining and time.monotonic() < deadline:
        time.sleep(10)
        remaining = _recording_versions(stack, call_id)
    assert remaining == [], f"{len(remaining)} recording version(s) remain, e.g. {remaining[:2]}"
    meeting = read_meeting(stack, call_id) or {}
    # A WAV, its raw audio and the screen video, at the least.
    assert meeting.get("RecordingFilesDeleted", 0) >= 3, meeting.get("RecordingFilesDeleted")


def test_the_meeting_records_the_deletion_and_links_no_recording(deleted, stack) -> None:
    from meeting_consent_probe import read_meeting

    meeting = read_meeting(stack, deleted["call_id"]) or {}
    assert _parse(meeting["RecordingsDeletedAt"]) <= datetime.now(timezone.utc)
    assert meeting.get("RecordingDeletionTrigger") in ("summary-written", "recording-added"), meeting
    left = {field: meeting[field] for field in LINK_FIELDS if meeting.get(field)}
    assert left == {}, f"the meeting still links to recordings: {left}"


def test_the_key_frames_and_their_images_are_kept(deleted, stack) -> None:
    from desktop_video_probe import check_images, read_keyframes

    frames = read_keyframes(stack.session.client("dynamodb"), stack.event_table, deleted["call_id"])
    assert len(frames) == len(deleted["run"]["keyframes"]), frames
    images = check_images(stack.session.client("s3"), frames)
    missing = [f"{i['which']} of {i['frame']}" for i in images if not i["exists"]]
    assert missing == [], f"key frame images deleted with the recordings: {missing}"


def test_the_summary_and_the_transcript_are_kept(deleted, stack) -> None:
    from meeting_consent_probe import read_meeting, segments

    meeting = read_meeting(stack, deleted["call_id"]) or {}
    assert meeting.get("CallSummaryText")
    if deleted["transcript_kept"]:
        assert "TranscriptDeletedAt" not in meeting, meeting
        assert segments(stack, deleted["call_id"]), "the transcript went with the recordings"
