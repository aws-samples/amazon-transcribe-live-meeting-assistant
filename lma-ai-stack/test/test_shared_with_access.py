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

The Python Lambdas that read SharedWith copy one function, `shared_with_entries`,
which reads it on the same rule.

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
    "getTranscriptSegmentsWithSentiment.response.vtl",
    "getVirtualParticipant.response.vtl",
    "listVirtualParticipants.response.vtl",
    "authorizeCallWrite.response.vtl",
    "dDbPutCondition.response.vtl",
    "deleteCall.response.vtl",
    "addVideoKeyframe.response.vtl",
    "updateVirtualParticipant.response.vtl",
    "unshareVirtualParticipant.request.vtl",
]
PUBLISHING_TEMPLATES = [
    "dDbPutCondition.response.vtl",
    "deleteCall.response.vtl",
    "addVideoKeyframe.response.vtl",
    "updateVirtualParticipant.response.vtl",
]
PUBLISHING_JS = ["shareMeetings.js", "endVirtualParticipant.js"]
ENTRIES_JS = [*PUBLISHING_JS, "meetingAccess.js"]
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


@pytest.mark.parametrize(
    "title,files",
    [("sharedWithEntries", ENTRIES_JS), ("publishedSharedWith", PUBLISHING_JS)],
)
def test_each_js_block_has_one_text(title: str, files: list) -> None:
    blocks = [_block(_read(name), f"// BEGIN {title}", f"// END {title}") for name in files]
    assert all(block == blocks[0] for block in blocks), title


def test_every_js_resolver_that_has_a_block_is_listed_here() -> None:
    for title, files in (("sharedWithEntries", ENTRIES_JS), ("publishedSharedWith", PUBLISHING_JS)):
        having = sorted(
            p.name for p in APPSYNC.glob("*.js") if f"// BEGIN {title}" in p.read_text()
        )
        assert having == sorted(files), title


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
    assert '.split(",")' in block
    assert ".trim()" in block


def test_brackets_and_commas_only_separate_entries() -> None:
    """A String is split at every "[", "]" and ","; a List element holding one
    is not an entry. So no entry contains a separator, in the VTL read and in
    the JS one."""
    block = _vtl_block(ENTRIES_TEMPLATES[0], "SharedWith entries")
    assert '$sharedWithRaw.replace("[", ",").replace("]", ",")' in block
    for separator in ("[", "]", ","):
        assert f'!$sharedWithEntry.contains("{separator}")' in block
    js = _block(_read(ENTRIES_JS[0]), "// BEGIN sharedWithEntries", "// END sharedWithEntries")
    assert "value.split('[').join(',').split(']').join(',').split(',')" in js
    assert "['[', ']', ','].some((separator) => entry.includes(separator))" in js


@pytest.mark.parametrize("name", ENTRIES_TEMPLATES)
def test_each_item_is_read_on_its_own_shared_with(name: str) -> None:
    """A Velocity #set whose value is null leaves the variable as it was, so the
    value read is defaulted: in a loop over items, an item without SharedWith
    must not be judged on the previous item's."""
    assignments = re.findall(r"#set\( \$sharedWithRaw = (.+) \)", _read(name))
    assert len(assignments) == 1, assignments
    assert assignments[0].startswith("$util.defaultIfNull(") and assignments[0].endswith(', "")')


# ── every access decision uses the entries ──────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        "getCall.response.vtl",
        "getTranscriptSegments.response.vtl",
        "getTranscriptSegmentsWithSentiment.response.vtl",
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
            if mutation == "createVirtualParticipant":
                # A new row is shared with nobody, so its message reaches the
                # owner and Admins alone.
                assert '$util.qr($input.put("SharedWith", ""))' in props["RequestMappingTemplate"]
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


def test_every_subscription_has_a_resolver(resources: dict) -> None:
    """A subscription field without a resolver is sent every message published
    to it, so each one has a resolver that decides who receives them."""
    schema = SCHEMA.read_text(encoding="utf-8")
    subscriptions = _subscriptions(schema)
    assert "onCreateVirtualParticipant" in subscriptions and "onAddChatToken" in subscriptions
    missing = [s for s in subscriptions if _resolver_for(resources, "Subscription", s) is None]
    assert missing == []


def test_new_virtual_participants_are_filtered_as_updates_are(resources: dict) -> None:
    for field in ("onCreateVirtualParticipant", "onUpdateVirtualParticipant"):
        resolver = _resolver_for(resources, "Subscription", field)
        assert resolver["CodeS3Location"].endswith("/subscription.js"), field


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


def test_the_filter_matches_a_name_holding_a_separator_as_the_owner_only() -> None:
    """No published entry holds "[", "]" or ",", so the SharedWith rules are
    set only for a name without one."""
    text = _read("subscription.js")
    guard = "!['[', ']', ','].some((separator) => username.includes(separator))"
    assert guard in text
    assert text.index(guard) < text.index("{ SharedWith: { eq:")


# ── subscriptions and writes answered by the getCall rule ───────────────────


@pytest.mark.parametrize("field", ["onSendChatMessage", "onAddChatToken", "onVNCPreviewToggle"])
def test_a_subscription_without_shared_with_reads_the_meeting_it_names(
    resources: dict, field: str
) -> None:
    resolver = _resolver_for(resources, "Subscription", field)
    assert resolver["CodeS3Location"].endswith("/meetingAccess.js")
    # The meeting table, which getCall reads.
    get_call = _resolver_for(resources, "Query", "getCall")
    assert resolver["DataSourceName"] == get_call["DataSourceName"]
    code = _read("meetingAccess.js")
    assert "operation: 'GetItem'" in code
    assert "const key = `c#${callId}`;" in code
    assert (
        "meeting.Owner === username || sharedWithEntries(meeting.SharedWith).includes(username)"
        in code
    )


def test_a_vnc_preview_toggle_is_written_after_the_meeting_access_check(resources: dict) -> None:
    resolver = _resolver_for(resources, "Mutation", "toggleVNCPreview")
    assert resolver["Kind"] == "PIPELINE"
    assert resolver["PipelineConfig"]["Functions"] == [
        {"Fn::GetAtt": "MeetingAccessAppSyncFunction.FunctionId"},
        {"Fn::GetAtt": "ToggleVNCPreviewAppSyncFunction.FunctionId"},
    ]
    assert "return ctx.prev.result;" in resolver["Code"]
    check = resources["MeetingAccessAppSyncFunction"]["Properties"]
    write = resources["ToggleVNCPreviewAppSyncFunction"]["Properties"]
    assert check["CodeS3Location"].endswith("/meetingAccess.js")
    assert write["CodeS3Location"].endswith("/toggleVNCPreview.js")
    get_call = _resolver_for(resources, "Query", "getCall")
    assert check["DataSourceName"] == get_call["DataSourceName"]
    # The mutation names the meeting in its input.
    assert "ctx.args.CallId || (ctx.args.input && ctx.args.input.CallId)" in _read(
        "meetingAccess.js"
    )


# ── Virtual Participant writes ──────────────────────────────────────────────

VP_OWNER_WRITES = [
    "shareVirtualParticipant",
    "unshareVirtualParticipant",
    "updateVirtualParticipant",
    "acknowledgeVPFailure",
]


def _write_props(resources: dict, field: str) -> dict:
    """The properties of the resolver, or of the last function of a pipeline
    resolver, which is the one that writes."""
    props = _resolver_for(resources, "Mutation", field)
    assert props, field
    if props.get("Kind") == "PIPELINE":
        last_id = props["PipelineConfig"]["Functions"][-1]["Fn::GetAtt"].split(".")[0]
        props = resources[last_id]["Properties"]
    return props


def _request_template(resources: dict, field: str) -> str:
    props = _write_props(resources, field)
    location = props.get("RequestMappingTemplateS3Location")
    if location:
        return _read(Path(location).name)
    return props["RequestMappingTemplate"]


def _response_template(resources: dict, field: str) -> str:
    props = _write_props(resources, field)
    return props["ResponseMappingTemplate"]


def test_each_virtual_participant_write_has_the_owner_condition(resources: dict) -> None:
    title = "Virtual Participant owner condition"
    blocks = [
        _block(_request_template(resources, f), f"## BEGIN {title}", f"## END {title}")
        for f in VP_OWNER_WRITES
    ]
    assert all(block == blocks[0] for block in blocks)
    block = blocks[0]
    assert '$ctx.identity.userArn.contains(":assumed-role/")' in block
    assert '$ctx.identity.groups.contains("Admin")' in block
    assert '$util.qr( $conditions.add("#conditionOwner = :conditionCaller") )' in block
    assert '$util.qr( $conditionNames.put("#conditionOwner", "Owner") )' in block


@pytest.mark.parametrize("field", VP_OWNER_WRITES)
def test_a_virtual_participant_write_carries_its_conditions(resources: dict, field: str) -> None:
    template = _request_template(resources, field)
    assert '"expression": "$conditionExpression"' in template
    assert template.index("## END Virtual Participant owner condition") < template.index(
        '#set( $conditionExpression = "" )'
    )
    if field != "updateVirtualParticipant":
        # UpdateItem creates a missing row; sharing one is refused instead.
        assert '#set( $conditions = ["attribute_exists(#conditionId)"] )' in template


@pytest.mark.parametrize("field", [f for f in VP_OWNER_WRITES if f != "updateVirtualParticipant"])
def test_a_refused_virtual_participant_write_says_why(resources: dict, field: str) -> None:
    response = _response_template(resources, field)
    assert '$ctx.error.type == "DynamoDB:ConditionalCheckFailedException"' in response
    assert "is not authorized to change Virtual Participant" in response
    assert "was not found" in response


def test_unsharing_reads_the_row_before_writing_what_remains(resources: dict) -> None:
    resolver = _resolver_for(resources, "Mutation", "unshareVirtualParticipant")
    assert resolver["Kind"] == "PIPELINE"
    assert resolver["PipelineConfig"]["Functions"] == [
        {"Fn::GetAtt": "ReadVirtualParticipantSharingAppSyncFunction.FunctionId"},
        {"Fn::GetAtt": "UnshareVirtualParticipantAppSyncFunction.FunctionId"},
    ]
    read = resources["ReadVirtualParticipantSharingAppSyncFunction"]["Properties"]
    write = resources["UnshareVirtualParticipantAppSyncFunction"]["Properties"]
    assert read["DataSourceName"] == write["DataSourceName"]
    assert '"operation": "GetItem"' in read["RequestMappingTemplate"]
    assert '"consistentRead": true' in read["RequestMappingTemplate"]
    assert (
        '$ctx.stash.put("sharedWithAsRead", $ctx.result.SharedWith)'
        in (read["ResponseMappingTemplate"])
    )
    template = _request_template(resources, "unshareVirtualParticipant")
    assert '#set( $sharedWithRaw = $util.defaultIfNull($ctx.stash.sharedWithAsRead, "") )' in (
        template
    )
    assert "!$sharedWithRemoved.contains($sharedWithEntry)" in template
    assert '"#conditionSharedWith = :conditionSharedWithAsRead"' in template
    assert '"attribute_not_exists(#conditionSharedWith)"' in template


def test_a_virtual_participant_is_shared_with_entries_free_of_separators(resources: dict) -> None:
    template = _request_template(resources, "shareVirtualParticipant")
    block = _block(
        template, "## BEGIN SharedWith entries written", "## END SharedWith entries written"
    )
    assert (
        '$recipientText.contains("[") || $recipientText.contains("]") || '
        '$recipientText.contains(",")'
    ) in block
    assert "BadRequest" in block


def test_a_meeting_is_shared_with_entries_free_of_separators(resources: dict) -> None:
    template = _request_template(resources, "shareCall")
    if isinstance(template, dict):
        template = template["Fn::Sub"]
    assert (
        '$recipient.contains("[") || $recipient.contains("]") || $recipient.contains(",")'
    ) in template
    assert template.index("BadRequest") < template.index('"TransactWriteItems"')


# ── what a new Virtual Participant's message carries ────────────────────────

REPO = AI_STACK.parent
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts"}
SKIPPED_PARTS = {"node_modules", "dist", "build", ".aws-sam"}


def _create_vp_selections() -> list:
    """(path, selection set) of every createVirtualParticipant mutation the
    repository sends. A subscription message carries only the fields the
    mutation selected, and the onCreateVirtualParticipant filter reads Owner."""
    found = []
    for directory in ("lma-ai-stack/source", "lib", "lma-virtual-participant-stack", "utilities"):
        for path in (REPO / directory).rglob("*"):
            if path.suffix not in SOURCE_SUFFIXES or SKIPPED_PARTS & set(path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in re.finditer(r"createVirtualParticipant\(input: \$\w+\)\s*\{", text):
                depth, end = 1, match.end()
                while depth:
                    depth += {"{": 1, "}": -1}.get(text[end], 0)
                    end += 1
                found.append((path.relative_to(REPO).as_posix(), text[match.end() : end - 1]))
    return found


def test_every_virtual_participant_create_selects_owner_and_shared_with() -> None:
    selections = _create_vp_selections()
    paths = {path for path, _ in selections}
    for expected in (
        "lma-ai-stack/source/lambda_functions/mcp_analytics/tools/schedule_meeting.py",
        "lma-ai-stack/source/lambda_functions/mcp_analytics/tools/start_meeting_now.py",
        "lma-ai-stack/source/ui/src/components/virtual-participant-layout/VirtualParticipantList.jsx",
        "lib/lma_sdk/lma_sdk/operations/virtual_participant.py",
    ):
        assert expected in paths, expected
    for path, selection in selections:
        fields = set(re.findall(r"\b\w+\b", selection))
        assert {"Owner", "SharedWith"} <= fields, path


# ── the Python Lambdas that read SharedWith ─────────────────────────────────

LAMBDA_FUNCTIONS = AI_STACK / "source" / "lambda_functions"
PYTHON_READERS = [
    "agenda_deriver/index.py",
    "call_cost_resolver/index.py",
    "list_calls_gsi_resolver/index.py",
    "mcp_analytics/tools/get_virtual_participant_status.py",
    "meeting_controls_resolver/index.py",
    "microvm_vnc_token/vp_access.py",
    "speaker_name_resolver/index.py",
    "summary_regenerator/index.py",
    "video_keyframes_resolver/index.py",
    "vnc_edge_token/vp_access.py",
]
PY_BEGIN = "# BEGIN shared_with_entries"
PY_END = "# END shared_with_entries"


def _python_block(rel: str) -> str:
    return _block((LAMBDA_FUNCTIONS / rel).read_text(encoding="utf-8"), PY_BEGIN, PY_END)


def _lambda_sources() -> list:
    return [
        p
        for p in LAMBDA_FUNCTIONS.rglob("*.py")
        if not p.name.startswith("test_") and not SKIPPED_PARTS & set(p.parts)
    ]


def test_each_python_reader_has_one_text() -> None:
    """Each Lambda is packaged on its own, so the rule is copied into each one
    that reads SharedWith; the copies are held to one text."""
    blocks = {rel: _python_block(rel) for rel in PYTHON_READERS}
    first = blocks[PYTHON_READERS[0]]
    for rel, block in blocks.items():
        assert block == first, f"{rel}: shared_with_entries has drifted from {PYTHON_READERS[0]}"


def test_every_python_reader_is_listed_here() -> None:
    having = sorted(
        p.relative_to(LAMBDA_FUNCTIONS).as_posix()
        for p in _lambda_sources()
        if PY_BEGIN in p.read_text(encoding="utf-8")
    )
    assert having == sorted(PYTHON_READERS)


def test_no_lambda_splits_shared_with_outside_the_shared_rule() -> None:
    """The ad hoc forms the shared rule stands in for appear nowhere else."""
    for path in _lambda_sources():
        text = path.read_text(encoding="utf-8")
        if PY_BEGIN in text:
            text = text.replace(_block(text, PY_BEGIN, PY_END), "")
        for form in ('.strip("[]")', '[1:-1].split(","', 'shared.split(",")'):
            assert form not in text, (path.relative_to(LAMBDA_FUNCTIONS).as_posix(), form)


@pytest.fixture(scope="module")
def shared_with_entries():
    namespace: dict = {}
    exec(_python_block(PYTHON_READERS[0]), namespace)  # noqa: S102 - the repository's own source
    return namespace["shared_with_entries"]


LISTED = "bob@example.com"
OTHER_LISTED = "alice@example.com"


@pytest.mark.parametrize(
    "value,expected",
    [
        ([OTHER_LISTED, LISTED], [OTHER_LISTED, LISTED]),
        ((f" {LISTED} ",), [LISTED]),
        (f"{OTHER_LISTED},{LISTED}", [OTHER_LISTED, LISTED]),
        (f"{OTHER_LISTED}, {LISTED}", [OTHER_LISTED, LISTED]),
        (f"[{OTHER_LISTED}, {LISTED}]", [OTHER_LISTED, LISTED]),
        (f"[{LISTED}]", [LISTED]),
        (LISTED, [LISTED]),
        ("", []),
        ("[]", []),
        (None, []),
        ({"S": LISTED}, []),
        ([LISTED, None, 7, ""], [LISTED]),
    ],
)
def test_the_python_rule_reads_every_stored_shape(shared_with_entries, value, expected) -> None:
    assert shared_with_entries(value) == expected


def test_the_python_rule_reads_a_string_set(shared_with_entries) -> None:
    assert sorted(shared_with_entries({OTHER_LISTED, LISTED})) == [OTHER_LISTED, LISTED]


# The values test_shared_with_access_evaluated.py evaluates getCall against,
# with whether the listed user is an entry: the Python rule agrees with it.
@pytest.mark.parametrize(
    "value,expected",
    [
        ([f"{OTHER_LISTED}, {LISTED}"], False),
        ([f"[{LISTED}]"], False),
        ([f"{LISTED}]", OTHER_LISTED], False),
        ([f"x], {LISTED}", LISTED], True),
        (f"[x], {LISTED}]", True),
        (f"{OTHER_LISTED}][{LISTED}", True),
        (f"[[{LISTED}]]", True),
    ],
)
def test_the_python_rule_never_reads_a_separator_into_an_entry(
    shared_with_entries, value, expected: bool
) -> None:
    entries = shared_with_entries(value)
    assert (LISTED in entries) is expected
    assert not any(separator in entry for entry in entries for separator in "[],")
