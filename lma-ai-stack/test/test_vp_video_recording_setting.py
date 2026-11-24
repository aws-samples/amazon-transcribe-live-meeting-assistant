# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Meeting Recording › Record Virtual Participant meeting video, on the API side.

createVirtualParticipant reads the setting (the pipeline's second function, by
the meeting preparation resolver Lambda) and, when it is off and the request
asks for video, stores why on the row (videoRecordingOffReason). The Virtual
Participant writes the same field when it starts, through
updateVirtualParticipant. The Create Virtual Participant form reads the
setting with getVirtualParticipantVideoRecording.

The `evaluated` tests run the templates through AppSync's
EvaluateMappingTemplate API, which touches no resource; they run only with
LMA_EVALUATE_APPSYNC=1 (and AWS credentials), as in test_vp_launch_settings.py.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from ai_stack_templates import flattened

AI_STACK = Path(__file__).resolve().parents[1]
APPSYNC = AI_STACK / "source" / "appsync"
SCHEMA = (APPSYNC / "schema.graphql").read_text(encoding="utf-8")
UI = AI_STACK / "source" / "ui" / "src"
CHECK_REQUEST = (APPSYNC / "checkVirtualParticipantVideoRecording.request.vtl").read_text(
    encoding="utf-8"
)
CHECK_RESPONSE = (APPSYNC / "checkVirtualParticipantVideoRecording.response.vtl").read_text(
    encoding="utf-8"
)


@pytest.fixture(scope="module")
def resources() -> dict:
    return flattened()["Resources"]


def _block(header: str) -> str:
    start = SCHEMA.index(header)
    return SCHEMA[start : SCHEMA.index("\n}", start)]


def _template(resources: dict, name: str) -> str:
    request = resources[name]["Properties"]["RequestMappingTemplate"]
    return request["Fn::Sub"] if isinstance(request, dict) else request


def test_the_schema_declares_the_reason_and_the_read() -> None:
    assert re.search(
        r"^\tvideoRecordingOffReason: String$", _block("type VirtualParticipant "), re.M
    )
    update = _block("input UpdateVirtualParticipantInput")
    assert "\tvideoRecordingOffReason: String" in update
    assert "\tclearVideoRecordingOffReason: Boolean" in update
    # Never a create input: the resolver decides it.
    assert "videoRecordingOffReason" not in _block("input CreateVirtualParticipantInput")
    assert re.search(
        r"^\tgetVirtualParticipantVideoRecording: Boolean @aws_cognito_user_pools @aws_iam$",
        _block("type Query"),
        re.M,
    )


def test_the_create_pipeline_reads_the_setting_before_it_writes(resources: dict) -> None:
    functions = resources["CreateVirtualParticipantResolver"]["Properties"]["PipelineConfig"][
        "Functions"
    ]
    names = [
        entry["Fn::GetAtt"][0]
        if isinstance(entry["Fn::GetAtt"], list)
        else entry["Fn::GetAtt"].split(".")[0]
        for entry in functions
    ]
    assert names == [
        "ValidateMeetingPreparationAppSyncFunction",
        "CheckVirtualParticipantVideoRecordingAppSyncFunction",
        "CreateVirtualParticipantAppSyncFunction",
    ]
    check = resources["CheckVirtualParticipantVideoRecordingAppSyncFunction"]["Properties"]
    assert "MeetingPreparationLambdaDataSource" in json.dumps(check["DataSourceName"])
    query = resources["GetVirtualParticipantVideoRecordingAppSyncResolver"]["Properties"]
    assert query["FieldName"] == "getVirtualParticipantVideoRecording"
    assert "MeetingPreparationLambdaDataSource" in json.dumps(query["DataSourceName"])


def test_the_check_sends_nothing_of_the_request_and_skips_a_request_without_video() -> None:
    assert '"fieldName": "virtualParticipantVideoRecording"' in CHECK_REQUEST
    assert '"arguments": {}' in CHECK_REQUEST
    assert (
        "$util.defaultIfNull($ctx.args.input.enableVideoRecording, true) == false" in CHECK_REQUEST
    )
    assert CHECK_REQUEST.index("#return({})") < CHECK_REQUEST.index('"operation": "Invoke"')
    # A failed read is not the request's error.
    assert "$util.error" not in CHECK_RESPONSE
    assert '$ctx.stash.put("recordVpMeetingVideo", $ctx.result)' in CHECK_RESPONSE


def test_the_create_stores_the_reason_only_when_the_setting_refuses_requested_video(
    resources: dict,
) -> None:
    vtl = _template(resources, "CreateVirtualParticipantAppSyncFunction")
    assert (
        "#if($ctx.stash.recordVpMeetingVideo == false && "
        "$util.defaultIfNull($ctx.args.input.enableVideoRecording, true) != false)"
    ) in vtl
    assert '$util.qr($input.put("videoRecordingOffReason", "configuration"))' in vtl
    assert vtl.index('"videoRecordingOffReason"') < vtl.index('"operation": "PutItem"')


def test_the_update_sets_or_removes_the_reason_and_accepts_only_its_two_values(
    resources: dict,
) -> None:
    vtl = _template(resources, "UpdateVirtualParticipantResolver")
    assert '#set( $videoOffReasons = ["configuration", "deployment"] )' in vtl
    assert "$util.defaultIfNull($ctx.args.input.clearVideoRecordingOffReason, false) == true" in vtl
    assert '$util.qr( $expRemove.add("#videoRecordingOffReason") )' in vtl


def test_the_ui_reads_the_reason_and_the_setting() -> None:
    details = (
        UI / "components" / "virtual-participant-layout" / "VirtualParticipantDetails.jsx"
    ).read_text(encoding="utf-8")
    assert re.search(r"^\s+videoRecordingOffReason$", details, re.M)
    hook = (UI / "hooks" / "use-vp-video-recording-setting.js").read_text(encoding="utf-8")
    assert "getVirtualParticipantVideoRecording" in hook


def test_the_vp_task_carries_the_deployment_switch() -> None:
    template = (AI_STACK.parent / "lma-virtual-participant-stack" / "template.yaml").read_text(
        encoding="utf-8"
    )
    assert re.search(
        r"- Name: DEPLOYMENT_VIDEO_RECORDING\n\s+Value: !Ref EnableVideoRecording\n", template
    ), (
        "the Virtual Participant task does not pass EnableVideoRecording as DEPLOYMENT_VIDEO_RECORDING"
    )


# ── evaluated by AppSync ────────────────────────────────────────────────────

_when_asked = pytest.mark.skipif(
    os.environ.get("LMA_EVALUATE_APPSYNC") != "1",
    reason="evaluates templates with the AppSync API; set LMA_EVALUATE_APPSYNC=1 to run",
)


def evaluated(test):
    return pytest.mark.real_aws(_when_asked(test))


SIGNED_IN = {"username": "owner@example.com", "groups": [], "sub": "sub-1"}
REQUEST = {
    "meetingName": "m",
    "meetingPlatform": "ZOOM",
    "meetingId": "1",
    "status": "INITIALIZING",
}


@pytest.fixture(scope="module")
def appsync():
    import boto3

    return boto3.client("appsync", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _stored(appsync, resources: dict, request: dict, stash: dict) -> dict:
    response = appsync.evaluate_mapping_template(
        template=_template(resources, "CreateVirtualParticipantAppSyncFunction"),
        context=json.dumps(
            {"arguments": {"input": request}, "identity": SIGNED_IN, "stash": stash}
        ),
    )
    assert not response.get("error"), response.get("error")
    return json.loads(response["evaluationResult"])["attributeValues"]


@evaluated
@pytest.mark.parametrize(
    "requested, stash, reason",
    [
        (None, {"recordVpMeetingVideo": False}, "configuration"),
        (True, {"recordVpMeetingVideo": False}, "configuration"),
        (False, {"recordVpMeetingVideo": False}, None),
        (True, {"recordVpMeetingVideo": True}, None),
        (True, {}, None),
    ],
)
def test_create_records_why_the_setting_refuses_the_video(
    appsync, resources, requested, stash, reason
) -> None:
    request = dict(REQUEST) if requested is None else {**REQUEST, "enableVideoRecording": requested}
    stored = _stored(appsync, resources, request, stash)
    assert stored.get("videoRecordingOffReason") == ({"S": reason} if reason else None)
    if requested is not None:
        assert stored["enableVideoRecording"] == {"BOOL": requested}, "the request is kept as asked"


@evaluated
def test_the_check_is_skipped_for_a_request_without_video(appsync) -> None:
    skipped = appsync.evaluate_mapping_template(
        template=CHECK_REQUEST,
        context=json.dumps({"arguments": {"input": {**REQUEST, "enableVideoRecording": False}}}),
    )
    assert not skipped.get("error"), skipped.get("error")
    invoked = appsync.evaluate_mapping_template(
        template=CHECK_REQUEST, context=json.dumps({"arguments": {"input": REQUEST}})
    )
    assert json.loads(invoked["evaluationResult"])["payload"]["info"] == {
        "fieldName": "virtualParticipantVideoRecording"
    }
