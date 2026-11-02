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

import ast
import json
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
VP_SRC = AI_STACK.parent / "lma-virtual-participant-stack" / "backend" / "src"
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
    write_id = resolver["PipelineConfig"]["Functions"][-1]["Fn::GetAtt"].split(".")[0]
    write = resources[write_id]["Properties"]
    assert write["RequestMappingTemplateS3Location"].endswith("updateCallAgenda.request.vtl")
    assert write["ResponseMappingTemplateS3Location"].endswith("dDbPutCondition.response.vtl")


def test_a_signed_in_caller_is_held_to_the_meetings_access_rule(agenda_vtl: str) -> None:
    """Admin, the owner, or a user it is shared with -- stated exactly as the
    summary write states it, so the two cannot give different answers about one
    meeting."""
    summary = SUMMARY_VTL.read_text(encoding="utf-8")
    clause = "## BEGIN SharedWith write condition"
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


# ── carried in from starting or scheduling a Virtual Participant ───────────


def test_create_call_accepts_the_agenda_fields(schema_sdl: str) -> None:
    """createCall writes its whole input, so declaring the fields is what lets a
    START event create the meeting with its agenda."""
    create = _block(schema_sdl, "input CreateCallInput")
    for field in AGENDA_FIELDS:
        assert f"\t{field}:" in create, f"CreateCallInput does not declare {field}"


def test_the_vp_row_declares_its_agenda(schema_sdl: str) -> None:
    vp = _block(schema_sdl, "type VirtualParticipant ")
    assert "\tagenda: String" in vp
    assert "\tagendaSource: String" in vp
    create_input = _block(schema_sdl, "input CreateVirtualParticipantInput")
    assert "\tagenda: String" in create_input
    # The source is decided by the resolver from the caller, never sent.
    assert "agendaSource" not in create_input


def _create_vp_template(resources: dict) -> str:
    return resources["CreateVirtualParticipantResolver"]["Properties"]["RequestMappingTemplate"]


def test_the_vp_row_records_where_its_agenda_came_from(resources: dict) -> None:
    vtl = _create_vp_template(resources)
    agenda_block = vtl[vtl.index("isNullOrBlank($ctx.args.input.agenda)") :]
    agenda_block = agenda_block[: agenda_block.index('"version"')]
    assert '$util.qr($input.put("agenda", $ctx.args.input.agenda))' in agenda_block
    # An IAM caller is the MCP server; anyone else used the form.
    assert "#if($ctx.identity.userArn)" in agenda_block
    assert '$input.put("agendaSource", "mcp")' in agenda_block
    assert '$input.put("agendaSource", "scheduled")' in agenda_block
    assert "length() > 10000" in agenda_block


def test_the_vp_reads_its_agenda_from_its_row_and_sends_it_in_start() -> None:
    status_manager = (VP_SRC / "status-manager.ts").read_text(encoding="utf-8")
    stream = (VP_SRC / "kinesis-stream.ts").read_text(encoding="utf-8")
    index = (VP_SRC / "index.ts").read_text(encoding="utf-8")
    getter = status_manager[status_manager.index("async getAgenda()") :]
    getter = getter[: getter.index("\n  }\n")]
    assert "agenda\n            agendaSource" in getter
    start = stream[stream.index("async sendStartMeeting()") :]
    start = start[: start.index("await this.sendRecord(record);")]
    assert "...agendaStartFields(this.agenda, details.lmaUser)," in start
    # Read before the meeting starts, where the CallId is linked.
    assert "setAgenda(await statusManager.getAgenda());" in index


def test_the_scheduling_form_sends_the_agenda() -> None:
    form = (
        UI_SRC / "components" / "virtual-participant-layout" / "VirtualParticipantList.jsx"
    ).read_text(encoding="utf-8")
    assert "if (agendaText) vpInput.agenda = agendaText;" in form
    assert "agenda: data.agenda || prev.agenda," in form


# ── the two MCP endpoints advertise the same arguments ─────────────────────

MCP_INDEX = AI_STACK / "source" / "lambda_functions" / "mcp_analytics" / "index.py"


def _api_key_endpoint_tools() -> dict:
    """MCP_TOOLS from the MCP Lambda: what the API-key endpoint's tools/list returns."""
    tree = ast.parse(MCP_INDEX.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "MCP_TOOLS" for t in node.targets
        ):
            return {tool["name"]: tool for tool in ast.literal_eval(node.value)}
    raise AssertionError("MCP_TOOLS not found")


def _gateway_tools(resources: dict) -> dict:
    """The AgentCore Gateway target's inline tool list: what the OAuth endpoint offers."""
    for resource in resources.values():
        config = (resource.get("Properties") or {}).get("TargetConfiguration") or {}
        payload = ((config.get("Mcp") or {}).get("Lambda") or {}).get("ToolSchema", {})
        if "InlinePayload" in payload:
            return {tool["Name"]: tool for tool in payload["InlinePayload"]}
    raise AssertionError("no gateway target with an inline tool schema")


@pytest.mark.parametrize("tool", ["start_meeting_now", "schedule_meeting"])
def test_both_mcp_endpoints_offer_the_agenda_argument(resources: dict, tool: str) -> None:
    """The OAuth endpoint (an AgentCore Gateway target) declares its own copy of
    each tool's schema in the stack template; a client on it learns only the
    arguments that copy names, so an argument added to one list alone is
    invisible to half the clients."""
    gateway = _gateway_tools(resources)[tool]["InputSchema"]
    api_key = _api_key_endpoint_tools()[tool]["inputSchema"]
    assert gateway["Properties"]["agenda"]["Type"] == "string"
    assert api_key["properties"]["agenda"]["type"] == "string"
    assert "agenda" not in gateway.get("Required", [])
    assert "agenda" not in api_key.get("required", [])
    assert set(gateway["Properties"]) == set(api_key["properties"])


# ── derivation from the transcript ──────────────────────────────────────────


def test_the_derive_mutation_reaches_the_deriver_with_the_callers_identity(
    resources: dict, schema_sdl: str
) -> None:
    line = next(line for line in schema_sdl.splitlines() if "deriveCallAgenda(" in line)
    assert "@aws_cognito_user_pools" in line and "@aws_iam" in line
    resolver = resources["DeriveCallAgendaResolver"]["Properties"]
    assert resolver["FieldName"] == "deriveCallAgenda"
    assert resolver["CodeS3Location"].endswith("deriveCallAgenda.js")
    source = resources["AgendaDeriverLambdaDataSource"]["Properties"]
    assert source["LambdaConfig"]["LambdaFunctionArn"] == {
        "Fn::GetAtt": "AgendaDeriverFunction.Arn"
    }
    js = (APPSYNC / "deriveCallAgenda.js").read_text(encoding="utf-8")
    assert "payload: { arguments: ctx.arguments, identity: ctx.identity }" in js


def _deriver_statements(resources: dict) -> list:
    role = resources["AgendaDeriverFunctionExecutionRole"]["Properties"]
    return role["Policies"][0]["PolicyDocument"]["Statement"]


def test_the_deriver_writes_the_record_only_for_its_claim_and_metering(resources: dict) -> None:
    """The agenda itself goes through AppSync; the two direct writes are each
    held to their own partitions."""
    leading = {}
    for statement in _deriver_statements(resources):
        actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
        for action in actions:
            if action in ("dynamodb:UpdateItem", "dynamodb:PutItem"):
                leading[action] = statement["Condition"]["ForAllValues:StringLike"][
                    "dynamodb:LeadingKeys"
                ]
    assert leading == {"dynamodb:UpdateItem": ["c#*"], "dynamodb:PutItem": ["mtr#*"]}
    graphql = [s for s in _deriver_statements(resources) if "appsync:GraphQL" in s["Action"]]
    assert len(graphql) == 1
    assert graphql[0]["Resource"]["Fn::Sub"].endswith("/types/Mutation/fields/updateCallAgenda")


def test_the_event_processor_queues_the_automatic_pass(resources: dict) -> None:
    function = resources["CallEventProcessorFunction"]["Properties"]
    env = function["Environment"]["Variables"]
    assert env["AGENDA_DERIVER_ARN"] == {"Fn::GetAtt": "AgendaDeriverFunction.Arn"}
    # The default of the Meeting Agenda section, read when the configuration cannot be.
    assert env["AGENDA_DERIVATION_AFTER_SECONDS"] == {"Ref": "AgendaDerivationAfterSeconds"}


def test_derivation_is_metered_under_its_own_context() -> None:
    records = (
        AI_STACK / "source" / "lambda_layers" / "configuration_layer" / "lma_metering" / "records.py"
    ).read_text(encoding="utf-8")
    assert '    "AgendaDerivation",\n' in records
    labels = (UI_SRC / "components" / "common" / "call-cost.js").read_text(encoding="utf-8")
    assert "AgendaDerivation: 'Agenda derivation'," in labels


def test_the_summary_ships_an_agenda_coverage_section_in_both_copies() -> None:
    copies = [
        AI_STACK / "source" / "lambda_functions" / "configuration_seeder" / "defaults",
        AI_STACK.parent / "lma-llm-template-setup-stack" / "source" / "lambda_functions",
    ]
    prompts = [
        json.loads((d / "LLMPromptSummaryTemplate.json").read_text(encoding="utf-8"))["AGENDA COVERAGE"]
        for d in copies
    ]
    assert prompts[0] == prompts[1]
    assert "{agenda}" in prompts[0]
