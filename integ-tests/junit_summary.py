#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Turn the nightly run's JUnit report into a chat-notification payload.

The ``nightly_integ_tests`` job writes ``integ-tests-report.xml``; its
``after_script`` runs this to build the JSON body it posts to Slack. Printing a
payload rather than posting it keeps the webhook URL — a bearer credential — out
of this process's arguments and out of Python entirely: the caller pipes the
output to ``curl``.

Usage (see .gitlab-ci.yml)::

    python integ-tests/junit_summary.py \\
        --report integ-tests-report.xml \\
        --status "$CI_JOB_STATUS" \\
        --job-url "$CI_JOB_URL" \\
        --commit "$CI_COMMIT_SHA" \\
        --stack "$LMA_INTEG_STACK" > slack-payload.json

PAYLOAD SHAPE. The target is a Slack **Workflow Builder** webhook, not an
incoming webhook: installing a Slack app needs workspace-admin approval that a
workflow does not. The two take different bodies and the difference is silent.
An incoming webhook wants ``{"text": …, "blocks": […]}``; a Workflow Builder
trigger wants a FLAT object of string values whose keys are the variables
declared on the trigger, and ignores anything it does not recognise. So the four
keys below — ``status``, ``commit``, ``job_url``, ``summary`` — are a contract
with the workflow and must be declared there, as Text, under exactly these
names. Renaming one here without renaming it there posts a message with a blank
variable rather than failing. The stack name and the per-test detail are folded
into ``summary`` for that reason, rather than being keys of their own.

The workflow's message template must not use Slack's ``<url|label>`` link syntax
or typed backticks and asterisks. Those are API mrkdwn: they render only in a
body sent by an app, and a message composed in Workflow Builder shows them
literally. Put ``{{job_url}}`` on its own line and Slack auto-links it, which is
why ``job_url`` is sent as a bare URL.

This runs in an ``after_script`` after a run that may have failed in any number
of ways, so it never raises: a missing, truncated or unparseable report produces
a payload that says so. A notification that reports "no report" is useful; a
traceback where the notification should have been is not.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Slack rejects a whole message past 4000 characters rather than truncating it,
# and the summary is one variable among several in the workflow's template. Well
# under the cap, with the full detail a click away in the job artifacts.
_DEFAULT_MAX_CHARS = 2000

# Per-failure budget. A pytest assertion message can be hundreds of lines of
# diff; the first few lines are what identifies the failure in a chat message.
_MAX_FAILURE_CHARS = 400
_MAX_FAILURES_LISTED = 8


@dataclass
class _Failure:
    name: str
    kind: str  # "FAILED", "ERROR" or "SKIPPED"
    message: str


@dataclass
class _Report:
    """What the JUnit XML says, or why it could not be read."""

    total: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    duration: float = 0.0
    detail: list[_Failure] = field(default_factory=list)
    problem: str | None = None

    @property
    def passed(self) -> int:
        return max(self.total - self.failures - self.errors - self.skipped, 0)


def _text_of(element: ET.Element) -> str:
    """The most informative text a failure element carries.

    pytest puts a one-line summary in ``message=`` and the full traceback in the
    element body. The attribute is the better choice for a chat message, but it
    is absent on some writers, so fall back to the body.
    """
    message = (element.get("message") or "").strip()
    if message:
        return message
    return (element.text or "").strip()


def _first_lines(text: str, limit: int = _MAX_FAILURE_CHARS) -> str:
    """Collapse a traceback to its leading, most identifying part."""
    collapsed = " ".join(line.strip() for line in text.splitlines() if line.strip())
    if len(collapsed) > limit:
        return collapsed[:limit].rstrip() + " …"
    return collapsed


def parse_report(path: Path) -> _Report:
    """Read a pytest JUnit XML report. Never raises; sets ``problem`` instead."""
    if not path.is_file():
        return _Report(
            problem=(
                f"No JUnit report at {path} — the run failed before pytest produced "
                "one (credential setup, dependency install, or the job timing out). "
                "The job log has the reason."
            )
        )
    try:
        root = ET.parse(path).getroot()  # noqa: S314 - our own CI's output
    except ET.ParseError as exc:
        return _Report(problem=f"Could not parse {path}: {exc}. See the job log.")

    # pytest writes <testsuites><testsuite>…</testsuite></testsuites>; some
    # versions and other writers emit a bare <testsuite>. Accept both.
    suites = list(root.iter("testsuite"))
    if not suites:
        return _Report(problem=f"{path} contains no test suite. See the job log.")

    report = _Report()
    for suite in suites:
        report.total += int(suite.get("tests") or 0)
        report.failures += int(suite.get("failures") or 0)
        report.errors += int(suite.get("errors") or 0)
        report.skipped += int(suite.get("skipped") or 0)
        report.duration += float(suite.get("time") or 0.0)

        for case in suite.iter("testcase"):
            for tag, kind in (("failure", "FAILED"), ("error", "ERROR"), ("skipped", "SKIPPED")):
                for element in case.findall(tag):
                    report.detail.append(
                        _Failure(
                            name=case.get("name") or "(unnamed test)",
                            kind=kind,
                            message=_first_lines(_text_of(element)),
                        )
                    )
    return report


def build_summary(
    report: _Report,
    *,
    stack: str | None = None,
    region: str | None = None,
    ok: bool = True,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str:
    """Compose the human-readable body of the notification."""
    where = stack or "(stack not named)"
    if region:
        where = f"{where} ({region})"

    lines: list[str] = []
    if report.problem:
        lines.append(f"Stack {where} — no results to report.")
        lines.append(report.problem)
    else:
        lines.append(
            f"Stack {where} — {report.passed} passed, "
            f"{report.failures + report.errors} failed, "
            f"{report.skipped} skipped, in {report.duration:.0f}s"
        )
        if report.total == 0:
            # Red flag of its own: the suite collected nothing, which under
            # --no-skips should be impossible. A green job here would mean the
            # nightly tested nothing at all.
            lines.append(
                "No tests ran. The suite collected nothing — check the pytest "
                "invocation and the stack name."
            )
        shown = [failure for failure in report.detail if failure.kind != "SKIPPED"] or [
            failure for failure in report.detail if failure.kind == "SKIPPED"
        ]
        if shown:
            lines.append("")
            for failure in shown[:_MAX_FAILURES_LISTED]:
                lines.append(f"{failure.kind} {failure.name}")
                if failure.message:
                    lines.append(f"    {failure.message}")
            remaining = len(shown) - _MAX_FAILURES_LISTED
            if remaining > 0:
                lines.append(f"… and {remaining} more — see the JUnit report in the artifacts.")

    if not ok:
        lines.append("")
        # Plain text, no backticks or asterisks: a message composed in Slack's
        # Workflow Builder renders those literally rather than as formatting.
        lines.append(
            "The nightly tests an existing stack and does not deploy one, so a "
            "failure is either a regression on develop or drift in the stack "
            "itself. Triage with the JUnit report first, then the job log."
        )

    body = "\n".join(lines).strip()
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "\n… (truncated — full report in the job artifacts)"
    return body


def build_payload(
    report: _Report,
    *,
    status: str,
    job_url: str,
    commit: str,
    stack: str | None = None,
    region: str | None = None,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> dict[str, str]:
    """Build the flat, all-string object a Workflow Builder trigger accepts."""
    normalised = (status or "").strip().lower()
    if normalised == "success":
        headline, ok = "✅ PASSED", True
    elif normalised in ("canceled", "cancelled"):
        headline, ok = "⚠️ CANCELED", False
    else:
        headline, ok = "❌ FAILED", False

    # A job that reports success while its report lists failures means the two
    # disagree; say so rather than posting a green headline over red detail.
    if ok and not report.problem and (report.failures or report.errors):
        headline = "⚠️ PASSED (report lists failures)"

    return {
        "status": headline,
        "commit": (commit or "unknown")[:8],
        "job_url": job_url or "",
        "summary": build_summary(report, stack=stack, region=region, ok=ok, max_chars=max_chars),
    }


def main(argv: list[str] | None = None) -> int:
    """Print the notification payload as JSON; returns a process exit code."""
    args_parser = argparse.ArgumentParser(description="Summarise a JUnit report for chat.")
    args_parser.add_argument(
        "--report",
        default="integ-tests-report.xml",
        help="Path to the pytest JUnit XML (default: integ-tests-report.xml).",
    )
    args_parser.add_argument(
        "--status",
        default="",
        help="Job status, e.g. $CI_JOB_STATUS. Anything but 'success' reads as failed.",
    )
    args_parser.add_argument("--job-url", default="", help="Job URL, e.g. $CI_JOB_URL.")
    args_parser.add_argument("--commit", default="", help="Commit SHA, e.g. $CI_COMMIT_SHA.")
    args_parser.add_argument("--stack", default=None, help="Target stack name, for the summary.")
    args_parser.add_argument("--region", default=None, help="Target region, for the summary.")
    args_parser.add_argument(
        "--max-chars",
        type=int,
        default=_DEFAULT_MAX_CHARS,
        help=f"Cap on the summary (default: {_DEFAULT_MAX_CHARS}).",
    )
    args = args_parser.parse_args(argv)

    payload = build_payload(
        parse_report(Path(args.report)),
        status=args.status,
        job_url=args.job_url,
        commit=args.commit,
        stack=args.stack,
        region=args.region,
        max_chars=args.max_chars,
    )
    json.dump(payload, sys.stdout)
    # The same thing in readable form, on stderr so it cannot contaminate the
    # payload being piped to curl. This is what makes the job log end with the
    # run's result rather than with pytest's last line, including on the runs
    # where no webhook is configured and nothing is posted anywhere.
    print(f"{payload['status']}\n{payload['summary']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
