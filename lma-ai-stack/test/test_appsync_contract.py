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

    def __repr__(self) -> str:  # shows up in pytest parametrize IDs
        return f"{self.type_name}.{self.field_name}"


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
        resolvers.append(Resolver(logical_id, type_name.group(1), field_name.group(1), files))
    return resolvers


RESOLVERS = _load_resolvers()


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
