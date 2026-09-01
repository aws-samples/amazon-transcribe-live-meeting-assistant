# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Acquire, re-token and release ASR MicroVMs for the WebSocket transcriber.

Invoked synchronously by the transcriber task (one MicroVM per meeting):

    {"action": "acquire", "callId": "..."}   -> {"ok", "microvmId", "endpoint", "authToken", "expiresAt"}
    {"action": "token",   "microvmId": "..."} -> {"ok", "authToken", "expiresAt"}
    {"action": "release", "microvmId": "..."} -> {"ok"}

The transcriber owns the session and could call the lambda-microvms API itself,
but that API is absent from the AWS SDKs (see microvm_client.py), so the calls
live here in Python where the SigV4 shim already exists, and the task role only
needs lambda:InvokeFunction on this one function.

Soft failures are returned as {"ok": false, "reason": ...} rather than raised so
the transcriber can log a real reason and fall back to Amazon Transcribe for the
meeting instead of losing the transcript.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from microvm_client import MicrovmClient, MicrovmError

logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO"), logging.INFO))

microvms = MicrovmClient()

ASR_PORT = 8080
# CreateMicrovmAuthToken rejects anything above 60 minutes ("ExpirationMinutes
# 180 exceeded max allowed of 60"), so this is clamped rather than trusted. A
# meeting outliving its token is fine: the token authorizes the WebSocket
# upgrade, and a reconnect mints a fresh one through the "token" action.
MAX_TOKEN_TTL_MINUTES = 60
TOKEN_TTL_MINUTES = min(
    int(os.environ.get("TOKEN_TTL_MINUTES", str(MAX_TOKEN_TTL_MINUTES))),
    MAX_TOKEN_TTL_MINUTES,
)
MAX_MEETING_SECONDS = min(int(os.environ.get("MAX_MEETING_SECONDS", "14400")), 28800)
ACQUIRE_TIMEOUT_SECONDS = int(os.environ.get("ACQUIRE_TIMEOUT_SECONDS", "240"))
POLL_INTERVAL_SECONDS = 2

_REGION = os.environ.get("AWS_REGION", "us-east-1")
_CONNECTOR = "arn:aws:lambda:{}:aws:network-connector:aws-network-connector:{}"
INGRESS_CONNECTOR = _CONNECTOR.format(_REGION, "ALL_INGRESS")
EGRESS_CONNECTOR = os.environ.get("EGRESS_CONNECTOR_ARN") or _CONNECTOR.format(
    _REGION, "INTERNET_EGRESS"
)


def _client_token(call_id: str) -> str:
    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:32]


def _mint_token(microvm_id: str) -> dict:
    response = microvms.create_microvm_auth_token(
        microvm_identifier=microvm_id,
        expiration_in_minutes=TOKEN_TTL_MINUTES,
        allowed_ports=[{"port": ASR_PORT}],
    )
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_TTL_MINUTES)
    return {
        "authToken": response["authToken"]["X-aws-proxy-auth"],
        "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
    }


def _acquire(call_id: str) -> dict:
    if not call_id:
        return {"ok": False, "reason": "callId is required to acquire an ASR MicroVM"}

    try:
        response = microvms.run_microvm(
            imageIdentifier=os.environ["MICROVM_IMAGE_ARN"],
            executionRoleArn=os.environ["EXECUTION_ROLE_ARN"],
            ingressNetworkConnectors=[INGRESS_CONNECTOR],
            egressNetworkConnectors=[EGRESS_CONNECTOR],
            # Auto-suspend would stop transcribing mid-meeting, so idling is
            # allowed for the whole meeting and never suspended.
            idlePolicy={
                "autoResumeEnabled": False,
                "maxIdleDurationSeconds": MAX_MEETING_SECONDS,
                "suspendedDurationSeconds": 0,
            },
            maximumDurationInSeconds=MAX_MEETING_SECONDS,
            clientToken=_client_token(call_id),
        )
    except MicrovmError as exc:
        logger.error("RunMicrovm failed: %s", exc)
        return {"ok": False, "reason": f"RunMicrovm failed: {exc}"}
    except Exception as exc:  # noqa: BLE001 - the transcriber falls back on any reason
        logger.exception("RunMicrovm failed")
        return {"ok": False, "reason": f"RunMicrovm failed: {exc}"}

    microvm_id = response["microvmId"]
    endpoint = response.get("endpoint", "")
    state = response.get("state", "PENDING")
    logger.info("Started ASR MicroVM %s state=%s endpoint=%s", microvm_id, state, endpoint)

    deadline = time.time() + ACQUIRE_TIMEOUT_SECONDS
    while state in ("PENDING", "STARTING") and time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            state = microvms.get_microvm(microvm_id).get("state", state)
        except MicrovmError as exc:
            logger.warning("GetMicrovm failed while waiting: %s", exc)

    if state != "RUNNING":
        _release(microvm_id)
        return {
            "ok": False,
            "reason": f"ASR MicroVM did not reach RUNNING (state={state})",
            "microvmId": microvm_id,
        }

    if not endpoint:
        try:
            endpoint = microvms.get_microvm(microvm_id).get("endpoint", "")
        except MicrovmError as exc:
            logger.warning("Could not read the MicroVM endpoint: %s", exc)

    if not endpoint:
        _release(microvm_id)
        return {"ok": False, "reason": "ASR MicroVM has no endpoint"}

    try:
        token = _mint_token(microvm_id)
    except (MicrovmError, KeyError) as exc:
        logger.error("Could not mint an auth token for %s: %s", microvm_id, exc)
        _release(microvm_id)
        return {"ok": False, "reason": f"CreateMicrovmAuthToken failed: {exc}"}

    return {
        "ok": True,
        "reason": "",
        "microvmId": microvm_id,
        "endpoint": endpoint,
        "port": ASR_PORT,
        **token,
    }


def _release(microvm_id: str) -> dict:
    if not microvm_id:
        return {"ok": False, "reason": "microvmId is required to release an ASR MicroVM"}
    try:
        state = microvms.terminate_microvm(microvm_id).get("state", "")
        logger.info("Terminated ASR MicroVM %s state=%s", microvm_id, state)
        return {"ok": True, "reason": "", "microvmId": microvm_id, "state": state}
    except MicrovmError as exc:
        # A MicroVM that is already gone is a successful release.
        if exc.status == 404:
            logger.info("ASR MicroVM %s is already gone", microvm_id)
            return {"ok": True, "reason": "already terminated", "microvmId": microvm_id}
        logger.error("TerminateMicrovm failed for %s: %s", microvm_id, exc)
        return {"ok": False, "reason": f"TerminateMicrovm failed: {exc}"}


def lambda_handler(event, context):
    action = (event or {}).get("action", "")
    logger.info("ASR launcher action=%s", action)

    if action == "acquire":
        return _acquire(str(event.get("callId", "")))
    if action == "release":
        return _release(str(event.get("microvmId", "")))
    if action == "token":
        microvm_id = str(event.get("microvmId", ""))
        if not microvm_id:
            return {"ok": False, "reason": "microvmId is required to mint a token"}
        try:
            return {"ok": True, "reason": "", "microvmId": microvm_id, **_mint_token(microvm_id)}
        except (MicrovmError, KeyError) as exc:
            logger.error("Could not mint an auth token for %s: %s", microvm_id, exc)
            return {"ok": False, "reason": f"CreateMicrovmAuthToken failed: {exc}"}

    return {"ok": False, "reason": f"unknown action {action!r}"}
