# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
""""Async Query Execute"""
import asyncio
import logging
from random import randint
from typing import Callable, Dict, Optional, Union


from graphql import print_ast
from graphql.language.ast import DocumentNode
from gql.client import AsyncClientSession, ExecutionResult

LOGGER = logging.getLogger(__name__)
DEFAULT_IGNORED_EXCEPTION_RESPONSE: Dict[str, object] = {"ok": True}


async def execute_gql_query_with_retries(
    query: DocumentNode,
    client_session: AsyncClientSession,
    max_retries: int = 3,
    min_sleep_time: float = 0.750,
    logger: logging.Logger = LOGGER,
    should_ignore_exception_fn: Callable[[Exception], bool] = lambda _: False,
    ignored_exception_response: Optional[Dict[str, object]] = None,
) -> Union[Dict[str, object], ExecutionResult]:
    """Executes a query asynchronously with retries

    Implements retries using exponential backoff with jitter

    :param query: GraphQL query as AST Node object
    :param client_session: Asynchonous GraphQL client session

    :param max_retries: Number of times to retry appsync GraphQL queries
        after the initial query fails. This helps with async issues where
        mutations may occur out of order (e.g. transcript segment before a
        event has been processed)
    :param min_sleep_time: Minimum time in seconds to sleep between retries
        of a GraphQL query error. Uses exponential backoff with base 2
    :param logger: Logger
    :param should_ignore_exception_fn: Function that is called when there is an
        exception to verify it it should be ignored
    :param ignored_exception_response: Response to send when an exception has
        been ignored
    """
    # pylint: disable=too-many-arguments
    query_string = print_ast(query)
    _ignored_exception_response = (
        DEFAULT_IGNORED_EXCEPTION_RESPONSE
        if ignored_exception_response is None
        else ignored_exception_response
    )
    result: Union[Dict[str, object], ExecutionResult] = {}
    retries = 0
    while True:
        try:
            logger.debug(
                "executing query document - retry: [%d]",
                retries,
                extra=dict(query=query_string),
            )
            result = await client_session.execute(query)
            logger.debug(
                "query document retry: [%d] result - ",
                retries,
                extra=dict(result=result),
            )
            break
        except Exception as error:  # pylint: disable=broad-except
            if retries >= max_retries:
                logger.error(
                    "max retries on query - retries: [%d] - error: [%s]",
                    retries,
                    error,
                    extra=dict(query=query_string),
                )
                logger.exception("gql query exception")
                raise

            if should_ignore_exception_fn(error):
                logger.info("ignorable exception - not retrying - error: [%s]", error)
                result = _ignored_exception_response
                break

            retries = retries + 1
            # exponential backoff with jitter using base 2
            sleep_time = min_sleep_time * randint(1, 2**retries)  # nosec
            logger.warning(
                "error on query - retry: [%d] - sleeping for [%f]s - error: [%s]",
                retries,
                sleep_time,
                error,
                extra=dict(query=query_string),
            )
            await asyncio.sleep(sleep_time)

    return result
