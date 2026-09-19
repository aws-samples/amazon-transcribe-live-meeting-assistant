#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Transcription Passthrough Lambda Function"""

import asyncio
import json
import re
from os import environ, getenv
from typing import TYPE_CHECKING, Any, Dict, List

import boto3

# imports from Lambda layer
# pylint: disable=import-error
from appsync_utils import AppsyncAioGqlClient

# third-party imports from Lambda layer
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

# local imports
from batch_item_failures import SequenceTrackingBatchProcessor, send_discarded_records
from botocore.config import Config as BotoCoreConfig
from event_processor import execute_process_event_api_mutation

# pylint: enable=import-error

if TYPE_CHECKING:
    from boto3 import Session as Boto3Session
    from mypy_boto3_comprehend.client import ComprehendClient
    from mypy_boto3_dynamodb.service_resource import DynamoDBServiceResource
    from mypy_boto3_dynamodb.service_resource import Table as DynamoDbTable
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_sns.client import SNSClient
    from mypy_boto3_sqs.client import SQSClient
    from mypy_boto3_ssm.client import SSMClient
else:
    Boto3Session = object
    DynamoDBServiceResource = object
    DynamoDbTable = object
    LambdaClient = object
    ComprehendClient = object
    SNSClient = object
    SQSClient = object
    SSMClient = object

APPSYNC_GRAPHQL_URL = environ["APPSYNC_GRAPHQL_URL"]
APPSYNC_CLIENT = AppsyncAioGqlClient(url=APPSYNC_GRAPHQL_URL, fetch_schema_from_transport=True)

BOTO3_SESSION: Boto3Session = boto3.Session()
CLIENT_CONFIG = BotoCoreConfig(
    retries={"mode": "adaptive", "max_attempts": 3},
)

STATE_DYNAMODB_TABLE_NAME = environ["STATE_DYNAMODB_TABLE_NAME"]
STATE_DYNAMODB_RESOURCE: DynamoDBServiceResource = BOTO3_SESSION.resource(
    "dynamodb",
    config=CLIENT_CONFIG,
)
STATE_DYNAMODB_TABLE: DynamoDbTable = STATE_DYNAMODB_RESOURCE.Table(STATE_DYNAMODB_TABLE_NAME)


IS_LAMBDA_AGENT_ASSIST_ENABLED = getenv("IS_LAMBDA_AGENT_ASSIST_ENABLED", "true").lower() == "true"

IS_SENTIMENT_ANALYSIS_ENABLED = getenv("IS_SENTIMENT_ANALYSIS_ENABLED", "true").lower() == "true"
if IS_SENTIMENT_ANALYSIS_ENABLED:
    COMPREHEND_CLIENT: ComprehendClient = BOTO3_SESSION.client("comprehend", config=CLIENT_CONFIG)
else:
    COMPREHEND_CLIENT = None
COMPREHEND_LANGUAGE_CODE = getenv("COMPREHEND_LANGUAGE_CODE", "en")

SNS_CLIENT: SNSClient = BOTO3_SESSION.client("sns", config=CLIENT_CONFIG)
SSM_CLIENT: SSMClient = BOTO3_SESSION.client("ssm", config=CLIENT_CONFIG)

# Records that cannot be decoded are copied here instead of being reported as
# batch item failures. The event source mapping's own on-failure destination is
# the same queue, but it only receives batches the invocation actually failed.
SQS_CLIENT: SQSClient = BOTO3_SESSION.client("sqs", config=CLIENT_CONFIG)
DISCARDED_RECORDS_QUEUE_URL = getenv("DISCARDED_RECORDS_QUEUE_URL", "")

LOGGER = Logger(location="%(filename)s:%(lineno)d - %(funcName)s()")

EVENT_LOOP = asyncio.get_event_loop()

setting_response = SSM_CLIENT.get_parameter(Name=getenv("PARAMETER_STORE_NAME"))
SETTINGS = json.loads(setting_response["Parameter"]["Value"])
if "CategoryAlertRegex" in SETTINGS:
    SETTINGS["AlertRegEx"] = re.compile(SETTINGS["CategoryAlertRegex"])
if "AssistantWakePhraseRegEx" in SETTINGS:
    SETTINGS["AssistantWakePhraseRegEx"] = re.compile(SETTINGS["AssistantWakePhraseRegEx"])


async def process_event(event) -> SequenceTrackingBatchProcessor:
    """Processes a Batch of Transcript Records"""
    async with SequenceTrackingBatchProcessor(
        appsync_client=APPSYNC_CLIENT,
        agent_assist_args=dict(
            is_lambda_agent_assist_enabled=IS_LAMBDA_AGENT_ASSIST_ENABLED,
        ),
        sentiment_analysis_args=dict(
            comprehend_client=COMPREHEND_CLIENT, comprehend_language_code=COMPREHEND_LANGUAGE_CODE
        ),
        # called for each record right before the context manager exits
        api_mutation_fn=execute_process_event_api_mutation,
        sns_client=SNS_CLIENT,
        settings=SETTINGS,
    ) as processor:
        await processor.handle_event(event=event)

    return processor


@LOGGER.inject_lambda_context
def handler(event, context: LambdaContext) -> Dict[str, List[Dict[str, str]]]:
    # pylint: disable=unused-argument
    """Lambda handler.

    Returns a Kinesis partial batch response. The event source mapping is
    configured with ``FunctionResponseTypes: [ReportBatchItemFailures]``, so the
    shard checkpoint advances past every record except the ones named here. See
    ``batch_item_failures`` for which failures are reported and why.
    """
    LOGGER.debug("lambda event", extra={"event": event})

    processor = EVENT_LOOP.run_until_complete(process_event(event=event))
    event_processor_results: Dict[str, List[Any]] = processor.results
    LOGGER.debug("event processor results", extra=dict(event_results=event_processor_results))

    errors = event_processor_results.get("errors", [])
    for error in errors:
        LOGGER.error(
            "event processor error: %s",
            error,
            exc_info=error if isinstance(error, BaseException) else None,
        )

    discarded = processor.discarded_sequence_numbers
    if discarded:
        sent = send_discarded_records(
            sqs_client=SQS_CLIENT,
            queue_url=DISCARDED_RECORDS_QUEUE_URL,
            event=event,
            sequence_numbers=discarded,
        )
        LOGGER.warning(
            "skipped Kinesis records that could not be decoded",
            extra=dict(
                discarded_sequence_numbers=discarded,
                copied_to_discarded_records_queue=sent,
            ),
        )

    if errors and processor.has_unreportable_failure:
        # No sequence number can express this failure, so the only way to keep
        # the event source mapping from checkpointing past it is to fail the
        # whole invocation.
        raise RuntimeError("event processor failed with no reportable Kinesis record")

    response = processor.batch_item_failures_response
    if response["batchItemFailures"]:
        LOGGER.warning(
            "reporting Kinesis batch item failures",
            extra=dict(
                batch_item_failures=response["batchItemFailures"],
                failing_sequence_numbers=processor.failing_sequence_numbers,
                discarded_sequence_numbers=processor.discarded_sequence_numbers,
            ),
        )

    return response
