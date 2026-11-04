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

Powertools and gql's two HTTP transport modules are stubbed, so the only runtime
dependencies this needs are the ones the root Makefile's ``setup-python`` and
``setup-cli-dev`` targets install between them -- gql and graphql-core come from
``lib/lma_sdk[dev]``, so ``setup-python`` alone leaves this skipping. gql's
client and DSL, graphql-core and the layer are the real ones: the queries and
the response reads are the pipeline's own.
"""

from __future__ import annotations

import asyncio
import json
import re
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
CONFIG_LAYER = SOURCE / "lambda_layers" / "configuration_layer"
SCHEMA_FILE = SOURCE / "appsync" / "schema.graphql"
for path in (str(HERE), str(LAYER), str(CONFIG_LAYER)):
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
    # The configuration accessor, likewise: a test that stands in for the store
    # it reads settings through has to reach the module the processor uses.
    import lma_config.parameters  # noqa: F401
    from gql.transport.exceptions import TransportQueryError
    from graphql import build_ast_schema, parse
    from graphql.language.printer import print_ast
    from graphql_helpers.response import GraphQLResponseError

    # The metric catalogue, imported here for the same reason as the others: the
    # module under test must get this exact module object, so that the names the
    # tests assert on are the names it emits.
    from metrics_utils import catalogue as metric_names
except ModuleNotFoundError as exc:  # pragma: no cover - depends on the environment
    pytest.skip(
        f"needs gql, graphql-core and the transcript enrichment layer; "
        f"{exc.name} is not installed.",
        allow_module_level=True,
    )

with mock.patch.dict(sys.modules, _STUBS):
    with mock.patch("boto3.Session", return_value=mock.MagicMock()):
        import event_processor.call_event_processor as cep  # noqa: E402

        # In here because its package imports gql's HTTP transports, the ones the
        # stubs stand in for.
        from appsync_utils import execute_gql_query_with_retries  # noqa: E402

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
            "SharedWith": "someone@example.com",
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

    records = [record for record in cep.LOGGER.records if record["level"] == "ERROR"]
    assert len(records) == 1, f"expected one error record, got {records}"
    assert "ServiceUnavailable" in records[0]["message"]
    assert MESSAGE_QUOTING_CONTENT not in records[0]["message"]
    # The frames, so the line says where the read failed.
    frames = records[0]["kwargs"]["extra"]["frames"]
    assert any("in raising_execute" in frame for frame in frames), frames
    # No traceback anywhere in the record: a rendered one ends in that text.
    assert cep.LOGGER.at_level("EXCEPTION") == []
    assert all("exc_info" not in record["kwargs"] for record in records)


def test_get_call_status_does_not_log_a_transcript_or_an_error_message(schema):
    """A response's own error text can quote the argument the server rejected.

    Every record a default log level would emit, message and kwargs both. The
    debug records are excluded on purpose and not by oversight: the layer's query
    wrapper logs the whole response under ``extra`` at debug, which is the
    affordance that makes a payload inspectable when somebody turns the level up.
    """
    session = FakeSession(
        schema,
        {
            "errors": [
                {"errorType": "MappingTemplate", "message": MESSAGE_QUOTING_CONTENT},
            ]
        },
    )
    run(cep.get_call_status(message={"CallId": "call-1"}, appsync_session=session))
    logged = " ".join(
        f"{record['message']} {record['kwargs']!r}"
        for record in cep.LOGGER.records
        if record["level"] != "DEBUG"
    )
    assert MESSAGE_QUOTING_CONTENT not in logged
    assert "MappingTemplate" in logged


# ── get_call_details: the record fails ──────────────────────────────────────


def test_get_call_details_returns_the_fields_when_the_response_carries_them(schema):
    """The ordinary path."""
    session = FakeSession(schema, GET_CALL_RESPONSES["populated"])
    details = run(cep.get_call_details(message={"CallId": "call-1"}, appsync_session=session))
    assert details["CallId"] == "call-1"
    assert details["SharedWith"] == "someone@example.com"
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
    assert "getCall" in errors[0] and "looksTransient" in errors[0]


def test_get_call_details_marks_a_request_shape_error_as_permanent(schema):
    """Covers the classification, not a shape this session can deliver.

    gql raises ``TransportQueryError`` for a response carrying ``errors`` before
    a caller holds it, so this arrives at ``get_call_details`` only through a
    caller holding an ``ExecutionResult`` or a raw envelope. The classification
    is informational either way -- see ``is_retryable_graphql_response`` -- and
    this asserts it reaches the exception rather than that anything acts on it.
    """
    session = FakeSession(
        schema, {"errors": [{"errorType": "ValidationError", "message": "bad input"}]}
    )
    with pytest.raises(GraphQLResponseError) as raised:
        run(cep.get_call_details(message={"CallId": "call-1"}, appsync_session=session))
    assert raised.value.looks_transient is False


def test_get_call_details_marks_an_unrecognised_failure_as_retryable(schema):
    """Not retrying a transient failure costs a transcript segment."""
    session = FakeSession(schema, GET_CALL_RESPONSES["null field"])
    with pytest.raises(GraphQLResponseError) as raised:
        run(cep.get_call_details(message={"CallId": "call-1"}, appsync_session=session))
    assert raised.value.looks_transient is True


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
    "empty list": {"getTranscriptSegmentsWithSentiment": {"TranscriptSegmentsWithSentiment": []}},
}

# A list that came back, holding nothing worth scoring. An empty aggregation is
# the right answer and writing it is right.
SCORES_NOTHING = ["empty list", "null entry in the list"]

# No list came back at all. That is not an answer, and writing an empty
# aggregation for it would replace the stored one.
NO_SEGMENT_LIST = [
    name for name in SENTIMENT_RESPONSES if name not in {"populated", *SCORES_NOTHING}
]


def test_aggregated_sentiment_scores_the_segments_the_response_carries(schema):
    """The ordinary path: a scored segment reaches the aggregation."""
    session = FakeSession(schema, SENTIMENT_RESPONSES["populated"])
    aggregated = run(
        cep.get_aggregated_sentiment(message={"CallId": "call-1"}, appsync_session=session)
    )
    # The segment is stored under CALLER, as a meeting from before 0.4.0 is; the
    # aggregation is written under its current name.
    assert aggregated["OverallSentiment"] == {"REMOTE": 2.5}


def test_aggregated_sentiment_merges_both_spellings_of_a_channel(schema):
    """A meeting recorded across an upgrade holds CALLER and REMOTE segments of one channel."""

    def scored(channel, segment_id, start, score):
        return {
            "Channel": channel,
            "SegmentId": segment_id,
            "StartTime": start,
            "EndTime": start + 1.0,
            "Sentiment": "POSITIVE",
            "SentimentWeighted": score,
        }

    response = {
        "getTranscriptSegmentsWithSentiment": {
            "TranscriptSegmentsWithSentiment": [
                scored("CALLER", "s1", 0.0, 1.0),
                scored("REMOTE", "s2", 2.0, 3.0),
                scored("AGENT", "s3", 4.0, -1.0),
                scored("LOCAL", "s4", 6.0, -3.0),
                scored("AGENT_ASSISTANT", "s5", 8.0, 5.0),
            ]
        }
    }
    aggregated = run(
        cep.get_aggregated_sentiment(
            message={"CallId": "call-1"}, appsync_session=FakeSession(schema, response)
        )
    )
    assert aggregated["OverallSentiment"] == {"REMOTE": 2.0, "LOCAL": -2.0}
    assert set(aggregated["SentimentByPeriod"]["QUARTER"]) == {"REMOTE", "LOCAL"}


def test_a_transcript_hook_returning_an_older_channel_name_is_written_as_the_current_one(
    monkeypatch,
):
    """A hook written for an earlier version may hand back the segment with CALLER."""

    class Payload:
        def read(self):
            return json.dumps(
                {"Channel": "CALLER", "Transcript": "hi", "IsPartial": False}
            ).encode()

    class HookClient:
        def invoke(self, **_kwargs):
            return {"Payload": Payload()}

    monkeypatch.setattr(cep, "LAMBDA_HOOK_CLIENT", HookClient(), raising=False)
    monkeypatch.setattr(cep, "TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN", "arn:hook", raising=False)
    result = cep.invoke_transcript_lambda_hook(
        {"Channel": "REMOTE", "Transcript": "hi", "IsPartial": False}
    )
    assert result["Channel"] == "REMOTE"


@pytest.mark.parametrize("shape", SCORES_NOTHING)
def test_an_empty_segment_list_aggregates_to_an_empty_aggregation(shape, schema):
    """A list with nothing to score is a valid answer, and writing it is right."""
    session = FakeSession(schema, SENTIMENT_RESPONSES[shape])
    aggregated = run(
        cep.get_aggregated_sentiment(message={"CallId": "call-1"}, appsync_session=session)
    )
    assert aggregated == {"OverallSentiment": {}, "SentimentByPeriod": {"QUARTER": {}}}


@pytest.mark.parametrize("shape", NO_SEGMENT_LIST)
def test_a_response_carrying_no_segment_list_aggregates_nothing_at_all(shape, schema):
    """Not an answer, so there is nothing to write -- and no failure either.

    An empty aggregation written with a fresh ``UpdatedAt`` passes
    ``updateCall.request.vtl``'s condition, so it would replace the stored one.
    Failing instead would fail the record that carried the transcript segment.
    """
    session = FakeSession(schema, SENTIMENT_RESPONSES[shape])
    assert (
        run(cep.get_aggregated_sentiment(message={"CallId": "call-1"}, appsync_session=session))
        is None
    )


@pytest.mark.parametrize("shape", NO_SEGMENT_LIST)
def test_no_aggregation_is_sent_when_no_segment_list_came_back(shape, schema):
    """The decision has to reach the writes, not just the aggregation."""
    session = FakeSession(schema, SENTIMENT_RESPONSES[shape])
    assert (
        run(
            cep.get_call_aggregation_tasks(
                message={"CallId": "call-1", "EndTime": 1.0}, appsync_session=session
            )
        )
        == []
    )


@pytest.mark.parametrize(
    "error",
    [
        # What gql actually does with a response carrying errors: it raises,
        # rather than handing back a dict with no data. The "errors and no data"
        # cases above cover the shape; this covers the delivery.
        TransportQueryError(
            "Unauthorized", errors=[{"errorType": "Unauthorized", "message": "nope"}]
        ),
        ConnectionResetError("connection reset by peer"),
        TimeoutError(),
    ],
    ids=["transport query error", "connection reset", "timeout"],
)
def test_a_sentiment_read_that_raises_aggregates_nothing_and_keeps_the_record(
    error, schema, monkeypatch
):
    """The transcript segment's own write is gathered after this read.

    Letting the exception out would fail the record and take the segment with it.
    The aggregation is recomputed on the next final segment, so it is the cheaper
    thing to lose; ``get_call_details`` reads the same meeting earlier in the
    record and would already have failed it if AppSync were unreachable.

    The retry wrapper is replaced rather than driven, so the exception reaches
    the branch under test without four attempts of real backoff.
    ``test_execute_query.py`` covers what the wrapper does on the way.
    """

    async def raising_execute(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(cep, "execute_gql_query_with_retries", raising_execute)
    session = FakeSession(schema, None)
    assert (
        run(cep.get_aggregated_sentiment(message={"CallId": "call-1"}, appsync_session=session))
        is None
    )
    assert (
        run(
            cep.get_call_aggregation_tasks(
                message={"CallId": "call-1", "EndTime": 1.0}, appsync_session=session
            )
        )
        == []
    )
    errors = [record for record in cep.LOGGER.records if record["level"] == "ERROR"]
    assert errors, "the read that failed has to leave a line behind"
    assert any("aggregating nothing" in record["message"] for record in errors), errors
    assert all("nope" not in record["message"] for record in errors), errors


@pytest.mark.parametrize("shape", ["populated", *SCORES_NOTHING])
def test_an_aggregation_is_sent_when_a_segment_list_came_back(shape, schema):
    """Including an empty one, which is a real answer about the meeting."""
    session = FakeSession(schema, SENTIMENT_RESPONSES[shape])
    tasks = run(
        cep.get_call_aggregation_tasks(
            message={"CallId": "call-1", "EndTime": 1.0}, appsync_session=session
        )
    )
    assert len(tasks) == 1
    for task in tasks:
        task.close()


# ── recognising a duplicate write ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("message", "resolver"),
    [
        # dDbPutCondition.response.vtl, shared by the segment and update writes.
        ("item put condition failure", "dDbPutCondition.response.vtl"),
        # createCall.response.vtl, which writes transactionally and has its own.
        ("Item already exists", "createCall.response.vtl"),
    ],
)
def test_a_rejected_write_is_recognised_from_the_exception_gql_raises(message, resolver):
    """Both messages the resolvers raise, read off the exception's attribute.

    ``createCall`` does not share the response template the others do, so
    matching only the shared message would leave a redelivered START event
    retrying and then failing.
    """
    error = TransportQueryError(message, errors=[{"message": message, "errorType": "DynamoDB"}])
    assert cep.is_put_condition_failure(error) is True, resolver


def test_both_resolver_messages_are_the_ones_the_templates_raise():
    """Pins the constant to the resolvers, which is where the strings come from."""
    appsync = SOURCE / "appsync"
    for name, expected in (
        ("dDbPutCondition.response.vtl", "item put condition failure"),
        ("createCall.response.vtl", "Item already exists"),
    ):
        template = (appsync / name).read_text(encoding="utf-8")
        assert f'$util.error("{expected}")' in template, name
        assert expected in cep.PUT_CONDITION_FAILURE_MESSAGES


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


@pytest.mark.parametrize("attempt_of_the_duplicate", [1, 4])
def test_the_real_predicate_in_the_real_retry_loop_accepts_a_duplicate(
    attempt_of_the_duplicate, schema
):
    """The whole path: ``is_put_condition_failure`` inside the retry wrapper.

    The two cases are the duplicate arriving first and the duplicate arriving on
    the final attempt, after the retry budget is spent -- that second one is
    reachable when a write times out after it had in fact stored its item.
    """
    attempts = {"count": 0}
    duplicate = TransportQueryError(
        "item put condition failure",
        errors=[{"message": "item put condition failure", "errorType": "DynamoDB"}],
    )
    transient = TransportQueryError(
        "service unavailable", errors=[{"errorType": "ServiceUnavailable"}]
    )

    class DuplicateOnAttemptSession:
        """Fails transiently until the attempt that answers with the duplicate."""

        def __init__(self, schema: Any) -> None:
            self.client = types.SimpleNamespace(schema=schema)

        async def execute(self, _query: Any) -> Any:
            attempts["count"] += 1
            raise duplicate if attempts["count"] >= attempt_of_the_duplicate else transient

    result = run(
        execute_gql_query_with_retries(
            cep.dsl_gql(
                cep.DSLMutation(
                    cep.DSLSchema(schema)
                    .Mutation.addTranscriptSegment.args(
                        input={
                            "CallId": "call-1",
                            "SegmentId": "segment-1",
                            "StartTime": 0.0,
                            "EndTime": 1.0,
                            "Transcript": "hello there",
                            "IsPartial": False,
                            "Status": "TRANSCRIBING",
                        }
                    )
                    .select(cep.DSLSchema(schema).TranscriptSegment.CallId)
                )
            ),
            client_session=DuplicateOnAttemptSession(schema),
            logger=cep.LOGGER,
            min_sleep_time=0,
            should_ignore_exception_fn=cep.is_put_condition_failure,
        )
    )
    assert attempts["count"] == attempt_of_the_duplicate
    assert result == {"ok": True}, "a stored item is the state the write wanted"


# ── the start-of-call hook runs once per meeting ─────────────────────────────


def process_start_event(schema: Any, create_call_response: Any, monkeypatch) -> List[Dict]:
    """Runs a START event and returns the start-of-call hook's invocations."""
    invocations: List[Dict] = []

    class RecordingLambdaClient:
        """Captures invoke calls instead of making them."""

        def invoke(self, **kwargs: Any) -> Dict:
            invocations.append(kwargs)
            return {"StatusCode": 202}

    monkeypatch.setattr(cep, "START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN", "arn:hook", raising=False)
    monkeypatch.setattr(cep, "LAMBDA_HOOK_CLIENT", RecordingLambdaClient(), raising=False)
    monkeypatch.setattr(cep, "SETTINGS", {}, raising=False)
    run(
        cep.execute_process_event_api_mutation(
            message={"EventType": "START", "CallId": "call-1", "AgentId": "someone@example.com"},
            appsync_session=FakeSession(schema, create_call_response),
            agent_assist_args={},
            sentiment_analysis_args={},
        )
    )
    return invocations


def test_a_first_start_event_invokes_the_start_of_call_hook(schema, monkeypatch):
    """The ordinary path, so the case below is not passing for the wrong reason."""
    created = {"createCall": {"CallId": "call-1", "Owner": "someone@example.com"}}
    assert len(process_start_event(schema, created, monkeypatch)) == 1


def test_a_redelivered_start_event_does_not_invoke_the_hook_again(schema, monkeypatch):
    """A deployment's own hook would read a second invocation as a second meeting.

    Redelivery is routine here: the event source mapping reports partial batch
    failures, so every record from a failing one onwards comes back.
    """
    already_created = TransportQueryError(
        "Item already exists",
        errors=[{"message": "Item already exists", "errorType": "DynamoDB"}],
    )
    assert process_start_event(schema, already_created, monkeypatch) == []
    info = [record["message"] for record in cep.LOGGER.records if record["level"] == "INFO"]
    assert any("already created" in message for message in info), info


def test_only_the_already_created_marker_counts_as_already_created():
    """Any other createCall result must leave the hook alone to run."""
    assert cep.was_already_created(cep.CALL_ALREADY_CREATED) is True
    assert cep.was_already_created({"ok": True}) is False
    assert cep.was_already_created({"createCall": {"CallId": "call-1"}}) is False
    assert cep.was_already_created(None) is False


# ── what the application metrics count ──────────────────────────────────────
#
# These drive the real dispatch rather than calling the emitter directly, since
# the thing that can be wrong is *when* a metric is counted, not how a record is
# written -- metrics_utils/test_emf.py covers the record. Two kinds of mistake
# are in range here and neither shows up anywhere else: counting a redelivered
# event as a second meeting, which the Kinesis event source makes routine; and
# counting a write that failed, which would make the meeting count disagree with
# the meetings that exist while looking healthier than the truth.


class QueuedSession:
    """An AppSync session that answers each ``execute`` from a queue.

    ``FakeSession`` answers everything with one response, which is enough for a
    single-mutation path. The transcript-segment and END paths issue several
    queries in a fixed order, and what is being asserted is the difference
    between an earlier one succeeding and a later one failing.
    """

    def __init__(self, schema: Any, responses: List[Any]) -> None:
        self.client = types.SimpleNamespace(schema=schema)
        self.responses = list(responses)
        self.queries: List[Any] = []

    async def execute(self, query: Any) -> Any:
        self.queries.append(query)
        # The last response repeats, so a path that fans out to an unknown
        # number of identical mutations does not need them all written out.
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response


class RecordingEmitter:
    """Collects metric calls in place of writing records."""

    def __init__(self) -> None:
        self.counts: Dict[str, float] = {}
        self.measurements: Dict[str, List[float]] = {}

    @property
    def enabled(self) -> bool:
        return True

    def count(self, name: str, value: float = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + value

    def measure(self, name: str, value: float) -> None:
        self.measurements.setdefault(name, []).append(value)

    def flush(self) -> None:
        pass


@pytest.fixture
def emitted(monkeypatch) -> RecordingEmitter:
    recorder = RecordingEmitter()
    monkeypatch.setattr(cep, "METRICS", recorder, raising=True)
    return recorder


def process_event(session: Any, message: Dict[str, Any], monkeypatch) -> None:
    """Runs one event through the real dispatch with the hooks switched off.

    The retry backoff is made instant. The cases below that assert on a *failed*
    write go through `execute_gql_query_with_retries`, whose default backoff
    sleeps for real -- three attempts at a 0.75-second base cost about six
    seconds per test, and these tests are about which metric is counted rather
    than about how long a retry waits. The retries themselves still happen, so
    the path under test is unchanged.
    """

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant)
    monkeypatch.setattr(cep, "START_OF_CALL_LAMBDA_HOOK_FUNCTION_ARN", "", raising=False)
    monkeypatch.setattr(cep, "TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN", "", raising=False)
    monkeypatch.setattr(cep, "SPEAKER_NAME_RESOLVER_ARN", "", raising=False)
    monkeypatch.setattr(cep, "IS_TRANSCRIPT_SUMMARY_ENABLED", False, raising=False)
    monkeypatch.setattr(cep, "IS_SENTIMENT_ANALYSIS_ENABLED", False, raising=False)
    monkeypatch.setattr(cep, "IS_LAMBDA_AGENT_ASSIST_ENABLED", False, raising=False)
    monkeypatch.setattr(cep, "SETTINGS", {}, raising=False)
    run(
        cep.execute_process_event_api_mutation(
            message=message,
            appsync_session=session,
            agent_assist_args={},
            sentiment_analysis_args={},
        )
    )


def test_a_first_start_event_counts_one_meeting_started(schema, emitted, monkeypatch):
    created = {"createCall": {"CallId": "call-1", "Owner": "someone@example.com"}}
    process_event(
        FakeSession(schema, created),
        {"EventType": "START", "CallId": "call-1", "AgentId": "someone@example.com"},
        monkeypatch,
    )
    assert emitted.counts.get(metric_names.MEETINGS_STARTED) == 1


def test_a_start_event_creates_the_meeting_with_the_agenda_it_carries(schema, emitted, monkeypatch):
    """A Virtual Participant started with an agenda sends it in START, so the
    meeting has it from creation rather than from a second write racing it.

    Nothing in the processor names the fields: createCall's input is the
    message itself, and gql keeps only the keys CreateCallInput declares, so
    the fields reach the resolver because the input type declares them."""
    created = {"createCall": {"CallId": "call-1", "Owner": "someone@example.com"}}
    session = FakeSession(schema, created)
    process_event(
        session,
        {
            "EventType": "START",
            "CallId": "call-1",
            "AgentId": "someone@example.com",
            "Agenda": "Budget\nHiring",
            "AgendaSource": "scheduled",
            "AgendaUpdatedBy": "someone@example.com",
            "AgendaUpdatedAt": "2026-10-31T10:00:00.000Z",
        },
        monkeypatch,
    )
    sent = print_ast(session.queries[0])
    assert 'Agenda: "Budget\\nHiring"' in sent
    assert 'AgendaSource: "scheduled"' in sent
    assert 'AgendaUpdatedBy: "someone@example.com"' in sent
    assert 'AgendaUpdatedAt: "2026-10-31T10:00:00.000Z"' in sent


def test_a_start_event_without_an_agenda_creates_the_meeting_without_one(
    schema, emitted, monkeypatch
):
    created = {"createCall": {"CallId": "call-1", "Owner": "someone@example.com"}}
    session = FakeSession(schema, created)
    process_event(
        session,
        {"EventType": "START", "CallId": "call-1", "AgentId": "someone@example.com"},
        monkeypatch,
    )
    assert "Agenda" not in print_ast(session.queries[0])


def test_a_redelivered_start_event_counts_no_meeting_started(schema, emitted, monkeypatch):
    """Otherwise the meeting count measures stream redelivery, not meetings.

    The same condition the start-of-call hook uses, deliberately: if the two
    ever disagree, one of them is wrong about whether a meeting began.
    """
    already_created = TransportQueryError(
        "Item already exists",
        errors=[{"message": "Item already exists", "errorType": "DynamoDB"}],
    )
    process_event(
        FakeSession(schema, already_created),
        {"EventType": "START", "CallId": "call-1", "AgentId": "someone@example.com"},
        monkeypatch,
    )
    assert metric_names.MEETINGS_STARTED not in emitted.counts


def test_a_failed_create_counts_no_meeting_started(schema, emitted, monkeypatch):
    """A meeting that could not be created has not started.

    `was_already_created` is false for an ordinary failure as well as for a
    first delivery, so on its own it would not tell these two apart. What does
    is that a failure the resolver did not reject as a duplicate propagates out
    of the dispatch instead of being returned, so the count is never reached and
    the record is redelivered. This asserts that, rather than leaving it to be
    reasoned about: if `execute_create_call_mutation` ever starts returning its
    exceptions, the meeting count silently begins including meetings that do not
    exist.
    """
    rejected = TransportQueryError(
        "service unavailable",
        errors=[{"message": "service unavailable", "errorType": "ServiceUnavailable"}],
    )
    with pytest.raises(Exception):  # noqa: B017 - the transport's own type
        process_event(
            FakeSession(schema, rejected),
            {"EventType": "START", "CallId": "call-1", "AgentId": "someone@example.com"},
            monkeypatch,
        )
    assert metric_names.MEETINGS_STARTED not in emitted.counts


def test_an_end_event_counts_one_meeting_ended(schema, emitted, monkeypatch):
    # getCall first -- a meeting still open -- then the status write.
    session = QueuedSession(
        schema,
        [
            {"getCall": {"CallId": "call-1", "Status": "STARTED"}},
            {"updateCallStatus": {"CallId": "call-1", "Status": "ENDED"}},
        ],
    )
    process_event(session, {"EventType": "END", "CallId": "call-1"}, monkeypatch)
    assert emitted.counts.get(metric_names.MEETINGS_ENDED) == 1


def test_a_redelivered_end_event_counts_no_meeting_ended(schema, emitted, monkeypatch):
    """A meeting already ENDED is not ending again."""
    session = QueuedSession(
        schema,
        [
            {"getCall": {"CallId": "call-1", "Status": "ENDED"}},
            {"updateCallStatus": {"CallId": "call-1", "Status": "ENDED"}},
        ],
    )
    process_event(session, {"EventType": "END", "CallId": "call-1"}, monkeypatch)
    assert metric_names.MEETINGS_ENDED not in emitted.counts


def test_a_failed_status_write_counts_no_meeting_ended(schema, emitted, monkeypatch):
    """The record is redelivered, so the meeting has not ended yet.

    Counting it here would both overstate the ending and count it twice: once
    now, and again when the redelivery succeeds.

    Unlike the transcript-segment mutations, the status write is awaited
    directly rather than gathered, so its failure propagates out of the
    dispatch. The raise is asserted here too, because it is the reason this
    metric needs no alarm of its own -- but not because it reaches
    AWS/Lambda Errors, which it does not: the batch processor attributes the
    exception to the record's sequence number and the invocation returns a
    partial-batch response normally. What makes the failure visible is that the
    record is redelivered, and a record that cannot succeed eventually reaches
    the event source's on-failure destination, which the
    discarded-records-queue-depth alarm watches. A gathered segment failure gets
    none of that, which is what the two metrics are for.
    """
    session = QueuedSession(
        schema,
        [
            {"getCall": {"CallId": "call-1", "Status": "STARTED"}},
            TransportQueryError("boom", errors=[{"message": "boom"}]),
        ],
    )
    with pytest.raises(Exception):  # noqa: B017 - the transport's own type
        process_event(session, {"EventType": "END", "CallId": "call-1"}, monkeypatch)
    assert metric_names.MEETINGS_ENDED not in emitted.counts


# The responses a transcript-segment event draws, in the order it draws them:
# the meeting record for the segment being written, then the scored segments the
# meeting aggregation is computed from. The last response in a QueuedSession
# repeats, so it answers both the segment write and the aggregation write.
def segment_path_responses(write_response: Any) -> List[Any]:
    return [
        {"getCall": {"CallId": "call-1", "Status": "STARTED"}},
        SENTIMENT_RESPONSES["populated"],
        write_response,
    ]


SEGMENT_EVENT = {
    "EventType": "ADD_TRANSCRIPT_SEGMENT",
    "CallId": "call-1",
    "Transcript": "hello there",
    "IsPartial": False,
    # The schema declares both Float!, and normalisation passes through whatever
    # the producer sent, so omitting them fails query building rather than the
    # write. A non-zero EndTime also gives the meeting aggregation a duration to
    # write, which is what puts a non-segment task in the gather below.
    "StartTime": 0.0,
    "EndTime": 1.5,
}


@pytest.fixture
def gathered(monkeypatch) -> Dict[str, Any]:
    """Records how many tasks the segment path hands to `asyncio.gather`.

    The segment counts are taken from a *slice* of the gathered results, because
    the gather also carries sentiment and meeting-aggregation tasks
    whose failures are not lost transcript lines. A fixture whose gather holds
    only segment tasks cannot tell that slice from the whole list, so the tests
    below assert the gather holds more than the slice does before believing
    their own counts.

    Every call is recorded, and `total` is the largest. The dispatch gathers
    once today, so the distinction does not bite -- but `asyncio.gather` is
    replaced process-wide for the test (`cep.asyncio` *is* the module), so a
    nested gather added anywhere under this call would otherwise leave the
    guard reading whichever happened to run last.
    """
    seen: Dict[str, Any] = {"calls": []}
    real_gather = asyncio.gather

    async def counting_gather(*awaitables, **kwargs):
        seen["calls"].append(len(awaitables))
        seen["total"] = max(seen["calls"])
        return await real_gather(*awaitables, **kwargs)

    monkeypatch.setattr(cep.asyncio, "gather", counting_gather)
    return seen


def test_written_segments_are_counted_and_no_failure_is(schema, emitted, gathered, monkeypatch):
    """The ordinary path, so the failure case below is not passing vacuously.

    Also pins that only the segment writes are counted. The meeting aggregation
    is gathered alongside them and succeeds here too, so a count that read the
    whole gather would report two segments written where one was.
    """
    session = QueuedSession(
        schema,
        segment_path_responses({"addTranscriptSegment": {"CallId": "call-1", "SegmentId": "s1"}}),
    )
    process_event(session, dict(SEGMENT_EVENT), monkeypatch)

    assert gathered["total"] > 1, (
        "this fixture put only segment tasks in the gather, so it cannot tell a count of "
        "the segment slice from a count of everything"
    )
    assert emitted.counts.get(metric_names.TRANSCRIPT_SEGMENTS_WRITTEN) == 1
    assert metric_names.TRANSCRIPT_SEGMENT_WRITE_FAILED not in emitted.counts


def test_a_failed_segment_write_is_counted_as_a_failure(schema, emitted, gathered, monkeypatch):
    """The reason this metric exists.

    The segment mutations are gathered with ``return_exceptions=True``, so this
    failure is collected into the invocation's result and never raised: the
    batch processor does not attribute the record, nothing redelivers it on its
    own account, and AWS/Lambda Errors stays flat. Another record in the same
    batch raising would redeliver this one with the rest of the tail, but
    nothing makes that happen for this failure's sake. So this count is the only
    notice that a transcript line was lost.
    """
    session = QueuedSession(
        schema,
        segment_path_responses(
            TransportQueryError("write rejected", errors=[{"message": "write rejected"}])
        ),
    )
    process_event(session, dict(SEGMENT_EVENT), monkeypatch)

    assert gathered["total"] > 1, "see test_written_segments_are_counted_and_no_failure_is"
    assert emitted.counts.get(metric_names.TRANSCRIPT_SEGMENT_WRITE_FAILED) == 1
    assert metric_names.TRANSCRIPT_SEGMENTS_WRITTEN not in emitted.counts


# ── the automatic agenda pass ───────────────────────────────────────────────


class RecordingLambda:
    def __init__(self) -> None:
        self.invocations: List[Dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> Dict[str, Any]:
        self.invocations.append(kwargs)
        return {}


@pytest.fixture
def agenda_hook(monkeypatch) -> RecordingLambda:
    hook = RecordingLambda()
    monkeypatch.setattr(
        cep, "AGENDA_DERIVER_ARN", "arn:aws:lambda:us-east-1:123456789012:function:d"
    )
    monkeypatch.setattr(cep, "AGENDA_DERIVATION_AFTER_SECONDS", 300.0)
    monkeypatch.setattr(cep, "AGENDA_PASS_QUEUED", set())
    monkeypatch.setattr(cep, "LAMBDA_HOOK_CLIENT", hook, raising=False)
    return hook


def _segment(end_time: float, partial: bool = False) -> Dict[str, Any]:
    return {**SEGMENT_EVENT, "StartTime": end_time - 1, "EndTime": end_time, "IsPartial": partial}


def _respond(schema: Any) -> QueuedSession:
    return QueuedSession(
        schema,
        segment_path_responses({"addTranscriptSegment": {"CallId": "call-1", "SegmentId": "s1"}}),
    )


def test_the_agenda_pass_is_queued_once_the_meeting_passes_five_minutes(
    schema, emitted, agenda_hook, monkeypatch
):
    process_event(_respond(schema), _segment(120.0), monkeypatch)
    assert agenda_hook.invocations == []
    process_event(_respond(schema), _segment(301.0), monkeypatch)
    process_event(_respond(schema), _segment(320.0), monkeypatch)
    (invocation,) = agenda_hook.invocations
    assert invocation["InvocationType"] == "Event"
    assert json.loads(invocation["Payload"]) == {"CallId": "call-1", "Trigger": "auto"}


def test_a_partial_segment_does_not_queue_the_agenda_pass(
    schema, emitted, agenda_hook, monkeypatch
):
    process_event(_respond(schema), _segment(400.0, partial=True), monkeypatch)
    assert agenda_hook.invocations == []


def test_a_failure_to_queue_the_agenda_pass_does_not_fail_the_segment(
    schema, emitted, agenda_hook, monkeypatch
):
    def refuse(**_kwargs: Any) -> Dict[str, Any]:
        raise RuntimeError("throttled")

    monkeypatch.setattr(agenda_hook, "invoke", refuse)
    process_event(_respond(schema), _segment(301.0), monkeypatch)
    assert emitted.counts.get(metric_names.TRANSCRIPT_SEGMENTS_WRITTEN) == 1


# ── the transcript deletion is told when a meeting ends and is summarized ────

DELETION_ARN = "arn:aws:lambda:us-east-1:123456789012:function:stack-TranscriptDeletion"


@pytest.fixture
def deletion_function(monkeypatch) -> RecordingLambda:
    hook = RecordingLambda()
    monkeypatch.setattr(cep, "TRANSCRIPT_DELETION_FUNCTION_ARN", DELETION_ARN)
    monkeypatch.setattr(cep, "LAMBDA_HOOK_CLIENT", hook, raising=False)
    monkeypatch.setattr(cep, "POST_CALL_SUMMARY_LAMBDA_HOOK_FUNCTION_ARN", "", raising=False)
    return hook


def _deletion_payloads(hook: RecordingLambda) -> List[Dict[str, Any]]:
    return [
        json.loads(invocation["Payload"])
        for invocation in hook.invocations
        if invocation["FunctionName"] == DELETION_ARN
    ]


def test_the_first_end_tells_the_transcript_deletion_the_meeting_ended(
    schema, emitted, deletion_function, monkeypatch
):
    session = QueuedSession(
        schema,
        [
            {"getCall": {"CallId": "call-1", "Status": "STARTED"}},
            {"updateCallStatus": {"CallId": "call-1", "Status": "ENDED"}},
        ],
    )
    process_event(session, {"EventType": "END", "CallId": "call-1"}, monkeypatch)
    assert _deletion_payloads(deletion_function) == [
        {"CallId": "call-1", "Trigger": "meeting-ended"}
    ]


def test_a_redelivered_end_does_not_tell_the_transcript_deletion_again(
    schema, emitted, deletion_function, monkeypatch
):
    session = QueuedSession(
        schema,
        [
            {"getCall": {"CallId": "call-1", "Status": "ENDED"}},
            {"updateCallStatus": {"CallId": "call-1", "Status": "ENDED"}},
        ],
    )
    process_event(session, {"EventType": "END", "CallId": "call-1"}, monkeypatch)
    assert _deletion_payloads(deletion_function) == []


def test_a_stored_summary_tells_the_transcript_deletion(
    schema, emitted, deletion_function, monkeypatch
):
    session = QueuedSession(schema, [{"addCallSummaryText": {"CallId": "call-1"}}])
    process_event(
        session,
        {"EventType": "ADD_SUMMARY", "CallId": "call-1", "CallSummaryText": "{}"},
        monkeypatch,
    )
    assert _deletion_payloads(deletion_function) == [
        {"CallId": "call-1", "Trigger": "summary-written"}
    ]


def test_a_summary_is_stored_even_if_the_transcript_deletion_cannot_be_told(
    schema, emitted, monkeypatch
):
    def unavailable(**_kwargs: Any) -> Dict[str, Any]:
        raise RuntimeError("Lambda unavailable")

    monkeypatch.setattr(cep, "TRANSCRIPT_DELETION_FUNCTION_ARN", DELETION_ARN)
    monkeypatch.setattr(cep, "POST_CALL_SUMMARY_LAMBDA_HOOK_FUNCTION_ARN", "", raising=False)
    monkeypatch.setattr(
        cep,
        "LAMBDA_HOOK_CLIENT",
        type("C", (), {"invoke": staticmethod(unavailable)})(),
        raising=False,
    )
    session = QueuedSession(schema, [{"addCallSummaryText": {"CallId": "call-1"}}])
    process_event(
        session,
        {"EventType": "ADD_SUMMARY", "CallId": "call-1", "CallSummaryText": "{}"},
        monkeypatch,
    )
    logged = [record["message"] for record in cep.LOGGER.records]
    assert any("could not tell the transcript deletion function" in m for m in logged), logged


@pytest.mark.parametrize("text", ["An error occurred.", "No summary available", "  "])
def test_a_failed_summary_does_not_bring_the_transcript_deletion_forward(
    schema, emitted, deletion_function, monkeypatch, text
):
    """The transcript waits for the end-of-meeting fallback, so the summary can be regenerated."""
    session = QueuedSession(schema, [{"addCallSummaryText": {"CallId": "call-1"}}])
    process_event(
        session,
        {"EventType": "ADD_SUMMARY", "CallId": "call-1", "CallSummaryText": text},
        monkeypatch,
    )
    assert _deletion_payloads(deletion_function) == []


# ── the meeting's duration ───────────────────────────────────────────────────


def test_the_duration_is_the_latest_final_speech_segment_end_not_this_segments(schema):
    """A batch carrying an early segment must not shorten the stored duration.

    Final segments arrive out of order across batches; the duration written is
    the latest end of every final speech segment read back, so a late batch with
    an early segment still writes the meeting's full length so far.
    """
    response = {
        "getTranscriptSegmentsWithSentiment": {
            "TranscriptSegmentsWithSentiment": [
                {"Channel": "REMOTE", "SegmentId": "a", "StartTime": 0.0, "EndTime": 30.0},
                {"Channel": "LOCAL", "SegmentId": "b", "StartTime": 1000.0, "EndTime": 1036.5},
                {
                    "Channel": "AGENT_ASSISTANT",
                    "SegmentId": "c",
                    "StartTime": 2000.0,
                    "EndTime": 2001.0,
                },
            ]
        }
    }
    session = FakeSession(schema, response)
    aggregation = run(
        cep.get_aggregate_call_data(
            message={"CallId": "call-1", "EndTime": 30.0, "EventType": "ADD_TRANSCRIPT_SEGMENT"},
            appsync_session=session,
        )
    )
    # The assistant's row is not speech in the meeting and does not count.
    assert aggregation["TotalConversationDurationMillis"] == 1036500.0


def test_the_duration_includes_a_segment_not_yet_readable(schema):
    """The segment this record carries may be later than anything read back."""
    response = {"getTranscriptSegmentsWithSentiment": {"TranscriptSegmentsWithSentiment": []}}
    session = FakeSession(schema, response)
    aggregation = run(
        cep.get_aggregate_call_data(
            message={
                "CallId": "call-1",
                "Channel": "REMOTE",
                "EndTime": 12.25,
                "EventType": "ADD_TRANSCRIPT_SEGMENT",
            },
            appsync_session=session,
        )
    )
    assert aggregation["TotalConversationDurationMillis"] == 12250.0


def test_a_non_speech_segment_not_yet_readable_does_not_lengthen_the_duration(schema):
    """The record's own segment counts only on a speech channel, as the read-back ones do."""
    response = {
        "getTranscriptSegmentsWithSentiment": {
            "TranscriptSegmentsWithSentiment": [
                {"Channel": "LOCAL", "SegmentId": "a", "StartTime": 0.0, "EndTime": 20.0}
            ]
        }
    }
    aggregation = run(
        cep.get_aggregate_call_data(
            message={
                "CallId": "call-1",
                "Channel": "AGENT_ASSISTANT",
                "EndTime": 90.0,
                "EventType": "ADD_TRANSCRIPT_SEGMENT",
            },
            appsync_session=FakeSession(schema, response),
        )
    )
    assert aggregation["TotalConversationDurationMillis"] == 20000.0


def test_the_duration_takes_the_latest_end_of_every_final_segment_in_the_record(
    schema, monkeypatch
):
    """One Kinesis record can normalize into several final segments.

    The aggregation is built once, for the record's last final segment, before
    any of the record's segments is written. An earlier segment in the same
    record that ends later must still reach the duration: if this is the
    meeting's last record, nothing afterwards corrects it.
    """
    segments = [
        {"CallId": "call-1", "Channel": "REMOTE", "EndTime": 95.0, "IsPartial": False},
        {"CallId": "call-1", "Channel": "AGENT_ASSISTANT", "EndTime": 120.0, "IsPartial": False},
        {"CallId": "call-1", "Channel": "LOCAL", "EndTime": 99.0, "IsPartial": True},
        {"CallId": "call-1", "Channel": "LOCAL", "EndTime": 60.0, "IsPartial": False},
    ]
    built = []

    async def no_details(**_kwargs):
        return {}

    async def recording_tasks(message, appsync_session, record_latest_end=None):
        built.append(
            await cep.get_aggregate_call_data(
                message=message,
                appsync_session=appsync_session,
                record_latest_end=record_latest_end,
            )
        )
        return []

    monkeypatch.setattr(cep, "normalize_transcript_segments", lambda _message: segments)
    monkeypatch.setattr(cep, "get_call_details", no_details)
    monkeypatch.setattr(cep, "add_transcript_segments", lambda **_kwargs: [])
    monkeypatch.setattr(cep, "AGENDA_DERIVER_ARN", "", raising=False)
    monkeypatch.setattr(cep, "get_call_aggregation_tasks", recording_tasks)
    empty = {"getTranscriptSegmentsWithSentiment": {"TranscriptSegmentsWithSentiment": []}}
    process_event(
        FakeSession(schema, empty),
        {"EventType": "ADD_TRANSCRIPT_SEGMENT", "CallId": "call-1"},
        monkeypatch,
    )
    # One aggregation for the record. The partial segment and the assistant's
    # segment end later than 95 s but are not final speech.
    assert len(built) == 1
    assert built[0]["TotalConversationDurationMillis"] == 95000.0


# ── a transcript segment takes the meeting's Owner and SharedWith ────────────

MEETING_OWNER = "owner@example.com"
MEETING_SHARED_WITH = "[colleague@example.com]"


def _meeting_record(**fields: Any) -> Dict[str, Any]:
    return {"getCall": {"CallId": "call-1", "Status": "STARTED", **fields}}


def mutation_inputs(session: Any, field: str) -> List[Dict[str, Any]]:
    """The ``input`` argument of each ``field`` mutation the session was sent."""
    inputs = []
    for query in session.queries:
        operation = query.definitions[0]
        if operation.operation.value != "mutation":
            continue
        selection = operation.selection_set.selections[0]
        if selection.name.value != field:
            continue
        (argument,) = selection.arguments
        inputs.append(
            {
                item.name.value: (
                    None
                    if item.value.kind == "null_value"
                    else getattr(item.value, "value", item.value)
                )
                for item in argument.value.fields
            }
        )
    return inputs


def run_with(
    session: Any, message: Dict[str, Any], monkeypatch, **switches: Any
) -> RecordingLambda:
    """``process_event``, with the given module switches turned on afterwards."""
    hook = RecordingLambda()
    monkeypatch.setattr(cep, "LAMBDA_HOOK_CLIENT", hook, raising=False)
    monkeypatch.setattr(cep, "AGENDA_DERIVER_ARN", "", raising=False)

    real_run = cep.execute_process_event_api_mutation

    async def with_switches(**kwargs: Any) -> Any:
        for name, value in switches.items():
            monkeypatch.setattr(cep, name, value, raising=False)
        # The dispatch sets this switch from its argument, not the module.
        if switches.get("IS_LAMBDA_AGENT_ASSIST_ENABLED"):
            kwargs["agent_assist_args"] = {"is_lambda_agent_assist_enabled": True}
        return await real_run(**kwargs)

    monkeypatch.setattr(cep, "execute_process_event_api_mutation", with_switches)
    process_event(session, message, monkeypatch)
    return hook


def segment_session(schema: Any, meeting: Dict[str, Any]) -> QueuedSession:
    return QueuedSession(
        schema,
        [
            meeting,
            SENTIMENT_RESPONSES["populated"],
            {"addTranscriptSegment": {"CallId": "call-1", "SegmentId": "s1"}},
        ],
    )


@pytest.mark.parametrize("channel", ["REMOTE", "LOCAL", "MEETING_CHAT"])
def test_a_stored_segment_carries_the_meetings_owner_and_shared_with(
    schema, emitted, channel, monkeypatch
):
    session = segment_session(
        schema, _meeting_record(Owner=MEETING_OWNER, SharedWith=MEETING_SHARED_WITH)
    )
    process_event(session, {**SEGMENT_EVENT, "Channel": channel}, monkeypatch)
    (written,) = mutation_inputs(session, "addTranscriptSegment")
    assert written["Owner"] == MEETING_OWNER
    assert written["SharedWith"] == MEETING_SHARED_WITH
    assert written["Channel"] == channel


def test_a_stored_segment_takes_the_meetings_owner_over_one_the_event_carries(
    schema, emitted, monkeypatch
):
    session = segment_session(schema, _meeting_record(Owner=MEETING_OWNER))
    monkeypatch.setattr(
        cep, "normalize_transcript_segments", lambda m: [{**m, "Owner": "someone-else@example.com"}]
    )
    process_event(session, dict(SEGMENT_EVENT), monkeypatch)
    (written,) = mutation_inputs(session, "addTranscriptSegment")
    assert written["Owner"] == MEETING_OWNER


def test_a_segment_of_a_meeting_without_an_owner_is_stored_without_one(
    schema, emitted, monkeypatch
):
    session = segment_session(schema, _meeting_record())
    process_event(session, dict(SEGMENT_EVENT), monkeypatch)
    (written,) = mutation_inputs(session, "addTranscriptSegment")
    assert written.get("Owner") is None
    assert written.get("SharedWith") is None


def test_the_sentiment_write_of_a_segment_carries_the_meetings_owner_too(
    schema, emitted, monkeypatch
):
    """The sentiment write replaces the stored item, so it has to keep the owner."""
    session = segment_session(
        schema, _meeting_record(Owner=MEETING_OWNER, SharedWith=MEETING_SHARED_WITH)
    )
    run_with(
        session,
        {**SEGMENT_EVENT, "Channel": "REMOTE", "Sentiment": "POSITIVE"},
        monkeypatch,
        IS_SENTIMENT_ANALYSIS_ENABLED=True,
    )
    writes = mutation_inputs(session, "addTranscriptSegment")
    assert len(writes) == 2, "expected the segment write and its sentiment write"
    for written in writes:
        assert written["Owner"] == MEETING_OWNER
        assert written["SharedWith"] == MEETING_SHARED_WITH


def test_an_assistant_answer_carries_the_meetings_owner_and_shared_with(
    schema, emitted, monkeypatch
):
    """ADD_AGENT_ASSIST rows are stored like the meeting's other segments."""
    session = QueuedSession(
        schema,
        [
            _meeting_record(Owner=MEETING_OWNER, SharedWith=MEETING_SHARED_WITH),
            {"addTranscriptSegment": {"CallId": "call-1", "SegmentId": "a1"}},
        ],
    )
    process_event(
        session,
        {
            **SEGMENT_EVENT,
            "EventType": "ADD_AGENT_ASSIST",
            "Channel": "CHAT_ASSISTANT",
            "SegmentId": "a1",
            "Transcript": "an answer",
            # The orchestrator's Owner is whoever asked, not the meeting's owner.
            "Owner": "asker@example.com",
        },
        monkeypatch,
    )
    (written,) = mutation_inputs(session, "addTranscriptSegment")
    assert written["Owner"] == MEETING_OWNER
    assert written["SharedWith"] == MEETING_SHARED_WITH


def test_the_wake_phrase_request_is_made_with_the_owner_the_event_carried(
    schema, emitted, monkeypatch
):
    """The orchestrator passes Owner on as the asker's identity; a spoken wake phrase has none."""
    session = segment_session(schema, _meeting_record(Owner=MEETING_OWNER))
    hook = run_with(
        session,
        {**SEGMENT_EVENT, "Channel": "REMOTE", "Transcript": "ok assistant what is next"},
        monkeypatch,
        IS_LAMBDA_AGENT_ASSIST_ENABLED=True,
        ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN="arn:aws:lambda:us-east-1:123456789012:function:o",
        wake_phrase_pattern=lambda: re.compile("ok assistant", re.IGNORECASE),
    )
    (written,) = mutation_inputs(session, "addTranscriptSegment")
    assert written["Owner"] == MEETING_OWNER
    (invocation,) = [i for i in hook.invocations if i["FunctionName"].endswith(":function:o")]
    assert json.loads(invocation["Payload"])["Owner"] is None


def test_get_call_details_returns_the_meetings_owner(schema):
    session = FakeSession(schema, _meeting_record(Owner=MEETING_OWNER))
    details = run(cep.get_call_details(message={"CallId": "call-1"}, appsync_session=session))
    assert details["Owner"] == MEETING_OWNER
