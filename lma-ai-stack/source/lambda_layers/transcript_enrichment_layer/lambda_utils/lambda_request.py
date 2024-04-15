# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
""" Async Lambda Client Utilities
"""
import json
import asyncio
from typing import TYPE_CHECKING, Any, Dict

# third-party imports from Lambda layer
from aws_lambda_powertools import Logger


LOGGER = Logger(child=True, location="%(filename)s:%(lineno)d - %(funcName)s()")


if TYPE_CHECKING:
    from mypy_boto3_lambda.type_defs import InvocationResponseTypeDef
    from mypy_boto3_lambda.client import LambdaClient
else:
    LambdaClient = object
    InvocationResponseTypeDef = object


async def invoke_lambda(
    payload: Dict[str, Any],
    lambda_client: LambdaClient,
    lambda_agent_assist_function_arn: str,
    max_retries: int = 3,
) -> InvocationResponseTypeDef:
    """Runs Lambda Invoke in the Async Event Loop"""
    # pylint: disable=too-many-arguments
    retry_count = 0
    lambda_responded: bool = False
    lambda_response: InvocationResponseTypeDef
    while not lambda_responded and retry_count < max_retries:
        try:
            event_loop = asyncio.get_event_loop()
            lambda_response = await event_loop.run_in_executor(
                None,
                lambda: lambda_client.invoke(
                    FunctionName=lambda_agent_assist_function_arn,
                    InvocationType='RequestResponse',
                    Payload = json.dumps(payload)
                ),
            )
            lambda_responded = True
        except lambda_client.exceptions.ResourceConflictException as error:
            retry_count = retry_count + 1
            LOGGER.warning(
                "invoke_lambda retriable exception",
                extra=dict(error=error, retry_count=retry_count),
            )
            await asyncio.sleep(0.25 * retry_count)
            if retry_count >= max_retries:
                raise
        except Exception:  # pylint: disable=broad-except
            LOGGER.exception("invoke_lambda")
            raise

    return lambda_response
