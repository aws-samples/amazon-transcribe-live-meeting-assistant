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


def test_resuming_a_prompts_status_is_conditional_on_it_still_prompting() -> None:
    """The status to resume comes from a read, so the write has to be guarded.

    Clearing a prompt and supplying a status together mean "put back the status
    this prompt interrupted", and that status can only have come from reading
    the row first. The condition makes the write apply only while the row is
    still prompting, so a status written in between stands.
    """
    resolver = _resolver("Mutation", "updateVirtualParticipant")
    guard = re.search(
        r"#if\(\s*\$util\.defaultIfNull\(\$ctx\.args\.input\.clearManualAction, false\) == true"
        r"\s*&&\s*!\$util\.isNullOrBlank\(\$ctx\.args\.input\.status\)\s*\)(.*?)#end",
        resolver.block,
        re.S,
    )
    assert guard, "the condition is not gated on clearing a prompt with a status"
    body = guard.group(1)
    assert '"condition"' in body, "the gated branch emits no condition block"
    assert "MANUAL_ACTION_REQUIRED" in body, "the condition does not name the prompting status"
    # The condition's own expression names are separate from the update's, so a
    # name reused across the two blocks would be undeclared in one of them.
    assert '"#conditionStatus": "status"' in body


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
