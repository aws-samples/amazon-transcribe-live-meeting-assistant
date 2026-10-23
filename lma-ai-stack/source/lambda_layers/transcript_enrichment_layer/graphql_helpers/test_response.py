# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for reading a field out of an AppSync GraphQL response.

The reads in the call event processor go through ``graphql_helpers.response``,
and the shapes exercised here are the ones AppSync
can answer with: the field populated, the field present but null, the field
absent, ``errors`` with no ``data``, an empty object, and a response that is not
a mapping at all. Each has a defined outcome -- a value, a default, or
``GraphQLResponseError`` -- and none of them may surface as an ``AttributeError``
from a chained lookup, because that error names neither the field nor the
response and leaves an operator with nothing to act on.

The assertions on the log lines are part of the contract, not decoration: the
line has to name the field and the shape that came back, and it must not carry a
GraphQL error ``message``, which can quote the mutation argument it rejected.

No AWS and no network: these call the helpers directly with literal responses.
"""

from __future__ import annotations

import logging

import pytest
from graphql import GraphQLError, Source

from graphql_helpers.response import (
    GraphQLResponseError,
    describe_exception,
    describe_graphql_response,
    describe_value,
    graphql_error_messages,
    graphql_errors,
    is_retryable_graphql_response,
    optional_graphql_field,
    require_graphql_field,
    response_data,
    traceback_frames,
)

LOGGER = logging.getLogger("test_graphql_response")

# A getCall response as AppSync answers it when the meeting is stored.
POPULATED = {"getCall": {"CallId": "call-1", "Status": "STARTED", "SharedWith": None}}

# The meeting is not readable: the field is declared nullable, so this is a
# successful response and not an error.
NULL_FIELD = {"getCall": None}

# An error-shaped response: errors and no data. The message is long and quotes a
# mutation argument, which is why it must not reach a log line.
ERRORS_NO_DATA = {
    "errors": [
        {
            "errorType": "MappingTemplate",
            "path": ["getCall"],
            "locations": [{"line": 2, "column": 3}],
            "message": "Unable to transform the request: Transcript 'the figures we discussed'",
        }
    ]
}

PERMANENT_ERRORS = {
    "errors": [
        {"errorType": "ValidationError", "path": ["addTranscriptSegment"], "message": "bad input"}
    ]
}

# Each case is (label, response). Every helper is run against all of them, so a
# shape added here is covered by every test below that parametrizes on it.
RESPONSES = [
    ("populated", POPULATED),
    ("null field", NULL_FIELD),
    ("errors and no data", ERRORS_NO_DATA),
    ("none", None),
    ("empty dict", {}),
    ("data without the field", {"getTranscriptSegments": {"TranscriptSegments": []}}),
    ("not a mapping", "unexpected"),
]

UNUSABLE = [case for case in RESPONSES if case[0] != "populated"]


# ── no chained-lookup failures, whatever the shape ──────────────────────────


@pytest.mark.parametrize(("label", "response"), RESPONSES, ids=[case[0] for case in RESPONSES])
def test_optional_field_returns_a_value_or_the_default_for_every_shape(label, response):
    """The optional read answers for every shape AppSync can return."""
    sentinel = object()
    value = optional_graphql_field(response, "getCall", "Status", logger=LOGGER, default=sentinel)
    if label == "populated":
        assert value == "STARTED"
    else:
        assert value is sentinel


@pytest.mark.parametrize(("label", "response"), UNUSABLE, ids=[case[0] for case in UNUSABLE])
def test_required_field_raises_graphql_response_error_not_attribute_error(label, response):
    """An unusable response fails the caller by name, with the field attached."""
    with pytest.raises(GraphQLResponseError) as raised:
        require_graphql_field(response, "getCall", logger=LOGGER)
    assert not isinstance(raised.value, AttributeError)
    assert raised.value.path == ("getCall",)
    assert "getCall" in str(raised.value)


def test_required_field_returns_the_value_when_the_response_carries_it():
    """The success path hands back the field itself, untouched."""
    assert require_graphql_field(POPULATED, "getCall", logger=LOGGER) == POPULATED["getCall"]
    assert require_graphql_field(POPULATED, "getCall", "CallId", logger=LOGGER) == "call-1"


def test_a_null_leaf_is_treated_as_absent():
    """A field present with a null value carries nothing the caller can read."""
    assert optional_graphql_field(POPULATED, "getCall", "SharedWith", logger=LOGGER) is None
    with pytest.raises(GraphQLResponseError):
        require_graphql_field(POPULATED, "getCall", "SharedWith", logger=LOGGER)


def test_a_list_field_comes_back_as_the_list():
    """The segment aggregation reads a list, which must survive unchanged."""
    response = {"getTranscriptSegmentsWithSentiment": {"TranscriptSegmentsWithSentiment": [1, 2]}}
    assert optional_graphql_field(
        response,
        "getTranscriptSegmentsWithSentiment",
        "TranscriptSegmentsWithSentiment",
        logger=LOGGER,
        default=[],
    ) == [1, 2]


# ── the log line ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("label", "response"), UNUSABLE, ids=[case[0] for case in UNUSABLE])
def test_optional_read_logs_the_field_and_the_shape_it_got(label, response, caplog):
    """The warning has to be enough to tell which field and which shape."""
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        optional_graphql_field(response, "getCall", "Status", logger=LOGGER)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "getCall" in message
    assert "responseType" in message and "dataShape" in message


@pytest.mark.parametrize(("label", "response"), UNUSABLE, ids=[case[0] for case in UNUSABLE])
def test_required_read_logs_the_field_the_shape_and_whether_to_retry(label, response, caplog):
    """The error line carries the retry classification the exception also holds."""
    with caplog.at_level(logging.ERROR, logger=LOGGER.name):
        with pytest.raises(GraphQLResponseError):
            require_graphql_field(response, "getCall", logger=LOGGER)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "getCall" in message
    assert "looksTransient" in message


def test_the_reads_log_nothing_when_the_field_is_there(caplog):
    """A populated response is the common case and must stay quiet."""
    with caplog.at_level(logging.DEBUG, logger=LOGGER.name):
        optional_graphql_field(POPULATED, "getCall", "Status", logger=LOGGER)
        require_graphql_field(POPULATED, "getCall", logger=LOGGER)
    assert caplog.records == []


# ── what is and is not disclosed ────────────────────────────────────────────


def test_the_description_names_the_error_class_and_the_field_it_failed_on():
    """errorType and path are what make the line actionable."""
    description = describe_graphql_response(ERRORS_NO_DATA, ("getCall",))
    assert description["errors"] == [
        {
            "errorType": "MappingTemplate",
            "path": ["getCall"],
            "locations": [{"line": 2, "column": 3}],
            "messageLength": len(ERRORS_NO_DATA["errors"][0]["message"]),
        }
    ]
    assert description["missingField"] == "getCall"
    assert description["dataShape"] == "null"


def test_the_description_omits_the_error_message_text():
    """A GraphQL error message can quote the argument it rejected."""
    rendered = repr(describe_graphql_response(ERRORS_NO_DATA, ("getCall",)))
    assert "the figures we discussed" not in rendered
    assert "Unable to transform" not in rendered


def test_the_description_names_field_names_but_no_field_values():
    """Field names come from the schema; values come from the meeting."""
    response = {"getCall": {"CallId": "call-1", "Status": "STARTED"}}
    rendered = repr(describe_graphql_response(response, ("getCall", "AgentId")))
    assert "CallId" in rendered and "Status" in rendered
    assert "call-1" not in rendered and "STARTED" not in rendered


def test_the_shape_of_a_null_field_is_distinguishable_from_an_absent_one():
    """These two call for different operator action, so the line must tell them apart."""
    assert describe_value(NULL_FIELD) == "object(getCall=null)"
    assert describe_value({}) == "object()"
    assert describe_value(None) == "null"
    assert describe_value([1, 2, 3]) == "list[3]"
    assert describe_value("a transcript line") == "string"


def test_the_shape_description_stops_at_a_bounded_depth():
    """A deep response must not expand into an unbounded log line."""
    nested = {"a": {"b": {"c": {"d": "e"}}}}
    assert describe_value(nested) == "object(a=object(b=object(1 field)))"


# ── classifying and matching errors ─────────────────────────────────────────


def test_a_request_shape_error_is_classified_as_permanent():
    """A request the schema rejects is rejected again on redelivery."""
    assert is_retryable_graphql_response(PERMANENT_ERRORS) is False


@pytest.mark.parametrize(
    "response",
    [
        ERRORS_NO_DATA,
        {"errors": [{"errorType": "ServiceUnavailable"}]},
        {"errors": [{"message": "no errorType here"}]},
        {"errors": [{"errorType": "ValidationError"}, {"errorType": "InternalFailure"}]},
        NULL_FIELD,
        None,
    ],
)
def test_anything_not_known_to_be_permanent_is_treated_as_retryable(response):
    """Retrying a permanent failure costs a delay; not retrying costs a segment."""
    assert is_retryable_graphql_response(response) is True


def test_the_retry_classification_reaches_the_exception():
    """It is available on the exception, so a handler need not re-derive it."""
    with pytest.raises(GraphQLResponseError) as raised:
        require_graphql_field(PERMANENT_ERRORS, "getCall", logger=LOGGER)
    assert raised.value.looks_transient is False
    assert raised.value.description["errors"][0]["errorType"] == "ValidationError"


def test_error_summaries_are_read_off_an_exception_without_its_message():
    """What the batch processor logs in place of an exception's own str().

    A transport exception stringifies as its first GraphQL error message, so the
    summary is what a log line can carry.
    """

    class _TransportQueryError(Exception):
        def __init__(self, errors):
            super().__init__(str(errors[0]))
            self.errors = errors

    error = _TransportQueryError(
        [
            {
                "errorType": "ValidationError",
                "path": ["addTranscriptSegment"],
                "message": "rejected Transcript 'the figures we discussed'",
            }
        ]
    )
    summaries = graphql_errors(error)
    assert summaries == [
        {
            "errorType": "ValidationError",
            "path": ["addTranscriptSegment"],
            "messageLength": len(error.errors[0]["message"]),
        }
    ]
    assert "the figures we discussed" not in repr(summaries)
    # The guard this stands in for: the exception's own text does carry it.
    assert "the figures we discussed" in str(error)


def test_the_traceback_frames_are_the_whole_chain_and_not_the_exception():
    """What the log lines carry in place of a rendered traceback.

    The whole chain, not the innermost frame: a transport exception re-raised by
    a bare ``raise`` keeps the frame it was first raised in, and
    ``GraphQLResponseError`` keeps ``require_graphql_field`` -- the same frame
    whichever call failed. The call is named by the frames around it.
    """

    def inner():
        raise ValueError("rejected Transcript 'the figures we discussed'")

    def a_named_mutation():
        try:
            inner()
        except ValueError:
            raise  # bare re-raise, as the retry wrapper does

    try:
        a_named_mutation()
    except ValueError as error:
        frames = traceback_frames(error)

    rendered = " ".join(frames)
    assert "in a_named_mutation" in rendered, rendered
    assert "in inner" in rendered, rendered
    assert "the figures we discussed" not in rendered
    assert all(__file__ in frame and " in " in frame for frame in frames)


def test_the_frames_of_a_required_read_name_the_caller_not_just_the_helper():
    """The helper raises, so its own frame alone would say nothing useful."""

    def a_named_read():
        return require_graphql_field({"getCall": None}, "getCall", logger=LOGGER)

    try:
        a_named_read()
    except GraphQLResponseError as error:
        frames = traceback_frames(error)

    rendered = " ".join(frames)
    assert "in require_graphql_field" in rendered, rendered
    assert "in a_named_read" in rendered, rendered


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        # The case that makes the type necessary: str() is empty, so a line built
        # from the exception alone would say nothing about what failed.
        (TimeoutError(), "TimeoutError"),
        (ConnectionResetError("connection reset by peer"), "ConnectionResetError: "),
        (ValueError("invalid AppSync schema"), "ValueError: invalid AppSync schema"),
    ],
)
def test_an_exception_is_described_by_type_even_with_no_text(error, expected):
    """Every failure line names the exception's class, text or no text."""
    described = describe_exception(error)
    assert described.startswith(expected)
    assert described == expected or str(error) in described


def test_a_graphql_error_object_is_summarised_like_its_wire_form():
    """graphql-core hands back objects, not mappings, for a locally-caught error."""
    error = GraphQLError(
        ERRORS_NO_DATA["errors"][0]["message"],
        path=["getCall", "Status"],
        source=Source("{ getCall { Status } }"),
        positions=[2],
    )
    [summary] = graphql_errors({"errors": [error]})
    assert summary["errorType"] == "GraphQLError"
    assert summary["path"] == ["getCall", "Status"]
    assert summary["locations"], "a located error must report where"
    assert summary["messageLength"] == len(ERRORS_NO_DATA["errors"][0]["message"])
    assert "the figures we discussed" not in repr(summary)


def test_a_raised_graphql_error_is_summarised_rather_than_stringified():
    """Its ``str()`` prints an excerpt of the query document, arguments and all.

    gql validates a document against the schema before sending it, so a mutation
    the schema rejects raises this locally rather than returning it.
    """
    document = (
        'mutation { addTranscriptSegment(input: {Transcript: "the figures we discussed"})'
        " { CallId } }"
    )
    error = GraphQLError("Field is not defined", source=Source(document), positions=[11])
    # The guard this stands in for: the exception's own text does carry it.
    assert "the figures we discussed" in str(error)

    described = describe_exception(error)
    assert "GraphQLError" in described
    assert "messageLength" in described
    assert "the figures we discussed" not in described
    assert "addTranscriptSegment" not in described


def test_a_graphql_error_object_reports_the_servers_error_type_when_it_has_one():
    """So the object and wire forms say the same thing about the same failure."""
    error = GraphQLError("rejected", extensions={"errorType": "Unauthorized"})
    [summary] = graphql_errors({"errors": [error]})
    assert summary["errorType"] == "Unauthorized"


def test_a_described_exception_carrying_graphql_errors_omits_their_messages():
    """The reduction still applies; the type is added in front of it."""

    class _TransportQueryError(Exception):
        def __init__(self, errors):
            super().__init__(str(errors[0]))
            self.errors = errors

    error = _TransportQueryError(
        [{"errorType": "ValidationError", "message": ERRORS_NO_DATA["errors"][0]["message"]}]
    )
    described = describe_exception(error)
    assert described.startswith("_TransportQueryError: ")
    assert "ValidationError" in described
    assert "the figures we discussed" not in described


def test_an_unraised_exception_has_no_frames():
    """An exception built but never raised has no traceback to read."""
    assert traceback_frames(ValueError("never raised")) == []


def test_error_messages_are_read_off_an_exception_that_carries_them():
    """gql keeps the GraphQL errors on an attribute of the raised exception."""

    class _TransportQueryError(Exception):
        def __init__(self, errors):
            super().__init__("transport error")
            self.errors = errors

    error = _TransportQueryError([{"message": "item put condition failure"}])
    assert graphql_error_messages(error) == ["item put condition failure"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"message": "item put condition failure"}, ["item put condition failure"]),
        ({"errors": [{"message": "one"}, {"message": "two"}]}, ["one", "two"]),
        ({"errors": [{"errorType": "NoMessage"}]}, []),
        (ValueError("not a graphql error"), []),
        (None, []),
        ("", []),
    ],
)
def test_error_message_extraction_never_raises(value, expected):
    """It is called from inside an except block, where a second failure masks the first."""
    assert graphql_error_messages(value) == expected


@pytest.mark.parametrize(("label", "response"), RESPONSES, ids=[case[0] for case in RESPONSES])
def test_error_summaries_are_a_list_for_every_shape(label, response):
    """`graphql_errors` is read unguarded by the description builder."""
    assert isinstance(graphql_errors(response), list)


def test_errors_with_no_data_resolve_to_no_data():
    """The envelope form must not be mistaken for a data mapping named `errors`."""
    assert response_data(ERRORS_NO_DATA) is None
    assert response_data({"data": {"getCall": None}, "errors": []}) == {"getCall": None}
    # gql's session.execute hands back the data mapping on its own.
    assert response_data(POPULATED) == POPULATED
