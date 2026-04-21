# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
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
DEFAULT_LANGUAGE_CODE = os.environ.get("DEFAULT_LANGUAGE_CODE", "en-US")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# Human-friendly Transcribe job name = the callId. Must be 1–200 chars,
# alphanumeric + ._-. We already enforce that shape on callId in the
# initiator, so we can pass it straight through.
TRANSCRIBE_JOB_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]{1,200}$")

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
    """Extract ``<callId>`` from a key of the form ``lma-uploads-pending/<callId>/<filename>``.

    Returns ``None`` for any key that doesn't match that shape so the Lambda
    can idempotently ignore unrelated puts (e.g. transcript outputs landing
    back in the same bucket).
    """
    if not key.startswith(UPLOADS_PENDING_PREFIX):
        return None
    remainder = key[len(UPLOADS_PENDING_PREFIX):]
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
    """Update the UploadJob row. Best-effort — errors are logged but not raised,
    so a transient DynamoDB blip can't prevent us from retrying Transcribe."""
    pk = f"uj#{call_id}"
    now = datetime.now(timezone.utc).isoformat()
    # Build an UpdateExpression dynamically from ``extra`` to keep the per-status
    # call sites short.
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


def _emit_start_event(job: dict) -> None:
    """Put a ``START`` event on the Call Data Kinesis stream.

    Matches the shape produced by
    ``lma-websocket-transcriber-stack/source/app/src/calleventdata/transcribe.ts``
    so ``lma-ai-stack/source/lambda_functions/call_event_processor`` can
    ``createCall`` without any code changes.

    We intentionally omit ``AccessToken`` / ``IdToken`` / ``RefreshToken``:
    this is a server-side event, so ``call_event_processor.execute_create_call_mutation``
    will fall through to the "JWT decode failed" branch and use ``AgentId``
    as ``Owner`` (same path the virtual-participant integration uses). We set
    ``AgentId`` to the original Cognito caller recorded on the UploadJob row
    so UBAC attribution is preserved.
    """
    call_id = job["CallId"]
    owner = job.get("Owner") or job.get("AgentId") or "system@lma.aws"
    created_at = job.get("MeetingDateTime") or datetime.now(timezone.utc).isoformat()

    start_event = {
        "EventType": "START",
        "CallId": call_id,
        "CustomerPhoneNumber": job.get("FromNumber") or "Customer",
        "SystemPhoneNumber": job.get("ToNumber") or "System",
        # AgentId becomes Owner in call_event_processor when no JWT is present.
        "AgentId": owner,
        "CreatedAt": created_at,
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

    # TranscriptionJobName is globally-unique per account+region and hard-coded
    # by us to callId. If we retry (e.g. S3 event redelivery) the AWS SDK will
    # raise ConflictException which the handler below treats as idempotency.
    if not TRANSCRIBE_JOB_NAME_RE.match(call_id):
        raise ValueError(f"CallId {call_id!r} is not a valid TranscriptionJobName")

    settings: dict[str, Any] = {}
    if job.get("EnableDiarization"):
        max_speakers = int(job.get("MaxSpeakers") or 4)
        # Transcribe accepts 2–30 in ShowSpeakerLabels mode.
        max_speakers = max(2, min(30, max_speakers))
        settings["ShowSpeakerLabels"] = True
        settings["MaxSpeakerLabels"] = max_speakers

    language_code = (job.get("LanguageCode") or DEFAULT_LANGUAGE_CODE).strip()

    params: dict[str, Any] = {
        "TranscriptionJobName": call_id,
        "LanguageCode": language_code,
        "Media": {"MediaFileUri": media_file_uri},
        "OutputBucketName": bucket,
        "OutputKey": f"{TRANSCRIPTS_PREFIX}{call_id}.transcribe.json",
        # Tags let the Stage 3 finalizer cross-reference jobs to our stack.
        "Tags": [
            {"Key": "lma:callId", "Value": call_id},
            {"Key": "lma:source", "Value": "upload_meeting_processor"},
        ],
    }
    if settings:
        params["Settings"] = settings

    logger.info("StartTranscriptionJob: %s", json.dumps(params, default=str))
    try:
        transcribe_client.start_transcription_job(**params)
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        if code == "ConflictException":
            # Already running or completed — treat as success.
            logger.warning("Transcribe job %s already exists — treating as idempotent", call_id)
        else:
            logger.error("StartTranscriptionJob failed for %s: %s", call_id, err)
            raise
    return call_id


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def lambda_handler(event, context):  # noqa: ARG001
    """S3 ObjectCreated:* handler.

    One S3 batch may contain multiple records. We process them independently;
    a single bad record doesn't poison the rest. If any record raises, we
    re-raise at the end so Lambda's retry + DLQ (if configured) take over.
    """
    logger.debug("S3 event: %s", json.dumps(event, default=str))

    errors: list[tuple[str, Exception]] = []
    records = event.get("Records") or []

    for record in records:
        s3_info = record.get("s3") or {}
        bucket_obj = s3_info.get("bucket") or {}
        obj = s3_info.get("object") or {}
        bucket = bucket_obj.get("name", "")
        # S3 URL-encodes spaces and other special chars in the event payload.
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
                # Could be a manual s3 cp, a race with a deleted job row, or a
                # stale TTL'd row. Log + skip rather than fail loudly; the
                # orphan file will be cleaned up by the 7-day lifecycle rule.
                logger.warning(
                    "No UploadJob row for callId=%s (key=%s, size=%s) — skipping",
                    call_id, key, size,
                )
                continue

            status = job.get("Status")
            # Idempotency: second delivery of the same event, or a retry after
            # Transcribe already accepted the job. We intentionally allow
            # re-processing from any pre-UPLOADED state (PENDING_UPLOAD is
            # the normal case).
            if status in ("TRANSCRIBING", "COMPLETED", "FAILED"):
                logger.info(
                    "callId=%s already in status %s — skipping reprocessing",
                    call_id, status,
                )
                continue

            # Mark UPLOADED first so downstream observers can tell an object
            # landed even if the Kinesis or Transcribe call fails mid-flight.
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
                call_id, job_name,
            )
        except Exception as err:  # noqa: BLE001
            logger.exception(
                "Processing failed for callId=%s key=%s: %s",
                call_id, key, err,
            )
            # Best-effort surface the error on the job row so it's visible in
            # the UI (Stage 4) and via the CLI (`lma-cli logs …`).
            _mark_job(
                call_id,
                "ERROR",
                extra={"ErrorMessage": str(err)[:1024]},
            )
            errors.append((call_id, err))

    if errors:
        # Let Lambda retry per its event-source policy; partial-batch reporting
        # isn't useful here because S3→Lambda already splits records into
        # separate invocations in practice.
        raise RuntimeError(
            f"{len(errors)} of {len(records)} records failed: "
            + "; ".join(f"{cid}: {exc}" for cid, exc in errors)
        )

    return {"processed": len(records)}
