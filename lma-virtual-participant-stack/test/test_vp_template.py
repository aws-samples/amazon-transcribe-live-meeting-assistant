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

    rendered = body
    for name in re.findall(r"\$\{([A-Za-z0-9_.]+)\}", body):
        if name in ("RunTaskDispatch", "DispatchStateName", "ScheduledMeetingTarget"):
            continue
        rendered = rendered.replace("${" + name + "}", "PLACEHOLDER")

    dispatch = "RunMicrovm" if is_microvm else "RunTask"
    run_task_dispatch = (
        '"CapacityProviderStrategy": [{ "CapacityProvider": "CP", "Weight": 1 }]'
        if is_ec2
        else '"LaunchType": "FARGATE"'
    )
    rendered = rendered.replace("${DispatchStateName}", dispatch)
    rendered = rendered.replace("${RunTaskDispatch}", run_task_dispatch)
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


def test_both_dispatch_states_exist_in_every_deployment(template: dict) -> None:
    """RunTask and RunMicrovm both live in the static definition.

    Only the entry point differs (DispatchStateName), which keeps the change a
    one-line switch rather than two variants of the whole state machine.
    """
    body, _ = _state_machine_definition(template)
    assert '"RunTask": {' in body
    assert '"RunMicrovm": {' in body
    assert '"MicrovmLaunched": {' in body
    assert '"MarkMicrovmSoftFailure": {' in body


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


def test_launcher_field_map_matches_container_per_meeting_keys(raw: str) -> None:
    """The launcher's FIELD_MAP must cover the container's PER_MEETING_KEYS.

    These are two separate files (inline Python here, TypeScript in the
    container). A value present in one but not the other is silently dropped,
    so keep them in step.
    """
    ts = (TEMPLATE.parent / "backend" / "src" / "launch-mode.ts").read_text()
    ts_keys = set(re.findall(r"^\s+'([A-Z_]+)',$", ts, re.M))
    py_block = raw.split("FIELD_MAP = {", 1)[1].split("}", 1)[0]
    py_keys = set(re.findall(r'"([A-Z_]+)":', py_block))

    assert ts_keys, "could not parse PER_MEETING_KEYS from launch-mode.ts"
    assert py_keys == ts_keys, (
        "launcher FIELD_MAP and container PER_MEETING_KEYS disagree: "
        f"only in template={sorted(py_keys - ts_keys)}, "
        f"only in container={sorted(ts_keys - py_keys)}"
    )


def test_launcher_payload_limit_matches_container(raw: str) -> None:
    # Both sides must refuse the same size, or one truncates auth tokens.
    ts = (TEMPLATE.parent / "backend" / "src" / "launch-mode.ts").read_text()
    ts_limit = int(re.search(r"RUN_HOOK_PAYLOAD_MAX_BYTES = (\d+)", ts).group(1))
    py_limit = int(re.search(r"RUN_HOOK_PAYLOAD_MAX_BYTES = (\d+)", raw).group(1))
    assert ts_limit == py_limit == 16384


def test_launcher_disables_auto_suspend(raw: str) -> None:
    """A suspended VP stops capturing audio and would miss meeting content."""
    block = raw.split("idlePolicy={", 1)[1].split("}", 1)[0]
    assert '"autoResumeEnabled": False' in block
    assert "MAX_DURATION_SECONDS" in raw
    # MicroVMs cap at 8 hours (28800s); the launcher must not request more.
    assert re.search(r"MAX_DURATION_SECONDS = 28800", raw)


def test_execution_role_has_no_alb_permissions(template: dict) -> None:
    """MicroVMs have no ALB; granting ELB rights would be dead privilege."""
    role = template["Resources"]["VPMicrovmExecutionRole"]
    text = json.dumps(role)
    assert "elasticloadbalancing" not in text.lower()


def test_launcher_can_pass_only_the_execution_role(template: dict) -> None:
    role = template["Resources"]["VPMicrovmLauncherRole"]
    statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    pass_role = [s for s in statements if s.get("Action") == "iam:PassRole"]
    assert len(pass_role) == 1, "expected exactly one iam:PassRole statement"
    assert pass_role[0]["Condition"]["StringEquals"]["iam:PassedToService"] == (
        "lambda.amazonaws.com"
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
    props = template["Resources"]["VPMicrovmImage"]["Properties"]
    assert props["EnvironmentVariables"]["VP_LAUNCH_TYPE"] == "MICROVM"
