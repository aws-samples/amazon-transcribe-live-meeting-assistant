# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for Stage 1 of the meeting-upload pipeline (``createUploadMeeting``).

This resolver is the only writer of the ``uj#<callId>`` UploadJob row and the only
producer of the pending-upload S3 key. Two later Lambdas -- upload_meeting_processor
(Stage 2, S3 ObjectCreated) and upload_meeting_finalizer (Stage 3, Transcribe state
change) -- read that row and parse that key, and neither can be deployed with a
compile-time reference to this module. The field names and the key shape are
therefore a wire format, so the tests below pin them by value rather than merely
checking that the row was written.

The rest of the coverage is input validation (this is the boundary where browser
input first becomes an S3 key and a DynamoDB row), the presigned-URL parameters,
and the two TTLs, both asserted as durations rather than as "a number is present":
a retention test that only checks presence passes for any retention.

No AWS calls are made. boto3 is patched before import because the module builds
its S3 client and DynamoDB table handle at import time, and the fakes installed
per test replace those handles entirely.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from botocore.exceptions import ClientError

BUCKET = "lma-recordings-bucket"
TABLE = "lma-event-sourcing-table"

# Only the two required variables are set. UPLOADS_PENDING_PREFIX,
# UPLOAD_URL_TTL_SECONDS and UPLOAD_JOB_TTL_DAYS are deliberately left unset so the
# module defaults are the values under test -- the CloudFormation template passes
# the same literals, so a changed default here is a changed contract in production.
os.environ["S3_BUCKET_NAME"] = BUCKET
os.environ["EVENT_SOURCING_TABLE"] = TABLE
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

HERE = Path(__file__).resolve().parent


def _load_by_path(name: str, path: Path):
    """Import a module from an explicit path under a unique name.

    Every Lambda source directory in this tree contains an ``index.py``, so a plain
    ``import index`` resolves to whichever directory happens to be first on
    sys.path. Naming the module explicitly keeps that ambiguity out of the suite.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


with mock.patch("boto3.client"), mock.patch("boto3.resource"):
    index = _load_by_path("upload_meeting_initiator_index", HERE / "index.py")


# The instant every test observes, so timestamp and TTL arithmetic can be asserted
# as exact values instead of as a tolerance window.
FROZEN_NOW = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
PRESIGNED_URL = "https://example.invalid/presigned"


class _FrozenDatetime(datetime):
    """A datetime whose ``now()`` never moves; ``fromisoformat`` still works."""

    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW


class FakeTable:
    """Records ``put_item`` calls; can be told to fail like DynamoDB would."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.error = error

    # boto3 spells its keyword arguments in PascalCase.
    def put_item(self, Item, ConditionExpression=None):  # noqa: N803
        if self.error is not None:
            raise self.error
        self.calls.append({"Item": Item, "ConditionExpression": ConditionExpression})
        return {}

    @property
    def item(self) -> dict:
        assert len(self.calls) == 1, f"expected exactly one put_item, got {len(self.calls)}"
        return self.calls[0]["Item"]


class FakeS3:
    """Captures presigned-URL requests instead of signing one."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn, HttpMethod):  # noqa: N803
        self.calls.append(
            {
                "ClientMethod": ClientMethod,
                "Params": Params,
                "ExpiresIn": ExpiresIn,
                "HttpMethod": HttpMethod,
            }
        )
        return PRESIGNED_URL


def conditional_check_failed() -> ClientError:
    """The error DynamoDB raises when ``attribute_not_exists(PK)`` is not met."""
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
        "PutItem",
    )


@pytest.fixture(name="env")
def env_fixture(monkeypatch: pytest.MonkeyPatch):
    """Install the fakes and the frozen clock, and hand both back to the test."""
    table = FakeTable()
    s3_client = FakeS3()
    monkeypatch.setattr(index, "job_table", table)
    monkeypatch.setattr(index, "s3_client", s3_client)
    monkeypatch.setattr(index, "datetime", _FrozenDatetime)
    return table, s3_client


def make_event(identity: dict | None = None, field_name: str = "createUploadMeeting", **overrides):
    """Build an AppSync direct-Lambda resolver event with sensible defaults."""
    payload = {
        "meetingTopic": "Weekly Sync",
        "agentId": "bob@example.com",
        "filename": "recording.mp3",
        "contentType": "audio/mpeg",
    }
    payload.update(overrides)
    return {
        "info": {"fieldName": field_name},
        "identity": (
            {"username": "bob@example.com", "groups": ["Admin"]} if identity is None else identity
        ),
        "arguments": {"input": payload},
    }


def invoke(**kwargs):
    """Run the handler; keyword arguments become mutation input fields."""
    return index.lambda_handler(make_event(**kwargs), None)


# ---------------------------------------------------------------------------
# Caller identity and resolver wiring
# ---------------------------------------------------------------------------


def test_only_the_upload_mutation_is_served(env) -> None:
    """One resolver per field: a misrouted field must not write an UploadJob row."""
    table, _ = env
    with pytest.raises(index.ValidationError, match="Unexpected fieldName"):
        invoke(field_name="createCall")
    assert table.calls == []


def test_an_unauthenticated_caller_is_rejected(env) -> None:
    """Owner drives UBAC downstream, so a row must never be written without one."""
    table, _ = env
    with pytest.raises(index.ValidationError, match="Cognito"):
        invoke(identity={})
    assert table.calls == []


def test_the_cognito_subject_identifies_the_owner_when_no_username_is_present(env) -> None:
    """Deployments differ in which identity field is populated; both must work."""
    table, _ = env
    index.lambda_handler(make_event(identity={"sub": "uuid-1234"}), None)
    assert table.item["Owner"] == "uuid-1234"


def test_the_callers_groups_are_recorded_on_the_row(env) -> None:
    """Group membership is captured at request time for later access decisions."""
    table, _ = env
    index.lambda_handler(
        make_event(identity={"username": "bob@example.com", "groups": ["Admin", "Users"]}),
        None,
    )
    assert table.item["OwnerGroups"] == ["Admin", "Users"]
    assert table.item["Owner"] == "bob@example.com"


def test_absent_groups_are_recorded_as_an_empty_list(env) -> None:
    """A list, not None: DynamoDB rejects None and the reader expects a sequence."""
    table, _ = env
    index.lambda_handler(make_event(identity={"username": "bob@example.com"}), None)
    assert table.item["OwnerGroups"] == []


# ---------------------------------------------------------------------------
# The response returned to the browser
# ---------------------------------------------------------------------------


def test_the_response_carries_exactly_the_fields_the_ui_needs(env) -> None:
    """The UI PUTs straight to S3, so it needs the URL, the bucket and the key."""
    _, s3_client = env
    result = invoke()
    assert set(result) == {
        "callId",
        "uploadUrl",
        "uploadBucket",
        "uploadKey",
        "contentType",
        "expiresInSeconds",
    }
    assert result["uploadUrl"] == PRESIGNED_URL
    assert result["uploadBucket"] == BUCKET
    assert result["contentType"] == "audio/mpeg"
    assert result["expiresInSeconds"] == index.UPLOAD_URL_TTL_SECONDS
    assert len(s3_client.calls) == 1


def test_the_announced_expiry_is_the_one_the_url_was_signed_with(env) -> None:
    """The browser uses expiresInSeconds to decide when to re-request a URL."""
    _, s3_client = env
    result = invoke()
    assert result["expiresInSeconds"] == s3_client.calls[0]["ExpiresIn"]


# ---------------------------------------------------------------------------
# S3 key construction -- the string Stage 2 parses back apart
# ---------------------------------------------------------------------------


def test_the_pending_key_is_prefix_then_call_id_then_filename(env) -> None:
    """Stage 2 splits this key on '/' to recover the callId; the shape is fixed."""
    table, _ = env
    result = invoke(callId="call-abc")
    assert result["uploadKey"] == "lma-uploads-pending/call-abc/recording.mp3"
    assert table.item["PendingObjectKey"] == result["uploadKey"]
    assert table.item["PendingObjectBucket"] == BUCKET


def test_uploads_land_under_the_pending_prefix(env) -> None:
    """The prefix is shared with Stage 2 and 3 and with the bucket's S3
    notification filter and lifecycle rule, so it is pinned by value here."""
    assert index.UPLOADS_PENDING_PREFIX == "lma-uploads-pending/"
    assert invoke()["uploadKey"].startswith("lma-uploads-pending/")


def test_only_the_final_path_segment_of_the_supplied_filename_is_used(env) -> None:
    """A browser may send a full path; the key must stay one level under callId."""
    result = invoke(callId="call-abc", filename="/home/user/My Meeting (1).mp3")
    assert result["uploadKey"] == "lma-uploads-pending/call-abc/My_Meeting_1_.mp3"


@pytest.mark.parametrize(
    "filename",
    ["", "../../etc/passwd", "///", "....", "a/b/c.mp3", None],
    ids=["empty", "relative", "slashes", "dots", "nested", "none"],
)
def test_the_key_always_has_exactly_three_segments_and_a_usable_name(env, filename) -> None:
    """Stage 2 requires ``<prefix><callId>/<filename>`` with both parts non-empty;
    anything else is dropped there as unparseable, so no input may reshape it."""
    result = invoke(callId="call-abc", filename=filename)
    prefix, call_id, name = result["uploadKey"].split("/")
    assert f"{prefix}/" == index.UPLOADS_PENDING_PREFIX
    assert call_id == "call-abc"
    assert re.fullmatch(r"[a-zA-Z0-9._-]+", name), name


def test_a_long_filename_is_truncated_but_keeps_its_extension(env) -> None:
    """Total key length is bounded and the extension decides the player's MIME
    handling after Stage 3 promotes the file."""
    result = invoke(callId="call-abc", filename="x" * 200 + ".mp4")
    name = result["uploadKey"].rsplit("/", 1)[-1]
    assert len(name) == 128
    assert name.endswith(".mp4")


# ---------------------------------------------------------------------------
# callId generation
# ---------------------------------------------------------------------------


def test_a_generated_call_id_is_a_topic_slug_a_utc_stamp_and_a_random_suffix(env) -> None:
    """The callId is user-visible and becomes both an S3 path segment and a
    Transcribe job name, so it must be slugified and made unique per request."""
    result = invoke(meetingTopic="Weekly Sync!! / Review")
    assert re.fullmatch(r"Weekly-Sync-Review-20250102T030405-[0-9a-f]{8}", result["callId"])


def test_two_requests_for_the_same_topic_get_different_call_ids(env) -> None:
    """Same topic and same second: the random suffix is what keeps rows apart."""
    first = invoke()["callId"]
    second = invoke()["callId"]
    assert first != second


def test_a_caller_supplied_call_id_is_used_unchanged(env) -> None:
    """Re-driving a known callId must address the same row and the same key."""
    table, _ = env
    assert invoke(callId="my.call_id-1")["callId"] == "my.call_id-1"
    assert table.item["CallId"] == "my.call_id-1"


@pytest.mark.parametrize(
    "call_id",
    ["has space", "has/slash", "x" * 129, "semi;colon", "uj#other"],
    ids=["space", "slash", "too-long", "semicolon", "hash"],
)
def test_a_supplied_call_id_must_match_the_permitted_character_set(env, call_id) -> None:
    """The callId is concatenated into an S3 key, a DynamoDB partition key and a
    Transcribe job name, each of which has its own legal alphabet."""
    table, _ = env
    with pytest.raises(index.ValidationError, match="callId must match"):
        invoke(callId=call_id)
    assert table.calls == []


# ---------------------------------------------------------------------------
# The UploadJob row -- the contract with Stage 2 and Stage 3
# ---------------------------------------------------------------------------


def test_the_row_is_keyed_in_its_own_namespace(env) -> None:
    """``uj#`` keeps upload jobs from colliding with meeting rows (``c#``) and
    meeting-list shards (``cls#``) in the shared EventSourcing table."""
    table, _ = env
    call_id = invoke()["callId"]
    item = table.item
    assert item["PK"] == f"uj#{call_id}"
    assert item["SK"] == item["PK"]
    assert item["RecordType"] == "UploadJob"


def test_the_row_is_written_only_if_the_call_id_is_unused(env) -> None:
    """Two requests for one callId would otherwise share an S3 key and a row."""
    table, _ = env
    invoke()
    assert table.calls[0]["ConditionExpression"] == "attribute_not_exists(PK)"


def test_a_new_row_starts_in_the_state_stage_two_acts_on(env) -> None:
    """Stage 2 processes a row only while it is still PENDING_UPLOAD; any other
    value would make every upload notification a no-op."""
    table, _ = env
    invoke()
    assert table.item["Status"] == "PENDING_UPLOAD"


def test_the_row_carries_every_field_the_later_stages_read(env) -> None:
    """Mirror of the reads in upload_meeting_processor and
    upload_meeting_finalizer. Those Lambdas are packaged separately and cannot
    import this module, so a renamed field surfaces only at runtime -- as a
    meeting that uploads, then silently never transcribes."""
    table, _ = env
    call_id = invoke(
        fromNumber="+15550001111",
        toNumber="+15550002222",
        languageCode="es-US",
        meetingDateTime="2025-01-01T10:00:00Z",
        enableDiarization=True,
        maxSpeakers=6,
        fileSize=1234,
    )["callId"]
    expected = {
        "CallId": call_id,
        "Owner": "bob@example.com",
        "AgentId": "bob@example.com",
        "FromNumber": "+15550001111",
        "ToNumber": "+15550002222",
        "MeetingTopic": "Weekly Sync",
        "Filename": "recording.mp3",
        "ContentType": "audio/mpeg",
        "FileSize": 1234,
        "EnableDiarization": True,
        "MaxSpeakers": 6,
        "LanguageCode": "es-US",
        "MeetingDateTime": "2025-01-01T10:00:00Z",
        "PendingObjectBucket": BUCKET,
        "PendingObjectKey": f"lma-uploads-pending/{call_id}/recording.mp3",
    }
    # Compared by projection rather than field by field, so a renamed or dropped
    # attribute reads as a diff instead of a KeyError.
    assert {name: table.item.get(name) for name in expected} == expected


def test_optional_fields_are_left_out_rather_than_stored_as_null(env) -> None:
    """DynamoDB rejects a None attribute value, so an unfilled optional field has
    to be dropped from the item for the whole PutItem to succeed."""
    table, _ = env
    invoke()
    item = table.item
    assert "FileSize" not in item
    assert "LanguageCode" not in item
    assert "MeetingDateTime" not in item
    assert [k for k, v in item.items() if v is None] == []


def test_the_parties_default_to_the_placeholder_names_the_ui_shows(env) -> None:
    """These become CustomerPhoneNumber/SystemPhoneNumber on the Kinesis START
    event, which the meetings list renders verbatim."""
    table, _ = env
    invoke()
    assert table.item["FromNumber"] == "Customer"
    assert table.item["ToNumber"] == "System"


def test_a_duplicate_call_id_is_reported_to_the_caller_as_invalid_input(
    monkeypatch: pytest.MonkeyPatch, env
) -> None:
    """The UI can show "already used"; an unhandled error would be a 500."""
    _, _ = env
    monkeypatch.setattr(index, "job_table", FakeTable(error=conditional_check_failed()))
    with pytest.raises(index.ValidationError, match="already exists for callId"):
        invoke(callId="call-abc")


def test_other_dynamodb_failures_are_not_reported_as_invalid_input(
    monkeypatch: pytest.MonkeyPatch, env
) -> None:
    """A throttle or an access error must stay an error, so the client retries
    instead of telling the user their request was malformed."""
    _, _ = env
    throttled = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow down"}},
        "PutItem",
    )
    monkeypatch.setattr(index, "job_table", FakeTable(error=throttled))
    with pytest.raises(ClientError):
        invoke()


def test_no_presigned_url_is_issued_when_the_row_cannot_be_claimed(
    monkeypatch: pytest.MonkeyPatch, env
) -> None:
    """The row is the claim on the key; a URL handed out without one would let a
    file land where no stage is watching for it."""
    _, s3_client = env
    monkeypatch.setattr(index, "job_table", FakeTable(error=conditional_check_failed()))
    with pytest.raises(index.ValidationError):
        invoke(callId="call-abc")
    assert s3_client.calls == []


# ---------------------------------------------------------------------------
# Timestamps and retention
# ---------------------------------------------------------------------------


def test_created_and_updated_are_the_same_iso_instant_on_a_new_row(env) -> None:
    """Stage 2 and 3 only ever move UpdatedAt forward, so they must start equal."""
    table, _ = env
    invoke()
    item = table.item
    assert item["CreatedAt"] == FROZEN_NOW.isoformat()
    assert item["UpdatedAt"] == item["CreatedAt"]


def test_the_row_is_retained_for_fourteen_days_after_creation(env) -> None:
    """ExpiresAfter is the table's DynamoDB TTL attribute: a shorter value would
    delete the metadata while a long upload or transcription is still running,
    and a longer one keeps caller-supplied meeting metadata past its purpose.
    Asserted as an offset from CreatedAt, so the duration itself is under test."""
    table, _ = env
    invoke()
    item = table.item
    assert index.UPLOAD_JOB_TTL_DAYS == 14
    expected = int((FROZEN_NOW + timedelta(days=14)).timestamp())
    assert item["ExpiresAfter"] == expected
    created = datetime.fromisoformat(item["CreatedAt"])
    assert item["ExpiresAfter"] - int(created.timestamp()) == 14 * 24 * 60 * 60
    assert isinstance(item["ExpiresAfter"], int)


def test_the_upload_url_is_valid_for_fifteen_minutes(env) -> None:
    """Long enough for a large file on a slow link, short enough that a URL
    copied out of the browser's network log stops working quickly."""
    _, s3_client = env
    invoke()
    assert index.UPLOAD_URL_TTL_SECONDS == 900
    assert s3_client.calls[0]["ExpiresIn"] == 900


# ---------------------------------------------------------------------------
# Presigned PUT parameters
# ---------------------------------------------------------------------------


def test_the_url_signs_a_put_of_exactly_one_key_with_one_content_type(env) -> None:
    """The signature pins bucket, key and Content-Type, so the browser cannot
    redirect the upload elsewhere or relabel what it is sending."""
    _, s3_client = env
    result = invoke(callId="call-abc", contentType="video/mp4", filename="a.mp4")
    call = s3_client.calls[0]
    assert call["ClientMethod"] == "put_object"
    assert call["HttpMethod"] == "PUT"
    assert call["Params"] == {
        "Bucket": BUCKET,
        "Key": "lma-uploads-pending/call-abc/a.mp4",
        "ContentType": "video/mp4",
    }
    assert result["uploadKey"] == call["Params"]["Key"]


def test_presigned_urls_are_signed_with_sigv4(env) -> None:
    """The recordings bucket is KMS-encrypted; SigV2 presigned URLs are refused
    there, so the client config carries the signature version explicitly."""
    # Re-imported under a patched boto3 so the config the module asks for is
    # visible; the fixture's fake has already replaced the module-level client.
    with mock.patch("boto3.client") as client_factory, mock.patch("boto3.resource"):
        _load_by_path("upload_meeting_initiator_reimport", HERE / "index.py")
    config = client_factory.call_args.kwargs["config"]
    assert config.signature_version == "s3v4"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"meetingTopic": ""}, "meetingTopic is required"),
        ({"meetingTopic": "   "}, "meetingTopic is required"),
        ({"meetingTopic": None}, "meetingTopic is required"),
        ({"meetingTopic": "t" * 201}, "200 characters or fewer"),
        ({"agentId": ""}, "agentId is required"),
        ({"agentId": "a" * 201}, "200 characters or fewer"),
        ({"contentType": ""}, "contentType is required"),
        ({"contentType": "application/pdf"}, "contentType must start with"),
        ({"contentType": "text/plain"}, "contentType must start with"),
        ({"fileSize": "not-a-number"}, "fileSize must be an integer"),
        ({"fileSize": 0}, "fileSize must be positive"),
        ({"fileSize": -1}, "fileSize must be positive"),
        ({"fileSize": 5 * 1024 * 1024 * 1024 + 1}, "exceeds the"),
        ({"maxSpeakers": 1}, "maxSpeakers must be between 2 and 30"),
        ({"maxSpeakers": 31}, "maxSpeakers must be between 2 and 30"),
        ({"maxSpeakers": "many"}, "maxSpeakers must be an integer"),
        ({"meetingDateTime": "last tuesday"}, "ISO-8601"),
        ({"languageCode": "e"}, "BCP-47"),
        ({"languageCode": "en_US"}, "BCP-47"),
        ({"languageCode": "english!"}, "BCP-47"),
    ],
)
def test_malformed_input_is_rejected_before_anything_is_written(env, overrides, message) -> None:
    """Every rejected request must leave no row and no presigned URL behind, or a
    later upload would find metadata for a request that was never accepted."""
    table, s3_client = env
    with pytest.raises(index.ValidationError, match=message):
        invoke(**overrides)
    assert table.calls == []
    assert s3_client.calls == []


@pytest.mark.parametrize(
    "content_type",
    ["audio/mpeg", "audio/wav", "video/mp4", "video/webm", " AUDIO/MP4 "],
    ids=["mpeg", "wav", "mp4", "webm", "padded-uppercase"],
)
def test_audio_and_video_uploads_are_accepted_and_normalized(env, content_type) -> None:
    """Transcribe reads both families. The value is normalized once, here, so the
    row, the signature and the response all agree on it."""
    table, s3_client = env
    result = invoke(contentType=content_type)
    expected = content_type.strip().lower()
    assert result["contentType"] == expected
    assert table.item["ContentType"] == expected
    assert s3_client.calls[0]["Params"]["ContentType"] == expected


@pytest.mark.parametrize("max_speakers", [2, 4, 30])
def test_the_ends_of_the_speaker_range_are_accepted(env, max_speakers) -> None:
    """Transcribe's own ShowSpeakerLabels range is 2-30 inclusive."""
    table, _ = env
    invoke(enableDiarization=True, maxSpeakers=max_speakers)
    assert table.item["MaxSpeakers"] == max_speakers


def test_diarization_is_off_and_four_speakers_are_assumed_by_default(env) -> None:
    """Stage 2 reads both fields unconditionally, so both need a value even when
    the client omits them."""
    table, _ = env
    invoke()
    item = table.item
    assert item["EnableDiarization"] is False
    assert item["MaxSpeakers"] == 4


@pytest.mark.parametrize(
    "supplied",
    ["2025-01-01T10:00:00Z", "2025-01-01T10:00:00+00:00", "2025-01-01T10:00:00.500Z"],
    ids=["zulu", "offset", "fractional"],
)
def test_an_iso_meeting_time_is_stored_exactly_as_supplied(env, supplied) -> None:
    """Stage 2 copies this string straight onto the START event's CreatedAt, so it
    must survive validation unmodified rather than being reformatted."""
    table, _ = env
    invoke(meetingDateTime=supplied)
    assert table.item["MeetingDateTime"] == supplied


def test_the_file_size_ceiling_is_five_gibibytes(env) -> None:
    """A single presigned PUT is capped at 5 GiB by S3 itself, so accepting more
    would hand out a URL the browser cannot complete."""
    table, _ = env
    assert index.MAX_FILE_BYTES == 5 * 1024 * 1024 * 1024
    invoke(fileSize=index.MAX_FILE_BYTES)
    assert table.item["FileSize"] == index.MAX_FILE_BYTES


def test_a_numeric_string_file_size_is_stored_as_a_number(env) -> None:
    """GraphQL clients have sent this as a string; DynamoDB would otherwise keep
    it as one and later comparisons would be lexicographic."""
    table, _ = env
    invoke(fileSize="4096")
    assert table.item["FileSize"] == 4096


def test_surrounding_whitespace_is_trimmed_from_the_topic_and_agent(env) -> None:
    """The topic becomes a slug and part of a key; the agent becomes the Owner."""
    table, _ = env
    invoke(meetingTopic="  Weekly Sync  ", agentId="  bob@example.com  ")
    assert table.item["MeetingTopic"] == "Weekly Sync"
    assert table.item["AgentId"] == "bob@example.com"
