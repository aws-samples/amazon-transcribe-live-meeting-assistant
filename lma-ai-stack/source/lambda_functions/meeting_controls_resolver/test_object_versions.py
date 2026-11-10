# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Deleting a meeting deletes every version of its files in the recordings bucket.

The bucket is versioned, so a file may hold earlier versions and delete markers
besides its current version. Same setup as test_video_recordings.py: no AWS,
and a fake versioned bucket.
"""

from __future__ import annotations

import pytest
from test_delete_metering_records import index
from test_video_recordings import FakeS3, _url

AUDIO = "rec/m1.wav"


def test_every_version_and_delete_marker_under_a_prefix_is_deleted(monkeypatch) -> None:
    s3 = FakeS3(
        [],
        versions={
            "trs/m1-TRANSCRIPT.txt": ["v2", "v1", "v0"],
            # A file whose current version has already expired: only a delete
            # marker and noncurrent versions are left.
            "trs/m1-SUMMARY.txt": ["dm1", "v0"],
            "trs/other-TRANSCRIPT.txt": ["v0"],
        },
    )
    monkeypatch.setattr(index, "s3_client", s3)
    assert index._batch_delete_prefix("bucket", "trs/m1") == 2
    assert sorted(s3.versions) == ["trs/other-TRANSCRIPT.txt"]
    assert ("trs/m1-SUMMARY.txt", "dm1") in s3.deleted_versions


def test_versions_are_deleted_in_batches_of_at_most_1000(monkeypatch) -> None:
    batches = []
    s3 = FakeS3([], versions={"rec/m1.wav": [f"v{n}" for n in range(2500)]})
    original = s3.delete_objects

    def delete_objects(Bucket, Delete):  # noqa: N803
        batches.append(len(Delete["Objects"]))
        return original(Bucket=Bucket, Delete=Delete)

    s3.delete_objects = delete_objects
    monkeypatch.setattr(index, "s3_client", s3)
    assert index._batch_delete_prefix("bucket", "rec/m1") == 1
    assert batches == [1000, 1000, 500]
    assert s3.versions == {}


def test_a_version_s3_did_not_delete_under_a_prefix_raises(monkeypatch) -> None:
    s3 = FakeS3(["trs/m1-TRANSCRIPT.txt"], errors=[{"Key": "trs/m1", "Code": "AccessDenied"}])
    monkeypatch.setattr(index, "s3_client", s3)
    with pytest.raises(RuntimeError, match="AccessDenied"):
        index._batch_delete_prefix("bucket", "trs/m1")


def test_every_version_of_the_recording_on_the_record_is_deleted(monkeypatch) -> None:
    s3 = FakeS3(
        [],
        versions={
            AUDIO: ["v1", "v0"],
            # Begins with the recording's key, but is another file.
            f"{AUDIO}.bak": ["v0"],
        },
    )
    monkeypatch.setattr(index, "s3_client", s3)
    assert index.delete_audio_recording("m1", _url(AUDIO)) == 1
    assert sorted(s3.deleted_versions) == [(AUDIO, "v0"), (AUDIO, "v1")]
    assert sorted(s3.versions) == [f"{AUDIO}.bak"]


def test_a_recording_with_no_version_left_counts_as_none_deleted(monkeypatch) -> None:
    s3 = FakeS3([])
    monkeypatch.setattr(index, "s3_client", s3)
    assert index.delete_audio_recording("m1", _url(AUDIO)) == 0
    assert s3.deleted == []


def test_a_version_of_the_recording_on_the_record_s3_did_not_delete_raises(monkeypatch) -> None:
    s3 = FakeS3([], versions={AUDIO: ["v1", "v0"]}, errors=[{"Key": AUDIO, "Code": "AccessDenied"}])
    monkeypatch.setattr(index, "s3_client", s3)
    with pytest.raises(RuntimeError, match="audio recording of m1"):
        index.delete_audio_recording("m1", _url(AUDIO))
    assert AUDIO in s3.versions


def test_the_meeting_record_is_kept_when_its_recording_is_not_deleted(monkeypatch) -> None:
    """delete_meeting stops before deleteCall, so the record still names the file."""
    s3 = FakeS3([], versions={AUDIO: ["v0"]}, errors=[{"Key": AUDIO, "Code": "AccessDenied"}])
    monkeypatch.setattr(index, "s3_client", s3)
    monkeypatch.setattr(index, "cleanup_virtual_participants", lambda callid: None)
    monkeypatch.setattr(index, "delete_transcript_items", lambda callid: 0)
    monkeypatch.setattr(
        index,
        "get_call_details",
        lambda *a: {"getCall": {"SharedWith": None, "RecordingUrl": _url(AUDIO)}},
    )
    monkeypatch.setattr(index, "delete_recordings_transcripts", lambda callid: None)
    monkeypatch.setattr(index, "delete_metering_records", lambda callid: 0)
    monkeypatch.setattr(index, "delete_keyframes", lambda callid: 0)
    monkeypatch.setattr(index, "delete_consent_evidence", lambda callid: 0)
    monkeypatch.setattr(index, "delete_video_recordings", lambda callid, url: 0)
    executed = []

    class _Session:
        def execute(self, query):
            executed.append(query)
            return {}

    with pytest.raises(RuntimeError, match="audio recording of m1"):
        index.delete_meeting(_Session(), None, "m1", "cls#x", "x", "owner@example.com")
    assert executed == []
