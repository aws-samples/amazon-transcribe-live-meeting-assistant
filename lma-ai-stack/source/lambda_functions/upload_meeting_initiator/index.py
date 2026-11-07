# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Upload Meeting Initiator — AppSync Lambda resolver for the `createUploadMeeting` mutation.

When a signed-in user requests to upload a pre-recorded meeting file, this Lambda:

1. Validates the request (content-type, diarization args).
2. Generates a deterministic ``callId`` from the supplied meeting topic + timestamp.
3. Generates a presigned S3 ``PutObject`` URL scoped to the pending-uploads prefix
   (``lma-uploads-pending/<callId>/<filename>``) on the existing recordings bucket.
4. Persists an ``UploadJob`` row in the EventSourcing DynamoDB table so downstream
   Lambdas (upload_meeting_processor / upload_meeting_finalizer, Stage 2 & 3) can
   look up the caller-supplied metadata when the S3 object notification fires.
5. Returns the ``callId``, presigned URL, bucket, and key to the UI so the browser
   can PUT the file directly to S3 (bypassing API Gateway / Lambda payload limits).

The actual transcription / event emission happens in later stages of the pipeline;
this Lambda only sets up the upload handoff.
"""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
S3_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]
UPLOADS_PENDING_PREFIX = os.environ.get("UPLOADS_PENDING_PREFIX", "lma-uploads-pending/")
UPLOAD_URL_TTL_SECONDS = int(os.environ.get("UPLOAD_URL_TTL_SECONDS", "900"))  # 15 min
UPLOAD_JOB_TTL_DAYS = int(os.environ.get("UPLOAD_JOB_TTL_DAYS", "14"))
EVENT_SOURCING_TABLE = os.environ["EVENT_SOURCING_TABLE"]
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Upper bounds on user-provided values.
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_BYTES", str(5 * 1024 * 1024 * 1024)))  # 5 GiB
MIN_SPEAKERS = 2
MAX_SPEAKERS = 30

ALLOWED_CONTENT_TYPE_PREFIXES = ("audio/", "video/")

# The media formats Amazon Transcribe accepts as StartTranscriptionJob's
# MediaFormat. An upload whose format is one of these is keyed with it as the
# extension and the format is stored on the UploadJob row for Stage 2 to pass.
SUPPORTED_MEDIA_FORMATS = frozenset({"mp3", "mp4", "wav", "flac", "ogg", "amr", "webm", "m4a"})

# The format for a browser-reported content type, used when the file name has no
# supported extension. Browsers report the same format under several types.
CONTENT_TYPE_MEDIA_FORMATS = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mpeg3": "mp3",
    "audio/x-mpeg-3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "video/mp4": "mp4",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/vnd.wave": "wav",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/ogg": "ogg",
    "video/ogg": "ogg",
    "audio/amr": "amr",
    "audio/webm": "webm",
    "video/webm": "webm",
}

# ---------------------------------------------------------------------------
# Logging / clients
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# signature_version=s3v4 is required for presigned URLs on KMS-encrypted buckets.
s3_client = boto3.client("s3", config=Config(signature_version="s3v4", region_name=AWS_REGION))
ddb = boto3.resource("dynamodb")
job_table = ddb.Table(EVENT_SOURCING_TABLE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class ValidationError(Exception):
    """Raised when the mutation input is malformed or fails authorization."""


def _slugify(value: str, fallback: str = "meeting") -> str:
    """Produce an S3-key-safe slug."""
    if not value:
        return fallback
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-_.")
    return slug[:64] or fallback


def _split_media_extension(base: str) -> tuple[str, str | None]:
    """Split ``base`` into a name and a supported media format. Only an extension
    that is one of SUPPORTED_MEDIA_FORMATS (in any case) counts; anything else
    after the last dot ("Weekly 1.5 sync", "notes.txt") stays part of the name."""
    if "." in base:
        stem, ext = base.rsplit(".", 1)
        ext = ext.strip().lower()
        if ext in SUPPORTED_MEDIA_FORMATS:
            return stem, ext
    return base, None


def _media_format(filename: str, content_type: str) -> str | None:
    """The upload's media format: the file name's extension when it is a
    supported one, else the format the content type names, else None (the key
    then has no extension and Transcribe is left to detect the format)."""
    _, ext = _split_media_extension(os.path.basename(filename or ""))
    if ext:
        return ext
    return CONTENT_TYPE_MEDIA_FORMATS.get(content_type.split(";", 1)[0].strip())


def _sanitize_filename(name: str, media_format: str | None) -> str:
    """Strip any path components the browser may have included and restrict the
    name to a conservative character set, then end it with ``media_format`` as
    the extension. The extension is never derived from what is left of the name
    after the restriction, so a name the set cannot carry (one in a non-Latin
    script, say) loses the name, not the format: ``recording.<format>``."""
    stem, _ = _split_media_extension(os.path.basename(name or ""))
    ext = f".{media_format}" if media_format else ""
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", stem).lstrip("._").rstrip(".") or "recording"
    return stem[: 128 - len(ext)] + ext


def _get_username(event: dict) -> str:
    identity = event.get("identity") or {}
    username = identity.get("username") or identity.get("sub")
    if not username:
        raise ValidationError("Caller identity is missing — Cognito auth required.")
    return username


def _get_user_groups(event: dict) -> list:
    identity = event.get("identity") or {}
    return list(identity.get("groups") or [])


def _parse_and_validate_input(arguments: dict) -> dict:
    """Return a normalized dict of safe input values. Raise ValidationError on
    anything malformed."""
    raw = arguments.get("input") or {}

    meeting_topic = (raw.get("meetingTopic") or "").strip()
    if not meeting_topic:
        raise ValidationError("meetingTopic is required")
    if len(meeting_topic) > 200:
        raise ValidationError("meetingTopic must be 200 characters or fewer")

    # localSpeakerName and remoteSpeakerName are the documented names; agentId
    # and fromNumber are their older names, still accepted.
    agent_id = (raw.get("localSpeakerName") or "").strip() or (raw.get("agentId") or "").strip()
    if not agent_id:
        raise ValidationError("localSpeakerName (or agentId) is required")
    if len(agent_id) > 200:
        raise ValidationError("localSpeakerName (or agentId) must be 200 characters or fewer")

    # The name the transcript shows for the other participants when none is given:
    # the placeholder the UI and the WebSocket server use.
    from_number = (
        (raw.get("remoteSpeakerName") or "").strip()
        or (raw.get("fromNumber") or "").strip()
        or "Other Participant"
    )
    to_number = (raw.get("toNumber") or "").strip() or "System"

    content_type = (raw.get("contentType") or "").strip().lower()
    if not content_type:
        raise ValidationError("contentType is required")
    if not content_type.startswith(ALLOWED_CONTENT_TYPE_PREFIXES):
        raise ValidationError(
            f"contentType must start with one of: {', '.join(ALLOWED_CONTENT_TYPE_PREFIXES)}"
        )

    media_format = _media_format(raw.get("filename") or "", content_type)
    filename = _sanitize_filename(raw.get("filename") or "", media_format)
    if not filename:
        raise ValidationError("filename is required")

    file_size = raw.get("fileSize")
    if file_size is not None:
        try:
            file_size = int(file_size)
        except (TypeError, ValueError) as err:
            raise ValidationError("fileSize must be an integer") from err
        if file_size <= 0:
            raise ValidationError("fileSize must be positive")
        if file_size > MAX_FILE_BYTES:
            raise ValidationError(f"fileSize exceeds the {MAX_FILE_BYTES} byte limit")

    enable_diarization = bool(raw.get("enableDiarization"))
    max_speakers = raw.get("maxSpeakers")
    if max_speakers is None:
        max_speakers = 4
    try:
        max_speakers = int(max_speakers)
    except (TypeError, ValueError) as err:
        raise ValidationError("maxSpeakers must be an integer") from err
    if not (MIN_SPEAKERS <= max_speakers <= MAX_SPEAKERS):
        raise ValidationError(f"maxSpeakers must be between {MIN_SPEAKERS} and {MAX_SPEAKERS}")

    meeting_date_time = raw.get("meetingDateTime")
    if meeting_date_time:
        try:
            datetime.fromisoformat(meeting_date_time.replace("Z", "+00:00"))
        except ValueError as err:
            raise ValidationError("meetingDateTime must be an ISO-8601 timestamp") from err

    language_code = (raw.get("languageCode") or "").strip() or None
    if language_code and not re.match(r"^[a-zA-Z]{2,3}(-[a-zA-Z0-9]+)*$", language_code):
        raise ValidationError("languageCode must look like a BCP-47 tag (e.g. en-US)")

    caller_supplied_call_id = (raw.get("callId") or "").strip() or None
    if caller_supplied_call_id and not re.match(
        r"^[a-zA-Z0-9._-]{1,128}$", caller_supplied_call_id
    ):
        raise ValidationError("callId must match [a-zA-Z0-9._-]{1,128}")

    return {
        "meetingTopic": meeting_topic,
        "agentId": agent_id,
        "fromNumber": from_number,
        "toNumber": to_number,
        "filename": filename,
        "contentType": content_type,
        "mediaFormat": media_format,
        "fileSize": file_size,
        "enableDiarization": enable_diarization,
        "maxSpeakers": max_speakers,
        "meetingDateTime": meeting_date_time,
        "languageCode": language_code,
        "callerSuppliedCallId": caller_supplied_call_id,
    }


def _build_call_id(meeting_topic: str, caller_supplied: str | None) -> str:
    if caller_supplied:
        return caller_supplied
    slug = _slugify(meeting_topic)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    short = uuid.uuid4().hex[:8]
    return f"{slug}-{stamp}-{short}"


def _build_object_key(call_id: str, filename: str) -> str:
    return f"{UPLOADS_PENDING_PREFIX}{call_id}/{filename}"


def _generate_presigned_put_url(object_key: str, content_type: str) -> str:
    params = {
        "Bucket": S3_BUCKET_NAME,
        "Key": object_key,
        "ContentType": content_type,
    }
    try:
        return s3_client.generate_presigned_url(
            "put_object",
            Params=params,
            ExpiresIn=UPLOAD_URL_TTL_SECONDS,
            HttpMethod="PUT",
        )
    except ClientError as err:
        logger.error("Failed to generate presigned URL: %s", err)
        raise


def _write_upload_job(
    call_id: str,
    owner: str,
    groups: list,
    normalized: dict,
    object_key: str,
) -> None:
    """Persist the upload-job metadata so downstream Lambdas can look it up.

    Uses the existing EventSourcing DynamoDB table with a dedicated PK/SK
    namespace (``uj#<callId>``) so it never collides with meeting records
    (``c#...``) or meeting-list shards (``cls#...``).
    """
    now = datetime.now(timezone.utc)
    expires_at = int((now + timedelta(days=UPLOAD_JOB_TTL_DAYS)).timestamp())
    pk = f"uj#{call_id}"

    item = {
        "PK": pk,
        "SK": pk,
        "RecordType": "UploadJob",
        "CallId": call_id,
        "Status": "PENDING_UPLOAD",
        "Owner": owner,
        "OwnerGroups": groups,
        "MeetingTopic": normalized["meetingTopic"],
        "AgentId": normalized["agentId"],
        "FromNumber": normalized["fromNumber"],
        "ToNumber": normalized["toNumber"],
        "Filename": normalized["filename"],
        "ContentType": normalized["contentType"],
        # Absent when neither the file name nor the content type names a format
        # Transcribe accepts; Stage 2 then leaves MediaFormat for it to detect.
        "MediaFormat": normalized.get("mediaFormat"),
        "FileSize": normalized.get("fileSize"),
        "EnableDiarization": normalized["enableDiarization"],
        "MaxSpeakers": normalized["maxSpeakers"],
        "LanguageCode": normalized.get("languageCode"),
        "MeetingDateTime": normalized.get("meetingDateTime"),
        "PendingObjectBucket": S3_BUCKET_NAME,
        "PendingObjectKey": object_key,
        "CreatedAt": now.isoformat(),
        "UpdatedAt": now.isoformat(),
        "ExpiresAfter": expires_at,
    }
    # DynamoDB disallows None values.
    item = {k: v for k, v in item.items() if v is not None}

    try:
        job_table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(PK)",
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ValidationError(f"An upload job already exists for callId {call_id}") from err
        logger.error("Failed to write UploadJob row: %s", err)
        raise


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def lambda_handler(event, context):  # noqa: ARG001
    """AppSync direct-Lambda resolver invocation.

    Expected event shape (from the pass-through APPSYNC_JS resolver in
    ``createUploadMeeting.js``):

    .. code-block:: json

        {
          "arguments": { "input": { ... } },
          "identity":  { "username": "...", "sub": "...", "groups": [...] },
          "info":      { "fieldName": "createUploadMeeting", ... }
        }
    """
    logger.debug("Event: %s", json.dumps(event, default=str))

    field_name = (event.get("info") or {}).get("fieldName")
    if field_name != "createUploadMeeting":
        raise ValidationError(f"Unexpected fieldName: {field_name}")

    try:
        owner = _get_username(event)
        groups = _get_user_groups(event)
        normalized = _parse_and_validate_input(event.get("arguments") or {})
    except ValidationError as err:
        logger.warning("Validation error: %s", err)
        raise

    call_id = _build_call_id(normalized["meetingTopic"], normalized["callerSuppliedCallId"])
    object_key = _build_object_key(call_id, normalized["filename"])

    _write_upload_job(call_id, owner, groups, normalized, object_key)
    upload_url = _generate_presigned_put_url(object_key, normalized["contentType"])

    result = {
        "callId": call_id,
        "uploadUrl": upload_url,
        "uploadBucket": S3_BUCKET_NAME,
        "uploadKey": object_key,
        "contentType": normalized["contentType"],
        "expiresInSeconds": UPLOAD_URL_TTL_SECONDS,
    }
    logger.info(
        "Created upload job callId=%s owner=%s key=%s diarize=%s maxSpeakers=%s",
        call_id,
        owner,
        object_key,
        normalized["enableDiarization"],
        normalized["maxSpeakers"],
    )
    return result
