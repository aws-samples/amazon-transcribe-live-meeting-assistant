# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Static tests for the VNC ALB gating in the AI stack template.

The VNC ALB exists only to route the UI's noVNC connection to an ECS task's
websockify port. Under VPLaunchType=MICROVM each MicroVM returns its own HTTPS
endpoint from RunMicrovm, so the ALB has no targets and is pure cost -- roughly
$16-20/month for a load balancer that can never serve a request.

Gating it is easy to get wrong in a way CloudFormation only catches at deploy
time: a resource that survives (the CloudFront distribution, the VP manager
Lambda) may still reference one that does not, which fails the whole stack
update. These tests walk the template and assert every reference to a gated
resource sits inside a ShouldCreateVNCALB branch.

Lives in the VP stack's test directory because that is what `make
test-vp-template` runs; the resources are in the AI stack but exist solely for
the Virtual Participant.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "lma-ai-stack"
    / "deployment"
    / "lma-ai-stack.yaml"
)

CONDITION = "ShouldCreateVNCALB"


class _CfnLoader(yaml.SafeLoader):
    """YAML loader that tolerates CloudFormation short-form intrinsics."""


def _intrinsic(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):  # noqa: ANN202
    name = f"Fn::{tag_suffix}" if tag_suffix != "Ref" else "Ref"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node)}
    return {name: loader.construct_mapping(node)}


_CfnLoader.add_multi_constructor("!", _intrinsic)


@pytest.fixture(scope="module")
def template() -> dict:
    return yaml.load(TEMPLATE.read_text(), Loader=_CfnLoader)


@pytest.fixture(scope="module")
def gated(template: dict) -> set[str]:
    return {
        name
        for name, body in template["Resources"].items()
        if body.get("Condition") == CONDITION
    }


def _unguarded_refs(node, gated: set[str], guarded: bool, path: str, out: list) -> None:
    """Collect Ref/GetAtt targets in `gated` that are NOT inside an Fn::If
    branch guarded by ShouldCreateVNCALB."""
    if isinstance(node, dict):
        if len(node) == 1 and "Fn::If" in node:
            branches = node["Fn::If"]
            if isinstance(branches, list) and len(branches) == 3:
                cond = branches[0] if isinstance(branches[0], str) else None
                # The true branch inherits the guard; the false branch does not.
                _unguarded_refs(
                    branches[1],
                    gated,
                    guarded or cond == CONDITION,
                    f"{path}/If[true]",
                    out,
                )
                _unguarded_refs(branches[2], gated, guarded, f"{path}/If[false]", out)
                return
        for key, value in node.items():
            if key in ("Ref", "Fn::GetAtt"):
                target = value if isinstance(value, str) else (value or [""])[0]
                target = str(target).split(".")[0]
                if target in gated and not guarded:
                    out.append(f"{path} -> {key} {target}")
            else:
                _unguarded_refs(value, gated, guarded, f"{path}/{key}", out)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _unguarded_refs(item, gated, guarded, f"{path}[{index}]", out)


def test_vp_launch_type_parameter_exists(template: dict) -> None:
    """The AI stack needs VPLaunchType to know whether an ALB is wanted.

    It previously did not receive this parameter at all, which is why a MICROVM
    deployment still paid for an ALB with zero registered targets.
    """
    param = template["Parameters"]["VPLaunchType"]
    assert set(param["AllowedValues"]) == {"EC2", "FARGATE", "MICROVM"}


def test_condition_is_not_microvm(template: dict) -> None:
    """Deliberately "not MICROVM" rather than "is EC2 or FARGATE".

    The ALB is needed by anything that routes through an ECS task, so a future
    ECS-shaped launch type should get one by default rather than silently
    losing its VNC path.
    """
    cond = template["Conditions"][CONDITION]
    assert cond == {"Fn::Not": [{"Fn::Equals": [{"Ref": "VPLaunchType"}, "MICROVM"]}]}


def test_expected_resources_are_gated(gated: set[str]) -> None:
    """Everything whose only purpose is the VNC ALB path."""
    for name in (
        "VNCALB",
        "VNCTargetGroup",
        "VNCALBListener",
        "ALBSecurityGroup",
        "ALBToECSEgressRule",
        "ECSFromALBIngressRule",
        # Only the ALB security group consumes the CloudFront prefix list.
        "CloudFrontPrefixList",
        "GetCloudFrontPrefixListFunction",
        "GetCloudFrontPrefixListRole",
        # Authenticates the /vnc/* behavior; MicroVM uses a port-scoped auth
        # token instead, so this replicated edge function would sit unused.
        "EdgeAuthFunction",
    ):
        assert name in gated, f"{name} must be gated on {CONDITION}"


def test_vp_security_group_is_never_gated(template: dict) -> None:
    """The VP task security group is shared, not ALB-specific.

    Under MICROVM it is reused as the egress connector's security group, so
    gating it would break MicroVM networking entirely.
    """
    sg = template["Resources"]["VPSecurityGroup"]
    assert "Condition" not in sg


def test_no_surviving_resource_references_a_gated_resource(
    template: dict, gated: set[str]
) -> None:
    """The failure this whole test file exists to prevent.

    An ungated resource referencing a gated one makes the MICROVM stack update
    fail with an unresolved-reference error after ~25 minutes of deploying.
    """
    problems: list[str] = []
    for name, body in template["Resources"].items():
        if name in gated:
            continue
        _unguarded_refs(body, gated, False, name, problems)
    assert not problems, "unguarded references to conditional resources:\n" + "\n".join(
        problems
    )


def test_alb_outputs_fall_back_to_empty_string(template: dict) -> None:
    """Outputs must still resolve under MICROVM.

    The VP stack takes these as plain String parameters and only uses them on
    the ECS launch paths, so an empty string is correct -- but an output that
    Refs a non-existent resource fails the stack.
    """
    outputs = template["Outputs"]
    for name in ("VNCTargetGroupArn", "VNCALBSecurityGroupId", "VNCALBListenerArn"):
        value = outputs[name]["Value"]
        assert "Fn::If" in value, f"{name} must be conditional"
        branches = value["Fn::If"]
        assert branches[0] == CONDITION
        assert branches[2] == "", f"{name} must fall back to an empty string"


def test_alb_listener_output_is_not_exported(template: dict) -> None:
    """An exported empty string is still an export.

    Export values cannot change while any stack imports them, so keeping the
    export would add an update constraint for no benefit -- nothing in the
    solution imports it (the VP stack receives it as a parameter).
    """
    assert "Export" not in template["Outputs"]["VNCALBListenerArn"]


def test_cloudfront_omits_the_vnc_behavior_under_microvm(template: dict) -> None:
    """No /vnc/* cache behavior when there is no ALB to send it to."""
    dist = template["Resources"]["WebAppCloudFrontDistribution"]["Properties"][
        "DistributionConfig"
    ]
    behaviors = dist["CacheBehaviors"]
    assert "Fn::If" in behaviors
    assert behaviors["Fn::If"][0] == CONDITION
    # MICROVM branch drops the property entirely rather than sending an empty
    # list, which CloudFront rejects.
    assert behaviors["Fn::If"][2] == {"Ref": "AWS::NoValue"}

    origins = dist["Origins"]
    assert "Fn::If" in origins
    microvm_origins = origins["Fn::If"][2]
    assert len(microvm_origins) == 1
    assert microvm_origins[0]["Id"] == "webapp-s3-bucket"
    assert "vnc-alb" not in json.dumps(microvm_origins, default=str)


def test_vp_manager_alb_listener_env_tolerates_no_alb(template: dict) -> None:
    """The manager's ALB cleanup is skipped when the variable is empty.

    index.py guards with `if listener_arn:`, so "" disables cleanup rather than
    raising -- which is correct, because there are no per-VP listener rules or
    target groups to clean up under MICROVM.
    """
    env = template["Resources"]["VirtualParticipantManagerFunction"]["Properties"][
        "Environment"
    ]["Variables"]
    value = env["ALB_LISTENER_ARN"]
    assert value["Fn::If"][0] == CONDITION
    assert value["Fn::If"][2] == ""

    manager = (
        TEMPLATE.parents[1]
        / "source"
        / "lambda_functions"
        / "virtual_participant_manager"
        / "index.py"
    ).read_text()
    assert re.search(r'listener_arn = os\.environ\.get\("ALB_LISTENER_ARN"\)', manager)
    assert "if listener_arn:" in manager


# --------------------------------------------------------------------------
# The /vnc/* access token
#
# The viewer calls createVncEdgeToken(vpId), which checks the caller's access to
# that VP and returns a short-lived token signed with a per-deployment key. The
# Lambda@Edge viewer-request function on the /vnc/* behavior checks the signature
# before CloudFront forwards to the ALB. The two halves cannot share a module --
# Lambda@Edge supports no layers and the deployer embeds its source as a string
# literal -- so the wiring between them is asserted here.
# --------------------------------------------------------------------------

DEPLOYER_INDEX = (
    TEMPLATE.parents[1] / "source" / "lambda_functions" / "edge_auth_deployer" / "index.py"
)
MINTER_INDEX = (
    TEMPLATE.parents[1] / "source" / "lambda_functions" / "vnc_edge_token" / "index.py"
)
SCHEMA = TEMPLATE.parents[1] / "source" / "appsync" / "schema.graphql"


@pytest.fixture(scope="module")
def edge_code() -> str:
    """The edge function source, as the deployer will zip it."""
    source = DEPLOYER_INDEX.read_text()
    match = re.search(r"EDGE_FUNCTION_CODE = r?('''|\"\"\")(.*?)\1", source, re.S)
    assert match, "EDGE_FUNCTION_CODE should be a string literal in the deployer"
    return match.group(2)


def test_the_signing_key_is_generated_per_deployment(template: dict) -> None:
    """Not a parameter and not a fixed value: nothing outside the stack knows it."""
    secret = template["Resources"]["VncEdgeTokenSigningSecret"]
    assert secret["Type"] == "AWS::SecretsManager::Secret"
    generate = secret["Properties"]["GenerateSecretString"]
    assert generate["PasswordLength"] >= 32
    assert secret["Properties"]["KmsKeyId"] == {"Ref": "CustomerManagedEncryptionKeyArn"}


def test_the_token_resources_are_not_gated_on_the_alb_condition(template: dict) -> None:
    """EdgeAuthFunctionRole is unconditional and reads the secret.

    A gated secret would leave that role with an unresolvable reference under
    MICROVM, which fails the stack update rather than just skipping the ALB.
    """
    for name in (
        "VncEdgeTokenSigningSecret",
        "VncEdgeTokenFunction",
        "VncEdgeTokenFunctionRole",
        "CreateVncEdgeTokenResolver",
    ):
        assert "Condition" not in template["Resources"][name], f"{name} must be unconditional"


def test_the_edge_role_reads_only_the_one_secret(template: dict) -> None:
    statements = template["Resources"]["EdgeAuthFunctionRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    reads = [s for s in statements if "secretsmanager:GetSecretValue" in json.dumps(s["Action"])]
    assert len(reads) == 1
    assert reads[0]["Resource"] == {"Ref": "VncEdgeTokenSigningSecret"}


def test_the_edge_function_is_told_where_to_resolve_the_secret(template: dict) -> None:
    """Replicas run in every region; the secret exists in exactly one.

    Lambda@Edge takes no environment variables, so both values are substituted
    into the source at deploy time.
    """
    props = template["Resources"]["EdgeAuthFunction"]["Properties"]
    assert props["SigningSecretArn"] == {"Ref": "VncEdgeTokenSigningSecret"}
    assert props["SigningSecretRegion"] == {"Ref": "AWS::Region"}
    assert "PLACEHOLDER" in props["CodeHash"], (
        "the build substitutes CodeHash so a source change redeploys the edge function"
    )


def test_a_source_change_still_triggers_a_redeploy() -> None:
    """The Makefile hashes the deployer sources into the custom resource."""
    makefile = (TEMPLATE.parents[1] / "Makefile").read_text()
    assert "EDGE_AUTH_CODE_HASH_PLACEHOLDER" in makefile
    assert "edge_auth_deployer" in makefile


def test_the_edge_function_has_no_third_party_imports(edge_code: str) -> None:
    """Lambda@Edge cannot attach layers, so only the standard library and boto3.

    boto3 ships in the Lambda Python runtime itself.
    """
    imports = set(re.findall(r"^\s*import (\w+)", edge_code, re.M))
    imports |= set(re.findall(r"^\s*from (\w+)", edge_code, re.M))
    allowed = {"base64", "hashlib", "hmac", "json", "time", "urllib", "boto3"}
    assert imports <= allowed, f"unavailable at the edge: {sorted(imports - allowed)}"


def test_the_edge_function_compiles(edge_code: str) -> None:
    """It is a string literal, so nothing else would catch a syntax error."""
    compile(edge_code, "edge_function.py", "exec")


def test_the_edge_function_compares_signatures_in_constant_time(edge_code: str) -> None:
    assert "compare_digest" in edge_code


def test_the_edge_function_logs_neither_the_token_nor_the_request_uri(edge_code: str) -> None:
    """Edge logs land in every replica region; keep request detail out of them."""
    for printed in re.findall(r"print\((.*?)\)\s*$", edge_code, re.M):
        assert "token" not in printed.lower(), printed
        assert "uri" not in printed.lower(), printed


def test_the_resolver_field_matches_the_schema(template: dict) -> None:
    resolver = template["Resources"]["CreateVncEdgeTokenResolver"]["Properties"]
    assert resolver["TypeName"] == "Mutation"
    field = resolver["FieldName"]
    schema = SCHEMA.read_text()
    assert re.search(rf"^\s*{field}\(vpId: ID!\)", schema, re.M), (
        f"{field} should be declared on Mutation"
    )


def test_the_minted_token_is_short_lived() -> None:
    """A token that leaves the browser stops being useful quickly."""
    minter = MINTER_INDEX.read_text()
    match = re.search(r"TOKEN_TTL_SECONDS = (\d+)", minter)
    assert match, "the minter should name its TTL"
    assert 0 < int(match.group(1)) <= 900


def test_the_minter_reads_the_vp_table_and_the_secret_only(template: dict) -> None:
    """Least privilege: one table item read and one secret read."""
    statements = template["Resources"]["VncEdgeTokenFunctionRole"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"]
    actions = json.dumps([s["Action"] for s in statements])
    assert "dynamodb:GetItem" in actions
    for write in (
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Scan",
    ):
        assert write not in actions, f"the minter does not need {write}"
