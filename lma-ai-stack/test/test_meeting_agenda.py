# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Static contract tests for the meeting agenda (no AWS).

The agenda is one set of fields on the meeting record, written through one
browser-reachable mutation. What has to hold is spread across the schema, the
request template, the stack template and the browser, so these tests read all
four and assert the parts that would fail silently if they drifted: the access
rule, the provenance a signed-in caller cannot choose, the conditional write
that keeps a stored agenda from being replaced by a fill-in, and the
subscription publish that keeps open pages current.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

AI_STACK = Path(__file__).resolve().parents[1]
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
APPSYNC = AI_STACK / "source" / "appsync"
SCHEMA = APPSYNC / "schema.graphql"
AGENDA_VTL = APPSYNC / "updateCallAgenda.request.vtl"
SUMMARY_VTL = APPSYNC / "updateCallSummaryText.request.vtl"
UI_SRC = AI_STACK / "source" / "ui" / "src"
CALL_FIELDS = (
    AI_STACK
    / "source"
    / "lambda_layers"
    / "transcript_enrichment_layer"
    / "graphql_helpers"
    / "call_fields.py"
)

AGENDA_FIELDS = ("Agenda", "AgendaSource", "AgendaUpdatedBy", "AgendaUpdatedAt")


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


@pytest.fixture(scope="module")
def agenda_vtl() -> str:
    return AGENDA_VTL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def schema_sdl() -> str:
    return SCHEMA.read_text(encoding="utf-8")


def _block(sdl: str, header: str) -> str:
    start = sdl.index(header)
    return sdl[start : sdl.index("\n}", start)]


# ── schema ──────────────────────────────────────────────────────────────────


def test_the_call_type_declares_every_agenda_field(schema_sdl: str) -> None:
    call = _block(schema_sdl, "type Call implements DynamoDbBase")
    for field in AGENDA_FIELDS:
        assert f"\t{field}:" in call, f"Call does not declare {field}"


def test_the_agenda_mutation_is_reachable_by_signed_in_users_and_backend_roles(
    schema_sdl: str,
) -> None:
    line = next(line for line in schema_sdl.splitlines() if "updateCallAgenda(" in line)
    assert "@aws_cognito_user_pools" in line
    assert "@aws_iam" in line
    # No group argument: access is per meeting, decided in the template.
    assert "cognito_groups" not in line


def test_an_agenda_write_is_published_to_the_meeting_subscription(schema_sdl: str) -> None:
    lines = schema_sdl.splitlines()
    on_update = next(i for i, line in enumerate(lines) if line.strip().startswith("onUpdateCall("))
    subscribe = next(line for line in lines[on_update:] if "@aws_subscribe" in line)
    assert '"updateCallAgenda"' in subscribe


# ── request template ────────────────────────────────────────────────────────


def test_the_resolver_uses_the_dedicated_template(resources: dict) -> None:
    resolver = resources["UpdateCallAgendaAppSyncResolver"]["Properties"]
    assert resolver["FieldName"] == "updateCallAgenda"
    assert resolver["TypeName"] == "Mutation"
    assert resolver["RequestMappingTemplateS3Location"].endswith("updateCallAgenda.request.vtl")
    assert resolver["ResponseMappingTemplateS3Location"].endswith("dDbPutCondition.response.vtl")


def test_a_signed_in_caller_is_held_to_the_meetings_access_rule(agenda_vtl: str) -> None:
    """Admin, the owner, or a user it is shared with -- stated exactly as the
    summary write states it, so the two cannot give different answers about one
    meeting."""
    summary = SUMMARY_VTL.read_text(encoding="utf-8")
    clause = "#Owner = :caller OR contains(#SharedWith, :caller)"
    detection = '$context.identity.userArn.contains(":assumed-role/")'
    for source in (agenda_vtl, summary):
        assert clause in source
        assert detection in source
    assert 'groups.contains("Admin")' in agenda_vtl
    assert "attribute_exists(#PK)" in agenda_vtl


def test_a_signed_in_callers_write_is_recorded_as_their_own(agenda_vtl: str) -> None:
    """The browser cannot choose the provenance shown beside the agenda."""
    cognito_branch = agenda_vtl.split("#if( $isCognitoUser )", 1)[1].split("#else", 1)[0]
    assert '#set( $source = "ui" )' in cognito_branch
    assert "#set( $updatedBy = $context.identity.username )" in cognito_branch
    assert "ctx.args.input" not in cognito_branch


def test_a_backend_role_must_name_a_known_source(agenda_vtl: str) -> None:
    assert '["scheduled", "mcp", "ui", "derived"]' in agenda_vtl
    assert "!$allowedSources.contains($source)" in agenda_vtl


def test_only_if_unset_is_part_of_the_write_condition(agenda_vtl: str) -> None:
    """Checked in DynamoDB with the write, not read-then-write in a Lambda, so an
    agenda somebody saves always survives a concurrent fill-in."""
    assert "attribute_not_exists(#ExistingAgenda) OR #ExistingAgenda = :noAgenda" in agenda_vtl
    assert "$ctx.args.input.OnlyIfUnset == true" in agenda_vtl


def test_the_template_writes_only_the_agenda_fields(agenda_vtl: str) -> None:
    expression = next(line for line in agenda_vtl.splitlines() if '"expression": "SET' in line)
    for field in AGENDA_FIELDS:
        assert f"#{field} = :{field}" in expression
    assert expression.count("=") == len(AGENDA_FIELDS)


def test_the_agenda_length_is_bounded(agenda_vtl: str) -> None:
    assert "#set( $maxAgendaChars = 10000 )" in agenda_vtl
    agenda_js = (UI_SRC / "components" / "common" / "agenda.js").read_text(encoding="utf-8")
    assert "MAX_AGENDA_CHARS = 10000" in agenda_js


# ── browser ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "operation",
    [
        "getCall.js",
        "onUpdateCall.js",
        "updateCallAgenda.js",
        "updateSpeakerIdentities.js",
        "updateCallSummaryText.js",
    ],
)
def test_the_browser_selects_the_agenda_wherever_it_reads_a_whole_meeting(operation: str) -> None:
    body = (UI_SRC / "graphql" / "queries" / operation).read_text(encoding="utf-8")
    for field in AGENDA_FIELDS:
        assert f"      {field}\n" in body, f"{operation} does not select {field}"


def test_the_pipeline_selector_does_not_name_the_agenda() -> None:
    """The shared selector is built against the schema the API is running, so a
    field it names must already be deployed when the layer ships. Leaving the
    agenda out keeps the two independently deployable, as SpeakerIdentities and
    CallSummaryState are; the browser carries the agenda over instead."""
    source = CALL_FIELDS.read_text(encoding="utf-8")
    for field in AGENDA_FIELDS:
        assert f"schema.Call.{field}," not in source


def test_the_browser_carries_the_agenda_over_a_pipeline_update() -> None:
    hook = (UI_SRC / "hooks" / "use-calls-graphql-api.js").read_text(encoding="utf-8")
    assert "...(call.AgendaUpdatedAt" in hook
    for field in AGENDA_FIELDS:
        assert f"{field}: existing?.{field}," in hook
