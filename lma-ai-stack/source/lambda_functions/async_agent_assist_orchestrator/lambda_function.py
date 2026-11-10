#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

import json
import uuid
from datetime import datetime
from os import getenv
from typing import TYPE_CHECKING, Any, Dict, Tuple

import boto3

# third-party imports from Lambda layer
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.config import Config as BotoCoreConfig
from eventprocessor_utils import get_transcription_ttl

# pylint: enable=import-error
LOGGER = Logger(location="%(filename)s:%(lineno)d - %(funcName)s()")

if TYPE_CHECKING:
    from boto3 import Session as Boto3Session
    from mypy_boto3_kinesis.client import KinesisClient
    from mypy_boto3_lambda.client import LambdaClient
else:
    Boto3Session = object
    LambdaClient = object
    KinesisClient = object

BOTO3_SESSION: Boto3Session = boto3.Session()
CLIENT_CONFIG = BotoCoreConfig(
    retries={"mode": "adaptive", "max_attempts": 3},
)

LAMBDA_CLIENT: LambdaClient = BOTO3_SESSION.client(
    "lambda",
    config=CLIENT_CONFIG,
)
KINESIS_CLIENT: KinesisClient = BOTO3_SESSION.client("kinesis")
DYNAMODB_CLIENT = BOTO3_SESSION.client("dynamodb", config=CLIENT_CONFIG)

CALL_DATA_STREAM_NAME = getenv("CALL_DATA_STREAM_NAME", "")

LAMBDA_AGENT_ASSIST_FUNCTION_ARN = getenv("LAMBDA_AGENT_ASSIST_FUNCTION_ARN", "")

IS_LAMBDA_AGENT_ASSIST_ENABLED = getenv("IS_LAMBDA_AGENT_ASSIST_ENABLED", "false").lower() == "true"

DYNAMODB_TABLE_NAME = getenv("DYNAMODB_TABLE_NAME", "")

#: The channel of the meeting page's assistant chat (strands_chat_interface).
#: Its questions arrive stamped with the wall-clock time they were asked, not a
#: time on the meeting's transcript timeline.
CHAT_ASSISTANT_CHANNEL = "CHAT_ASSISTANT"

#: The speaker the meeting assistant's answers are stored under: the name the
#: meeting page gives the assistant.
ASSISTANT_SPEAKER = "Meeting Assistant"

#: The meeting assistant's tool scope for a request spoken with the wake phrase
#: (strands_meeting_assist_function.IN_MEETING_TOOL_SCOPES).
WAKE_PHRASE_TOOL_SCOPE = "wake_phrase"


def meeting_time_seconds(call_id: str) -> float:
    """How far the meeting's transcript has reached, in seconds from its start.

    The call record's TotalConversationDurationMillis, which the call event
    processor keeps at the latest end of the meeting's final speech segments:
    the current point on the transcript timeline while the meeting runs, and
    its end afterwards. 0 when the meeting has no speech yet or the record
    cannot be read, so an answer is never placed beyond the meeting.
    """
    if not DYNAMODB_TABLE_NAME or not call_id:
        return 0.0
    key = f"c#{call_id}"
    try:
        item = (
            DYNAMODB_CLIENT.get_item(
                TableName=DYNAMODB_TABLE_NAME,
                Key={"PK": {"S": key}, "SK": {"S": key}},
                ProjectionExpression="TotalConversationDurationMillis",
            ).get("Item")
            or {}
        )
        raw = item.get("TotalConversationDurationMillis", {}).get("N")
        return max(0.0, float(raw) / 1000) if raw is not None else 0.0
    except Exception as error:  # pylint: disable=broad-except
        LOGGER.warning("Could not read the meeting's transcript extent: %s", error)
        return 0.0


def answer_times(message: Dict[str, Any]) -> Tuple[Any, Any]:
    """The start and end of the answer segment, on the meeting's transcript timeline.

    A wake-phrase answer takes the times of the transcript segment that asked,
    which are already on that timeline. A question from the meeting page's
    chat is placed where the transcript has reached when it is answered.
    """
    if message.get("Channel") == CHAT_ASSISTANT_CHANNEL:
        at = meeting_time_seconds(message.get("CallId", ""))
        return at, at
    return message["StartTime"], message["EndTime"]


def write_agent_assist_to_kds(message: Dict[str, Any]):
    callId = message.get("CallId", None)
    message["EventType"] = "ADD_AGENT_ASSIST"

    if callId:
        try:
            KINESIS_CLIENT.put_record(
                StreamName=CALL_DATA_STREAM_NAME, PartitionKey=callId, Data=json.dumps(message)
            )
            LOGGER.debug("Write AGENT_ASSIST event to KDS: %s", json.dumps(message))
        except Exception as error:
            LOGGER.error(
                "Error writing AGENT_ASSIST event to KDS ",
                extra=error,
            )
    return


def publish_lambda_agent_assist_transcript_segment(
    message: Dict[str, Any],
):
    """Add Lambda Agent Assist GraphQL Mutations"""
    # pylint: disable=too-many-locals

    call_id: str = message["CallId"]
    channel: str = message["Channel"]
    is_partial: bool = message["IsPartial"]
    message["SegmentId"]
    start_time, end_time = answer_times(message)
    end_time = float(end_time) + 0.001  # UI sort order
    # Use "OriginalTranscript", if defined (optionally set by transcript lambda hook fn)"
    transcript: str = message.get("OriginalTranscript", message["Transcript"])
    created_at = datetime.utcnow().astimezone().isoformat()

    # Extract Owner (user email) for UBAC
    owner = message.get("Owner", "")

    # Extract conversation history if provided (for chat messages)
    conversation_history = message.get("ConversationHistory", [])

    # Determine response channel based on input channel
    response_channel = "CHAT_ASSISTANT" if channel == "CHAT_ASSISTANT" else "AGENT_ASSISTANT"

    # Extract MessageId if provided (for chat streaming)
    message_id = message.get("MessageId")

    transcript_segment_args = dict(
        CallId=call_id,
        Channel=response_channel,
        CreatedAt=created_at,
        EndTime=end_time,
        ExpiresAfter=get_transcription_ttl(),
        IsPartial=is_partial,
        SegmentId=str(uuid.uuid4()),
        StartTime=start_time,
        # Without one the event processor stores the placeholder participant name.
        Speaker=ASSISTANT_SPEAKER,
        Status="TRANSCRIBING",
        Owner=owner,  # Pass Owner for userEmail extraction
        ConversationHistory=conversation_history,  # Pass conversation history
    )

    # Add MessageId if provided (for token streaming)
    if message_id:
        transcript_segment_args["MessageId"] = message_id

    lambda_agent_assist_input = dict(
        content=transcript,
        transcript_segment_args=transcript_segment_args,
    )

    # write initial message to indicate that wake word was detected and request submitted.
    checking_segment = {**transcript_segment_args, "Transcript": "Checking...", "IsPartial": True}
    write_agent_assist_to_kds(checking_segment)

    transcript_segment = get_lambda_agent_assist_transcript(
        **lambda_agent_assist_input,
    )

    write_agent_assist_to_kds(transcript_segment)

    # Return the transcript segment for synchronous callers
    return transcript_segment


def get_lambda_agent_assist_transcript(
    transcript_segment_args: Dict[str, Any],
    content: str,
):
    """Sends Lambda Agent Assist Requests"""
    call_id = transcript_segment_args["CallId"]

    # Extract Owner (user email) from transcript_segment_args if available
    owner = transcript_segment_args.get("Owner", "")

    payload = {
        "text": content,
        "call_id": call_id,
        "conversation_history": transcript_segment_args.get(
            "ConversationHistory", []
        ),  # Pass conversation history
        "transcript_segment_args": transcript_segment_args,
        "dynamodb_table_name": DYNAMODB_TABLE_NAME,
        "dynamodb_pk": f"c#{call_id}",
        "userEmail": owner,  # Add userEmail for tools that require UBAC
    }
    # A wake phrase is spoken in the meeting by whoever is in it, so it is an
    # in-meeting request: the meeting assistant answers it as the Meeting
    # Assistant section's In-meeting requests setting allows. A question from
    # the meeting page's chat is the signed-in user's, and carries no scope.
    if transcript_segment_args.get("Channel") != CHAT_ASSISTANT_CHANNEL:
        payload["toolScope"] = WAKE_PHRASE_TOOL_SCOPE

    LOGGER.debug("Agent Assist Lambda Request: %s", content)

    lambda_response = LAMBDA_CLIENT.invoke(
        FunctionName=LAMBDA_AGENT_ASSIST_FUNCTION_ARN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )

    LOGGER.info("Agent Assist Lambda Response: ", extra=lambda_response)

    transcript_segment = {}
    transcript = process_lambda_response(lambda_response)
    if transcript:
        transcript_segment = {**transcript_segment_args, "Transcript": transcript}

    return transcript_segment


def process_lambda_response(lambda_response):
    message = ""
    try:
        payload = json.loads(lambda_response.get("Payload").read().decode("utf-8"))
        # Lambda result payload should include field 'message'
        message = payload["message"]
    except Exception as error:
        LOGGER.error(
            "Agent assist Lambda result payload parsing exception. Lambda must return object with key 'message'",
            extra=error,
        )
    return message


@LOGGER.inject_lambda_context
def handler(event, context: LambdaContext):
    # pylint: disable=unused-argument
    """Lambda handler"""
    # The event carries the transcript segment that asked, so DEBUG only.
    LOGGER.debug("Agent assist lambda event", extra={"event": event})

    data = json.loads(json.dumps(event))

    if IS_LAMBDA_AGENT_ASSIST_ENABLED:
        LOGGER.info("Invoking Lambda agent assist")
        transcript_segment = publish_lambda_agent_assist_transcript_segment(data)
        # Return the response for synchronous callers (like chat interface)
        if transcript_segment and transcript_segment.get("Transcript"):
            return {"message": transcript_segment.get("Transcript")}
    else:
        LOGGER.warning("Agent assist is not enabled but orchestrator invoked")
    return {}
