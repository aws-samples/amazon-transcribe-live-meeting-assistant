# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Static contract tests for the meeting preparation (no AWS).

A meeting's labels, audience, meeting context and checklist are fields on the
meeting record, written through five browser-reachable mutations answered by
one Lambda, and given to a Virtual Participant at creation through a pipeline
that checks them with the same Lambda. What has to hold is spread across the
schema, the request templates, the stack template, the Virtual Participant and
the browser, so these tests read them all and assert the parts that would fail
silently if they drifted. The rules themselves are tested in the configuration
layer (lma_meeting_preparation/test_rules.py) and the resolver beside it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

AI_STACK = Path(__file__).resolve().parents[1]
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
APPSYNC = AI_STACK / "source" / "appsync"
SCHEMA = APPSYNC / "schema.graphql"
VALIDATE_VTL = APPSYNC / "validateMeetingPreparation.request.vtl"
SCHEDULED_EDIT_VTL = APPSYNC / "updateScheduledVirtualParticipant.request.vtl"
UI_QUERIES = AI_STACK / "source" / "ui" / "src" / "graphql" / "queries"
VP_SRC = AI_STACK.parent / "lma-virtual-participant-stack" / "backend" / "src"

MUTATIONS = (
    "updateCallMeetingContext",
    "updateCallChecklist",
    "updateCallChecklistItem",
    "updateCallLabels",
    "updateCallAudience",
)
CALL_FIELDS = (
    "Labels",
    "Audience",
    "MeetingMetadataUpdatedAt",
    "MeetingContext",
    "MeetingContextUpdatedBy",
    "MeetingContextUpdatedAt",
    "Checklist",
    "ChecklistUpdatedAt",
)


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
def schema_sdl() -> str:
    return SCHEMA.read_text(encoding="utf-8")


def _block(sdl: str, header: str) -> str:
    start = sdl.index(header)
    return sdl[start : sdl.index("\n}", start)]


def _resolver(resources: dict, field: str) -> dict:
    (props,) = [
        r["Properties"]
        for r in resources.values()
        if r.get("Type") == "AWS::AppSync::Resolver"
        and r["Properties"].get("TypeName") == "Mutation"
        and r["Properties"].get("FieldName") == field
    ]
    return props


# ── the schema ──────────────────────────────────────────────────────────────


def test_the_call_type_has_the_fixed_fields(schema_sdl):
    call = _block(schema_sdl, "type Call implements DynamoDbBase")
    for line in (
        "Labels: [CallLabel]",
        "Audience: String",
        "MeetingContext: String",
        "MeetingContextUpdatedBy: String",
        "MeetingContextUpdatedAt: AWSDateTime",
        "Checklist: [ChecklistItem]",
        "ChecklistUpdatedAt: AWSDateTime",
        "MeetingMetadataUpdatedAt: AWSDateTime",
    ):
        assert f"\t{line}\n" in call + "\n", line


def test_the_item_types_have_the_fixed_fields_and_are_readable_by_backend_roles(schema_sdl):
    expected = {
        "type CallLabel": ["Key: String!", "Value: String!"],
        "type ChecklistEvidence": ["SegmentId: String", "StartTime: Float", "Quote: String"],
        "type ChecklistItem ": [
            "ItemId: String!",
            "Text: String!",
            "Kind: String!",
            "Priority: String",
            "Source: String",
            "Status: String!",
            "StatusSource: String",
            "Evidence: [ChecklistEvidence]",
            "Note: String",
            "UpdatedBy: String",
            "UpdatedAt: AWSDateTime",
        ],
    }
    for header, fields in expected.items():
        block = _block(schema_sdl, header)
        assert "@aws_cognito_user_pools" in block and "@aws_iam" in block, header
        declared = [line.strip() for line in block.splitlines()[2:] if line.strip() and not line.strip().startswith("#")]
        assert declared == fields, header


def test_each_mutation_is_for_signed_in_users_and_backend_roles_and_published(schema_sdl):
    lines = schema_sdl.splitlines()
    for mutation in MUTATIONS:
        line = next(line for line in lines if line.strip().startswith(f"{mutation}("))
        assert line.strip().endswith("): Call @aws_cognito_user_pools @aws_iam"), mutation
    on_update = next(i for i, line in enumerate(lines) if line.strip().startswith("onUpdateCall("))
    subscribe = next(line for line in lines[on_update:] if "@aws_subscribe" in line)
    for mutation in MUTATIONS:
        assert f'"{mutation}"' in subscribe


def test_a_virtual_participant_takes_the_four_inputs_at_creation_and_in_an_edit(schema_sdl):
    for header in ("input CreateVirtualParticipantInput", "input UpdateScheduledVirtualParticipantInput"):
        block = _block(schema_sdl, header)
        for line in (
            "labels: [CallLabelInput!]",
            "audience: String",
            "meetingContext: String",
            "checklist: [ChecklistItemInput!]",
        ):
            assert line in block, (header, line)
    create_call = _block(schema_sdl, "input CreateCallInput")
    for field in CALL_FIELDS:
        assert f"\t{field}:" in create_call, field


# ── the stack template ──────────────────────────────────────────────────────


def test_each_mutation_is_answered_by_the_lambda_in_the_subscription_s_form(resources):
    for mutation in MUTATIONS:
        props = _resolver(resources, mutation)
        assert props["DataSourceName"] == {"Fn::GetAtt": "MeetingPreparationLambdaDataSource.Name"}
        assert props["RequestMappingTemplateS3Location"].endswith("meetingPreparation.request.vtl")
        assert props["ResponseMappingTemplateS3Location"].endswith("dDbPutCondition.response.vtl")


def test_the_lambda_writes_meeting_rows_only(resources):
    role = resources["MeetingPreparationResolverFunctionExecutionRole"]["Properties"]
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    dynamo = next(s for s in statements if "dynamodb:UpdateItem" in s["Action"])
    assert dynamo["Action"] == ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    assert dynamo["Condition"]["ForAllValues:StringLike"]["dynamodb:LeadingKeys"] == ["c#*"]
    function = resources["MeetingPreparationResolverFunction"]["Properties"]
    assert function["Layers"] == [{"Ref": "ConfigurationPythonLayer"}]
    assert function["CodeUri"].endswith("meeting_preparation_resolver")


def test_a_virtual_participant_is_checked_before_it_is_stored(resources):
    create = _resolver(resources, "createVirtualParticipant")
    assert create["Kind"] == "PIPELINE"
    assert create["PipelineConfig"]["Functions"] == [
        {"Fn::GetAtt": "ValidateMeetingPreparationAppSyncFunction.FunctionId"},
        {"Fn::GetAtt": "CreateVirtualParticipantAppSyncFunction.FunctionId"},
    ]
    edit = _resolver(resources, "updateScheduledVirtualParticipant")
    assert edit["PipelineConfig"]["Functions"][0] == {
        "Fn::GetAtt": "ValidateMeetingPreparationAppSyncFunction.FunctionId"
    }
    validate = resources["ValidateMeetingPreparationAppSyncFunction"]["Properties"]
    assert validate["DataSourceName"] == {"Fn::GetAtt": "MeetingPreparationLambdaDataSource.Name"}


def test_the_check_is_sent_the_four_inputs_only_and_skipped_without_them():
    vtl = VALIDATE_VTL.read_text(encoding="utf-8")
    # Never the rest of the request, which can hold a meeting password.
    assert "meetingPassword" not in vtl
    assert "$ctx.arguments" not in vtl and "$ctx.args.input)" not in vtl
    assert '#return({ "present": [] })' in vtl
    assert '"fieldName": "validateMeetingPreparation"' in vtl


def test_the_writes_store_what_the_check_returned(resources):
    request = resources["CreateVirtualParticipantAppSyncFunction"]["Properties"]["RequestMappingTemplate"]
    create = request["Fn::Sub"] if isinstance(request, dict) else request
    assert "$ctx.stash.preparation" in create
    assert "$util.qr($input.put($name, $preparation.get($name)))" in create
    edit = SCHEDULED_EDIT_VTL.read_text(encoding="utf-8")
    assert "$ctx.stash.preparation" in edit
    assert "$util.qr( $removeNames.add($name) )" in edit
    assert "$util.qr( $setValues.put($name, $preparation.get($name)) )" in edit


# ── the Virtual Participant ─────────────────────────────────────────────────


def test_the_vp_reads_the_four_inputs_with_the_agenda_and_sends_them_in_start():
    status_manager = (VP_SRC / "status-manager.ts").read_text(encoding="utf-8")
    stream = (VP_SRC / "kinesis-stream.ts").read_text(encoding="utf-8")
    getter = status_manager[status_manager.index("STARTUP_FIELDS = `") :]
    getter = getter[: getter.index("`;")]
    for field in ("labels { Key Value }", "audience", "meetingContext", "checklist { ItemId Text Kind Priority Source }"):
        assert field in getter
    start = stream[stream.index("async sendStartMeeting()") :]
    start = start[: start.index("await this.sendRecord(record);")]
    assert "...preparationStartFields(this.preparation, details.lmaUser)," in start


# ── the browser ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("query", ["getCall.js", "onUpdateCall.js"])
def test_the_meeting_page_reads_every_field(query):
    text = (UI_QUERIES / query).read_text(encoding="utf-8")
    for field in CALL_FIELDS:
        assert f"\n      {field}" in text, (query, field)


@pytest.mark.parametrize("mutation", MUTATIONS)
def test_each_page_mutation_selects_what_the_subscription_needs(mutation):
    text = (UI_QUERIES / f"{mutation}.js").read_text(encoding="utf-8")
    for field in ("CallId", "Owner", "SharedWith", *CALL_FIELDS):
        assert field in text, (mutation, field)
