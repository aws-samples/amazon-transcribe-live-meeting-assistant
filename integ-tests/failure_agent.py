# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Agentic triage for a failed nightly integration run.

WHY THIS EXISTS
---------------
The notification `junit_summary.py` builds says which tests failed and what
they asserted. That is enough to act on when the assertion is the cause, and
not enough when it is a symptom. ``test_ws_stream_transcribes_to_meeting``
fails with "no transcript segments were produced within 90s" whether the cause
is the WebSocket server rejecting the upgrade, Transcribe refusing the stream,
the Kinesis consumer erroring, the Call Event Processor throwing, or an
expired Cognito user — and the evidence that distinguishes those lives in five
different CloudWatch log groups, not in the pytest output.

Reading those logs is evidence GATHERING, which a fixed summary cannot do: you
cannot know which log group to quote until you have read the first one. So this
runs Claude Code headless (``claude -p``) against Bedrock, hands it the JUnit
report and the run's own output ON DISK, and lets it grep those, read the repo,
and query the live stack read-only.

The log goes to disk rather than into the prompt deliberately: a nightly's
output is tens of thousands of lines, most of it pytest progress, and paying
per-token for the model to read "collected 40 items" is waste. A file it greps
costs almost nothing.

SAFETY MODEL
------------
The nightly's own role carries AdministratorAccess in the test account (it
deploys a dozen nested stacks; see docs/scheduled-integration-tests.md on why
the containment is the account rather than the role). An agent inheriting that
could delete the stack it is diagnosing.

So, unlike the summary it feeds, this module is **fail-closed on credentials**:

* IAM is the boundary. The agent runs only on credentials from
  ``sts:AssumeRole`` into a read-only role named by
  ``LMA_FAILURE_AGENT_ROLE_ARN``. If that is unset, or the assume fails, the
  agent DOES NOT RUN. It does not fall back to the ambient credentials — with
  AdministratorAccess ambient, a fallback is the whole risk.
* The harness allowlist is a speed bump, not a boundary. ``--allowedTools``
  lists read-only verbs and ``--disallowedTools`` strips the write tools from
  the agent's context, but Bash allowlist rules are prefix globs, so
  ``Bash(aws logs *)`` does not stop a chained
  ``aws logs ... ; aws cloudformation delete-stack ...``. Treat it as noise
  reduction; the read-only role is what makes it safe.
* Blast radius on failure: this module never raises and never decides
  pass/fail. It returns a string or None, and Python owns the verdict. A
  notification that reported a test as passing because an agent said so would
  be worse than no agent.

Opt-in via ``LMA_FAILURE_AGENT=1`` so a slow or flaky agent cannot destabilise
the nightly. With it off, the notification is exactly what it was.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess  # nosec B404 - fixed argv, no shell, no interpolated input
import sys
import time
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────

AGENT_ENABLED = os.environ.get("LMA_FAILURE_AGENT", "0").strip().lower() in ("1", "true")

# Pinned so a surprise npm release cannot change CI behaviour mid-week.
CLAUDE_CODE_NPM_SPEC = os.environ.get(
    "LMA_FAILURE_AGENT_NPM_SPEC", "@anthropic-ai/claude-code@2.1.281"
)

# Inference profiles that are ACTIVE in the test account. The Haiku pin matters:
# on Bedrock, background chores (session titles and similar) follow the primary
# model unless a Haiku model is named, so without it they would bill at Sonnet
# rates for no benefit.
AGENT_MODEL = os.environ.get(
    "LMA_FAILURE_AGENT_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
AGENT_HAIKU_MODEL = os.environ.get(
    "LMA_FAILURE_AGENT_HAIKU_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

# Three independent caps. --max-turns and --max-budget-usd are enforced by the
# CLI; the wall clock is ours, because a single turn can stall on a slow AWS
# call and neither CLI cap would fire. All three are well inside the job's own
# timeout, which is itself inside the one-hour credential ceiling.
AGENT_MAX_TURNS = int(os.environ.get("LMA_FAILURE_AGENT_MAX_TURNS", "30"))
AGENT_MAX_BUDGET_USD = os.environ.get("LMA_FAILURE_AGENT_BUDGET_USD", "3.00")
AGENT_WALL_CLOCK_SECONDS = int(os.environ.get("LMA_FAILURE_AGENT_TIMEOUT", "600"))

# Read-only role. No default: running without it is the thing this refuses to
# do, so an unset variable has to be visible rather than papered over.
AGENT_ROLE_ARN = os.environ.get("LMA_FAILURE_AGENT_ROLE_ARN", "").strip()

# Read-only AWS verbs the agent may run unprompted — the services that actually
# hold LMA failure evidence.
_ALLOWED_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "Bash(aws logs describe-log-groups*)",
    "Bash(aws logs describe-log-streams*)",
    "Bash(aws logs get-log-events*)",
    "Bash(aws logs filter-log-events*)",
    "Bash(aws logs start-query*)",
    "Bash(aws logs get-query-results*)",
    "Bash(aws cloudformation describe-stacks*)",
    "Bash(aws cloudformation describe-stack-events*)",
    "Bash(aws cloudformation describe-stack-resources*)",
    "Bash(aws cloudformation list-stack-resources*)",
    "Bash(aws ecs describe-services*)",
    "Bash(aws ecs list-services*)",
    "Bash(aws ecs list-clusters*)",
    "Bash(aws ecs describe-tasks*)",
    "Bash(aws ecs list-tasks*)",
    "Bash(aws elbv2 describe-target-health*)",
    "Bash(aws elbv2 describe-target-groups*)",
    "Bash(aws elbv2 describe-load-balancers*)",
    "Bash(aws elbv2 describe-rules*)",
    "Bash(aws kinesis describe-stream*)",
    "Bash(aws dynamodb query*)",
    "Bash(aws dynamodb scan*)",
    "Bash(aws dynamodb describe-table*)",
    "Bash(aws appsync get-graphql-api*)",
    "Bash(aws appsync list-graphql-apis*)",
    "Bash(aws stepfunctions list-executions*)",
    "Bash(aws stepfunctions describe-execution*)",
    "Bash(aws stepfunctions get-execution-history*)",
    "Bash(aws cognito-idp describe-user-pool*)",
    "Bash(aws cognito-idp list-users*)",
    "Bash(aws lambda get-function-configuration*)",
    "Bash(aws sts get-caller-identity*)",
    "Bash(git log*)",
    "Bash(git show*)",
    "Bash(git diff*)",
    "Bash(git blame*)",
]

# Bare names remove the tool from the agent's context entirely.
_DISALLOWED_TOOLS = ["Edit", "Write", "NotebookEdit", "WebSearch", "WebFetch"]

_AGENT_TASK = """\
The LMA nightly integration run failed. Diagnose it.

{brief}

HOW TO WORK
- The JUnit report and the run's full output are on disk at the paths above.
  Grep them; do not ask for them to be pasted.
- The stack is still deployed and you have READ-ONLY credentials for its
  account. The evidence for a transcription failure is usually in CloudWatch:
  the WebSocket transcriber's Fargate log group, the Call Event Processor
  Lambda, and the Virtual Participant's. Find the log groups rather than
  guessing their names.
- `git log` and `git diff` are available if you need to know what changed.

GROUNDING RULES - follow strictly
- Base the root cause ONLY on evidence you actually read: a concrete log line,
  a stack event, an API response, or the test's own assertion. Do NOT offer
  likely causes.
- If you cannot find the cause, say so plainly and say which evidence was
  missing and how to capture it next time. That is a useful answer. A
  confident guess is not.
- Distinguish "the deployed code is wrong" from "the test environment is
  wrong" (an expired secret, a throttle, a quota, an account setting) - they
  go to different people.
- You are advisory. Do not state a verdict on whether the run passed; that is
  already decided and reported.

OUTPUT
Plain text, no markdown, at most 15 lines, in this shape:

ROOT CAUSE: <one or two sentences, or "not determined">
EVIDENCE: <the specific lines/resources you based that on>
NEXT STEP: <the single most useful thing a human should do>
"""


# ── Helpers ────────────────────────────────────────────────────────────────


def _log(message: str) -> None:
    print(f"failure-agent: {message}", flush=True)


def _readonly_credentials() -> dict[str, str] | None:
    """Assume the read-only role. Returns None if that is not possible."""
    if not AGENT_ROLE_ARN:
        return None
    try:
        import boto3  # noqa: PLC0415 - optional at import time, like the agent itself

        response = boto3.client("sts").assume_role(
            RoleArn=AGENT_ROLE_ARN,
            RoleSessionName="lma-nightly-failure-agent",
            DurationSeconds=3600,
        )
    except Exception as exc:  # noqa: BLE001 - never raise out of the agent
        _log(f"could not assume {AGENT_ROLE_ARN}: {exc}")
        return None
    creds = response["Credentials"]
    return {
        "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
        "AWS_SESSION_TOKEN": creds["SessionToken"],
        # Any inherited profile or shared-credentials pointer would otherwise
        # win over these in the SDK's search order.
        "AWS_PROFILE": "",
        "AWS_SHARED_CREDENTIALS_FILE": "",
    }


def ensure_claude_code() -> str | None:
    """Return a path to the claude binary, installing it if needed."""
    existing = shutil.which("claude")
    if existing:
        return existing
    if not shutil.which("npm"):
        _log("npm is not available, so Claude Code cannot be installed — skipping")
        return None
    _log(f"installing {CLAUDE_CODE_NPM_SPEC}")
    try:
        subprocess.run(  # nosec B603,B607 - fixed argv, pinned spec, no shell
            ["npm", "install", "-g", CLAUDE_CODE_NPM_SPEC],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _log(f"install failed: {exc} — skipping")
        return None
    return shutil.which("claude")


def build_brief(report_path: Path, log_path: Path, stack: str, region: str) -> str:
    """The deterministic facts the agent starts from.

    Deliberately small. Its job is to point at the evidence, not to be the
    evidence — everything here is a path the agent can read or a name it can
    query, so the brief costs a handful of tokens rather than a log's worth.
    """
    failures: list[str] = []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import junit_summary  # noqa: PLC0415 - same directory, no package

        parsed = junit_summary.parse_report(report_path)
        if parsed.problem:
            failures.append(f"(no usable report: {parsed.problem})")
        else:
            for failure in parsed.detail:
                if failure.kind != "SKIPPED":
                    failures.append(f"- {failure.name}: {failure.message}")
    except Exception as exc:  # noqa: BLE001 - the brief must not be the failure
        failures.append(f"(could not read the report: {exc})")

    lines = [
        f"Stack: {stack or '(not named)'}",
        f"Region: {region or '(not named)'}",
        f"JUnit report on disk: {report_path}",
        f"Full run output on disk: {log_path}"
        if log_path.is_file()
        else "Full run output: not captured",
        "",
        "Tests that did not pass:",
        *(failures or ["(none recorded — the run may have failed before pytest ran)"]),
    ]
    return "\n".join(lines)


# ── Entry point ────────────────────────────────────────────────────────────


def run_failure_agent(
    *,
    report_path: Path,
    log_path: Path,
    stack: str,
    region: str,
) -> str | None:
    """Diagnose a failed nightly with Claude Code headless.

    Returns the agent's report, or None when it is disabled, cannot be run
    safely, or could not finish. Never raises.
    """
    if not AGENT_ENABLED:
        _log("disabled (set LMA_FAILURE_AGENT=1 to enable) — skipping")
        return None

    started = time.time()
    try:
        creds = _readonly_credentials()
        if creds is None:
            # Fail closed. See the module docstring: the ambient credentials are
            # AdministratorAccess in this account, so "run anyway" is the one
            # outcome worth refusing.
            _log(
                "no read-only credentials (LMA_FAILURE_AGENT_ROLE_ARN unset or "
                "not assumable) — NOT running. The nightly's own credentials "
                "are administrative, and the agent must never inherit them."
            )
            return None

        claude = ensure_claude_code()
        if not claude:
            return None

        brief = build_brief(report_path, log_path, stack, region)

        env = os.environ.copy()
        env.update(creds)
        env.update(
            {
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "ANTHROPIC_MODEL": AGENT_MODEL,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": AGENT_HAIKU_MODEL,
                "AWS_REGION": region or env.get("AWS_DEFAULT_REGION", "us-west-2"),
                # Never let CI wait on a TTY that does not exist.
                "CI": "1",
                "DISABLE_TELEMETRY": "1",
                "DISABLE_AUTOUPDATER": "1",
            }
        )

        cmd = [
            claude,
            "-p",
            _AGENT_TASK.format(brief=brief),
            "--output-format",
            "json",
            "--model",
            AGENT_MODEL,
            "--max-turns",
            str(AGENT_MAX_TURNS),
            "--max-budget-usd",
            str(AGENT_MAX_BUDGET_USD),
            # Anything outside the allowlist is DENIED rather than queued for a
            # prompt nobody can answer. Without this the agent hangs.
            "--permission-prompts",
            "none",
            "--no-session-persistence",
            "--allowedTools",
            *_ALLOWED_TOOLS,
            "--disallowedTools",
            *_DISALLOWED_TOOLS,
        ]

        _log(
            f"starting on read-only credentials (model={AGENT_MODEL}, "
            f"max_turns={AGENT_MAX_TURNS}, budget=${AGENT_MAX_BUDGET_USD}, "
            f"wall_clock={AGENT_WALL_CLOCK_SECONDS}s)"
        )
        result = subprocess.run(  # nosec B603 - fixed argv, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=AGENT_WALL_CLOCK_SECONDS,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        _log(f"timed out after {AGENT_WALL_CLOCK_SECONDS}s — no triage this run")
        return None
    except Exception as exc:  # noqa: BLE001 - advisory code must not break the job
        _log(f"unexpected error: {exc} — no triage this run")
        return None

    elapsed = time.time() - started
    if result.returncode != 0:
        _log(
            f"exit {result.returncode} after {elapsed:.0f}s — no triage this run. "
            f"stderr: {result.stderr[-500:]}"
        )
        return None

    report, cost = _parse_agent_output(result.stdout)
    if not report:
        _log(f"produced no text after {elapsed:.0f}s — no triage this run")
        return None
    _log(f"done in {elapsed:.0f}s" + (f", cost ${cost:.2f}" if cost is not None else ""))
    return report


def _parse_agent_output(stdout: str) -> tuple[str | None, float | None]:
    """Pull the report text and cost out of `--output-format json`."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        # Shape changed, or the CLI printed something else entirely. The raw
        # text is still better than nothing, so long as it is not enormous.
        text = stdout.strip()
        return (text if 0 < len(text) < 8000 else None), None
    if isinstance(payload, list):  # streamed turns; the last one holds the result
        payload = payload[-1] if payload else {}
    text = (payload.get("result") or payload.get("text") or "").strip()
    cost = payload.get("total_cost_usd")
    return (text or None), (float(cost) if isinstance(cost, (int, float)) else None)


def main(argv: list[str] | None = None) -> int:
    """Write the agent's report to a file, or write nothing. Always exits 0.

    Exits 0 even when no report is produced, because the caller is an
    `after_script`-adjacent step in a job that has ALREADY failed: a non-zero
    exit here would replace a useful test failure with a useless one.
    """
    parser = argparse.ArgumentParser(description="Triage a failed nightly integration run.")
    parser.add_argument("--report", default="integ-tests-report.xml", help="JUnit XML path.")
    parser.add_argument(
        "--log", default="integ-tests-output.log", help="The run's captured output."
    )
    parser.add_argument("--stack", default=os.environ.get("LMA_INTEG_STACK", ""))
    parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", ""))
    parser.add_argument(
        "--out",
        default="triage-report.txt",
        help="Where to write the report. Not written when there is nothing to say.",
    )
    args = parser.parse_args(argv)

    text = run_failure_agent(
        report_path=Path(args.report),
        log_path=Path(args.log),
        stack=args.stack,
        region=args.region,
    )
    if text:
        Path(args.out).write_text(text, encoding="utf-8")
        _log(f"report written to {args.out}")
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
