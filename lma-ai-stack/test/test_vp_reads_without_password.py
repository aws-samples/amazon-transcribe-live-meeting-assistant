# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""No read of a Virtual Participant returns its meeting password.

The password is stored on the row for the launch paths, which take it from the
row (the VPScheduler Lambda, from the table's stream) or from their own input
(the launcher state machine and the MicroVM launcher). Every GraphQL answer that
carries a VirtualParticipant -- getVirtualParticipant, listVirtualParticipants,
and the create, update and end mutations whose answers are also published to
the onCreate/onUpdate subscriptions -- answers null for meetingPassword, to
every caller.

The `evaluated` tests run the templates through AppSync's EvaluateMappingTemplate
and EvaluateCode APIs, which touch no resource; they run only with
LMA_EVALUATE_APPSYNC=1 (and AWS credentials), as in
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
REPO = AI_STACK.parent
TEMPLATE = AI_STACK / "deployment" / "lma-ai-stack.yaml"
APPSYNC = AI_STACK / "source" / "appsync"
SCHEMA = APPSYNC / "schema.graphql"

OWNER = "owner@example.com"
IDENTITIES = {
    "owner": {"username": OWNER, "groups": []},
    "admin": {"username": "admin@example.com", "groups": ["Admin"]},
    "backend": {"userArn": "arn:aws:sts::123456789012:assumed-role/LambdaRole/session"},
}
ROW = {"id": "vp1", "Owner": OWNER, "SharedWith": "", "meetingPassword": "pw", "meetingId": "1"}


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


def _create_response(resources: dict) -> str:
    return resources["CreateVirtualParticipantResolver"]["Properties"]["ResponseMappingTemplate"]


# ── text ────────────────────────────────────────────────────────────────────


def test_the_schema_keeps_the_field_and_marks_it_deprecated() -> None:
    sdl = SCHEMA.read_text(encoding="utf-8")
    start = sdl.index("type VirtualParticipant ")
    block = sdl[start : sdl.index("\n}", start)]
    assert re.search(r"^\tmeetingPassword: String @deprecated\(reason: ", block, re.M)


@pytest.mark.parametrize(
    "name",
    [
        "getVirtualParticipant.response.vtl",
        "listVirtualParticipants.response.vtl",
        "updateVirtualParticipant.response.vtl",
    ],
)
def test_each_read_template_drops_the_password(name: str) -> None:
    assert '.remove("meetingPassword")' in (APPSYNC / name).read_text(encoding="utf-8")


def test_the_create_answer_drops_the_password(resources: dict) -> None:
    assert '$ctx.result.remove("meetingPassword")' in _create_response(resources)


def test_the_end_answer_drops_the_password() -> None:
    assert "body.meetingPassword = null;" in (APPSYNC / "endVirtualParticipant.js").read_text()


def test_no_graphql_client_in_the_repository_reads_the_password() -> None:
    """The launch paths read it from the row or their input, never through
    GraphQL; a selection of it would only ever receive null."""
    readers = []
    for directory in ("lma-ai-stack/source", "lib", "lma-virtual-participant-stack/backend/src"):
        for path in (REPO / directory).rglob("*"):
            if path.suffix not in {".py", ".ts", ".js", ".jsx"}:
                continue
            if {"node_modules", "dist", "build"} & set(path.parts) or path.name == "mutations.js":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for query in re.findall(
                r"(?:query|subscription)\s+\w+[^`\"]*?\{[^`]*?(?:`|\"\"\")", text
            ):
                if re.search(r"^\s*meetingPassword\s*$", query, re.M):
                    readers.append(path.relative_to(REPO).as_posix())
    assert not readers, readers


# ── evaluated by AppSync ────────────────────────────────────────────────────

evaluated = pytest.mark.skipif(
    os.environ.get("LMA_EVALUATE_APPSYNC") != "1",
    reason="evaluates templates with the AppSync API; set LMA_EVALUATE_APPSYNC=1 to run",
)


@pytest.fixture(scope="module")
def appsync():
    import boto3  # imported here so the module collects without boto3 when skipped

    return boto3.client("appsync", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _evaluate(appsync, template: str, context: dict):
    response = appsync.evaluate_mapping_template(template=template, context=json.dumps(context))
    assert not response.get("error"), response.get("error")
    return json.loads(response["evaluationResult"])


@evaluated
@pytest.mark.parametrize("caller", sorted(IDENTITIES))
def test_get_answers_no_password_to_any_caller(appsync, caller: str) -> None:
    template = (APPSYNC / "getVirtualParticipant.response.vtl").read_text(encoding="utf-8")
    answer = _evaluate(appsync, template, {"result": dict(ROW), "identity": IDENTITIES[caller]})
    assert "meetingPassword" not in answer
    assert answer["meetingId"] == "1"


@evaluated
@pytest.mark.parametrize("caller", ["owner", "admin"])
def test_list_answers_no_password_to_any_caller(appsync, caller: str) -> None:
    template = (APPSYNC / "listVirtualParticipants.response.vtl").read_text(encoding="utf-8")
    identity = IDENTITIES[caller]
    answer = _evaluate(appsync, template, {"result": {"items": [dict(ROW)]}, "identity": identity})
    assert len(answer) == 1
    assert "meetingPassword" not in answer[0]


@evaluated
def test_update_and_create_answers_carry_no_password(appsync, resources: dict) -> None:
    update = (APPSYNC / "updateVirtualParticipant.response.vtl").read_text(encoding="utf-8")
    for template in (update, _create_response(resources)):
        answer = _evaluate(
            appsync, template, {"result": dict(ROW), "identity": IDENTITIES["owner"]}
        )
        assert "meetingPassword" not in answer


@evaluated
def test_the_end_answer_carries_no_password(appsync) -> None:
    response = appsync.evaluate_code(
        runtime={"name": "APPSYNC_JS", "runtimeVersion": "1.0.0"},
        code=(APPSYNC / "endVirtualParticipant.js").read_text(encoding="utf-8"),
        context=json.dumps({"result": {"statusCode": 200, "body": dict(ROW)}, "arguments": {}}),
        function="response",
    )
    assert not response.get("error"), response
    assert json.loads(response["evaluationResult"])["meetingPassword"] is None


@evaluated
@pytest.mark.parametrize("name", ["endVirtualParticipant.js", "deleteVirtualParticipants.js"])
def test_a_refusal_from_the_lambda_is_an_authorization_error(appsync, name: str) -> None:
    body = {"error": "User bob is not authorized to change Virtual Participant vp1"}
    response = appsync.evaluate_code(
        runtime={"name": "APPSYNC_JS", "runtimeVersion": "1.0.0"},
        code=(APPSYNC / name).read_text(encoding="utf-8"),
        context=json.dumps(
            {
                "result": {"statusCode": 403, "body": {**body, "errorType": "AuthorizationError"}},
                "arguments": {},
            }
        ),
        function="response",
    )
    assert "not authorized" in response["error"]["message"]
    # The evaluator reports the message only; the type is passed through as is.
    assert "result.body.errorType || 'InternalError'" in (APPSYNC / name).read_text()
