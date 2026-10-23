# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

""""Async Query Execute"""
import asyncio
import logging
from random import randint
from typing import Callable, Dict, Optional, Union


from graphql import print_ast
from graphql.language.ast import DocumentNode
from gql.client import AsyncClientSession, ExecutionResult

# module imports from Lambda layer
# pylint: disable=import-error
from graphql_helpers import describe_exception, traceback_frames

# pylint: enable=import-error

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
        exception to verify whether it should be ignored. Asked on every attempt,
        before the retry budget is checked, so an error it recognises is
        recognised on the last attempt as well as the first. It is called from
        inside an ``except`` block and must not raise: an exception from it
        replaces the one being handled
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
            # See graphql_helpers.response's "Log hygiene" note for what
            # describe_exception leaves out and why. Local to these lines: the
            # query document stays on the debug line above, which is the attempt
            # they are about, because repeating it here would put the arguments
            # of every retried write into a log stream that is on by default.
            described_error = describe_exception(error)
            # Asked before the retry budget is checked, so an error the caller
            # recognises is recognised on the last attempt as well as the first.
            # A write that timed out after it had in fact stored its item sees a
            # condition failure on the retry, and that can be the final one.
            if should_ignore_exception_fn(error):
                logger.info("ignorable exception - not retrying - error: [%s]", described_error)
                result = _ignored_exception_response
                break

            if retries >= max_retries:
                # Frames rather than a rendered traceback, because a rendered
                # traceback ends in the exception's own text -- the same reason
                # the error is reduced above. One line rather than two, because
                # the exception is re-raised and the caller logs it again.
                logger.error(
                    "max retries on query - retries: [%d] - error: [%s]",
                    retries,
                    described_error,
                    extra=dict(frames=traceback_frames(error)),
                )
                raise

            retries = retries + 1
            # exponential backoff with jitter using base 2
            sleep_time = min_sleep_time * randint(1, 2**retries)  # nosec
            logger.warning(
                "error on query - retry: [%d] - sleeping for [%f]s - error: [%s]",
                retries,
                sleep_time,
                described_error,
            )
            await asyncio.sleep(sleep_time)

    return result
