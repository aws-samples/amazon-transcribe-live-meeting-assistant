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

0.4.0 also renames the two speech channels: CALLER is now REMOTE and AGENT is
LOCAL. Segments stored before then keep the older names, so the enum declares
both, the queries must not filter the older names out, and the meeting's
per-channel sentiment fields exist under both names.

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
SPEECH_CHANNELS = {"REMOTE", "LOCAL"}
# The names LMA before 0.4.0 wrote for REMOTE and LOCAL, still stored and read.
DEPRECATED_SPEECH_CHANNELS = {"CALLER", "AGENT"}
CHANNELS = {
    *SPEECH_CHANNELS,
    *DEPRECATED_SPEECH_CHANNELS,
    "AGENT_VOICETONE",
    "CALLER_VOICETONE",
    "AGENT_ASSISTANT",
    "CHAT_ASSISTANT",
}
# The meeting's sentiment types, keyed by channel.
SENTIMENT_BY_CHANNEL_TYPES = (
    "OverallSentiment",
    "OverallSentimentInput",
    "SentimentByChannel",
    "SentimentByChannelInput",
)
UI_QUERIES = AI_STACK / "source" / "ui" / "src" / "graphql" / "queries"
PY_SENTIMENT_SELECTIONS = (
    AI_STACK / "source" / "lambda_functions" / "summary_regenerator" / "index.py",
    AI_STACK / "source" / "lambda_functions" / "agenda_deriver" / "index.py",
)

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


def _request_template(field_name: str, type_name: str = "Query") -> Path:
    """The request mapping template of the resolver on this field."""
    matches = []
    for block in _RESOURCE_SPLIT.split(TEMPLATE.read_text()):
        if "Type: AWS::AppSync::Resolver" not in block:
            continue
        if not re.search(rf"^      TypeName: {type_name}$", block, re.M):
            continue
        if not re.search(rf"^      FieldName: {field_name}$", block, re.M):
            continue
        location = re.search(r"^      RequestMappingTemplateS3Location: (\S+)", block, re.M)
        assert location, f"{type_name}.{field_name} has no request template file"
        matches.append((TEMPLATE.parent / location.group(1)).resolve())
    assert len(matches) == 1, f"expected exactly one resolver on {type_name}.{field_name}"
    return matches[0]


def test_channel_enum_declares_the_channels_written_and_the_older_speech_names(schema) -> None:
    assert set(schema.get_type("Channel").values) == CHANNELS


def test_the_older_speech_channel_names_are_marked_deprecated_in_the_schema() -> None:
    """The comment above the enum names both old values as deprecated.

    It sits above the enum, not between its values: AppSync refuses that.
    """
    text = SCHEMA.read_text()
    start = text.index("enum Channel {")
    comment = text[text.rindex("\n\n", 0, start) : start]
    assert "deprecated" in comment
    for name in DEPRECATED_SPEECH_CHANNELS:
        assert name in comment, name


def test_no_enum_holds_a_comment_between_its_values() -> None:
    """AppSync's schema parser refuses a `#` comment inside an enum body.

    graphql-core accepts it, so only a deploy would find out: the schema update
    fails with "Failed to parse schema document" and the stack rolls back.
    """
    for name, body in re.findall(r"\benum\s+(\w+)\s*\{([^}]*)\}", SCHEMA.read_text()):
        assert "#" not in body, f"enum {name} has a comment between its values"


def test_a_new_segment_may_carry_either_name_of_a_speech_channel(schema) -> None:
    """addTranscriptSegment's input is the enum, so an older producer still validates."""
    channel = schema.get_type("AddTranscriptSegmentInput").fields["Channel"].type.of_type
    assert SPEECH_CHANNELS | DEPRECATED_SPEECH_CHANNELS <= set(channel.values)


@pytest.mark.parametrize("type_name", SENTIMENT_BY_CHANNEL_TYPES)
def test_sentiment_is_keyed_by_both_names_of_each_speech_channel(schema, type_name) -> None:
    fields = set(schema.get_type(type_name).fields)
    assert SPEECH_CHANNELS | DEPRECATED_SPEECH_CHANNELS <= fields


def _selected_sentiment_fields(text: str) -> set[str]:
    """The fields selected under OverallSentiment in a GraphQL document."""
    block = re.search(r"OverallSentiment\s*\{([^}]*)\}", text)
    return set(block.group(1).split()) if block else set()


def _ui_operations_selecting_sentiment() -> list[Path]:
    return sorted(p for p in UI_QUERIES.glob("*.js") if "OverallSentiment" in p.read_text())


def test_the_ui_operations_selecting_sentiment_were_found() -> None:
    names = {p.stem for p in _ui_operations_selecting_sentiment()}
    assert {"getCall", "listCallsDateRange", "onUpdateCall"} <= names


@pytest.mark.parametrize(
    "path",
    [*_ui_operations_selecting_sentiment(), *PY_SENTIMENT_SELECTIONS],
    ids=lambda p: p.stem if p.suffix == ".js" else p.parent.name,
)
def test_every_sentiment_selection_reads_both_names(path: Path) -> None:
    """A meeting written today has REMOTE/LOCAL; one stored earlier has CALLER/AGENT.

    The mutations' selections must also match onUpdateCall's, which AppSync fills
    from each mutation's response.
    """
    text = path.read_text()
    selected = _selected_sentiment_fields(text)
    assert selected == SPEECH_CHANNELS | DEPRECATED_SPEECH_CHANNELS, path
    quarter = re.search(r"QUARTER\s*\{(.*?)\n\s*\}\s*\n\s*\}", text, re.S)
    assert quarter, path
    for name in SPEECH_CHANNELS | DEPRECATED_SPEECH_CHANNELS:
        assert re.search(rf"\b{name}\s*\{{", quarter.group(1)), (path, name)


@pytest.mark.parametrize(
    "field_name", ["getTranscriptSegments", "getTranscriptSegmentsWithSentiment"]
)
def test_each_segment_query_keeps_both_names_of_the_speech_channels(field_name: str) -> None:
    """The only channel the filter excludes is the retired one."""
    text = _request_template(field_name).read_text()
    filter_block = text[text.index('"filter"') :]
    for name in SPEECH_CHANNELS | DEPRECATED_SPEECH_CHANNELS:
        assert f'"{name}"' not in filter_block, name
    assert re.findall(r":\w+", filter_block.split('"expression"')[1].split("\n")[0]) == [
        ":isPartial",
        ":retiredChannel",
    ]


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


def _evaluated_request(response: dict) -> dict:
    """The request an EvaluateMappingTemplate call rendered.

    The templates keep trailing commas, which AppSync accepts and the json
    module does not, so they are removed before parsing.
    """
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", response["evaluationResult"]))


# The older name of each speech channel and the name addTranscriptSegment stores it under.
OLDER_TO_CURRENT = {"CALLER": "REMOTE", "AGENT": "LOCAL"}


def test_add_transcript_segment_stores_each_older_speech_name_under_the_current_one() -> None:
    """A producer writing segments through the API may still send CALLER or AGENT.

    The request template rewrites the input's Channel before building the
    PutItem, so the stored segment carries the current name, as one written
    through the Kinesis path does.
    """
    text = _request_template("addTranscriptSegment", type_name="Mutation").read_text()
    put_item = text.index('"operation": "PutItem"')
    assert sorted(OLDER_TO_CURRENT) == sorted(DEPRECATED_SPEECH_CHANNELS)
    for older, current in OLDER_TO_CURRENT.items():
        rewrite = re.search(
            rf'#(?:else)?if\(\s*\$ctx\.args\.input\.Channel == "{older}"\s*\)\s*\n'
            rf'\s*\$util\.qr\(\$ctx\.args\.input\.put\("Channel", "{current}"\)\)',
            text,
        )
        assert rewrite, f"{older} is not stored as {current}"
        assert rewrite.end() < put_item, f"the {older} rewrite comes after the PutItem"


@pytest.mark.skipif(
    os.environ.get("LMA_EVALUATE_APPSYNC") != "1",
    reason="evaluates templates with the AppSync API; set LMA_EVALUATE_APPSYNC=1 to run",
)
@pytest.mark.parametrize("sent", [*OLDER_TO_CURRENT, *SPEECH_CHANNELS, "AGENT_ASSISTANT"])
def test_the_evaluated_segment_write_stores_the_current_channel_name(sent: str) -> None:
    import boto3  # imported here so the module collects without boto3 when skipped

    client = boto3.client("appsync", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    response = client.evaluate_mapping_template(
        template=_request_template("addTranscriptSegment", type_name="Mutation").read_text(),
        context=json.dumps(
            {
                "arguments": {
                    "input": {
                        "CallId": "c1",
                        "SegmentId": "s1",
                        "Channel": sent,
                        "StartTime": 0.0,
                        "EndTime": 1.0,
                        "Transcript": "hello",
                        "IsPartial": False,
                    }
                }
            }
        ),
    )
    assert not response.get("error"), response.get("error")
    stored = _evaluated_request(response)["attributeValues"]["Channel"]
    assert stored == {"S": OLDER_TO_CURRENT.get(sent, sent)}


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
    request = _evaluated_request(response)
    query_filter = request["filter"]
    assert query_filter["expressionNames"]["#Channel"] == "Channel"
    assert query_filter["expressionValues"][":retiredChannel"] == {"S": RETIRED_CHANNEL}
    assert query_filter["expressionValues"][":isPartial"] == {"BOOL": False}
