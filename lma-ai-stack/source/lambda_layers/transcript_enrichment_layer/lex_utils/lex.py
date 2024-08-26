# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
""" Async Lex Client Utilities
"""
import asyncio
from typing import TYPE_CHECKING

# third-party imports from Lambda layer
from aws_lambda_powertools import Logger


LOGGER = Logger(
    child=True, location="%(filename)s:%(lineno)d - %(funcName)s()")


if TYPE_CHECKING:
    from mypy_boto3_lexv2_runtime.client import LexRuntimeV2Client
    from mypy_boto3_lexv2_runtime.type_defs import RecognizeTextResponseTypeDef
else:
    LexRuntimeV2Client = object
    RecognizeTextResponseTypeDef = object


def recognize_text_lex(
    text: str,
    session_id: str,
    lex_client: LexRuntimeV2Client,
    bot_id: str,
    bot_alias_id: str,
    locale_id: str,
    max_retries: int = 3,
    request_attributes: dict = None,
) -> RecognizeTextResponseTypeDef:
    """Runs Lex Recognize Text in the Async Event Loop"""
    # pylint: disable=too-many-arguments
    retry_count = 0
    bot_responded: bool = False
    bot_response: RecognizeTextResponseTypeDef

    while not bot_responded and retry_count < max_retries:
        try:
            # we do not set sessionAttributes here, since client is stateless and we want to
            # preserve Lex sessionAttribute state. Use requestAttributes for callId.
            bot_response = lex_client.recognize_text(
                text=text,
                sessionId=session_id,
                botId=bot_id,
                botAliasId=bot_alias_id,
                localeId=locale_id,
                requestAttributes={
                    "callId": request_attributes["callId"],
                    "idtokenjwt": request_attributes["idtokenjwt"],
                    "accesstokenjwt": request_attributes["accesstokenjwt"],
                    "refreshtoken": request_attributes["refreshtoken"]
                }
            )
            bot_responded = True
        except lex_client.exceptions.ConflictException as error:
            retry_count = retry_count + 1
            LOGGER.warning(
                "recognize_text retriable exception",
                extra=dict(error=error, retry_count=retry_count),
            )
            sleep(0.25 * retry_count)
            if retry_count >= max_retries:
                raise
        except Exception:  # pylint: disable=broad-except
            LOGGER.exception("recognize_text_lex")
            raise

    return bot_response
