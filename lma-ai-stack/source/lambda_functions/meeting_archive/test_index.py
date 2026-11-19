# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the meeting archive function (no AWS calls).

Asserted here: an archive is prepared for, and read back by, exactly the
callers ``getCall`` answers, and only by the caller who asked for it (or an
Admin); the consent record and evidence are in it only for the meeting's owner
or an Admin; the transcript is left out once deleted under the Retention
settings; every session's recordings are in it; objects are streamed in chunks
and the archive uploaded in parts, never held whole; README.md lists every file
and what was left out; an archive over the size limit fails with a message.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import zipfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

HERE = Path(__file__).resolve().parent
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["EVENT_SOURCING_TABLE"] = "calls"
os.environ["RECORDINGS_BUCKET"] = "rec-bucket"
os.environ.setdefault("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "example")

_SPEC = importlib.util.spec_from_file_location("meeting_archive_index", HERE / "index.py")
index = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index)

CALL = "Weekly sync-2026-11-03-14-05-09-123"
POSIX = index.posixify_filename(CALL)
HOST = "https://rec-bucket.s3.us-east-1.amazonaws.com"
ADMIN = {"claims": {"cognito:groups": ["Admin"], "sub": "a"}, "username": "admin@example.com"}
OWNER = {"claims": {"sub": "o"}, "username": "owner@example.com"}
FRIEND = {"claims": {"sub": "f"}, "username": "friend@example.com"}
STRANGER = {"claims": {"sub": "x"}, "username": "x@example.com"}
BACKEND = {"userArn": "arn:aws:sts::123456789012:assumed-role/role/session", "username": "AROA"}


def s(value):
    return {"S": value}


def meeting_item(**extra) -> Dict[str, Any]:
    found = {
        "PK": s(f"c#{CALL}"),
        "SK": s(f"c#{CALL}"),
        "CallId": s(CALL),
        "Owner": s("owner@example.com"),
        "SharedWith": {"L": [s("friend@example.com")]},
        "CreatedAt": s("2026-11-03T14:05:09.123Z"),
        "TotalConversationDurationMillis": {"N": "61000"},
        "CallSummaryText": s(
            json.dumps(
                {
                    "DETAILS": "- <script>alert(1)</script> [x](javascript:alert(1)) [doc](https://docs.example.com/a)",
                    "SUMMARY": "**Plan** agreed. ![Slide 1](keyframe:0000030000-ab12cd)",
                    "CHAPTERS": "- [00:00] Welcome",
                }
            )
        ),
        "RecordingUrl": s(f"{HOST}/lma-audio-recordings/{POSIX}.wav"),
        "VideoRecordingUrl": s(f"{HOST}/lma-video-recordings/{POSIX}.webm"),
        "Consent": {"M": {"Mode": s("opt_in"), "Status": s("granted")}},
        "ExpiresAfter": {"N": "1999999999"},
    }
    found.update(extra)
    return found


class FakeDynamo:
    def __init__(
        self,
        meeting: Dict[str, Any] | None,
        partitions: Dict[str, List[Dict[str, Any]]] | None = None,
    ):
        self.meeting = meeting
        self.partitions = partitions or {}

    def get_item(self, **kwargs):
        return {"Item": self.meeting} if self.meeting else {}

    def query(self, **kwargs):
        pk = kwargs["ExpressionAttributeValues"][":pk"]["S"]
        return {"Items": self.partitions.get(pk, [])}


class Body:
    """An S3 body that can only be read in chunks; it records the biggest chunk asked for."""

    def __init__(self, size: int, fill: bytes):
        self.size, self.fill, self.biggest = size, fill, 0

    def read(self, *_args):
        raise AssertionError("an object was read whole")

    def iter_chunks(self, chunk_size):
        self.biggest = max(self.biggest, chunk_size)
        sent = 0
        while sent < self.size:
            n = min(chunk_size, self.size - sent)
            sent += n
            yield self.fill * n

    def close(self):
        pass


class NoSuchKey(Exception):
    pass


class FakeS3:
    def __init__(self, objects: Dict[str, int]):
        self.objects = dict(objects)
        self.stored: Dict[str, bytes] = {}
        self.uploads: Dict[str, Dict[str, Any]] = {}
        self.part_sizes: List[int] = []
        self.bodies: List[Body] = []
        self.aborted: List[str] = []
        self.exceptions = type("E", (), {"NoSuchKey": NoSuchKey})

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise NoSuchKey(Key)
        return {"ContentLength": self.objects[Key]}

    def get_object(self, Bucket, Key):
        if Key in self.stored:
            return {"Body": io.BytesIO(self.stored[Key])}
        if Key not in self.objects:
            raise NoSuchKey(Key)
        body = Body(self.objects[Key], b"v" if Key.endswith((".wav", ".webm")) else b"k")
        self.bodies.append(body)
        return {"Body": body}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.stored[Key] = Body

    def create_multipart_upload(self, Bucket, Key, ContentType):
        upload_id = f"u{len(self.uploads)}"
        self.uploads[upload_id] = {"Key": Key, "parts": {}}
        return {"UploadId": upload_id}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        self.uploads[UploadId]["parts"][PartNumber] = Body
        self.part_sizes.append(len(Body))
        return {"ETag": f"e{PartNumber}"}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        parts = self.uploads[UploadId]["parts"]
        self.stored[Key] = b"".join(parts[p["PartNumber"]] for p in MultipartUpload["Parts"])

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.aborted.append(Key)

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://signed.example.com/{Params['Key']}?expires={ExpiresIn}"

    def get_paginator(self, _name):
        objects = {**self.objects, **{k: len(v) for k, v in self.stored.items()}}

        class Pages:
            def paginate(self, Bucket, Prefix):
                yield {
                    "Contents": [
                        {"Key": k, "Size": v} for k, v in objects.items() if k.startswith(Prefix)
                    ]
                }

        return Pages()


class FakeLambda:
    def __init__(self):
        self.invocations: List[Dict[str, Any]] = []

    def invoke(self, **kwargs):
        self.invocations.append(kwargs)


class Context:
    invoked_function_arn = "stack-MeetingArchive"


FRAME_KEY = f"lma-video-keyframes/{POSIX}/0000030000-ab12cd.jpg"
ATTACHMENT_KEY = f"lma-chat-attachments/{POSIX}/att1/plan.pdf"
EVIDENCE_KEY = f"lma-consent-evidence/{POSIX}/evidence-1.jpg"
OBJECTS = {
    f"lma-audio-recordings/{POSIX}.wav": 3 * 1024 * 1024 + 17,
    f"lma-video-recordings/{POSIX}.webm": 700_000,
    FRAME_KEY: 40_000,
    ATTACHMENT_KEY: 12_000,
    EVIDENCE_KEY: 9_000,
}


def partitions():
    return {
        f"trs#{CALL}": [
            {
                "Channel": s("CALLER"),
                "Speaker": s("Sarah"),
                "Transcript": s("Hello <b>all</b>"),
                "StartTime": {"N": "1.5"},
                "EndTime": {"N": "3"},
                "IsPartial": {"BOOL": False},
            },
            {
                "Channel": s("CALLER"),
                "Speaker": s("Sarah"),
                "Transcript": s("partial"),
                "StartTime": {"N": "4"},
                "EndTime": {"N": "5"},
                "IsPartial": {"BOOL": True},
            },
        ],
        f"kf#{CALL}": [{"SK": s("f#0000030000-ab12cd"), "ImageUrl": s(f"{HOST}/{FRAME_KEY}")}],
        f"att#{CALL}": [
            {
                "SK": s("a#att1"),
                "FileName": s("plan.pdf"),
                "Status": s("STORED"),
                "S3Key": s(ATTACHMENT_KEY),
            },
            {
                "SK": s("a#att2"),
                "FileName": s("../../etc/passwd"),
                "Status": s("STORED"),
                "S3Key": s("lma-transcripts/x.txt"),
            },
        ],
    }


@pytest.fixture
def fakes(monkeypatch):
    def install(meeting=None, objects=None, parts=None):
        dynamo = FakeDynamo(
            meeting_item() if meeting is None else meeting, partitions() if parts is None else parts
        )
        s3 = FakeS3(OBJECTS if objects is None else objects)
        lam = FakeLambda()
        monkeypatch.setattr(index, "DYNAMODB", dynamo)
        monkeypatch.setattr(index, "S3", s3)
        monkeypatch.setattr(index, "LAMBDA", lam)
        monkeypatch.setattr(index, "PART_BYTES", 1024 * 1024)
        monkeypatch.setattr(index, "READ_CHUNK_BYTES", 256 * 1024)
        return dynamo, s3, lam

    return install


def prepare(identity, contents="EVERYTHING"):
    event = {
        "arguments": {"CallId": CALL, "Contents": contents},
        "identity": identity,
        "info": {"fieldName": "prepareMeetingArchive"},
    }
    return index.handler(event, Context())


def get(identity, archive_id):
    event = {
        "arguments": {"CallId": CALL, "ArchiveId": archive_id},
        "identity": identity,
        "info": {"fieldName": "getMeetingArchive"},
    }
    return index.handler(event, Context())


def run_build(lam):
    payload = json.loads(lam.invocations[-1]["Payload"])
    assert lam.invocations[-1]["InvocationType"] == "Event"
    return index.handler(payload, Context())


def archive_of(s3, answer) -> zipfile.ZipFile:
    status = json.loads(s3.stored[index.status_key(CALL, answer["ArchiveId"])])
    return zipfile.ZipFile(io.BytesIO(s3.stored[status["ArchiveKey"]]))


def build_for(fakes, identity, contents="EVERYTHING", **kwargs):
    _dynamo, s3, lam = fakes(**kwargs)
    answer = prepare(identity, contents)
    run_build(lam)
    return s3, answer, get(identity, answer["ArchiveId"])


# --- access ------------------------------------------------------------------------


@pytest.mark.parametrize("identity", [OWNER, FRIEND, ADMIN, BACKEND])
def test_archive_is_prepared_for_the_callers_get_call_answers(fakes, identity):
    _dynamo, s3, lam = fakes()
    answer = prepare(identity)
    assert answer["Status"] == "PREPARING"
    assert answer["DownloadUrl"] is None
    assert len(lam.invocations) == 1
    assert lam.invocations[0]["FunctionName"] == Context.invoked_function_arn


@pytest.mark.parametrize("meeting", [None, {}])
def test_stranger_and_missing_meeting_get_the_same_refusal(fakes, meeting):
    _dynamo, _s3, lam = fakes(meeting=meeting_item() if meeting is None else meeting)
    identity = STRANGER if meeting is None else OWNER
    with pytest.raises(RuntimeError, match="do not have access to this meeting"):
        prepare(identity)
    assert lam.invocations == []


def test_an_archive_is_read_back_only_by_the_caller_who_asked_for_it_or_an_admin(fakes):
    _dynamo, s3, lam = fakes()
    answer = prepare(FRIEND)
    run_build(lam)
    assert get(FRIEND, answer["ArchiveId"])["Status"] == "READY"
    assert get(ADMIN, answer["ArchiveId"])["Status"] == "READY"
    with pytest.raises(RuntimeError, match="no such archive"):
        get(OWNER, answer["ArchiveId"])
    with pytest.raises(RuntimeError, match="do not have access"):
        get(STRANGER, answer["ArchiveId"])
    with pytest.raises(RuntimeError, match="required"):
        get(FRIEND, "../other")


def test_build_leg_runs_only_without_an_identity(fakes):
    _dynamo, s3, lam = fakes()
    answer = prepare(OWNER)
    with pytest.raises(RuntimeError, match="unknown field"):
        index.handler(
            {
                "Trigger": "build",
                "CallId": CALL,
                "ArchiveId": answer["ArchiveId"],
                "identity": OWNER,
            },
            Context(),
        )
    assert (
        json.loads(s3.stored[index.status_key(CALL, answer["ArchiveId"])])["Status"] == "PREPARING"
    )


# --- contents ------------------------------------------------------------------------


@pytest.mark.parametrize("identity", [OWNER, ADMIN])
def test_consent_record_and_evidence_are_included_for_the_owner_and_admins(fakes, identity):
    s3, answer, ready = build_for(fakes, identity)
    names = archive_of(s3, answer).namelist()
    assert "consent/consent.json" in names
    assert "consent/evidence/evidence-1.jpg" in names
    assert "Consent" not in json.loads(archive_of(s3, answer).read("meeting.json"))


@pytest.mark.parametrize("identity", [FRIEND, BACKEND])
def test_consent_record_and_evidence_are_left_out_for_everyone_else(fakes, identity):
    s3, answer, _ready = build_for(fakes, identity)
    archive = archive_of(s3, answer)
    assert not [n for n in archive.namelist() if n.startswith("consent/")]
    assert "Consent" not in json.loads(archive.read("meeting.json"))
    assert "Consent record and evidence" in archive.read("README.md").decode()


def test_everything_holds_summary_transcript_recordings_frames_attachments_and_details(fakes):
    s3, answer, ready = build_for(fakes, OWNER)
    assert ready["Status"] == "READY"
    assert ready["DownloadUrl"].startswith("https://signed.example.com/lma-meeting-archives/")
    assert ready["FileName"] == f"Meeting-{POSIX}.zip"
    archive = archive_of(s3, answer)
    assert archive.testzip() is None
    names = archive.namelist()
    for name in (
        "summary/summary.html",
        "summary/summary.md",
        "transcript/transcript.txt",
        "transcript/transcript.html",
        "recordings/audio-session-1.wav",
        "recordings/video-session-1.webm",
        "keyframes/0000030000-ab12cd.jpg",
        "chat-attachments/att1-plan.pdf",
        "meeting.json",
        "README.md",
    ):
        assert name in names, name
    # An attachment whose key is outside the meeting's folder is not read.
    assert not [n for n in names if "passwd" in n]
    assert (
        len(archive.read("recordings/audio-session-1.wav"))
        == OBJECTS[f"lma-audio-recordings/{POSIX}.wav"]
    )
    details = json.loads(archive.read("meeting.json"))
    assert details["Participants"] == ["Sarah"]
    assert "PK" not in details and "ExpiresAfter" not in details
    assert (
        archive.read("transcript/transcript.txt").decode() == "Sarah [00:01.5]: Hello <b>all</b>\n"
    )
    summary_md = archive.read("summary/summary.md").decode()
    assert (
        summary_md.index("## SUMMARY")
        < summary_md.index("## DETAILS")
        < summary_md.index("## CHAPTERS")
    )
    assert "![Slide 1](../keyframes/0000030000-ab12cd.jpg)" in summary_md


def test_summary_web_page_escapes_the_text_and_keeps_only_web_links(fakes):
    s3, answer, _ready = build_for(fakes, OWNER)
    page = archive_of(s3, answer).read("summary/summary.html").decode()
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert 'href="javascript' not in page
    assert (
        '<a href="https://docs.example.com/a" rel="noopener noreferrer" target="_blank">doc</a>'
        in page
    )
    assert "default-src &#x27;none&#x27;" in page or "default-src 'none'" in page
    transcript = archive_of(s3, answer).read("transcript/transcript.html").decode()
    assert "Hello &lt;b&gt;all&lt;/b&gt;" in transcript


def test_transcript_is_left_out_once_deleted_under_retention(fakes):
    meeting = meeting_item(TranscriptDeletedAt=s("2026-11-10T00:00:00Z"))
    s3, answer, _ready = build_for(fakes, OWNER, meeting=meeting)
    archive = archive_of(s3, answer)
    assert not [n for n in archive.namelist() if n.startswith("transcript/")]
    assert "deleted under the Retention settings" in archive.read("README.md").decode()


def test_every_session_recording_is_included(fakes):
    earlier = f"{HOST}/lma-audio-recordings/{POSIX}-first.wav"
    meeting = meeting_item(
        RecordingUrls={"L": [s(earlier), s(earlier), s(f"{HOST}/lma-audio-recordings/{POSIX}.wav")]}
    )
    objects = {**OBJECTS, f"lma-audio-recordings/{POSIX}-first.wav": 1000}
    s3, answer, _ready = build_for(fakes, OWNER, meeting=meeting, objects=objects)
    names = archive_of(s3, answer).namelist()
    assert "recordings/audio-session-1.wav" in names and "recordings/audio-session-2.wav" in names
    assert "recordings/audio-session-3.wav" not in names


def test_a_recording_outside_the_recordings_prefix_is_not_read(fakes):
    meeting = meeting_item(RecordingUrl=s(f"{HOST}/lma-transcripts/{POSIX}-TRANSCRIPT.txt"))
    s3, answer, _ready = build_for(fakes, OWNER, meeting=meeting)
    archive = archive_of(s3, answer)
    assert not [n for n in archive.namelist() if n.startswith("recordings/audio")]
    assert "Audio recording, session 1" in archive.read("README.md").decode()


def test_key_frames_and_attachments_archives_hold_only_those_files(fakes):
    s3, answer, ready = build_for(fakes, OWNER, contents="KEY_FRAMES")
    assert ready["FileName"] == f"Meeting-{POSIX}-key-frames.zip"
    assert archive_of(s3, answer).namelist() == ["keyframes/0000030000-ab12cd.jpg", "README.md"]
    s3, answer, _ready = build_for(fakes, OWNER, contents="CHAT_ATTACHMENTS")
    assert archive_of(s3, answer).namelist() == ["chat-attachments/att1-plan.pdf", "README.md"]


def test_readme_lists_every_file_and_what_was_left_out(fakes):
    s3, answer, _ready = build_for(fakes, FRIEND)
    archive = archive_of(s3, answer)
    readme = archive.read("README.md").decode()
    for name in archive.namelist():
        assert f"`{name}`" in readme, name
    assert "## Left out" in readme
    assert "Slides (PDF)" in readme and "Summary (Word, PDF)" in readme
    assert "deleted after a day" in readme


# --- streaming and limits -------------------------------------------------------------


def test_objects_are_streamed_in_chunks_and_the_archive_uploaded_in_parts(fakes):
    s3, answer, _ready = build_for(fakes, OWNER)
    assert s3.bodies, "objects were read"
    assert max(body.biggest for body in s3.bodies) <= index.READ_CHUNK_BYTES
    # Every part but the last is exactly one part long; several parts were sent.
    assert len(s3.part_sizes) >= 2
    assert all(size == index.PART_BYTES for size in s3.part_sizes[:-1])


def test_the_writer_holds_at_most_one_part_and_one_chunk(fakes):
    _dynamo, s3, _lam = fakes()
    writer = index.MultipartWriter(
        s3, "rec-bucket", "lma-meeting-archives/x/y.zip", part_bytes=1000, limit=10**9
    )
    for _ in range(50):
        writer.write(b"x" * 300)
    writer.complete()
    assert writer.most_buffered < 1000 + 300
    assert len(s3.stored["lma-meeting-archives/x/y.zip"]) == 15000


def test_an_archive_over_the_size_limit_fails_with_a_message(fakes, monkeypatch):
    _dynamo, s3, lam = fakes()
    monkeypatch.setattr(index, "MAX_ARCHIVE_BYTES", 1024 * 1024)
    answer = prepare(OWNER)
    run_build(lam)
    failed = get(OWNER, answer["ArchiveId"])
    assert failed["Status"] == "FAILED"
    assert "more than the" in failed["Message"] and "GB" in failed["Message"]
    assert failed["DownloadUrl"] is None
    assert s3.uploads == {}


def test_the_writer_stops_and_the_upload_is_aborted_past_the_limit(fakes, monkeypatch):
    _dynamo, s3, lam = fakes()
    answer = prepare(OWNER)
    # The plan fits, but the stream outgrows the limit (as a file that grew would).
    monkeypatch.setattr(
        index,
        "MultipartWriter",
        lambda *a, **k: ORIGINAL_WRITER(*a, **{**k, "limit": 2 * 1024 * 1024}),
    )
    run_build(lam)
    failed = get(OWNER, answer["ArchiveId"])
    assert failed["Status"] == "FAILED"
    assert "GB an archive can hold" in failed["Message"]
    assert len(s3.aborted) == 1


ORIGINAL_WRITER = index.MultipartWriter


def test_a_build_that_never_finished_reads_as_failed(fakes):
    _dynamo, s3, _lam = fakes()
    answer = prepare(OWNER)
    key = index.status_key(CALL, answer["ArchiveId"])
    status = json.loads(s3.stored[key])
    s3.stored[key] = json.dumps({**status, "CreatedAt": "2020-01-01T00:00:00Z"}).encode()
    assert get(OWNER, answer["ArchiveId"])["Status"] == "FAILED"


def test_archives_are_written_under_the_archive_prefix_in_the_meetings_folder(fakes):
    s3, answer, _ready = build_for(fakes, OWNER)
    written = [k for k in s3.stored]
    assert written and all(
        k.startswith(f"lma-meeting-archives/{POSIX}/{answer['ArchiveId']}/") for k in written
    )


def test_unknown_contents_is_refused(fakes):
    fakes()
    with pytest.raises(RuntimeError, match="required"):
        prepare(OWNER, contents="ALL_FILES")


def test_asking_again_returns_the_archive_already_preparing_or_ready(fakes):
    _dynamo, s3, lam = fakes()
    first = prepare(OWNER)
    again = prepare(OWNER)
    assert again["ArchiveId"] == first["ArchiveId"]
    assert len(lam.invocations) == 1
    run_build(lam)
    ready = prepare(OWNER)
    assert ready["ArchiveId"] == first["ArchiveId"]
    assert ready["Status"] == "READY" and ready["DownloadUrl"]
    assert len(lam.invocations) == 1


def test_another_caller_or_other_contents_gets_an_archive_of_its_own(fakes):
    _dynamo, _s3, lam = fakes()
    first = prepare(OWNER)
    assert prepare(FRIEND)["ArchiveId"] != first["ArchiveId"]
    assert prepare(OWNER, contents="KEY_FRAMES")["ArchiveId"] != first["ArchiveId"]
    assert len(lam.invocations) == 3


def test_a_stale_or_old_archive_is_not_offered_again(fakes):
    _dynamo, s3, lam = fakes()
    first = prepare(OWNER)
    key = index.status_key(CALL, first["ArchiveId"])
    status = json.loads(s3.stored[key])
    s3.stored[key] = json.dumps({**status, "CreatedAt": "2020-01-01T00:00:00Z"}).encode()
    assert prepare(OWNER)["ArchiveId"] != first["ArchiveId"]
    assert len(lam.invocations) == 2


def test_a_missing_status_reads_as_no_archive_whatever_s3_answers(fakes, monkeypatch):
    _dynamo, s3, _lam = fakes()

    def denied(**_kwargs):
        raise index.ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    monkeypatch.setattr(s3, "get_object", lambda Bucket, Key: denied())
    with pytest.raises(RuntimeError, match="no such archive"):
        get(OWNER, "b" * 32)


def test_meeting_json_holds_only_listed_fields(fakes):
    meeting = meeting_item(InternalNote=s("not for the archive"))
    s3, answer, _ready = build_for(fakes, OWNER, meeting=meeting)
    details = json.loads(archive_of(s3, answer).read("meeting.json"))
    assert "InternalNote" not in details and "CallSummaryText" not in details
    assert details["CallId"] == CALL and details["Owner"] == "owner@example.com"
