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


# --------------------------------------------------------------------------
# Tying the load balancer to this deployment's own distribution
#
# The VNC ALB is internet-facing, because CloudFront needs a publicly resolvable
# origin unless VPC origins are used, and its security group admits the
# com.amazonaws.global.cloudfront.origin-facing managed prefix list -- CloudFront's
# shared origin-facing range, which establishes that a request came from
# CloudFront but not from which distribution. An origin-verify header is what
# identifies this deployment's own distribution. These tests exist so a later
# template edit cannot quietly drop it.
# --------------------------------------------------------------------------

ORIGIN_VERIFY_SECRET = "VncOriginVerifyHeaderValueSecret"


def _vnc_origin(template: dict) -> dict:
    origins = template["Resources"]["WebAppCloudFrontDistribution"]["Properties"][
        "DistributionConfig"
    ]["Origins"]["Fn::If"][1]
    matches = [o for o in origins if o["Id"] == "vnc-alb"]
    assert matches, "the vnc-alb origin should exist on the ECS branch"
    return matches[0]


def test_the_listener_refuses_a_request_matching_no_rule(template: dict) -> None:
    """Every route to a participant is a rule that states its condition.

    The ECS service registers its tasks into VNCTargetGroup, so the default action
    is a live route; keeping it non-forwarding is what makes "matches no rule" and
    "reaches a task" mutually exclusive.
    """
    actions = template["Resources"]["VNCALBListener"]["Properties"]["DefaultActions"]
    assert len(actions) == 1
    assert actions[0]["Type"] == "fixed-response"
    assert actions[0]["FixedResponseConfig"]["StatusCode"] == "403"


def test_the_shared_target_group_is_reached_only_with_the_origin_verify_header(
    template: dict,
) -> None:
    rule = template["Resources"]["VNCALBListenerRule"]["Properties"]
    assert rule["Actions"][0]["TargetGroupArn"] == {"Ref": "VNCTargetGroup"}
    header = [c for c in rule["Conditions"] if c["Field"] == "http-header"]
    assert len(header) == 1, "the rule must require the origin-verify header"
    values = header[0]["HttpHeaderConfig"]["Values"]
    assert len(values) == 1
    assert ORIGIN_VERIFY_SECRET in json.dumps(values), (
        "the expected value must come from the generated secret, not a literal"
    )


def test_the_distribution_sends_the_origin_verify_header(template: dict) -> None:
    headers = _vnc_origin(template)["OriginCustomHeaders"]
    assert len(headers) == 1
    assert ORIGIN_VERIFY_SECRET in json.dumps(headers[0]["HeaderValue"])


def test_the_header_name_matches_everywhere_it_is_spelled(template: dict) -> None:
    """Three places, and the template has no Mappings section for a constant.

    A mismatch would not fail the deploy. The listener rule would simply never
    match, and every viewer would get the default 403. The third place is the
    published output, which is what reaches the Virtual Participant so the rule it
    creates for itself carries the same name.
    """
    sent = _vnc_origin(template)["OriginCustomHeaders"][0]["HeaderName"]
    rule = template["Resources"]["VNCALBListenerRule"]["Properties"]
    required = [c for c in rule["Conditions"] if c["Field"] == "http-header"][0][
        "HttpHeaderConfig"
    ]["HttpHeaderName"]
    published = template["Outputs"]["VncOriginVerifyHeaderName"]["Value"]
    names = {sent, required, published}
    assert all(isinstance(n, str) for n in names), f"all three must be literals: {names}"
    assert len({n.lower() for n in names}) == 1, f"header name differs between places: {names}"


# --------------------------------------------------------------------------
# Reaching the Virtual Participant, which creates one listener rule per
# participant at run time and needs the same condition on it
# --------------------------------------------------------------------------

VP_TEMPLATE = TEMPLATE.parents[2] / "lma-virtual-participant-stack" / "template.yaml"
MAIN_TEMPLATE = TEMPLATE.parents[2] / "lma-main.yaml"
SECRET_ARN_PARAM = "VncOriginVerifyHeaderValueSecretArn"


@pytest.fixture(scope="module")
def vp_template() -> dict:
    return yaml.load(VP_TEMPLATE.read_text(), Loader=_CfnLoader)


@pytest.fixture(scope="module")
def main_template() -> dict:
    return yaml.load(MAIN_TEMPLATE.read_text(), Loader=_CfnLoader)


def test_the_ai_stack_publishes_the_secret_arn_not_its_value(template: dict) -> None:
    """A value in a task definition's environment is readable via ECS describe.

    So the participant is handed the ARN and reads the value itself.
    """
    output = template["Outputs"][SECRET_ARN_PARAM]["Value"]
    assert output["Fn::If"][0] == CONDITION
    assert output["Fn::If"][1] == {"Ref": "VncOriginVerifyHeaderValueSecret"}
    assert output["Fn::If"][2] == "", "must resolve to an empty string under MICROVM"


def test_the_main_template_wires_both_values_to_the_vp_stack(main_template: dict) -> None:
    params = main_template["Resources"]["VIRTUALPARTICIPANTSTACK"]["Properties"]["Parameters"]
    for name in (SECRET_ARN_PARAM, "VncOriginVerifyHeaderName"):
        assert params[name] == {"Fn::GetAtt": f"AISTACK.Outputs.{name}"}, name


def test_the_task_definition_carries_the_arn_and_never_the_value(vp_template: dict) -> None:
    containers = vp_template["Resources"]["TaskDefinition"]["Properties"]["ContainerDefinitions"]
    env = {e["Name"]: e["Value"] for c in containers for e in c.get("Environment", [])}
    assert env["VNC_ORIGIN_VERIFY_SECRET_ARN"] == {"Ref": SECRET_ARN_PARAM}
    assert env["VNC_ORIGIN_VERIFY_HEADER_NAME"] == {"Ref": "VncOriginVerifyHeaderName"}
    # The value itself must not be resolved anywhere in the task definition.
    assert "resolve:secretsmanager" not in json.dumps(containers, default=str)


def test_the_task_may_read_only_that_one_secret(vp_template: dict) -> None:
    policies = vp_template["Resources"]["TaskRole"]["Properties"]["Policies"]
    conditional = [p for p in policies if isinstance(p, dict) and "Fn::If" in p]
    matching = [
        p["Fn::If"][1]
        for p in conditional
        if isinstance(p["Fn::If"][1], dict)
        and p["Fn::If"][1].get("PolicyName") == "VncOriginVerifyPolicy"
    ]
    assert len(matching) == 1, "the origin-verify read must be its own conditional policy"
    statements = matching[0]["PolicyDocument"]["Statement"]
    reads = [s for s in statements if "secretsmanager" in json.dumps(s["Action"])]
    assert len(reads) == 1
    assert reads[0]["Action"] == "secretsmanager:GetSecretValue"
    assert reads[0]["Resource"] == {"Ref": SECRET_ARN_PARAM}


def test_the_participant_is_not_granted_the_read_when_there_is_no_load_balancer(
    vp_template: dict,
) -> None:
    """Under MICROVM the AI stack outputs an empty string for the ARN.

    An IAM statement naming an empty resource is rejected, so the policy has to be
    absent rather than present-and-empty.
    """
    condition = vp_template["Conditions"]["HasVncOriginVerifySecret"]
    assert condition == {"Fn::Not": [{"Fn::Equals": [{"Ref": SECRET_ARN_PARAM}, ""]}]}
    policies = vp_template["Resources"]["TaskRole"]["Properties"]["Policies"]
    for policy in policies:
        if isinstance(policy, dict) and "Fn::If" in policy:
            if policy["Fn::If"][0] == "HasVncOriginVerifySecret":
                assert policy["Fn::If"][2] == {"Ref": "AWS::NoValue"}


def test_the_runtime_rule_requires_the_same_two_conditions(template: dict) -> None:
    """Both rules that can reach a task must agree on the condition.

    The template's own rule is asserted above; this pins the run-time one, whose
    behaviour is covered in detail by backend/src/vnc-listener-rule.test.ts.
    """
    status_manager = (
        VP_TEMPLATE.parent / "backend" / "src" / "status-manager.ts"
    ).read_text()
    assert "buildListenerRuleConditions" in status_manager
    assert "'http-header'" in status_manager
    assert "VNC_ORIGIN_VERIFY_HEADER_NAME" in status_manager
    # The priority range must not move: the template's rule sits at 50000 so it is
    # evaluated after these.
    assert "1000 + Math.abs(hash % 49000)" in status_manager
    published = template["Outputs"]["VncOriginVerifyHeaderName"]["Value"]
    assert published not in status_manager, (
        "the header name must come from the environment, not be duplicated in the app"
    )


def test_the_origin_verify_value_is_generated_not_configured(template: dict) -> None:
    secret = template["Resources"][ORIGIN_VERIFY_SECRET]
    assert secret["Type"] == "AWS::SecretsManager::Secret"
    generate = secret["Properties"]["GenerateSecretString"]
    assert generate["PasswordLength"] >= 32
    assert secret["Properties"]["KmsKeyId"] == {"Ref": "CustomerManagedEncryptionKeyArn"}


def test_the_header_gated_rule_is_evaluated_after_the_per_vp_rules(template: dict) -> None:
    """Ordering is load-bearing, not cosmetic.

    The Virtual Participant creates one rule per participant at run time with
    priorities 1000-49999 (status-manager.ts generateRulePriority) to route
    /vnc/<vpId> to that participant's own target group. A lower priority number
    here would make this rule claim /vnc/* first and send every viewer to the
    shared target group instead, which routes to an arbitrary running task.
    """
    assert template["Resources"]["VNCALBListenerRule"]["Properties"]["Priority"] == 50000


def test_the_origin_restriction_follows_the_alb_condition(template: dict) -> None:
    """No ALB under MICROVM, so nothing here may be created unconditionally."""
    for name in (ORIGIN_VERIFY_SECRET, "VNCALBListenerRule"):
        assert template["Resources"][name].get("Condition") == CONDITION


def test_the_prefix_list_ingress_is_kept_as_a_coarse_filter(template: dict) -> None:
    """Useful, but it is the header that identifies the distribution.

    Asserted so that adding the header is not later mistaken for a reason to drop
    the network-level restriction.
    """
    ingress = template["Resources"]["ALBSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert any("SourcePrefixListId" in rule for rule in ingress)


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
