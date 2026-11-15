# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Deleting a meeting deletes the audio recording its RecordingUrl names.

Same setup as test_video_recordings.py: no AWS, and a fake S3 client that
records what it is asked to delete. The test environment's audio prefix is
``rec/`` (test_delete_metering_records.py).
"""

from __future__ import annotations

import pytest
from test_delete_metering_records import index
from test_video_recordings import FakeS3, _url

# The Virtual Participant's file name: the meeting name and the time written,
# not the CallId.
VP_FILE = "rec/Team_sync_2026_11_07T13_52_15_023Z.wav"


def test_the_virtual_participants_audio_recording_is_deleted(monkeypatch) -> None:
    s3 = FakeS3([VP_FILE])
    monkeypatch.setattr(index, "s3_client", s3)
    assert index.delete_audio_recording("Team sync-2026-11-07-13-22-49-204", _url(VP_FILE)) == 1
    assert s3.deleted == [VP_FILE]


def test_an_unencoded_stored_url_is_followed(monkeypatch) -> None:
    key = "rec/Team_sync-2026-11-07-13-22-49-204.wav"
    s3 = FakeS3([key])
    monkeypatch.setattr(index, "s3_client", s3)
    assert (
        index.delete_audio_recording("Team sync-2026-11-07-13-22-49-204", _url(key, encode=False))
        == 1
    )
    assert s3.deleted == [key]


def test_a_stored_url_outside_the_audio_recordings_is_not_followed(monkeypatch) -> None:
    s3 = FakeS3([])
    monkeypatch.setattr(index, "s3_client", s3)
    for url in (
        "https://other-bucket.s3.us-east-1.amazonaws.com/rec/x.wav",
        _url("lma-video-recordings/x.mp4"),
        _url("rec/nested/x.wav"),
        _url("rec/"),
        "not a url",
        None,
    ):
        assert index.delete_audio_recording("m1", url) == 0, url
    assert s3.deleted == []


def test_an_audio_recording_s3_did_not_delete_is_logged_and_raises(monkeypatch, caplog) -> None:
    s3 = FakeS3([VP_FILE], errors=[{"Key": VP_FILE, "Code": "AccessDenied"}])
    monkeypatch.setattr(index, "s3_client", s3)
    with pytest.raises(RuntimeError, match="audio recording of m1"):
        index.delete_audio_recording("m1", _url(VP_FILE))
    assert "AccessDenied" in caplog.text and VP_FILE in caplog.text


def test_deleting_a_meeting_deletes_the_audio_named_on_its_record(monkeypatch) -> None:
    """delete_meeting reads RecordingUrl from the meeting and passes it on."""
    calls = []
    audio_url = _url(VP_FILE)
    monkeypatch.setattr(index, "cleanup_virtual_participants", lambda callid: None)
    monkeypatch.setattr(index, "delete_transcript_items", lambda callid: 0)
    monkeypatch.setattr(
        index,
        "get_call_details",
        lambda *a: {"getCall": {"SharedWith": None, "RecordingUrl": audio_url}},
    )
    monkeypatch.setattr(index, "delete_recordings_transcripts", lambda callid: None)
    monkeypatch.setattr(index, "delete_metering_records", lambda callid: 0)
    monkeypatch.setattr(index, "delete_keyframes", lambda callid: 0)
    monkeypatch.setattr(index, "delete_consent_evidence", lambda callid: 0)
    monkeypatch.setattr(index, "delete_video_recordings", lambda callid, url: 0)
    monkeypatch.setattr(
        index, "delete_audio_recording", lambda callid, url: calls.append((callid, url))
    )

    class _Field:
        def __getattr__(self, _name):
            return self

        def args(self, **_kwargs):
            return self

        def select(self, *_args):
            return self

    monkeypatch.setattr(index, "dsl_gql", lambda *a: None)
    monkeypatch.setattr(index, "DSLMutation", lambda *a: None)

    class _Session:
        def execute(self, _query):
            return {}

    index.delete_meeting(_Session(), _Field(), "m1", "cls#x", "x", "owner@example.com")
    assert calls == [("m1", audio_url)]
