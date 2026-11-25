#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run LMA's security tests as a set and write one summary (``make security-results``).

Offline, always:

``srt``
    the Sample Security Review Tool (``make srt-setup srt-scan``): static
    analysis and dependency checks. See docs/security-scanning.md.
``static-roles``
    the offline role checks: the role matrix against the schema, the Cognito
    template and the docs (lma-ai-stack/test/test_role_matrix.py), the AppSync
    contract and SharedWith tests, and the unit tests of the live tests' helpers.

With ``--stack-name`` (``STACK=``), also against that deployed stack:

``live-rbac``
    ``make stacktest-rbac``: integ-tests/test_role_access.py and
    test_security_access.py. Report: scratch/api-test-results/report.md.
``zap``
    ``make stacktest-zap`` (passive): scripts/security/zap_scan.py. WARN-only.
    Report: scratch/zap-reports/latest/summary.md.

Every step runs even when an earlier one fails. The summary goes to
``scratch/security-results/summary.md`` and ``summary.json``; each step's output
to ``<step>.log`` beside them. Exit status: 0 when every step passed (a ZAP run
that reported findings has passed: it is WARN-only), 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "scratch" / "security-results"

STATIC_ROLE_TESTS = (
    "lma-ai-stack/test/test_role_matrix.py",
    "lma-ai-stack/test/test_appsync_contract.py",
    "lma-ai-stack/test/test_shared_with_access.py",
    "integ-tests/test_security_helpers.py",
    "scripts/security/tests",
)


@dataclass
class Step:
    name: str
    command: list[str]
    report: str = ""
    status: int | None = None
    seconds: float = 0.0
    detail: str = ""

    @property
    def outcome(self) -> str:
        if self.status is None:
            return "not run"
        return "passed" if self.status == 0 else "failed"


def plan(stack: str | None, region: str | None, python: str) -> list[Step]:
    """The steps for this run, in order."""
    steps = [
        Step("srt", ["make", "srt-setup", "srt-scan"], ".srt/"),
        Step("static-roles", [python, "-m", "pytest", "-q", *STATIC_ROLE_TESTS]),
    ]
    if stack:
        where = [f"STACK={stack}"] + ([f"REGION={region}"] if region else [])
        steps += [
            Step(
                "live-rbac",
                ["make", "stacktest-rbac", *where],
                "scratch/api-test-results/report.md",
            ),
            Step("zap", ["make", "stacktest-zap", *where], "scratch/zap-reports/latest/summary.md"),
        ]
    return steps


def read_counts(step: Step, root: Path = REPO_ROOT) -> str:
    """A one-line result from the step's own report, when it wrote one."""
    try:
        if step.name == "live-rbac":
            totals = json.loads((root / "scratch/api-test-results/report.json").read_text())[
                "totals"
            ]
            return f"{totals['pass']} checks passed, {totals['fail']} failed, {totals['not-run']} not run"
        if step.name == "zap":
            summary = json.loads((root / "scratch/zap-reports/latest/summary.json").read_text())
            parts = []
            for result in summary["results"]:
                counts = result.get("counts") or {}
                if result.get("ran"):
                    parts.append(
                        f"{result['target']}: High {counts.get('High', 0)}, Medium {counts.get('Medium', 0)}, "
                        f"Low {counts.get('Low', 0)}"
                    )
                else:
                    parts.append(f"{result['target']}: not run")
            return "; ".join(parts)
    except (OSError, KeyError, ValueError):
        return ""
    return ""


def to_markdown(steps: list[Step], when: str, stack: str | None) -> str:
    lines = [
        "# LMA security results",
        "",
        f"{when}; " + (f"stack `{stack}`." if stack else "offline only (no STACK given)."),
        "",
        "| Step | Outcome | Seconds | Result | Report |",
        "|---|---|---|---|---|",
    ]
    for s in steps:
        lines.append(f"| {s.name} | {s.outcome} | {s.seconds:.0f} | {s.detail} | {s.report} |")
    return "\n".join(lines) + "\n"


def run(steps: list[Step], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for step in steps:
        print(f"== {step.name}: {' '.join(step.command)}", flush=True)
        started = time.monotonic()
        with open(out_dir / f"{step.name}.log", "w", encoding="utf-8") as log:
            done = subprocess.run(  # noqa: S603 - fixed commands
                step.command, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT, check=False
            )
        step.status = done.returncode
        step.seconds = time.monotonic() - started
        step.detail = read_counts(step)
        print(f"   {step.outcome} in {step.seconds:.0f} s {step.detail}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--stack-name", default=os.environ.get("STACK") or None)
    parser.add_argument("--region", default=os.environ.get("REGION") or None)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    args = parser.parse_args(argv)
    steps = plan(args.stack_name, args.region, args.python)
    out_dir = Path(args.out_dir)
    run(steps, out_dir)
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "generatedAt": when,
                "stack": args.stack_name,
                "steps": [asdict(s) | {"outcome": s.outcome} for s in steps],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    markdown = to_markdown(steps, when, args.stack_name)
    (out_dir / "summary.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0 if all(s.status == 0 for s in steps) else 1


if __name__ == "__main__":
    sys.exit(main())
