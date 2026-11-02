# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Deleting a meeting deletes its video recording and any leftover segments.

Same setup as test_delete_metering_records.py: no AWS, the AppSync client and
the GraphQL DSL replaced before import, and a fake S3 client that lists from a
fixed set of keys and records what it is asked to delete.
"""

from __future__ import annotations

from urllib.parse import quote

from test_delete_metering_records import index

STAMP = "2026-10-30T14-05-09-123Z"


class FakePaginator:
    def __init__(self, keys):
        self.keys = keys

    def paginate(self, Bucket, Prefix):  # noqa: N803
        assert Bucket == "bucket"
        yield {"Contents": [{"Key": k} for k in sorted(self.keys) if k.startswith(Prefix)]}


class FakeS3:
    def __init__(self, keys, errors=None):
        self.keys = set(keys)
        self.deleted = []
        self.errors = errors or []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self.keys)

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        assert Bucket == "bucket"
        self.deleted.extend(o["Key"] for o in Delete["Objects"])
        return {"Errors": self.errors} if self.errors else {}


def _url(key, encode=True):
    return f"https://bucket.s3.us-east-1.amazonaws.com/{quote(key, safe='') if encode else key}"


def test_the_virtual_participants_recording_and_its_segments_are_deleted(monkeypatch) -> None:
    vp_file = f"lma-video-recordings/call_Team_sync_ab_{STAMP}.mp4"
    s3 = FakeS3(
        [
            vp_file,
            "lma-video-chunks/Team sync+ab/segment_000.ts",
            "lma-video-chunks/Team sync+ab/segment_001.ts",
            # Another meeting whose id begins the same way keeps its segments.
            "lma-video-chunks/Team sync+ab 2/segment_000.ts",
        ]
    )
    monkeypatch.setattr(index, "s3_client", s3)
    assert index.delete_video_recordings("Team sync+ab", _url(vp_file)) == 1
    assert sorted(s3.deleted) == [
        "lma-video-chunks/Team sync+ab/segment_000.ts",
        "lma-video-chunks/Team sync+ab/segment_001.ts",
        vp_file,
    ]


def test_the_desktop_apps_recording_is_deleted(monkeypatch) -> None:
    desktop_file = f"lma-video-recordings/Team_sync_ab_{STAMP}.mp4"
    s3 = FakeS3([desktop_file])
    monkeypatch.setattr(index, "s3_client", s3)
    # The transcriber stores the key unencoded.
    assert index.delete_video_recordings("Team sync+ab", _url(desktop_file, encode=False)) == 1
    assert s3.deleted == [desktop_file]


def test_only_the_file_on_the_record_is_deleted_not_files_with_a_matching_name(
    monkeypatch,
) -> None:
    """Different CallIds can give one file name, so names are never searched.

    "Team sync+ab" and "Team sync ab" both name their VP file call_Team_sync_ab_*
    and their desktop file Team_sync_ab_*; and meeting "X"'s VP prefix call_X_
    also begins the desktop file of a meeting whose id is "call_X".
    """
    mine = f"lma-video-recordings/call_Team_sync_ab_{STAMP}.mp4"
    others = [
        "lma-video-recordings/call_Team_sync_ab_2026-10-30T15-00-00-000Z.mp4",
        f"lma-video-recordings/Team_sync_ab_{STAMP}.mp4",
        f"lma-video-recordings/call_Team_sync_ab_{STAMP}.mp4.bak",
    ]
    s3 = FakeS3([mine, *others])
    monkeypatch.setattr(index, "s3_client", s3)
    assert index.delete_video_recordings("Team sync+ab", _url(mine)) == 1
    assert s3.deleted == [mine]


def test_a_stored_url_outside_the_video_recordings_is_not_followed(monkeypatch) -> None:
    s3 = FakeS3([])
    monkeypatch.setattr(index, "s3_client", s3)
    for url in (
        "https://other-bucket.s3.us-east-1.amazonaws.com/lma-video-recordings/x.mp4",
        _url("lma-audio-recordings/m1.wav"),
        _url("lma-video-recordings/nested/x.mp4"),
        _url("lma-video-recordings/"),
        "not a url",
        None,
    ):
        assert index._video_key_from_url(url) is None, url
    assert index.delete_video_recordings("m1", _url("lma-transcripts/m1.json")) == 0
    assert s3.deleted == []


def test_a_meeting_without_video_deletes_nothing(monkeypatch) -> None:
    s3 = FakeS3([f"lma-video-recordings/call_m1_{STAMP}.mp4"])
    monkeypatch.setattr(index, "s3_client", s3)
    assert index.delete_video_recordings("m1", None) == 0
    assert s3.deleted == []


def test_a_recording_s3_did_not_delete_is_logged_and_not_counted(monkeypatch, caplog) -> None:
    key = f"lma-video-recordings/call_m1_{STAMP}.mp4"
    s3 = FakeS3([key], errors=[{"Key": key, "Code": "AccessDenied"}])
    monkeypatch.setattr(index, "s3_client", s3)
    assert index.delete_video_recordings("m1", _url(key)) == 0
    assert "AccessDenied" in caplog.text and key in caplog.text


def test_deleting_a_meeting_deletes_the_video_named_on_its_record(monkeypatch) -> None:
    """delete_meeting reads VideoRecordingUrl from the meeting and passes it on."""
    calls = []
    video_url = _url(f"lma-video-recordings/call_m1_{STAMP}.mp4")
    monkeypatch.setattr(index, "cleanup_virtual_participants", lambda callid: None)
    monkeypatch.setattr(index, "_delete_all_transcript_segments", lambda *a: None)
    monkeypatch.setattr(
        index,
        "get_call_details",
        lambda *a: {"getCall": {"SharedWith": None, "VideoRecordingUrl": video_url}},
    )
    monkeypatch.setattr(index, "delete_recordings_transcripts", lambda callid: None)
    monkeypatch.setattr(index, "delete_metering_records", lambda callid: 0)
    monkeypatch.setattr(index, "delete_keyframes", lambda callid: 0)
    monkeypatch.setattr(index, "delete_consent_evidence", lambda callid: 0)
    monkeypatch.setattr(
        index, "delete_video_recordings", lambda callid, url: calls.append((callid, url))
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
    assert calls == [("m1", video_url)]
