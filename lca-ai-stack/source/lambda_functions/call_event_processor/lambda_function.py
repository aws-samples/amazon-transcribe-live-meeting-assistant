#!/usr/bin/env python3.11
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
""" Transcription Passthrough Lambda Function
"""
import asyncio
from os import environ, getenv
from typing import TYPE_CHECKING, Dict, List
import json
import re

# third-party imports from Lambda layer
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
import boto3
from botocore.config import Config as BotoCoreConfig

# imports from Lambda layer
# pylint: disable=import-error
from appsync_utils import AppsyncAioGqlClient
from transcript_batch_processor import TranscriptBatchProcessor

# local imports
from event_processor import execute_process_event_api_mutation

# pylint: enable=import-error

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import DynamoDBServiceResource, Table as DynamoDbTable
    from mypy_boto3_lexv2_runtime.client import LexRuntimeV2Client
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_comprehend.client import ComprehendClient
    from mypy_boto3_sns.client import SNSClient
    from mypy_boto3_ssm.client import SSMClient
    from boto3 import Session as Boto3Session
else:
    Boto3Session = object
    DynamoDBServiceResource = object
    DynamoDbTable = object
    LexRuntimeV2Client = object
    LambdaClient = object
    ComprehendClient = object
    SNSClient = object
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

IS_LEX_AGENT_ASSIST_ENABLED = getenv("IS_LEX_AGENT_ASSIST_ENABLED", "true").lower() == "true"

IS_LAMBDA_AGENT_ASSIST_ENABLED = getenv("IS_LAMBDA_AGENT_ASSIST_ENABLED", "true").lower() == "true"

IS_SENTIMENT_ANALYSIS_ENABLED = getenv("IS_SENTIMENT_ANALYSIS_ENABLED", "true").lower() == "true"
if IS_SENTIMENT_ANALYSIS_ENABLED:
    COMPREHEND_CLIENT: ComprehendClient = BOTO3_SESSION.client("comprehend", config=CLIENT_CONFIG)
else:
    COMPREHEND_CLIENT = None
COMPREHEND_LANGUAGE_CODE = getenv("COMPREHEND_LANGUAGE_CODE", "en")

SNS_CLIENT:SNSClient = BOTO3_SESSION.client("sns", config=CLIENT_CONFIG)
SSM_CLIENT:SSMClient = BOTO3_SESSION.client("ssm", config=CLIENT_CONFIG)

LOGGER = Logger(location="%(filename)s:%(lineno)d - %(funcName)s()")

EVENT_LOOP = asyncio.get_event_loop()

setting_response = SSM_CLIENT.get_parameter(Name=getenv("PARAMETER_STORE_NAME"))
SETTINGS = json.loads(setting_response["Parameter"]["Value"])
if "CategoryAlertRegex" in SETTINGS:
    SETTINGS['AlertRegEx'] = re.compile(SETTINGS["CategoryAlertRegex"])
if "AssistantWakePhraseRegEx" in SETTINGS:
    SETTINGS['AssistantWakePhraseRegEx'] = re.compile(SETTINGS["AssistantWakePhraseRegEx"])

async def process_event(event) -> Dict[str, List]:
    """Processes a Batch of Transcript Records"""
    async with TranscriptBatchProcessor(
        appsync_client=APPSYNC_CLIENT,
        agent_assist_args=dict(
            is_lex_agent_assist_enabled=IS_LEX_AGENT_ASSIST_ENABLED,
            is_lambda_agent_assist_enabled=IS_LAMBDA_AGENT_ASSIST_ENABLED,
        ),
        sentiment_analysis_args=dict(
            comprehend_client=COMPREHEND_CLIENT,
            comprehend_language_code=COMPREHEND_LANGUAGE_CODE
        ),
        # called for each record right before the context manager exits
        api_mutation_fn=execute_process_event_api_mutation,
        sns_client=SNS_CLIENT,
        settings=SETTINGS
    ) as processor:
        await processor.handle_event(event=event)

    return processor.results

@LOGGER.inject_lambda_context
def handler(event, context: LambdaContext):
    # pylint: disable=unused-argument
    """Lambda handler"""
    LOGGER.debug("lambda event", extra={"event": event})

    event_processor_results = EVENT_LOOP.run_until_complete(process_event(event=event))
    LOGGER.debug("event processor results", extra=dict(event_results=event_processor_results))

    for error in event_processor_results.get("errors", []):
        LOGGER.error("event processor error: %s", error)
        if isinstance(error, Exception):
            try:
                raise error
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception("event processor exception")

    return 
