# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
""" Async SNS Client Utilities
"""
import asyncio
from typing import TYPE_CHECKING
import json

# third-party imports from Lambda layer
from aws_lambda_powertools import Logger

LOGGER = Logger(child=True, location="%(filename)s:%(lineno)d - %(funcName)s()")

if TYPE_CHECKING:
    from mypy_boto3_sns.client import SNSClient
    from mypy_boto3_sns.type_defs import PublishSNSResponseTypeDef
else:
    SNSClient = object
    PublishSNSResponseTypeDef = object

async def publish_sns(
    category_name: str,
    call_id: str,
    sns_topic_arn: str,
    sns_client: SNSClient,
    alert: bool = False,
    max_retries: int = 3,
) -> PublishSNSResponseTypeDef:
    """Runs Lex Recognize Text in the Async Event Loop"""
    # pylint: disable=too-many-arguments
    retry_count = 0
    sns_published: bool = False
    sns_response: PublishSNSResponseTypeDef
    while not sns_published and retry_count < max_retries:
        try:
            event_loop = asyncio.get_event_loop()
            sns_response = await event_loop.run_in_executor(
                None,
                lambda: sns_client.publish(
                    TargetArn=sns_topic_arn,
                    Message=json.dumps({'default': json.dumps({'call_id':call_id, 'category_name':category_name, 'alert': alert})}),
                    MessageStructure='json',
                    Subject='Call Category Match'
                ),
            )
            sns_published = True
        except sns_client.exceptions.ThrottledException as error:
            retry_count = retry_count + 1
            LOGGER.warning(
                "recognize_text retriable exception",
                extra=dict(error=error, retry_count=retry_count),
            )
            await asyncio.sleep(0.25 * retry_count)
            if retry_count >= max_retries:
                raise
        except Exception:  # pylint: disable=broad-except
            LOGGER.exception("publish_sns")
            raise

    return sns_response
