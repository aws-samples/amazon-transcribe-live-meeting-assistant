#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

import json
import uuid
from datetime import datetime
from os import getenv
from typing import TYPE_CHECKING, Any, Dict

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

CALL_DATA_STREAM_NAME = getenv("CALL_DATA_STREAM_NAME", "")

LAMBDA_AGENT_ASSIST_FUNCTION_ARN = getenv("LAMBDA_AGENT_ASSIST_FUNCTION_ARN", "")

IS_LAMBDA_AGENT_ASSIST_ENABLED = getenv("IS_LAMBDA_AGENT_ASSIST_ENABLED", "false").lower() == "true"

DYNAMODB_TABLE_NAME = getenv("DYNAMODB_TABLE_NAME", "")


def write_agent_assist_to_kds(message: Dict[str, Any]):
    callId = message.get("CallId", None)
    message["EventType"] = "ADD_AGENT_ASSIST"

    if callId:
        try:
            KINESIS_CLIENT.put_record(
                StreamName=CALL_DATA_STREAM_NAME, PartitionKey=callId, Data=json.dumps(message)
            )
            LOGGER.info("Write AGENT_ASSIST event to KDS: %s", json.dumps(message))
        except Exception as error:
            LOGGER.error(
                "Error writing AGENT_ASSIST event to KDS ",
                extra=error,
            )
    return


def publish_lambda_agent_assist_transcript_segment(
    message: Dict[str, Any],
):
    if "ContactId" in message.keys():
        publish_contact_lens_lambda_agent_assist_transcript_segment(message)
        return

    """Add Lambda Agent Assist GraphQL Mutations"""
    # pylint: disable=too-many-locals

    call_id: str = message["CallId"]
    channel: str = message["Channel"]
    is_partial: bool = message["IsPartial"]
    message["SegmentId"]
    start_time: float = message["StartTime"]
    end_time: float = message["EndTime"]
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

    LOGGER.info("Agent Assist Lambda Request: %s", content)

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


def transform_segment_to_issues_agent_assist(
    segment: Dict[str, Any],
    issue: Dict[str, Any],
) -> Dict[str, Any]:
    """Transforms Contact Lens Transcript Issues payload to Agent Assist"""
    # pylint: disable=too-many-locals
    call_id: str = segment["CallId"]
    created_at = datetime.utcnow().astimezone().isoformat()
    is_partial = False
    segment_id = str(uuid.uuid4())
    channel = "AGENT_ASSISTANT"
    segment_item = segment["ContactLensTranscript"]
    transcript = segment_item["Content"]

    issues_detected = segment.get("ContactLensTranscript", {}).get("IssuesDetected", [])
    if not issues_detected:
        raise ValueError("Invalid issue segment")

    begin_offset = issue["CharacterOffsets"]["BeginOffsetChar"]
    end_offset = issue["CharacterOffsets"]["EndOffsetChar"]
    issue_transcript = transcript[begin_offset:end_offset]
    start_time: float = segment_item["BeginOffsetMillis"] / 1000
    end_time: float = segment_item["EndOffsetMillis"] / 1000
    end_time = end_time + 0.001  # UI sort order

    return dict(
        CallId=call_id,
        Channel=channel,
        CreatedAt=created_at,
        ExpiresAfter=get_transcription_ttl(),
        EndTime=end_time,
        IsPartial=is_partial,
        SegmentId=segment_id,
        StartTime=start_time,
        Status="TRANSCRIBING",
        Transcript=issue_transcript,
    )


def transform_segment_to_categories_agent_assist(
    category: str,
    category_details: Dict[str, Any],
    call_id: str,
) -> Dict[str, Any]:
    """Transforms Contact Lens Categories segment payload to Agent Assist"""
    created_at = datetime.utcnow().astimezone().isoformat()
    is_partial = False
    segment_id = str(uuid.uuid4())
    channel = "AGENT_ASSISTANT"

    transcript = f"{category}"
    # get the min and maximum offsets to put a time range
    segment_item = {}
    segment_item["BeginOffsetMillis"] = min(
        (
            point_of_interest["BeginOffsetMillis"]
            for point_of_interest in category_details["PointsOfInterest"]
        )
    )
    segment_item["EndOffsetMillis"] = max(
        (
            point_of_interest["EndOffsetMillis"]
            for point_of_interest in category_details["PointsOfInterest"]
        )
    )

    start_time: float = segment_item["BeginOffsetMillis"] / 1000
    end_time: float = segment_item["EndOffsetMillis"] / 1000

    return dict(
        CallId=call_id,
        Channel=channel,
        CreatedAt=created_at,
        ExpiresAfter=get_transcription_ttl(),
        EndTime=end_time,
        IsPartial=is_partial,
        SegmentId=segment_id,
        StartTime=start_time,
        Status="TRANSCRIBING",
        Transcript=transcript,
    )


def publish_contact_lens_lambda_agent_assist_transcript_segment(
    segment: Dict[str, Any],
):
    """Add Lambda Agent Assist GraphQL Mutations"""
    # pylint: disable=too-many-locals
    call_id: str = segment["ContactId"]
    channel: str = "AGENT_ASSISTANT"
    status: str = "TRANSCRIBING"
    is_partial: bool = False

    created_at: str
    start_time: float
    end_time: float

    send_lambda_agent_assist_args = []
    # only send relevant segments to agent assist
    if not ("ContactLensTranscript" in segment or "Categories" in segment):
        return

    if "Utterance" in segment and segment["Utterance"].get("ParticipantRole") == "CUSTOMER":
        is_partial = False
        segment_item = segment["Utterance"]
        content = segment_item["PartialContent"]
        segment_id = str(uuid.uuid4())

        created_at = datetime.utcnow().astimezone().isoformat()
        start_time = segment_item["BeginOffsetMillis"] / 1000
        end_time = segment_item["EndOffsetMillis"] / 1000
        end_time = end_time + 0.001  # UI sort order

        send_lambda_agent_assist_args.append(
            dict(
                content=content,
                transcript_segment_args=dict(
                    CallId=call_id,
                    Channel=channel,
                    CreatedAt=created_at,
                    ExpiresAfter=get_transcription_ttl(),
                    EndTime=end_time,
                    IsPartial=is_partial,
                    SegmentId=segment_id,
                    StartTime=start_time,
                    Status=status,
                ),
            )
        )

    issues_detected = segment.get("ContactLensTranscript", {}).get("IssuesDetected", [])
    if (
        "ContactLensTranscript" in segment
        and segment["ContactLensTranscript"].get("ParticipantRole") == "CUSTOMER"
        and not issues_detected
    ):
        is_partial = False
        segment_item = segment["ContactLensTranscript"]
        content = segment_item["Content"]
        segment_id = str(uuid.uuid4())

        created_at = datetime.utcnow().astimezone().isoformat()
        start_time = segment_item["BeginOffsetMillis"] / 1000
        end_time = segment_item["EndOffsetMillis"] / 1000
        end_time = end_time + 0.001  # UI sort order

        send_lambda_agent_assist_args.append(
            dict(
                content=content,
                transcript_segment_args=dict(
                    CallId=call_id,
                    Channel=channel,
                    CreatedAt=created_at,
                    ExpiresAfter=get_transcription_ttl(),
                    EndTime=end_time,
                    IsPartial=is_partial,
                    SegmentId=segment_id,
                    StartTime=start_time,
                    Status=status,
                ),
            )
        )
    for issue in issues_detected:
        issue_segment = transform_segment_to_issues_agent_assist(
            segment={**segment, "CallId": call_id},
            issue=issue,
        )
        send_lambda_agent_assist_args.append(
            dict(content=issue_segment["Transcript"], transcript_segment_args=issue_segment),
        )

    categories = segment.get("Categories", {})
    for category in categories.get("MatchedCategories", []):
        category_details = categories["MatchedDetails"][category]
        category_segment = transform_segment_to_categories_agent_assist(
            category=category,
            category_details=category_details,
            call_id=call_id,
        )
        send_lambda_agent_assist_args.append(
            dict(
                content=category_segment["Transcript"],
                transcript_segment_args=category_segment,
            ),
        )

    for agent_assist_args in send_lambda_agent_assist_args:
        # write initial message to indicate that wake word was detected and request submitted.
        checking_segment = {
            **agent_assist_args["transcript_segment_args"],
            "Transcript": "Checking...",
            "IsPartial": True,
        }
        write_agent_assist_to_kds(checking_segment)

        transcript_segment = get_lambda_agent_assist_transcript(
            **agent_assist_args,
        )

        write_agent_assist_to_kds(transcript_segment)

    # Return the last transcript segment for synchronous callers
    return transcript_segment if transcript_segment else None


@LOGGER.inject_lambda_context
def handler(event, context: LambdaContext):
    # pylint: disable=unused-argument
    """Lambda handler"""
    LOGGER.info("Agent assist lambda event", extra={"event": event})

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
