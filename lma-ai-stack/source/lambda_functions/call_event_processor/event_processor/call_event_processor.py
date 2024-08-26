# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
""" Transcribe API Mutation Processor
"""
import asyncio
from datetime import datetime
from statistics import fmean
from os import getenv
from typing import TYPE_CHECKING, Any, Coroutine, Dict, List, Literal, Optional, TypedDict
import uuid
import json
import re

# third-party imports from Lambda layer
import boto3
from botocore.config import Config as BotoCoreConfig
from aws_lambda_powertools import Logger
from gql.client import AsyncClientSession as AppsyncAsyncClientSession
from gql.dsl import DSLMutation, DSLSchema, DSLQuery, dsl_gql
from graphql.language.printer import print_ast

# custom utils/helpers imports from Lambda layer
# pylint: disable=import-error
from appsync_utils import execute_gql_query_with_retries
from graphql_helpers import (
    call_fields,
    transcript_segment_fields,
    transcript_segment_sentiment_fields,
)
from sns_utils import publish_sns
from lambda_utils import invoke_lambda
from eventprocessor_utils import (
    normalize_transcript_segments,
    get_meeting_ttl,
    get_transcription_ttl,
    transform_segment_to_add_sentiment,
    transform_segment_to_categories_agent_assist,
    get_owner_from_jwt,
)
# pylint: enable=import-error
if TYPE_CHECKING:
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_lambda.type_defs import InvocationResponseTypeDef
    from mypy_boto3_sns.client import SNSClient
    from mypy_boto3_ssm.client import SSMClient
    from boto3 import Session as Boto3Session
else:
    LambdaClient = object
    InvocationResponseTypeDef = object
    Boto3Session = object
    SNSClient = object
    SSMClient = object

SNS_TOPIC_ARN = getenv("SNS_TOPIC_ARN", "")

IS_SENTIMENT_ANALYSIS_ENABLED = getenv("IS_SENTIMENT_ANALYSIS_ENABLED", "true").lower() == "true"

BOTO3_SESSION: Boto3Session = boto3.Session()
CLIENT_CONFIG = BotoCoreConfig(
    retries={"mode": "adaptive", "max_attempts": 3},
)
TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN = getenv("TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN", "")

START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN = getenv("START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN", "")
POST_CALL_SUMMARY_LAMBDA_HOOK_FUNCTION_ARN = getenv("POST_CALL_SUMMARY_LAMBDA_HOOK_FUNCTION_ARN", "")

ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN = getenv("ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN", "")
IS_TRANSCRIPT_SUMMARY_ENABLED = getenv("IS_TRANSCRIPT_SUMMARY_ENABLED", "false").lower() == "true"

ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN = getenv("ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN", "")

TRANSCRIPT_LAMBDA_HOOK_FUNCTION_NONPARTIAL_ONLY = getenv(
    "TRANSCRIPT_LAMBDA_HOOK_FUNCTION_NONPARTIAL_ONLY", "true").lower() == "true"
if (TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN
        or ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN
        or ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN
        or START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN
        or POST_CALL_SUMMARY_LAMBDA_HOOK_FUNCTION_ARN):
    LAMBDA_HOOK_CLIENT: LambdaClient = BOTO3_SESSION.client("lambda", config=CLIENT_CONFIG)

IS_LEX_AGENT_ASSIST_ENABLED = False

IS_LAMBDA_AGENT_ASSIST_ENABLED = False

SETTINGS: Dict[str, Any]

LOGGER = Logger(location="%(filename)s:%(lineno)d - %(funcName)s()")
EVENT_LOOP = asyncio.get_event_loop()

CALL_EVENT_TYPE_TO_STATUS = {
    "START": "STARTED",
    "END": "ENDED",
    "ADD_S3_RECORDING_URL": "ENDED",
}

# DEFAULT_CUSTOMER_PHONE_NUMBER used to replace an invalid CustomerPhoneNumber
# such as seen from calls originating with Skype ('anonymous')
DEFAULT_CUSTOMER_PHONE_NUMBER = getenv("DEFAULT_CUSTOMER_PHONE_NUMBER", "+18005550000")
DEFAULT_SYSTEM_PHONE_NUMBER = getenv("DEFAULT_SYSTEM_PHONE_NUMBER", "+18005551111")
CONNECT_CONTACT_ATTR_CUSTOMER_PHONE_NUMBER = getenv(
    "CONNECT_CONTACT_ATTR_CUSTOMER_PHONE_NUMBER", "LCA Caller Phone Number")
CONNECT_CONTACT_ATTR_SYSTEM_PHONE_NUMBER = getenv(
    "CONNECT_CONTACT_ATTR_SYSTEM_PHONE_NUMBER", "LCA System Phone Number")

CUSTOMER_PHONE_NUMBER = ""
CALL_ID = ""

CALL_DATA_STREAM_NAME = getenv("CALL_DATA_STREAM_NAME", "")

SentimentLabelType = Literal["NEGATIVE", "MIXED", "NEUTRAL", "POSITIVE"]
ChannelType = Literal["AGENT", "CALLER"]
StatusType = Literal["STARTED", "TRANSCRIBING", "ERRORED", "ENDED"]
SentimentPeriodType = Literal["QUARTER"]

class SentimentEntry(TypedDict):
    """Sentiment Shape
    Held in a list per channel
    """
    Id: str
    BeginOffsetMillis: float
    EndOffsetMillis: float
    Sentiment: SentimentLabelType
    Score: float

class SentimentEntry(TypedDict):
    """Sentiment Shape
    Held in a list per channel
    """
    Id: str
    BeginOffsetMillis: float
    EndOffsetMillis: float
    Sentiment: SentimentLabelType
    Score: float
class SentimentPerChannel(TypedDict):
    """StatePerChannel Shape
    Holds state per channel under StatePerCallId. Use to keep values needed
    for statistics and aggregations.
    """

    SentimentList: List[SentimentEntry]

class SentimentByPeriodEntry(TypedDict):
    """Sentiment By Period Shape"""
    BeginOffsetMillis: float
    EndOffsetMillis: float
    Score: float

class Sentiment(TypedDict):
    """Sentiment Shape"""
    OverallSentiment: Dict[ChannelType, float]
    SentimentByPeriod: Dict[SentimentPeriodType, Dict[ChannelType, List[SentimentByPeriodEntry]]]

##########################################################################
# Transcripts
##########################################################################

def add_transcript_segments(
    message: Dict[str, object],
    appsync_session: AppsyncAsyncClientSession,
) -> List[Coroutine]:
    """Add Transcript Segment GraphQL Mutation"""
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    tasks = []
    if message:
        issues_detected = message.get("IssuesDetected", None)
        transcript = message["Transcript"]
        if "OriginalTranscript" not in message:
            message["OriginalTranscript"] = transcript

        if issues_detected and len(issues_detected) > 0:
            LOGGER.debug("issue detected in add transcript segment")
            offsets = issues_detected[0].get("CharacterOffsets")
            start = int(offsets.get("Begin"))
            end = int(offsets.get("End"))
            transcript = f"{transcript[:start]}<span class='issue-span'>{transcript[start:end]}</span>{transcript[end:]}<br/><span class='issue-pill'>Issue Detected</span>"
            message["Transcript"] = transcript

        query = dsl_gql(
            DSLMutation(
                schema.Mutation.addTranscriptSegment.args(input=message).select(
                    *transcript_segment_fields(schema),
                )
            )
        )
        def ignore_exception_fn(e): return True if (
            e["message"] == 'item put condition failure') else False
        tasks.append(
            execute_gql_query_with_retries(
                query,
                client_session=appsync_session,
                logger=LOGGER,
                should_ignore_exception_fn=ignore_exception_fn,
            ),
        )

    return tasks

async def add_sentiment_to_transcript(
    message: Dict[str, Any],
    sentiment_analysis_args: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
):
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    transcript_segment_with_sentiment = await transform_segment_to_add_sentiment(message, sentiment_analysis_args)

    result = {}
    query = dsl_gql(
        DSLMutation(
            schema.Mutation.addTranscriptSegment.args(input=transcript_segment_with_sentiment).select(
                *transcript_segment_fields(schema),
                *transcript_segment_sentiment_fields(schema),
            )
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    return result

def add_transcript_sentiment_analysis(
    message: Dict[str, Any],
    sentiment_analysis_args: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> List[Coroutine]:
    """Add Transcript Sentiment GraphQL Mutation"""

    tasks = []

    task = add_sentiment_to_transcript(message, sentiment_analysis_args, appsync_session)
    tasks.append(task)

    return tasks

async def execute_create_call_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    global CUSTOMER_PHONE_NUMBER
    global CALL_ID
    CUSTOMER_PHONE_NUMBER = message.get("CustomerPhoneNumber", "")
    CALL_ID = message.get("CallId", "")

    owner = get_owner_from_jwt(message.get("AccessToken"))
    message.update({"Owner": owner})

    # Contact Lens STARTED event type doesn't provide customer and system phone numbers, nor does it
    # have CreatedAt, so we will create a new message structure that conforms to other KDS channels.

    if('ContactId' in message.keys()):
        CALL_ID = message.get("ContactId")
        created_at = datetime.utcnow().astimezone().isoformat()
        (CUSTOMER_PHONE_NUMBER, system_phone_number) = get_caller_and_system_phone_numbers_from_connect(message)
        message.update({"CallId": CALL_ID, "CreatedAt": created_at, "CustomerPhoneNumber": CUSTOMER_PHONE_NUMBER, "SystemPhoneNumber": system_phone_number})

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.createCall.args(input=message).select(
                schema.CreateCallOutput.CallId, schema.CreateCallOutput.Owner
            )
        )
    )

    def ignore_exception_fn(e): return True if (
        e["message"] == 'item put condition failure') else False
    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
        should_ignore_exception_fn=ignore_exception_fn,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result

async def execute_update_call_status_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    status = CALL_EVENT_TYPE_TO_STATUS.get(message.get("EventType"))
    if not status:
        error_message = "unrecognized status from event type in update call"
        raise TypeError(error_message)

    if status == "STARTED":
        # STARTED status is set by createCall - skip update mutation
        return {"ok": True}

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    # Contact Lens event requires CallId mapped to ContactId

    if('ContactId' in message.keys()):
        call_id = message.get("ContactId")
        updated_at = datetime.utcnow().astimezone().isoformat()
        message['CallId'] = call_id
        message['UpdatedAt'] = updated_at

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateCallStatus.args(input={**message, "Status": status}).select(
                *call_fields(schema)
            )
        )
    )
    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result

async def execute_get_transcript_segments_query(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    call_id = message.get("CallId")
    if not call_id:
        error_message = "callid does not exist"
        raise TypeError(error_message)

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    # get_transcript_segments_input = {
    #     "CallId": call_id
    # }
    query = dsl_gql(
        DSLQuery(
            schema.Query.getTranscriptSegmentsWithSentiment.args(callId=call_id).select(
                schema.TranscriptSegmentsWithSentimentList.TranscriptSegmentsWithSentiment.select(
                    schema.TranscriptSegmentWithSentiment.PK,
                    schema.TranscriptSegmentWithSentiment.SK,
                    schema.TranscriptSegmentWithSentiment.CallId,
                    schema.TranscriptSegmentWithSentiment.Channel,
                    schema.TranscriptSegmentWithSentiment.SegmentId,
                    schema.TranscriptSegmentWithSentiment.StartTime,
                    schema.TranscriptSegmentWithSentiment.EndTime,
                    schema.TranscriptSegmentWithSentiment.Sentiment,
                    schema.TranscriptSegmentWithSentiment.SentimentWeighted,
                )
            )
        )
    )
    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("get transcript segments result", extra=dict(query=query_string, result=result))

    return result

def _get_sentiment_per_quarter(
    sentiment_list: List[SentimentEntry],
) -> List[SentimentByPeriodEntry]:
    sorted_sentiment = sorted(sentiment_list, key=lambda i: i["BeginOffsetMillis"])
    min_begin_time: float = (
        min(
            sorted_sentiment,
            key=lambda i: i["BeginOffsetMillis"],
        ).get("BeginOffsetMillis", 0.0)
        if sorted_sentiment
        else 0.0
    )
    max_end_time: float = (
        max(sorted_sentiment, key=lambda i: i["EndOffsetMillis"]).get("EndOffsetMillis", 0.0)
        if sorted_sentiment
        else 0.0
    )
    time_range: float = max_end_time - min_begin_time
    time_ranges = (
        (
            max((min_begin_time + time_range * i / 4), min_begin_time),
            min((min_begin_time + time_range * (i + 1) / 4), max_end_time),
        )
        for i in range(4)
    )
    quarters = (
        [
            s
            for s in sorted_sentiment
            if s["EndOffsetMillis"] > time_range[0] and s["EndOffsetMillis"] <= time_range[1]
        ]
        for time_range in time_ranges
    )
    sentiment_per_quarter = [
        SentimentByPeriodEntry(
            {
                "Score": fmean((i["Score"] for i in quarter)) if quarter else 0,
                "BeginOffsetMillis": (
                    min((i["BeginOffsetMillis"] for i in quarter)) if quarter else 0
                ),
                "EndOffsetMillis": (
                    max((i["EndOffsetMillis"] for i in quarter)) if quarter else 0
                ),
            }
        )
        for quarter in quarters
    ]

    return sentiment_per_quarter

async def get_aggregated_sentiment(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    call_id = message.get("CallId")
    if not call_id:
        error_message = "callid does not exist"
        raise TypeError(error_message)
    
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)
 
    result = await execute_get_transcript_segments_query(
        message=message,
        appsync_session=appsync_session
    )
 
    sentiment_entry_list_by_channel: Dict[ChannelType, SentimentPerChannel] = {}

    for segment in result.get("getTranscriptSegmentsWithSentiment").get("TranscriptSegmentsWithSentiment"):
        channel = segment.get("Channel", None)
        if channel and channel in ["AGENT", "CALLER"] :
            if segment.get("SentimentWeighted", None):
                LOGGER.debug("Aggregating sentiment entry", extra=segment)
                sentiment_entry : SentimentEntry = {
                    "Id" : segment["SegmentId"],
                    "BeginOffsetMillis": segment["StartTime"] * 1000,
                    "EndOffsetMillis": segment["EndTime"] * 1000,
                    "Sentiment": segment["Sentiment"],
                    "Score": segment["SentimentWeighted"]
                }
                if channel in sentiment_entry_list_by_channel:
                    tmp = sentiment_entry_list_by_channel[channel].get("SentimentList", [])
                    tmp.append(sentiment_entry)
                    sentiment_list_obj = {
                        "SentimentList": tmp
                    }
                else:
                    sentiment_list_obj = {
                        "SentimentList": [sentiment_entry]
                    }

                sentiment_entry_list_by_channel[channel] = sentiment_list_obj
                
    aggregated_sentiment:Sentiment = {}
    overall_sentiment:Dict[ChannelType, float] = {}
    sentiment_by_period_by_channel:Dict[ChannelType, List[SentimentByPeriodEntry]] = {}
    
    for channel in sentiment_entry_list_by_channel.keys():
        sentiment_list = sentiment_entry_list_by_channel[channel].get("SentimentList", [])
        sentiment_scores = [i["Score"] for i in sentiment_list]
        sentiment_average = fmean(sentiment_scores) if sentiment_scores else 0

        sentiment_per_quarter = (
            _get_sentiment_per_quarter(sentiment_list) if sentiment_list else []
        )

        overall_sentiment[channel] = sentiment_average
        sentiment_by_period_by_channel[channel] = sentiment_per_quarter
    
    LOGGER.debug("Overall Sentiment: ", extra=dict(DebugOverallSentiment=overall_sentiment))
    LOGGER.debug("Sentiment by Period: ", extra=dict(DebugSentimentByPeriod=sentiment_by_period_by_channel))
        
    aggregated_sentiment = {
        "OverallSentiment": overall_sentiment,
        "SentimentByPeriod": {
                "QUARTER": sentiment_by_period_by_channel
            },
        }

    return aggregated_sentiment

async def get_aggregate_call_data(
    message: Dict[str, object],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    
    call_id = message.get("CallId")
    if not call_id:
        error_message = "callid does not exist"
        raise TypeError(error_message)
    
    total_duration = float(message.get("EndTime", 0.0)) * 1000

    sentiment = await get_aggregated_sentiment(
        message=message,
        appsync_session=appsync_session
    ) 
    
    updated_at = message.get("UpdatedAt", datetime.utcnow().astimezone().isoformat())
    event_type = message.get("EventType", "")
    if event_type == "END":
        call_aggregation: Dict[str, object] = {
            "CallId": call_id,
            "Sentiment": sentiment
        }
    else:
        call_aggregation: Dict[str, object] = {
            "CallId": call_id,
            "TotalConversationDurationMillis": total_duration,
            "Sentiment": sentiment,
            "UpdatedAt": updated_at
        }

    return call_aggregation
    
async def get_call_aggregation_tasks(
    message: Dict[str, object],
    appsync_session: AppsyncAsyncClientSession,
) -> List[Coroutine]:

    call_aggregation = await get_aggregate_call_data(
        message=message,
        appsync_session=appsync_session
    )
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateCallAggregation.args(
                input=call_aggregation
            ).select(*call_fields(schema))
        )
    )

    tasks = []

    def ignore_exception_fn(e): return True if (
        e["message"] == 'item put condition failure') else False
    tasks.append(
        execute_gql_query_with_retries(
            query,
            client_session=appsync_session,
            logger=LOGGER,
            should_ignore_exception_fn=ignore_exception_fn,
        ),
    )

    return tasks


async def execute_update_call_aggregation_mutation(
    message: Dict[str, object],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    call_aggregation = await get_aggregate_call_data(
        message=message,
        appsync_session=appsync_session
    )

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateCallAggregation.args(
                input=call_aggregation
            ).select(*call_fields(schema))
        )
    )

    def ignore_exception_fn(e): return True if (
        e["message"] == 'item put condition failure') else False
    
    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
        should_ignore_exception_fn=ignore_exception_fn,

    )

    query_string = print_ast(query)
    LOGGER.debug(
        "transcript aggregation mutation", extra=dict(query=query_string, result=result)
    )
    return result

async def execute_add_s3_recording_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    recording_url = message.get("RecordingUrl")
    if not recording_url:
        error_message = "recording url doesn't exist in add s3 recording url event"
        raise TypeError(error_message)

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateRecordingUrl.args(
                input={**message, "RecordingUrl": recording_url}
            ).select(*call_fields(schema))
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result

async def execute_add_pca_url_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    pca_url = message.get("PcaUrl")
    if not pca_url:
        error_message = "pca url doesn't exist in add pca url event"
        raise TypeError(error_message)

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updatePcaUrl.args(
                input={**message, "PcaUrl": pca_url}
            ).select(*call_fields(schema))
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result

async def execute_add_call_category_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    categories = message["CategoryEvent"]["MatchedCategories"]
    if (len(categories) == 0):
        error_message = "No MatchedCategories in ADD_CALL_CATEGORY event"
        raise TypeError(error_message)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.addCallCategory.args(
                input={**message, "CallCategories": categories}
            ).select(*call_fields(schema))
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result

async def execute_add_issues_detected_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    issues_detected = message.get("IssuesDetected", None)
    issueText = ""
    if issues_detected and len(issues_detected) > 0:
        LOGGER.debug("issue detected in add issues detected mutation")
        offsets = issues_detected[0].get("CharacterOffsets")
        start = int(offsets.get("Begin"))
        end = int(offsets.get("End"))
        if (start >= 0 and end >= 0):
            transcript = message["Transcript"]
            issueText = transcript[start:end]

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.addIssuesDetected.args(
                input={**message, "IssuesDetected": issueText}
            ).select(*call_fields(schema))
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result

async def execute_add_call_summary_text_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    calltext = message.get("CallSummaryText", None)
    call_summary_text = ""
    if calltext and len(calltext) > 0:
        call_summary_text = calltext

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    
    query = dsl_gql(
        DSLMutation(
            schema.Mutation.addCallSummaryText.args(
                input={**message, "CallSummaryText": call_summary_text}
            ).select(*call_fields(schema))
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result

async def execute_add_agent_assist_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    LOGGER.debug("Add Agent Assist Mutation message: %s", json.dumps(message))
    query = dsl_gql(
        DSLMutation(
            schema.Mutation.addTranscriptSegment.args(input=message).select(
                *transcript_segment_fields(schema),
            )
        )
    )

    LOGGER.debug("Executing QUERY: %s", query)


    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result

async def execute_update_agent_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    agentId = message.get("AgentId")
    if not agentId:
        error_message = "AgentId doesn't exist in UPDATE_AGENT event"
        raise TypeError(error_message)

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateAgent.args(
                input={**message, "AgentId": agentId}
            ).select(*call_fields(schema))
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result

##########################################################################
# Call Categories
##########################################################################

async def send_call_category(
    transcript_segment_args: Dict[str, Any],
    category: str,
    appsync_session: AppsyncAsyncClientSession
):
    """Send Call Category Transcript Segment"""
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    transcript_segment = {**transcript_segment_args, "Transcript": category}

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.addTranscriptSegment.args(input=transcript_segment).select(
                *transcript_segment_fields(schema),
            )
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    return result

async def publish_sns_category(
    sns_client: SNSClient,
    category_name: str,
    call_id: str
):
    LOGGER.debug("Publishing Call Category to SNS")
    isAlert = False
    if "AlertRegEx" in SETTINGS:
        isMatch = SETTINGS["AlertRegEx"].match(category_name)
        if isMatch:
            isAlert = True
    
    result = await publish_sns(category_name=category_name,
                               call_id=call_id,
                               sns_topic_arn=SNS_TOPIC_ARN,
                               sns_client=sns_client,
                               alert=isAlert
                               )
                        
    return result

def add_call_category(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
    sns_client: SNSClient
) -> List[Coroutine]:
    """Add Categories GraphQL Mutations"""
    # pylint: disable=too-many-locals
    LOGGER.debug("Detected Call Category")

    tasks = []
    for category in message["CategoryEvent"]["MatchedCategories"]:
        # Publish SNS message for the category
        sns_task = publish_sns_category(
            sns_client=sns_client,
            category_name=category,
            call_id=message["CallId"]
        )
        tasks.append(sns_task)
        # Insert Category marker into transcript, if timestamps are provided in the event.
        try:
            timestampRanges = message["CategoryEvent"]["MatchedDetails"][category]["TimestampRanges"]
        except KeyError:
            LOGGER.debug("Category: %s has no TimestampRanges. Skip transcript insertion.", category)
            continue
        for timestampRange in timestampRanges:
            start_time = timestampRange["EndOffsetMillis"]/1000
            end_time = start_time + 0.1
            send_call_category_args = []
            send_call_category_args.append(
                dict(
                    category=category,
                    transcript_segment_args=dict(
                        CallId=message["CallId"],
                        Channel="CATEGORY_MATCH",
                        CreatedAt=message["CreatedAt"],
                        EndTime=end_time,
                        ExpiresAfter=get_meeting_ttl(),
                        SegmentId=str(uuid.uuid4()),
                        StartTime=start_time,
                        IsPartial=False,
                        Status="TRANSCRIBING",
                    ),
                )
            )
            for call_category_args in send_call_category_args:
                task = send_call_category(
                    appsync_session=appsync_session,
                    **call_category_args,
                )
                tasks.append(task)

    return tasks

def add_contact_lens_call_category(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
    sns_client: SNSClient
) -> List[Coroutine]:
    """Add Categories GraphQL Mutations"""
    # pylint: disable=too-many-locals
    LOGGER.debug("Detected Call Category")
    send_call_category_args = []
    tasks = []
    call_id = message["ContactId"]

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    for segment in message.get("Segments", []):
        # only handle categories and transcripts with issues
        if (
            "Transcript" in segment and not segment["Transcript"].get("IssuesDetected")
        ) and "Categories" not in segment:
            continue

        categories = segment.get("Categories", {})
        matched_categories = categories.get("MatchedCategories", [])

        if (len(matched_categories) > 0):
            query = dsl_gql(
                DSLMutation(
                    schema.Mutation.addCallCategory.args(
                        input={"CallId": message["ContactId"], "CallCategories": matched_categories}
                    ).select(*call_fields(schema))
                )
            )

            tasks.append(
                execute_gql_query_with_retries(
                    query,
                    client_session=appsync_session,
                    logger=LOGGER,
                ),
            )

        for category in categories.get("MatchedCategories", []):
            category_details = categories["MatchedDetails"][category]
            category_segment = transform_segment_to_categories_agent_assist(
                category=category,
                category_details=category_details,
                call_id=call_id,
            )

            send_call_category_args.append(
                dict(
                    category=category_segment['Transcript'],
                    transcript_segment_args=dict(
                        CallId=message["ContactId"],
                        Channel="CATEGORY_MATCH",
                        CreatedAt=category_segment["CreatedAt"],
                        EndTime=category_segment['EndTime'],
                        ExpiresAfter=get_meeting_ttl(),
                        SegmentId=str(uuid.uuid4()),
                        StartTime=category_segment['StartTime'],
                        IsPartial=category_segment['IsPartial'],
                        Status="TRANSCRIBING",
                    ),
                )
            )

    for call_category_args in send_call_category_args:
        task = send_call_category(
            appsync_session=appsync_session,
            **call_category_args,
        )
        tasks.append(task)
        sns_task = publish_sns_category(
            sns_client=sns_client,
            category_name=category,
            call_id=message["ContactId"]
        )
        tasks.append(sns_task)


    return tasks



##########################################################################
# Transcript Lambda Hook
# User provided function should return a copy of the input event with
# optionally modified "Transcript" field (to support custom redaction or
# other transcript manipulation.
# The original transcript can be optionally returned as "OriginalTranscript"
# to be used as input for Agent Assist bot or Lambda, otherwise "Transcript"
# field is used for Agent Assist input.
##########################################################################

def invoke_transcript_lambda_hook(
    message: Dict[str, Any]
):
    if (message.get("IsPartial") == False or TRANSCRIPT_LAMBDA_HOOK_FUNCTION_NONPARTIAL_ONLY == False):
        LOGGER.debug("Transcript Lambda Hook Arn: %s", TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN)
        LOGGER.debug("Transcript Lambda Hook Request: %s", message)
        lambda_response = LAMBDA_HOOK_CLIENT.invoke(
            FunctionName=TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN,
            InvocationType='RequestResponse',
            Payload=json.dumps(message)
        )
        LOGGER.debug("Transcript Lambda Hook Response: ", extra=lambda_response)
        try:
            message = json.loads(lambda_response.get("Payload").read().decode("utf-8"))
        except Exception as error:
            LOGGER.error(
                "Transcript Lambda Hook result payload parsing exception. Lambda must return JSON object with (modified) input event fields",
                extra=error,
            )
    return message

async def get_call_details(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")

    global CUSTOMER_PHONE_NUMBER
    global CALL_ID

    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLQuery(
            schema.Query.getCall.args(CallId=message["CallId"]).select(
                *call_fields(schema),
            )
        )
    )

    result = await execute_gql_query_with_retries(
        query,
        client_session=appsync_session,
        logger=LOGGER,
    )

    result = result['getCall']
    LOGGER.debug("Get Call result %s", json.dumps(result))

    CUSTOMER_PHONE_NUMBER = result['CustomerPhoneNumber']
    CALL_ID = result['CallId']
    call_summary = result.get("CallSummaryText", "")

    return dict(
        CustomerPhoneNumber=CUSTOMER_PHONE_NUMBER,
        CallId=CALL_ID,
        CallDataStream=CALL_DATA_STREAM_NAME,
        CallSummaryText=call_summary
    )


def get_caller_and_system_phone_numbers_from_connect(
    message: Dict[str, Any]
):
    instanceId = message.get("InstanceId")
    contactId = message.get("ContactId")

    client = boto3.client('connect')
    response = client.get_contact_attributes(
        InstanceId=instanceId,
        InitialContactId=contactId
    )
    # Try to retrieve customer phone number from contact attribute
    customer_phone_number = response["Attributes"].get(CONNECT_CONTACT_ATTR_CUSTOMER_PHONE_NUMBER)
    if not customer_phone_number:
        LOGGER.warning(
            f"Unable to retrieve contact attribute: '{CONNECT_CONTACT_ATTR_CUSTOMER_PHONE_NUMBER}'. Reverting to default.")
        customer_phone_number = DEFAULT_CUSTOMER_PHONE_NUMBER
    # Try to retrieve system phone number from contact attribute: "LCA System Phone Number"
    system_phone_number = response["Attributes"].get(CONNECT_CONTACT_ATTR_SYSTEM_PHONE_NUMBER)
    if not system_phone_number:
        LOGGER.warning(
            "Unable to retrieve contact attribute: '{CONNECT_CONTACT_ATTR_SYSTEM_PHONE_NUMBER}'. Reverting to default.")
        system_phone_number = DEFAULT_SYSTEM_PHONE_NUMBER
    LOGGER.info(
        f"Setting customer_phone_number={customer_phone_number}, system_phone_number={system_phone_number}")
    return (customer_phone_number, system_phone_number)

def add_contact_lens_agent_assistances(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> List[Coroutine]:
    """Add Contact Lens Agent Assist GraphQL Mutations"""
    # pylint: disable=too-many-locals
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    call_id = message["ContactId"]

    tasks = []
    for segment in message.get("Segments", []):
        # only handle categories and transcripts with issues
        if (
            "Transcript" in segment and not segment["Transcript"].get("IssuesDetected")
        ) and "Categories" not in segment:
            continue

        transcript_segments = []

        categories = segment.get("Categories", {})
        for category in categories.get("MatchedCategories", []):
            category_details = categories["MatchedDetails"][category]
            category_segment = transform_segment_to_categories_agent_assist(
                category=category,
                category_details=category_details,
                call_id=call_id,
            )
            category_segment["Transcript"] = "[Matched Category] " + category_segment["Transcript"]
            transcript_segments.append(category_segment)

        for transcript_segment in transcript_segments:
            query = dsl_gql(
                DSLMutation(
                    schema.Mutation.addTranscriptSegment.args(input=transcript_segment).select(
                        *transcript_segment_fields(schema),
                    )
                )
            )
            tasks.append(
                execute_gql_query_with_retries(
                    query,
                    client_session=appsync_session,
                    logger=LOGGER,
                ),
            )

    return tasks

##########################################################################
# Fix CamelCasing from Chime
##########################################################################

def convert_keys_to_uppercamelcase(d):
    new_dict = {}
    for k, v in d.items():
        if isinstance(v, dict):
            new_dict[k[0].upper() + k[1:]] = convert_keys_to_uppercamelcase(v)
        else:
            new_dict[k[0].upper() + k[1:]] = v
    return new_dict

##########################################################################
# merge dicts
##########################################################################

def merge_dicts(d1, d2):
    new_dict = d1.copy()
    new_dict.update(d2)
    return new_dict


##########################################################################
# Send call id to session id mapping event
##########################################################################

def send_call_session_mapping_event(call_id, session_id):
    client = boto3.client('events')

    LOGGER.debug("Sending CALL_SESSION_MAPPING event. callId: %s, SessionId: %s", call_id, session_id)
    event_response = client.put_events(
        Entries=[
            {
                'Source': "lca-solution",
                'DetailType': "CALL_SESSION_MAPPING",
                'Detail': json.dumps({
                    'callId': call_id,
                    'sessionId': session_id,
                }),
            }
        ]
    )
    LOGGER.debug("Send CALL_SESSION_MAPPING Response: ", extra=event_response)

##########################################################################
# check for agent assist wake phrase
##########################################################################
def isAssistantWakePhrase(transcript):
    LOGGER.debug("Checking for Assistant Wake Phrase Regex match '%s'", SETTINGS['AssistantWakePhraseRegEx'])
    if (SETTINGS['AssistantWakePhraseRegEx'].search(transcript)):
        LOGGER.debug("Assistant Wake Phrase detected: %s", transcript)
        return True
    return False


##########################################################################
# Main event processing
##########################################################################

async def execute_process_event_api_mutation(
    message: Dict[str, Any],
    settings: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
    sns_client: SNSClient,
    agent_assist_args: Dict[str, Any],
    sentiment_analysis_args: Dict[str, Any]
) -> Dict[Literal["successes", "errors"], List]:

    """Executes AppSync API Mutation"""
    # pylint: disable=global-statement
    global IS_LEX_AGENT_ASSIST_ENABLED
    global IS_LAMBDA_AGENT_ASSIST_ENABLED
    global SETTINGS
    # pylint: enable=global-statement

    IS_LEX_AGENT_ASSIST_ENABLED = agent_assist_args.get("is_lex_agent_assist_enabled")
    IS_LAMBDA_AGENT_ASSIST_ENABLED = agent_assist_args.get("is_lambda_agent_assist_enabled")
    SETTINGS = settings

    return_value: Dict[Literal["successes", "errors"], List] = {
        "successes": [],
        "errors": [],
    }

    metadata = None
    
    # normalize the casing
    message = convert_keys_to_uppercamelcase(message)
    
    metadata_str = message.get("Metadata", None)
    if metadata_str != None:
        metadata = json.loads(metadata_str)
        metadata = convert_keys_to_uppercamelcase(metadata)
        
        message = merge_dicts(message, metadata)

    event_type_map = dict(
        STARTED="START",
        START="START",
        COMPLETED="END",
        END="END",
        SEGMENTS="ADD_TRANSCRIPT_SEGMENT",
        ADD_TRANSCRIPT_SEGMENT="ADD_TRANSCRIPT_SEGMENT",
        FAILED="ERRORED",
        UPDATE_AGENT="UPDATE_AGENT",
        ADD_SUMMARY="ADD_SUMMARY",
        ADD_AGENT_ASSIST="ADD_AGENT_ASSIST",
        ADD_CALL_CATEGORY="ADD_CALL_CATEGORY",
        ADD_S3_RECORDING_URL="ADD_S3_RECORDING_URL",
        ADD_PCA_URL="ADD_PCA_URL",
        CALL_ANALYTICS_METADATA="CALL_ANALYTICS_METADATA",
    )

    msg_event_type = message.get("EventType", "")
    event_type = event_type_map.get(msg_event_type, "")

    if event_type == "":
        # This is possibly a message from Flume. Let's fix the message if it is
        if message.get("UtteranceEvent", "") != "" or message.get("TranscriptEvent", "") != "":
            message["EventType"] = "ADD_TRANSCRIPT_SEGMENT"
            event_type = "ADD_TRANSCRIPT_SEGMENT"
        if message.get("CategoryEvent", "") != "":
            message["EventType"] = "ADD_CALL_CATEGORY"
            event_type = "ADD_CALL_CATEGORY"
        if message.get("Service-type", "") == "CallAnalytics" and message.get("Detail-type", "") == "CallAnalyticsMetadata":
            message["EventType"] = "CALL_ANALYTICS_METADATA"
            event_type = "CALL_ANALYTICS_METADATA"

    message["EventType"] = event_type
    # default expiration for meeting (note: transcript segments can have a different - earlier - expiration)
    message["ExpiresAfter"] = get_meeting_ttl() 

    LOGGER.debug("Process event. eventType: %s, callId: %s", event_type, message.get("CallId", ""))

    if event_type == "START":
        # CREATE CALL
        LOGGER.debug("CREATE CALL")
        response = await execute_create_call_mutation(
            message=message,
            appsync_session=appsync_session
        )

        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

        if (START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN):
            payload = dict(
                CustomerPhoneNumber=CUSTOMER_PHONE_NUMBER,
                CallId=CALL_ID,
                CallDataStream=CALL_DATA_STREAM_NAME,
            )
            LAMBDA_HOOK_CLIENT.invoke(
                FunctionName=START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN,
                InvocationType='Event',
                Payload=json.dumps(payload)
            )

    elif event_type in [
        "END",
    ]:
        LOGGER.debug("END Event: update status")
        response = await execute_update_call_status_mutation(
            message=message,
            appsync_session=appsync_session
        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)
        
        if (IS_TRANSCRIPT_SUMMARY_ENABLED):
            LAMBDA_HOOK_CLIENT.invoke(
                FunctionName=ASYNC_TRANSCRIPT_SUMMARY_ORCHESTRATOR_ARN,
                InvocationType='Event',
                Payload=json.dumps(message)
            )
            LOGGER.debug("END Event: Invoked Async Transcript Summary Lambda")
      
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

    elif event_type == "ADD_SUMMARY":

        LOGGER.debug("ADD_SUMMARY MUTATION ")
        response = await execute_add_call_summary_text_mutation(
            message=message,
            appsync_session=appsync_session
        )
 
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

        if (POST_CALL_SUMMARY_LAMBDA_HOOK_FUNCTION_ARN):
            payload = await get_call_details(
                message=message,
                appsync_session=appsync_session)

            LAMBDA_HOOK_CLIENT.invoke(
                FunctionName=POST_CALL_SUMMARY_LAMBDA_HOOK_FUNCTION_ARN,
                InvocationType='Event',
                Payload=json.dumps(payload)
            )

    elif event_type == "ADD_AGENT_ASSIST":
        LOGGER.debug("ADD_AGENT_ASSIST MUTATION ")
        normalized_message = normalize_transcript_segments({**message})

        response = await execute_add_agent_assist_mutation(
            message=normalized_message[0],
            appsync_session=appsync_session
        )

        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

    elif event_type == "ADD_TRANSCRIPT_SEGMENT":

        # ADD_TRANSCRIPT_SEGMENT event supports these 3 types of message structure.
        #   The logic for populating transcripts, sentiment values and agent assist messages depend on
        #    which one of these 3 json structures are populated in the KDS Event message.
        #
        #  1. custom i.e. source populates invidividual transcript fields, including optional sentiment field
        #  2. TranscriptEvent - json structure from standard Transcribe API
        #  3. UtteranceEvent - json structure from TCA streaming API

        utteranceEvent = message.get("UtteranceEvent", None)
        if utteranceEvent:
            participantRole = utteranceEvent.get("ParticipantRole", None)
            if not participantRole:
                return return_value

        # normalize_transcript_segments also sets transcript segment expiration time
        normalized_messages = normalize_transcript_segments({**message})

        add_transcript_tasks = []
        add_transcript_sentiment_tasks = []

        for normalized_message in normalized_messages:

            # Invoke custom lambda hook (if any) and use returned version of message.
            if (TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN):
                normalized_message = invoke_transcript_lambda_hook(normalized_message)

            issues_detected = normalized_message.get("IssuesDetected", None)
            if issues_detected and len(issues_detected) > 0:
                LOGGER.debug("Add Issues Detected to Call Summary")
                response = await execute_add_issues_detected_mutation(
                    message=normalized_message,
                    appsync_session=appsync_session
                )
                if isinstance(response, Exception):
                    return_value["errors"].append(response)
                else:
                    return_value["successes"].append(response)

            LOGGER.debug("Add Transcript Segment")
            add_transcript_tasks.extend(
                add_transcript_segments(
                    message=normalized_message,
                    appsync_session=appsync_session,
                )
            )
            if IS_SENTIMENT_ANALYSIS_ENABLED and not normalized_message["IsPartial"]:
                LOGGER.debug("Add Sentiment Analysis")
                add_transcript_sentiment_tasks.extend(
                    add_transcript_sentiment_analysis(
                        message=normalized_message,
                        sentiment_analysis_args=sentiment_analysis_args,
                        appsync_session=appsync_session,
                    )
                )
            if (IS_LEX_AGENT_ASSIST_ENABLED or IS_LAMBDA_AGENT_ASSIST_ENABLED) and (not normalized_message["IsPartial"] or 'ContactId' in normalized_message.keys()) and isAssistantWakePhrase(normalized_message["Transcript"]):
                LAMBDA_HOOK_CLIENT.invoke(
                    FunctionName=ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN,
                    InvocationType='Event',
                    Payload=json.dumps(normalized_message)
                )

        add_call_category_tasks = []

        if 'ContactId' in message.keys():
            add_call_category_tasks = add_contact_lens_call_category(
                message=message,
                appsync_session=appsync_session,
                sns_client=sns_client,
            )

        update_call_aggregation_tasks = []
        for normalized_message in normalized_messages:
            if not normalized_message["IsPartial"]:
                update_call_aggregation_tasks = await get_call_aggregation_tasks(
                    message=normalized_message,
                    appsync_session=appsync_session,
                )

        task_responses = await asyncio.gather(
            *add_transcript_tasks,
            *add_transcript_sentiment_tasks,
            *add_call_category_tasks,
            *update_call_aggregation_tasks,
            # *add_tca_agent_assist_tasks,
            return_exceptions=True,
        )

        for response in task_responses:
            if isinstance(response, Exception):
                return_value["errors"].append(response)
            else:
                return_value["successes"].append(response)

    elif event_type == "ADD_CALL_CATEGORY":
        LOGGER.debug("Add Call Category to Call details")
        response = await execute_add_call_category_mutation(
            message=message,
            appsync_session=appsync_session
        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

        LOGGER.debug("Add Call Category to Transcript segments")
        add_call_category_tasks = []
        add_call_category_tasks = add_call_category(
            message=message,
            appsync_session=appsync_session,
            sns_client=sns_client
        )
        task_responses = await asyncio.gather(
            *add_call_category_tasks,
            return_exceptions=True,
        )

        for response in task_responses:
            if isinstance(response, Exception):
                return_value["errors"].append(response)
            else:
                return_value["successes"].append(response)

    elif event_type == "ADD_S3_RECORDING_URL":
        # ADD S3 RECORDING URL
        LOGGER.debug("Add recording url")
        response = await execute_add_s3_recording_mutation(
            message=message,
            appsync_session=appsync_session
        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

    elif event_type == "ADD_PCA_URL":
        # ADD PCA URL
        LOGGER.debug("Add PCA url")
        response = await execute_add_pca_url_mutation(
            message=message,
            appsync_session=appsync_session
        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

    elif event_type == "UPDATE_AGENT":
        # UPDATE AGENT
        LOGGER.debug("Update AgentId for call")
        response = await execute_update_agent_mutation(
            message=message,
            appsync_session=appsync_session
        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)
    elif event_type == "CALL_ANALYTICS_METADATA":
        meta = json.loads(message['Metadata'])
        LOGGER.debug("S3 URL from metadata %s", meta['oneTimeMetadata']['s3RecordingUrl'])

        session_id = re.search(r"(?i)\/(.+\/)*(.+)\.(wav)$", meta['oneTimeMetadata']['s3RecordingUrl']).group(2)
        send_call_session_mapping_event(meta['callId'], session_id)

    else:
        LOGGER.warning("unknown event type [%s]", event_type)

    return return_value
