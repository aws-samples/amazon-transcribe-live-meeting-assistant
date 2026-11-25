#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""
STRANDS Chat Interface Lambda Function
Handles sendChatMessage GraphQL mutation and invokes AsyncAgentAssistOrchestrator

A question is answered for a caller who may read the meeting it names: an
Admin, the meeting's owner, or someone its SharedWith names (compared whole
entries, as getCall compares them). A Participant must be named in SharedWith:
owning a meeting is not enough for that role, which reads shared meetings only.
A Participant's question is passed on with RequesterRole "Participant" and the
meeting's owner, so that the orchestrator answers it with the tools approved
for Participant use and does not add it to the meeting's transcript.
"""

import json
import logging
import os
import uuid
from datetime import datetime

import boto3

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
lambda_client = boto3.client("lambda")
dynamodb = boto3.resource("dynamodb")

EVENT_SOURCING_TABLE_NAME = os.environ.get("EVENT_SOURCING_TABLE_NAME", "")

ADMIN = "Admin"
PARTICIPANT = "Participant"


# BEGIN shared_with_entries -- one text in every Lambda that reads SharedWith;
# lma-ai-stack/test/test_shared_with_access.py holds the copies to it.
# The entries of a SharedWith value, read as the AppSync resolvers read them
# (the "SharedWith entries" VTL block and sharedWithEntries in the JS
# resolvers): a list, tuple or set holds one entry per string element, and a
# string -- comma-separated or "[a, b]" -- is split at every "[", "]" and ",".
# Those three characters only ever separate entries, so a list element that
# contains one is not an entry. Entries are trimmed and empty ones dropped;
# membership is then a comparison of whole entries.
def shared_with_entries(value: object) -> list:
    """The trimmed, non-empty entries of a SharedWith value."""
    if isinstance(value, str):
        parts = value.replace("[", ",").replace("]", ",").split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = [
            part
            for part in value
            if isinstance(part, str) and not any(separator in part for separator in "[],")
        ]
    else:
        return []
    return [part.strip() for part in parts if part.strip()]


# END shared_with_entries


def requester_role(identity):
    """The caller's role from their groups: Admin, then Participant, else User."""
    groups = identity.get("groups") or []
    if ADMIN in groups:
        return ADMIN
    if PARTICIPANT in groups:
        return PARTICIPANT
    return "User"


def meeting_for(call_id, username, role):
    """The meeting record, when the caller may ask about it; PermissionError otherwise."""
    key = f"c#{call_id}"
    meeting = (
        dynamodb.Table(EVENT_SOURCING_TABLE_NAME)
        .get_item(
            Key={"PK": key, "SK": key},
            ProjectionExpression="#o, SharedWith",
            ExpressionAttributeNames={"#o": "Owner"},
        )
        .get("Item")
    )
    if role == ADMIN and meeting is not None:
        return meeting
    shared = bool(meeting) and username in shared_with_entries(meeting.get("SharedWith"))
    owner = bool(meeting) and bool(username) and meeting.get("Owner") == username
    if shared or (owner and role != PARTICIPANT):
        return meeting
    # The same words for a meeting that does not exist.
    raise PermissionError(f"Unauthorized: {username} may not ask about this meeting")


def lambda_handler(event, context):
    """
    Lambda handler for STRANDS chat interface GraphQL resolver

    Expected event structure from AppSync:
    {
        "arguments": {
            "input": {
                "CallId": "call-id",
                "Message": "user message"
            }
        },
        "identity": {
            "username": "user@example.com",
            "groups": ["Participant"],
            "claims": {...}
        }
    }
    """
    try:
        # Extract parameters from AppSync event
        arguments = event.get("arguments", {})
        input_data = arguments.get("input", {})
        identity = event.get("identity", {}) or {}

        call_id = input_data.get("CallId", "")
        message = input_data.get("Message", "")
        conversation_history = input_data.get("ConversationHistory", [])
        username = identity.get("username", "") or ""

        if not message or not call_id:
            logger.error("Missing required parameters: CallId and Message")
            raise ValueError("CallId and Message are required")

        role = requester_role(identity)
        meeting = meeting_for(call_id, username, role)

        logger.info(f"Conversation history length: {len(conversation_history)}")

        # Get AsyncAgentAssistOrchestrator ARN from environment
        orchestrator_arn = os.environ.get("ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN")
        if not orchestrator_arn:
            logger.error("ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN not configured")
            raise ValueError("AsyncAgentAssistOrchestrator not configured")

        # Generate unique message ID
        message_id = f"chat-{uuid.uuid4()}"

        # Create event payload for AsyncAgentAssistOrchestrator
        # Use CHAT_ASSISTANT channel to separate from voice wake phrases
        orchestrator_payload = {
            "CallId": call_id,
            "Channel": "CHAT_ASSISTANT",  # Use dedicated chat channel
            "SegmentId": message_id,
            "MessageId": message_id,  # Pass MessageId for token streaming
            "StartTime": datetime.now().timestamp(),
            "EndTime": datetime.now().timestamp() + 1,
            "Transcript": message,
            "ConversationHistory": conversation_history,  # Include conversation history
            "Speaker": username or "ChatUser",
            "IsPartial": False,
            "Status": "TRANSCRIBING",
            "CreatedAt": datetime.utcnow().isoformat() + "Z",
            "ExpiresAfter": int(datetime.now().timestamp()) + (90 * 24 * 60 * 60),  # 90 days
            "Owner": username,
        }
        if role == PARTICIPANT:
            # Answered under Participant use, for the meeting's owner as a
            # request made in the meeting is, and kept out of the transcript.
            orchestrator_payload["RequesterRole"] = PARTICIPANT
            orchestrator_payload["MeetingOwner"] = meeting.get("Owner", "")

        logger.info(
            f"STRANDS Chat Interface - Invoking AsyncAgentAssistOrchestrator: {orchestrator_arn}"
        )

        # Invoke AsyncAgentAssistOrchestrator asynchronously
        # The response will be streamed via addChatToken mutations
        lambda_client.invoke(
            FunctionName=orchestrator_arn,
            InvocationType="Event",  # Asynchronous invocation - don't wait for response
            Payload=json.dumps(orchestrator_payload),
        )

        logger.info(
            "STRANDS Chat Interface - AsyncAgentAssistOrchestrator invoked successfully (async)"
        )

        # Return immediately with MessageId so UI can subscribe to token stream
        # The actual response will come via onAddChatToken subscription
        return {
            "MessageId": message_id,
            "Status": "PROCESSING",
            "CallId": call_id,
            "Response": None,  # Response will be streamed via tokens
        }

    except Exception as e:
        logger.error(f"Error in STRANDS chat interface: {str(e)}")
        # Re-raise the exception so AppSync can handle it properly
        raise e
