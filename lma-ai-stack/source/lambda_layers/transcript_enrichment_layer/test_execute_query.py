# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""What the AppSync query retry wrapper records about a failed attempt.

``execute_gql_query_with_retries`` sits in front of every AppSync call the
transcript pipeline makes, so a write that fails goes through its retry warning
up to three times and then its give-up line. Three properties of those lines are
asserted:

* The error is reduced to ``errorType``/``path`` where the exception carries
  GraphQL errors. A transport exception stringifies as its first GraphQL error
  message, and these mutations inline their arguments into the query document.
* They carry no traceback. A rendered traceback ends in the exception's own text,
  so the chain of frames a traceback would be read for is logged instead,
  without the line it ends in.
* They carry no ``query`` field. The query document -- arguments and all -- is
  logged on the debug line for the attempt they are about, which is where it
  belongs; repeating it on a line that is on at the default level would put the
  arguments of every retried write into the log stream.

The assertions run against a real ``logging.Logger`` and format each record, so
an ``exc_info`` that a stub logger would quietly drop cannot slip past them.

The retry behaviour itself is asserted alongside: an error is retried up to
``max_retries`` and then re-raised, and an error the caller says to ignore stops
the retries and yields the ignored-exception response.

It sits at the layer root rather than beside the module it tests because
``appsync_utils/__init__.py`` imports gql's HTTP transports at import time --
whose optional extras the lint/test environment does not install -- and pytest
imports a test file inside a package through that package's ``__init__``, before
anything in the test file can install a stub.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any, Dict, List
from unittest import mock

import pytest

try:
    from gql.dsl import DSLQuery, DSLSchema, dsl_gql
    from gql.transport.exceptions import TransportQueryError
    from graphql import build_ast_schema, parse
except ModuleNotFoundError as exc:  # pragma: no cover - depends on the environment
    pytest.skip(
        f"needs gql and graphql-core; {exc.name} is not installed.", allow_module_level=True
    )

# gql's HTTP transports pull in optional extras (aiohttp, requests_toolbelt)
# that the lint/test environment does not install. Nothing here builds a client
# or opens a connection.
_STUBS: Dict[str, Any] = {
    "gql.transport.aiohttp": mock.MagicMock(),
    "gql.transport.requests": mock.MagicMock(),
}

with mock.patch.dict(sys.modules, _STUBS):
    from appsync_utils import execute_gql_query_with_retries  # noqa: E402

LOGGER = logging.getLogger("test_execute_query")

MESSAGE_QUOTING_CONTENT = "rejected Transcript 'the figures we discussed'"

SCHEMA = """
type Call { CallId: ID! Status: String }
type Query { getCall(CallId: ID!): Call }
"""


def transport_query_error(errors: List[Dict[str, Any]]) -> TransportQueryError:
    """gql's own exception, built the way gql builds it.

    ``AsyncClientSession.execute`` raises
    ``TransportQueryError(str_first_element(result.errors), errors=result.errors,
    ...)``, so its ``str()`` is the first error entry. The real class rather than
    a stand-in, so the assertions about keeping the exception's own text out of a
    log line hold against how gql actually stringifies it.
    """
    return TransportQueryError(str(errors[0]), errors=errors)


class FailingSession:
    """A gql session whose execute always raises."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.attempts = 0

    async def execute(self, _query: Any) -> Any:
        self.attempts += 1
        raise self.error


@pytest.fixture(scope="module")
def query():
    """A getCall query built by gql's DSL, as the pipeline builds its own."""
    schema = DSLSchema(build_ast_schema(parse(SCHEMA), assume_valid_sdl=True))
    return dsl_gql(DSLQuery(schema.Query.getCall.args(CallId="call-1").select(schema.Call.CallId)))


def run(coroutine):
    """Runs one coroutine to completion on a loop of its own, then closes it."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


def rendered(record: logging.LogRecord) -> str:
    """Everything a handler would write for a record, traceback included."""
    return logging.Formatter("%(levelname)s %(message)s").format(record)


def above_debug(records: List[logging.LogRecord]) -> List[logging.LogRecord]:
    """The records a default log level would emit."""
    return [record for record in records if record.levelno > logging.DEBUG]


def graphql_error() -> TransportQueryError:
    """A transport exception whose own text quotes a mutation argument."""
    error = transport_query_error(
        [
            {
                "errorType": "ValidationError",
                "path": ["getCall"],
                "message": MESSAGE_QUOTING_CONTENT,
            }
        ]
    )
    # The guard this stands in for: the exception's own text does carry it.
    assert MESSAGE_QUOTING_CONTENT in str(error)
    return error


def fail_query(query: Any, error: Exception, caplog) -> FailingSession:
    """Runs a query that always raises ``error``, capturing every log record."""
    session = FailingSession(error)
    with caplog.at_level(logging.DEBUG, logger=LOGGER.name):
        with pytest.raises(type(error)):
            run(
                execute_gql_query_with_retries(
                    query, client_session=session, logger=LOGGER, min_sleep_time=0
                )
            )
    return session


def test_the_retry_and_give_up_lines_carry_error_types_not_the_graphql_message(query, caplog):
    """Every record a default log level emits is read, tracebacks included."""
    session = fail_query(query, graphql_error(), caplog)
    assert session.attempts == 4, "three retries after the first attempt"
    emitted = above_debug(caplog.records)
    assert emitted, "a failing query must leave something in the log"
    whole_log = " ".join(rendered(record) for record in emitted)
    assert "ValidationError" in whole_log and "getCall" in whole_log
    assert MESSAGE_QUOTING_CONTENT not in whole_log
    assert "the figures we discussed" not in whole_log


def test_no_record_above_debug_carries_a_traceback(query, caplog):
    """A rendered traceback ends in the exception's own text."""
    fail_query(query, graphql_error(), caplog)
    for record in above_debug(caplog.records):
        assert record.exc_info is None, rendered(record)
        assert record.exc_text is None, record.exc_text
    # The frames a traceback would be read for are a field of their own instead,
    # under the same `frames` key the other failure lines on this path use.
    with_frames = [record for record in above_debug(caplog.records) if hasattr(record, "frames")]
    assert with_frames, "the give-up line must carry the frames"
    assert all(" in " in frame for record in with_frames for frame in record.frames)


def test_no_record_above_debug_carries_the_query_document(query, caplog):
    """The document holds the arguments, and a retried write logs several times."""
    fail_query(query, graphql_error(), caplog)
    for record in above_debug(caplog.records):
        assert not hasattr(record, "query"), rendered(record)
    # The document is still available to anyone who turns debug on.
    debug = [record for record in caplog.records if record.levelno == logging.DEBUG]
    assert any(hasattr(record, "query") for record in debug)


def test_an_exception_carrying_no_graphql_errors_still_reads_as_itself(query, caplog):
    """Reducing the error must not blank out an ordinary transport failure."""
    fail_query(query, ConnectionResetError("connection reset by peer"), caplog)
    whole_log = " ".join(rendered(record) for record in above_debug(caplog.records))
    assert "ConnectionResetError" in whole_log
    assert "connection reset by peer" in whole_log


def test_an_exception_with_no_text_at_all_is_still_named(query, caplog):
    """``str(TimeoutError())`` is empty, so the type is all the line can say."""
    fail_query(query, TimeoutError(), caplog)
    emitted = above_debug(caplog.records)
    assert emitted, "a failing query must leave something in the log"
    for record in emitted:
        assert "TimeoutError" in rendered(record), rendered(record)


def test_an_ignorable_error_stops_the_retries_and_answers_with_the_given_response(query):
    """The duplicate-write path: the item being stored is the state wanted."""
    session = FailingSession(transport_query_error([{"message": "item put condition failure"}]))
    result = run(
        execute_gql_query_with_retries(
            query,
            client_session=session,
            logger=LOGGER,
            min_sleep_time=0,
            should_ignore_exception_fn=lambda error: (
                "item put condition failure"
                in [entry["message"] for entry in getattr(error, "errors", [])]
            ),
            ignored_exception_response={"ok": True},
        )
    )
    assert result == {"ok": True}
    assert session.attempts == 1, "an ignored error is not retried"


def test_an_ignorable_error_is_recognised_on_the_last_attempt_too(query):
    """A write that timed out after storing its item sees this on a retry.

    That retry can be the final one, and the item being there is still the state
    the write was aiming for, so the budget running out must not turn it into a
    failure.
    """
    attempts = {"count": 0}

    class LateDuplicateSession:
        """Fails with something retryable until the final attempt."""

        async def execute(self, _query: Any) -> Any:
            attempts["count"] += 1
            if attempts["count"] <= 3:
                raise transport_query_error([{"errorType": "ServiceUnavailable"}])
            raise transport_query_error([{"message": "item put condition failure"}])

    result = run(
        execute_gql_query_with_retries(
            query,
            client_session=LateDuplicateSession(),
            logger=LOGGER,
            min_sleep_time=0,
            should_ignore_exception_fn=lambda error: (
                "item put condition failure"
                in [entry.get("message") for entry in getattr(error, "errors", [])]
            ),
            ignored_exception_response={"ok": True},
        )
    )
    assert attempts["count"] == 4, "the duplicate arrives on the final attempt"
    assert result == {"ok": True}
