# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""What the batch processor records when a per-record mutation fails.

``TranscriptBatchProcessor.__aexit__`` logs the one line that names a failed
mutation, and that line is what an operator searching CloudWatch finds. Two
things about it are asserted here:

* It names the exception's type and the whole chain of frames from its
  traceback. A line carrying only ``str()`` of an exception reads identically
  for a transport failure and for a response the code could not use, which is
  not enough to act on; the innermost frame on its own names only the helper
  that raised, which on the real paths is gql or ``require_graphql_field``.
* Where the exception carries GraphQL errors, the whole record carries their
  ``errorType`` and ``path`` and not the exception's own text -- not in the
  message, not in ``extra``, and not as a traceback, since a rendered traceback
  ends in that text. A transport exception stringifies as its first GraphQL
  error message, and the mutations this processor sends inline their arguments
  into the query document.

Powertools and gql's HTTP transports are stubbed, so the only runtime
dependency left is gql, which ``make setup-cli-dev`` installs with
``lib/lma_sdk[dev]``; the processor itself is the real one.

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
    """A processor whose single record's mutation raises ``error``.

    The mutation raises through a nested helper and re-raises with a bare
    ``raise``, which is the shape the real paths have: a transport exception is
    first raised inside gql and re-raised by the retry wrapper, and a
    ``GraphQLResponseError`` is raised inside ``require_graphql_field``. A
    mutation that raised directly would make a one-frame record look sufficient.
    """

    def raise_it() -> None:
        raise error

    async def failing_mutation(**_kwargs: Any) -> None:
        try:
            raise_it()
        except Exception:
            raise

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


def failure_records() -> List[Dict[str, Any]]:
    """The mutation-failure records logged at error level."""
    return [
        record
        for record in tbp.LOGGER.records
        if record["level"] == "ERROR" and "api mutation exception" in record["message"]
    ]


def exit_processor(processor: Any) -> Any:
    """Runs ``__aexit__`` on a loop of its own, then closes it."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(processor.__aexit__(None, None, None))
    finally:
        loop.close()


def rendered(record: Dict[str, Any]) -> str:
    """Everything the record would put in a log stream, message and kwargs.

    `exc_info` and `extra` both land in the kwargs, so neither a traceback nor a
    field can slip past an assertion that only reads the message.
    """
    return f"{record['message']} {record['kwargs']!r}"


def test_the_failure_record_names_the_exception_type_and_the_frame_chain():
    """A line carrying only str() reads the same for every kind of failure."""
    processor = processor_for(ValueError("invalid AppSync schema"))
    assert exit_processor(processor)
    assert len(failure_records()) == 1
    record = failure_records()[0]
    assert "ValueError" in record["message"]
    assert "invalid AppSync schema" in record["message"]
    # The chain, so the record says which of a record's mutations this was --
    # the innermost frame alone would name only the helper that raised.
    frames = record["kwargs"]["extra"]["frames"]
    assert any("in failing_mutation" in frame for frame in frames), frames
    assert any("in raise_it" in frame for frame in frames), frames
    assert all(f"{__file__}:" in frame for frame in frames)


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
    assert exit_processor(processor)
    assert len(failure_records()) == 1
    whole_record = rendered(failure_records()[0])
    assert "TransportQueryError" in whole_record
    assert "ValidationError" in whole_record and "addTranscriptSegment" in whole_record
    assert MESSAGE_QUOTING_CONTENT not in whole_record
    assert "the figures we discussed" not in whole_record
    # No traceback anywhere in the record: a rendered one ends in that text.
    assert "exc_info" not in failure_records()[0]["kwargs"]


def test_the_failure_is_still_recorded_as_an_error():
    """The log line is a diagnostic; the record must still fail."""
    error = TransportQueryError([{"errorType": "ServiceUnavailable", "message": "later"}])
    processor = processor_for(error)
    exit_processor(processor)
    assert processor.results["errors"] == [error]
    assert processor.results["successes"] == []
