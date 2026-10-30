#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Say why a nightly publish or deploy job failed, in a few lines.

The publish and deploy jobs post to Slack only when they fail, and until this
existed the post said only WHICH stage failed. The reason was a click and a
scroll away, and for a deploy it was further still: the job log names the
CloudFormation resource that failed, but when that resource is the custom
resource that runs an in-stack CodeBuild image build, the actual cause (a
compile error, a registry that timed out) is in that build's own log, in the
stack's account.

Run from the job's ``after_script`` on failure. Prints the cause to stdout,
which the caller writes to a file and hands to ``junit_summary.py --cause``::

    python integ-tests/failure_cause.py --stage deploy --log deploy-output.log \\
        --stack "$LMA_INTEG_STACK" --region "$AWS_DEFAULT_REGION" > failure-cause.txt

Two sources, best first:

  * deploy only — the stack itself. Walks the events of the latest update
    through every nested stack to the resources that failed on their own
    account (not the ones cancelled because something else failed, and not
    the nested-stack wrappers that only report a child's failure), and for a
    failed CodeBuild run, quotes the lines of that build's log that say why.
    It also says what state the stack was left in, because a stack whose
    rollback could not finish fails every later deploy until it is resumed.
  * the job's own output, captured by the job to ``--log``: the lines that say
    why, or the end of it when none of them match.

Like ``junit_summary``, this runs after a job that may have failed in any way,
so it never raises and never exits non-zero: an unavailable boto3, missing
credentials or an unreadable log each degrade to less detail, never to a
traceback where the cause should have been. Plain text throughout, no
backticks or asterisks: the message is composed in Slack's Workflow Builder,
which renders those literally.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_LINE = 300
MAX_LOG_LINES = 12
MAX_CAUSES = 4
MAX_BUILD_LINES = 3

# What a decisive line in a job log looks like. Matched against lines with the
# runner's ANSI colouring already removed.
_DECISIVE = re.compile(
    r"(^Traceback |^\w*(Error|Exception)\b|^E\s|^ERROR\b|^FAILED\b|^Error:|"
    r"\berror:|^npm ERR!|^make: \*\*\* |^✗|^❌|Status: )"
)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_RULE = re.compile(r"^[\s━─=\-]+$")

# The CloudFormation reasons that say "another resource failed", not "I failed".
_CASCADE = ("Resource creation cancelled", "Resource update cancelled", "resource creation Cancelled")
_NESTED_WRAPPER = ("was not successfully created", "was not successfully updated")
_OPERATION_START = {"CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS"}
_ROLLBACK_START = {"ROLLBACK_IN_PROGRESS", "UPDATE_ROLLBACK_IN_PROGRESS"}

# What each terminal stack state means for the next night. Only the states a
# failed deploy can leave; anything else is reported by name alone.
_STATE_MEANING = {
    "UPDATE_ROLLBACK_COMPLETE": (
        "it rolled back to its previous build and is usable, so the next night "
        "can deploy over it"
    ),
    "UPDATE_ROLLBACK_FAILED": (
        "its rollback could not finish, so every later deploy fails until the "
        "rollback is resumed (the next nightly deploy resumes it before deploying)"
    ),
    "ROLLBACK_COMPLETE": "its creation rolled back, so it must be deleted before it can be recreated",
    "ROLLBACK_FAILED": "its creation could not roll back, so it must be deleted by hand",
}

# A CodeBuild log's own bookkeeping, which never says why a build failed.
_CODEBUILD_NOISE = re.compile(r"^\[Container\] |^\s*$")
_CODEBUILD_FAILED_STATES = {"FAILED", "FAULT", "TIMED_OUT", "STOPPED"}


# CloudFormation's wrapping around a reason, which costs a chat message room and
# says nothing a reader acts on.
_REASON_NOISE = re.compile(
    r"^Received response status \[FAILED\] from custom resource\. Message returned: "
    r"|^Resource handler returned message: "
    r"|\s*\((RequestId|RequestToken): [^)]*\)\.?"
)


def _reason(text: str) -> str:
    return _REASON_NOISE.sub("", text).strip()


def _clip(line: str) -> str:
    line = line.rstrip()
    return line if len(line) <= MAX_LINE else line[:MAX_LINE].rstrip() + " …"


# ── the job's own output ───────────────────────────────────────────────────


def log_cause(text: str) -> list[str]:
    """The lines of a job's output that say why it failed.

    The decisive lines when there are any, else the last few lines, which is
    where a command that failed without a recognisable error says so.
    """
    lines = [_ANSI.sub("", raw.split("\r")[-1]).rstrip() for raw in text.splitlines()]
    lines = [line for line in lines if line.strip() and not _RULE.match(line)]
    picked = [line.strip() for line in lines if _DECISIVE.search(line.strip())]
    if not picked:
        picked = [line.strip() for line in lines[-MAX_LOG_LINES:]]
    unique: list[str] = []
    for line in picked:
        if line not in unique:
            unique.append(line)
    if len(unique) > MAX_LOG_LINES:
        # The last ones: a failure's final lines name what finally gave up.
        hidden = len(unique) - MAX_LOG_LINES
        unique = [f"… {hidden} earlier lines"] + unique[-MAX_LOG_LINES:]
    return [_clip(line) for line in unique]


# ── the stack ──────────────────────────────────────────────────────────────


def _events_since(cfn: Any, stack: str, since: datetime | None) -> list[dict]:
    """A stack's events, oldest first, from ``since`` (or its latest operation)."""
    events: list[dict] = []
    for page in cfn.get_paginator("describe_stack_events").paginate(StackName=stack):
        for event in page.get("StackEvents", []):
            if since is not None and event["Timestamp"] < since:
                return list(reversed(events))
            events.append(event)
            if (
                since is None
                and event.get("PhysicalResourceId") == event.get("StackId")
                and event.get("ResourceStatus") in _OPERATION_START
                and event.get("ResourceStatusReason") == "User Initiated"
            ):
                # Newest-first, so the first operation start seen is the latest.
                return list(reversed(events))
    return list(reversed(events))


def _own_failures(cfn: Any, stack: str, since: datetime, path: str, depth: int = 0) -> list[dict]:
    """Every resource in ``stack`` and its children that failed on its own account."""
    found: list[dict] = []
    for event in _events_since(cfn, stack, since):
        status = event.get("ResourceStatus", "")
        reason = event.get("ResourceStatusReason") or ""
        if "FAILED" not in status or event.get("PhysicalResourceId") == event.get("StackId"):
            continue
        if any(marker in reason for marker in _CASCADE):
            continue
        name = event.get("LogicalResourceId", "?")
        if event.get("ResourceType") == "AWS::CloudFormation::Stack" and any(
            marker in reason for marker in _NESTED_WRAPPER
        ):
            if depth < 5 and event.get("PhysicalResourceId"):
                found += _own_failures(
                    cfn,
                    event["PhysicalResourceId"],
                    since,
                    f"{path}{name} → ",
                    depth + 1,
                )
            continue
        found.append(
            {
                "where": f"{path}{name}",
                "type": event.get("ResourceType", ""),
                "reason": reason,
                "time": event["Timestamp"],
                "stack": event.get("StackId", stack),
            }
        )
    return found


def _build_cause(codebuild: Any, logs: Any, cfn: Any, stack: str, since: datetime) -> list[str]:
    """Why the CodeBuild run started by a custom resource in ``stack`` failed.

    The custom resource reports only that the build did not succeed. The build
    is the most recent failed one of the stack's CodeBuild projects that
    started after the update did; its failed phase and the lines of its log
    just before CodeBuild gave up are the cause.
    """
    projects = [
        summary["PhysicalResourceId"]
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack)
        for summary in page.get("StackResourceSummaries", [])
        if summary.get("ResourceType") == "AWS::CodeBuild::Project"
        and summary.get("PhysicalResourceId")
    ]
    for project in projects:
        ids = codebuild.list_builds_for_project(projectName=project).get("ids", [])[:5]
        if not ids:
            continue
        for build in codebuild.batch_get_builds(ids=ids).get("builds", []):
            if build.get("buildStatus") not in _CODEBUILD_FAILED_STATES:
                continue
            if build.get("startTime") and build["startTime"] < since:
                continue
            lines = [
                f"CodeBuild {phase['phaseType']} phase: {context.get('message') or context.get('statusCode')}"
                for phase in build.get("phases", [])
                if phase.get("phaseStatus") in _CODEBUILD_FAILED_STATES
                for context in phase.get("contexts", [])[:1]
            ][:1]
            group = build.get("logs", {}).get("groupName")
            stream = build.get("logs", {}).get("streamName")
            if group and stream:
                lines += _build_log_cause(logs, group, stream)
            return [_clip(line) for line in lines]
    return []


def _build_log_cause(logs: Any, group: str, stream: str) -> list[str]:
    """The output just before a CodeBuild command was reported as failed."""
    marker = "Command did not exit successfully"
    lines: list[str] = []
    token = None
    # Paged until the marker: a --no-cache image build can log more than one
    # page holds, and the lines that matter are just before the marker.
    for _ in range(20):
        kwargs = {"logGroupName": group, "logStreamName": stream, "startFromHead": True}
        if token:
            kwargs["nextToken"] = token
        response = logs.get_log_events(**kwargs)
        lines += [
            line.rstrip()
            for event in response.get("events", [])
            for line in event.get("message", "").splitlines()
        ]
        following = response.get("nextForwardToken")
        if any(marker in line for line in lines) or not following or following == token:
            break
        token = following
    end = next(
        (i for i, line in enumerate(lines) if marker in line),
        len(lines),
    )
    output = [line for line in lines[:end] if not _CODEBUILD_NOISE.match(line)]
    return output[-MAX_BUILD_LINES:]


def stack_cause(
    cfn: Any, codebuild: Any, logs: Any, stack: str, since: datetime | None = None
) -> list[str]:
    """The stack's state and the resources whose failure decided it.

    ``since`` is when the job started. An operation older than that was not
    started by this job — the deploy failed before CloudFormation began one —
    so its failures are another night's and are not reported as this one's;
    only the stack's state is, and the job's own output supplies the cause.
    """
    state = cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
    meaning = _STATE_MEANING.get(state)
    lines = [f"Stack is {state}" + (f": {meaning}." if meaning else ".")]

    root_events = _events_since(cfn, stack, None)
    if not root_events:
        return lines
    started = root_events[0]["Timestamp"]
    if since is not None and started < since:
        return lines
    rollback = next(
        (e["Timestamp"] for e in root_events
         if e.get("PhysicalResourceId") == e.get("StackId")
         and e.get("ResourceStatus") in _ROLLBACK_START),
        None,
    )

    failures = sorted(_own_failures(cfn, stack, started, ""), key=lambda f: f["time"])
    forward = [f for f in failures if rollback is None or f["time"] < rollback]
    backward = [f for f in failures if rollback is not None and f["time"] >= rollback]

    for heading, group in (("Failed:", forward), ("Then, during the rollback:", backward)):
        if not group:
            continue
        lines.append(heading)
        for failure in group[:MAX_CAUSES]:
            lines.append(_clip(f"• {failure['where']}: {_reason(failure['reason'])}"))
            if "build did not complete" in failure["reason"]:
                try:
                    detail = _build_cause(codebuild, logs, cfn, failure["stack"], started)
                except Exception as exc:  # noqa: BLE001 - detail is best-effort
                    detail = [f"(the build's log could not be read: {type(exc).__name__})"]
                lines += [f"    {line}" for line in detail]
        if len(group) > MAX_CAUSES:
            lines.append(f"… and {len(group) - MAX_CAUSES} more")
    return lines


def _stack_cause_or_none(
    stack: str, region: str | None, since: datetime | None
) -> list[str] | None:
    try:
        import boto3  # noqa: PLC0415 - optional; the log fallback needs no AWS
    except ImportError:
        return None
    try:
        session = boto3.Session(region_name=region)
        return stack_cause(
            session.client("cloudformation"),
            session.client("codebuild"),
            session.client("logs"),
            stack,
            since,
        )
    except Exception as exc:  # noqa: BLE001 - never fail the notification
        print(f"stack events unreadable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def describe(stage: str, log_text: str | None, stack_lines: list[str] | None) -> str:
    """The cause, from the best source available."""
    if stack_lines and len(stack_lines) > 1:
        return "\n".join(stack_lines)
    if log_text is None:
        return (
            f"The {stage} job failed before its main step started, so it wrote "
            "no output to quote: the reason is in the job's setup (installing "
            "tools or reading the AWS credentials). See the job log."
        )
    body = log_cause(log_text)
    if stack_lines:
        # The stack's state is still worth a line when its events said nothing
        # (a deploy refused before CloudFormation started, say).
        body = stack_lines + body
    return "\n".join(body) if body else f"The {stage} job wrote no output. See the job log."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Say why a nightly stage failed.")
    parser.add_argument("--stage", required=True, help="publish or deploy")
    parser.add_argument("--log", default=None, help="The job's captured output, if any.")
    parser.add_argument("--stack", default=None, help="Stack to read events from (deploy only).")
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--since",
        default=None,
        help="When the job started (ISO 8601, e.g. $CI_JOB_STARTED_AT); older operations are ignored.",
    )
    args = parser.parse_args(argv)

    log_text = None
    if args.log:
        try:
            log_text = Path(args.log).read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_text = None

    stack_lines = None
    if args.stage == "deploy" and args.stack:
        stack_lines = _stack_cause_or_none(args.stack, args.region, _parse_time(args.since))

    print(describe(args.stage, log_text, stack_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
