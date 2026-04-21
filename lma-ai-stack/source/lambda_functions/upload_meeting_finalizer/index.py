# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Upload Meeting Finalizer — Stage 3 of the upload pipeline.

Triggered by an EventBridge rule on ``Transcribe Job State Change`` events for
jobs in the ``COMPLETED`` (or ``FAILED``) states. Only reacts to jobs tagged
with ``lma:source=upload_meeting_processor`` (set by Stage 2) so it ignores any
other Transcribe traffic in the account.

When a job completes successfully the finalizer:

1. Looks up the UploadJob row ``uj#<callId>``.
2. Downloads the transcript JSON written by Amazon Transcribe to
   ``s3://<bucket>/lma-transcripts/<callId>.transcribe.json``.
3. For each utterance in ``results.audio_segments[]`` (or falls back to building
   them from ``results.items[]`` if Transcribe's preferred representation isn't
   present) emits an ``ADD_TRANSCRIPT_SEGMENT`` event onto ``CallDataStream``
   with the shape the existing ``call_event_processor`` expects from the live
   websocket transcriber. Channel mapping:
     * Non-diarization jobs → all segments are ``CALLER``.
     * Diarization jobs (``ShowSpeakerLabels=True``) → ``spk_0`` is ``CALLER``,
       everyone else maps to ``AGENT`` (LMA only supports two channels). The
       utterance's Speaker label is still set to ``spk_N`` so the UI can show
       distinct speakers.
4. Copies the media file from ``lma-uploads-pending/<callId>/...`` to
   ``lma-audio-recordings/<callId>.<ext>`` (matching the filename LMA's player
   expects) and removes the pending copy.
5. Emits ``ADD_S3_RECORDING_URL`` pointing at the promoted file, then ``END``
   so the existing summary orchestrator fires Bedrock and the meeting status
   flips to ``ENDED`` in the UI.
6. Updates the UploadJob row to ``COMPLETED`` (or ``FAILED`` on the error
   branch, with the reason from Transcribe).

If the Transcribe job FAILED this function marks the UploadJob row as ``FAILED``
with the ``FailureReason`` and still emits ``END`` so the meeting moves out of
the "in progress" state in the UI. A future iteration could add a UI badge.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from datetime import datetime, timezone
from typing import Any
from urllib.request import urlopen

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
EVENT_SOURCING_TABLE = os.environ["EVENT_SOURCING_TABLE"]
CALL_DATA_STREAM_NAME = os.environ["CALL_DATA_STREAM_NAME"]
S3_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]
UPLOADS_PENDING_PREFIX = os.environ.get("UPLOADS_PENDING_PREFIX", "lma-uploads-pending/")
RECORDINGS_PREFIX = os.environ.get("RECORDINGS_PREFIX", "lma-audio-recordings/")
TRANSCRIPTS_PREFIX = os.environ.get("TRANSCRIPTS_PREFIX", "lma-transcripts/")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# ---------------------------------------------------------------------------
# Logging / clients
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ddb = boto3.resource("dynamodb")
job_table = ddb.Table(EVENT_SOURCING_TABLE)
kinesis_client = boto3.client("kinesis", region_name=AWS_REGION)
transcribe_client = boto3.client("transcribe", region_name=AWS_REGION)
s3_client = boto3.client("s3", region_name=AWS_REGION)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_upload_job(call_id: str) -> dict | None:
    pk = f"uj#{call_id}"
    resp = job_table.get_item(Key={"PK": pk, "SK": pk}, ConsistentRead=True)
    return resp.get("Item")


def _mark_job(call_id: str, status: str, extra: dict | None = None) -> None:
    """Mirror of the processor's marker helper (best-effort, doesn't raise)."""
    pk = f"uj#{call_id}"
    set_parts = ["#status = :status", "UpdatedAt = :now"]
    expression_names: dict[str, str] = {"#status": "Status"}
    expression_values: dict[str, Any] = {":status": status, ":now": _now_iso()}
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
        logger.error("DDB update_item failed for uj#%s: %s", call_id, err)


def _put_kinesis(partition_key: str, payload: dict) -> None:
    kinesis_client.put_record(
        StreamName=CALL_DATA_STREAM_NAME,
        PartitionKey=partition_key,
        Data=json.dumps(payload, default=str).encode("utf-8"),
    )


def _job_is_from_upload_meeting(job_name: str) -> bool:
    """Return True if the Transcribe job was started by our Stage-2 Lambda
    (identified by the ``lma:source=upload_meeting_processor`` tag)."""
    try:
        arn = (
            f"arn:aws:transcribe:{AWS_REGION}:{os.environ.get('ACCOUNT_ID','')}"
            f":transcription-job/{job_name}"
        )
        # Prefer GetTranscriptionJob because it also returns the job's
        # OutputKey + Tags in one call; we already need those.
        resp = transcribe_client.get_transcription_job(TranscriptionJobName=job_name)
        for tag in resp["TranscriptionJob"].get("Tags") or []:
            if tag.get("Key") == "lma:source" and tag.get("Value") == "upload_meeting_processor":
                return True
    except ClientError as err:
        logger.warning(
            "Could not fetch Transcribe job %s to check tags: %s", job_name, err
        )
    return False


def _fetch_transcript_json(bucket: str, key: str) -> dict:
    """Read the Transcribe output JSON from S3. We chose OutputBucketName at job
    start time so this path is predictable — no need to follow the presigned
    TranscriptFileUri."""
    resp = s3_client.get_object(Bucket=bucket, Key=key)
    return json.loads(resp["Body"].read())


def _build_segments(
    transcript_json: dict,
    enable_diarization: bool,
) -> list[dict]:
    """Turn a Transcribe output JSON into a list of segment dicts in the shape
    ``call_event_processor.add_transcript_segments`` consumes.

    Preferred source is ``results.audio_segments[]`` which Transcribe produces
    as sentence-like chunks with start/end/transcript. If the job was run
    with diarization each item in ``audio_segments`` additionally has a
    ``speaker_label`` (e.g. ``spk_0``).
    """
    results = transcript_json.get("results") or {}
    audio_segments = results.get("audio_segments") or []

    # Fallback: if audio_segments absent (older Transcribe jobs or edge cases),
    # glue individual pronunciations into one big utterance. Better than nothing.
    if not audio_segments:
        transcripts = results.get("transcripts") or []
        text = " ".join(t.get("transcript", "") for t in transcripts).strip()
        if not text:
            return []
        return [
            {
                "SegmentId": "seg-0",
                "Transcript": text,
                "StartTime": 0.0,
                "EndTime": 0.0,
                "Speaker": "spk_0",
            }
        ]

    segments = []
    for i, seg in enumerate(audio_segments):
        transcript = (seg.get("transcript") or "").strip()
        if not transcript:
            continue
        # `start_time` / `end_time` come as strings.
        try:
            start = float(seg.get("start_time") or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        try:
            end = float(seg.get("end_time") or 0.0)
        except (TypeError, ValueError):
            end = 0.0
        # speaker_label is only present when diarization was requested.
        speaker_label = seg.get("speaker_label") or "spk_0"
        segments.append(
            {
                "SegmentId": f"seg-{i:05d}",
                "Transcript": transcript,
                "StartTime": start,
                "EndTime": end,
                "Speaker": speaker_label,
            }
        )
    return segments


def _channel_for_speaker(speaker_label: str, enable_diarization: bool) -> str:
    """Map a Transcribe speaker_label into the 2-valued LMA Channel enum.

    - Non-diarization: everything is CALLER. (The existing LMA UI is built
      around a 2-party call; uploaded meetings without diarization are visually
      single-stream and that's correct.)
    - Diarization: first speaker (spk_0) → CALLER, everyone else → AGENT.
      The original speaker_label survives as Segment.Speaker, so the UI can
      still show e.g. "Speaker 2 (spk_3)".
    """
    if not enable_diarization:
        return "CALLER"
    if speaker_label in ("spk_0", "ch_0"):
        return "CALLER"
    return "AGENT"


def _promote_media_file(
    bucket: str, pending_key: str, call_id: str
) -> tuple[str, str]:
    """Copy the uploaded media from ``lma-uploads-pending/...`` to
    ``lma-audio-recordings/<callId>.<ext>`` and delete the pending copy.

    Returns (new_key, new_s3_url).
    """
    # Preserve the extension. Filename under pending is lma-uploads-pending/<callId>/<originalName>.
    original_name = pending_key.rsplit("/", 1)[-1]
    ext = original_name.rsplit(".", 1)[-1] if "." in original_name else "bin"
    new_key = f"{RECORDINGS_PREFIX}{call_id}.{ext}"

    logger.info("Copying s3://%s/%s → s3://%s/%s", bucket, pending_key, bucket, new_key)
    s3_client.copy_object(
        Bucket=bucket,
        CopySource={"Bucket": bucket, "Key": pending_key},
        Key=new_key,
        MetadataDirective="COPY",
    )
    # Best effort: delete the pending object. If this fails the 7-day lifecycle
    # rule will clean it up; no need to raise.
    try:
        s3_client.delete_object(Bucket=bucket, Key=pending_key)
    except ClientError as err:
        logger.warning("Could not delete pending object %s: %s", pending_key, err)

    return new_key, f"s3://{bucket}/{new_key}"


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def lambda_handler(event, context):  # noqa: ARG001
    """EventBridge handler for `Transcribe Job State Change`.

    The event shape:

    {
      "source": "aws.transcribe",
      "detail-type": "Transcribe Job State Change",
      "detail": {
        "TranscriptionJobName": "<callId>",
        "TranscriptionJobStatus": "COMPLETED" | "FAILED",
        "FailureReason": "..."      # only on FAILED
      }
    }
    """
    logger.info("event=%s", json.dumps(event, default=str))

    detail = event.get("detail") or {}
    job_name = detail.get("TranscriptionJobName")
    job_status = detail.get("TranscriptionJobStatus")
    failure_reason = detail.get("FailureReason")
    if not job_name or not job_status:
        logger.warning("Missing TranscriptionJobName or status — nothing to do")
        return {"ok": False, "reason": "missing fields"}

    # Only handle jobs Stage 2 started. We can't fetch Tags *and* also check the
    # status in one API call without side-effects, so we do get_transcription_job
    # once and reuse it below.
    try:
        resp = transcribe_client.get_transcription_job(TranscriptionJobName=job_name)
    except ClientError as err:
        logger.warning("get_transcription_job(%s) failed: %s", job_name, err)
        return {"ok": False, "reason": str(err)}

    tj = resp["TranscriptionJob"]
    tags = tj.get("Tags") or []
    is_ours = any(
        t.get("Key") == "lma:source" and t.get("Value") == "upload_meeting_processor"
        for t in tags
    )
    if not is_ours:
        logger.info(
            "Job %s is not from upload_meeting_processor (tags=%s) — skipping",
            job_name, tags,
        )
        return {"ok": True, "skipped": "not-ours"}

    # call_id == job_name by convention (we set it in Stage 2).
    call_id = job_name

    # Load the UploadJob row so we know the bucket/key, owner, and diarization flag.
    job = _get_upload_job(call_id)
    if not job:
        logger.warning("No UploadJob row uj#%s — skipping", call_id)
        return {"ok": True, "skipped": "no-job-row"}

    bucket = job.get("PendingObjectBucket") or S3_BUCKET_NAME
    pending_key = job.get("PendingObjectKey")
    enable_diarization = bool(job.get("EnableDiarization"))

    # --- FAILED branch: surface the failure, emit END so UI moves on ---
    if job_status == "FAILED":
        logger.error("Transcribe job %s FAILED: %s", job_name, failure_reason)
        _mark_job(
            call_id,
            "FAILED",
            extra={"ErrorMessage": (failure_reason or "Transcribe job failed")[:1024]},
        )
        _emit_end_event(job, call_id)
        return {"ok": False, "reason": failure_reason or "FAILED"}

    if job_status != "COMPLETED":
        logger.info("Ignoring intermediate status %s for %s", job_status, job_name)
        return {"ok": True, "ignored": job_status}

    # --- COMPLETED branch: happy path ---
    transcript_key = f"{TRANSCRIPTS_PREFIX}{call_id}.transcribe.json"
    try:
        transcript_json = _fetch_transcript_json(bucket, transcript_key)
    except ClientError as err:
        logger.exception("Could not read transcript JSON: %s", err)
        _mark_job(
            call_id,
            "FAILED",
            extra={"ErrorMessage": f"Could not read transcript JSON: {err}"[:1024]},
        )
        _emit_end_event(job, call_id)
        return {"ok": False, "reason": str(err)}

    segments = _build_segments(transcript_json, enable_diarization)
    logger.info("Emitting %d transcript segment(s) for %s", len(segments), call_id)

    now = _now_iso()
    # Each segment is emitted as its own ADD_TRANSCRIPT_SEGMENT record.
    # call_event_processor's Kinesis consumer fans in batches of 200 so the
    # throughput is fine for reasonable-size meetings (an hour of speech is
    # typically a few hundred audio_segments).
    for seg in segments:
        channel = _channel_for_speaker(seg["Speaker"], enable_diarization)
        event_record = {
            "EventType": "ADD_TRANSCRIPT_SEGMENT",
            "CallId": call_id,
            "Channel": channel,
            "SegmentId": seg["SegmentId"],
            "StartTime": seg["StartTime"],
            "EndTime": seg["EndTime"],
            "Transcript": seg["Transcript"],
            "IsPartial": False,   # batch-mode Transcribe only gives us final segments
            "Speaker": seg["Speaker"],
            "CreatedAt": now,
            "UpdatedAt": now,
        }
        _put_kinesis(call_id, event_record)

    # Promote media from pending → audio-recordings and emit the URL.
    recording_s3_url = None
    if pending_key:
        try:
            _, recording_s3_url = _promote_media_file(bucket, pending_key, call_id)
        except ClientError as err:
            logger.exception("Could not promote media file: %s", err)

    if recording_s3_url:
        _put_kinesis(
            call_id,
            {
                "EventType": "ADD_S3_RECORDING_URL",
                "CallId": call_id,
                "RecordingUrl": recording_s3_url,
                "CreatedAt": _now_iso(),
                "UpdatedAt": _now_iso(),
            },
        )

    # END event — triggers existing summary orchestrator.
    _emit_end_event(job, call_id)

    _mark_job(
        call_id,
        "COMPLETED",
        extra={
            "TranscriptKey": transcript_key,
            "RecordingS3Url": recording_s3_url or "",
            "SegmentCount": len(segments),
        },
    )
    logger.info("Finalized callId=%s with %d segments", call_id, len(segments))
    return {"ok": True, "segments": len(segments), "recordingUrl": recording_s3_url}


def _emit_end_event(job: dict, call_id: str) -> None:
    """Emit an END event on Kinesis. Reuses the same AgentId fallback path
    as the START event so the Owner attribution lines up."""
    owner = job.get("Owner") or job.get("AgentId") or "system@lma.aws"
    now = _now_iso()
    _put_kinesis(
        call_id,
        {
            "EventType": "END",
            "CallId": call_id,
            "CustomerPhoneNumber": job.get("FromNumber") or "Customer",
            "SystemPhoneNumber": job.get("ToNumber") or "System",
            "AgentId": owner,
            "CreatedAt": now,
            "UpdatedAt": now,
        },
    )
