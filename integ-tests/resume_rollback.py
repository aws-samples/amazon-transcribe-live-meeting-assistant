#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Finish a nightly stack's interrupted rollback before deploying over it.

A deploy that fails rolls the stack back, and a rollback can itself fail — for
instance when it reaches a resource that is still busy with the update it is
undoing, such as a MicroVM image whose build had not finished. CloudFormation
then leaves the stack in UPDATE_ROLLBACK_FAILED and refuses every update until
the rollback is resumed, so one bad night would turn into every night failing
the same way until somebody noticed.

The nightly deploy job runs this first. On a stack in any other state it does
nothing. On UPDATE_ROLLBACK_FAILED it resumes the rollback once, without
skipping any resource, and waits: the usual cause is a resource that was busy
when the rollback first reached it and has since settled. If the resumed
rollback fails again the job fails here, with the stack's own reason, rather
than in a deploy CloudFormation would reject anyway.

Usage::

    .venv/bin/python integ-tests/resume_rollback.py --stack lma-integtest1 --region us-west-2
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

STUCK = "UPDATE_ROLLBACK_FAILED"
# A resumed rollback rebuilds whatever the failed update had replaced, which
# for a MicroVM image is a build of several minutes. Polled every 30 seconds,
# inside the deploy job's own timeout with the deploy still to run.
_WAIT = {"Delay": 30, "MaxAttempts": 50}


def resume_if_stuck(cfn: Any, stack: str) -> bool:
    """Resume the stack's rollback if it is stuck. Returns whether it was.

    Raises if the resumed rollback does not reach UPDATE_ROLLBACK_COMPLETE.
    """
    status = cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
    if status != STUCK:
        print(f"Stack {stack} is {status}; no rollback to resume.")
        return False
    print(f"Stack {stack} is {STUCK}; resuming its rollback before deploying.")
    cfn.continue_update_rollback(StackName=stack)
    cfn.get_waiter("stack_rollback_complete").wait(StackName=stack, WaiterConfig=_WAIT)
    print(f"Stack {stack} rolled back; deploying over it.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resume a stuck rollback before deploying.")
    parser.add_argument("--stack", required=True)
    parser.add_argument("--region", default=None)
    args = parser.parse_args(argv)

    import boto3  # noqa: PLC0415 - imported late so --help works without it

    try:
        resume_if_stuck(boto3.client("cloudformation", region_name=args.region), args.stack)
    except Exception as exc:  # noqa: BLE001 - reported, then the job fails
        print(f"Could not finish the rollback of {args.stack}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
