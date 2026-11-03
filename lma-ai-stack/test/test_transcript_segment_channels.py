# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The transcript `Channel` enum and the queries that return stored segments.

LMA before 0.4.0 stored call-category markers in a meeting's transcript with the
channel `CATEGORY_MATCH`. 0.4.0 removes that value from the GraphQL `Channel`
enum, but the markers stay in DynamoDB until they expire. AppSync cannot return
an item whose `Channel` is not an enum value, and the error would cost a whole
older meeting its transcript, along with the sharing and deletion that read it
through the same query. So every query that returns stored transcript segments
leaves those items out in its DynamoDB filter.

The tests find those queries from the schema rather than from a list kept here:
any Query field whose result reaches a type with a `Channel` field must have a
request template that applies the filter. A new query of that kind fails here
until it does.

The last test evaluates the request templates with AppSync's
EvaluateMappingTemplate API, which touches no resource but needs AWS
credentials, so it runs only when LMA_EVALUATE_APPSYNC=1 is set:

    LMA_EVALUATE_APPSYNC=1 AWS_PROFILE=default python -m pytest \
        lma-ai-stack/test/test_transcript_segment_channels.py -q
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

# graphql-core is a dev dependency of lib/lma_sdk, as for test_appsync_contract.py.
from graphql import (
    GraphQLEnumType,
    GraphQLNonNull,
    GraphQLObjectType,
    build_ast_schema,
    get_named_type,
    parse,
)

AI_STACK = Path(__file__).resolve().parents[1]
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
APPSYNC_DIR = AI_STACK / "source" / "appsync"
SCHEMA = APPSYNC_DIR / "schema.graphql"

RETIRED_CHANNEL = "CATEGORY_MATCH"
CHANNELS = {
    "CALLER",
    "AGENT",
    "AGENT_VOICETONE",
    "CALLER_VOICETONE",
    "AGENT_ASSISTANT",
    "CHAT_ASSISTANT",
}

# The scalars and directives AppSync declares implicitly; see test_appsync_contract.py.
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

_RESOURCE_SPLIT = re.compile(r"\n  (?=\w+:\n)")


@pytest.fixture(scope="module")
def schema():
    return build_ast_schema(parse(APPSYNC_PRELUDE + SCHEMA.read_text()), assume_valid_sdl=True)


def _reaches_channel(type_, seen: set[str]) -> bool:
    """Whether a value of this type can contain a field of the `Channel` enum."""
    named = get_named_type(type_)
    if not isinstance(named, GraphQLObjectType) or named.name in seen:
        return False
    seen.add(named.name)
    for field in named.fields.values():
        field_type = get_named_type(field.type)
        if isinstance(field_type, GraphQLEnumType) and field_type.name == "Channel":
            return True
        if _reaches_channel(field.type, seen):
            return True
    return False


def _queries_returning_channels(schema) -> list[str]:
    return sorted(
        name
        for name, field in schema.query_type.fields.items()
        if _reaches_channel(field.type, set())
    )


def _request_template(field_name: str) -> Path:
    """The request mapping template of the Query resolver on this field."""
    matches = []
    for block in _RESOURCE_SPLIT.split(TEMPLATE.read_text()):
        if "Type: AWS::AppSync::Resolver" not in block:
            continue
        if not re.search(r"^      TypeName: Query$", block, re.M):
            continue
        if not re.search(rf"^      FieldName: {field_name}$", block, re.M):
            continue
        location = re.search(r"^      RequestMappingTemplateS3Location: (\S+)", block, re.M)
        assert location, f"Query.{field_name} has no request template file"
        matches.append((TEMPLATE.parent / location.group(1)).resolve())
    assert len(matches) == 1, f"expected exactly one resolver on Query.{field_name}"
    return matches[0]


def test_channel_enum_declares_only_the_channels_written_today(schema) -> None:
    assert set(schema.get_type("Channel").values) == CHANNELS


def test_the_queries_returning_stored_segments_are_the_two_transcript_reads(schema) -> None:
    """Guards the discovery the next test relies on."""
    assert _queries_returning_channels(schema) == [
        "getTranscriptSegments",
        "getTranscriptSegmentsWithSentiment",
    ]


@pytest.mark.parametrize(
    "field_name", ["getTranscriptSegments", "getTranscriptSegmentsWithSentiment"]
)
def test_each_segment_query_filters_out_the_retired_channel(field_name: str) -> None:
    text = _request_template(field_name).read_text()
    filter_block = text[text.index('"filter"') :]
    expression = re.search(r'"expression":\s*"([^"]*)"', filter_block).group(1)
    assert "attribute_not_exists(#Channel) OR #Channel <> :retiredChannel" in expression
    # The filter the query already applied is kept.
    assert "#IsPartial = :isPartial" in expression
    assert '"#Channel": "Channel"' in filter_block
    assert f'":retiredChannel": $util.dynamodb.toDynamoDBJson("{RETIRED_CHANNEL}")' in filter_block


def test_every_query_returning_stored_segments_is_filtered(schema) -> None:
    for field_name in _queries_returning_channels(schema):
        assert ":retiredChannel" in _request_template(field_name).read_text(), field_name


def test_a_new_segment_carries_a_channel_of_the_enum(schema) -> None:
    """The subscription relays addTranscriptSegment, whose input is enum-typed."""
    channel = schema.get_type("AddTranscriptSegmentInput").fields["Channel"].type
    assert isinstance(channel, GraphQLNonNull)
    assert channel.of_type.name == "Channel"
    subscription = schema.subscription_type.fields["onAddTranscriptSegment"]
    directive = next(d for d in subscription.ast_node.directives if d.name.value == "aws_subscribe")
    mutations = [value.value for value in directive.arguments[0].value.values]
    assert mutations == ["addTranscriptSegment"]


@pytest.mark.skipif(
    os.environ.get("LMA_EVALUATE_APPSYNC") != "1",
    reason="evaluates templates with the AppSync API; set LMA_EVALUATE_APPSYNC=1 to run",
)
@pytest.mark.parametrize(
    "field_name", ["getTranscriptSegments", "getTranscriptSegmentsWithSentiment"]
)
def test_the_evaluated_request_excludes_the_retired_channel(field_name: str) -> None:
    import boto3  # imported here so the module collects without boto3 when skipped

    client = boto3.client("appsync", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    response = client.evaluate_mapping_template(
        template=_request_template(field_name).read_text(),
        context=json.dumps({"arguments": {"callId": "c1"}}),
    )
    assert not response.get("error"), response.get("error")
    request = json.loads(response["evaluationResult"])
    query_filter = request["filter"]
    assert query_filter["expressionNames"]["#Channel"] == "Channel"
    assert query_filter["expressionValues"][":retiredChannel"] == {"S": RETIRED_CHANNEL}
    assert query_filter["expressionValues"][":isPartial"] == {"BOOL": False}
