# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Upload driver — pre-recorded meeting via ``createUploadMeeting`` + S3 PUT.

Flow per meeting (mirrors the UI's Upload Audio feature):

1. Call the ``createUploadMeeting`` AppSync mutation as the synthetic user —
   backend returns a presigned S3 PUT URL scoped to
   ``lma-uploads-pending/<callId>/<filename>``.
2. PUT the WAV fixture bytes directly to S3.
3. S3 object-created notification → upload_meeting_processor Lambda emits
   ``START`` to Kinesis and starts an Amazon Transcribe **batch** job.
4. When Transcribe finishes, upload_meeting_finalizer emits
   ``ADD_TRANSCRIPT_SEGMENT`` per utterance and an ``END`` event, and the
   summary orchestrator kicks a Bedrock summary.

This driver submits all uploads in parallel and then returns immediately — it
does *not* block on Transcribe batch completion (that happens asynchronously
in the backend pipeline and is observable through CloudWatch).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from lma_load.auth.cognito import SyntheticUserPool
from lma_load.run_context import RunContext
from lma_load.stack_info import LMAStackInfo

logger = logging.getLogger(__name__)


_CREATE_UPLOAD_MEETING = """
mutation CreateUploadMeeting($input: CreateUploadMeetingInput!) {
  createUploadMeeting(input: $input) {
    callId
    uploadUrl
    uploadBucket
    uploadKey
    contentType
    expiresInSeconds
  }
}
"""


def _resolve_wav_bytes(override: str | None) -> bytes:
    if override:
        return Path(override).expanduser().resolve().read_bytes()
    try:
        with resources.as_file(
            resources.files("lma_load").joinpath("fixtures", "stereo-16k-30s.wav")
        ) as path:
            return path.read_bytes()
    except (FileNotFoundError, ModuleNotFoundError) as err:
        raise RuntimeError(
            "No --wav provided and shipped fixture was not found. "
            "Pass --wav <path>."
        ) from err


# ---------------------------------------------------------------------------
# Per-meeting coroutine
# ---------------------------------------------------------------------------
async def _upload_one(
    idx: int,
    appsync_url: str,
    wav_bytes: bytes,
    jwt_id: str,
    run_id: str,
    agent_label: str,
) -> dict[str, Any]:
    import aiohttp  # optional-dep

    call_id = f"loadtest-{run_id}-upload-{idx:05d}"
    filename = "synthetic.wav"
    input_obj = {
        "callId": call_id,
        "meetingTopic": f"LoadTest Upload {idx:05d}",
        "agentId": agent_label,
        "fromNumber": "LoadSim-Customer",
        "toNumber": "LoadSim-System",
        "filename": filename,
        "contentType": "audio/wav",
        "fileSize": len(wav_bytes),
        "enableDiarization": False,
        "maxSpeakers": 2,
        "meetingDateTime": datetime.now(timezone.utc).isoformat(),
    }
    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession() as session:
            # 1) AppSync mutation — Cognito User Pools JWT auth (ID token).
            async with session.post(
                appsync_url,
                headers={
                    "authorization": jwt_id,
                    "content-type": "application/json",
                },
                json={
                    "query": _CREATE_UPLOAD_MEETING,
                    "variables": {"input": input_obj},
                },
            ) as resp:
                body = await resp.json()
            if "errors" in body and body["errors"]:
                raise RuntimeError(
                    f"createUploadMeeting failed: {body['errors']}"
                )
            out = body["data"]["createUploadMeeting"]
            upload_url = out["uploadUrl"]
            upload_content_type = out["contentType"]

            # 2) Direct S3 PUT of the WAV bytes to the presigned URL.
            async with session.put(
                upload_url,
                data=wav_bytes,
                headers={"content-type": upload_content_type},
            ) as put_resp:
                if put_resp.status not in (200, 201):
                    raise RuntimeError(
                        f"S3 PUT returned {put_resp.status}: {await put_resp.text()}"
                    )
        return {
            "callId": out["callId"],
            "status": "ok",
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            "bytes": len(wav_bytes),
        }
    except Exception as err:  # noqa: BLE001
        return {
            "callId": call_id,
            "status": "error",
            "error": str(err),
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
        }


# ---------------------------------------------------------------------------
# Driver entry point
# ---------------------------------------------------------------------------
async def drive_upload(ctx: RunContext, stack: LMAStackInfo, params) -> list[dict]:
    if not params.email_prefix or not params.email_domain:
        raise RuntimeError(
            "--email-prefix and --email-domain are required for the upload driver."
        )

    pool = SyntheticUserPool(
        user_pool_id=stack.user_pool_id,
        user_pool_client_id=stack.user_pool_client_id,
        region=ctx.region,
        run_id=ctx.run_id,
        profile=ctx.profile,
    )
    user_count = max(1, params.user_pool_size or min(params.concurrency, params.meetings))
    pool.provision(
        count=user_count,
        email_prefix=params.email_prefix,
        email_domain=params.email_domain,
    )
    pool.authenticate_all()
    users_with_tokens = [u for u in pool.users if u.id_token]
    if not users_with_tokens:
        raise RuntimeError("No synthetic users authenticated.")

    wav_bytes = _resolve_wav_bytes(params.wav_path)
    semaphore = asyncio.Semaphore(params.concurrency)
    rng = random.Random(ctx.run_id)

    async def _bounded(idx: int) -> dict:
        await asyncio.sleep(
            (idx / max(params.meetings, 1)) * params.ramp_s
            + rng.random() * params.jitter_s
        )
        async with semaphore:
            user = users_with_tokens[(idx - 1) % len(users_with_tokens)]
            return await _upload_one(
                idx=idx,
                appsync_url=stack.appsync_graphql_url,
                wav_bytes=wav_bytes,
                jwt_id=user.id_token,
                run_id=ctx.run_id,
                agent_label=user.email,
            )

    tasks = [asyncio.create_task(_bounded(i)) for i in range(1, params.meetings + 1)]
    out: list[dict] = []
    for t in asyncio.as_completed(tasks):
        try:
            out.append(await t)
        except Exception as err:  # noqa: BLE001
            logger.exception("upload driver meeting failed: %s", err)
            out.append({"status": "error", "error": str(err)})
    return out
