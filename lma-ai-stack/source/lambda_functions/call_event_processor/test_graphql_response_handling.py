#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""What the call event processor does with each shape AppSync can answer with.

``graphql_helpers/test_response.py`` covers the helpers on their own. These
tests drive the three functions in ``call_event_processor`` that read a field out
of a response, each with the real query built against
``source/appsync/schema.graphql`` and only the GraphQL transport replaced, so
what is under test is the decision each one makes:

* ``get_call_status`` is documented as fail-open -- an unknown status means the
  caller treats the meeting as still open -- so a response it cannot read must
  give back ``None``, not fail the record.
* ``get_aggregated_sentiment`` reads a list it can legitimately find empty, so a
  response without one aggregates nothing rather than failing the record that
  carried the transcript segment.
* ``get_call_details`` returns fields the caller has no substitute for, so a
  response without them fails the record. That is what gets it redelivered, and
  once the event source mapping's retries are spent its shard and sequence
  number are described to the on-failure destination; returning blanks would
  report success with the meeting half written.

None of them may raise ``AttributeError``: that names neither the field nor the
response, so a batch failing on one leaves nothing to act on.

Powertools and gql's two HTTP transport modules are stubbed, so this runs in the
environment the root Makefile's setup-python target builds. gql's client and DSL,
graphql-core and the layer are the real ones -- the queries and the response reads
are the pipeline's own.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest

HERE = Path(__file__).parent
# source/lambda_functions/call_event_processor -> source
SOURCE = HERE.parents[1]
LAYER = SOURCE / "lambda_layers" / "transcript_enrichment_layer"
SCHEMA_FILE = SOURCE / "appsync" / "schema.graphql"
for path in (str(HERE), str(LAYER)):
    if path not in sys.path:
        sys.path.insert(0, path)


class RecordingLogger:
    """Stands in for aws_lambda_powertools.Logger, keeping what was logged."""

    def __init__(self, *_args, **_kwargs) -> None:
        self.records: List[Dict[str, Any]] = []

    def _record(self, level: str, message: str, *args: Any, **kwargs: Any) -> None:
        # Every kwarg, not only `extra`: an `exc_info` dropped here would let a
        # traceback -- which ends in the exception's own text -- past the
        # assertions below unexamined.
        self.records.append(
            {
                "level": level,
                "message": message % args if args else message,
                "kwargs": kwargs,
            }
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

    def at_level(self, level: str) -> List[str]:
        """The messages logged at one level."""
        return [record["message"] for record in self.records if record["level"] == level]


# Each dotted name needs its own entry: `from a.b import c` imports a, then a.b,
# so a stub for `a` alone does not satisfy the later step.
#
# The two gql transport modules are stubbed rather than the packages behind them
# (aiohttp, requests_toolbelt), which are optional gql extras the lint/test
# environment does not install. Stubbing at the transport boundary keeps the list
# short and independent of which extras happen to be present: the layer's
# appsync_utils only needs the transport classes to exist, and nothing here
# builds a client or opens a connection.
_STUBS: Dict[str, Any] = {
    "aws_lambda_powertools": mock.MagicMock(Logger=RecordingLogger),
    "aws_lambda_powertools.utilities": mock.MagicMock(),
    "aws_lambda_powertools.utilities.typing": mock.MagicMock(),
    "gql.transport.aiohttp": mock.MagicMock(),
    "gql.transport.requests": mock.MagicMock(),
}

# Imported out here, ahead of the stubs, on purpose. ``mock.patch.dict`` restores
# sys.modules to the contents it had on entry, which drops everything imported
# inside the block -- and a second import of graphql-core would hand out a second
# ``GraphQLSchema`` class that gql's DSL then rejects. Anything the test and the
# module under test have to agree on is therefore imported first; only the module
# under test is imported with the stubs in place, so the stubs do not outlive this
# import and reach a sibling test file that wants the real Powertools.
try:
    from gql.transport.exceptions import TransportQueryError
    from graphql import build_ast_schema, parse
    from graphql_helpers.response import GraphQLResponseError
except ModuleNotFoundError as exc:  # pragma: no cover - depends on the environment
    pytest.skip(
        f"needs gql, graphql-core and the transcript enrichment layer; "
        f"{exc.name} is not installed.",
        allow_module_level=True,
    )

with mock.patch.dict(sys.modules, _STUBS):
    with mock.patch("boto3.Session", return_value=mock.MagicMock()):
        import event_processor.call_event_processor as cep  # noqa: E402

# AppSync provides these implicitly, so the committed SDL does not declare them
# and graphql-core cannot build it unaided. Same prelude as the layer's
# graphql_helpers tests, for the same reason.
APPSYNC_PRELUDE = """
scalar AWSDate
scalar AWSTime
scalar AWSDateTime
scalar AWSTimestamp
scalar AWSEmail
scalar AWSJSON
scalar AWSURL
scalar AWSPhone
scalar AWSIPAddress
directive @aws_subscribe(mutations: [String!]!) on FIELD_DEFINITION
directive @aws_auth(cognito_groups: [String!]) on FIELD_DEFINITION
directive @aws_cognito_user_pools(
    cognito_groups: [String!]
) on OBJECT | FIELD_DEFINITION
directive @aws_iam on OBJECT | FIELD_DEFINITION
directive @aws_api_key on OBJECT | FIELD_DEFINITION
directive @aws_oidc on OBJECT | FIELD_DEFINITION
directive @aws_lambda on OBJECT | FIELD_DEFINITION
"""


@pytest.fixture(scope="module")
def schema():
    """The schema the stack deploys, so the queries built here are the real ones."""
    assert SCHEMA_FILE.is_file(), f"schema not found at {SCHEMA_FILE}"
    return build_ast_schema(
        parse(APPSYNC_PRELUDE + SCHEMA_FILE.read_text(encoding="utf-8")),
        assume_valid_sdl=True,
    )


class FakeSession:
    """An AppSync session that answers with a queued response.

    Only ``execute`` is replaced: the query reaching it was built by the function
    under test against the real schema.
    """

    def __init__(self, schema: Any, response: Any) -> None:
        self.client = types.SimpleNamespace(schema=schema)
        self.response = response
        self.queries: List[Any] = []

    async def execute(self, query: Any) -> Any:
        self.queries.append(query)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture(autouse=True)
def quiet_logger():
    """Each test starts with an empty log."""
    cep.LOGGER.records.clear()
    yield
    cep.LOGGER.records.clear()


def run(coroutine):
    """Runs one coroutine to completion on a loop of its own, then closes it."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


# The response shapes AppSync can answer a getCall query with. "populated" is
# the ordinary one; the rest are what the reads have to survive.
GET_CALL_RESPONSES = {
    "populated": {
        "getCall": {
            "CallId": "call-1",
            "Status": "ENDED",
            "CustomerPhoneNumber": "+18005550000",
            "CallSummaryText": "a summary",
            "SharedWith": ["someone@example.com"],
        }
    },
    "null field": {"getCall": None},
    "errors and no data": {
        "errors": [{"errorType": "MappingTemplate", "path": ["getCall"], "message": "nope"}]
    },
    "none": None,
    "empty dict": {},
    "data without the field": {"getTranscriptSegments": {"TranscriptSegments": []}},
}

UNUSABLE_GET_CALL = [name for name in GET_CALL_RESPONSES if name != "populated"]

# A GraphQL error message that quotes the argument the server rejected, which is
# what these mutations inline into the query document.
MESSAGE_QUOTING_CONTENT = "rejected Transcript 'the figures we discussed'"


# ── get_call_status: fail open ──────────────────────────────────────────────


def test_get_call_status_returns_the_status_when_the_response_carries_it(schema):
    """The ordinary path, so the cases below are not passing for the wrong reason."""
    session = FakeSession(schema, GET_CALL_RESPONSES["populated"])
    status = run(cep.get_call_status(message={"CallId": "call-1"}, appsync_session=session))
    assert status == "ENDED"
    assert cep.LOGGER.at_level("WARNING") == []


@pytest.mark.parametrize("shape", UNUSABLE_GET_CALL)
def test_get_call_status_is_none_for_a_response_it_cannot_read(shape, schema):
    """Fail-open: an unreadable status leaves the meeting treated as still open."""
    session = FakeSession(schema, GET_CALL_RESPONSES[shape])
    status = run(cep.get_call_status(message={"CallId": "call-1"}, appsync_session=session))
    assert status is None


@pytest.mark.parametrize("shape", UNUSABLE_GET_CALL)
def test_get_call_status_logs_the_field_and_the_shape(shape, schema):
    """The log line is the only trace left, so it has to name both."""
    session = FakeSession(schema, GET_CALL_RESPONSES[shape])
    run(cep.get_call_status(message={"CallId": "call-1"}, appsync_session=session))
    warnings = cep.LOGGER.at_level("WARNING")
    assert len(warnings) == 1, f"expected one warning, got {warnings}"
    assert "getCall" in warnings[0]
    assert "dataShape" in warnings[0]


def test_get_call_status_records_a_raised_query_without_a_traceback(schema, monkeypatch):
    """Nothing above sees this exception, so the line is the whole record of it.

    The retry wrapper is replaced rather than driven, so the exception reaches
    ``get_call_status``'s own ``except`` without four attempts of real backoff.
    ``test_execute_query.py`` covers what the wrapper logs on the way.
    """
    error = TransportQueryError(
        "boom",
        errors=[{"errorType": "ServiceUnavailable", "message": MESSAGE_QUOTING_CONTENT}],
    )

    async def raising_execute(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(cep, "execute_gql_query_with_retries", raising_execute)
    session = FakeSession(schema, None)
    status = run(cep.get_call_status(message={"CallId": "call-1"}, appsync_session=session))
    assert status is None, "fail-open: an unreadable status is unknown"

    errors = cep.LOGGER.at_level("ERROR")
    assert len(errors) == 1, f"expected one error line, got {errors}"
    assert "ServiceUnavailable" in errors[0]
    assert "raised at" in errors[0]
    assert MESSAGE_QUOTING_CONTENT not in errors[0]
    # No traceback anywhere in the record: a rendered one ends in that text.
    assert cep.LOGGER.at_level("EXCEPTION") == []
    assert all(
        "exc_info" not in record["kwargs"]
        for record in cep.LOGGER.records
        if record["level"] == "ERROR"
    )


def test_get_call_status_does_not_log_a_transcript_or_an_error_message(schema):
    """A response's own error text can quote the argument the server rejected."""
    session = FakeSession(
        schema,
        {
            "errors": [
                {
                    "errorType": "MappingTemplate",
                    "message": "rejected Transcript 'the figures we discussed'",
                }
            ]
        },
    )
    run(cep.get_call_status(message={"CallId": "call-1"}, appsync_session=session))
    logged = " ".join(record["message"] for record in cep.LOGGER.records)
    assert "the figures we discussed" not in logged
    assert "MappingTemplate" in logged


# ── get_call_details: the record fails ──────────────────────────────────────


def test_get_call_details_returns_the_fields_when_the_response_carries_them(schema):
    """The ordinary path."""
    session = FakeSession(schema, GET_CALL_RESPONSES["populated"])
    details = run(cep.get_call_details(message={"CallId": "call-1"}, appsync_session=session))
    assert details["CallId"] == "call-1"
    assert details["SharedWith"] == ["someone@example.com"]
    assert details["CallSummaryText"] == "a summary"


@pytest.mark.parametrize("shape", UNUSABLE_GET_CALL)
def test_get_call_details_raises_a_named_error_rather_than_an_attribute_error(shape, schema):
    """The record has to fail so it is redelivered and finally preserved."""
    session = FakeSession(schema, GET_CALL_RESPONSES[shape])
    with pytest.raises(GraphQLResponseError) as raised:
        run(cep.get_call_details(message={"CallId": "call-1"}, appsync_session=session))
    assert not isinstance(raised.value, AttributeError)
    assert raised.value.path == ("getCall",)
    errors = cep.LOGGER.at_level("ERROR")
    assert len(errors) == 1, f"expected one error line, got {errors}"
    assert "getCall" in errors[0] and "retryable" in errors[0]


def test_get_call_details_marks_a_request_shape_error_as_permanent(schema):
    """A request the schema rejects will be rejected again on redelivery."""
    session = FakeSession(
        schema, {"errors": [{"errorType": "ValidationError", "message": "bad input"}]}
    )
    with pytest.raises(GraphQLResponseError) as raised:
        run(cep.get_call_details(message={"CallId": "call-1"}, appsync_session=session))
    assert raised.value.retryable is False


def test_get_call_details_marks_an_unrecognised_failure_as_retryable(schema):
    """Not retrying a transient failure costs a transcript segment."""
    session = FakeSession(schema, GET_CALL_RESPONSES["null field"])
    with pytest.raises(GraphQLResponseError) as raised:
        run(cep.get_call_details(message={"CallId": "call-1"}, appsync_session=session))
    assert raised.value.retryable is True


# ── get_aggregated_sentiment: an empty aggregation ──────────────────────────

SENTIMENT_RESPONSES = {
    "populated": {
        "getTranscriptSegmentsWithSentiment": {
            "TranscriptSegmentsWithSentiment": [
                {
                    "Channel": "CALLER",
                    "SegmentId": "segment-1",
                    "StartTime": 0.0,
                    "EndTime": 1.5,
                    "Sentiment": "POSITIVE",
                    "SentimentWeighted": 2.5,
                }
            ]
        }
    },
    "null wrapper": {"getTranscriptSegmentsWithSentiment": None},
    "null list": {"getTranscriptSegmentsWithSentiment": {"TranscriptSegmentsWithSentiment": None}},
    "errors and no data": {"errors": [{"errorType": "ServiceUnavailable", "message": "later"}]},
    "none": None,
    "empty dict": {},
    "data without the field": {"getCall": {"CallId": "call-1"}},
    "null entry in the list": {
        "getTranscriptSegmentsWithSentiment": {"TranscriptSegmentsWithSentiment": [None]}
    },
}

EMPTY_SENTIMENT = [name for name in SENTIMENT_RESPONSES if name != "populated"]


def test_aggregated_sentiment_scores_the_segments_the_response_carries(schema):
    """The ordinary path: a scored segment reaches the aggregation."""
    session = FakeSession(schema, SENTIMENT_RESPONSES["populated"])
    aggregated = run(
        cep.get_aggregated_sentiment(message={"CallId": "call-1"}, appsync_session=session)
    )
    assert aggregated["OverallSentiment"]["CALLER"] == 2.5


@pytest.mark.parametrize("shape", EMPTY_SENTIMENT)
def test_aggregated_sentiment_is_empty_for_a_response_without_segments(shape, schema):
    """No segments to score is a valid answer; failing here fails the record."""
    session = FakeSession(schema, SENTIMENT_RESPONSES[shape])
    aggregated = run(
        cep.get_aggregated_sentiment(message={"CallId": "call-1"}, appsync_session=session)
    )
    assert aggregated["OverallSentiment"] == {}
    assert aggregated["SentimentByPeriod"] == {"QUARTER": {}}


# ── recognising a duplicate write ───────────────────────────────────────────


def test_a_duplicate_write_is_recognised_from_the_exception_gql_raises():
    """gql keeps the GraphQL errors on an attribute of the exception, not a key."""
    error = TransportQueryError(
        "item put condition failure",
        errors=[{"message": "item put condition failure", "errorType": "DynamoDB"}],
    )
    assert cep.is_put_condition_failure(error) is True


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("connection reset"),
        None,
        {"errors": [{"errorType": "Unauthorized"}]},
    ],
)
def test_any_other_failure_is_not_taken_for_a_duplicate_write(error):
    """Anything unrecognised must stay a failure, so it is retried and reported."""
    assert cep.is_put_condition_failure(error) is False
