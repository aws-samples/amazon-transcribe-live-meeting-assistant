# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""updateScheduledVirtualParticipant: changing a scheduled Virtual Participant.

The mutation is a pipeline: ReadScheduledVirtualParticipant reads the row into
the stash, and updateScheduledVirtualParticipant.request.vtl checks it and writes
on condition that it is still as read. The VPScheduler Lambda then moves the
launch schedule (tested in lma-virtual-participant-stack/test/test_vp_template.py).

The `evaluated` tests run the templates through AppSync's EvaluateMappingTemplate
API, which evaluates a template against a supplied context and touches no
resource; they run only with LMA_EVALUATE_APPSYNC=1 (and AWS credentials), as in
test_shared_with_access_evaluated.py.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml

AI_STACK = Path(__file__).resolve().parents[1]
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
APPSYNC = AI_STACK / "source" / "appsync"
SCHEMA = APPSYNC / "schema.graphql"
WRITE_VTL = APPSYNC / "updateScheduledVirtualParticipant.request.vtl"

OWNER = "owner@example.com"
OWNER_IDENTITY = {"username": OWNER, "groups": []}
ADMIN_IDENTITY = {"username": "admin@example.com", "groups": ["Admin"]}
OTHER_IDENTITY = {"username": "other@example.com", "groups": []}
BACKEND_IDENTITY = {"userArn": "arn:aws:sts::123456789012:assumed-role/LambdaRole/session"}


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
def write_vtl() -> str:
    return WRITE_VTL.read_text(encoding="utf-8")


# ── wiring ──────────────────────────────────────────────────────────────────


def test_the_mutation_reads_the_row_then_writes(resources: dict) -> None:
    resolver = resources["UpdateScheduledVirtualParticipantResolver"]["Properties"]
    assert resolver["Kind"] == "PIPELINE"
    assert resolver["PipelineConfig"]["Functions"] == [
        {"Fn::GetAtt": "ReadScheduledVirtualParticipantAppSyncFunction.FunctionId"},
        {"Fn::GetAtt": "UpdateScheduledVirtualParticipantAppSyncFunction.FunctionId"},
    ]
    read = resources["ReadScheduledVirtualParticipantAppSyncFunction"]["Properties"]
    write = resources["UpdateScheduledVirtualParticipantAppSyncFunction"]["Properties"]
    assert read["DataSourceName"] == write["DataSourceName"]
    assert '"consistentRead": true' in read["RequestMappingTemplate"]
    assert '$ctx.stash.put("vpAsRead", $ctx.result)' in read["ResponseMappingTemplate"]
    assert write["RequestMappingTemplateS3Location"].endswith(WRITE_VTL.name)


def test_the_write_is_conditional_on_the_row_as_read(write_vtl: str) -> None:
    for condition in (
        "#conditionStatus = :conditionScheduled",
        "#conditionIsScheduled = :conditionTrue",
        "#conditionMeetingTime = :conditionMeetingTimeAsRead",
    ):
        assert f'$conditions.add("{condition}")' in write_vtl, condition
    assert '"expression": "$conditionExpression"' in write_vtl


def test_the_owner_is_checked_before_the_row_is_described(write_vtl: str) -> None:
    assert write_vtl.index("is not authorized to change Virtual Participant") < write_vtl.index(
        "was not found"
    )
    assert write_vtl.index("was not found") < write_vtl.index("is not waiting for a scheduled")


def test_the_zoom_account_is_the_owners(write_vtl: str) -> None:
    assert '$setValues.put("userZoomSub", $row.userSub)' in write_vtl
    assert "ctx.args.input.userZoomSub" not in write_vtl


def test_the_input_does_not_offer_the_identity_fields() -> None:
    sdl = SCHEMA.read_text(encoding="utf-8")
    start = sdl.index("input UpdateScheduledVirtualParticipantInput")
    block = sdl[start : sdl.index("\n}", start)]
    for field in ("userSub", "userZoomSub", "owner", "Owner", "status", "isScheduled"):
        assert f"\t{field}:" not in block, field


def test_the_response_does_not_return_the_password(resources: dict) -> None:
    response = resources["UpdateScheduledVirtualParticipantAppSyncFunction"]["Properties"][
        "ResponseMappingTemplate"
    ]
    assert '$ctx.result.remove("meetingPassword")' in response
    assert response.index('remove("meetingPassword")') < response.index("$util.toJson($ctx.result)")


# ── evaluated by AppSync ────────────────────────────────────────────────────

_when_asked = pytest.mark.skipif(
    os.environ.get("LMA_EVALUATE_APPSYNC") != "1",
    reason="evaluates templates with the AppSync API; set LMA_EVALUATE_APPSYNC=1 to run",
)


def evaluated(test):
    """Run only when asked, and allowed to call AWS (see scripts/testing/no_real_aws.py)."""
    return pytest.mark.real_aws(_when_asked(test))


@pytest.fixture(scope="module")
def appsync():
    import boto3  # imported here so the module collects without boto3 when skipped

    return boto3.client("appsync", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _row(**overrides) -> dict:
    row = {
        "id": "vp1",
        "Owner": OWNER,
        "status": "SCHEDULED",
        "isScheduled": True,
        "meetingTime": int(time.time()) + 3600,
        "userSub": "sub-owner",
        "meetingPassword": "pw",
    }
    row.update(overrides)
    return row


def _write(appsync, write_vtl: str, fields: dict, identity: dict, row: dict | None) -> dict:
    context = {
        "arguments": {"input": {"id": "vp1", **fields}},
        "identity": identity,
        "stash": {"vpFound": row is not None, **({"vpAsRead": row} if row else {})},
    }
    response = appsync.evaluate_mapping_template(template=write_vtl, context=json.dumps(context))
    if response.get("error"):
        return {"error": response["error"]["message"]}
    return json.loads(response["evaluationResult"])


def _error(result: dict) -> str:
    assert "error" in result, f"expected a refusal, got {result}"
    return result["error"]


@evaluated
def test_the_owner_changes_every_field(appsync, write_vtl: str) -> None:
    new_time = int(time.time()) + 7200
    request = _write(
        appsync,
        write_vtl,
        {
            "meetingName": "Renamed",
            "meetingPlatform": "TEAMS",
            "meetingId": "42",
            "meetingPassword": "new-pw",
            "meetingTime": new_time,
            "scheduledTimeZone": "Europe/Paris",
            "agenda": "One\nTwo",
            "consentMode": "opt_in",
            "consentRule": "owner_attests",
            "enableVideoRecording": False,
            "useStoredZoomAccount": True,
        },
        OWNER_IDENTITY,
        _row(),
    )
    assert "error" not in request, request
    values = request["update"]["expressionValues"]
    names = request["update"]["expressionNames"]
    assert values[":meetingName"] == {"S": "Renamed"}
    assert str(values[":meetingTime"]["N"]) == str(new_time)
    assert values.get(":scheduledFor", {}).get("S", "").startswith("20"), values[":scheduledFor"]
    assert values[":agendaSource"] == {"S": "scheduled"}
    assert values[":consentRule"] == {"S": "owner_attests"}
    assert values[":enableVideoRecording"] == {"BOOL": False}
    # The owner's Zoom account, from the row.
    assert values[":userZoomSub"] == {"S": "sub-owner"}
    assert names["#meetingPassword"] == "meetingPassword"
    assert "REMOVE" not in request["update"]["expression"]
    # A note that the schedule was not moved is the scheduler's to clear.
    assert "#errorMessage" not in names
    condition = request["condition"]
    assert "#conditionStatus = :conditionScheduled" in condition["expression"]
    assert "#conditionOwner = :conditionCaller" in condition["expression"]
    assert condition["expressionValues"][":conditionCaller"] == {"S": OWNER}


@evaluated
def test_clearing_removes_the_attributes(appsync, write_vtl: str) -> None:
    request = _write(
        appsync,
        write_vtl,
        {
            "agenda": "",
            "consentMode": "",
            "clearMeetingPassword": True,
            "useStoredZoomAccount": False,
        },
        OWNER_IDENTITY,
        _row(),
    )
    assert "error" not in request, request
    expression = request["update"]["expression"]
    removed = expression.split("REMOVE", 1)[1]
    for name in (
        "#agenda",
        "#agendaSource",
        "#consentMode",
        "#consentRule",
        "#meetingPassword",
        "#userZoomSub",
    ):
        assert name in removed, name


@evaluated
def test_an_empty_password_keeps_the_current_one(appsync, write_vtl: str) -> None:
    request = _write(appsync, write_vtl, {"meetingPassword": ""}, OWNER_IDENTITY, _row())
    assert "meetingPassword" not in json.dumps(request["update"])


@evaluated
def test_an_admin_is_not_held_to_the_owner_condition(appsync, write_vtl: str) -> None:
    request = _write(appsync, write_vtl, {"meetingName": "x"}, ADMIN_IDENTITY, _row())
    assert "error" not in request, request
    assert "#conditionOwner" not in request["condition"]["expression"]


@evaluated
def test_a_backend_role_records_an_mcp_agenda(appsync, write_vtl: str) -> None:
    request = _write(appsync, write_vtl, {"agenda": "A"}, BACKEND_IDENTITY, _row())
    assert request["update"]["expressionValues"][":agendaSource"] == {"S": "mcp"}


@evaluated
def test_another_user_is_refused(appsync, write_vtl: str) -> None:
    assert "is not authorized" in _error(
        _write(appsync, write_vtl, {"meetingName": "x"}, OTHER_IDENTITY, _row())
    )


@evaluated
def test_another_user_is_not_told_that_a_row_is_missing(appsync, write_vtl: str) -> None:
    assert "is not authorized" in _error(
        _write(appsync, write_vtl, {"meetingName": "x"}, OTHER_IDENTITY, None)
    )
    assert "was not found" in _error(
        _write(appsync, write_vtl, {"meetingName": "x"}, ADMIN_IDENTITY, None)
    )


@evaluated
@pytest.mark.parametrize(
    "row",
    [
        {"status": "INITIALIZING"},
        {"status": "CANCELLED"},
        {"isScheduled": False},
    ],
)
def test_a_virtual_participant_that_is_not_waiting_is_refused(
    appsync, write_vtl: str, row: dict
) -> None:
    assert "is not waiting for a scheduled launch" in _error(
        _write(appsync, write_vtl, {"meetingName": "x"}, OWNER_IDENTITY, _row(**row))
    )


@evaluated
def test_a_meeting_less_than_five_minutes_away_is_refused(appsync, write_vtl: str) -> None:
    soon = _row(meetingTime=int(time.time()) + 240)
    assert "less than 5 minutes" in _error(
        _write(appsync, write_vtl, {"meetingName": "x"}, OWNER_IDENTITY, soon)
    )


@evaluated
def test_a_new_time_less_than_five_minutes_away_is_refused(appsync, write_vtl: str) -> None:
    fields = {"meetingTime": int(time.time()) + 240}
    assert "at least 5 minutes" in _error(
        _write(appsync, write_vtl, fields, OWNER_IDENTITY, _row())
    )


@evaluated
@pytest.mark.parametrize(
    "fields,message",
    [
        ({"meetingName": ""}, "meetingName cannot be empty"),
        ({"meetingPlatform": "SKYPE"}, "meetingPlatform must be"),
        ({"consentMode": "always"}, "consentMode must be"),
        ({"consentMode": "notify", "consentRule": "owner_attests"}, "only used with"),
        ({"consentRule": "owner_attests"}, "together with consentMode"),
        ({"agenda": "x" * 10001}, "longer than 10000"),
    ],
)
def test_invalid_input_is_refused(appsync, write_vtl: str, fields: dict, message: str) -> None:
    assert message in _error(_write(appsync, write_vtl, fields, OWNER_IDENTITY, _row()))


@evaluated
def test_the_zoom_account_needs_the_schedulers_identity(appsync, write_vtl: str) -> None:
    row = _row()
    del row["userSub"]
    assert "without a user identity" in _error(
        _write(appsync, write_vtl, {"useStoredZoomAccount": True}, OWNER_IDENTITY, row)
    )


def _respond(appsync, resources: dict, error: dict | None, identity: dict, row) -> dict:
    template = resources["UpdateScheduledVirtualParticipantAppSyncFunction"]["Properties"][
        "ResponseMappingTemplate"
    ]
    context = {
        "arguments": {"input": {"id": "vp1"}},
        "identity": identity,
        "stash": {
            "vpFound": row is not None,
            "vpCallerRestricted": "groups" in identity and "Admin" not in identity["groups"],
            **({"vpAsRead": row} if row else {}),
        },
        "result": row,
        **({"error": error} if error else {}),
    }
    response = appsync.evaluate_mapping_template(template=template, context=json.dumps(context))
    if response.get("error"):
        return {"error": response["error"]["message"]}
    return json.loads(response["evaluationResult"])


CONDITION_FAILED = {"type": "DynamoDB:ConditionalCheckFailedException", "message": "failed"}


@evaluated
def test_a_launch_that_wins_the_race_refuses_the_edit(appsync, resources: dict) -> None:
    """The row was SCHEDULED when read, and the write's condition failed."""
    answer = _respond(appsync, resources, CONDITION_FAILED, OWNER_IDENTITY, _row())
    assert "changed while it was being edited" in _error(answer)


@evaluated
def test_a_refused_write_by_another_user_says_so(appsync, resources: dict) -> None:
    answer = _respond(appsync, resources, CONDITION_FAILED, OTHER_IDENTITY, _row())
    assert "is not authorized" in _error(answer)


@evaluated
def test_the_answer_says_whether_a_password_is_set_without_it(appsync, resources: dict) -> None:
    answer = _respond(appsync, resources, None, OWNER_IDENTITY, _row())
    assert answer["hasMeetingPassword"] is True
    assert "meetingPassword" not in answer
