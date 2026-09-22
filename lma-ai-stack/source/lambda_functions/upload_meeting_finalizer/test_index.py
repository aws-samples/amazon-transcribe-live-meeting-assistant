# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for Stage 3 of the meeting-upload pipeline (Transcribe state change).

This Lambda is where an uploaded file becomes a readable meeting. It is triggered by
an EventBridge rule that sees every Transcribe job in the account, so the first
thing under test is the tag filter that decides whether a job is ours at all. After
that it reads a transcript from a path it reconstructs from the callId, converts
each utterance into the ``ADD_TRANSCRIPT_SEGMENT`` shape ``call_event_processor``
already consumes from the live websocket transcriber, promotes the media file out of
the pending prefix, and emits ``ADD_S3_RECORDING_URL`` and ``END``.

Three couplings are pinned by value here because nothing checks them at build time:
the transcript key (written by Stage 2, read here), the ``lma:source`` tag (set by
Stage 2, filtered on here), and the Kinesis event shape. Two tests run the real
Stage 1 and Stage 2 code in-process against this module rather than restating their
output. The ordering of the emitted events is asserted too: ``END`` is what starts
the Bedrock summary, so it has to be last.

Also covered: the FAILED branch (a failed transcription must still take the meeting
out of "in progress"), the speaker-label to two-channel mapping, and the
best-effort steps whose failure must not cost the user the meeting.

No AWS calls are made: boto3 is patched before import because the module builds its
clients and table handle at import time.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest
from botocore.exceptions import ClientError

BUCKET = "lma-recordings-bucket"
TABLE = "lma-event-sourcing-table"
STREAM = "lma-call-data-stream"
CALL_ID = "weekly-sync-20250102T030405-abcd1234"
PENDING_KEY = f"lma-uploads-pending/{CALL_ID}/recording.mp3"
TRANSCRIPT_KEY = f"lma-transcripts/{CALL_ID}.transcribe.json"

# The prefixes are left unset so the module defaults are what the tests assert; the
# CloudFormation template passes the same values to all three functions.
os.environ["EVENT_SOURCING_TABLE"] = TABLE
os.environ["CALL_DATA_STREAM_NAME"] = STREAM
os.environ["S3_BUCKET_NAME"] = BUCKET
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

HERE = Path(__file__).resolve().parent
PROCESSOR_DIR = HERE.parent / "upload_meeting_processor"
INITIATOR_DIR = HERE.parent / "upload_meeting_initiator"


def _load_by_path(name: str, path: Path):
    """Import a module from an explicit path under a unique name.

    All three stages are files called ``index.py``, so a plain ``import index``
    would resolve to whichever directory sorts first on sys.path. The stages are
    packaged as separate Lambdas and cannot import one another at runtime; loading
    them side by side is what lets the tests below check their agreement.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


with mock.patch("boto3.client"), mock.patch("boto3.resource"):
    index = _load_by_path("upload_meeting_finalizer_index", HERE / "index.py")
    processor = _load_by_path("upload_meeting_processor_index", PROCESSOR_DIR / "index.py")
    initiator = _load_by_path("upload_meeting_initiator_index", INITIATOR_DIR / "index.py")


OUR_TAGS = [
    {"Key": "lma:callId", "Value": CALL_ID},
    {"Key": "lma:source", "Value": "upload_meeting_processor"},
]

TRANSCRIPT = {
    "results": {
        "transcripts": [{"transcript": "Hello there. Hi."}],
        "audio_segments": [
            {
                "id": 0,
                "transcript": "Hello there.",
                "start_time": "0.0",
                "end_time": "1.5",
                "speaker_label": "spk_0",
            },
            {
                "id": 1,
                "transcript": "Hi.",
                "start_time": "1.6",
                "end_time": "2.25",
                "speaker_label": "spk_1",
            },
        ],
    }
}


def parse_update(call: dict) -> dict:
    """Decode an ``update_item`` call into the attributes it would set.

    The handler builds ``SET #status = :status, UpdatedAt = :now, #k0 = :v0 ...``
    with generated placeholders, so the assertions need the attribute names back.
    """
    names = call["ExpressionAttributeNames"]
    values = call["ExpressionAttributeValues"]
    assignments = call["UpdateExpression"].removeprefix("SET ").split(", ")
    decoded = {}
    for assignment in assignments:
        left, right = (part.strip() for part in assignment.split("="))
        decoded[names.get(left, left)] = values[right]
    return decoded


class FakeTable:
    """In-memory stand-in for the EventSourcing table."""

    def __init__(self, item: dict | None = None) -> None:
        self.item = item
        self.get_calls: list[dict] = []
        self.updates: list[dict] = []

    # boto3 spells its keyword arguments in PascalCase.
    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        self.get_calls.append({"Key": Key, "ConsistentRead": ConsistentRead})
        return {"Item": self.item} if self.item else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        return {}

    @property
    def final(self) -> dict:
        """The attributes set by the last update -- the terminal state."""
        assert self.updates, "no status update was written"
        return parse_update(self.updates[-1])


class FakeKinesis:
    """Captures records instead of putting them on the stream."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def put_record(self, StreamName, PartitionKey, Data):  # noqa: N803
        self.calls.append(
            {
                "StreamName": StreamName,
                "PartitionKey": PartitionKey,
                "payload": json.loads(Data.decode("utf-8")),
            }
        )
        return {}

    @property
    def payloads(self) -> list[dict]:
        return [call["payload"] for call in self.calls]

    @property
    def event_types(self) -> list[str]:
        return [payload["EventType"] for payload in self.payloads]

    def of_type(self, event_type: str) -> list[dict]:
        return [p for p in self.payloads if p["EventType"] == event_type]

    def only(self, event_type: str) -> dict:
        matches = self.of_type(event_type)
        assert len(matches) == 1, f"expected one {event_type}, got {len(matches)}"
        return matches[0]


class FakeS3:
    """Serves one transcript object and records copies and deletes."""

    def __init__(self, transcript: dict | None = None) -> None:
        self.transcript = TRANSCRIPT if transcript is None else transcript
        self.get_calls: list[dict] = []
        self.copies: list[dict] = []
        self.deletes: list[dict] = []
        self.get_error: Exception | None = None
        self.copy_error: Exception | None = None
        self.delete_error: Exception | None = None

    def get_object(self, Bucket, Key):  # noqa: N803
        self.get_calls.append({"Bucket": Bucket, "Key": Key})
        if self.get_error is not None:
            raise self.get_error
        return {"Body": io.BytesIO(json.dumps(self.transcript).encode("utf-8"))}

    def copy_object(self, **kwargs):
        self.copies.append(kwargs)
        if self.copy_error is not None:
            raise self.copy_error
        return {}

    def delete_object(self, **kwargs):
        self.deletes.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error
        return {}


class FakeTranscribe:
    """Answers GetTranscriptionJob with a chosen tag set."""

    def __init__(self, tags: list | None = None, error: Exception | None = None) -> None:
        self.tags = OUR_TAGS if tags is None else tags
        self.error = error
        self.calls: list[str] = []

    def get_transcription_job(self, TranscriptionJobName):  # noqa: N803
        self.calls.append(TranscriptionJobName)
        if self.error is not None:
            raise self.error
        return {
            "TranscriptionJob": {
                "TranscriptionJobName": TranscriptionJobName,
                "Tags": self.tags,
            }
        }


def s3_error(code: str = "NoSuchKey", operation: str = "GetObject") -> ClientError:
    """A ClientError as an S3 client would raise it."""
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def job_row(**overrides) -> dict:
    """An UploadJob row as it stands when Stage 2 has handed off to Stage 3."""
    row = {
        "PK": f"uj#{CALL_ID}",
        "SK": f"uj#{CALL_ID}",
        "RecordType": "UploadJob",
        "CallId": CALL_ID,
        "Status": "TRANSCRIBING",
        "Owner": "bob@example.com",
        "AgentId": "bob@example.com",
        "FromNumber": "Customer",
        "ToNumber": "System",
        "EnableDiarization": False,
        "MaxSpeakers": 4,
        "PendingObjectBucket": BUCKET,
        "PendingObjectKey": PENDING_KEY,
        "TranscriptionJobName": CALL_ID,
    }
    row.update(overrides)
    return {k: v for k, v in row.items() if v is not None}


def eventbridge_event(status: str = "COMPLETED", name: str = CALL_ID, **detail) -> dict:
    """A ``Transcribe Job State Change`` event as EventBridge delivers it."""
    payload = {"TranscriptionJobName": name, "TranscriptionJobStatus": status}
    payload.update(detail)
    return {
        "source": "aws.transcribe",
        "detail-type": "Transcribe Job State Change",
        "detail": {k: v for k, v in payload.items() if v is not None},
    }


class Harness:
    """The four fakes, installed on the module, with the handler bound."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, row: dict | None, transcript=None) -> None:
        self.table = FakeTable(row)
        self.kinesis = FakeKinesis()
        self.s3 = FakeS3(transcript)
        self.transcribe = FakeTranscribe()
        monkeypatch.setattr(index, "job_table", self.table)
        monkeypatch.setattr(index, "kinesis_client", self.kinesis)
        monkeypatch.setattr(index, "s3_client", self.s3)
        monkeypatch.setattr(index, "transcribe_client", self.transcribe)

    def run(self, event: dict | None = None):
        return index.lambda_handler(eventbridge_event() if event is None else event, None)


@pytest.fixture(name="h")
def harness_fixture(monkeypatch: pytest.MonkeyPatch) -> Harness:
    """A finalizer wired to a diarization-off job that transcribed successfully."""
    return Harness(monkeypatch, job_row())


# ---------------------------------------------------------------------------
# Which Transcribe events this Lambda acts on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "detail",
    [{}, {"TranscriptionJobName": CALL_ID}, {"TranscriptionJobStatus": "COMPLETED"}],
    ids=["empty", "no-status", "no-name"],
)
def test_an_event_without_a_job_name_and_status_is_reported_as_nothing_to_do(h, detail) -> None:
    """Both fields are needed to identify the meeting and decide the branch."""
    result = index.lambda_handler({"detail": detail}, None)
    assert result == {"ok": False, "reason": "missing fields"}
    assert (h.transcribe.calls, h.kinesis.calls, h.table.updates) == ([], [], [])


def test_only_jobs_started_by_stage_two_are_finalized(h, monkeypatch) -> None:
    """The EventBridge rule matches every Transcribe job in the account, including
    jobs from other workloads whose names could collide with a callId. The tag is
    the only thing that distinguishes ours."""
    monkeypatch.setattr(index, "transcribe_client", FakeTranscribe(tags=[]))
    assert h.run() == {"ok": True, "skipped": "not-ours"}
    assert (h.kinesis.calls, h.table.updates) == ([], [])


@pytest.mark.parametrize(
    "tags",
    [
        [{"Key": "lma:source", "Value": "somebody-else"}],
        [{"Key": "other", "Value": "upload_meeting_processor"}],
        [{"Key": "lma:callId", "Value": CALL_ID}],
    ],
    ids=["wrong-value", "wrong-key", "call-id-only"],
)
def test_the_source_tag_must_match_in_both_key_and_value(h, monkeypatch, tags) -> None:
    """A partial match is not a match: the key and the value are both checked."""
    monkeypatch.setattr(index, "transcribe_client", FakeTranscribe(tags=tags))
    assert h.run() == {"ok": True, "skipped": "not-ours"}


def test_the_tag_stage_two_sets_is_the_tag_this_stage_looks_for() -> None:
    """Run Stage 2's real code and filter its output with this stage's rule.

    The two Lambdas are packaged separately and share only this string literal.
    If it drifted, every uploaded meeting would transcribe and then stop, with
    Stage 3 logging that the job was not ours.
    """
    transcribe = _CapturingTranscribe()
    with mock.patch.object(processor, "transcribe_client", transcribe):
        processor._start_transcription_job(
            {
                "CallId": CALL_ID,
                "PendingObjectBucket": BUCKET,
                "PendingObjectKey": PENDING_KEY,
            }
        )
    tags = transcribe.params["Tags"]
    assert any(
        t.get("Key") == "lma:source" and t.get("Value") == "upload_meeting_processor" for t in tags
    ), tags


class _CapturingTranscribe:
    """Collects StartTranscriptionJob parameters from the Stage 2 module."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def start_transcription_job(self, **params):
        self.calls.append(params)
        return {}

    @property
    def params(self) -> dict:
        assert len(self.calls) == 1
        return self.calls[0]


def test_a_job_that_cannot_be_described_is_left_alone(h, monkeypatch) -> None:
    """Without the tags there is no way to know the job is ours, so the safe
    answer is to do nothing rather than finalize someone else's meeting."""
    monkeypatch.setattr(
        index,
        "transcribe_client",
        FakeTranscribe(error=s3_error("LimitExceededException", "GetTranscriptionJob")),
    )
    result = h.run()
    assert result["ok"] is False
    assert (h.kinesis.calls, h.table.updates) == ([], [])


def test_a_job_with_no_upload_row_is_skipped(monkeypatch) -> None:
    """The row carries the media location and the owner; there is nothing to
    finalize without it."""
    harness = Harness(monkeypatch, row=None)
    assert harness.run() == {"ok": True, "skipped": "no-job-row"}
    assert harness.kinesis.calls == []


def test_the_row_is_read_with_a_consistent_read(h) -> None:
    """Stage 2 updated this row moments earlier, in the same pipeline."""
    h.run()
    assert h.table.get_calls[0] == {
        "Key": {"PK": f"uj#{CALL_ID}", "SK": f"uj#{CALL_ID}"},
        "ConsistentRead": True,
    }


@pytest.mark.parametrize("status", ["IN_PROGRESS", "QUEUED", "SOMETHING_NEW"])
def test_intermediate_job_states_are_ignored(h, status) -> None:
    """Only the two terminal states are actionable; anything else will be
    followed by another event."""
    assert h.run(eventbridge_event(status=status)) == {"ok": True, "ignored": status}
    assert (h.kinesis.calls, h.table.updates) == ([], [])


# ---------------------------------------------------------------------------
# Transcript segments -- the contract with call_event_processor
# ---------------------------------------------------------------------------


def test_a_segment_event_matches_what_the_live_transcriber_emits(h) -> None:
    """call_event_processor is shared with the websocket transcriber and knows
    nothing about uploads, so this payload is field-for-field what it expects.
    Asserted as a whole dict: an extra or renamed key is as bad as a missing one."""
    h.run()
    # Read positionally rather than by type, so a renamed event type reads as a
    # diff against the expected payload instead of an empty selection.
    first = h.kinesis.payloads[0]
    created_at = first["CreatedAt"]
    assert first == {
        "EventType": "ADD_TRANSCRIPT_SEGMENT",
        "CallId": CALL_ID,
        "Channel": "CALLER",
        "SegmentId": "seg-00000",
        "StartTime": 0.0,
        "EndTime": 1.5,
        "Transcript": "Hello there.",
        "IsPartial": False,
        "Speaker": "spk_0",
        "CreatedAt": created_at,
        "UpdatedAt": created_at,
    }


def test_segments_are_final_not_partial(h) -> None:
    """Batch transcription produces no revisions, so a partial segment would sit
    in the UI forever waiting to be replaced."""
    h.run()
    assert all(seg["IsPartial"] is False for seg in h.kinesis.of_type("ADD_TRANSCRIPT_SEGMENT"))


def test_segment_ids_follow_the_order_of_the_transcript(h) -> None:
    """The UI orders by segment id, and the ids are zero-padded so that string
    ordering and numeric ordering agree past the tenth utterance."""
    transcript = {
        "results": {
            "audio_segments": [
                {"transcript": f"line {i}", "start_time": i, "end_time": i + 1} for i in range(12)
            ]
        }
    }
    h.s3.transcript = transcript
    h.run()
    ids = [seg["SegmentId"] for seg in h.kinesis.of_type("ADD_TRANSCRIPT_SEGMENT")]
    assert ids[:3] == ["seg-00000", "seg-00001", "seg-00002"]
    assert ids[-1] == "seg-00011"
    assert ids == sorted(ids)


def test_segment_times_are_numbers_the_player_can_seek_to(h) -> None:
    """Transcribe reports times as strings; the UI's audio player needs seconds as
    numbers to position the cursor."""
    h.run()
    segments = h.kinesis.of_type("ADD_TRANSCRIPT_SEGMENT")
    assert [(s["StartTime"], s["EndTime"]) for s in segments] == [(0.0, 1.5), (1.6, 2.25)]
    assert all(isinstance(s["StartTime"], float) for s in segments)


def test_a_segment_at_zero_seconds_keeps_its_time(h) -> None:
    """The first utterance of a meeting legitimately starts at 0.0, which is
    falsy: it must survive as 0.0 rather than being treated as absent."""
    h.s3.transcript = {
        "results": {
            "audio_segments": [{"transcript": "first words", "start_time": 0, "end_time": 0}]
        }
    }
    h.run()
    segment = h.kinesis.only("ADD_TRANSCRIPT_SEGMENT")
    assert segment["StartTime"] == 0.0
    assert segment["EndTime"] == 0.0


@pytest.mark.parametrize("bad_time", ["", None, "not-a-number", "NaN-ish"])
def test_an_unusable_time_becomes_zero_rather_than_losing_the_utterance(h, bad_time) -> None:
    """The words matter more than the timestamp: a malformed time must not drop
    the segment or fail the whole meeting."""
    h.s3.transcript = {
        "results": {
            "audio_segments": [
                {"transcript": "words", "start_time": bad_time, "end_time": bad_time}
            ]
        }
    }
    h.run()
    segment = h.kinesis.only("ADD_TRANSCRIPT_SEGMENT")
    assert (segment["StartTime"], segment["EndTime"]) == (0.0, 0.0)
    assert segment["Transcript"] == "words"


def test_empty_utterances_are_dropped(h) -> None:
    """Transcribe emits blank segments for silence; each one would otherwise be a
    blank row in the transcript view."""
    h.s3.transcript = {
        "results": {
            "audio_segments": [
                {"transcript": "  ", "start_time": 0, "end_time": 1},
                {"transcript": "real words", "start_time": 1, "end_time": 2},
                {"transcript": "", "start_time": 2, "end_time": 3},
            ]
        }
    }
    h.run()
    segments = h.kinesis.of_type("ADD_TRANSCRIPT_SEGMENT")
    assert [s["Transcript"] for s in segments] == ["real words"]


def test_surrounding_whitespace_is_trimmed_from_each_utterance(h) -> None:
    h.s3.transcript = {
        "results": {
            "audio_segments": [{"transcript": "  spaced  ", "start_time": 0, "end_time": 1}]
        }
    }
    h.run()
    assert h.kinesis.only("ADD_TRANSCRIPT_SEGMENT")["Transcript"] == "spaced"


def test_segments_go_to_the_call_data_stream_keyed_by_call_id(h) -> None:
    """Partitioning by callId keeps one meeting's events in order on one shard,
    which is what makes the transcript arrive in sequence."""
    h.run()
    assert {c["StreamName"] for c in h.kinesis.calls} == {STREAM}
    assert {c["PartitionKey"] for c in h.kinesis.calls} == {CALL_ID}


def test_an_older_transcript_without_audio_segments_still_produces_one(h) -> None:
    """``audio_segments`` is Transcribe's current representation. If it is absent
    the full text is still worth showing as a single segment rather than showing
    the user an empty meeting."""
    h.s3.transcript = {"results": {"transcripts": [{"transcript": "All of the words."}]}}
    h.run()
    segment = h.kinesis.only("ADD_TRANSCRIPT_SEGMENT")
    assert segment["SegmentId"] == "seg-0"
    assert segment["Transcript"] == "All of the words."
    assert (segment["StartTime"], segment["EndTime"]) == (0.0, 0.0)
    assert segment["Speaker"] == "spk_0"


def test_a_transcript_with_no_words_finalizes_with_no_segments(h) -> None:
    """A silent recording is a valid outcome: the meeting must still be promoted,
    ended and marked complete rather than left in progress."""
    h.s3.transcript = {"results": {"transcripts": [{"transcript": "   "}]}}
    result = h.run()
    assert result["segments"] == 0
    assert h.kinesis.event_types == ["ADD_S3_RECORDING_URL", "END"]
    assert h.table.final["Status"] == "COMPLETED"
    assert h.table.final["SegmentCount"] == 0


# ---------------------------------------------------------------------------
# Speaker labels mapped onto the two LMA channels
# ---------------------------------------------------------------------------


def test_every_utterance_is_one_channel_when_diarization_was_not_requested(h) -> None:
    """Without speaker labelling there is no basis for splitting the transcript,
    so a single-stream transcript is the correct rendering."""
    h.s3.transcript = {
        "results": {
            "audio_segments": [
                {"transcript": "a", "start_time": 0, "end_time": 1, "speaker_label": "spk_0"},
                {"transcript": "b", "start_time": 1, "end_time": 2, "speaker_label": "spk_1"},
            ]
        }
    }
    h.run()
    assert [s["Channel"] for s in h.kinesis.of_type("ADD_TRANSCRIPT_SEGMENT")] == [
        "CALLER",
        "CALLER",
    ]


def test_the_first_diarized_speaker_is_the_caller_and_the_rest_are_the_agent(monkeypatch) -> None:
    """LMA's transcript view has exactly two channels, so more than two speakers
    have to collapse onto them; the original label survives separately so the UI
    can still distinguish the speakers."""
    transcript = {
        "results": {
            "audio_segments": [
                {"transcript": "a", "start_time": 0, "end_time": 1, "speaker_label": "spk_0"},
                {"transcript": "b", "start_time": 1, "end_time": 2, "speaker_label": "spk_1"},
                {"transcript": "c", "start_time": 2, "end_time": 3, "speaker_label": "spk_3"},
            ]
        }
    }
    harness = Harness(monkeypatch, job_row(EnableDiarization=True), transcript)
    harness.run()
    segments = harness.kinesis.of_type("ADD_TRANSCRIPT_SEGMENT")
    assert [s["Channel"] for s in segments] == ["CALLER", "AGENT", "AGENT"]
    assert [s["Speaker"] for s in segments] == ["spk_0", "spk_1", "spk_3"]


@pytest.mark.parametrize(
    ("label", "channel"),
    [("spk_0", "CALLER"), ("ch_0", "CALLER"), ("spk_1", "AGENT"), ("spk_10", "AGENT")],
)
def test_the_channel_mapping_is_by_exact_label(label, channel) -> None:
    """``spk_10`` starts with the same characters as ``spk_1`` and must not be
    matched by a prefix test."""
    assert index._channel_for_speaker(label, True) == channel


def test_an_unlabelled_diarized_segment_is_attributed_to_the_first_speaker(monkeypatch) -> None:
    """Transcribe can omit the label on a segment; a missing Speaker would render
    as an empty name in the transcript view."""
    transcript = {
        "results": {"audio_segments": [{"transcript": "a", "start_time": 0, "end_time": 1}]}
    }
    harness = Harness(monkeypatch, job_row(EnableDiarization=True), transcript)
    harness.run()
    segment = harness.kinesis.only("ADD_TRANSCRIPT_SEGMENT")
    assert segment["Speaker"] == "spk_0"
    assert segment["Channel"] == "CALLER"


# ---------------------------------------------------------------------------
# Reading the transcript Stage 2 asked for
# ---------------------------------------------------------------------------


def test_the_transcript_is_read_from_the_path_stage_two_wrote_it_to(h) -> None:
    """The path is reconstructed from the callId rather than taken from the job's
    TranscriptFileUri, so it is pinned by value on both sides."""
    h.run()
    assert h.s3.get_calls == [{"Bucket": BUCKET, "Key": TRANSCRIPT_KEY}]
    assert index.TRANSCRIPTS_PREFIX == "lma-transcripts/"


def test_stage_two_writes_the_transcript_where_this_stage_reads_it() -> None:
    """Run Stage 2's real code and compare its chosen output key with the key this
    stage asks S3 for. These are separate Lambdas sharing only an environment
    variable, and a mismatch would present as every upload failing to finalize."""
    transcribe = _CapturingTranscribe()
    with mock.patch.object(processor, "transcribe_client", transcribe):
        processor._start_transcription_job(
            {
                "CallId": CALL_ID,
                "PendingObjectBucket": BUCKET,
                "PendingObjectKey": PENDING_KEY,
            }
        )
    assert processor.TRANSCRIPTS_PREFIX == index.TRANSCRIPTS_PREFIX
    assert transcribe.params["OutputKey"] == TRANSCRIPT_KEY
    assert transcribe.params["OutputBucketName"] == BUCKET


def test_the_stack_bucket_is_used_when_the_row_names_none(h, monkeypatch) -> None:
    """Rows written before the bucket was recorded must still finalize."""
    monkeypatch.setattr(index, "job_table", FakeTable(job_row(PendingObjectBucket=None)))
    h.run()
    assert h.s3.get_calls[0]["Bucket"] == BUCKET


def test_an_unreadable_transcript_fails_the_meeting_and_still_ends_it(h) -> None:
    """Leaving the meeting "in progress" forever is the worse outcome: the user
    needs to see that it finished and that something went wrong."""
    h.s3.get_error = s3_error("NoSuchKey")
    result = h.run()
    assert result["ok"] is False
    assert h.kinesis.event_types == ["END"]
    assert h.table.final["Status"] == "FAILED"
    assert "Could not read transcript JSON" in h.table.final["ErrorMessage"]


# ---------------------------------------------------------------------------
# Promoting the media file
# ---------------------------------------------------------------------------


def test_the_media_is_promoted_to_the_name_the_player_expects(h) -> None:
    """LMA's player builds its URL from the callId and the extension, so the
    promoted object is renamed to match rather than keeping the upload's name."""
    h.run()
    assert h.s3.copies == [
        {
            "Bucket": BUCKET,
            "CopySource": {"Bucket": BUCKET, "Key": PENDING_KEY},
            "Key": f"lma-audio-recordings/{CALL_ID}.mp3",
            "MetadataDirective": "COPY",
        }
    ]
    assert index.RECORDINGS_PREFIX == "lma-audio-recordings/"


def test_the_pending_copy_is_removed_once_promoted(h) -> None:
    """Otherwise every upload is stored twice, and a second notification for the
    pending key could restart the pipeline."""
    h.run()
    assert h.s3.deletes == [{"Bucket": BUCKET, "Key": PENDING_KEY}]


@pytest.mark.parametrize(
    ("filename", "promoted"),
    [
        ("recording.mp3", "mp3"),
        ("meeting.notes.m4a", "m4a"),
        ("MOVIE.MP4", "MP4"),
        ("no-extension", "bin"),
    ],
    ids=["mp3", "double-dot", "uppercase", "none"],
)
def test_the_uploaded_extension_is_carried_over(h, monkeypatch, filename, promoted) -> None:
    """The extension is all the player has to go on when choosing a decoder, and a
    file with none still has to end up somewhere predictable."""
    pending = f"lma-uploads-pending/{CALL_ID}/{filename}"
    monkeypatch.setattr(index, "job_table", FakeTable(job_row(PendingObjectKey=pending)))
    h.run()
    assert h.s3.copies[0]["Key"] == f"lma-audio-recordings/{CALL_ID}.{promoted}"


def test_the_recording_url_event_points_at_the_promoted_object(h) -> None:
    """This is the URL the UI turns into a playable link next to the transcript."""
    h.run()
    payload = h.kinesis.only("ADD_S3_RECORDING_URL")
    assert payload["RecordingUrl"] == f"s3://{BUCKET}/lma-audio-recordings/{CALL_ID}.mp3"
    assert payload["CallId"] == CALL_ID
    assert payload["CreatedAt"] and payload["UpdatedAt"]


def test_a_pending_delete_that_fails_does_not_cost_the_recording(h) -> None:
    """The copy already succeeded; the lifecycle rule removes the leftover."""
    h.s3.delete_error = s3_error("AccessDenied", "DeleteObject")
    result = h.run()
    assert result["ok"] is True
    assert result["recordingUrl"] == f"s3://{BUCKET}/lma-audio-recordings/{CALL_ID}.mp3"


def test_a_meeting_whose_media_cannot_be_promoted_is_still_finalized(h) -> None:
    """The transcript is the main artifact: failing to move the audio must not
    cost the user the transcript or leave the meeting in progress."""
    h.s3.copy_error = s3_error("AccessDenied", "CopyObject")
    result = h.run()
    assert result["ok"] is True
    assert result["recordingUrl"] is None
    assert h.kinesis.of_type("ADD_S3_RECORDING_URL") == []
    assert h.kinesis.event_types[-1] == "END"
    assert h.table.final["Status"] == "COMPLETED"


def test_a_missing_recording_url_is_stored_as_an_empty_string(h) -> None:
    """DynamoDB rejects a None attribute value, which would fail the whole update
    and lose the completion status as well as the URL."""
    h.s3.copy_error = s3_error("AccessDenied", "CopyObject")
    h.run()
    assert h.table.final["RecordingS3Url"] == ""


def test_a_row_with_no_media_key_finalizes_the_transcript_alone(h, monkeypatch) -> None:
    """Nothing to promote is not a failure; the transcript still stands."""
    monkeypatch.setattr(index, "job_table", FakeTable(job_row(PendingObjectKey=None)))
    result = h.run()
    assert result["ok"] is True
    assert (h.s3.copies, h.s3.deletes) == ([], [])
    assert h.kinesis.event_types[-1] == "END"


# ---------------------------------------------------------------------------
# Event ordering and the END event
# ---------------------------------------------------------------------------


def test_the_meeting_is_ended_only_after_its_transcript_and_recording(h) -> None:
    """END is what triggers the Bedrock summary orchestrator, so everything the
    summary reads has to be on the stream before it."""
    h.run()
    assert h.kinesis.event_types == [
        "ADD_TRANSCRIPT_SEGMENT",
        "ADD_TRANSCRIPT_SEGMENT",
        "ADD_S3_RECORDING_URL",
        "END",
    ]


def test_the_end_event_carries_no_updated_at(h) -> None:
    """updateCall.request.vtl only applies an update whose UpdatedAt is newer than
    the stored one. The segments emitted just above can carry a later stamp than
    anything this function could set, so the field is left out and the resolver
    supplies its own execution-time value. With a stamp here the condition can
    fail and the meeting stays "In Progress" in the UI."""
    h.run()
    end = h.kinesis.only("END")
    assert "UpdatedAt" not in end
    assert end["CreatedAt"]


def test_the_end_event_repeats_the_meeting_identity(h) -> None:
    """END is processed by the same resolver as START, so it carries the same
    fields; a different Owner attribution here would move the meeting."""
    h.run()
    assert h.kinesis.only("END") == {
        "EventType": "END",
        "CallId": CALL_ID,
        "CustomerPhoneNumber": "Customer",
        "SystemPhoneNumber": "System",
        "AgentId": "bob@example.com",
        "CreatedAt": h.kinesis.only("END")["CreatedAt"],
    }


def test_the_owner_recorded_at_upload_time_owns_the_ended_meeting(h, monkeypatch) -> None:
    """Owner is the authenticated caller; AgentId is client-supplied input."""
    monkeypatch.setattr(
        index,
        "job_table",
        FakeTable(job_row(Owner="owner@example.com", AgentId="claimed@example.com")),
    )
    h.run()
    assert h.kinesis.only("END")["AgentId"] == "owner@example.com"


def test_the_agent_field_is_used_when_no_owner_was_recorded(h, monkeypatch) -> None:
    monkeypatch.setattr(index, "job_table", FakeTable(job_row(Owner=None)))
    h.run()
    assert h.kinesis.only("END")["AgentId"] == "bob@example.com"


# ---------------------------------------------------------------------------
# Terminal states on the job row
# ---------------------------------------------------------------------------


def test_a_finalized_row_records_where_everything_landed(h) -> None:
    """These fields are how an operator answers "did this upload work" without
    reading logs, and the segment count is the only record of transcript size."""
    h.run()
    final = h.table.final
    assert final["Status"] == "COMPLETED"
    assert final["TranscriptKey"] == TRANSCRIPT_KEY
    assert final["RecordingS3Url"] == f"s3://{BUCKET}/lma-audio-recordings/{CALL_ID}.mp3"
    assert final["SegmentCount"] == 2
    assert final["UpdatedAt"]


def test_the_handler_reports_what_it_emitted(h) -> None:
    """The return value is what shows up in the invocation record."""
    assert h.run() == {
        "ok": True,
        "segments": 2,
        "recordingUrl": f"s3://{BUCKET}/lma-audio-recordings/{CALL_ID}.mp3",
    }


def test_a_failed_transcription_is_recorded_with_its_reason(h) -> None:
    """Transcribe's reason is the only explanation the user can be given, and it
    is not retrievable later once the job ages out."""
    result = h.run(eventbridge_event(status="FAILED", FailureReason="Unsupported media format"))
    assert result == {"ok": False, "reason": "Unsupported media format"}
    assert h.table.final["Status"] == "FAILED"
    assert h.table.final["ErrorMessage"] == "Unsupported media format"


def test_a_failed_transcription_still_ends_the_meeting(h) -> None:
    """The meeting is already showing in the list as in progress; without END it
    would stay there indefinitely."""
    h.run(eventbridge_event(status="FAILED", FailureReason="Unsupported media format"))
    assert h.kinesis.event_types == ["END"]
    assert (h.s3.get_calls, h.s3.copies) == ([], [])


def test_a_failure_with_no_reason_still_records_something(h) -> None:
    """DynamoDB rejects a None attribute value, and an empty explanation tells the
    operator nothing."""
    h.run(eventbridge_event(status="FAILED"))
    assert h.table.final["ErrorMessage"] == "Transcribe job failed"


def test_a_long_failure_reason_is_truncated_to_the_stored_limit(h) -> None:
    """DynamoDB items are size-limited and a Transcribe reason can be long."""
    h.run(eventbridge_event(status="FAILED", FailureReason="R" * 4000))
    assert len(h.table.final["ErrorMessage"]) == 1024


# ---------------------------------------------------------------------------
# Full handoff from Stage 1
# ---------------------------------------------------------------------------


def test_a_row_written_by_stage_one_finalizes_end_to_end(monkeypatch) -> None:
    """Stage 1 runs for real and its DynamoDB item is handed to this handler
    exactly as written, with nothing restated in between. This is the test that
    fails if either side renames a field on the row: the media location, the
    diarization flag and the owner attribution all come from Stage 1's code.
    """
    written: list[dict] = []

    class CapturingTable:
        """Captures Stage 1's PutItem."""

        def put_item(self, Item, ConditionExpression=None):  # noqa: N803
            written.append(Item)
            return {}

    class StubS3:
        """Stage 1 signs a URL; the value is irrelevant here."""

        def generate_presigned_url(self, *args, **kwargs):
            return "https://example.invalid/presigned"

    monkeypatch.setattr(initiator, "job_table", CapturingTable())
    monkeypatch.setattr(initiator, "s3_client", StubS3())
    created = initiator.lambda_handler(
        {
            "info": {"fieldName": "createUploadMeeting"},
            "identity": {"username": "carol@example.com", "groups": []},
            "arguments": {
                "input": {
                    "meetingTopic": "Quarterly Review",
                    "agentId": "someone-else@example.com",
                    "filename": "quarterly.mp4",
                    "contentType": "video/mp4",
                    "enableDiarization": True,
                    "maxSpeakers": 3,
                }
            },
        },
        None,
    )
    row = written[0]
    call_id = created["callId"]

    transcript = {
        "results": {
            "audio_segments": [
                {
                    "transcript": "Opening.",
                    "start_time": "0.0",
                    "end_time": "2.0",
                    "speaker_label": "spk_0",
                },
                {
                    "transcript": "Reply.",
                    "start_time": "2.0",
                    "end_time": "4.0",
                    "speaker_label": "spk_2",
                },
            ]
        }
    }
    harness = Harness(monkeypatch, row, transcript)
    monkeypatch.setattr(index, "transcribe_client", FakeTranscribe(tags=OUR_TAGS))

    result = harness.run(eventbridge_event(name=call_id))

    assert result["ok"] is True
    assert harness.s3.get_calls == [
        {"Bucket": BUCKET, "Key": f"lma-transcripts/{call_id}.transcribe.json"}
    ]
    segments = harness.kinesis.of_type("ADD_TRANSCRIPT_SEGMENT")
    # EnableDiarization survived from Stage 1, so the second speaker is the agent.
    assert [s["Channel"] for s in segments] == ["CALLER", "AGENT"]
    assert harness.s3.copies[0]["CopySource"] == {"Bucket": BUCKET, "Key": created["uploadKey"]}
    assert harness.s3.copies[0]["Key"] == f"lma-audio-recordings/{call_id}.mp4"
    # Owner, not the client-supplied agentId, attributes the finished meeting.
    assert harness.kinesis.only("END")["AgentId"] == "carol@example.com"
    assert harness.table.final["Status"] == "COMPLETED"
    assert harness.table.final["SegmentCount"] == 2
