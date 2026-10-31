# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Static contract tests for the AppSync API (no AWS).

The GraphQL surface is defined in three places that have to agree:

1. `source/appsync/schema.graphql` — the SDL that `AppSyncSchema` uploads.
2. `deployment/lma-ai-stack.yaml` — 74 `AWS::AppSync::Resolver` resources, each
   naming a `TypeName` / `FieldName` pair and (usually) a mapping-template file.
3. `source/ui/src/graphql/` and the UI components — the operations the React app
   actually sends.

Nothing checks that they agree. A resolver attached to a field that the schema
does not declare fails at deploy time; a UI operation selecting a field the
schema does not declare fails at run time, per request, with an error the user
sees but CI never does. The integration suite's `test_appsync_reachable` proves
the endpoint answers, not that any particular field resolves.

These tests parse all three and assert the invariants. They need no AWS, no
credentials and no deployed stack, and run in under a second.

Run via `make test-appsync` or pytest directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

# graphql-core is a dev dependency of lib/lma_sdk, installed by
# `make setup-cli-dev`. Imported unconditionally on purpose: skipping the whole
# module when it is absent would let CI report green without having checked
# anything.
from graphql import build_ast_schema, parse, validate
from graphql.language.ast import (
    InterfaceTypeDefinitionNode,
    ObjectTypeDefinitionNode,
    ObjectTypeExtensionNode,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
AI_STACK = REPO_ROOT / "lma-ai-stack"
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
APPSYNC_DIR = AI_STACK / "source" / "appsync"
SCHEMA = APPSYNC_DIR / "schema.graphql"
UI_SRC = AI_STACK / "source" / "ui" / "src"

# AppSync supplies these scalars and auth directives implicitly, so the SDL in
# the repo never declares them and graphql-core cannot build it as-is. Declaring
# them here is the whole shim: nothing else about the schema is altered, so a
# field name or type that graphql-core resolves is one AppSync resolves too.
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


# ── schema ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def schema_sdl() -> str:
    return SCHEMA.read_text()


@pytest.fixture(scope="module")
def schema(schema_sdl: str):
    """The executable schema, for validating operations against."""
    return build_ast_schema(
        parse(APPSYNC_PRELUDE + schema_sdl),
        # AppSync accepts SDL that graphql-core's strict rules reject (unknown
        # directive locations, repeated auth directives). Field and type
        # resolution — which is all these tests rely on — is unaffected.
        assume_valid_sdl=True,
    )


@pytest.fixture(scope="module")
def schema_fields(schema_sdl: str) -> dict[str, set[str]]:
    """Map of type name -> declared field names, from the SDL directly.

    Read from the AST rather than the built schema so a type that only appears
    in an `extend` block is still accounted for.
    """
    fields: dict[str, set[str]] = {}
    for node in parse(schema_sdl).definitions:
        if isinstance(
            node,
            (
                ObjectTypeDefinitionNode,
                ObjectTypeExtensionNode,
                InterfaceTypeDefinitionNode,
            ),
        ):
            fields.setdefault(node.name.value, set()).update(
                field.name.value for field in (node.fields or [])
            )
    return fields


@pytest.fixture(scope="module")
def field_nodes(schema_sdl: str) -> dict[tuple[str, str], object]:
    """Map of (type name, field name) -> AST node, so directives stay readable.

    `schema_fields` above keeps only names; the group-restriction tests need the
    directives attached to each field, which the built schema does not expose
    for AppSync's auth directives.
    """
    nodes: dict[tuple[str, str], object] = {}
    for node in parse(schema_sdl).definitions:
        if isinstance(node, (ObjectTypeDefinitionNode, ObjectTypeExtensionNode)):
            for field in node.fields or []:
                nodes[(node.name.value, field.name.value)] = field
    return nodes


# ── administrative operations are pinned to the Admin group ─────────────────

# Each of these changes, or reads, something that is account-wide: one stored
# configuration record drives every meeting in the deployment, and the user
# operations act on the Cognito pool itself. They are pinned to the "Admin"
# group in the SDL, and the resolver behind each one re-checks the same claim, so
# the directive is one of two independent places the constraint lives. This test
# is what holds the SDL half in place.
ADMIN_ONLY_FIELDS = [
    ("Mutation", "updateAsrConfig"),
    ("Mutation", "updateChatButtonConfig"),
    ("Mutation", "updateLLMPromptTemplate"),
    ("Mutation", "updateNovaSonicConfig"),
    ("Mutation", "installMCPServer"),
    ("Mutation", "uninstallMCPServer"),
    ("Mutation", "updateMCPServer"),
    ("Mutation", "initOAuthFlow"),
    ("Mutation", "handleOAuthCallback"),
    ("Mutation", "createUser"),
    ("Mutation", "deleteUser"),
    ("Query", "getLLMPromptTemplate"),
    ("Query", "getNovaSonicConfig"),
    ("Query", "listUsers"),
    # The price table behind every meeting's cost estimate.
    ("Mutation", "updateCallPricing"),
    ("Query", "getCallPricing"),
    # The configuration history: who changed the account-wide configuration and
    # what it held, and putting an earlier state of it back.
    ("Query", "listConfigurationRevisions"),
    ("Query", "getConfigurationRevision"),
    ("Query", "diffConfigurationRevisions"),
    ("Mutation", "restoreConfigurationRevision"),
    ("Mutation", "labelConfigurationRevision"),
]

# Reads every signed-in user performs in the course of using the product, so the
# group argument is deliberately absent and adding one would remove a feature:
#
# * getChatButtonConfig -- the Meeting Assist chat panel renders its quick-action
#   buttons from this record for whoever is viewing the meeting.
# * getAsrConfig -- the Stream Audio and Upload Audio forms read which engine is
#   selected, because the capture sample rate and the speaker-count field depend
#   on it. The Virtual Participant task role reads it with SigV4 as well, which
#   is a separate authorization mode and would be unaffected either way.
# * listInstalledMCPServers / getMCPServer -- which servers are installed is
#   account-wide information the meeting assistant's users are meant to see;
#   managing them is what belongs to the Admin group. `redact_server` in
#   mcp_server_manager projects AuthConfig away on this path, and
#   test_mcp_server_manager.py pins both that and the open read.
UNRESTRICTED_READS = [
    ("Query", "getChatButtonConfig"),
    ("Query", "getAsrConfig"),
    ("Query", "listInstalledMCPServers"),
    ("Query", "getMCPServer"),
]

# Everything else the schema declares. Listed so that the three lists together
# have to account for every field: without this, a new administrative mutation
# added later with no group argument would pass the suite by simply not being
# mentioned anywhere, which is the drift these tests exist to catch. Membership
# here is a claim that the operation acts on one meeting, on the caller's own
# data, or is reached only by a service role with SigV4 -- not that it is
# unimportant. Moving a field onto one of the lists above is the deliberate act
# that adding it here avoids.
OTHER_FIELDS = [
    # Writes on the event-sourcing path, reached by the Kinesis consumer and the
    # Virtual Participant with SigV4 (`type Mutation @aws_iam`).
    ("Mutation", "createCall"),
    ("Mutation", "updateCallStatus"),
    ("Mutation", "updateCallAggregation"),
    ("Mutation", "updateRecordingUrl"),
    ("Mutation", "updateVideoRecordingUrl"),
    ("Mutation", "updatePcaUrl"),
    ("Mutation", "updateAgent"),
    ("Mutation", "addCallCategory"),
    ("Mutation", "addIssuesDetected"),
    ("Mutation", "addCallSummaryText"),
    ("Mutation", "addTranscriptSegment"),
    ("Mutation", "addChatToken"),
    # One meeting at a time, authorized per meeting by the resolver against its
    # owner and share list rather than by group.
    ("Mutation", "updateSpeakerIdentities"),
    ("Mutation", "resolveSpeakerNames"),
    # The summary of one meeting: updateCallSummaryText carries the owner/share
    # rule as a DynamoDB condition, and regenerateCallSummary is a Lambda
    # resolver that applies the same rule in `caller_may_edit`.
    ("Mutation", "updateCallSummaryText"),
    ("Mutation", "regenerateCallSummary"),
    ("Mutation", "deleteCall"),
    ("Mutation", "shareCall"),
    ("Mutation", "unshareCall"),
    ("Mutation", "deleteTranscriptSegment"),
    ("Mutation", "shareTranscriptSegment"),
    ("Mutation", "shareMeetings"),
    ("Mutation", "deleteMeetings"),
    ("Mutation", "createUploadMeeting"),
    ("Mutation", "sendChatMessage"),
    ("Mutation", "acknowledgeVPFailure"),
    # A user's own Virtual Participants.
    ("Mutation", "createVirtualParticipant"),
    ("Mutation", "updateVirtualParticipant"),
    ("Mutation", "endVirtualParticipant"),
    ("Mutation", "deleteVirtualParticipants"),
    ("Mutation", "shareVirtualParticipant"),
    ("Mutation", "unshareVirtualParticipant"),
    ("Mutation", "toggleVNCPreview"),
    ("Mutation", "createMicrovmVncToken"),
    ("Mutation", "createVncEdgeToken"),
    # A user acting on their own account: their voice, their credentials, their
    # browser profile, their API keys. First-party by design -- nobody may
    # consent, enrol or store credentials on another person's behalf -- so a
    # group argument would be the wrong control here.
    ("Mutation", "grantVoiceConsent"),
    ("Mutation", "enrolMyVoice"),
    ("Mutation", "revokeVoiceConsent"),
    ("Mutation", "generateMCPApiKey"),
    ("Mutation", "revokeMCPApiKey"),
    ("Mutation", "setMyZoomCredentials"),
    ("Mutation", "deleteMyZoomCredentials"),
    ("Mutation", "deleteMyChromeProfile"),
    # Reads scoped to the caller, to one meeting, or to the caller's own record.
    ("Query", "getCall"),
    # One meeting's cost estimate, authorized in the resolver by getCall's rule.
    ("Query", "getCallCost"),
    ("Query", "voiceEnrollmentTerms"),
    ("Query", "getTranscriptSegments"),
    ("Query", "getTranscriptSegmentsWithSentiment"),
    ("Query", "listCallsDateRange"),
    ("Query", "getCallCount"),
    ("Query", "queryKnowledgeBase"),
    ("Query", "listVirtualParticipants"),
    ("Query", "getVirtualParticipant"),
    ("Query", "parseMeetingInvitation"),
    ("Query", "listMCPApiKeys"),
    ("Query", "getMyZoomCredentialsStatus"),
    ("Query", "getMyChromeProfileStatus"),
]


def _cognito_groups(field) -> tuple[str, ...] | None:
    """The `cognito_groups` argument of a field's `@aws_cognito_user_pools`.

    `None` when the field carries no such directive, and an empty tuple when it
    carries the directive without a group argument.
    """
    for directive in field.directives:
        if directive.name.value != "aws_cognito_user_pools":
            continue
        for argument in directive.arguments:
            if argument.name.value == "cognito_groups":
                # A list in every current use, but the directive also accepts a
                # bare string. `values` is absent on a StringValueNode, and
                # reading it blindly would raise AttributeError from inside the
                # helper instead of failing the assertion with a message.
                values = getattr(argument.value, "values", None)
                if values is None:
                    return (argument.value.value,)
                return tuple(value.value for value in values)
        return ()
    return None


@pytest.mark.parametrize("type_name,field_name", ADMIN_ONLY_FIELDS, ids=lambda part: str(part))
def test_administrative_operation_is_restricted_to_the_admin_group(
    type_name: str, field_name: str, field_nodes: dict[tuple[str, str], object]
) -> None:
    field = field_nodes.get((type_name, field_name))
    assert field is not None, f"{SCHEMA.name} no longer declares {type_name}.{field_name}"
    groups = _cognito_groups(field)
    assert groups is not None, (
        f"{type_name}.{field_name} carries no @aws_cognito_user_pools directive"
    )
    assert "Admin" in groups, (
        f"{type_name}.{field_name} is an administrative operation, so its "
        f'@aws_cognito_user_pools directive must name cognito_groups: ["Admin"]; '
        f"it names {list(groups)}"
    )


@pytest.mark.parametrize("type_name,field_name", UNRESTRICTED_READS, ids=lambda part: str(part))
def test_read_used_by_every_signed_in_user_is_available_to_every_group(
    type_name: str, field_name: str, field_nodes: dict[tuple[str, str], object]
) -> None:
    """Narrowing one of these to a group would remove a feature for most users."""
    field = field_nodes.get((type_name, field_name))
    assert field is not None, f"{SCHEMA.name} no longer declares {type_name}.{field_name}"
    assert _cognito_groups(field) == (), (
        f"{type_name}.{field_name} is read by every signed-in user, so its "
        f"@aws_cognito_user_pools directive must not name a group"
    )


def test_every_mutation_and_query_field_is_classified(
    schema_fields: dict[str, set[str]],
) -> None:
    """The three lists above must account for every field the schema declares.

    Each of the other tests here is parametrized over one of those lists, so an
    operation nobody listed is an operation nothing checks. Adding a field to the
    schema therefore fails this test until somebody says which kind it is -- which
    is the point: for an administrative operation, the answer is a group argument
    and a resolver-side check, and the alternative to being asked is shipping
    without either.
    """
    classified = {(type_name, field) for type_name, field in ADMIN_ONLY_FIELDS}
    classified |= {(type_name, field) for type_name, field in UNRESTRICTED_READS}
    classified |= {(type_name, field) for type_name, field in OTHER_FIELDS}

    declared = {
        (type_name, field)
        for type_name in ("Mutation", "Query")
        for field in schema_fields.get(type_name, set())
    }

    assert declared, "no Mutation or Query fields found -- the SDL parser is not seeing the schema"

    unclassified = declared - classified
    assert not unclassified, (
        "these fields are in the schema but on none of the lists in this file; add "
        "each to ADMIN_ONLY_FIELDS, UNRESTRICTED_READS or OTHER_FIELDS according to "
        f"who should be able to call it: {sorted(unclassified)}"
    )

    stale = classified - declared
    assert not stale, (
        f"these fields are listed in this file but the schema no longer declares them: "
        f"{sorted(stale)}"
    )


def test_no_field_is_classified_twice() -> None:
    """Two lists asserting opposite things about one field would contradict."""
    everything = ADMIN_ONLY_FIELDS + UNRESTRICTED_READS + OTHER_FIELDS
    duplicates = {field for field in everything if everything.count(field) > 1}
    assert not duplicates, f"listed more than once: {sorted(duplicates)}"


# ── resolvers declared in the template ──────────────────────────────────────

# Resource blocks are indented two spaces under `Resources:`; splitting on that
# boundary is enough to attribute properties to the right logical ID without
# needing the CloudFormation intrinsic-tolerant YAML loader.
_RESOURCE_SPLIT = re.compile(r"\n  (?=\w+:\n)")


@dataclass(frozen=True)
class Resolver:
    """One `AWS::AppSync::Resolver` as declared in the template."""

    logical_id: str
    type_name: str
    field_name: str
    files: tuple[str, ...]
    block: str

    def __repr__(self) -> str:  # shows up in pytest parametrize IDs
        return f"{self.type_name}.{self.field_name}"

    @property
    def mapping_source(self) -> str:
        """Everything this resolver's mapping templates contain.

        Inline templates live in the template block; the rest are separate files
        under `source/appsync/`, named by `*S3Location` and uploaded at package
        time. Both are read, because a field name is as easy to get wrong in one
        as in the other.
        """
        parts = [self.block]
        for location in self.files:
            path = (TEMPLATE.parent / location).resolve()
            if path.is_file():
                parts.append(path.read_text())
        return "\n".join(parts)

    @property
    def input_references(self) -> set[str]:
        """Input field names this resolver's mapping templates read.

        Two things that look like field reads are not. A name followed by `(` is
        a method call on the input map itself -- `put(...)`, `entrySet()`,
        `keySet()`, `get(...)` -- excluded by the lookahead; `\\b` before it
        stops the name matching short of the parenthesis. And a name the
        template puts into the map itself is readable from it afterwards without
        being part of the input type.
        """
        source = self.mapping_source
        references = set(re.findall(r"\$ctx\.args\.input\.(\w+)\b(?!\s*\()", source))
        return references - set(re.findall(r'\$ctx\.args\.input\.put\(\s*"(\w+)"', source))


def _load_resolvers() -> list[Resolver]:
    resolvers: list[Resolver] = []
    for block in _RESOURCE_SPLIT.split(TEMPLATE.read_text()):
        if "Type: AWS::AppSync::Resolver" not in block:
            continue
        logical_id = block.split(":", 1)[0].strip()
        type_name = re.search(r"^      TypeName: (\S+)", block, re.M)
        field_name = re.search(r"^      FieldName: (\S+)", block, re.M)
        assert type_name and field_name, f"{logical_id} is missing TypeName/FieldName"
        files = tuple(re.findall(r"^      \w*S3Location: (\S+)", block, re.M))
        resolvers.append(
            Resolver(logical_id, type_name.group(1), field_name.group(1), files, block)
        )
    return resolvers


RESOLVERS = _load_resolvers()
RESOLVERS_READING_INPUT = [resolver for resolver in RESOLVERS if resolver.input_references]


def _resolver(type_name: str, field_name: str) -> Resolver:
    matches = [r for r in RESOLVERS if (r.type_name, r.field_name) == (type_name, field_name)]
    assert len(matches) == 1, f"expected exactly one resolver on {type_name}.{field_name}"
    return matches[0]


def test_template_declares_the_expected_resolver_count() -> None:
    """Guards the parser itself.

    Every other resolver test is parametrized over `RESOLVERS`, so a regex that
    silently stops matching would turn this file green by collecting nothing.
    """
    assert len(RESOLVERS) >= 70, f"only parsed {len(RESOLVERS)} resolvers from {TEMPLATE.name}"


@pytest.mark.parametrize("resolver", RESOLVERS, ids=repr)
def test_resolver_field_is_declared_in_the_schema(
    resolver: Resolver, schema_fields: dict[str, set[str]]
) -> None:
    """A resolver on an undeclared field is rejected when the stack deploys."""
    assert resolver.type_name in schema_fields, (
        f"{resolver.logical_id} attaches to type {resolver.type_name!r}, "
        f"which {SCHEMA.name} does not declare"
    )
    assert resolver.field_name in schema_fields[resolver.type_name], (
        f"{resolver.logical_id} attaches to "
        f"{resolver.type_name}.{resolver.field_name}, which {SCHEMA.name} "
        f"does not declare"
    )


@pytest.mark.parametrize("resolver", RESOLVERS, ids=repr)
def test_resolver_mapping_template_files_exist(resolver: Resolver) -> None:
    """`*S3Location` paths are relative to the template and resolved at package time."""
    for location in resolver.files:
        path = (TEMPLATE.parent / location).resolve()
        assert path.is_file(), f"{resolver.logical_id} references missing file {location}"


def test_resolvers_reading_the_input_argument_were_found() -> None:
    """Guards the inline-template extractor, as the resolver-count test does."""
    assert len(RESOLVERS_READING_INPUT) >= 5, (
        f"only found {len(RESOLVERS_READING_INPUT)} resolvers reading $ctx.args.input"
    )


@pytest.mark.parametrize("resolver", RESOLVERS_READING_INPUT, ids=repr)
def test_resolver_reads_only_declared_input_fields(resolver: Resolver, schema) -> None:
    """A mapping template reading an undeclared input field reads null forever.

    The field can never arrive -- AppSync rejects an operation that sends it --
    so the attribute the template meant to write is silently never written, with
    no error anywhere. A rename on either side lands here instead.
    """
    root = {"Mutation": schema.mutation_type, "Query": schema.query_type}[resolver.type_name]
    argument = root.fields[resolver.field_name].args["input"]
    input_type = argument.type
    while hasattr(input_type, "of_type"):
        input_type = input_type.of_type
    declared = set(input_type.fields)
    undeclared = resolver.input_references - declared
    assert not undeclared, (
        f"{resolver.logical_id} reads {sorted(undeclared)} from "
        f"{input_type.name}, which {SCHEMA.name} does not declare"
    )


def test_update_virtual_participant_writes_status_only_when_one_is_supplied() -> None:
    """A caller updating one unrelated field must leave the lifecycle status alone.

    `status` is optional on the input for exactly this reason: a caller that owns
    only the CallId, or only the manual-action prompt, sends that field by itself
    and the row keeps whatever status the VP's own lifecycle last wrote.
    """
    resolver = _resolver("Mutation", "updateVirtualParticipant")
    status_set = re.search(
        r'#if\( !\$util\.isNullOrBlank\(\$ctx\.args\.input\.status\) \)\s*\n\s*'
        r'\$util\.qr\( \$expSet\.put\("#status", ":status"\) \)',
        resolver.block,
    )
    assert status_set, "the #status SET is not guarded on status being supplied"


def test_update_virtual_participant_clear_flags_remove_their_attributes() -> None:
    """Clearing has to be expressible, and distinguishable from omission.

    An omitted input field means "leave this attribute as it is", so a detail
    written during one phase of a VP's life can only be taken off the row again
    through an explicit flag that maps to a DynamoDB REMOVE.
    """
    resolver = _resolver("Mutation", "updateVirtualParticipant")
    assert "REMOVE" in resolver.block, "the resolver builds no REMOVE clause"
    for flag, attributes in (
        ("clearErrorMessage", ["errorMessage"]),
        (
            "clearManualAction",
            [
                "manualActionType",
                "manualActionMessage",
                "manualActionTimeoutSeconds",
                "manualActionStartTime",
                # The status the prompt interrupted is bookkeeping for the
                # prompt, so it goes when the prompt goes.
                "statusBeforeManualAction",
            ],
        ),
    ):
        branch = re.search(
            r"#if\( \$util\.defaultIfNull\(\$ctx\.args\.input\."
            + flag
            + r", false\) == true \)(.*?)#(?:else|end)",
            resolver.block,
            re.S,
        )
        assert branch, f"{flag} does not gate a branch of the mapping template"
        for attribute in attributes:
            assert f'$expRemove.add("#{attribute}")' in branch.group(1), (
                f"{flag} does not remove {attribute}"
            )


def test_clearing_a_prompt_is_conditional_on_the_row_the_caller_read() -> None:
    """Both forms of the clear decide what to do from a read, so both are guarded.

    With a status the caller is restoring the status a prompt interrupted, which
    holds only while that prompt is still current. Without one it is clearing
    opportunistically, having seen a row that was not prompting — so the
    condition is the converse, and a prompt raised since does not have its
    description removed from under it. Either way the writer with current
    information wins.
    """
    resolver = _resolver("Mutation", "updateVirtualParticipant")
    # `clearManualAction` gates two blocks: the REMOVE clause earlier in the
    # template, and the condition. Both are wanted elsewhere, so select by
    # content rather than by position.
    gates = re.findall(
        r"#if\(\s*\$util\.defaultIfNull\(\$ctx\.args\.input\.clearManualAction, false\)"
        r" == true\s*\)(.*?)\n        #end",
        resolver.block,
        re.S,
    )
    bodies = [gate for gate in gates if "$conditions.add" in gate]
    assert len(bodies) == 1, "expected exactly one clearManualAction-gated condition block"
    body = bodies[0]
    assert "MANUAL_ACTION_REQUIRED" in body, "the condition does not name the prompting status"
    # The condition's expression names are a separate block from the update's, so
    # a name shared between them would be undeclared in one of the two.
    assert '$conditionNames.put("#conditionStatus", "status")' in body

    resume, opportunistic = (
        re.search(
            r"#if\( !\$util\.isNullOrBlank\(\$ctx\.args\.input\.status\) \)\s*\n\s*"
            r"\$util\.qr\( \$conditions\.add\(\"([^\"]+)\"\) \)",
            body,
        ),
        re.search(
            r"#else\s*\n\s*\$util\.qr\( \$conditions\.add\(\"([^\"]+)\"\) \)",
            body,
        ),
    )
    assert resume and opportunistic, "the two condition expressions are not both set"
    assert resume.group(1) == "#conditionStatus = :promptingStatus"
    # Parenthesised, because it is ANDed with any other condition on the write.
    assert opportunistic.group(1) == (
        "(attribute_not_exists(#conditionStatus) OR #conditionStatus <> :promptingStatus)"
    )


def test_progress_status_is_conditional_on_the_row_not_having_been_ended() -> None:
    """A progress status does not land on a row whose end has been recorded.

    The end request writes `endReason` and stops the task, so a non-terminal
    status arriving afterwards (a write already in flight when the stop came)
    would otherwise leave an ended participant showing as in progress. Terminal
    statuses stay unconditioned, so the participant can still record how it
    ended, and the condition keys on `endReason` rather than on a terminal
    status, so a FAILED written by the launcher or the task reaper — which do
    not write `endReason` — does not stop a running participant reporting.
    """
    resolver = _resolver("Mutation", "updateVirtualParticipant")
    guard = re.search(
        r"#set\( \$terminalStatuses = \[([^\]]*)\] \)\s*\n\s*"
        r"#if\( !\$util\.isNullOrBlank\(\$ctx\.args\.input\.status\) && "
        r"!\$terminalStatuses\.contains\(\$ctx\.args\.input\.status\) \)(.*?)#end",
        resolver.block,
        re.S,
    )
    assert guard, "the progress-status condition is not gated on a non-terminal status"
    terminal = set(re.findall(r'"(\w+)"', guard.group(1)))
    assert terminal == {"COMPLETED", "FAILED", "ENDED", "CANCELLED"}
    assert '$conditionNames.put("#conditionEndReason", "endReason")' in guard.group(2)
    assert '$conditions.add("attribute_not_exists(#conditionEndReason)")' in guard.group(2)

    # The end request is the one writer of endReason. The launcher state
    # machine's and the task reaper's FAILED writes, in the VP stack template,
    # do not carry it: none of that template's update expressions names it,
    # either directly or through an expression attribute name.
    manager = (
        TEMPLATE.parent.parent / "source/lambda_functions/virtual_participant_manager/index.py"
    ).read_text()
    assert "endReason = :end_reason" in manager
    vp_template = (
        TEMPLATE.parent.parent.parent / "lma-virtual-participant-stack/template.yaml"
    ).read_text()
    update_expressions = re.findall(r"UpdateExpression[\"']?\s*[:=]\s*\(?\s*([^\n]+)", vp_template)
    assert len(update_expressions) >= 3, "the VP stack's status writes were not found"
    for expression in update_expressions:
        assert "endReason" not in expression, expression
    assert not re.search(r"[\"']endReason[\"']", vp_template), (
        "the VP stack template maps an expression attribute name to endReason"
    )

    # Every condition is emitted, ANDed, in the one condition block.
    assert "${conditionExpression} AND " in resolver.block
    assert re.search(
        r"#if\( !\$conditions\.isEmpty\(\) \)\s*\n\s*,\"condition\": \{\s*\n\s*"
        r"\"expression\": \"\$conditionExpression\"",
        resolver.block,
    ), "the condition block does not carry the combined expression"


def test_the_prompt_template_resolvers_both_name_the_field_they_serve() -> None:
    """One Lambda answers both the read and the write, and dispatches on this.

    The read moved off a DynamoDB data source because the shared configuration
    table stores a record's body compressed and an AppSync JavaScript resolver
    cannot decompress one. The function refuses a request that names no field
    rather than inferring one from the argument shape, so a resolver that
    stopped forwarding `ctx.info.fieldName` would fail every call -- which is
    better than a read being treated as a write, and is what this pins.
    """
    for type_name, field_name in (
        ("Query", "getLLMPromptTemplate"),
        ("Mutation", "updateLLMPromptTemplate"),
        ("Query", "getCallCost"),
        ("Query", "getCallPricing"),
        ("Mutation", "updateCallPricing"),
    ):
        resolver = _resolver(type_name, field_name)
        assert "ctx.info.fieldName" in resolver.mapping_source, (
            f"{resolver.logical_id} does not forward ctx.info.fieldName, which the "
            "function it invokes requires"
        )
        assert "operation: 'Invoke'" in resolver.mapping_source, (
            f"{resolver.logical_id} is no longer a Lambda invocation"
        )


def test_every_appsync_source_file_is_referenced_by_the_template() -> None:
    """An unreferenced resolver file is never uploaded, so it silently does nothing."""
    referenced = set(re.findall(r"\.\./source/appsync/([\w.\-]+)", TEMPLATE.read_text()))
    on_disk = {path.name for path in APPSYNC_DIR.iterdir() if path.is_file()}
    assert not (on_disk - referenced), (
        f"files in {APPSYNC_DIR.name}/ that no template resource references: "
        f"{sorted(on_disk - referenced)}"
    )


def test_subscriptions_are_wired_to_declared_mutations(schema_sdl: str) -> None:
    """`@aws_subscribe(mutations: [...])` naming an absent mutation fails schema creation."""
    document = parse(schema_sdl)
    mutations: set[str] = set()
    subscriptions = []
    for node in document.definitions:
        if not isinstance(node, (ObjectTypeDefinitionNode, ObjectTypeExtensionNode)):
            continue
        if node.name.value == "Mutation":
            mutations.update(field.name.value for field in (node.fields or []))
        elif node.name.value == "Subscription":
            subscriptions.extend(node.fields or [])

    assert mutations, "no Mutation type found — the SDL parser is not seeing the schema"
    assert subscriptions, "no Subscription type found — the SDL parser is not seeing the schema"

    unknown = []
    for field in subscriptions:
        for directive in field.directives:
            if directive.name.value != "aws_subscribe":
                continue
            for argument in directive.arguments:
                for value in argument.value.values:
                    if value.value.strip() not in mutations:
                        unknown.append(f"{field.name.value} -> {value.value}")
    assert not unknown, f"@aws_subscribe names mutations the schema does not declare: {unknown}"


# ── operations the UI sends ─────────────────────────────────────────────────

# Comments are stripped before extraction: a backtick pair inside a JSDoc block
# (`...` around inline code) otherwise yields a "literal" starting with a word
# like `query`.
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)

# A template literal whose content opens with an operation keyword, an optional
# operation name, then a variable list or a selection set. Interpolated literals
# (`${...}`) are not GraphQL documents on their own and are skipped.
_OPERATION_LITERAL = re.compile(
    r"`(\s*(?:query|mutation|subscription)\s+\w*\s*[({][^`]*)`",
    re.S,
)


def _ui_operations() -> list[tuple[str, str]]:
    """Every GraphQL document embedded in the UI, as (label, body) pairs."""
    operations: list[tuple[str, str]] = []
    for path in sorted(UI_SRC.rglob("*.js")) + sorted(UI_SRC.rglob("*.jsx")):
        source = _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", path.read_text()))
        for body in _OPERATION_LITERAL.findall(source):
            if "${" in body:
                continue
            named = re.search(r"\b(?:query|mutation|subscription)\s+(\w+)", body)
            label = f"{path.relative_to(UI_SRC)}::{named.group(1) if named else 'anonymous'}"
            operations.append((label, body))
    return operations


UI_OPERATIONS = _ui_operations()


def test_ui_operations_were_found() -> None:
    """Guards the extractor, for the same reason as the resolver-count test."""
    assert len(UI_OPERATIONS) >= 90, f"only extracted {len(UI_OPERATIONS)} UI operations"


@pytest.mark.parametrize(
    "body", [body for _, body in UI_OPERATIONS], ids=[label for label, _ in UI_OPERATIONS]
)
def test_ui_operation_validates_against_the_schema(body: str, schema) -> None:
    """Each UI document selects only fields, arguments and types the schema declares."""
    errors = validate(schema, parse(body))
    assert not errors, "\n".join(error.message for error in errors)
