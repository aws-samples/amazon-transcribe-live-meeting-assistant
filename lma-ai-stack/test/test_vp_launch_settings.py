# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""What a Virtual Participant was created with, as its page reads it back.

The Create Virtual Participant form's values are stored on the VP row by the
createVirtualParticipant resolver and read by getVirtualParticipant. These tests
hold the schema, the resolver templates and the page's query to each other.

The `evaluated` tests run the templates through AppSync's EvaluateMappingTemplate
API, which evaluates a template against a supplied context and touches no
resource; they run only with LMA_EVALUATE_APPSYNC=1 (and AWS credentials), as in
test_shared_with_access_evaluated.py.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

AI_STACK = Path(__file__).resolve().parents[1]
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
APPSYNC = AI_STACK / "source" / "appsync"
SCHEMA = APPSYNC / "schema.graphql"
UI_VP = AI_STACK / "source" / "ui" / "src" / "components" / "virtual-participant-layout"

# Stored by createVirtualParticipant from the form, and read back by the page.
FORM_FIELDS_STORED = (
    "meetingName",
    "meetingPlatform",
    "meetingId",
    "meetingTime",
    "isScheduled",
    "enableVideoRecording",
    "agenda",
    "consentMode",
    "consentRule",
    "scheduledTimeZone",
    "useStoredZoomAccount",
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


def _create_template(resources: dict) -> str:
    return resources["CreateVirtualParticipantResolver"]["Properties"]["RequestMappingTemplate"]


def _page_query() -> str:
    page = (UI_VP / "VirtualParticipantDetails.jsx").read_text(encoding="utf-8")
    start = page.index("query GetVirtualParticipant(")
    return page[start : page.index("`;", start)]


# ── schema ──────────────────────────────────────────────────────────────────


def test_the_vp_type_declares_every_stored_form_field(schema_sdl: str) -> None:
    vp = _block(schema_sdl, "type VirtualParticipant ")
    for field in (*FORM_FIELDS_STORED, "hasMeetingPassword"):
        assert re.search(rf"^\t{field}: ", vp, re.M), f"VirtualParticipant does not declare {field}"


def test_the_create_input_accepts_the_new_fields(schema_sdl: str) -> None:
    create = _block(schema_sdl, "input CreateVirtualParticipantInput")
    assert "\tscheduledTimeZone: String" in create
    assert "\tuseStoredZoomAccount: Boolean" in create
    # Answered by the read, never sent.
    assert "hasMeetingPassword" not in create


# ── createVirtualParticipant ────────────────────────────────────────────────


def test_the_create_resolver_stores_the_new_fields(resources: dict) -> None:
    vtl = _create_template(resources)
    assert '$util.qr($input.put("scheduledTimeZone", $ctx.args.input.scheduledTimeZone))' in vtl
    assert (
        '$util.qr($input.put("useStoredZoomAccount", $ctx.args.input.useStoredZoomAccount))' in vtl
    )
    # A Boolean false is a choice, so it is stored; only an absent one is skipped.
    assert "#if(!$util.isNull($ctx.args.input.useStoredZoomAccount))" in vtl
    assert "scheduledTimeZone.length() > 64" in vtl


def test_the_create_resolver_writes_its_put_after_the_new_fields(resources: dict) -> None:
    vtl = _create_template(resources)
    assert vtl.index('$input.put("scheduledTimeZone"') < vtl.index('"operation": "PutItem"')


# ── getVirtualParticipant ───────────────────────────────────────────────────


def test_the_read_answers_whether_a_password_was_given() -> None:
    vtl = (APPSYNC / "getVirtualParticipant.response.vtl").read_text(encoding="utf-8")
    assert (
        "#set( $hasMeetingPassword = !$util.isNullOrBlank($context.result.meetingPassword) )"
    ) in vtl
    assert '$context.result.put("hasMeetingPassword", $hasMeetingPassword)' in vtl
    # Derived before the access decision, so every reader the rule admits sees
    # it and nobody else does: the access rule itself is unchanged.
    assert vtl.index("hasMeetingPassword") < vtl.index("#if($context.identity.userArn)")


def test_the_page_selects_every_stored_form_field_and_not_the_password() -> None:
    query = _page_query()
    selected = set(re.findall(r"^\s+(\w+)\s*$", query, re.M))
    for field in (*FORM_FIELDS_STORED, "hasMeetingPassword", "agendaSource", "scheduledFor"):
        assert field in selected, f"the Virtual Participant page does not select {field}"
    assert "meetingPassword" not in selected
    assert "userZoomSub" not in selected


def test_the_form_sends_the_new_fields() -> None:
    form = (UI_VP / "VirtualParticipantList.jsx").read_text(encoding="utf-8")
    assert "vpInput.scheduledTimeZone = timeZone;" in form
    assert "vpInput.useStoredZoomAccount = !!userZoomSub;" in form


# ── evaluated by AppSync ────────────────────────────────────────────────────

_when_asked = pytest.mark.skipif(
    os.environ.get("LMA_EVALUATE_APPSYNC") != "1",
    reason="evaluates templates with the AppSync API; set LMA_EVALUATE_APPSYNC=1 to run",
)


def evaluated(test):
    """Run only when asked, and allowed to call AWS (see scripts/testing/no_real_aws.py)."""
    return pytest.mark.real_aws(_when_asked(test))


SIGNED_IN = {"username": "owner@example.com", "groups": [], "sub": "sub-1"}


@pytest.fixture(scope="module")
def appsync():
    import boto3  # imported here so the module collects without boto3 when skipped

    return boto3.client("appsync", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _evaluate(appsync, template: str, context: dict) -> dict:
    return appsync.evaluate_mapping_template(template=template, context=json.dumps(context))


@evaluated
def test_create_stores_the_time_zone_and_a_false_zoom_choice(appsync, resources: dict) -> None:
    arguments = {
        "input": {
            "meetingName": "Weekly sync",
            "meetingPlatform": "ZOOM",
            "meetingId": "123",
            "status": "SCHEDULED",
            "meetingTime": 1788299100,
            "isScheduled": True,
            "scheduledTimeZone": "America/New_York",
            "useStoredZoomAccount": False,
        }
    }
    response = _evaluate(
        appsync, _create_template(resources), {"arguments": arguments, "identity": SIGNED_IN}
    )
    assert not response.get("error"), response.get("error")
    stored = json.loads(response["evaluationResult"])["attributeValues"]
    assert stored["scheduledTimeZone"] == {"S": "America/New_York"}
    # The instant as an ISO date-time, which the page and the status card read.
    assert stored["scheduledFor"] == {"S": "2026-09-01T21:45:00Z"}
    assert stored["useStoredZoomAccount"] == {"BOOL": False}


@evaluated
def test_create_refuses_an_overlong_time_zone(appsync, resources: dict) -> None:
    arguments = {
        "input": {
            "meetingName": "m",
            "meetingPlatform": "ZOOM",
            "meetingId": "1",
            "status": "SCHEDULED",
            "scheduledTimeZone": "x" * 65,
        }
    }
    response = _evaluate(
        appsync, _create_template(resources), {"arguments": arguments, "identity": SIGNED_IN}
    )
    assert "longer than 64" in response["error"]["message"]


@evaluated
@pytest.mark.parametrize(
    "row,expected",
    [
        ({"meetingPassword": "pw"}, (True, None)),
        ({"meetingPassword": ""}, (False, None)),
        ({}, (False, None)),
        ({"userZoomSub": "sub-1"}, (False, True)),
        ({"userZoomSub": "sub-1", "useStoredZoomAccount": False}, (False, False)),
    ],
)
def test_the_read_answers_the_derived_fields(appsync, row: dict, expected: tuple) -> None:
    template = (APPSYNC / "getVirtualParticipant.response.vtl").read_text(encoding="utf-8")
    result = {"id": "vp1", "Owner": SIGNED_IN["username"], "SharedWith": "", **row}
    response = _evaluate(appsync, template, {"result": result, "identity": SIGNED_IN})
    assert not response.get("error"), response.get("error")
    answered = json.loads(response["evaluationResult"])
    assert (answered["hasMeetingPassword"], answered.get("useStoredZoomAccount")) == expected


@evaluated
def test_the_read_still_refuses_another_user(appsync) -> None:
    template = (APPSYNC / "getVirtualParticipant.response.vtl").read_text(encoding="utf-8")
    result = {
        "id": "vp1",
        "Owner": "someone@example.com",
        "SharedWith": "",
        "meetingPassword": "pw",
    }
    response = _evaluate(appsync, template, {"result": result, "identity": SIGNED_IN})
    assert "not authorized" in response["error"]["message"]


@evaluated
def test_the_read_of_a_missing_row_is_unchanged_for_a_backend_role(appsync) -> None:
    template = (APPSYNC / "getVirtualParticipant.response.vtl").read_text(encoding="utf-8")
    identity = {"userArn": "arn:aws:sts::123456789012:assumed-role/LambdaRole/session"}
    response = _evaluate(appsync, template, {"result": None, "identity": identity})
    assert not response.get("error"), response.get("error")
    assert json.loads(response["evaluationResult"]) is None
