# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""How the AppSync resolvers read a meeting's SharedWith (no AWS).

SharedWith is stored in several shapes -- a DynamoDB List (shareCall,
shareTranscriptSegment), a string set, and a comma-separated or "[a, b]" String
(older meeting rows; transcript segments and key frames, which take the
meeting's SharedWith as getCall returns it; Virtual Participant rows). Every
resolver that decides who may see or change a meeting reads it as a list of
whole entries, in one of four shared blocks:

* "SharedWith entries" (VTL) -- the read, in every template that checks access
  or publishes the value;
* "SharedWith published" (VTL) and `publishedSharedWith` (APPSYNC_JS) -- the
  value every message published to a subscription.js subscription carries,
  "[a, b]", which is what lets the filter match whole entries by delimiters;
* "SharedWith write condition" (VTL) -- the condition of the three meeting
  writes signed-in users reach, after authorizeCallWrite has read the meeting.

The tests here hold each block to one text, check that every resolver that
should use one does, and check the subscription filter against the published
form. The templates' behaviour is evaluated by AppSync itself in
`test_shared_with_access_evaluated.py`, which runs only when asked to.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

AI_STACK = Path(__file__).resolve().parents[1]
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
APPSYNC = AI_STACK / "source" / "appsync"
SCHEMA = APPSYNC / "schema.graphql"

ENTRIES_TEMPLATES = [
    "getCall.response.vtl",
    "getTranscriptSegments.response.vtl",
    "getVirtualParticipant.response.vtl",
    "listVirtualParticipants.response.vtl",
    "authorizeCallWrite.response.vtl",
    "dDbPutCondition.response.vtl",
    "deleteCall.response.vtl",
    "addVideoKeyframe.response.vtl",
    "updateVirtualParticipant.response.vtl",
]
PUBLISHING_TEMPLATES = [
    "dDbPutCondition.response.vtl",
    "deleteCall.response.vtl",
    "addVideoKeyframe.response.vtl",
    "updateVirtualParticipant.response.vtl",
]
PUBLISHING_JS = ["shareMeetings.js", "endVirtualParticipant.js"]
WRITE_TEMPLATES = [
    "updateSpeakerIdentities.request.vtl",
    "updateCallSummaryText.request.vtl",
    "updateCallAgenda.request.vtl",
]


class _CfnLoader(yaml.SafeLoader):
    """YAML loader that tolerates CloudFormation short-form intrinsics."""


def _intrinsic(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):  # noqa: ANN202
    name = f"Fn::{tag_suffix}" if tag_suffix != "Ref" else "Ref"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


_CfnLoader.add_multi_constructor("!", _intrinsic)


@pytest.fixture(scope="module")
def resources() -> dict:
    return yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_CfnLoader)["Resources"]


def _read(name: str) -> str:
    return (APPSYNC / name).read_text(encoding="utf-8")


def _block(text: str, begin: str, end: str) -> str:
    assert text.count(begin) == 1 and text.count(end) == 1, (begin, end)
    return text[text.index(begin) : text.index(end) + len(end)]


def _vtl_block(name: str, title: str) -> str:
    return _block(_read(name), f"## BEGIN {title}", f"## END {title}")


# ── one text per block ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title,files",
    [
        ("SharedWith entries", ENTRIES_TEMPLATES),
        ("SharedWith published", PUBLISHING_TEMPLATES),
        ("SharedWith write condition", WRITE_TEMPLATES),
    ],
)
def test_each_shared_block_has_one_text(title: str, files: list) -> None:
    blocks = {name: _vtl_block(name, title) for name in files}
    first = blocks[files[0]]
    for name, block in blocks.items():
        assert block == first, f"{name}: the {title!r} block has drifted from {files[0]}"


def test_the_published_form_has_one_text_in_the_js_resolvers() -> None:
    blocks = [
        _block(_read(name), "// BEGIN publishedSharedWith", "// END publishedSharedWith")
        for name in PUBLISHING_JS
    ]
    assert blocks[0] == blocks[1]


def test_every_template_that_has_a_block_is_listed_here() -> None:
    """A new template that copies a block is held to the same text only if it
    is named above."""
    for title, files in (
        ("SharedWith entries", ENTRIES_TEMPLATES),
        ("SharedWith published", PUBLISHING_TEMPLATES),
        ("SharedWith write condition", WRITE_TEMPLATES),
    ):
        having = sorted(
            p.name for p in APPSYNC.glob("*.vtl") if f"## BEGIN {title}" in p.read_text()
        )
        assert having == sorted(files), title


def test_the_entries_block_reads_every_stored_shape() -> None:
    block = _vtl_block(ENTRIES_TEMPLATES[0], "SharedWith entries")
    assert "$util.isList($sharedWithRaw)" in block  # a List, and a string set as AppSync reads it
    assert "$util.isString($sharedWithRaw)" in block
    assert '.startsWith("[")' in block and '.endsWith("]")' in block
    assert '.split(",")' in block
    assert ".trim()" in block


# ── every access decision uses the entries ──────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "getCall.response.vtl",
        "getTranscriptSegments.response.vtl",
        "getVirtualParticipant.response.vtl",
        "listVirtualParticipants.response.vtl",
        "authorizeCallWrite.response.vtl",
    ],
)
def test_an_access_check_compares_whole_entries(name: str) -> None:
    text = _read(name)
    assert "$sharedWithEntries.contains(" in text
    assert re.search(r"SharedWith\.contains\(", text) is None


def _inline_templates(resources: dict) -> list:
    out = []
    for logical_id, resource in resources.items():
        if not resource.get("Type", "").startswith("AWS::AppSync::"):
            continue
        props = resource.get("Properties", {})
        for key in ("RequestMappingTemplate", "ResponseMappingTemplate"):
            value = props.get(key)
            if isinstance(value, dict):
                value = value.get("Fn::Sub", "")
            if isinstance(value, str):
                out.append((logical_id, key, value))
    return out


def test_contains_on_shared_with_is_limited_to_list_types(resources: dict) -> None:
    """`contains` on a String compares substrings, so it appears only where the
    value is known to be a List or string set."""
    sources = [(p.name, p.read_text(encoding="utf-8")) for p in APPSYNC.glob("*.vtl")]
    sources += [(f"{lid}.{key}", text) for lid, key, text in _inline_templates(resources)]
    for name, text in sources:
        assert re.search(r"SharedWith\.contains\(", text) is None, name
        for match in re.finditer(r"contains\(#SharedWith", text):
            line = text[text.rindex("\n", 0, match.start()) : text.index("\n", match.end())]
            assert "attribute_type(#SharedWith, :sharedWithList)" in line, (name, line)


def test_the_only_subscription_filter_using_contains_reads_a_list() -> None:
    """unshareCallSubscription.js filters UnshareCallOutput.SharedWith, a
    [String!] -- `contains` there is element membership. Every other filter is
    in subscription.js and matches the published "[a, b]" form."""
    schema = SCHEMA.read_text(encoding="utf-8")
    unshare_output = schema[schema.index("type UnshareCallOutput") :]
    unshare_output = unshare_output[: unshare_output.index("\n}")]
    assert re.search(r"SharedWith:\s*\[String!\]", unshare_output)
    # Any `contains` on SharedWith other than subscription.js's delimited
    # patterns (", <name>, " and ", <name>]"), however the operand is spelled.
    bare_contains = re.compile(r"SharedWith\s*:\s*\{\s*contains\s*:(?!\s*`, )")
    found = []
    for path in APPSYNC.glob("*.js"):
        if bare_contains.search(path.read_text(encoding="utf-8")):
            found.append(path.name)
    assert found == ["unshareCallSubscription.js"], found


# ── the three writes signed-in users reach ──────────────────────────────────


@pytest.mark.parametrize(
    "resolver_id,function_id,template",
    [
        (
            "UpdateSpeakerIdentitiesAppSyncResolver",
            "UpdateSpeakerIdentitiesAppSyncFunction",
            "updateSpeakerIdentities.request.vtl",
        ),
        (
            "UpdateCallSummaryTextAppSyncResolver",
            "UpdateCallSummaryTextAppSyncFunction",
            "updateCallSummaryText.request.vtl",
        ),
        (
            "UpdateCallAgendaAppSyncResolver",
            "UpdateCallAgendaAppSyncFunction",
            "updateCallAgenda.request.vtl",
        ),
    ],
)
def test_a_meeting_write_reads_the_meeting_first(
    resources: dict, resolver_id: str, function_id: str, template: str
) -> None:
    resolver = resources[resolver_id]["Properties"]
    assert resolver["Kind"] == "PIPELINE"
    assert resolver["PipelineConfig"]["Functions"] == [
        {"Fn::GetAtt": "AuthorizeCallWriteAppSyncFunction.FunctionId"},
        {"Fn::GetAtt": f"{function_id}.FunctionId"},
    ]
    write = resources[function_id]["Properties"]
    assert write["RequestMappingTemplateS3Location"].endswith(template)
    assert write["ResponseMappingTemplateS3Location"].endswith("dDbPutCondition.response.vtl")
    assert (
        write["DataSourceName"]
        == resources["AuthorizeCallWriteAppSyncFunction"]["Properties"]["DataSourceName"]
    )


def test_the_read_before_a_write_is_of_the_meeting_row(resources: dict) -> None:
    function = resources["AuthorizeCallWriteAppSyncFunction"]["Properties"]
    assert function["RequestMappingTemplateS3Location"].endswith("authorizeCallWrite.request.vtl")
    assert function["ResponseMappingTemplateS3Location"].endswith("authorizeCallWrite.response.vtl")
    request = _read("authorizeCallWrite.request.vtl")
    assert '"operation" : "GetItem"' in request
    assert '"c#${ctx.args.input.CallId}"' in request
    assert '"consistentRead": true' in request


def test_the_write_condition_restates_each_finding() -> None:
    block = _vtl_block(WRITE_TEMPLATES[0], "SharedWith write condition")
    assert '$ctx.stash.callerAccess == "shared-list"' in block
    assert '$ctx.stash.callerAccess == "shared-text"' in block
    assert (
        "(attribute_type(#SharedWith, :sharedWithList) OR attribute_type(#SharedWith, "
        ":sharedWithSet)) AND contains(#SharedWith, :caller)"
    ) in block
    assert "#SharedWith = :sharedWithAsRead" in block
    assert '#set( $condition = "${condition} AND #Owner = :caller" )' in block
    response = _read("authorizeCallWrite.response.vtl")
    for access in ('"owner"', '"shared-list"', '"shared-text"', '"none"'):
        assert access in response


# ── every subscription filtered on SharedWith is fed the published form ─────


def _subscriptions(schema: str) -> dict:
    """Subscription field -> the mutations it is subscribed to."""
    body = schema[schema.index("type Subscription {") :]
    body = body[: body.index("\n}")]
    out = {}
    for match in re.finditer(
        r"(\w+)(?:\([^)]*\))?:\s*\w+\s*@aws_subscribe\(mutations:\s*\[([^\]]*)\]", body
    ):
        out[match.group(1)] = re.findall(r'"(\w+)"', match.group(2))
    return out


def _resolver_for(resources: dict, type_name: str, field: str) -> dict | None:
    for resource in resources.values():
        if resource.get("Type") != "AWS::AppSync::Resolver":
            continue
        props = resource["Properties"]
        if props.get("TypeName") == type_name and props.get("FieldName") == field:
            return props
    return None


def _output_has_shared_with(schema: str, mutation: str) -> bool:
    match = re.search(rf"\b{mutation}\([^)]*\):\s*(\w+)", schema)
    assert match, mutation
    output = schema[schema.index(f"type {match.group(1)} ") :]
    output = output[: output.index("\n}")]
    return re.search(r"^\s*SharedWith:", output, re.MULTILINE) is not None


def _publishes(resources: dict, props: dict) -> bool:
    if props.get("Kind") == "PIPELINE":
        last_id = props["PipelineConfig"]["Functions"][-1]["Fn::GetAtt"].split(".")[0]
        props = resources[last_id]["Properties"]
    response = props.get("ResponseMappingTemplateS3Location", "")
    code = props.get("CodeS3Location", "")
    return any(response.endswith(name) for name in PUBLISHING_TEMPLATES) or any(
        code.endswith(name) for name in PUBLISHING_JS
    )


def test_every_mutation_a_filtered_subscription_receives_publishes_shared_with(
    resources: dict,
) -> None:
    schema = SCHEMA.read_text(encoding="utf-8")
    checked = []
    for subscription, mutations in _subscriptions(schema).items():
        resolver = _resolver_for(resources, "Subscription", subscription)
        if not resolver or not resolver.get("CodeS3Location", "").endswith("/subscription.js"):
            continue
        for mutation in mutations:
            if not _output_has_shared_with(schema, mutation):
                continue
            props = _resolver_for(resources, "Mutation", mutation)
            assert props, mutation
            response = props.get("ResponseMappingTemplateS3Location", "")
            if response.endswith("createCall.response.vtl"):
                # Answers with CallId and Owner only, so the message carries no
                # SharedWith and reaches the owner and Admins alone.
                assert "SharedWith" not in _read("createCall.response.vtl")
                continue
            assert _publishes(resources, props), f"{mutation} (published to {subscription})"
            checked.append(mutation)
    # The set is large; make sure the parse found it rather than nothing.
    for mutation in (
        "updateCallStatus",
        "addTranscriptSegment",
        "addVideoKeyframe",
        "shareMeetings",
    ):
        assert mutation in checked
    for mutation in ("deleteCall", "updateVirtualParticipant", "endVirtualParticipant"):
        assert mutation in checked


# ── the filter against the published form ───────────────────────────────────


def _filter_rules(username: str) -> list:
    """The SharedWith rules subscription.js sets, read from its source."""
    text = _read("subscription.js")
    rules = re.findall(r"\{ SharedWith: \{ (eq|beginsWith|contains): `([^`]*)` \} \}", text)
    assert len(rules) == 4, rules
    return [(op, pattern.replace("${username}", username)) for op, pattern in rules]


def _matches(published: str, username: str) -> bool:
    """AppSync's string operators: eq is equality, beginsWith a prefix test and
    contains a substring test."""
    for op, pattern in _filter_rules(username):
        if op == "eq" and published == pattern:
            return True
        if op == "beginsWith" and published.startswith(pattern):
            return True
        if op == "contains" and pattern in published:
            return True
    return False


def _published(entries: list) -> str:
    """What the "SharedWith published" block writes for these entries."""
    return "[" + ", ".join(entries) + "]"


BOB = "bob@example.com"


@pytest.mark.parametrize(
    "entries,expected",
    [
        ([BOB], True),
        ([BOB, "carol@example.com"], True),
        (["alice@example.com", BOB, "carol@example.com"], True),
        (["alice@example.com", BOB], True),
        (["x" + BOB], False),
        ([BOB + "x"], False),
        (["alice@example.com", "x" + BOB], False),
        (["alice@example.com", BOB + "x"], False),
        (["x" + BOB, "carol@example.com"], False),
        ([BOB + "x", "carol@example.com"], False),
        (["alice@example.com", BOB + "x", "carol@example.com"], False),
        (["alice@example.com"], False),
    ],
)
def test_the_filter_matches_a_whole_entry_of_the_published_form(
    entries: list, expected: bool
) -> None:
    assert _matches(_published(entries), BOB) is expected


def test_the_filter_keeps_the_owner_rule() -> None:
    assert "{ Owner: { eq: username } }" in _read("subscription.js")
