"""Launch one Lambda MicroVM per Virtual Participant meeting.

Invoked two ways:
  - by the VP scheduler state machine (immediate "start now" meetings),
    with the state machine's {apiInfo, data} payload;
  - by EventBridge Scheduler for meetings booked in advance, with a
    flat payload of the same per-meeting fields.

Per-meeting config CANNOT be passed as environment variables: MicroVM
image env vars are baked into the image and shared by every MicroVM
launched from it. It goes in runHookPayload instead, which the
container's /run hook applies before starting the VP app.

Returns {"ok": bool, "reason": str, ...} rather than raising for soft
failures, so the state machine can route them to MarkVPFailed and show
the user a real reason (mirroring how CheckRunTaskFailures inspects
the ECS RunTask result).
"""

import json
import logging
import os
import time

import boto3

from microvm_client import MicrovmClient, MicrovmError

logger = logging.getLogger()
logger.setLevel(getattr(logging, os.environ.get("LOG_LEVEL", "INFO")))

# Not boto3.client("lambda-microvms"): the Lambda runtime's bundled botocore has
# no service model for that service (UnknownServiceError). See microvm_client.py.
microvms = MicrovmClient()
dynamodb = boto3.client("dynamodb")

# Must stay in sync with PER_MEETING_KEYS in the container's
# src/launch-mode.ts. Anything not listed here is ignored by the
# container, so adding a value needs a change in both places.
FIELD_MAP = {
    "VIRTUAL_PARTICIPANT_ID": ("virtualParticipantId", "id"),
    "MEETING_PLATFORM": ("meetingPlatform",),
    "MEETING_ID": ("meetingId",),
    "MEETING_PASSWORD": ("meetingPassword",),
    "MEETING_NAME": ("meetingName",),
    "MEETING_TIME": ("meetingTime",),
    "LMA_USER": ("userName", "owner"),
    "LMA_USER_SUB": ("userSub",),
    "USER_ACCESS_TOKEN": ("accessToken",),
    # NOTE: 'rereshToken' is the existing (misspelled) key used by the
    # state machine and the UI. Accept both so this keeps working if
    # the typo is ever fixed.
    "USER_REFRESH_TOKEN": ("rereshToken", "refreshToken"),
    "USER_ID_TOKEN": ("idToken",),
    "ZOOM_CREDENTIALS_SECRET_NAME": ("zoomCredentialsSecretName",),
    "ENABLE_VIDEO_RECORDING": ("enableVideoRecording",),
}

SECRET_KEYS = {
    "USER_ACCESS_TOKEN",
    "USER_ID_TOKEN",
    "USER_REFRESH_TOKEN",
    "MEETING_PASSWORD",
}

# The AWS developer guide says runHookPayload allows 16 KB, but the SERVICE
# enforces 4096 (see RunMicrovmRequestRunHookPayloadString in the
# lambda-microvms service model: {"max": 4096}). A live launch failed with
# "Value at 'runHookPayload' failed to satisfy constraint: Member must have
# length less than or equal to 4096".
#
# 4096 is not enough: three Cognito JWTs alone are ~3.6 KB, so the per-meeting
# values alone measured 4086 bytes with no room for the ~3.7 KB of static stack
# config. So the payload carries only a POINTER — the vpId — and the container
# reads the full configuration from the VP task registry, which the launcher
# writes first. The registry is KMS-encrypted and TTL'd, and the container
# already reads it for the MicroVM endpoint.
RUN_HOOK_PAYLOAD_MAX_BYTES = 4096

# Static, per-stack configuration (GraphQL endpoint, S3 buckets, Transcribe
# settings, voice-assistant config, ...) that the ECS task definition supplies as
# ~60 container environment variables.
#
# These CANNOT be MicroVM image environment variables:
#   - the image caps at 50, and 60 are needed;
#   - they are per-stack values, so baking them into the image would force a full
#     image rebuild (minutes) on every parameter change.
#
# So they travel in runHookPayload alongside the per-meeting values. Measured at
# ~3.7 KB for a real stack, leaving room for the three Cognito JWTs inside the
# 16 KB limit. STATIC_CONFIG_JSON is populated from the CloudFormation template,
# which is where these values already live.
# Read from the ECS task definition rather than duplicating ~60 values into this
# Lambda's environment: the task definition is where they already live, so there
# is exactly one source of truth and no drift when a stack parameter changes.
# (The task definition is created under MICROVM too — it is unused for running
# tasks, but it carries the resolved configuration.)
_STATIC_CONFIG_CACHE: dict | None = None


def _static_config() -> dict:
    """Static per-stack config, from the VP ECS task definition.

    Cached for the life of the execution environment: the values only change on a
    stack update, which replaces this Lambda anyway.
    """
    global _STATIC_CONFIG_CACHE
    if _STATIC_CONFIG_CACHE is not None:
        return _STATIC_CONFIG_CACHE

    task_definition = os.environ.get("TASK_DEFINITION_ARN", "")
    if not task_definition:
        logger.warning("TASK_DEFINITION_ARN not set - no static config available")
        _STATIC_CONFIG_CACHE = {}
        return _STATIC_CONFIG_CACHE

    try:
        described = boto3.client("ecs").describe_task_definition(
            taskDefinition=task_definition
        )
        containers = described["taskDefinition"]["containerDefinitions"]
        env = containers[0].get("environment", [])
        # Per-meeting values are supplied per launch and must not be frozen here.
        config = {
            item["name"]: item["value"]
            for item in env
            if item["name"] not in FIELD_MAP and item.get("value")
        }
        logger.info("Loaded %d static config values from the task definition", len(config))
        _STATIC_CONFIG_CACHE = config
    except Exception:  # noqa: BLE001 - degrade rather than fail the meeting
        logger.exception("Could not read static config from the task definition")
        _STATIC_CONFIG_CACHE = {}
    return _STATIC_CONFIG_CACHE

# Meetings can run long, but a MicroVM's hard ceiling is 8 hours.
MAX_DURATION_SECONDS = 28800

# Lambda-managed connectors. ALL_INGRESS exposes the VM's ports via its
# dedicated HTTPS endpoint (needed for noVNC on 5901); INTERNET_EGRESS
# lets the VP reach the meeting platforms and AWS public APIs.
_REGION = os.environ.get("AWS_REGION", "us-east-1")
_CONNECTOR = "arn:aws:lambda:{}:aws:network-connector:aws-network-connector:{}"
INGRESS_CONNECTOR = _CONNECTOR.format(_REGION, "ALL_INGRESS")
EGRESS_CONNECTOR = _CONNECTOR.format(_REGION, "INTERNET_EGRESS")


def _redact(config):
    return {
        k: (f"<redacted:{len(v)}>" if k in SECRET_KEYS else v)
        for k, v in config.items()
    }


def _extract(payload):
    """Pull per-meeting fields out of either payload shape."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    config = {}
    for env_key, candidates in FIELD_MAP.items():
        for candidate in candidates:
            value = data.get(candidate)
            if value not in (None, ""):
                config[env_key] = (
                    str(value).lower() if isinstance(value, bool) else str(value)
                )
                break
    return config


def _write_config_to_registry(vp_id: str, config: dict) -> bool:
    """Stage the VP's full configuration in the task registry.

    Written BEFORE RunMicrovm so it is present when the container's /run hook
    fires. The registry table is KMS-encrypted and TTL'd, which matters because
    the config includes three Cognito JWTs and the meeting password.
    """
    try:
        dynamodb.update_item(
            TableName=os.environ["VP_TASK_REGISTRY_TABLE_NAME"],
            Key={"vpId": {"S": vp_id}},
            UpdateExpression="SET vpConfig = :c, configStagedAt = :t, expiresAt = :x",
            ExpressionAttributeValues={
                ":c": {"S": json.dumps(config)},
                ":t": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                ":x": {"N": str(int(time.time()) + MAX_DURATION_SECONDS + 3600)},
            },
        )
        logger.info("Staged %d config values for VP %s", len(config), vp_id)
        return True
    except Exception:  # noqa: BLE001 - reported to the UI by the caller
        logger.exception("Could not stage VP config in the registry")
        return False

def lambda_handler(event, context):
    logger.info("MicroVM launcher invoked")
    config = _extract(event if isinstance(event, dict) else {})
    logger.info("Per-meeting config: %s", json.dumps(_redact(config)))

    vp_id = config.get("VIRTUAL_PARTICIPANT_ID")
    if not vp_id:
        # Without this we cannot report status back to the UI at all.
        return {"ok": False, "reason": "no virtualParticipantId in payload"}

    # Static stack config first so per-meeting values win on any collision.
    payload_config = {**_static_config(), **config}

    # Stash the full configuration in the registry BEFORE starting the MicroVM,
    # so it is already there when the container's /run hook fires and reads it.
    if not _write_config_to_registry(vp_id, payload_config):
        return {"ok": False, "reason": "could not stage VP config in the registry"}

    # The payload itself carries only a pointer (see RUN_HOOK_PAYLOAD_MAX_BYTES).
    run_hook_payload = json.dumps({"VIRTUAL_PARTICIPANT_ID": vp_id})
    size = len(run_hook_payload.encode("utf-8"))
    if size > RUN_HOOK_PAYLOAD_MAX_BYTES:
        return {
            "ok": False,
            "reason": (
                f"runHookPayload is {size} bytes, over the "
                f"{RUN_HOOK_PAYLOAD_MAX_BYTES}-byte limit"
            ),
        }

    try:
        response = microvms.run_microvm(
            imageIdentifier=os.environ["MICROVM_IMAGE_ARN"],
            executionRoleArn=os.environ["EXECUTION_ROLE_ARN"],
            # Ingress is required for the noVNC viewer to reach this
            # VM at all: without a connector the endpoint exists but
            # forwards nothing. Access is still gated by the
            # port-scoped auth token the UI has to mint.
            ingressNetworkConnectors=[INGRESS_CONNECTOR],
            egressNetworkConnectors=[EGRESS_CONNECTOR],
            # Auto-suspend is deliberately DISABLED: a suspended VP
            # stops capturing audio, so it would silently miss meeting
            # content. The VP runs to completion and is terminated when
            # the meeting ends.
            idlePolicy={
                "autoResumeEnabled": False,
                "maxIdleDurationSeconds": MAX_DURATION_SECONDS,
                "suspendedDurationSeconds": 0,
            },
            maximumDurationInSeconds=MAX_DURATION_SECONDS,
            runHookPayload=run_hook_payload,
            clientToken=vp_id,
        )
    except MicrovmError as exc:
        # Quota exhaustion arrives as a 402/429 from the API; surface whatever
        # the service said so the UI shows a real reason rather than "failed".
        logger.error("RunMicrovm failed: %s", exc)
        return {"ok": False, "reason": f"RunMicrovm failed: {exc}"}
    except Exception as exc:  # noqa: BLE001 - surface any reason to the UI
        logger.exception("RunMicrovm failed")
        return {"ok": False, "reason": f"RunMicrovm failed: {exc}"}

    microvm_id = response["microvmId"]
    endpoint = response.get("endpoint", "")
    logger.info("Started MicroVM %s endpoint=%s", microvm_id, endpoint)

    # Wait for RUNNING so a failure to start is reported now, while the
    # state machine can still mark the VP failed, rather than leaving
    # the UI waiting on a VP that never boots.
    state = response.get("state", "PENDING")
    deadline = time.time() + 120
    while state in ("PENDING", "STARTING") and time.time() < deadline:
        time.sleep(2)
        state = microvms.get_microvm(microvm_id).get("state", state)

    if state != "RUNNING":
        logger.error("MicroVM %s did not reach RUNNING (state=%s)", microvm_id, state)
        try:
            microvms.terminate_microvm(microvm_id)
        except Exception:  # noqa: BLE001 - best-effort cleanup
            logger.warning("Could not terminate %s", microvm_id)
        return {
            "ok": False,
            "reason": f"MicroVM state={state}",
            "microvmId": microvm_id,
        }

    # Publish the endpoint so the UI's VNC viewer can reach this VM.
    # Under ECS the container self-registers with an ALB; here the
    # launcher already knows the address, so it records it instead.
    try:
        dynamodb.update_item(
            TableName=os.environ["VP_TASK_REGISTRY_TABLE_NAME"],
            Key={"vpId": {"S": vp_id}},
            UpdateExpression=(
                "SET microvmId = :m, vncEndpoint = :e, launchType = :l, "
                "createdAt = :c, expiresAt = :x"
            ),
            ExpressionAttributeValues={
                ":m": {"S": microvm_id},
                ":e": {"S": f"wss://{endpoint}" if endpoint else ""},
                ":l": {"S": "MICROVM"},
                ":c": {"S": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                ":x": {"N": str(int(time.time()) + MAX_DURATION_SECONDS + 3600)},
            },
        )
    except Exception:  # noqa: BLE001 - the VM is up; don't fail the meeting
        logger.exception("Could not write MicroVM endpoint to the registry")

    return {
        "ok": True,
        "reason": "",
        "microvmId": microvm_id,
        "endpoint": endpoint,
        "virtualParticipantId": vp_id,
    }
