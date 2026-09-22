# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The layer's field selectors must resolve against the real AppSync schema.

`graphql_helpers` builds the field lists for the `createCall` /
`updateCall` / `addTranscriptSegment` mutations using gql's DSL, where every
`schema.Call.Something` is an attribute lookup resolved against the schema at
call time. A field that AppSync no longer declares therefore raises — but only
when the Lambda runs, in the middle of processing a Kinesis batch, as a mutation
that fails for every record of that type.

These tests build a `DSLSchema` from `source/appsync/schema.graphql` itself and
call each selector, so that drift fails here in milliseconds instead. That is the
same class of failure the integration suite's `test_kds_pipeline_creates_meeting`
guards, which needs a deployed stack to notice it.

gql is pinned to the same major the layer's requirements.txt pins
(`gql~=3.5.3`), so the DSL exercised here is the DSL the Lambda uses. No AWS and
no network: only the schema file on disk is read.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from graphql import build_ast_schema, parse
from gql.dsl import DSLSchema

from graphql_helpers.call_fields import call_fields
from graphql_helpers.transcript_segment_fields import transcript_segment_fields
from graphql_helpers.transcript_segment_sentiment_fields import (
    transcript_segment_sentiment_fields,
)

# graphql_helpers -> transcript_enrichment_layer -> lambda_layers -> source
SCHEMA = Path(__file__).resolve().parents[3] / "appsync" / "schema.graphql"

# AppSync provides these implicitly, so the committed SDL does not declare them
# and graphql-core cannot build it unaided. Nothing else about the schema is
# changed, so a field the DSL resolves here is one AppSync resolves too.
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

SELECTORS = {
    "call_fields": call_fields,
    "transcript_segment_fields": transcript_segment_fields,
    "transcript_segment_sentiment_fields": transcript_segment_sentiment_fields,
}


@pytest.fixture(scope="module")
def dsl_schema() -> DSLSchema:
    """gql's DSL wrapper around the schema the stack actually deploys."""
    assert SCHEMA.is_file(), f"schema not found at {SCHEMA}"
    schema = build_ast_schema(
        parse(APPSYNC_PRELUDE + SCHEMA.read_text()), assume_valid_sdl=True
    )
    return DSLSchema(schema)


def test_the_schema_under_test_is_the_deployed_one(dsl_schema: DSLSchema):
    """Guards the fixture: a stub schema would make every selector resolve."""
    assert SCHEMA.parts[-2:] == ("appsync", "schema.graphql")
    # Types the selectors reach into, so an empty or placeholder schema is caught
    # before the assertions below start passing for the wrong reason.
    for type_name in ("Call", "TranscriptSegment", "SentimentAggregation"):
        assert getattr(dsl_schema, type_name) is not None


@pytest.mark.parametrize("name", sorted(SELECTORS))
def test_selector_resolves_every_field_against_the_schema(name: str, dsl_schema: DSLSchema):
    """Any field the schema no longer declares raises out of the selector."""
    fields = SELECTORS[name](dsl_schema)
    assert fields, f"{name} selected no fields"


@pytest.mark.parametrize(
    ("name", "minimum"),
    [("call_fields", 15), ("transcript_segment_fields", 12),
     ("transcript_segment_sentiment_fields", 3)],
)
def test_selector_returns_the_expected_number_of_fields(
    name: str, minimum: int, dsl_schema: DSLSchema
):
    """A selector quietly shrinking means the UI stops being sent something.

    Asserted as a lower bound: adding a field is routine, losing several is the
    regression worth noticing.
    """
    assert len(SELECTORS[name](dsl_schema)) >= minimum


def test_call_selector_covers_the_fields_the_ui_reads(dsl_schema: DSLSchema):
    """The mutation has to send what the meeting list and detail pages render.

    Named explicitly rather than counted, because these are the ones whose
    absence shows up as a blank column rather than an error.
    """
    selected = {str(field.name) for field in call_fields(dsl_schema)}
    for expected in (
        "CallId", "Status", "CreatedAt", "UpdatedAt", "AgentId",
        "RecordingUrl", "VideoRecordingUrl", "PcaUrl",
        "Owner", "SharedWith", "CallSummaryText",
        "TotalConversationDurationMillis",
    ):
        assert expected in selected, f"call_fields no longer selects {expected}"


def test_transcript_segment_selector_covers_the_fields_the_ui_reads(dsl_schema: DSLSchema):
    selected = {str(field.name) for field in transcript_segment_fields(dsl_schema)}
    for expected in (
        "CallId", "SegmentId", "StartTime", "EndTime", "Transcript",
        "IsPartial", "Channel", "Speaker", "Owner", "SharedWith",
    ):
        assert expected in selected, f"transcript_segment_fields no longer selects {expected}"


def test_every_selected_call_field_is_declared_on_the_call_type(dsl_schema: DSLSchema):
    """The reverse direction: nothing selected that the type does not define.

    gql raises on an unknown attribute, so this mostly restates the resolution
    test — but it also catches a field that exists on some *other* type and was
    reached by mistake.
    """
    declared = set(dsl_schema.Call._type.fields)  # pylint: disable=protected-access
    selected = {str(field.name) for field in call_fields(dsl_schema)}
    assert selected <= declared, f"selected but not on Call: {sorted(selected - declared)}"
