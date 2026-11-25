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
``scratch/security-results/summary.md`` and ``summary.json``, with the commit the
tests ran from and, with a stack, the version its template description gives;
each step's output to ``<step>.log`` beside them. Exit status: 0 when every step
passed (a ZAP run that reported findings has passed: it is WARN-only), 1
otherwise.

With ``--snapshot`` (``SNAPSHOT=1``) it then writes the public-safe snapshot of
this run to ``security/test-results/<VERSION>/`` with curate_snapshot.py, from
this run's reports only: a step that was not run is recorded as not run, never
read from an earlier run's report. ``--replace`` overwrites an existing snapshot
of the same version.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import curate_snapshot  # noqa: E402

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
    started: float = 0.0  # wall clock, to tell this run's report from an earlier one

    @property
    def outcome(self) -> str:
        if self.status is None:
            return "not run"
        return "passed" if self.status == 0 else "failed"


def plan(stack: str | None, region: str | None, python: str, out_dir: Path = OUT_DIR) -> list[Step]:
    """The steps for this run, in order."""
    junit = out_dir / "static-roles.xml"
    steps = [
        Step("srt", ["make", "srt-setup", "srt-scan"], ".srt/"),
        Step(
            "static-roles",
            [python, "-m", "pytest", "-q", f"--junitxml={junit}", *STATIC_ROLE_TESTS],
        ),
    ]
    if stack:
        where = [f"STACK={stack}"] + ([f"REGION={region}"] if region else [])
        steps += [
            Step(
                "live-rbac",
                [
                    "make",
                    "stacktest-rbac",
                    *where,
                    # A skipped test records no check, so the snapshot reads skips from here.
                    f"RBAC_PYTEST_ARGS=--junitxml={out_dir / 'live-rbac.xml'}",
                ],
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


def stack_version(stack: str, region: str | None) -> str:
    """The version in the stack's template description, ``LMA (v<version>)``; empty if unread."""
    try:
        import boto3  # pylint: disable=import-outside-toplevel

        stacks = boto3.client("cloudformation", region_name=region).describe_stacks(StackName=stack)
        description = stacks["Stacks"][0].get("Description") or ""
    except Exception:  # noqa: BLE001 - the version is optional provenance
        return ""
    return version_of(description)


def version_of(description: str) -> str:
    match = re.search(r"\(v([0-9][0-9A-Za-z.+-]*)\)", description)
    return match.group(1) if match else ""


def snapshot_inputs(
    steps: list[Step], commit: str, out_dir: Path = OUT_DIR, root: Path = REPO_ROOT
) -> tuple[dict, dict, dict]:
    """Sources, commits and not-run reasons for a snapshot of this run's steps only."""
    reports = {
        "srt": root / ".srt" / "issues.json",
        "static-roles": out_dir / "static-roles.xml",
        "live-rbac": root / "scratch" / "api-test-results" / "report.json",
        "zap": root / "scratch" / "zap-reports" / "latest" / "summary.json",
    }
    ran = {s.name: s for s in steps if s.status is not None}
    sources: dict[str, Path | None] = {}
    not_run: dict[str, str] = {}
    for part in curate_snapshot.PARTS:
        report = reports[part]
        if part not in ran:
            sources[part] = None
            not_run[part] = "no stack was given" if part in ("live-rbac", "zap") else "not run"
        elif report.is_file() and report.stat().st_mtime >= ran[part].started:
            sources[part] = report
        else:
            # A step that stopped early can leave an earlier run's report in place.
            sources[part] = None
            not_run[part] = "the step wrote no report in this run"
    commits = {part: commit for part in ran} if commit else {}
    return sources, commits, not_run


def fresh_live_junit(steps: list[Step], out_dir: Path = OUT_DIR) -> Path | None:
    """The live tests' JUnit file, if this run's live-rbac step wrote it."""
    junit = out_dir / "live-rbac.xml"
    step = next((s for s in steps if s.name == "live-rbac" and s.status is not None), None)
    if step is None or not junit.is_file() or junit.stat().st_mtime < step.started:
        return None
    return junit


def run(steps: list[Step], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for step in steps:
        print(f"== {step.name}: {' '.join(step.command)}", flush=True)
        started = time.monotonic()
        step.started = time.time()
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
    parser.add_argument(
        "--snapshot", action="store_true", help="then write security/test-results/<VERSION>/"
    )
    parser.add_argument("--replace", action="store_true", help="overwrite that version's snapshot")
    parser.add_argument(
        "--allow-failures",
        action="store_true",
        help="write a snapshot that records failures (only once they are handled)",
    )
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir)
    steps = plan(args.stack_name, args.region, args.python, out_dir)
    commit = curate_snapshot.head_commit()
    run(steps, out_dir)
    version = stack_version(args.stack_name, args.region) if args.stack_name else ""
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "generatedAt": when,
                "stack": args.stack_name,
                "commit": commit,
                "stackVersion": version,
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
    status = 0 if all(s.status == 0 for s in steps) else 1
    if args.snapshot:
        sources, commits, not_run = snapshot_inputs(steps, commit, out_dir)
        snapshot, forbidden = curate_snapshot.build(
            sources,
            version=curate_snapshot.repo_version(),
            date=when[:10],
            stack_version=version,
            commits=commits,
            not_run=not_run,
            suppressions=REPO_ROOT / ".srt" / "suppressions.json",
            live_junit=fresh_live_junit(steps, out_dir),
        )
        try:
            target = curate_snapshot.write(
                snapshot,
                forbidden,
                curate_snapshot.SNAPSHOT_ROOT,
                args.replace,
                args.allow_failures,
            )
        except (
            curate_snapshot.UnsafeSnapshot,
            curate_snapshot.UnhandledFailures,
            FileExistsError,
        ) as err:
            print(err, file=sys.stderr)
            return 1
        print(f"Snapshot: {target}/summary.md. Read it line by line before committing it.")
    return status


if __name__ == "__main__":
    sys.exit(main())
