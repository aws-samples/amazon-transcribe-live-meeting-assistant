# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""What the AppSync query retry wrapper records about a failed attempt.

``execute_gql_query_with_retries`` sits in front of every AppSync call the
transcript pipeline makes, so a write that fails goes through its retry warning
up to three times and then its give-up line. Two properties of those lines are
asserted:

* The error is reduced to ``errorType``/``path`` where the exception carries
  GraphQL errors. A transport exception stringifies as its first GraphQL error
  message, and these mutations inline their arguments into the query document.
* They carry no ``query`` field. The query document -- arguments and all -- is
  logged on the debug line for the attempt they are about, which is where it
  belongs; repeating it on a line that is on at the default level would put the
  arguments of every retried write into the log stream.

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
import sys
from typing import Any, Dict, List
from unittest import mock

import pytest

try:
    from gql.dsl import DSLQuery, DSLSchema, dsl_gql  # noqa: F401
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

MESSAGE_QUOTING_CONTENT = "rejected Transcript 'the figures we discussed'"

SCHEMA = """
type Call { CallId: ID! Status: String }
type Query { getCall(CallId: ID!): Call }
"""


class TransportQueryError(Exception):
    """The shape gql raises for a response carrying GraphQL errors."""

    def __init__(self, errors: List[Dict[str, Any]]) -> None:
        super().__init__(str(errors[0]))
        self.errors = errors


class RecordingLogger:
    """A logger that keeps every call, so a test can assert on the whole record."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def _record(self, level: str, message: str, *args: Any, **kwargs: Any) -> None:
        self.records.append(
            {"level": level, "message": message % args if args else message, "kwargs": kwargs}
        )

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._record("DEBUG", message, *args, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._record("INFO", message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._record("WARNING", message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._record("ERROR", message, *args, **kwargs)

    def exception(self, message: str, *args: Any, **kwargs: Any) -> None:
        self._record("EXCEPTION", message, *args, **kwargs)

    def above_debug(self) -> List[Dict[str, Any]]:
        """The records that a default log level would emit."""
        return [record for record in self.records if record["level"] != "DEBUG"]


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
    """Runs one coroutine to completion."""
    return asyncio.new_event_loop().run_until_complete(coroutine)


def graphql_error() -> TransportQueryError:
    """A transport exception whose own text quotes a mutation argument."""
    error = TransportQueryError(
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


def test_the_retry_and_give_up_lines_carry_error_types_not_the_graphql_message(query):
    """Every line a default log level emits is read for this."""
    logger = RecordingLogger()
    session = FailingSession(graphql_error())
    with pytest.raises(TransportQueryError):
        run(
            execute_gql_query_with_retries(
                query, client_session=session, logger=logger, min_sleep_time=0
            )
        )
    assert session.attempts == 4, "three retries after the first attempt"
    emitted = logger.above_debug()
    assert emitted, "a failing query must leave something in the log"
    rendered = " ".join(f"{record['message']} {record['kwargs']!r}" for record in emitted)
    assert "ValidationError" in rendered and "getCall" in rendered
    assert MESSAGE_QUOTING_CONTENT not in rendered
    assert "the figures we discussed" not in rendered


def test_no_line_above_debug_carries_the_query_document(query):
    """The document holds the arguments, and a retried write logs several times."""
    logger = RecordingLogger()
    session = FailingSession(graphql_error())
    with pytest.raises(TransportQueryError):
        run(
            execute_gql_query_with_retries(
                query, client_session=session, logger=logger, min_sleep_time=0
            )
        )
    for record in logger.above_debug():
        assert "query" not in (record["kwargs"].get("extra") or {}), record
    # The document is still available to anyone who turns debug on.
    debug = [record for record in logger.records if record["level"] == "DEBUG"]
    assert any("query" in (record["kwargs"].get("extra") or {}) for record in debug)


def test_an_exception_carrying_no_graphql_errors_still_reads_as_itself(query):
    """Reducing the error must not blank out an ordinary transport failure."""
    logger = RecordingLogger()
    session = FailingSession(ConnectionResetError("connection reset by peer"))
    with pytest.raises(ConnectionResetError):
        run(
            execute_gql_query_with_retries(
                query, client_session=session, logger=logger, min_sleep_time=0
            )
        )
    rendered = " ".join(record["message"] for record in logger.above_debug())
    assert "connection reset by peer" in rendered


def test_an_ignorable_error_stops_the_retries_and_answers_with_the_given_response(query):
    """The duplicate-write path: the item being stored is the state wanted."""
    logger = RecordingLogger()
    session = FailingSession(TransportQueryError([{"message": "item put condition failure"}]))
    result = run(
        execute_gql_query_with_retries(
            query,
            client_session=session,
            logger=logger,
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
