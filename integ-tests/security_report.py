# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The access-check report the live security tests write, one line per check.

test_role_access.py and test_security_access.py record every call they make and
what came back. At the end of a pytest session that recorded anything,
conftest.py writes two files to ``scratch/api-test-results/`` at the repository
root (or ``$LMA_SECURITY_REPORT_DIR``):

* ``report.json``: every check, with its suite, subject, caller, expected and
  observed outcome, and whether they agree;
* ``report.md``: the counts per suite, then every check that did not pass.

``scratch/`` is ignored by git. The report names the stack and the calls made,
so it is a local file: it is not attached to a pull request or a CI job, and in
CI (``CI`` set) a failed test's assertion message gives counts only
(``describe_failures``), because the scheduled run's log, JUnit report and Slack
summary are kept and shared.

Outcomes: ``pass`` (observed what was expected), ``fail`` (observed something
else) and ``not-run`` (the condition the check needs is absent on this stack,
with the reason). A ``not-run`` is not a pass.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = REPO_ROOT / "scratch" / "api-test-results"

PASS = "pass"
FAIL = "fail"
NOT_RUN = "not-run"


@dataclass
class Check:
    suite: str
    subject: str
    caller: str
    expected: str
    observed: str
    outcome: str
    detail: str = ""


@dataclass
class Report:
    """The checks of one session. Thread-safe so a probe may record from a worker."""

    stack: str = ""
    region: str = ""
    checks: list[Check] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record(
        self,
        suite: str,
        subject: str,
        caller: str,
        expected: str,
        observed: str,
        detail: str = "",
    ) -> Check:
        """Record one check; it passes when ``observed`` equals ``expected``."""
        check = Check(suite, subject, caller, expected, observed, PASS if observed == expected else FAIL, detail)
        with self._lock:
            self.checks.append(check)
        return check

    def not_run(self, suite: str, subject: str, caller: str, expected: str, reason: str) -> Check:
        check = Check(suite, subject, caller, expected, "", NOT_RUN, reason)
        with self._lock:
            self.checks.append(check)
        return check

    def failures(self, suite: str | None = None) -> list[Check]:
        return [c for c in self.checks if c.outcome == FAIL and (suite is None or c.suite == suite)]

    def counts(self) -> dict[str, dict[str, int]]:
        """``{suite: {"pass": n, "fail": n, "not-run": n}}``, in the order suites were first seen."""
        counts: dict[str, dict[str, int]] = {}
        for check in self.checks:
            suite = counts.setdefault(check.suite, {PASS: 0, FAIL: 0, NOT_RUN: 0})
            suite[check.outcome] += 1
        return counts

    def totals(self) -> dict[str, int]:
        totals = {PASS: 0, FAIL: 0, NOT_RUN: 0}
        for suite in self.counts().values():
            for outcome, number in suite.items():
                totals[outcome] += number
        return totals


REPORT = Report()


def describe_failures(checks: list[Check], limit: int = 40) -> str:
    """An assertion message: one line per failed check, at most ``limit`` lines.

    In CI (``CI`` set) only the number of failed checks per suite: the job log,
    the JUnit report and the Slack summary of the scheduled run are kept and
    shared, and what a failed check observed belongs in the local report only.
    """
    if os.environ.get("CI"):
        per_suite: dict[str, int] = {}
        for check in checks:
            per_suite[check.suite] = per_suite.get(check.suite, 0) + 1
        counts = ", ".join(f"{suite}: {n}" for suite, n in per_suite.items())
        return (
            f"{len(checks)} access checks failed ({counts}). Details are in the run's local report, "
            "scratch/api-test-results/report.md; rerun with make stacktest-rbac to read them."
        )
    lines = [
        f"{c.subject} as {c.caller}: expected {c.expected}, observed {c.observed}"
        + (f" ({c.detail[:160]})" if c.detail else "")
        for c in checks[:limit]
    ]
    if len(checks) > limit:
        lines.append(f"... and {len(checks) - limit} more; see scratch/api-test-results/report.md")
    return "\n".join(lines)


def to_json(report: Report, generated_at: datetime | None = None) -> dict:
    return {
        "stack": report.stack,
        "region": report.region,
        "generatedAt": (generated_at or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "totals": report.totals(),
        "suites": report.counts(),
        "checks": [asdict(c) for c in report.checks],
    }


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def to_markdown(report: Report, generated_at: datetime | None = None) -> str:
    when = (generated_at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    totals = report.totals()
    lines = [
        "# LMA live access checks",
        "",
        f"Stack `{report.stack or '?'}` in `{report.region or '?'}`, {when}.",
        "",
        f"{totals[PASS]} passed, {totals[FAIL]} failed, {totals[NOT_RUN]} not run.",
        "",
        "| Suite | Passed | Failed | Not run |",
        "|---|---|---|---|",
    ]
    for suite, counts in report.counts().items():
        lines.append(f"| {_cell(suite)} | {counts[PASS]} | {counts[FAIL]} | {counts[NOT_RUN]} |")
    for title, outcome in (("Failed checks", FAIL), ("Checks not run", NOT_RUN)):
        rows = [c for c in report.checks if c.outcome == outcome]
        if not rows:
            continue
        lines += ["", f"## {title}", "", "| Suite | Subject | Caller | Expected | Observed | Detail |", "|---|---|---|---|---|---|"]
        lines += [
            f"| {_cell(c.suite)} | {_cell(c.subject)} | {_cell(c.caller)} | {_cell(c.expected)} "
            f"| {_cell(c.observed)} | {_cell(c.detail[:300])} |"
            for c in rows
        ]
    return "\n".join(lines) + "\n"


def write(report: Report, directory: Path | None = None) -> tuple[Path, Path] | None:
    """Write report.json and report.md; nothing (None) when no check was recorded."""
    if not report.checks:
        return None
    target = Path(directory or os.environ.get("LMA_SECURITY_REPORT_DIR") or DEFAULT_DIR)
    target.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    json_path = target / "report.json"
    md_path = target / "report.md"
    json_path.write_text(json.dumps(to_json(report, now), indent=2) + "\n", encoding="utf-8")
    md_path.write_text(to_markdown(report, now), encoding="utf-8")
    return json_path, md_path
