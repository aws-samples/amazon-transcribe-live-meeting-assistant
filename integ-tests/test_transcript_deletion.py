# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Deleting a meeting's transcript once it is summarized, end to end.

Sets the deployment's Retention › "Delete the transcript after the summary" to
Immediately for the length of the module, streams one meeting as Stream Audio
does, waits for its summary, and reads back what the stack kept. See
docs/transcript-deletion.md for the behaviour asserted:

- the meeting's transcript segments are deleted, and its summary is kept;
- the meeting record says when the transcript was deleted, and how many
  segments and files were removed;
- the transcript file the summary step writes for the knowledge base is gone
  from the recordings bucket, every version of it;
- a sweep is scheduled for ten minutes later.

Opt-in. It runs only with ``LMA_TEST_TRANSCRIPT_DELETION=1``, because it
changes a deployment-wide setting while it runs (every meeting that ends in
that time has its transcript deleted), takes about three minutes, and calls
Amazon Transcribe and Bedrock:

    AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \\
      make integ-tests STACK=lma-integtest1 LMA_TEST_TRANSCRIPT_DELETION=1

The setting is written through the configuration store into the active
profile, as the consent test writes its own, and put back in the module's
teardown whatever the tests did.
"""

from __future__ import annotations

import importlib
import os
from datetime import datetime, timezone

import cognito_test_user
import pytest

ENABLED = os.environ.get("LMA_TEST_TRANSCRIPT_DELETION", "") not in ("", "0", "false")

pytestmark = [
    pytest.mark.optional_feature,
    pytest.mark.skipif(not ENABLED, reason="opt-in: set LMA_TEST_TRANSCRIPT_DELETION=1"),
]

SECTION = "retention"
SETTING = "transcriptDeletionAfterSummary"
# A summary takes up to a couple of minutes after the meeting ends.
SUMMARY_TIMEOUT_SECONDS = 300
DELETION_TIMEOUT_SECONDS = 120


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def stack(client):
    for module in ("pycognito", "websockets"):
        try:
            importlib.import_module(module)
        except ImportError:
            pytest.fail(f"LMA_TEST_TRANSCRIPT_DELETION is set but {module} is missing "
                        "(pip install -r integ-tests/requirements.txt)")
    if not cognito_test_user.available(client.region):
        pytest.fail("LMA_TEST_TRANSCRIPT_DELETION is set but there is no Cognito test user — "
                    f"{cognito_test_user.describe_sources()}")
    from meeting_consent_probe import Stack

    found = Stack.find(client.stack_name, client.region)
    if "TranscriptDeletionFunction" not in found.ai_resources:
        pytest.fail(f"{client.stack_name} has no transcript deletion function; deploy a version with it")
    return found


@pytest.fixture(scope="module")
def user(stack):
    from meeting_consent_probe import tokens_for_test_user

    username, tokens = tokens_for_test_user(stack)
    return {"username": username, "tokens": tokens}


@pytest.fixture(scope="module")
def deletion_on(stack):
    """The Retention section with deletion set to Immediately, always put back."""
    from meeting_consent_probe import ConsentSettingsOverride

    override = ConsentSettingsOverride(
        stack, section=SECTION, updated_by="integ-tests/test_transcript_deletion.py"
    )
    override.save()
    print(f"retention: saved the {override.profile!r} profile's section: {override.saved}")
    try:
        override.apply(**{SETTING: "immediately"})
        assert override.resolved().get(SETTING) == "immediately"
        yield override
    finally:
        override.restore()
        print(f"retention: restored; the deployment resolves {override.resolved().get(SETTING)!r}")


@pytest.fixture(scope="module")
def deleted(stack, user, deletion_on):
    """One streamed meeting, summarized and then deleted."""
    from meeting_consent_probe import read_meeting, segments, stream_meeting, wait_for

    session = stream_meeting(stack, user["tokens"], user["username"], label="tdel", consent=None)
    print(session.summary())
    call_id = session.call_id
    summarized = wait_for(
        lambda: (read_meeting(stack, call_id) or {}).get("CallSummaryText"), SUMMARY_TIMEOUT_SECONDS, 5
    )
    assert summarized, f"{call_id} has no summary {SUMMARY_TIMEOUT_SECONDS}s after it ended"
    meeting = wait_for(
        lambda: (lambda m: m if m and m.get("TranscriptDeletedAt") else None)(read_meeting(stack, call_id)),
        DELETION_TIMEOUT_SECONDS,
        3,
    )
    assert meeting, f"{call_id} was summarized but its transcript was not deleted"
    return {"call_id": call_id, "meeting": meeting, "segments": segments(stack, call_id)}


def test_the_transcript_segments_are_deleted(deleted) -> None:
    kept = deleted["segments"]
    assert kept == [], f"{len(kept)} transcript item(s) remain, e.g. {kept[:1]}"


def test_the_summary_is_kept(deleted) -> None:
    assert deleted["meeting"].get("CallSummaryText")


def test_the_meeting_records_the_deletion(deleted) -> None:
    meeting = deleted["meeting"]
    assert meeting.get("TranscriptSegmentsDeleted", 0) > 0, meeting
    assert meeting.get("TranscriptDeletionTrigger") == "summary-written", meeting
    assert _parse(meeting["TranscriptDeletedAt"]) <= datetime.now(timezone.utc)


def test_the_transcript_file_is_gone_in_every_version(deleted, stack) -> None:
    import re

    name = re.sub(r"_+$", "", re.sub(r"^_+", "", re.sub(r"[^a-zA-Z0-9_.]", "_", deleted["call_id"])))
    key = f"lma-transcripts/{name}-TRANSCRIPT.txt"
    listed = stack.session.client("s3").list_object_versions(Bucket=stack.recordings_bucket, Prefix=key)
    remaining = [v for v in listed.get("Versions", []) + listed.get("DeleteMarkers", []) if v["Key"] == key]
    assert remaining == [], f"{key} still has {len(remaining)} version(s)"
    assert deleted["meeting"].get("TranscriptFilesDeleted", 0) >= 1, deleted["meeting"]


def test_a_sweep_is_scheduled_after_an_immediate_deletion(deleted, stack) -> None:
    from meeting_consent_probe import read_meeting, wait_for

    # The sweep is scheduled straight after the deletion is recorded.
    meeting = wait_for(
        lambda: (lambda m: m if m.get("TranscriptDeletionScheduledFor") else None)(
            read_meeting(stack, deleted["call_id"]) or {}
        ),
        30,
        2,
    ) or {}
    due = meeting.get("TranscriptDeletionScheduledFor")
    assert due, f"no sweep is scheduled for {deleted['call_id']}: {meeting}"
    minutes = (_parse(due) - _parse(meeting["TranscriptDeletedAt"])).total_seconds() / 60
    assert 9 <= minutes <= 12, f"the sweep is {minutes:.1f} minutes after the deletion"
