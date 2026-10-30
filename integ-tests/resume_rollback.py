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
rollback fails again the job fails here, printing the reason each resource
that failed gave (read from the stack's events, as failure_cause.py does),
rather than in a deploy CloudFormation would reject anyway.

Usage::

    .venv/bin/python integ-tests/resume_rollback.py --stack lma-integtest1 --region us-west-2
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import failure_cause  # noqa: E402

STUCK = "UPDATE_ROLLBACK_FAILED"
# A resumed rollback rebuilds whatever the failed update had replaced, which
# for a MicroVM image is a build of several minutes; one observed took about
# ten. Capped at 15 minutes so a slow rollback fails here, saying so, rather
# than leaving the deploy too little of the job's 55-minute timeout. A stack
# that needed it may then finish the deploy only on the next night.
_WAIT = {"Delay": 30, "MaxAttempts": 30}


def resume_if_stuck(cfn: Any, stack: str) -> bool:
    """Resume the stack's rollback if it is stuck. Returns whether it was.

    Raises if the resumed rollback does not reach UPDATE_ROLLBACK_COMPLETE.
    """
    status = cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
    if status != STUCK:
        print(f"Stack {stack} is {status}; no rollback to resume.")
        return False
    print(f"Stack {stack} is {STUCK}; resuming its rollback before deploying.")
    resumed = datetime.now(timezone.utc)
    cfn.continue_update_rollback(StackName=stack)
    try:
        cfn.get_waiter("stack_rollback_complete").wait(StackName=stack, WaiterConfig=_WAIT)
    except Exception:
        # The waiter names only the stack's final status. The reasons are in
        # the events of the resources that failed again, in this stack or a
        # nested one; printed so they reach the job's captured output.
        try:
            failures = failure_cause.own_failures(cfn, stack, resumed)
        except Exception as exc:  # noqa: BLE001 - the reason is best-effort
            failures = []
            print(f"(the stack's events could not be read: {type(exc).__name__})")
        print("Error: the resumed rollback did not complete.")
        for failure in failures:
            print(f"✗ {failure['where']}: {failure_cause.reason(failure['reason'])}")
        raise
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
