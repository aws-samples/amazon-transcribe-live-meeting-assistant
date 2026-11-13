# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""
Upload Meeting Processor — S3 ObjectCreated trigger for the "upload a
pre-recorded meeting file" feature (Stage 2 of the LMA upload pipeline).

When the browser completes its presigned PUT to::

    s3://<RecordingsBucket>/lma-uploads-pending/<callId>/<filename>

S3 fires an ObjectCreated:* notification that invokes this Lambda. We:

1. Resolve the ``callId`` from the S3 key and load the ``uj#<callId>`` job row
   written by ``upload_meeting_initiator`` during the AppSync mutation.
2. Idempotency: if the job has already moved past ``PENDING_UPLOAD``
   (e.g. S3 redelivered the event, or a human re-ran us) we short-circuit.
3. Emit a ``START`` event onto the existing ``CallDataStream`` Kinesis stream
   with exactly the shape ``call_event_processor`` expects from the live
   websocket transcriber — so the unchanged LMA pipeline will ``createCall``,
   the meeting immediately appears in the Meetings list as ``STARTED``, and
   UBAC is enforced via ``AgentId`` → ``Owner`` (same fallback the virtual
   participant uses).
4. Start an Amazon Transcribe **batch** job on the uploaded media with the
   user-selected diarization settings and tags that let the Stage 3 finalizer
   look the job back up. The job output is written to the same bucket under
   ``lma-transcripts/<callId>.transcribe.json``.
5. Update the UploadJob row to ``TRANSCRIBING`` and record the Transcribe job
   name so Stage 3 (EventBridge ``Transcribe Job State Change`` rule) can
   finish the pipeline.

This Lambda does NOT finalize the meeting. Stage 3 (``upload_meeting_finalizer``)
consumes the Transcribe completion event, injects per-utterance segments as
``ADD_TRANSCRIPT_SEGMENT`` Kinesis events, promotes the media file from
``lma-uploads-pending/`` to ``lma-audio-recordings/``, emits
``ADD_S3_RECORDING_URL`` + ``END`` events, and lets the existing
``async_transcript_summary_orchestrator`` generate the Bedrock summary.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
EVENT_SOURCING_TABLE = os.environ["EVENT_SOURCING_TABLE"]
CALL_DATA_STREAM_NAME = os.environ["CALL_DATA_STREAM_NAME"]
S3_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]
UPLOADS_PENDING_PREFIX = os.environ.get("UPLOADS_PENDING_PREFIX", "lma-uploads-pending/")
TRANSCRIPTS_PREFIX = os.environ.get("TRANSCRIPTS_PREFIX", "lma-transcripts/")
#: The language of an upload whose UploadJob row names none: one started before
#: uploads followed the Configuration page's language.
LEGACY_LANGUAGE_CODE = "en-US"
#: Each identify mode Stage 1 records, as its StartTranscriptionJob parameter.
IDENTIFY_PARAMETERS = {
    "identify-language": "IdentifyLanguage",
    "identify-multiple-languages": "IdentifyMultipleLanguages",
}
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# The stack's customer-managed key. Amazon Transcribe encrypts the transcript it
# writes with SSE-S3 unless the job names a key, so the job names this one and
# the transcript is encrypted with the same key as the rest of the bucket.
OUTPUT_ENCRYPTION_KMS_KEY_ID = os.environ.get("OUTPUT_ENCRYPTION_KMS_KEY_ID", "")

# Transcribe job name == callId. Must be 1-200 chars, alphanumeric + ._-
TRANSCRIBE_JOB_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,200}$")

# StartTranscriptionJob's MediaFormat values. Stage 1 stores one of these on the
# row as MediaFormat; anything else (or none, on a row written before Stage 1
# stored it) leaves the parameter out for Transcribe to detect the format.
SUPPORTED_MEDIA_FORMATS = frozenset({"mp3", "mp4", "wav", "flac", "ogg", "amr", "webm", "m4a"})

# ---------------------------------------------------------------------------
# Logging / clients
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ddb = boto3.resource("dynamodb")
job_table = ddb.Table(EVENT_SOURCING_TABLE)
kinesis_client = boto3.client("kinesis", region_name=AWS_REGION)
transcribe_client = boto3.client("transcribe", region_name=AWS_REGION)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _call_id_from_key(key: str) -> str | None:
    """Extract ``<callId>`` from a key of the form
    ``lma-uploads-pending/<callId>/<filename>``. Returns ``None`` for any key
    that doesn't match that shape."""
    if not key.startswith(UPLOADS_PENDING_PREFIX):
        return None
    remainder = key[len(UPLOADS_PENDING_PREFIX) :]
    parts = remainder.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0]


def _load_upload_job(call_id: str) -> dict | None:
    """Fetch the UploadJob row written by ``upload_meeting_initiator``."""
    pk = f"uj#{call_id}"
    try:
        resp = job_table.get_item(Key={"PK": pk, "SK": pk}, ConsistentRead=True)
    except ClientError as err:
        logger.error("DDB get_item failed for %s: %s", pk, err)
        raise
    return resp.get("Item")


def _mark_job(call_id: str, status: str, extra: dict | None = None) -> None:
    """Update the UploadJob row. Best-effort — errors are logged but not raised."""
    pk = f"uj#{call_id}"
    now = datetime.now(timezone.utc).isoformat()
    set_parts: list[str] = [
        "#status = :status",
        "UpdatedAt = :now",
    ]
    expression_names: dict[str, str] = {"#status": "Status"}
    expression_values: dict[str, Any] = {":status": status, ":now": now}
    if extra:
        for i, (key, value) in enumerate(extra.items()):
            placeholder = f"#k{i}"
            valuekey = f":v{i}"
            set_parts.append(f"{placeholder} = {valuekey}")
            expression_names[placeholder] = key
            expression_values[valuekey] = value
    try:
        job_table.update_item(
            Key={"PK": pk, "SK": pk},
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=expression_names,
            ExpressionAttributeValues=expression_values,
        )
    except ClientError as err:
        logger.error("DDB update_item failed for %s: %s", pk, err)


def _language_settings(job: dict) -> dict[str, Any]:
    """The StartTranscriptionJob language parameters for the upload.

    Stage 1 records them when the upload starts: ``LanguageCode``, or
    ``LanguageIdentification`` (identify-language / identify-multiple-languages)
    with ``LanguageOptions``. A row written before uploads followed the
    Configuration page's language may name neither; it runs in en-US, as it would
    have then.
    """
    mode = (job.get("LanguageIdentification") or "").strip()
    if mode in IDENTIFY_PARAMETERS:
        return {
            IDENTIFY_PARAMETERS[mode]: True,
            "LanguageOptions": [str(code).strip() for code in job.get("LanguageOptions") or []],
        }
    return {"LanguageCode": (job.get("LanguageCode") or LEGACY_LANGUAGE_CODE).strip()}


def _transcription_language(job: dict) -> str:
    """The meeting's TranscriptionLanguage: the language code, or the identify mode.

    The meeting's TranscriptionLanguage keeps the identify mode, as for a
    streaming meeting; Stage 3 lists the identified languages as its
    DetectedLanguages through each segment's LanguageCode.
    """
    mode = (job.get("LanguageIdentification") or "").strip()
    if mode in IDENTIFY_PARAMETERS:
        return mode
    return _language_settings(job)["LanguageCode"]


def _emit_start_event(job: dict) -> None:
    """Put a ``START`` event on the Call Data Kinesis stream.

    Matches the shape produced by the live websocket transcriber so
    ``call_event_processor`` can ``createCall`` without any code changes.
    AccessToken/IdToken/RefreshToken are intentionally omitted — without a
    JWT, ``call_event_processor`` uses ``AgentId`` as ``Owner`` (same path
    the virtual-participant integration takes), which preserves UBAC
    attribution to the original Cognito caller.
    """
    call_id = job["CallId"]
    owner = job.get("Owner") or job.get("AgentId") or "system@lma.aws"
    created_at = job.get("MeetingDateTime") or datetime.now(timezone.utc).isoformat()

    start_event = {
        "EventType": "START",
        "CallId": call_id,
        "CustomerPhoneNumber": job.get("FromNumber") or "Other Participant",
        "SystemPhoneNumber": job.get("ToNumber") or "System",
        "AgentId": owner,
        "CreatedAt": created_at,
        # How the meeting is captured and transcribed (Call.CaptureSource and the
        # fields beside it): an Amazon Transcribe batch job over the uploaded
        # file, whose transcript is all on the REMOTE channel.
        "CaptureSource": "UPLOAD",
        "TranscriptionEngine": "TRANSCRIBE",
        "TranscriptionLanguage": _transcription_language(job),
        "SpeakerSeparation": "REMOTE" if job.get("EnableDiarization") else "NONE",
    }
    logger.info("Emitting START to Kinesis: %s", json.dumps(start_event, default=str))
    kinesis_client.put_record(
        StreamName=CALL_DATA_STREAM_NAME,
        PartitionKey=call_id,
        Data=json.dumps(start_event).encode("utf-8"),
    )


def _start_transcription_job(job: dict) -> str:
    """Kick off an Amazon Transcribe batch job. Returns the TranscriptionJobName."""
    call_id = job["CallId"]
    bucket = job["PendingObjectBucket"]
    key = job["PendingObjectKey"]
    media_file_uri = f"s3://{bucket}/{key}"

    # TranscriptionJobName == callId. Retries raise ConflictException,
    # handled as idempotency below.
    if not TRANSCRIBE_JOB_NAME_RE.match(call_id):
        raise ValueError(f"CallId {call_id!r} is not a valid TranscriptionJobName")

    settings: dict[str, Any] = {}
    if job.get("EnableDiarization"):
        max_speakers = int(job.get("MaxSpeakers") or 4)
        # Transcribe accepts 2-30 speakers in ShowSpeakerLabels mode.
        max_speakers = max(2, min(30, max_speakers))
        settings["ShowSpeakerLabels"] = True
        settings["MaxSpeakerLabels"] = max_speakers

    params: dict[str, Any] = {
        "TranscriptionJobName": call_id,
        **_language_settings(job),
        "Media": {"MediaFileUri": media_file_uri},
        "OutputBucketName": bucket,
        "OutputKey": f"{TRANSCRIPTS_PREFIX}{call_id}.transcribe.json",
        # The `lma:source` tag is used by the Stage 3 finalizer to filter jobs.
        "Tags": [
            {"Key": "lma:callId", "Value": call_id},
            {"Key": "lma:source", "Value": "upload_meeting_processor"},
        ],
    }
    if settings:
        params["Settings"] = settings
    if OUTPUT_ENCRYPTION_KMS_KEY_ID:
        params["OutputEncryptionKMSKeyId"] = OUTPUT_ENCRYPTION_KMS_KEY_ID
    media_format = str(job.get("MediaFormat") or "").lower()
    if media_format in SUPPORTED_MEDIA_FORMATS:
        params["MediaFormat"] = media_format

    logger.info("StartTranscriptionJob: %s", json.dumps(params, default=str))
    try:
        transcribe_client.start_transcription_job(**params)
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code == "ConflictException":
            logger.warning("Transcribe job %s already exists — treating as idempotent", call_id)
        else:
            logger.error("StartTranscriptionJob failed for %s: %s", call_id, err)
            raise
    return call_id


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def lambda_handler(event, context):  # noqa: ARG001
    """S3 ObjectCreated:* handler. Processes records independently; re-raises
    at the end if any failed so Lambda retries take over."""
    logger.debug("S3 event: %s", json.dumps(event, default=str))

    errors: list[tuple[str, Exception]] = []
    records = event.get("Records") or []

    for record in records:
        s3_info = record.get("s3") or {}
        bucket_obj = s3_info.get("bucket") or {}
        obj = s3_info.get("object") or {}
        bucket = bucket_obj.get("name", "")
        key = urllib.parse.unquote_plus(obj.get("key", ""))
        size = obj.get("size", 0)

        if bucket != S3_BUCKET_NAME:
            logger.info("Ignoring event for unexpected bucket: %s", bucket)
            continue
        if not key.startswith(UPLOADS_PENDING_PREFIX):
            logger.info("Ignoring event outside the pending prefix: %s", key)
            continue

        call_id = _call_id_from_key(key)
        if not call_id:
            logger.warning("Could not parse callId from key %r — skipping", key)
            continue

        try:
            job = _load_upload_job(call_id)
            if not job:
                # Orphan files are cleaned up by the 7-day lifecycle rule.
                logger.warning(
                    "No UploadJob row for callId=%s (key=%s, size=%s) — skipping",
                    call_id,
                    key,
                    size,
                )
                continue

            status = job.get("Status")
            # Idempotency: skip reprocessing if the job has already advanced.
            if status in ("TRANSCRIBING", "COMPLETED", "FAILED"):
                logger.info(
                    "callId=%s already in status %s — skipping reprocessing",
                    call_id,
                    status,
                )
                continue

            _mark_job(
                call_id,
                "UPLOADED",
                extra={
                    "UploadedObjectSize": int(size) if size else None,
                    "UploadedAt": datetime.now(timezone.utc).isoformat(),
                },
            )

            _emit_start_event(job)
            job_name = _start_transcription_job(job)

            _mark_job(
                call_id,
                "TRANSCRIBING",
                extra={"TranscriptionJobName": job_name},
            )
            logger.info(
                "callId=%s: START emitted, Transcribe job %s started, status=TRANSCRIBING",
                call_id,
                job_name,
            )
        except Exception as err:  # noqa: BLE001
            logger.exception(
                "Processing failed for callId=%s key=%s: %s",
                call_id,
                key,
                err,
            )
            _mark_job(
                call_id,
                "ERROR",
                extra={"ErrorMessage": str(err)[:1024]},
            )
            errors.append((call_id, err))

    if errors:
        raise RuntimeError(
            f"{len(errors)} of {len(records)} records failed: "
            + "; ".join(f"{cid}: {exc}" for cid, exc in errors)
        )

    return {"processed": len(records)}
