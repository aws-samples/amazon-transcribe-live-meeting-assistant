# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
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


# Regions where AWS Lambda MicroVMs are available. Every region LMA publishes to
# must be in this set, because MICROVM is now the default launch type AND
# cloudformation:ValidateTemplate (which publish calls per region) rejects the VP
# template outright where AWS::Lambda::MicrovmImage is unknown -- regardless of
# the parameter value. Verified in ap-southeast-2.
MICROVM_REGIONS = {
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "ap-northeast-1",
    "eu-west-1",
}


def test_microvm_is_an_allowed_launch_type(template: dict) -> None:
    allowed = template["Parameters"]["VPLaunchType"]["AllowedValues"]
    assert set(allowed) == {"EC2", "FARGATE", "MICROVM"}
    # MICROVM is the default: it is faster to start, has no warm-instance cost,
    # and needs no ALB / autoscaling / host patching. Published regions are
    # restricted to those that support it (see test_published_regions_*).
    assert template["Parameters"]["VPLaunchType"]["Default"] == "MICROVM"


def test_launch_type_default_matches_across_all_three_templates() -> None:
    """lma-main, the VP stack and the AI stack must agree on the default.

    lma-main always passes VPLaunchType explicitly, so a mismatch is invisible in
    a normal deploy — but the AI stack's default decides whether the VNC ALB is
    created, so a standalone deploy of it would either pay for an idle load
    balancer or omit one the ECS paths need.
    """
    repo = TEMPLATE.parents[1]
    paths = {
        "lma-main.yaml": repo / "lma-main.yaml",
        "vp-stack": TEMPLATE,
        "ai-stack": repo / "lma-ai-stack" / "deployment" / "lma-ai-stack.yaml",
    }
    defaults = {}
    for label, path in paths.items():
        doc = yaml.load(path.read_text(), Loader=_CfnLoader)
        defaults[label] = doc["Parameters"]["VPLaunchType"]["Default"]
    assert len(set(defaults.values())) == 1, f"VPLaunchType defaults differ: {defaults}"


def test_published_regions_all_support_microvms() -> None:
    """aws-release.sh must not publish to a region without MicroVM support.

    publish calls cloudformation:ValidateTemplate on the VP template in the target
    region, and that call fails with "Unrecognized resource types:
    [AWS::Lambda::MicrovmImage]" where the type is unknown -- so publishing to
    such a region aborts the release, whatever VPLaunchType says.
    """
    release = (TEMPLATE.parents[1] / "aws-release.sh").read_text()
    published = set(re.findall(r"^\./publish\.sh\s+\S+\s+\S+\s+(\S+)", release, re.M))
    assert published, "no publish.sh invocations found in aws-release.sh"
    unsupported = published - MICROVM_REGIONS
    assert not unsupported, (
        f"aws-release.sh publishes to region(s) without Lambda MicroVM support: "
        f"{sorted(unsupported)}. Either remove them or stop defaulting to MICROVM."
    )


def test_cli_template_urls_match_published_regions() -> None:
    """`lma deploy --region X` resolves a public template from this map.

    A region in aws-release.sh but missing here cannot be deployed with the CLI
    without an explicit --template-url; a region here but not published points at
    an S3 object that does not exist.
    """
    repo = TEMPLATE.parents[1]
    release = (repo / "aws-release.sh").read_text()
    published = set(re.findall(r"^\./publish\.sh\s+\S+\s+\S+\s+(\S+)", release, re.M))
    cli = (
        repo / "lib" / "lma_cli_pkg" / "lma_cli" / "commands" / "stack.py"
    ).read_text()
    block = cli[cli.index("TEMPLATE_URLS = {") : cli.index("}", cli.index("TEMPLATE_URLS = {"))]
    mapped = set(re.findall(r'^\s*"([a-z0-9-]+)":', block, re.M))
    assert mapped == published, (
        f"CLI TEMPLATE_URLS regions {sorted(mapped)} do not match published "
        f"regions {sorted(published)}"
    )


def test_readme_launch_buttons_match_published_regions() -> None:
    """A Launch Stack button for an unpublished region 404s on the template URL."""
    repo = TEMPLATE.parents[1]
    release = (repo / "aws-release.sh").read_text()
    published = set(re.findall(r"^\./publish\.sh\s+\S+\s+\S+\s+(\S+)", release, re.M))
    readme = (repo / "README.md").read_text()
    # Only the Quick Deploy table's console links, not arbitrary doc links.
    button_regions = set(
        re.findall(r"https://([a-z0-9-]+)\.console\.aws\.amazon\.com/cloudformation", readme)
    )
    assert button_regions == published, (
        f"README Launch Stack regions {sorted(button_regions)} do not match "
        f"published regions {sorted(published)}"
    )


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
    # Observed peak ~1650 MB for Chromium + avatar + voice, plus X and audio.
    # An 8 GB baseline was tried for faster browser launch and changed nothing
    # (135s at 4 GB vs 149s at 8 GB), so the extra cost was not justified.
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


def test_microvm_reuses_the_ecs_task_role(template: dict) -> None:
    """The MicroVM execution role IS TaskRole, deliberately.

    A separate hand-written role drifted from TaskRole and the VP failed at run
    time on missing appsync:EventConnect, the VP profiles bucket (S3 403 on
    profile restore), Bedrock, Secrets Manager and more. Sharing the role means
    the MicroVM path inherits every permission the VP application needs, and
    there is only one place to update.
    """
    assert "VPMicrovmExecutionRole" not in template["Resources"], (
        "the separate MicroVM execution role should be gone; TaskRole is shared"
    )
    trust = template["Resources"]["TaskRole"]["Properties"]["AssumeRolePolicyDocument"]
    services = trust["Statement"][0]["Principal"]["Service"]
    # Compare as a SET, not with `in`. `services` is a YAML-parsed list of service
    # principals, so `"x" in services` is exact list membership -- but CodeQL reads
    # it as substring-sanitization of a URL (py/incomplete-url-substring-sanitization,
    # 2 high alerts). Set equality is both stricter and unambiguous.
    assert isinstance(services, list), "Principal.Service must be a list of principals"
    assert set(services) == {"ecs-tasks.amazonaws.com", "lambda.amazonaws.com"}, (
        "TaskRole must be assumable by ECS tasks AND by Lambda (for MicroVMs)"
    )


def test_launcher_can_pass_only_the_task_role(template: dict) -> None:
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
    # Must target exactly the role the MicroVM runs as, never "*".
    resource = json.dumps(pass_role[0]["Resource"])
    assert "TaskRole" in resource
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


CONNECTOR_SCHEMA_PATH = (
    Path(__file__).resolve().parent / "schemas" / "AWS-Lambda-NetworkConnector.json"
)


_PLACEHOLDERS = {
    "BuildRoleArn": "arn:aws:iam::123456789012:role/placeholder",
    "BaseImageArn": "arn:aws:lambda:us-west-2:aws:microvm-image:al2023-1",
    "EgressNetworkConnectors": (
        "arn:aws:lambda:us-west-2:aws:network-connector:aws-network-connector:INTERNET_EGRESS"
    ),
    "OperatorRole": "arn:aws:iam::123456789012:role/placeholder",
    # subnet-/sg- prefixed: the connector schema pattern-checks these.
    "SubnetIds": "subnet-0123456789abcdef0",
    "SecurityGroupIds": "sg-0123456789abcdef0",
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


def test_egress_connector_matches_the_provider_schema(template: dict) -> None:
    """Validate VPMicrovmEgressConnector against the real CloudFormation schema.

    Same rationale as the MicrovmImage schema test: this resource type is new
    enough that cfn-lint has no schema for it, so without this the first
    validation is a live deploy. Note the schema alone is NOT sufficient -- it
    marks OperatorRole optional while the service requires it for VPC_EGRESS
    (see test_egress_connector_sets_an_operator_role).
    """
    import jsonschema

    schema = json.loads(CONNECTOR_SCHEMA_PATH.read_text())
    props = _resolve_intrinsics(
        template["Resources"]["VPMicrovmEgressConnector"]["Properties"]
    )
    validatable = {
        k: v
        for k, v in schema.items()
        if k in ("properties", "definitions", "additionalProperties", "required")
    }
    for read_only in schema.get("readOnlyProperties", []):
        props.pop(read_only.split("/")[-1], None)

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
    assert "egressNetworkConnectors=" in launcher


def test_launcher_reads_the_egress_connector_arn_from_the_environment(
    launcher: str,
) -> None:
    """The VPC egress connector ARN cannot be constructed.

    Unlike the Lambda-managed connectors (whose ARNs end in a well-known name
    like ALL_INGRESS), a customer-owned connector's ARN embeds a
    service-generated id -- `network-connector:nc-<uuid>` -- with no relation to
    the Name property. Building it by string format would silently produce an
    ARN that does not exist, and RunMicrovm would fail every meeting.
    """
    assert 'os.environ.get("EGRESS_CONNECTOR_ARN")' in launcher


def test_egress_connector_uses_private_subnets_and_the_vp_security_group(
    template: dict,
) -> None:
    """Egress must match the ECS path: out through the VPC's NAT gateway.

    The Lambda-managed INTERNET_EGRESS pool uses AWS-owned shared addresses, and
    Zoom redirected anonymous joins from those to a sign-in page with reCAPTCHA
    while the byte-identical container on ECS joined fine. Only the private
    subnets carry a NAT route, so public subnets here would break egress.
    """
    conn = template["Resources"]["VPMicrovmEgressConnector"]
    assert conn["Condition"] == "UseMicrovmLaunchType"
    cfg = conn["Properties"]["Configuration"]["VpcEgressConfiguration"]
    assert cfg["SubnetIds"] == [{"Ref": "PrivateSubnet1"}, {"Ref": "PrivateSubnet2"}]
    assert cfg["SecurityGroupIds"] == [{"Ref": "VPSecurityGroupId"}]
    # "MicroVm" is the only value the service accepts today.
    assert cfg["AssociatedComputeResourceTypes"] == ["MicroVm"]


def test_egress_connector_operator_role_has_no_source_account_condition(
    template: dict,
) -> None:
    """CreateNetworkConnector does not populate aws:SourceAccount.

    A confused-deputy condition on the trust policy therefore evaluates to a
    DENY, and the connector create fails with "The service is unable to assume
    the provided NetworkConnectorOperatorRole." This is the same trap as the
    iam:PassedToService condition on VPMicrovmLauncherRole -- both cost a full
    deploy cycle to discover, so both are pinned here.

    Confidentiality is preserved by the role's minimal permission set (ENI
    management only) rather than by a condition key the service never sends.
    """
    role = template["Resources"]["VPMicrovmEgressOperatorRole"]
    for statement in role["Properties"]["AssumeRolePolicyDocument"]["Statement"]:
        assert "Condition" not in statement, (
            "aws:SourceAccount is not populated by CreateNetworkConnector; "
            "a condition here makes the connector un-assumable"
        )


def test_egress_connector_sets_an_operator_role(template: dict) -> None:
    """Required by the service even though the CFN schema marks it optional.

    A live create without it fails with "NetworkConnectorOperatorRole is
    required for VPC_EGRESS connector type". Lambda assumes this role to manage
    the ENIs that carry egress traffic.
    """
    conn = template["Resources"]["VPMicrovmEgressConnector"]
    assert conn["Properties"]["OperatorRole"] == {
        "Fn::GetAtt": "VPMicrovmEgressOperatorRole.Arn"
    }


def test_microvm_image_egresses_through_the_vpc_connector(
    template: dict,
) -> None:
    """The image and the run call must agree on the egress path."""
    image = template["Resources"]["VPMicrovmImage"]
    assert image["Properties"]["EgressNetworkConnectors"] == [
        {"Fn::GetAtt": "VPMicrovmEgressConnector.Arn"}
    ]


def test_launcher_may_pass_the_vpc_egress_connector(template: dict) -> None:
    """PassNetworkConnector is checked against the connector resource itself.

    RunMicrovm permission alone is not sufficient, so switching the egress
    connector without updating this policy would fail every launch.
    """
    role = template["Resources"]["VPMicrovmLauncherRole"]
    statements = role["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    pass_stmt = next(
        s for s in statements if s.get("Action") == "lambda:PassNetworkConnector"
    )
    assert {"Fn::GetAtt": "VPMicrovmEgressConnector.Arn"} in pass_stmt["Resource"]


def test_both_microvm_roles_can_decrypt_the_dynamodb_key(template: dict) -> None:
    """Registry access needs KMS as well as DynamoDB.

    Both tables use the customer-managed key. The launcher stages the VP's
    configuration there and the container reads it back (the runHookPayload limit
    is 4096, too small for the real config), so a missing grant means the VP
    starts with no configuration at all.
    """
    for role_name in ("TaskRole", "VPMicrovmLauncherRole"):
        # str(), not json.dumps: the resource tree contains intrinsics that are
        # not JSON-serialisable once parsed by the CFN loader.
        text = str(template["Resources"][role_name])
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


def test_image_carries_the_bootstrap_env_vars(template: dict) -> None:
    """The container needs enough env to FIND its staged config.

    Config is staged in the VP task registry (runHookPayload caps at 4096), but
    the container cannot read the registry without the table name — and the table
    name is itself part of that config. A live launch failed exactly this way:
    "VP_TASK_REGISTRY_TABLE_NAME not set; no staged config", after which the VP
    ran with defaults (platform Chime, empty meeting id) and never reported
    status.

    Keep this set MINIMAL: every value here is baked into the image, so adding one
    means a stack parameter change forces an image rebuild.
    """
    env = template["Resources"]["VPMicrovmImage"]["Properties"]["EnvironmentVariables"]
    keys = {item["Key"] for item in env}
    # VP_AWS_REGION, not AWS_REGION: the latter is reserved by the service and
    # rejected at image-build time (see test_image_env_avoids_reserved_keys).
    required = {"VP_LAUNCH_TYPE", "VP_TASK_REGISTRY_TABLE_NAME", "VP_AWS_REGION"}
    missing = required - keys
    assert not missing, f"image is missing bootstrap env vars: {sorted(missing)}"
    # Guard against the image quietly becoming the config channel again.
    assert len(keys) <= 5, (
        f"image env should stay minimal (bootstrap only), found {sorted(keys)}"
    )


# Keys the Lambda runtime owns. The MicroVM image build rejects these outright:
# "Environment variable key 'AWS_REGION' is reserved" — which failed a deploy
# after the image had already spent minutes building. Sourced from the documented
# Lambda reserved environment variables.
RESERVED_ENV_KEYS = frozenset(
    {
        "_HANDLER",
        "_X_AMZN_TRACE_ID",
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
        "AWS_EXECUTION_ENV",
        "AWS_LAMBDA_FUNCTION_NAME",
        "AWS_LAMBDA_FUNCTION_MEMORY_SIZE",
        "AWS_LAMBDA_FUNCTION_VERSION",
        "AWS_LAMBDA_INITIALIZATION_TYPE",
        "AWS_LAMBDA_LOG_GROUP_NAME",
        "AWS_LAMBDA_LOG_STREAM_NAME",
        "AWS_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_LAMBDA_RUNTIME_API",
        "LAMBDA_TASK_ROOT",
        "LAMBDA_RUNTIME_DIR",
    }
)


def test_image_env_avoids_reserved_keys(template: dict) -> None:
    """No image environment variable may use a reserved Lambda key.

    These are rejected at image-build time, so the failure costs a full build
    plus a stack rollback. AWS_REGION is the one that actually bit us — pass the
    region under a non-reserved name (VP_AWS_REGION) instead.
    """
    env = template["Resources"]["VPMicrovmImage"]["Properties"]["EnvironmentVariables"]
    used = {item["Key"] for item in env}
    clashes = used & RESERVED_ENV_KEYS
    assert not clashes, f"reserved env keys in the MicroVM image: {sorted(clashes)}"


def test_launcher_field_names_match_the_state_machine(raw: str, launcher: str) -> None:
    """Every FIELD_MAP candidate must name a real $.data key.

    A live join failed with an EMPTY meeting id — and a misleading "Invalid
    meeting ID" from Zoom — because the launcher read "meetingId" while the
    scheduler state machine sends "meetingID" (capital D). Nothing else catches a
    silent field-name mismatch: the value is simply absent and the app falls back
    to its default.
    """
    authoritative = set(re.findall(r"\$\.data\.([a-zA-Z]+)", raw))
    assert authoritative, "could not find any $.data.* keys in the template"

    block = launcher.split("FIELD_MAP = {", 1)[1].split("\n}", 1)[0]
    entries = re.findall(r'"([A-Z_]+)":\s*\(([^)]*)\)', block)
    assert entries, "could not parse FIELD_MAP"

    # Keys the launcher may source from elsewhere (e.g. the DynamoDB row rather
    # than the state machine payload).
    row_only = {"id", "owner", "refreshToken", "meetingId"}

    for env_key, raw_candidates in entries:
        candidates = [
            c.strip().strip("\"'") for c in raw_candidates.split(",") if c.strip()
        ]
        matched = [c for c in candidates if c in authoritative or c in row_only]
        assert matched, (
            f"{env_key} maps to {candidates}, none of which the template sends; "
            f"available: {sorted(authoritative)}"
        )
