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

# The triage agent's report gets its own budget on top of the body's, so a
# verbose agent cannot crowd out the test results it is commenting on. Both
# budgets together have to stay inside Slack's own 4000-character message cap,
# which the whole post is rejected for exceeding.
_MAX_AGENT_CHARS = 1200

# Why a publish or deploy failed (see failure_cause.py). Inside the body's own
# budget rather than on top of it: for those stages it is most of the message.
_MAX_CAUSE_CHARS = 1400

# Above this many distinct checks, a module is summarised by its count rather
# than listed. The live suite sits well under it and is listed in full; the
# pipeline's own plumbing tests are numerous, uniform and not what anyone reads
# this notification for.
_MAX_CASES_PER_MODULE = 16

# The nightly builds the branch, deploys it, then tests it, and each of those is
# a separate job so that each gets its own freshly vended AWS credentials. Any
# of them can be the one that failed, and the notification has to say which --
# a message about a missing JUnit report would describe the symptom of a failed
# deploy rather than the deploy.
_PUBLISH_STAGE = "publish"
_DEPLOY_STAGE = "deploy"
_TESTS_STAGE = "tests"
STAGES = (_PUBLISH_STAGE, _DEPLOY_STAGE, _TESTS_STAGE)

# Where to look first, per stage. Plain text, no backticks or asterisks: a
# message composed in Slack's Workflow Builder renders those literally.
_STAGE_TRIAGE = {
    _PUBLISH_STAGE: (
        "The build of the branch failed before anything was deployed, so the "
        "stack still holds the previous build and is unchanged. This is a build "
        "failure, not a test failure: read the job log, not the stack."
    ),
    _DEPLOY_STAGE: (
        "The branch built but CloudFormation did not accept it. A rollback left "
        "unfinished is resumed by the next nightly deploy before it deploys; a "
        "failure that repeats is the branch, or a stack that needs a person."
    ),
}


@dataclass
class _Failure:
    name: str
    kind: str  # "FAILED", "ERROR" or "SKIPPED"
    message: str


@dataclass
class _Case:
    """One test and how it ended, in the order pytest ran it."""

    name: str
    outcome: str  # "passed", "failed", "error" or "skipped"
    module: str = ""
    duration: float = 0.0

    @property
    def icon(self) -> str:
        return {"passed": "✓", "failed": "✗", "error": "✗", "skipped": "⏭"}.get(
            self.outcome, "?"
        )

    @property
    def base_name(self) -> str:
        """The test's name without its ``test_`` prefix or parametrisation.

        ``required_output_present[LMAWebsocketEndpoint]`` and its six siblings
        are one check applied to seven values; listing them separately spends
        seven lines of a capped message saying one thing.
        """
        name = self.name[5:] if self.name.startswith("test_") else self.name
        bracket = name.find("[")
        return name[:bracket] if bracket != -1 else name


@dataclass
class _Report:
    """What the JUnit XML says, or why it could not be read."""

    total: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    duration: float = 0.0
    detail: list[_Failure] = field(default_factory=list)
    cases: list[_Case] = field(default_factory=list)
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


def _capped(text: str, limit: int) -> str:
    """Trim to a budget while keeping the line structure.

    Unlike ``_first_lines`` this preserves newlines, because it is used on
    prose written to be read as lines rather than on a traceback being reduced
    to one identifying string. Cuts at the last line break inside the budget so
    the result never ends mid-sentence.
    """
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind("\n")
    if boundary > limit // 2:
        cut = cut[:boundary]
    return cut.rstrip() + "\n… (truncated — the full report is in the job artifacts)"


def _read_optional(path: str | None) -> str | None:
    """Read a file that may legitimately not be there.

    The triage agent is advisory: it is opt-in, it can be disabled, and it
    returns nothing when it cannot finish. All three cases have to leave the
    notification intact rather than turning an informative failure report into
    a traceback from the thing reporting it.
    """
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


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
            name = case.get("name") or "(unnamed test)"
            outcome = "passed"
            for tag, kind in (("failure", "FAILED"), ("error", "ERROR"), ("skipped", "SKIPPED")):
                for element in case.findall(tag):
                    outcome = kind.lower()
                    report.detail.append(
                        _Failure(
                            name=name,
                            kind=kind,
                            message=_first_lines(_text_of(element)),
                        )
                    )
            # Every case, not just the interesting ones: a reader wants to see
            # WHICH checks ran, and a count alone cannot show that the one test
            # they care about was among them.
            report.cases.append(
                _Case(
                    name=name,
                    outcome=outcome,
                    module=case.get("classname") or "",
                    duration=float(case.get("time") or 0.0),
                )
            )
    return report


def _case_lines(report: _Report) -> list[str]:
    """What the run actually checked, grouped by test module.

    The counts say how the run went; this says what it covered, which is the
    thing a reader cannot reconstruct from counts and the thing they need in
    order to notice that an expected check is missing entirely.

    Grouped by module because the modules mean different things — one is the
    live end-to-end suite, another is the pipeline's own plumbing — and a flat
    list of forty names buries that. Parametrised cases collapse to one line
    with a count, and a module whose every test passed collapses to a single
    line: the detail that earns its place in a capped message is the detail
    that is not uniform.
    """
    if not report.cases:
        return []

    by_module: dict[str, list[_Case]] = {}
    for case in report.cases:
        by_module.setdefault(case.module or "tests", []).append(case)

    # Listed-in-full modules first: the live suite is what this notification is
    # for, and a reader should not have to scroll past the plumbing to find it.
    def detail_first(item: tuple[str, list[_Case]]) -> tuple[int, str]:
        module, cases = item
        distinct = len({c.base_name for c in cases})
        return (1 if distinct > _MAX_CASES_PER_MODULE else 0, module)

    lines: list[str] = []
    for module, cases in sorted(by_module.items(), key=detail_first):
        label = module.rsplit(".", 1)[-1] or module
        bad = [c for c in cases if c.outcome in ("failed", "error")]
        skipped = [c for c in cases if c.outcome == "skipped"]

        # Collapse parametrisation: one entry per distinct check, carrying the
        # worst outcome among its variants and how many there were.
        checks: dict[str, _Case] = {}
        counts: dict[str, int] = {}
        for case in cases:
            counts[case.base_name] = counts.get(case.base_name, 0) + 1
            kept = checks.get(case.base_name)
            if kept is None or (kept.outcome == "passed" and case.outcome != "passed"):
                checks[case.base_name] = case

        passed = len(cases) - len(bad) - len(skipped)
        tally = (
            f"all {len(cases)} passed" if not bad and not skipped
            else f"{passed}/{len(cases)} passed"
        )

        if lines:
            lines.append("")

        if len(checks) > _MAX_CASES_PER_MODULE:
            # Too many distinct checks to read in a chat message. Give the
            # tally, and name whatever did not pass — the part that cannot be
            # summarised away without losing the point of the message.
            lines.append(f"{label} — {tally}")
            for name, case in checks.items():
                if case.outcome != "passed":
                    suffix = f" (×{counts[name]})" if counts[name] > 1 else ""
                    lines.append(f"• {name} {case.icon}{suffix}")
            continue

        lines.append(f"{label} — {tally}:")
        for name, case in checks.items():
            suffix = f" (×{counts[name]})" if counts[name] > 1 else ""
            lines.append(f"• {name} {case.icon}{suffix}")
    return lines


def build_summary(
    report: _Report,
    *,
    stack: str | None = None,
    region: str | None = None,
    ok: bool = True,
    stage: str = _TESTS_STAGE,
    template_url: str | None = None,
    agent_report: str | None = None,
    cause: str | None = None,
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str:
    """Compose the human-readable body of the notification."""
    where = stack or "(stack not named)"
    if region:
        where = f"{where} ({region})"

    lines: list[str] = []
    if stage != _TESTS_STAGE:
        # A stage before the tests failed, so there is no report to summarise
        # and its absence is not the news. Name the stage that failed instead:
        # "no results to report" would read as a broken notification rather
        # than as a build that never got as far as testing.
        lines.append(f"Stack {where} — the {stage} stage failed, so no tests ran.")
        if cause and cause.strip():
            # The reason first: it is what decides who acts and how, and the
            # stage's general guidance below is the same every night.
            lines.append("")
            lines.append(_capped(cause.strip(), _MAX_CAUSE_CHARS))
        lines.append("")
        lines.append(_STAGE_TRIAGE.get(stage, _STAGE_TRIAGE[_DEPLOY_STAGE]))
        body = "\n".join(lines).strip()
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "\n… (truncated — see the job log)"
        return body

    if report.problem:
        lines.append(f"Stack {where} — no results to report.")
        lines.append(report.problem)
    else:
        lines.append(
            f"Stack {where} — {report.passed} passed, "
            f"{report.failures + report.errors} failed, "
            f"{report.skipped} skipped, in {report.duration:.0f}s"
        )
        if template_url:
            # What was actually deployed and tested. Worth a line because the
            # nightly builds and deploys the commit before testing it, so "which
            # artifacts" is a real question with a real answer.
            lines.append(f"Deployed: {template_url}")
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

        case_lines = _case_lines(report)
        if case_lines:
            # No heading: the counts line above already says how it went, and
            # each module names its own tally.
            lines.append("")
            lines.extend(case_lines)

    if not ok:
        lines.append("")
        # Plain text, no backticks or asterisks: a message composed in Slack's
        # Workflow Builder renders those literally rather than as formatting.
        lines.append(
            "The stack was built and deployed from this commit before the tests "
            "ran, so a failure here is the branch's behaviour rather than a "
            "stale deployment. Triage with the JUnit report first, then the job "
            "log."
        )

    body = "\n".join(lines).strip()

    # The triage agent's reading of the failure, if it ran. Appended AFTER the
    # deterministic body is already within budget, and given its own slice of
    # the message, so a long agent report can never push the test results out
    # of the notification: the facts outrank the interpretation of them.
    if agent_report and agent_report.strip():
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "\n… (truncated — full report in the job artifacts)"
        head = _capped(agent_report.strip(), _MAX_AGENT_CHARS)
        return f"{body}\n\n🤖 Triage:\n{head}"

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
    stage: str = _TESTS_STAGE,
    template_url: str | None = None,
    agent_report: str | None = None,
    cause: str | None = None,
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
        "summary": build_summary(
            report,
            stack=stack,
            region=region,
            ok=ok,
            stage=stage,
            template_url=template_url,
            agent_report=agent_report,
            cause=cause,
            max_chars=max_chars,
        ),
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
        "--stage",
        default=_TESTS_STAGE,
        choices=STAGES,
        help=(
            "Which stage of the nightly is reporting. Anything but "
            f"'{_TESTS_STAGE}' means the run failed before the tests, and the "
            "summary names that stage instead of summarising a report that was "
            f"never written (default: {_TESTS_STAGE})."
        ),
    )
    args_parser.add_argument(
        "--template-url",
        default=None,
        help="The template this run deployed and tested, shown in the summary.",
    )
    args_parser.add_argument(
        "--agent-report",
        default=None,
        help=(
            "Path to the triage agent's report. Appended under a 'Triage' "
            "heading when the file exists and is non-empty; a missing file is "
            "not an error, because the agent is advisory and may not have run."
        ),
    )
    args_parser.add_argument(
        "--cause",
        default=None,
        help=(
            "Path to the reason a publish or deploy failed, from "
            "failure_cause.py. Shown under the failed stage when the file exists "
            "and is non-empty; ignored for the tests stage, which has its report."
        ),
    )
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
        stage=args.stage,
        template_url=args.template_url,
        agent_report=_read_optional(args.agent_report),
        cause=_read_optional(args.cause),
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
