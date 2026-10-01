# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for Stage 2 of the meeting-upload pipeline (S3 ObjectCreated).

This Lambda is the join between the three stages. It parses a callId back out of
the S3 key that upload_meeting_initiator constructed, reads the ``uj#<callId>``
row that Lambda wrote, puts the meeting on the Kinesis call-data stream in the
exact shape the live websocket transcriber produces, and starts a Transcribe batch
job whose name, output location and tags are the only things
upload_meeting_finalizer has to find it by.

None of those three couplings is a function call, so none of them breaks at build
time. The tests therefore pin each by value, and two of them run the real Stage 1
code in-process: one round-trips a key through both modules, and one feeds an item
produced by the initiator straight into this handler. A renamed field would
otherwise show up in production as an upload that completes and then never
transcribes, with no error anywhere.

The rest of the coverage is the status machine (which statuses are reprocessed and
which are skipped on an S3 redelivery), the diarization settings handed to
Transcribe, and partial-failure reporting across a multi-record event.

No AWS calls are made: boto3 is patched before import because the module builds
its clients and table handle at import time.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest
from botocore.exceptions import ClientError

BUCKET = "lma-recordings-bucket"
TABLE = "lma-event-sourcing-table"
STREAM = "lma-call-data-stream"
CALL_ID = "weekly-sync-20250102T030405-abcd1234"
PENDING_KEY = f"lma-uploads-pending/{CALL_ID}/recording.mp3"

# Prefixes and the default language are left unset so the module defaults are what
# the tests assert; the CloudFormation template passes the same literals.
os.environ["EVENT_SOURCING_TABLE"] = TABLE
os.environ["CALL_DATA_STREAM_NAME"] = STREAM
os.environ["S3_BUCKET_NAME"] = BUCKET
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

HERE = Path(__file__).resolve().parent
INITIATOR_DIR = HERE.parent / "upload_meeting_initiator"


def _load_by_path(name: str, path: Path):
    """Import a module from an explicit path under a unique name.

    Both Stage 1 and Stage 2 are files called ``index.py``, so a plain
    ``import index`` would resolve to whichever directory sorts first on sys.path.
    Both are needed here, so both are named.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


with mock.patch("boto3.client"), mock.patch("boto3.resource"):
    index = _load_by_path("upload_meeting_processor_index", HERE / "index.py")
    initiator = _load_by_path("upload_meeting_initiator_index", INITIATOR_DIR / "index.py")


FROZEN_NOW = datetime(2025, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """A datetime whose ``now()`` never moves, so stamps are exact values."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW


def parse_update(call: dict) -> dict:
    """Decode an ``update_item`` call into the attributes it would set.

    The handler builds ``SET #status = :status, UpdatedAt = :now, #k0 = :v0 ...``
    with generated placeholders, so the assertions below need the real attribute
    names back rather than the placeholder text.
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
        self.update_error: Exception | None = None
        self.get_error: Exception | None = None

    # boto3 spells its keyword arguments in PascalCase.
    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        if self.get_error is not None:
            raise self.get_error
        self.get_calls.append({"Key": Key, "ConsistentRead": ConsistentRead})
        return {"Item": self.item} if self.item else {}

    def update_item(self, **kwargs):
        if self.update_error is not None:
            raise self.update_error
        self.updates.append(kwargs)
        return {}

    @property
    def statuses(self) -> list[str]:
        """The Status values written, in order."""
        return [parse_update(u)["Status"] for u in self.updates]

    def update_for(self, status: str) -> dict:
        """The decoded attributes of the update that set ``status``."""
        for call in self.updates:
            decoded = parse_update(call)
            if decoded["Status"] == status:
                return decoded
        raise AssertionError(f"no update set Status={status}: {self.statuses}")


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


class FakeTranscribe:
    """Captures StartTranscriptionJob parameters; can raise a chosen error."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    def start_transcription_job(self, **params):
        self.calls.append(params)
        if self.error is not None:
            raise self.error
        return {}

    @property
    def params(self) -> dict:
        assert len(self.calls) == 1, f"expected one job start, got {len(self.calls)}"
        return self.calls[0]


def transcribe_error(code: str) -> ClientError:
    """A ClientError as the Transcribe client would raise it."""
    return ClientError({"Error": {"Code": code, "Message": code}}, "StartTranscriptionJob")


def job_row(**overrides) -> dict:
    """An UploadJob row as Stage 1 writes it, before the upload completes."""
    row = {
        "PK": f"uj#{CALL_ID}",
        "SK": f"uj#{CALL_ID}",
        "RecordType": "UploadJob",
        "CallId": CALL_ID,
        "Status": "PENDING_UPLOAD",
        "Owner": "bob@example.com",
        "AgentId": "bob@example.com",
        "FromNumber": "Customer",
        "ToNumber": "System",
        "Filename": "recording.mp3",
        "ContentType": "audio/mpeg",
        "EnableDiarization": False,
        "MaxSpeakers": 4,
        "PendingObjectBucket": BUCKET,
        "PendingObjectKey": PENDING_KEY,
    }
    row.update(overrides)
    return {k: v for k, v in row.items() if v is not None}


def s3_event(key: str = PENDING_KEY, bucket: str = BUCKET, size: int = 2048) -> dict:
    """One ObjectCreated record, in the shape S3 delivers it."""
    return {
        "Records": [
            {
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key, "size": size},
                }
            }
        ]
    }


@pytest.fixture(name="env")
def env_fixture(monkeypatch: pytest.MonkeyPatch):
    """Install the fakes and the frozen clock; the table starts with a fresh row."""
    table = FakeTable(job_row())
    kinesis = FakeKinesis()
    transcribe = FakeTranscribe()
    monkeypatch.setattr(index, "job_table", table)
    monkeypatch.setattr(index, "kinesis_client", kinesis)
    monkeypatch.setattr(index, "transcribe_client", transcribe)
    monkeypatch.setattr(index, "datetime", _FrozenDatetime)
    return table, kinesis, transcribe


# ---------------------------------------------------------------------------
# Recovering the callId from the S3 key written by Stage 1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (PENDING_KEY, CALL_ID),
        ("lma-uploads-pending/call-1/a.mp3", "call-1"),
        ("lma-uploads-pending/call-1/nested/a.mp3", "call-1"),
        ("lma-uploads-pending/call-1/", None),
        ("lma-uploads-pending/call-1", None),
        ("lma-uploads-pending/", None),
        ("lma-uploads-pending//a.mp3", None),
        ("lma-audio-recordings/call-1/a.mp3", None),
        ("lma-transcripts/call-1.transcribe.json", None),
        ("", None),
    ],
    ids=[
        "generated-id",
        "simple",
        "nested",
        "no-filename",
        "no-separator",
        "prefix-only",
        "empty-id",
        "recordings-prefix",
        "transcripts-prefix",
        "empty",
    ],
)
def test_the_call_id_is_the_first_segment_under_the_pending_prefix(key, expected) -> None:
    """Stage 1 builds ``<prefix><callId>/<filename>``; anything else is not one of
    our uploads and must resolve to no callId rather than to a wrong one."""
    assert index._call_id_from_key(key) == expected


def test_the_key_stage_one_builds_round_trips_back_to_its_call_id() -> None:
    """Run both stages' real code against each other. Stage 1's key builder and
    this stage's parser are in separately packaged Lambdas and share only the
    UPLOADS_PENDING_PREFIX environment variable, so their agreement is pinned here
    rather than discovered when an upload silently never starts transcribing."""
    assert initiator.UPLOADS_PENDING_PREFIX == index.UPLOADS_PENDING_PREFIX
    for call_id, filename in [
        ("weekly-sync-20250102T030405-abcd1234", "recording.mp3"),
        ("caller.supplied_id-9", "My_Meeting_1_.mp4"),
    ]:
        key = initiator._build_object_key(call_id, filename)
        assert index._call_id_from_key(key) == call_id


def test_the_pending_prefix_is_the_one_the_bucket_notification_filters_on() -> None:
    """The S3 notification, the lifecycle rule and all three Lambdas use this
    literal; the transcript output lands in the same bucket under a different
    prefix, so a wrong value here would either miss uploads or pick up
    Transcribe's own output objects."""
    assert index.UPLOADS_PENDING_PREFIX == "lma-uploads-pending/"


# ---------------------------------------------------------------------------
# Which notifications this Lambda acts on
# ---------------------------------------------------------------------------


def test_notifications_for_another_bucket_are_ignored(env) -> None:
    """The handler is wired to one bucket; anything else is not ours to process."""
    table, kinesis, transcribe = env
    result = index.lambda_handler(s3_event(bucket="somebody-elses-bucket"), None)
    assert result == {"processed": 1}
    assert (table.get_calls, table.updates, kinesis.calls, transcribe.calls) == ([], [], [], [])


@pytest.mark.parametrize(
    "key",
    [
        f"lma-transcripts/{CALL_ID}.transcribe.json",
        f"lma-audio-recordings/{CALL_ID}.mp3",
        "some/other/object.txt",
    ],
    ids=["transcript-output", "promoted-recording", "unrelated"],
)
def test_objects_outside_the_pending_prefix_are_ignored(env, key) -> None:
    """Stage 2 and Stage 3 both write into this same bucket -- the Transcribe
    output and the promoted recording included -- so a notification for those must
    not start a second transcription job."""
    table, kinesis, transcribe = env
    index.lambda_handler(s3_event(key=key), None)
    assert (table.get_calls, kinesis.calls, transcribe.calls) == ([], [], [])


def test_an_unparseable_pending_key_is_skipped(env) -> None:
    """A file dropped directly under the prefix has no callId to look up."""
    table, kinesis, transcribe = env
    result = index.lambda_handler(s3_event(key="lma-uploads-pending/stray.mp3"), None)
    assert result == {"processed": 1}
    assert (table.get_calls, kinesis.calls, transcribe.calls) == ([], [], [])


def test_an_upload_with_no_job_row_is_skipped_rather_than_failing(env, monkeypatch) -> None:
    """Orphaned objects are expected (the lifecycle rule removes them) and must
    not start a meeting whose caller-supplied metadata is unknown, nor make the
    invocation fail and be retried until the event expires."""
    monkeypatch.setattr(index, "job_table", FakeTable(item=None))
    _, kinesis, transcribe = env
    result = index.lambda_handler(s3_event(), None)
    assert result == {"processed": 1}
    assert (kinesis.calls, transcribe.calls) == ([], [])


def test_the_job_row_is_read_with_a_consistent_read(env) -> None:
    """The row was written moments earlier by the AppSync mutation; an eventually
    consistent read can miss it and the upload would look orphaned."""
    table, _, _ = env
    index.lambda_handler(s3_event(), None)
    assert table.get_calls[0]["ConsistentRead"] is True


# ---------------------------------------------------------------------------
# Status machine and redelivery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["TRANSCRIBING", "COMPLETED", "FAILED"])
def test_a_job_that_has_already_advanced_is_not_processed_again(env, monkeypatch, status) -> None:
    """S3 can deliver a notification more than once, and an operator may replay
    one. Reprocessing would emit a second START for a meeting already in the list
    and restart transcription of a meeting that is already finished."""
    monkeypatch.setattr(index, "job_table", FakeTable(job_row(Status=status)))
    _, kinesis, transcribe = env
    result = index.lambda_handler(s3_event(), None)
    assert result == {"processed": 1}
    assert (kinesis.calls, transcribe.calls) == ([], [])


def test_a_pending_job_moves_through_uploaded_to_transcribing(env) -> None:
    """The intermediate UPLOADED state is what an operator sees while Transcribe
    is being started, and TRANSCRIBING is what Stage 3 expects to find."""
    table, _, _ = env
    index.lambda_handler(s3_event(), None)
    assert table.statuses == ["UPLOADED", "TRANSCRIBING"]


def test_the_uploaded_marker_records_the_object_size_and_the_time(env) -> None:
    """Size and time are the operator's evidence that the browser's PUT finished,
    and the only record of how large the media was."""
    table, _, _ = env
    index.lambda_handler(s3_event(size=4096), None)
    uploaded = table.update_for("UPLOADED")
    assert uploaded["UploadedObjectSize"] == 4096
    assert uploaded["UploadedAt"] == FROZEN_NOW.isoformat()
    assert uploaded["UpdatedAt"] == FROZEN_NOW.isoformat()


def test_the_transcribing_marker_records_the_job_name_stage_three_resolves(env) -> None:
    """Stage 3 is triggered by job name; this is the stored link between the two."""
    table, _, _ = env
    index.lambda_handler(s3_event(), None)
    assert table.update_for("TRANSCRIBING")["TranscriptionJobName"] == CALL_ID


def test_a_failure_leaves_the_job_in_error_with_the_reason(env, monkeypatch) -> None:
    """The row is the only place an operator can see why an upload stalled."""
    table, _, _ = env
    monkeypatch.setattr(index, "transcribe_client", FakeTranscribe(transcribe_error("BadRequest")))
    with pytest.raises(RuntimeError):
        index.lambda_handler(s3_event(), None)
    assert table.statuses[-1] == "ERROR"
    assert "BadRequest" in table.update_for("ERROR")["ErrorMessage"]


def test_a_long_error_is_truncated_to_the_stored_limit(env, monkeypatch) -> None:
    """DynamoDB item size is finite and a Transcribe reason can be long."""
    table, _, _ = env
    monkeypatch.setattr(index, "transcribe_client", FakeTranscribe(transcribe_error("E" * 4000)))
    with pytest.raises(RuntimeError):
        index.lambda_handler(s3_event(), None)
    assert len(table.update_for("ERROR")["ErrorMessage"]) == 1024


def test_a_status_write_that_fails_does_not_stop_the_pipeline(env, monkeypatch) -> None:
    """Status is progress reporting, not the pipeline itself: losing it must not
    cost the user their transcription."""
    table, kinesis, transcribe = env
    table.update_error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}},
        "UpdateItem",
    )
    result = index.lambda_handler(s3_event(), None)
    assert result == {"processed": 1}
    assert len(kinesis.payloads) == 1
    assert transcribe.params["TranscriptionJobName"] == CALL_ID


# ---------------------------------------------------------------------------
# The START event -- the contract with call_event_processor
# ---------------------------------------------------------------------------


def test_the_start_event_matches_what_the_live_transcriber_emits(env) -> None:
    """call_event_processor is shared with the websocket transcriber and is not
    aware of uploads, so this payload has to be field-for-field what it expects.
    Asserted as a whole dict: an extra or renamed key is as bad as a missing one."""
    _, kinesis, _ = env
    index.lambda_handler(s3_event(), None)
    assert kinesis.payloads == [
        {
            "EventType": "START",
            "CallId": CALL_ID,
            "CustomerPhoneNumber": "Customer",
            "SystemPhoneNumber": "System",
            "AgentId": "bob@example.com",
            "CreatedAt": FROZEN_NOW.isoformat(),
        }
    ]


def test_the_start_event_goes_to_the_call_data_stream_keyed_by_call_id(env) -> None:
    """Partitioning by callId keeps one meeting's events in order on one shard."""
    _, kinesis, _ = env
    index.lambda_handler(s3_event(), None)
    assert kinesis.calls[0]["StreamName"] == STREAM
    assert kinesis.calls[0]["PartitionKey"] == CALL_ID


def test_the_start_event_carries_no_tokens_so_the_agent_id_becomes_the_owner(env) -> None:
    """call_event_processor derives Owner from a JWT when one is present and from
    AgentId otherwise. Uploads have no user token at this point, so AgentId must
    carry the Cognito caller for access control to attribute the meeting to them."""
    _, kinesis, _ = env
    index.lambda_handler(s3_event(), None)
    payload = kinesis.payloads[0]
    assert payload["AgentId"] == "bob@example.com"
    assert not {"AccessToken", "IdToken", "RefreshToken"} & set(payload)


def test_the_owner_recorded_by_stage_one_wins_over_the_agent_field(env, monkeypatch) -> None:
    """Owner is the authenticated caller; AgentId is client-supplied input."""
    monkeypatch.setattr(
        index,
        "job_table",
        FakeTable(job_row(Owner="owner@example.com", AgentId="claimed@example.com")),
    )
    _, kinesis, _ = env
    index.lambda_handler(s3_event(), None)
    assert kinesis.payloads[0]["AgentId"] == "owner@example.com"


def test_the_agent_field_is_used_when_no_owner_was_recorded(env, monkeypatch) -> None:
    """Rows written before Owner existed must still produce an attributable
    meeting rather than one owned by nobody."""
    monkeypatch.setattr(index, "job_table", FakeTable(job_row(Owner=None)))
    _, kinesis, _ = env
    index.lambda_handler(s3_event(), None)
    assert kinesis.payloads[0]["AgentId"] == "bob@example.com"


def test_the_users_meeting_time_becomes_the_meetings_creation_time(env, monkeypatch) -> None:
    """An uploaded recording is of a meeting that already happened; the list sorts
    on CreatedAt, so it must be the meeting's time, not the upload's."""
    monkeypatch.setattr(
        index, "job_table", FakeTable(job_row(MeetingDateTime="2024-12-25T09:30:00Z"))
    )
    _, kinesis, _ = env
    index.lambda_handler(s3_event(), None)
    assert kinesis.payloads[0]["CreatedAt"] == "2024-12-25T09:30:00Z"


def test_the_upload_time_is_used_when_no_meeting_time_was_given(env) -> None:
    """CreatedAt is required downstream, so it always has a value."""
    _, kinesis, _ = env
    index.lambda_handler(s3_event(), None)
    assert kinesis.payloads[0]["CreatedAt"] == FROZEN_NOW.isoformat()


def test_the_party_labels_fall_back_when_the_row_omits_them(env, monkeypatch) -> None:
    """These render directly in the meetings list, which has no empty state."""
    monkeypatch.setattr(index, "job_table", FakeTable(job_row(FromNumber=None, ToNumber=None)))
    _, kinesis, _ = env
    index.lambda_handler(s3_event(), None)
    assert kinesis.payloads[0]["CustomerPhoneNumber"] == "Customer"
    assert kinesis.payloads[0]["SystemPhoneNumber"] == "System"


# ---------------------------------------------------------------------------
# StartTranscriptionJob -- the contract with Stage 3
# ---------------------------------------------------------------------------


def test_the_transcription_job_is_named_after_the_call(env) -> None:
    """Stage 3 receives only a job name in its EventBridge event and treats it as
    the callId, so the two must be the same string."""
    _, _, transcribe = env
    index.lambda_handler(s3_event(), None)
    assert transcribe.params["TranscriptionJobName"] == CALL_ID


def test_the_media_is_read_from_the_location_recorded_on_the_row(env) -> None:
    """The row, not the notification, is the record of what was uploaded."""
    _, _, transcribe = env
    index.lambda_handler(s3_event(), None)
    assert transcribe.params["Media"] == {"MediaFileUri": f"s3://{BUCKET}/{PENDING_KEY}"}


def test_the_transcript_is_written_where_stage_three_reads_it(env) -> None:
    """Stage 3 reads a path it reconstructs from the callId rather than following
    the job's TranscriptFileUri, so the output location is fixed here by value."""
    _, _, transcribe = env
    index.lambda_handler(s3_event(), None)
    assert transcribe.params["OutputBucketName"] == BUCKET
    assert transcribe.params["OutputKey"] == f"lma-transcripts/{CALL_ID}.transcribe.json"
    assert index.TRANSCRIPTS_PREFIX == "lma-transcripts/"


def test_the_job_is_tagged_so_stage_three_can_recognize_it(env) -> None:
    """Stage 3's EventBridge rule sees every Transcribe job in the account; the
    ``lma:source`` tag is how it tells ours apart from anyone else's."""
    _, _, transcribe = env
    index.lambda_handler(s3_event(), None)
    assert transcribe.params["Tags"] == [
        {"Key": "lma:callId", "Value": CALL_ID},
        {"Key": "lma:source", "Value": "upload_meeting_processor"},
    ]


def test_a_call_id_that_cannot_name_a_transcribe_job_is_reported(env, monkeypatch) -> None:
    """Transcribe accepts 1-200 characters of ``[a-zA-Z0-9._-]``. Failing before
    the API call keeps the reason in the job row instead of in a raw API error."""
    table = FakeTable(job_row(CallId="not a legal job name"))
    monkeypatch.setattr(index, "job_table", table)
    _, _, transcribe = env
    with pytest.raises(RuntimeError):
        index.lambda_handler(s3_event(), None)
    assert transcribe.calls == []
    assert table.statuses[-1] == "ERROR"


# ---------------------------------------------------------------------------
# Diarization settings
# ---------------------------------------------------------------------------


def test_diarization_is_requested_with_the_users_speaker_count(env, monkeypatch) -> None:
    """The speaker count the user chose in the UI has to reach Transcribe; Stage 3
    maps the resulting labels onto the two LMA channels."""
    monkeypatch.setattr(
        index, "job_table", FakeTable(job_row(EnableDiarization=True, MaxSpeakers=6))
    )
    _, _, transcribe = env
    index.lambda_handler(s3_event(), None)
    assert transcribe.params["Settings"] == {"ShowSpeakerLabels": True, "MaxSpeakerLabels": 6}


@pytest.mark.parametrize(
    ("requested", "sent"), [(1, 2), (0, 4), (31, 30), (999, 30), (None, 4), (2, 2), (30, 30)]
)
def test_the_speaker_count_is_held_inside_the_range_transcribe_accepts(
    env, monkeypatch, requested, sent
) -> None:
    """Transcribe rejects anything outside 2-30 with a ValidationException, which
    would lose the whole upload over a stale or hand-edited row."""
    monkeypatch.setattr(
        index, "job_table", FakeTable(job_row(EnableDiarization=True, MaxSpeakers=requested))
    )
    _, _, transcribe = env
    index.lambda_handler(s3_event(), None)
    assert transcribe.params["Settings"]["MaxSpeakerLabels"] == sent


def test_no_speaker_settings_are_sent_when_diarization_is_off(env) -> None:
    """Speaker labelling costs time and money and changes Stage 3's channel
    mapping, so it is only ever on at the user's request."""
    _, _, transcribe = env
    index.lambda_handler(s3_event(), None)
    assert "Settings" not in transcribe.params


def test_the_language_the_user_chose_is_used(env, monkeypatch) -> None:
    monkeypatch.setattr(index, "job_table", FakeTable(job_row(LanguageCode="es-US")))
    _, _, transcribe = env
    index.lambda_handler(s3_event(), None)
    assert transcribe.params["LanguageCode"] == "es-US"


def test_the_language_defaults_when_the_row_does_not_name_one(env) -> None:
    """LanguageCode is optional in the mutation, but required by Transcribe."""
    _, _, transcribe = env
    index.lambda_handler(s3_event(), None)
    assert index.DEFAULT_LANGUAGE_CODE == "en-US"
    assert transcribe.params["LanguageCode"] == "en-US"


# ---------------------------------------------------------------------------
# Idempotency and partial failure
# ---------------------------------------------------------------------------


def test_an_already_started_transcription_is_accepted_as_done(env, monkeypatch) -> None:
    """A retried invocation that reaches Transcribe a second time gets a
    ConflictException. The job it names is the one we wanted, so the pipeline
    continues to TRANSCRIBING instead of failing and retrying forever."""
    table = FakeTable(job_row())
    monkeypatch.setattr(index, "job_table", table)
    monkeypatch.setattr(
        index, "transcribe_client", FakeTranscribe(transcribe_error("ConflictException"))
    )
    result = index.lambda_handler(s3_event(), None)
    assert result == {"processed": 1}
    assert table.statuses == ["UPLOADED", "TRANSCRIBING"]
    assert table.update_for("TRANSCRIBING")["TranscriptionJobName"] == CALL_ID


def test_every_record_is_attempted_before_the_invocation_fails(env, monkeypatch) -> None:
    """S3 batches notifications. One bad upload must not block the others, and the
    invocation must still fail so Lambda retries -- the failing record's own
    idempotency guard is what stops the successful ones being redone."""
    rows = {
        "call-bad": job_row(CallId="call-bad", PendingObjectKey="lma-uploads-pending/call-bad/a"),
        "call-good": job_row(
            CallId="call-good", PendingObjectKey="lma-uploads-pending/call-good/a"
        ),
    }

    class Router(FakeTable):
        """Serves a different row per callId and remembers each update."""

        def get_item(self, Key, ConsistentRead=False):  # noqa: N803
            self.get_calls.append({"Key": Key, "ConsistentRead": ConsistentRead})
            return {"Item": rows[Key["PK"].removeprefix("uj#")]}

    class SelectiveTranscribe(FakeTranscribe):
        """Fails only the job named call-bad."""

        def start_transcription_job(self, **params):
            self.calls.append(params)
            if params["TranscriptionJobName"] == "call-bad":
                raise transcribe_error("InternalFailureException")
            return {}

    table = Router()
    transcribe = SelectiveTranscribe()
    monkeypatch.setattr(index, "job_table", table)
    monkeypatch.setattr(index, "transcribe_client", transcribe)
    event = {
        "Records": [
            s3_event(key="lma-uploads-pending/call-bad/a.mp3")["Records"][0],
            s3_event(key="lma-uploads-pending/call-good/a.mp3")["Records"][0],
        ]
    }

    with pytest.raises(RuntimeError, match="1 of 2 records failed"):
        index.lambda_handler(event, None)

    assert [c["TranscriptionJobName"] for c in transcribe.calls] == ["call-bad", "call-good"]
    # The bad record is marked ERROR; the good one still reaches TRANSCRIBING.
    updated = [(u["Key"]["PK"], parse_update(u)["Status"]) for u in table.updates]
    assert ("uj#call-bad", "ERROR") in updated
    assert ("uj#call-good", "TRANSCRIBING") in updated


def test_the_failure_report_names_the_affected_meetings(env, monkeypatch) -> None:
    """The raised message is all CloudWatch sees, so it has to identify which
    callId failed and how many records were involved."""
    monkeypatch.setattr(
        index, "transcribe_client", FakeTranscribe(transcribe_error("InternalFailureException"))
    )
    with pytest.raises(RuntimeError) as raised:
        index.lambda_handler(s3_event(), None)
    message = str(raised.value)
    assert "1 of 1 records failed" in message
    assert CALL_ID in message


def test_an_empty_event_is_a_no_op(env) -> None:
    """A warm invocation with nothing to do must not raise."""
    _, kinesis, transcribe = env
    assert index.lambda_handler({}, None) == {"processed": 0}
    assert (kinesis.calls, transcribe.calls) == ([], [])


# ---------------------------------------------------------------------------
# Full handoff from Stage 1
# ---------------------------------------------------------------------------


def test_a_row_written_by_stage_one_is_processed_by_this_stage(monkeypatch) -> None:
    """End-to-end over the stage boundary with no hand-written row in between.

    Stage 1 runs for real and its DynamoDB item is handed to this handler exactly
    as it was written. This is the test that fails if either side renames a field:
    every value asserted below was produced by Stage 1's code, not by this file.
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
                    "agentId": "carol@example.com",
                    "filename": "quarterly.mp4",
                    "contentType": "video/mp4",
                    "enableDiarization": True,
                    "maxSpeakers": 5,
                    "languageCode": "en-GB",
                    "meetingDateTime": "2025-01-05T14:00:00Z",
                }
            },
        },
        None,
    )
    row = written[0]

    table = FakeTable(row)
    kinesis = FakeKinesis()
    transcribe = FakeTranscribe()
    monkeypatch.setattr(index, "job_table", table)
    monkeypatch.setattr(index, "kinesis_client", kinesis)
    monkeypatch.setattr(index, "transcribe_client", transcribe)

    result = index.lambda_handler(s3_event(key=created["uploadKey"]), None)

    assert result == {"processed": 1}
    call_id = created["callId"]
    assert table.get_calls[0]["Key"] == {"PK": f"uj#{call_id}", "SK": f"uj#{call_id}"}
    assert table.statuses == ["UPLOADED", "TRANSCRIBING"]
    assert kinesis.payloads[0]["EventType"] == "START"
    assert kinesis.payloads[0]["CallId"] == call_id
    assert kinesis.payloads[0]["AgentId"] == "carol@example.com"
    assert kinesis.payloads[0]["CreatedAt"] == "2025-01-05T14:00:00Z"
    assert transcribe.params["TranscriptionJobName"] == call_id
    assert transcribe.params["LanguageCode"] == "en-GB"
    assert transcribe.params["Settings"] == {"ShowSpeakerLabels": True, "MaxSpeakerLabels": 5}
    assert transcribe.params["Media"]["MediaFileUri"] == f"s3://{BUCKET}/{created['uploadKey']}"
    assert transcribe.params["OutputKey"] == f"lma-transcripts/{call_id}.transcribe.json"
