"""Unit tests for the Virtual Participant CloudFormation template.

These are static-analysis tests (no AWS): they parse template.yaml and assert
the invariants that are easy to break and expensive to discover at deploy time —
in particular that the scheduler state-machine definition still renders as valid
JSON for every VPLaunchType, and that adding MICROVM did not disturb the ECS
launch paths.

Run via `make test-vp-template` or pytest directly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

TEMPLATE = Path(__file__).resolve().parents[1] / "template.yaml"

# The state machine definition is a !Sub of a JSON string plus a substitution
# map. To validate it we render each launch type's substitutions and re-parse.
LAUNCH_TYPES = ["EC2", "FARGATE", "MICROVM"]


class _CfnLoader(yaml.SafeLoader):
    """YAML loader that tolerates CloudFormation short-form intrinsics."""


def _intrinsic(loader: yaml.Loader, tag_suffix: str, node: yaml.Node):  # noqa: ANN202
    name = f"Fn::{tag_suffix}" if tag_suffix != "Ref" else "Ref"
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node)}
    return {name: loader.construct_mapping(node)}


for _tag in (
    "Ref",
    "Sub",
    "GetAtt",
    "If",
    "Equals",
    "And",
    "Or",
    "Not",
    "Join",
    "Select",
    "Split",
    "FindInMap",
    "Base64",
    "Condition",
    "ImportValue",
    "GetAZs",
):
    _CfnLoader.add_constructor(
        f"!{_tag}", lambda loader, node, tag=_tag: _intrinsic(loader, tag, node)
    )
_CfnLoader.add_multi_constructor("!", _intrinsic)


@pytest.fixture(scope="module")
def template() -> dict:
    return yaml.load(TEMPLATE.read_text(), Loader=_CfnLoader)


@pytest.fixture(scope="module")
def raw() -> str:
    return TEMPLATE.read_text()


LAUNCHER_SRC = TEMPLATE.parent / "lambda_functions" / "microvm_launcher" / "index.py"


@pytest.fixture(scope="module")
def launcher() -> str:
    """The MicroVM launcher Lambda's source.

    It is a real deployment package (not inline `Code: ZipFile`) because it
    imports microvm_client.py, and inline code is one file capped at 4 KB.
    """
    return LAUNCHER_SRC.read_text()


def test_microvm_is_an_allowed_launch_type(template: dict) -> None:
    allowed = template["Parameters"]["VPLaunchType"]["AllowedValues"]
    assert set(allowed) == {"EC2", "FARGATE", "MICROVM"}
    # Default must stay EC2: MICROVM is opt-in until validated on live meetings,
    # and is unavailable in some regions LMA publishes to (e.g. ap-southeast-2).
    assert template["Parameters"]["VPLaunchType"]["Default"] == "EC2"


def test_launch_type_conditions_exist(template: dict) -> None:
    conditions = template["Conditions"]
    for name in (
        "UseEC2LaunchType",
        "UseFargateLaunchType",
        "UseMicrovmLaunchType",
        "UseECSLaunchType",
    ):
        assert name in conditions, f"missing condition {name}"


def test_use_ecs_launch_type_is_ec2_or_fargate(template: dict) -> None:
    # If this ever became "not MICROVM" it would silently include future launch
    # types in the ECS-only resources.
    cond = template["Conditions"]["UseECSLaunchType"]
    assert "Fn::Or" in cond
    branches = json.dumps(cond["Fn::Or"])
    assert "UseEC2LaunchType" in branches
    assert "UseFargateLaunchType" in branches


def test_microvm_resources_are_conditional(template: dict) -> None:
    """Nothing MicroVM-specific may be created for ECS deployments."""
    resources = template["Resources"]
    for name in (
        "VPMicrovmImage",
        "VPMicrovmBuildRole",
        "VPMicrovmExecutionRole",
        "VPMicrovmLauncherRole",
        "VPMicrovmLauncherFunction",
        "VPScheduleInvokeLauncherRole",
    ):
        assert name in resources, f"{name} missing from template"
        assert resources[name].get("Condition") == "UseMicrovmLaunchType", (
            f"{name} must be gated on UseMicrovmLaunchType"
        )


def test_ecs_service_not_created_under_microvm(template: dict) -> None:
    # ECS::Service only accepts LaunchType EC2|FARGATE, and its sole purpose is
    # the ALB target-group association that MicroVMs do not have.
    svc = template["Resources"]["VirtualParticipantService"]
    assert svc.get("Condition") == "UseECSLaunchType"


def test_ec2_only_resources_still_gated_on_ec2(template: dict) -> None:
    """Adding MICROVM must not widen the EC2-only infrastructure."""
    resources = template["Resources"]
    for name in (
        "VPCapacityProvider",
        "VPClusterCapacityProviderAssociation",
    ):
        assert resources[name].get("Condition") == "UseEC2LaunchType"


def test_microvm_image_hooks_configured_for_snapshot_correctness(
    template: dict,
) -> None:
    props = template["Resources"]["VPMicrovmImage"]["Properties"]
    hooks = props["Hooks"]
    # /ready gates the snapshot: without it Lambda may snapshot a half-booted
    # stack, so every launch would redo part of the boot.
    assert hooks["MicrovmImageHooks"]["Ready"] == "ENABLED"
    # /validate lets Lambda prefetch the snapshot pages actually touched.
    assert hooks["MicrovmImageHooks"]["Validate"] == "ENABLED"
    # /run delivers per-meeting config; without it the VP has no meeting to join.
    assert hooks["MicrovmHooks"]["Run"] == "ENABLED"
    assert hooks["MicrovmHooks"]["Terminate"] == "ENABLED"
    # Hook port must match HOOK_PORT in the container (src/microvm-supervisor.ts).
    assert hooks["Port"] == 9000


def test_microvm_image_requests_chromium_capabilities(template: dict) -> None:
    props = template["Resources"]["VPMicrovmImage"]["Properties"]
    # Chromium's sandbox needs more than the default restricted capability set.
    assert props["AdditionalOsCapabilities"] == ["ALL"]
    # Observed peak ~1650 MB for Chromium + avatar + voice; plus X and audio.
    assert props["Resources"][0]["MinimumMemoryInMiB"] >= 4096


def test_microvm_image_suppresses_only_the_missing_schema_rule(template: dict) -> None:
    # cfn-lint has no schema for AWS::Lambda::MicrovmImage yet, so E3006 must be
    # ignored — but narrowly, at this resource, not globally.
    meta = template["Resources"]["VPMicrovmImage"]["Metadata"]
    assert meta["cfn-lint"]["config"]["ignore_checks"] == ["E3006"]


def _state_machine_definition(template: dict) -> tuple[str, dict]:
    sub = template["Resources"]["LMAVirtualParticipantSchedulerStateMachine"][
        "Properties"
    ]["DefinitionString"]["Fn::Sub"]
    assert isinstance(sub, list) and len(sub) == 2, "expected !Sub [template, vars]"
    return sub[0], sub[1]


def _render_definition(
    template: dict, *, is_microvm: bool, is_ec2: bool = False
) -> dict:
    """Resolve the !Sub substitutions the way CloudFormation would, then parse."""
    body, variables = _state_machine_definition(template)

    dispatch_frag = variables["DispatchStates"]["Fn::If"][1 if is_microvm else 2]
    if isinstance(dispatch_frag, dict):
        dispatch_frag = dispatch_frag["Fn::Sub"]

    rendered = body.replace("${DispatchStates}", dispatch_frag)
    for name in re.findall(r"\$\{([A-Za-z0-9_.]+)\}", rendered):
        if name in ("DispatchStateName", "ScheduledMeetingTarget"):
            continue
        rendered = rendered.replace("${" + name + "}", "PLACEHOLDER")

    dispatch = "RunMicrovm" if is_microvm else "RunTask"
    rendered = rendered.replace("${DispatchStateName}", dispatch)
    rendered = rendered.replace(
        "${ScheduledMeetingTarget}",
        _render_target(variables["ScheduledMeetingTarget"], is_microvm),
    )
    return json.loads(rendered)


def _rendered_states(template: dict, *, is_microvm: bool) -> dict:
    return _render_definition(template, is_microvm=is_microvm)["States"]


@pytest.mark.parametrize("launch_type", LAUNCH_TYPES)
def test_state_machine_renders_valid_json(template: dict, launch_type: str) -> None:
    """The definition must parse as JSON for every launch type.

    This is the test that would have caught an unbalanced or mis-quoted
    substitution fragment — a class of bug that otherwise only shows up as a
    CloudFormation failure ~30 minutes into a deploy.
    """
    is_microvm = launch_type == "MICROVM"
    parsed = _render_definition(
        template, is_microvm=is_microvm, is_ec2=(launch_type == "EC2")
    )
    dispatch = "RunMicrovm" if is_microvm else "RunTask"
    assert "States" in parsed
    assert parsed["States"][dispatch], f"{dispatch} state must exist"
    # MergeZoomSecret is the hand-off point; it must route to this launch type's
    # dispatch state, or the meeting silently never starts.
    assert parsed["States"]["MergeZoomSecret"]["Next"] == dispatch


def _render_target(fragment: object, is_microvm: bool) -> str:
    """Resolve the ScheduledMeetingTarget !If down to its JSON fragment."""
    assert isinstance(fragment, dict) and "Fn::If" in fragment
    _cond, if_true, if_false = fragment["Fn::If"]
    chosen = if_true if is_microvm else if_false
    text = chosen["Fn::Sub"] if isinstance(chosen, dict) else chosen
    return re.sub(r"\$\{[A-Za-z0-9_.:]+\}", "PLACEHOLDER", text)


@pytest.mark.parametrize("launch_type", LAUNCH_TYPES)
def test_only_the_active_launch_types_states_are_emitted(
    template: dict, launch_type: str
) -> None:
    """Step Functions rejects unreachable states.

    An earlier attempt kept BOTH dispatch blocks in one static definition and
    switched only the entry point. CloudFormation accepted it; Step Functions did
    not:
      MISSING_TRANSITION_TARGET: State "RunTask" is not reachable.
    So the states are substituted per launch type, and the other set must be
    absent entirely.
    """
    is_microvm = launch_type == "MICROVM"
    states = _rendered_states(template, is_microvm=is_microvm)

    microvm_states = {"RunMicrovm", "MicrovmLaunched", "MarkMicrovmSoftFailure"}
    ecs_states = {"RunTask", "CheckRunTaskFailures", "MarkRunTaskSoftFailure"}
    present, absent = (
        (microvm_states, ecs_states) if is_microvm else (ecs_states, microvm_states)
    )
    for name in present:
        assert name in states, f"{launch_type} must include {name}"
    for name in absent:
        assert name not in states, (
            f"{launch_type} must NOT include {name} — it would be unreachable"
        )


@pytest.mark.parametrize("launch_type", LAUNCH_TYPES)
def test_no_unreachable_or_dangling_states(template: dict, launch_type: str) -> None:
    """Every state reachable from StartAt, every target existing.

    This is exactly the invariant Step Functions enforces at deploy time, so
    checking it here converts a failed stack update into a local test failure.
    """
    is_microvm = launch_type == "MICROVM"
    definition = _render_definition(
        template, is_microvm=is_microvm, is_ec2=(launch_type == "EC2")
    )
    states = definition["States"]

    targets = {definition["StartAt"]}

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("Next", "Default") and isinstance(value, str):
                    targets.add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(definition)

    unreachable = sorted(s for s in states if s not in targets)
    dangling = sorted(t for t in targets if t not in states)
    assert not unreachable, f"{launch_type}: unreachable states {unreachable}"
    assert not dangling, f"{launch_type}: transitions to missing states {dangling}"


def test_microvm_soft_failures_route_to_the_existing_failure_state(
    template: dict,
) -> None:
    # Soft failures (quota, image not ready) do not throw, so — exactly like
    # CheckRunTaskFailures for ECS — they must be inspected and routed, or the
    # UI would show a VP that silently never starts.
    # Parse the states rather than string-splitting: the Pass state contains a
    # nested "Parameters" object, so splitting on the first "}," stops early.
    states = _rendered_states(template, is_microvm=True)

    choice = states["MicrovmLaunched"]
    assert choice["Type"] == "Choice"
    assert choice["Choices"][0]["BooleanEquals"] is False
    assert choice["Choices"][0]["Next"] == "MarkMicrovmSoftFailure"

    soft = states["MarkMicrovmSoftFailure"]
    assert soft["Next"] == "MarkVPFailed"
    # Reuses the same failure state as the ECS path, so the UI reporting and
    # DynamoDB status update are identical for both launch types.
    assert "MarkVPFailed" in states


def test_scheduled_ecs_target_is_unchanged(template: dict) -> None:
    """The ECS scheduled-meeting target must be preserved exactly.

    Scheduled meetings are a separate dispatch path from "start now"; a
    regression here would only surface when a booked meeting failed to start.
    """
    _body, variables = _state_machine_definition(template)
    ecs_target = _render_target(variables["ScheduledMeetingTarget"], is_microvm=False)
    parsed = json.loads("{" + ecs_target + "}")["Target"]

    assert parsed["EcsParameters"]["TaskCount"] == 1
    assert (
        parsed["EcsParameters"]["NetworkConfiguration"]["AwsvpcConfiguration"][
            "AssignPublicIp"
        ]
        == "DISABLED"
    )
    env = parsed["Input"]["containerOverrides"][0]["environment"]
    # Note the mixed key casing: the LMA_USER entry uses Name/Value while the
    # others use name/value. Preserved verbatim from the original template.
    assert env[0] == {"name": "MEETING_PLATFORM", "value.$": "$.data.meetingPlatform"}
    assert env[1] == {"name": "MEETING_ID", "value.$": "$.data.meetingID"}
    assert env[2] == {"name": "MEETING_PASSWORD", "value.$": "$.data.meetingPassword"}
    assert env[3] == {"name": "MEETING_NAME", "value.$": "$.data.meetingName"}
    assert env[4] == {"Name": "LMA_USER", "Value.$": "$.data.userName"}
    assert len(env) == 5


def test_scheduled_microvm_target_invokes_the_launcher(template: dict) -> None:
    # EventBridge Scheduler has a native ECS runTask target but none for
    # lambda-microvms, so scheduled MICROVM meetings must go via the Lambda.
    _body, variables = _state_machine_definition(template)
    target = _render_target(variables["ScheduledMeetingTarget"], is_microvm=True)
    parsed = json.loads("{" + target + "}")["Target"]
    assert "EcsParameters" not in parsed
    data = parsed["Input"]["data"]
    for key in ("meetingPlatform", "meetingId", "meetingName", "virtualParticipantId"):
        assert f"{key}.$" in data, f"scheduled MICROVM target must pass {key}"


def test_launcher_field_map_matches_container_per_meeting_keys(launcher: str) -> None:
    """The launcher's FIELD_MAP must cover the container's PER_MEETING_KEYS.

    These are two separate files (inline Python here, TypeScript in the
    container). A value present in one but not the other is silently dropped,
    so keep them in step.
    """
    ts = (TEMPLATE.parent / "backend" / "src" / "launch-mode.ts").read_text()
    # Parse ONLY the PER_MEETING_KEYS array — the file also contains
    # BLOCKED_ENV_KEYS, which the same loose pattern would otherwise pick up.
    ts_block = ts.split("PER_MEETING_KEYS = [", 1)[1].split("]", 1)[0]
    ts_keys = set(re.findall(r"'([A-Z_]+)'", ts_block))
    py_block = launcher.split("FIELD_MAP = {", 1)[1].split("}", 1)[0]
    py_keys = set(re.findall(r'"([A-Z_]+)":', py_block))

    assert ts_keys, "could not parse PER_MEETING_KEYS from launch-mode.ts"
    assert py_keys == ts_keys, (
        "launcher FIELD_MAP and container PER_MEETING_KEYS disagree: "
        f"only in template={sorted(py_keys - ts_keys)}, "
        f"only in container={sorted(ts_keys - py_keys)}"
    )


def test_launcher_payload_limit_matches_container(launcher: str) -> None:
    # Both sides must refuse the same size, or one truncates auth tokens.
    ts = (TEMPLATE.parent / "backend" / "src" / "launch-mode.ts").read_text()
    ts_limit = int(re.search(r"RUN_HOOK_PAYLOAD_MAX_BYTES = (\d+)", ts).group(1))
    py_limit = int(re.search(r"RUN_HOOK_PAYLOAD_MAX_BYTES = (\d+)", launcher).group(1))
    # 4096, not the 16384 the developer guide states: the service model says
    # {"max": 4096} and a live launch failed at that boundary. Both sides must
    # agree, or one silently builds a payload the service rejects.
    assert ts_limit == py_limit == 4096


def test_launcher_disables_auto_suspend(launcher: str) -> None:
    """A suspended VP stops capturing audio and would miss meeting content."""
    block = launcher.split("idlePolicy={", 1)[1].split("}", 1)[0]
    assert '"autoResumeEnabled": False' in block
    assert "MAX_DURATION_SECONDS" in launcher
    # MicroVMs cap at 8 hours (28800s); the launcher must not request more.
    assert re.search(r"MAX_DURATION_SECONDS = 28800", launcher)


def test_execution_role_has_no_alb_permissions(template: dict) -> None:
    """MicroVMs have no ALB; granting ELB rights would be dead privilege."""
    role = template["Resources"]["VPMicrovmExecutionRole"]
    text = json.dumps(role)
    assert "elasticloadbalancing" not in text.lower()


def test_launcher_can_pass_only_the_execution_role(template: dict) -> None:
    """iam:PassRole must be scoped by Resource, and carry NO PassedToService.

    RunMicrovm does not populate the iam:PassedToService context key, so a
    condition on it evaluates to a deny — verified with
    `aws iam simulate-principal-policy` (implicitDeny when the key is absent)
    after a live launch failed with "no identity-based policy allows the
    iam:PassRole action". Least privilege comes from naming the single role.
    """
    role = template["Resources"]["VPMicrovmLauncherRole"]
    statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    pass_role = [s for s in statements if s.get("Action") == "iam:PassRole"]
    assert len(pass_role) == 1, "expected exactly one iam:PassRole statement"
    # Must target exactly the MicroVM execution role, never "*".
    resource = json.dumps(pass_role[0]["Resource"])
    assert "VPMicrovmExecutionRole" in resource
    assert pass_role[0]["Resource"] != "*"
    assert "Condition" not in pass_role[0], (
        "a PassedToService condition here denies the call; see the docstring"
    )


def test_microvm_image_uses_the_root_dockerfile_artifact(template: dict) -> None:
    """CreateMicrovmImage needs the Dockerfile at the ZIP ROOT.

    The main VP source zip is rooted at the stack directory, so its Dockerfile
    lands at `backend/Dockerfile`, and codeArtifact accepts only a bare URI —
    there is no way to point it at a subdirectory. The publish step therefore
    produces a second artifact rooted at backend/, and the image must reference
    THAT one.
    """
    props = template["Resources"]["VPMicrovmImage"]["Properties"]
    uri = json.dumps(props["CodeArtifact"]["Uri"])
    assert "MicrovmSourceCodeLocation" in uri
    assert "SourceCodeLocation}" not in uri.replace("MicrovmSourceCodeLocation}", "")


def test_microvm_source_location_parameter_exists(template: dict) -> None:
    param = template["Parameters"]["MicrovmSourceCodeLocation"]
    # Defaults to empty so ECS deployments need not supply it.
    assert param.get("Default") == ""


def test_build_role_reads_the_microvm_artifact(template: dict) -> None:
    role = template["Resources"]["VPMicrovmBuildRole"]
    text = json.dumps(role)
    assert "MicrovmSourceCodeLocation" in text


def test_codebuild_skips_container_pipeline_under_microvm(raw: str) -> None:
    """Under MICROVM there is no ECR image, SOCI index, or ECS service.

    All three buildspec phases must short-circuit, and VP_LAUNCH_TYPE must
    actually reach the build environment or the guards never fire.
    """
    assert raw.count('if [ "$VP_LAUNCH_TYPE" = "MICROVM" ]') >= 3, (
        "expected guards in pre_build, build and post_build"
    )
    assert "- Name: VP_LAUNCH_TYPE" in raw, (
        "VP_LAUNCH_TYPE must be passed to the CodeBuild environment"
    )


def test_microvm_image_sets_launch_type_env(template: dict) -> None:
    """The container dispatches to the supervisor on this variable."""
    env = template["Resources"]["VPMicrovmImage"]["Properties"]["EnvironmentVariables"]
    assert {"Key": "VP_LAUNCH_TYPE", "Value": "MICROVM"} in env


# ---------------------------------------------------------------------------
# Schema validation for AWS::Lambda::MicrovmImage
#
# cfn-lint ships no schema for this resource type yet (service launched
# 2026-06), so a malformed property is otherwise only caught by CloudFormation
# — several minutes into a deploy, followed by a full stack rollback. That
# happened once during development: five property errors at once, including
# EnvironmentVariables being a map where the resource wants an array.
#
# schemas/AWS-Lambda-MicrovmImage.json is the real provider schema, captured via
#   aws cloudformation describe-type --type RESOURCE \
#     --type-name AWS::Lambda::MicrovmImage --query Schema
# Refresh it the same way if the resource gains properties.
# ---------------------------------------------------------------------------

SCHEMA_PATH = (
    Path(__file__).resolve().parent / "schemas" / "AWS-Lambda-MicrovmImage.json"
)


_PLACEHOLDERS = {
    "BuildRoleArn": "arn:aws:iam::123456789012:role/placeholder",
    "BaseImageArn": "arn:aws:lambda:us-west-2:aws:microvm-image:al2023-1",
    "EgressNetworkConnectors": (
        "arn:aws:lambda:us-west-2:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
    ),
    "Uri": "s3://bucket/key.zip",
    "LogGroup": "/aws/lambda-microvms/placeholder",
}


def _resolve_intrinsics(value, prop_name=None):
    """Replace CFN intrinsics with representative strings so the shape can be
    validated against the JSON schema (which knows nothing about !Sub/!Ref)."""
    if isinstance(value, dict):
        if len(value) == 1:
            key = next(iter(value))
            if key in (
                "Ref",
                "Fn::Sub",
                "Fn::GetAtt",
                "Fn::Join",
                "Fn::Select",
                "Fn::ImportValue",
            ):
                # Several properties carry ARN/URI regex patterns, so the
                # stand-in must be shaped like the real thing. The caller
                # passes the property name to pick a plausible value.
                return _PLACEHOLDERS.get(prop_name, "resolved-value")
            if key == "Fn::If":
                # Validate the "true" branch; both branches are shape-identical
                # for the properties we care about.
                return _resolve_intrinsics(value[key][1], prop_name)
        return {k: _resolve_intrinsics(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_intrinsics(v, prop_name) for v in value]
    return value


def test_microvm_image_matches_the_provider_schema(template: dict) -> None:
    """Validate VPMicrovmImage against the real CloudFormation schema.

    This is the test that turns a ~7-minute deploy + rollback into a sub-second
    local failure.
    """
    # Hard import (not importorskip): jsonschema is a declared dev dependency
    # in lib/lma_sdk. Skipping would silently disable the one check that catches
    # this failure class before a deploy.
    import jsonschema

    schema = json.loads(SCHEMA_PATH.read_text())
    props = _resolve_intrinsics(template["Resources"]["VPMicrovmImage"]["Properties"])

    # Read-only attributes are outputs, never inputs.
    validatable = {
        k: v
        for k, v in schema.items()
        if k in ("properties", "definitions", "additionalProperties", "required")
    }
    for read_only in schema.get("readOnlyProperties", []):
        name = read_only.split("/")[-1]
        validatable.get("required", []) and None
        if name in props:
            del props[name]

    jsonschema.validate(instance=props, schema=validatable)


def test_microvm_image_sets_every_required_property(template: dict) -> None:
    """The CFN resource marks ALL properties required, unlike the API.

    Relying on service-side defaults (as the API allows) fails validation, so
    each one must be set explicitly.
    """
    schema = json.loads(SCHEMA_PATH.read_text())
    props = template["Resources"]["VPMicrovmImage"]["Properties"]
    read_only = {p.split("/")[-1] for p in schema.get("readOnlyProperties", [])}
    missing = [r for r in schema["required"] if r not in props and r not in read_only]
    assert not missing, f"VPMicrovmImage is missing required properties: {missing}"


def test_microvm_image_is_arm64(template: dict) -> None:
    # ARM_64 is the only value the schema permits, and the reason the ARM64
    # CloakBrowser Chromium question was the gating one for this whole change.
    props = template["Resources"]["VPMicrovmImage"]["Properties"]
    assert props["CpuConfigurations"] == [{"Architecture": "ARM_64"}]


def test_microvm_environment_variables_are_key_value_pairs(template: dict) -> None:
    # A plain map here is valid-looking YAML but fails CFN validation with
    # "expected type: JSONArray, found: JSONObject".
    env = template["Resources"]["VPMicrovmImage"]["Properties"]["EnvironmentVariables"]
    assert isinstance(env, list)
    for item in env:
        assert set(item) == {"Key", "Value"}, f"bad env var entry: {item}"
    # The container dispatches to the supervisor on this variable.
    assert any(e["Key"] == "VP_LAUNCH_TYPE" and e["Value"] == "MICROVM" for e in env)


def test_base_image_version_is_a_bare_major_version(template: dict) -> None:
    """BaseImageVersion must be a single major version number.

    "1.0" passes CloudFormation's schema validation (it is just a string) but is
    rejected by the service at create time:
      Invalid baseMicroVMImageVersion: 1.0. Expected a single major version
      number (e.g., 1).
    Valid values come from `list-managed-microvm-image-versions`.
    """
    version = template["Resources"]["VPMicrovmImage"]["Properties"]["BaseImageVersion"]
    assert re.fullmatch(r"\d+", str(version)), (
        f"BaseImageVersion must be a bare major version, got {version!r}"
    )


def test_getatt_references_to_microvm_image_use_real_attributes(raw: str) -> None:
    """Every !GetAtt on VPMicrovmImage must name a real read-only attribute.

    CloudFormation rejects an unknown one at CREATE time ("Requested attribute
    Arn does not exist in schema for AWS::Lambda::MicrovmImage") — after the
    image has already been built, so the failed deploy is an expensive one. The
    attribute here is ImageArn, not the conventional Arn.
    """
    schema = json.loads(SCHEMA_PATH.read_text())
    valid = {p.split("/")[-1] for p in schema.get("readOnlyProperties", [])}
    used = set(re.findall(r"!GetAtt\s+VPMicrovmImage\.([A-Za-z0-9]+)", raw))
    assert used, "expected at least one !GetAtt on VPMicrovmImage"
    invalid = used - valid
    assert not invalid, (
        f"invalid VPMicrovmImage attributes {sorted(invalid)}; valid: {sorted(valid)}"
    )


def _inline_lambda_source(raw: str, marker: str) -> str:
    """Extract and de-indent an inline `Code: ZipFile: |` Python body."""
    block = raw.split(marker, 1)[1].split("ZipFile: |\n", 1)[1]
    lines: list[str] = []
    for line in block.split("\n"):
        if line.strip() == "":
            lines.append("")
            continue
        # The block ends at the first line indented less than its body.
        if not line.startswith("          "):
            break
        lines.append(line)
    indent = min(
        (len(x) - len(x.lstrip()) for x in lines if x.strip()),
        default=0,
    )
    return "\n".join(x[indent:] if len(x) >= indent else x for x in lines)


def test_launcher_python_is_syntactically_valid(launcher: str) -> None:
    """Compile the launcher's inline source.

    An inline ZipFile body is never checked by cfn-lint or any linter, so a
    syntax error would deploy successfully and only fail when the first meeting
    tries to start.
    """
    import ast

    ast.parse(launcher)  # raises SyntaxError on malformed code
    assert "def lambda_handler" in launcher


def test_launcher_attaches_an_ingress_connector(launcher: str) -> None:
    """Without ingress the MicroVM endpoint forwards nothing.

    RunMicrovm succeeds and returns an endpoint either way, so omitting this
    surfaces only as a VNC viewer that cannot connect.
    """
    assert "ingressNetworkConnectors=" in launcher
    assert "ALL_INGRESS" in launcher
    assert "INTERNET_EGRESS" in launcher


def test_both_microvm_roles_can_decrypt_the_dynamodb_key(template: dict) -> None:
    """Registry access needs KMS as well as DynamoDB.

    Both tables use the customer-managed key. The launcher stages the VP's
    configuration there and the container reads it back (the runHookPayload limit
    is 4096, too small for the real config), so a missing grant means the VP
    starts with no configuration at all.
    """
    for role_name in ("VPMicrovmExecutionRole", "VPMicrovmLauncherRole"):
        text = json.dumps(template["Resources"][role_name])
        assert "kms:Decrypt" in text, f"{role_name} needs kms:Decrypt"
        assert "CustomerManagedEncryptionKeyArn" in text, (
            f"{role_name} must scope KMS to the stack's key"
        )


def test_run_hook_payload_carries_only_a_pointer(launcher: str) -> None:
    """The service enforces 4096 bytes, not the documented 16 KB.

    Three Cognito JWTs alone are ~3.6 KB, so the launcher must stage the full
    config in the registry and send only the vpId. Sending it inline fails with
    "Member must have length less than or equal to 4096".
    """
    assert "_write_config_to_registry" in launcher
    # The payload built for RunMicrovm must contain nothing but the id.
    payload_line = [
        line
        for line in launcher.splitlines()
        if "run_hook_payload = json.dumps" in line
    ]
    assert payload_line, "could not find the runHookPayload construction"
    assert "VIRTUAL_PARTICIPANT_ID" in payload_line[0]
    assert "payload_config" not in payload_line[0], (
        "the full config must not be sent inline; it exceeds the 4096-byte limit"
    )
