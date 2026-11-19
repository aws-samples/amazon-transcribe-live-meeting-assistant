# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Download > Everything (.zip) against a deployed stack.

The test writes a synthetic meeting of its own -- a meeting record with a
summary, one transcript segment, and one key frame with its picture in the
recordings bucket -- asks for its archive through AppSync
(``prepareMeetingArchive``), reads ``getMeetingArchive`` until it is READY,
downloads the archive through the signed link it returns, and checks its files
and README.md. Everything it wrote, and the archive, is deleted afterwards.

Opt-in: ``LMA_TEST_MEETING_ARCHIVE=1``. Needs an assumed role (CI's, or a
developer's SSO role), which AppSync treats as the backend: an IAM user is
read as a person, who is not the synthetic meeting's owner.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import time
import urllib.request
import uuid
import zipfile
from datetime import UTC, datetime

import meeting_consent_probe
import pytest

ENABLED = os.environ.get("LMA_TEST_MEETING_ARCHIVE", "") not in ("", "0", "false")

pytestmark = [
    pytest.mark.optional_feature,
    pytest.mark.skipif(
        not ENABLED, reason="set LMA_TEST_MEETING_ARCHIVE=1 to build a meeting archive"
    ),
]

PREPARE = """
mutation Prepare($CallId: ID!, $Contents: MeetingArchiveContents) {
  prepareMeetingArchive(CallId: $CallId, Contents: $Contents) { ArchiveId Status FileName }
}
"""
READ = """
query Read($CallId: ID!, $ArchiveId: ID!) {
  getMeetingArchive(CallId: $CallId, ArchiveId: $ArchiveId) { Status FileName Size DownloadUrl Message }
}
"""
FRAME_ID = "0000001000-integ0"
# The smallest JPEG header; the archive copies the bytes and never decodes them.
FRAME_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


def _posix(name: str) -> str:
    return re.sub(r"_+$", "", re.sub(r"^_+", "", re.sub(r"[^a-zA-Z0-9_.]", "_", name)))


@pytest.fixture(scope="module")
def stack(stack_name, region):
    found = meeting_consent_probe.Stack.find(stack_name, region)
    if "MeetingArchiveFunction" not in found.ai_resources:
        pytest.skip("the deployed stack has no meeting archive function")
    arn = found.session.client("sts").get_caller_identity().get("Arn", "")
    if ":assumed-role/" not in arn:
        pytest.skip("needs an assumed role, which AppSync treats as the backend")
    return found


@pytest.fixture
def meeting(stack):
    """A synthetic meeting; every item and object written for it is removed afterwards."""
    call_id = f"LMA integ archive {uuid.uuid4().hex[:8]}-{datetime.now(UTC):%Y-%m-%d-%H-%M-%S}"
    posix = _posix(call_id)
    ddb = stack.session.client("dynamodb")
    s3 = stack.session.client("s3")
    frame_key = f"lma-video-keyframes/{posix}/{FRAME_ID}.jpg"
    written = []
    now = datetime.now(UTC).isoformat()
    summary = {
        "SUMMARY": "The archive test meeting.",
        "ACTIONS": "- Check the archive.",
    }
    items = [
        {
            "PK": {"S": f"c#{call_id}"},
            "SK": {"S": f"c#{call_id}"},
            "CallId": {"S": call_id},
            "Owner": {"S": "lma-integ-archive"},
            "CreatedAt": {"S": now},
            "UpdatedAt": {"S": now},
            "CallSummaryText": {"S": json.dumps(summary)},
            "Consent": {"M": {"Mode": {"S": "opt_in"}}},
            "ExpiresAfter": {"N": str(int(time.time()) + 86400)},
        },
        {
            "PK": {"S": f"trs#{call_id}"},
            "SK": {"S": "s#integ-1"},
            "Channel": {"S": "CALLER"},
            "Speaker": {"S": "Integ Speaker"},
            "Transcript": {"S": "Hello from the archive test."},
            "StartTime": {"N": "1"},
            "EndTime": {"N": "3"},
            "IsPartial": {"BOOL": False},
            "ExpiresAfter": {"N": str(int(time.time()) + 86400)},
        },
        {
            "PK": {"S": f"kf#{call_id}"},
            "SK": {"S": f"f#{FRAME_ID}"},
            "ImageUrl": {
                "S": f"https://{stack.recordings_bucket}.s3.{stack.region}.amazonaws.com/{frame_key}"
            },
            "ExpiresAfter": {"N": str(int(time.time()) + 86400)},
        },
    ]
    try:
        s3.put_object(
            Bucket=stack.recordings_bucket,
            Key=frame_key,
            Body=FRAME_BYTES,
            ContentType="image/jpeg",
        )
        written.append(frame_key)
        for item in items:
            ddb.put_item(TableName=stack.event_table, Item=item)
        yield call_id
    finally:
        # Best effort: the items expire on their own, and the archive prefix after a day.
        for item in items:
            with contextlib.suppress(Exception):
                ddb.delete_item(
                    TableName=stack.event_table,
                    Key={"PK": item["PK"], "SK": item["SK"]},
                )
        archive_prefix = f"lma-meeting-archives/{posix}/"
        with contextlib.suppress(Exception):
            listed = s3.list_objects_v2(
                Bucket=stack.recordings_bucket, Prefix=archive_prefix
            ).get("Contents", [])
            written.extend(entry["Key"] for entry in listed)
        for key in written:
            with contextlib.suppress(Exception):
                s3.delete_object(Bucket=stack.recordings_bucket, Key=key)


def test_everything_archive_holds_the_meeting_files_and_lists_them(client, meeting):
    started = client.appsync.graphql(
        PREPARE, {"CallId": meeting, "Contents": "EVERYTHING"}
    )
    archive = started["prepareMeetingArchive"]
    assert archive["Status"] == "PREPARING"

    deadline = time.time() + 300
    status = archive
    while time.time() < deadline:
        status = client.appsync.graphql(
            READ, {"CallId": meeting, "ArchiveId": archive["ArchiveId"]}
        )["getMeetingArchive"]
        if status["Status"] != "PREPARING":
            break
        time.sleep(3)
    assert status["Status"] == "READY", status.get("Message")
    assert status["DownloadUrl"].startswith("https://")

    with urllib.request.urlopen(status["DownloadUrl"], timeout=60) as response:
        body = response.read()
    assert len(body) == status["Size"]
    files = zipfile.ZipFile(io.BytesIO(body))
    assert files.testzip() is None
    names = set(files.namelist())
    for name in (
        "README.md",
        "meeting.json",
        "summary/summary.md",
        "summary/summary.html",
        "transcript/transcript.txt",
        "transcript/transcript.html",
        f"keyframes/{FRAME_ID}.jpg",
    ):
        assert name in names, name
    assert files.read(f"keyframes/{FRAME_ID}.jpg") == FRAME_BYTES
    assert (
        "Hello from the archive test."
        in files.read("transcript/transcript.txt").decode()
    )
    readme = files.read("README.md").decode()
    for name in names - {"README.md"}:
        assert f"`{name}`" in readme, name
    # The backend is neither the owner nor an Admin.
    assert not [n for n in names if n.startswith("consent/")]
    assert "Consent record and evidence" in readme
