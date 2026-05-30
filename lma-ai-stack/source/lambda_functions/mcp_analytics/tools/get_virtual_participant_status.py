# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Tool: get_virtual_participant_status.

Lets the MCP agent poll the current state of a VP it launched. Returns the
high-level status, any error message, and — critically — whether the VP is
waiting on the human to solve a CAPTCHA / 2FA / SSO / consent challenge in
the VNC viewer (status == MANUAL_ACTION_REQUIRED).

The agent should poll this tool periodically after start_meeting_now if the
caller cares about reaching ACTIVE status (e.g. to confirm the meeting is
being recorded). On MANUAL_ACTION_REQUIRED, the agent should surface the
manualActionMessage to its user along with the virtualParticipantUrl so
they can open the LMA UI / VNC viewer and complete the challenge.
"""

import json
import logging
import os
from typing import Any, Dict

import boto3

from tools.url_helper import get_meeting_url, get_virtual_participant_url

logger = logging.getLogger()


def execute(
    virtual_participant_id: str,
    user_id: str = None,
    is_admin: bool = False,
) -> Dict[str, Any]:
    """Get the current status of a virtual participant.

    Args:
        virtual_participant_id: The id returned by start_meeting_now.
        user_id: User ID for access control.
        is_admin: Whether the caller is admin.

    Returns:
        Dict containing status, manual-action fields if applicable, vncUrl,
        meetingUrl, virtualParticipantUrl, and a human-readable summary.
    """
    if not virtual_participant_id:
        raise ValueError("virtual_participant_id is required")

    appsync_url = os.environ.get("APPSYNC_GRAPHQL_URL")
    if not appsync_url:
        raise ValueError("APPSYNC_GRAPHQL_URL environment variable not set")

    query = """
    query GetVirtualParticipant($id: ID!) {
      getVirtualParticipant(id: $id) {
        id
        meetingName
        meetingPlatform
        status
        Owner
        SharedWith
        CallId
        vncEndpoint
        vncReady
        manualActionType
        manualActionMessage
        manualActionTimeoutSeconds
        manualActionStartTime
        errorMessage
        createdAt
        updatedAt
      }
    }
    """
    variables = {"id": virtual_participant_id}

    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    session = boto3.Session()
    credentials = session.get_credentials()

    headers = {"Content-Type": "application/json"}
    body = json.dumps({"query": query, "variables": variables})
    request = AWSRequest(method="POST", url=appsync_url, data=body, headers=headers)
    SigV4Auth(credentials, "appsync", session.region_name).add_auth(request)
    response = requests.post(appsync_url, headers=dict(request.headers), data=body, timeout=(5, 30))
    response.raise_for_status()
    result = response.json()

    if "errors" in result:
        raise ValueError(f"Failed to load virtual participant: {result['errors'][0]['message']}")

    vp = (result.get("data") or {}).get("getVirtualParticipant")
    if not vp:
        raise ValueError(f"Virtual participant {virtual_participant_id} not found")

    # Per-user access control — admins see everything, owners see their own.
    if not is_admin and user_id:
        owner = vp.get("Owner") or ""
        shared = vp.get("SharedWith") or ""
        if owner != user_id and user_id not in shared.split(","):
            raise PermissionError("You do not have access to this virtual participant")

    status = vp.get("status") or "UNKNOWN"
    manual_action_required = status == "MANUAL_ACTION_REQUIRED"

    # Build a human-readable summary that the agent can read directly.
    if status == "ACTIVE":
        summary = "Virtual participant is in the meeting and recording is active."
    elif status == "JOINED":
        summary = "Virtual participant has joined the meeting."
    elif status == "JOINING":
        summary = "Virtual participant is joining the meeting."
    elif status == "INITIALIZING":
        summary = "Virtual participant is starting up — allocating Fargate compute."
    elif status == "WAITING_FOR_CAPACITY":
        summary = (
            "Waiting for compute capacity — task is queued for an EC2 host slot. "
            "If the cluster is full, the auto-scaler will launch a new host (~60-90s); "
            "otherwise the task is just waiting briefly for placement."
        )
    elif status == "BOOTING":
        summary = (
            "Container started — pulling Chrome image and starting display, audio, and VNC server."
        )
    elif status == "REGISTERING_NETWORK":
        summary = "Registering live-view networking — typically 30–60 seconds."
    elif status == "HYDRATING_PROFILE":
        summary = "Restoring saved browser cookies / trusted-device markers from S3."
    elif status == "LAUNCHING_BROWSER":
        summary = "Launching the browser and platform extensions."
    elif status == "WARMING_PROFILE":
        summary = "Warming a new profile with first-launch browsing (one-time, ~15s)."
    elif status == "CONNECTING":
        summary = "Virtual participant is connecting to the meeting."
    elif status == "VNC_READY":
        summary = "Virtual participant browser is ready; about to navigate to the meeting."
    elif manual_action_required:
        action = vp.get("manualActionType") or "ATTENTION"
        message = vp.get("manualActionMessage") or "Manual action required."
        summary = (
            f"⚠ MANUAL ACTION REQUIRED ({action}): {message} "
            f"Open the LMA viewer to complete the challenge."
        )
    elif status == "FAILED":
        err = vp.get("errorMessage") or "Unknown error"
        summary = f"Virtual participant failed: {err}"
    elif status == "COMPLETED":
        summary = "Virtual participant has finished and left the meeting."
    elif status == "SCHEDULED":
        summary = "Virtual participant is scheduled to join later."
    else:
        summary = f"Virtual participant status: {status}"

    response_data: Dict[str, Any] = {
        "virtualParticipantId": vp["id"],
        "meetingName": vp.get("meetingName"),
        "meetingPlatform": vp.get("meetingPlatform"),
        "status": status,
        "summary": summary,
        "manualActionRequired": manual_action_required,
        "createdAt": vp.get("createdAt"),
        "updatedAt": vp.get("updatedAt"),
        "virtualParticipantUrl": get_virtual_participant_url(vp["id"]),
    }

    if manual_action_required:
        response_data["manualActionType"] = vp.get("manualActionType")
        response_data["manualActionMessage"] = vp.get("manualActionMessage")
        response_data["manualActionTimeoutSeconds"] = vp.get("manualActionTimeoutSeconds")
        response_data["manualActionStartTime"] = vp.get("manualActionStartTime")

    if status == "FAILED":
        response_data["errorMessage"] = vp.get("errorMessage")

    if vp.get("CallId"):
        response_data["callId"] = vp["CallId"]
        response_data["meetingUrl"] = get_meeting_url(vp["CallId"])

    if vp.get("vncEndpoint"):
        response_data["vncEndpoint"] = vp["vncEndpoint"]
        response_data["vncReady"] = vp.get("vncReady")

    return response_data
