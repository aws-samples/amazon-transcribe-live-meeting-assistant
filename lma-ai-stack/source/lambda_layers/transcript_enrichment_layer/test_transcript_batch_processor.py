# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""What the batch processor records when a per-record mutation fails.

``TranscriptBatchProcessor.__aexit__`` logs the one line that names a failed
mutation, and that line is what an operator searching CloudWatch finds. Two
things about it are asserted here:

* It names the exception's type. A line carrying only ``str()`` of an exception
  reads identically for a transport failure and for a response the code could
  not use, which is not enough to act on.
* Where the exception carries GraphQL errors, the line carries their
  ``errorType`` and ``path`` rather than the exception's own text. A transport
  exception stringifies as its first GraphQL error message, and the mutations
  this processor sends inline their arguments into the query document, so a
  server-side validation message can quote meeting content.

Powertools and gql's HTTP transports are stubbed, so this runs wherever the
lint/test tooling is installed; the processor itself is the real one.

It sits at the layer root rather than beside the module it tests because
``transcript_batch_processor/__init__.py`` imports Powertools at import time, and
pytest imports a test file inside a package through that package's ``__init__``
-- before anything in the test file can install the stub.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Dict, List
from unittest import mock

import pytest


class RecordingLogger:
    """Stands in for aws_lambda_powertools.Logger, keeping what was logged."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
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


# Imported ahead of the stubs: mock.patch.dict restores sys.modules to what it
# held on entry, so a module imported inside the block is dropped and a second
# import of graphql-core would hand out a second GraphQLSchema class.
try:
    import gql.client  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover - depends on the environment
    pytest.skip(f"needs gql; {exc.name} is not installed.", allow_module_level=True)

# gql's HTTP transports pull in optional extras (aiohttp, requests_toolbelt)
# that the lint/test environment does not install, and the layer's appsync_utils
# imports both. Nothing here builds a client or opens a connection.
_STUBS: Dict[str, Any] = {
    "aws_lambda_powertools": mock.MagicMock(Logger=RecordingLogger),
    "aws_lambda_powertools.utilities": mock.MagicMock(),
    "aws_lambda_powertools.utilities.batch": mock.MagicMock(),
    "aws_lambda_powertools.utilities.data_classes": mock.MagicMock(),
    "aws_lambda_powertools.utilities.data_classes.kinesis_stream_event": mock.MagicMock(),
    "gql.transport.aiohttp": mock.MagicMock(),
    "gql.transport.requests": mock.MagicMock(),
}

with mock.patch.dict(sys.modules, _STUBS):
    import transcript_batch_processor.transcript_batch_processor as tbp  # noqa: E402


class FakeAppsyncClient:
    """Stands in for AppsyncAioGqlClient; yields a session nothing uses."""

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: Any) -> bool:
        return False


class TransportQueryError(Exception):
    """The shape gql raises for a response carrying GraphQL errors."""

    def __init__(self, errors: List[Dict[str, Any]]) -> None:
        super().__init__(str(errors[0]))
        self.errors = errors


MESSAGE_QUOTING_CONTENT = "rejected Transcript 'the figures we discussed'"


def processor_for(error: Exception) -> tbp.TranscriptBatchProcessor:
    """A processor whose single record's mutation raises ``error``."""

    async def failing_mutation(**_kwargs: Any) -> None:
        raise error

    instance = tbp.TranscriptBatchProcessor(
        appsync_client=FakeAppsyncClient(),
        api_mutation_fn=failing_mutation,
        sns_client=None,
        settings={},
    )
    instance._kds_processed_messages = [  # pylint: disable=protected-access
        {"status": "success", "result": {"CallId": "call-1"}}
    ]
    return instance


@pytest.fixture(autouse=True)
def empty_log():
    """Each test starts with an empty log."""
    tbp.LOGGER.records.clear()
    yield
    tbp.LOGGER.records.clear()


def failure_lines() -> List[str]:
    """The mutation-failure lines logged at error level."""
    return [
        record["message"]
        for record in tbp.LOGGER.records
        if record["level"] == "ERROR" and "api mutation exception" in record["message"]
    ]


def test_the_failure_line_names_the_exception_type():
    """A line carrying only str() reads the same for every kind of failure."""
    processor = processor_for(ValueError("invalid AppSync schema"))
    assert asyncio.new_event_loop().run_until_complete(processor.__aexit__(None, None, None))
    assert len(failure_lines()) == 1
    assert "ValueError" in failure_lines()[0]
    assert "invalid AppSync schema" in failure_lines()[0]


def test_the_failure_line_carries_error_types_not_the_graphql_message():
    """The exception's own text quotes the argument the server rejected."""
    error = TransportQueryError(
        [
            {
                "errorType": "ValidationError",
                "path": ["addTranscriptSegment"],
                "message": MESSAGE_QUOTING_CONTENT,
            }
        ]
    )
    # The guard this stands in for: the exception's own text does carry it.
    assert MESSAGE_QUOTING_CONTENT in str(error)

    processor = processor_for(error)
    assert asyncio.new_event_loop().run_until_complete(processor.__aexit__(None, None, None))
    line = failure_lines()[0]
    assert "TransportQueryError" in line
    assert "ValidationError" in line and "addTranscriptSegment" in line
    assert MESSAGE_QUOTING_CONTENT not in line
    assert "the figures we discussed" not in line


def test_the_failure_is_still_recorded_as_an_error():
    """The log line is a diagnostic; the record must still fail."""
    error = TransportQueryError([{"errorType": "ServiceUnavailable", "message": "later"}])
    processor = processor_for(error)
    asyncio.new_event_loop().run_until_complete(processor.__aexit__(None, None, None))
    assert processor.results["errors"] == [error]
    assert processor.results["successes"] == []
