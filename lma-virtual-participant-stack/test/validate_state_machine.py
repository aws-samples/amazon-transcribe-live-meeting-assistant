#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Validate the VP scheduler state-machine definition with the real AWS validator.

The unit suite checks reachability offline, but only Step Functions itself catches
every schema rule. `stepfunctions validate-state-machine-definition` is a free,
read-only API — far cheaper than discovering a bad definition ~40 minutes into a
stack update and then waiting out a rollback.

Requires AWS credentials (read-only). Not part of `make test-vp-template`, which
must stay AWS-free; run it before deploying a change to the state machine:

    python lma-virtual-participant-stack/test/validate_state_machine.py
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import boto3
import yaml

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE.parent / "template.yaml"

# Realistic stand-ins: Step Functions validates Resource ARNs, so the sentinel
# values used by the offline tests are rejected here.
SUBSTITUTIONS = {
    "AWS::Partition": "aws",
    "AWS::Region": "us-west-2",
    "AWS::AccountId": "123456789012",
    "MicrovmLauncherFunctionArn": "arn:aws:lambda:us-west-2:123456789012:function:launcher",
    "VPMicrovmLauncherFunction.Arn": "arn:aws:lambda:us-west-2:123456789012:function:launcher",
    "VPScheduleInvokeLauncherRole.Arn": "arn:aws:iam::123456789012:role/sched",
    "ClusterArn": "arn:aws:ecs:us-west-2:123456789012:cluster/lma",
    "Cluster.Arn": "arn:aws:ecs:us-west-2:123456789012:cluster/lma",
    "TargetECSRoleArn": "arn:aws:iam::123456789012:role/target-ecs",
    "TargetECSRole.Arn": "arn:aws:iam::123456789012:role/target-ecs",
    "TaskDefinition": "arn:aws:ecs:us-west-2:123456789012:task-definition/vp:1",
    "VPSecurityGroupId": "sg-0123456789abcdef0",
    "PrivateSubnet1": "subnet-0123456789abcdef0",
    "PrivateSubnet2": "subnet-0123456789abcdef1",
    "ScheduleGroup": "lma-vp-schedules",
    "LMAStackName": "LMA",
    "VirtualParticipantTableName": "LMA-vp",
}


def _load_test_helpers():
    spec = importlib.util.spec_from_file_location(
        "vp_tests", HERE / "test_vp_template.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render(launch_type: str) -> str:
    helpers = _load_test_helpers()
    template = yaml.load(TEMPLATE.read_text(), Loader=helpers._CfnLoader)
    body, variables = helpers._state_machine_definition(template)

    is_microvm = launch_type == "MICROVM"
    index = 1 if is_microvm else 2

    def branch(name: str) -> str:
        frag = variables[name]["Fn::If"][index]
        return frag.get("Fn::Sub", frag) if isinstance(frag, dict) else frag

    rendered = body.replace("${DispatchStates}", branch("DispatchStates"))
    rendered = rendered.replace(
        "${ScheduledMeetingTarget}", branch("ScheduledMeetingTarget")
    )
    rendered = rendered.replace(
        "${DispatchStateName}", "RunMicrovm" if is_microvm else "RunTask"
    )
    for key, value in SUBSTITUTIONS.items():
        rendered = rendered.replace("${" + key + "}", value)
    rendered = re.sub(r"\$\{[^}]+\}", "placeholder", rendered)
    return json.dumps(json.loads(rendered))


def main() -> int:
    client = boto3.client("stepfunctions")
    failures = 0
    for launch_type in ("EC2", "FARGATE", "MICROVM"):
        response = client.validate_state_machine_definition(
            definition=render(launch_type)
        )
        result = response["result"]
        print(f"{launch_type:8s} -> {result}")
        for diagnostic in response.get("diagnostics", []):
            print(
                f"    [{diagnostic['severity']}] {diagnostic['code']} "
                f"{diagnostic.get('location', '')}: {diagnostic['message']}"
            )
        if result != "OK":
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
