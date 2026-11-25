#!/usr/bin/env python3
# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Write a public-safe snapshot of LMA's security test results (``make security-snapshot``).

The snapshot goes to ``security/test-results/<version>/`` (``summary.md`` and
``summary.json``), one folder per release, and is committed. It holds counts
only:

* SRT: findings by priority and status, and how many are suppressed with a
  recorded decision;
* the static role checks: tests passed, failed, errored and skipped;
* the live access tests: checks passed, failed and not run per suite, and the
  reason given for each check not run;
* ZAP: the rule tally (PASS, WARN, FAIL, IGNORE, INFO) and the alert counts by
  risk per target, and the scan mode;
* the release version, the date, the stack's version and the commits the tests
  ran from, where known; and what was not run or is not tested.

It never holds an alert name, a finding's text, a check's subject or observed
answer, a URL, a hostname, an ARN, an account id or a stack name. The fields
above are the only ones read from the raw reports, and ``assert_public_safe``
then refuses the whole snapshot, writing nothing, when its text matches a
pattern of those identifiers or contains any such string taken from the raw
reports themselves (alert names, URLs, finding text, check subjects, the stack
name).

A part with no report is written as *not run*, never left out. Each part says
when it was measured, because a snapshot may be curated from runs made on
different days.

Usage::

    python3 scripts/security/curate_snapshot.py                # from the newest local reports
    python3 scripts/security/curate_snapshot.py --stack-version 0.4.1 --commit live-rbac=<sha>
    make security-results STACK=<name> REGION=<region> SNAPSHOT=1   # run everything, then this

See docs/security-testing.md (Release snapshots) and
.claude/skills/curate-security-results.md.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET  # nosec B405 - parses a JUnit file pytest wrote in this checkout
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ROOT = REPO_ROOT / "security" / "test-results"
SCHEMA_VERSION = 1

PARTS = ("srt", "static-roles", "live-rbac", "zap")
PART_TITLES = {
    "srt": "SRT (static analysis, dependencies, infrastructure as code)",
    "static-roles": "Static role checks",
    "live-rbac": "Live access tests",
    "zap": "ZAP DAST",
}
DEFAULTS = {
    "srt": REPO_ROOT / ".srt" / "issues.json",
    "suppressions": REPO_ROOT / ".srt" / "suppressions.json",
    "static-roles": REPO_ROOT / "scratch" / "security-results" / "static-roles.xml",
    "live-rbac": REPO_ROOT / "scratch" / "api-test-results" / "report.json",
    "live-junit": REPO_ROOT / "scratch" / "security-results" / "live-rbac.xml",
    "zap": REPO_ROOT / "scratch" / "zap-reports" / "latest" / "summary.json",
}
SRT_PRIORITIES = ("Critical", "High", "Medium", "Low", "Info")
SRT_STATUSES = ("Open", "reopened", "suppressed", "resolved")
ZAP_RISKS = ("High", "Medium", "Low", "Informational")
ZAP_TALLY = {
    "PASS": ("PASS",),
    "WARN": ("WARN-NEW", "WARN-INPROG"),
    "FAIL": ("FAIL-NEW", "FAIL-INPROG"),
    "IGNORE": ("IGNORE",),
    "INFO": ("INFO",),
}
REASON_MAX = 200

#: The tests' own scope limits: keep in step with "What these tests do not exercise"
#: in docs/security-testing.md (a test holds each item to that section).
NOT_EXERCISED = (
    "GraphQL subscriptions are held to the role matrix by the schema contract test only; "
    "no live test opens one.",
    "The ASR MicroVM sessions, the Virtual Participant launch state machine and the MicroVM "
    "viewer tokens are reached only through the operations the live tests call.",
    "The browser extension and the Desktop Capture App are covered by their own unit tests, "
    "not by these tests.",
    "Token revocation after sign-out, TLS protocol versions and rate limits are not tested.",
    "Recording links are held to their prefix scope, not to a per-meeting decision.",
    "ZAP sends HTTP requests only; it does not scan the WebSocket transcriber.",
)


class UnsafeSnapshot(ValueError):
    """The snapshot's text holds something that must not be published."""


# ── the public-safety guard ────────────────────────────────────────────────

#: (what it is, pattern). Matched case-insensitively against the whole snapshot.
UNSAFE_PATTERNS: tuple[tuple[str, re.Pattern], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE))
    for label, pattern in (
        ("a URL", r"\b[a-z][a-z0-9+.-]*://"),
        ("an ARN", r"\barn:aws[a-z-]*:"),
        ("an account-id-shaped number", r"(?<![0-9a-f.])\d{12}(?![0-9a-f])"),
        ("an access key id", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        ("a Cognito pool id", r"\b[a-z]{2}(?:-gov)?-[a-z]+-\d_[a-z0-9]{6,}\b"),
        (
            "a hostname",
            r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|aws|dev|amazon|invalid|internal|local|cloud)\b",
        ),
        ("an IPv4 address", r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])"),
        ("an email address", r"[\w.+-]+@[\w-]+\.[\w.-]+"),
        ("a local path", r"(?:/home/|/Users/|/tmp/|/private/|[A-Z]:\\)"),
        ("a scanner rule id", r"\b(?:CKV2?_[A-Z0-9_]+|B[1-7]\d\d|plugin\s*(?:id)?\s*\d+)\b"),
        (
            "ZAP alert wording",
            r"(?:header (?:not set|missing)|missing or invalid|\bcsp:|unsafe-(?:inline|eval)"
            r"|cross-domain|server leaks|information disclosure|cookie (?:no|without)"
            r"|x-[a-z-]+ header)",
        ),
    )
)
#: Shorter strings from the raw reports are too likely to be ordinary words.
LITERAL_MIN = 6
#: A check's subject or observed answer shorter than this is a generic word.
SUBJECT_MIN = 12


def assert_public_safe(text: str, forbidden: set[str] | frozenset[str] = frozenset()) -> None:
    """Raise ``UnsafeSnapshot`` when ``text`` matches an identifier pattern or holds a forbidden string.

    ``forbidden`` is what the raw reports name and the snapshot must not repeat:
    alert names, URLs and hosts, finding text, check subjects, the stack name.
    The message names the kind of problem only, never the matched text, so that
    the refusal itself can be printed in a shared log.
    """
    problems = [label for label, pattern in UNSAFE_PATTERNS if pattern.search(text)]
    lowered = text.lower()
    hits = sum(1 for s in forbidden if len(s) >= LITERAL_MIN and s.lower() in lowered)
    if hits:
        problems.append(f"{hits} string(s) taken from the raw reports")
    if problems:
        raise UnsafeSnapshot("refusing to write the snapshot: it contains " + "; ".join(problems))


# ── reading each raw report ────────────────────────────────────────────────


def _date(value: str | None) -> str:
    """The date part of an ISO timestamp, or ``not recorded``."""
    if not value:
        return "not recorded"
    match = re.match(r"\d{4}-\d{2}-\d{2}", str(value))
    return match.group(0) if match else "not recorded"


def _mtime_date(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date().isoformat()


def read_srt(issues: Path, suppressions: Path | None, forbidden: set[str]) -> dict:
    """Findings by priority and status from SRT's ``issues.json``."""
    data = json.loads(issues.read_text(encoding="utf-8"))
    items = data if isinstance(data, list) else data.get("issues") or []
    table: dict[str, Counter] = {}
    with_reason = 0
    for item in items:
        priority = str(item.get("priority") or item.get("severity") or "Info").capitalize()
        if priority not in SRT_PRIORITIES:
            priority = "Info"
        status = str(item.get("status") or "Open")
        status = {"open": "Open"}.get(status.lower(), status.lower())
        if status not in SRT_STATUSES:
            status = "Open"
        table.setdefault(priority, Counter())[status] += 1
        if status == "suppressed" and str(item.get("suppressionReason") or "").strip():
            with_reason += 1
        # A finding's own words. Paths are not secret, and the snapshot names none.
        for key in ("issue", "fix"):
            if item.get(key):
                forbidden.add(str(item[key]))
    decisions = None
    if suppressions and suppressions.is_file():
        decisions = len(json.loads(suppressions.read_text(encoding="utf-8")))
    total = Counter()
    for row in table.values():
        total.update(row)
    return {
        "measured": _mtime_date(issues),
        "byPriority": {
            p: {s: table.get(p, Counter())[s] for s in SRT_STATUSES} for p in SRT_PRIORITIES
        },
        "total": {s: total[s] for s in SRT_STATUSES},
        "suppressedWithRecordedDecision": with_reason,
        "suppressedWithoutReason": total["suppressed"] - with_reason,
        "trackedDecisions": decisions,
    }


def read_static(junit: Path) -> dict:
    """Totals from the JUnit file pytest wrote for the static role checks."""
    root = ET.parse(junit).getroot()  # nosec B314 - a file pytest wrote in this checkout
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = Counter()
    stamp = None
    for suite in suites:
        for key in ("tests", "failures", "errors", "skipped"):
            totals[key] += int(suite.get(key) or 0)
        stamp = stamp or suite.get("timestamp")
    failed = totals["failures"] + totals["errors"]
    return {
        "measured": _date(stamp) if stamp else _mtime_date(junit),
        "passed": totals["tests"] - failed - totals["skipped"],
        "failed": totals["failures"],
        "errors": totals["errors"],
        "skipped": totals["skipped"],
        "outcome": "passed" if failed == 0 and totals["tests"] > 0 else "failed",
    }


def read_skips(junit: Path) -> dict:
    """Tests run and skipped, with each skip's reason, from the live tests' JUnit file.

    A test pytest skips records no check, so ``report.json`` cannot show it.
    """
    root = ET.parse(junit).getroot()  # nosec B314 - a file pytest wrote in this checkout
    reasons: Counter = Counter()
    tests = failed = 0
    for case in root.iter("testcase"):
        tests += 1
        if case.find("failure") is not None or case.find("error") is not None:
            failed += 1
        skipped = case.find("skipped")
        if skipped is not None:
            reason = " ".join(str(skipped.get("message") or "no reason recorded").split())
            reasons[reason.removeprefix("Skipped: ")[:REASON_MAX]] += 1
    return {
        "tests": tests,
        "failed": failed,
        "skipped": [{"reason": r, "tests": n} for r, n in sorted(reasons.items())],
    }


#: report.json is written when the pytest session ends, the JUnit file just after.
SAME_RUN_SECONDS = 3600


def same_run(report: Path, junit: Path | None) -> bool:
    """Whether ``junit`` exists and was written by the run that wrote ``report``."""
    if junit is None or not junit.is_file():
        return False
    return abs(junit.stat().st_mtime - report.stat().st_mtime) <= SAME_RUN_SECONDS


def read_live(report: Path, forbidden: set[str], junit: Path | None = None) -> dict:
    """Checks per suite, and the reasons checks were not run, from the live tests' ``report.json``.

    With the run's JUnit file, also the tests pytest skipped (``tests``); without
    it, ``tests`` is None and the snapshot says that skips were not recorded.
    """
    data = json.loads(report.read_text(encoding="utf-8"))
    for key in ("stack", "region"):
        if data.get(key):
            forbidden.add(str(data[key]))
    reasons: Counter = Counter()
    for check in data.get("checks") or []:
        # What was called, and what a failed check saw; role names are not secret.
        keys = ("subject", "observed", "detail") if check.get("outcome") == "fail" else ("subject",)
        for key in keys:
            if len(str(check.get(key) or "")) >= SUBJECT_MIN:
                forbidden.add(str(check[key]))
        if check.get("outcome") == "not-run":
            reason = " ".join(str(check.get("detail") or "no reason recorded").split())
            reasons[(str(check.get("suite") or ""), reason[:REASON_MAX])] += 1
    suites = {
        name: {k: int(counts.get(k, 0)) for k in ("pass", "fail", "not-run")}
        for name, counts in (data.get("suites") or {}).items()
    }
    totals = data.get("totals") or {}
    return {
        "measured": _date(data.get("generatedAt")),
        "suites": suites,
        "totals": {k: int(totals.get(k, 0)) for k in ("pass", "fail", "not-run")},
        "notRunReasons": [
            {"suite": suite, "reason": reason, "checks": n}
            for (suite, reason), n in sorted(reasons.items())
        ],
        "tests": read_skips(junit) if same_run(report, junit) else None,
    }


def _zap_forbidden(summary: Path, data: dict, forbidden: set[str]) -> None:
    if data.get("stack"):
        forbidden.add(str(data["stack"]))
    for result in data.get("results") or []:
        for key in ("url", "error"):
            if result.get(key):
                forbidden.add(str(result[key]))
        for alert in result.get("alerts") or []:
            if alert.get("name"):
                forbidden.add(str(alert["name"]))
            forbidden.update(str(u) for u in alert.get("sampleUrls") or [])
    # ZAP's own reports beside the summary name every alert, even in a counts-only run.
    for report in summary.parent.glob("*-report.json"):
        try:
            raw = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for site in raw.get("site") or []:
            for key in ("@name", "@host"):
                if site.get(key):
                    forbidden.add(str(site[key]))
            for alert in site.get("alerts") or []:
                for key in ("alert", "name"):
                    if alert.get(key):
                        forbidden.add(str(alert[key]))


def read_zap(summary: Path, forbidden: set[str]) -> dict:
    """Rule tally and alert counts per target from ``zap_scan.py``'s ``summary.json``."""
    data = json.loads(summary.read_text(encoding="utf-8"))
    _zap_forbidden(summary, data, forbidden)
    targets = []
    for result in data.get("results") or []:
        row = {"target": str(result.get("target") or ""), "mode": str(result.get("mode") or "")}
        if result.get("ran"):
            tally = result.get("tally") or {}
            counts = result.get("counts") or {}
            row.update(
                ran=True,
                rules={
                    k: sum(int(tally.get(label, 0)) for label in labels)
                    for k, labels in ZAP_TALLY.items()
                },
                alerts={risk: int(counts.get(risk, 0)) for risk in ZAP_RISKS},
            )
        else:
            reason = " ".join(str(result.get("skipped") or "the scan wrote no report").split())
            row.update(ran=False, reason=reason[:REASON_MAX])
        targets.append(row)
    return {
        "measured": _date(data.get("generatedAt")),
        "mode": str(data.get("mode") or ""),
        "targets": targets,
    }


# ── building and writing ───────────────────────────────────────────────────


def repo_version(root: Path = REPO_ROOT) -> str:
    return (root / "VERSION").read_text(encoding="utf-8").strip()


def head_commit(root: Path = REPO_ROOT) -> str:
    """``HEAD``, marked ``(with local changes)`` when tracked files differ from it."""
    try:
        sha = subprocess.run(  # noqa: S603,S607 - fixed command
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        changed = subprocess.run(  # noqa: S603,S607 - fixed command
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
    return f"{sha} (with local changes)" if changed else sha


def build(
    sources: dict[str, Path | None],
    version: str,
    date: str,
    stack_version: str = "",
    commits: dict[str, str] | None = None,
    not_run: dict[str, str] | None = None,
    suppressions: Path | None = None,
    live_junit: Path | None = None,
) -> tuple[dict, set[str]]:
    """The snapshot as a dict, and the strings from the raw reports it must not contain.

    ``sources`` maps each part to its raw report; a part whose report is None or
    absent is recorded as not run, with the reason from ``not_run`` if given.
    """
    commits = commits or {}
    not_run = not_run or {}
    forbidden: set[str] = set()
    parts: dict[str, dict] = {}
    for part in PARTS:
        path = sources.get(part)
        if path is None or not Path(path).is_file():
            parts[part] = {"ran": False, "reason": not_run.get(part) or "no report from this part"}
            continue
        path = Path(path)
        if part == "srt":
            body = read_srt(path, suppressions, forbidden)
        elif part == "static-roles":
            body = read_static(path)
        elif part == "live-rbac":
            body = read_live(path, forbidden, live_junit)
        else:
            body = read_zap(path, forbidden)
        parts[part] = {"ran": True, "commit": commits.get(part) or "not recorded", **body}
    snapshot = {
        "schemaVersion": SCHEMA_VERSION,
        "version": version,
        "date": date,
        "stackVersion": stack_version or "not recorded",
        "parts": parts,
        "notExercised": list(NOT_EXERCISED),
    }
    return snapshot, forbidden


def _cell(value) -> str:
    return str(value).replace("|", "/").replace("\n", " ")


def to_markdown(snapshot: dict) -> str:  # noqa: C901 - one section per part
    p = snapshot["parts"]
    lines = [
        f"# LMA security test snapshot: {snapshot['version']}",
        "",
        "Counts only, curated by `scripts/security/curate_snapshot.py` from the local reports of "
        "`make security-results`. What each test checks, and how to read this file, is in "
        "[Security Testing](../../../docs/security-testing.md#release-snapshots).",
        "",
        "## Provenance",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Release version (`VERSION`) | `{snapshot['version']}` |",
        f"| Snapshot written | {snapshot['date']} |",
        f"| Stack version (its template description) | {snapshot['stackVersion']} |",
    ]
    for part in PARTS:
        body = p[part]
        if body["ran"]:
            lines.append(
                f"| {PART_TITLES[part]} | measured {body['measured']}, tests at commit `{body['commit']}` |"
            )
        else:
            lines.append(f"| {PART_TITLES[part]} | not run |")
    lines += ["", "## Results", "", "| Test | Result |", "|---|---|"]
    for part in PARTS:
        lines.append(f"| {PART_TITLES[part]} | {_cell(_headline(part, p[part]))} |")

    srt = p["srt"]
    lines += ["", f"## {PART_TITLES['srt']}", ""]
    if srt["ran"]:
        lines += [
            "Findings by priority and status. *Suppressed* findings were marked suppressed in "
            "triage; the lines below the table say how many carry a recorded reason.",
            "",
            "| Priority | Open | Reopened | Suppressed | Resolved |",
            "|---|---|---|---|---|",
        ]
        for priority in SRT_PRIORITIES:
            row = srt["byPriority"][priority]
            lines.append(
                f"| {priority} | {row['Open']} | {row['reopened']} | {row['suppressed']} | {row['resolved']} |"
            )
        t = srt["total"]
        lines += [
            f"| **Total** | {t['Open']} | {t['reopened']} | {t['suppressed']} | {t['resolved']} |",
            "",
            f"Suppressed with a recorded decision: {srt['suppressedWithRecordedDecision']}; "
            f"suppressed without a reason: {srt['suppressedWithoutReason']}.",
        ]
        if srt["trackedDecisions"] is not None:
            lines.append(
                f"Decisions tracked in `.srt/suppressions.json`: {srt['trackedDecisions']}."
            )
    else:
        lines.append(f"Not run: {srt['reason']}.")

    static = p["static-roles"]
    lines += ["", f"## {PART_TITLES['static-roles']}", ""]
    if static["ran"]:
        lines += [
            "The role matrix against the schema, the Cognito template and the docs, the AppSync "
            "contract and sharing tests, and the unit tests of the live tests' helpers.",
            "",
            "| Outcome | Passed | Failed | Errors | Skipped |",
            "|---|---|---|---|---|",
            f"| {static['outcome']} | {static['passed']} | {static['failed']} | {static['errors']} "
            f"| {static['skipped']} |",
        ]
    else:
        lines.append(f"Not run: {static['reason']}.")

    live = p["live-rbac"]
    lines += ["", f"## {PART_TITLES['live-rbac']}", ""]
    if live["ran"]:
        lines += ["| Suite | Passed | Failed | Not run |", "|---|---|---|---|"]
        for suite, c in live["suites"].items():
            lines.append(f"| {_cell(suite)} | {c['pass']} | {c['fail']} | {c['not-run']} |")
        t = live["totals"]
        lines.append(f"| **Total** | {t['pass']} | {t['fail']} | {t['not-run']} |")
        if live["notRunReasons"]:
            lines += ["", "Checks not run, and why:", ""]
            lines += [
                f"- {_cell(r['suite'])}, {r['checks']} check(s): {_cell(r['reason'])}"
                for r in live["notRunReasons"]
            ]
        lines += [
            "",
            "A check not run is not a pass: the condition it needs was absent on the stack.",
        ]
        tests = live.get("tests")
        if tests is None:
            lines += [
                "",
                "Tests skipped as a whole: not recorded (no JUnit file from the same run).",
            ]
        else:
            lines += [
                "",
                f"Tests run: {tests['tests']}; failed: {tests['failed']}; "
                f"skipped as a whole: {sum(s['tests'] for s in tests['skipped'])}. A skipped test "
                "records no check, so its checks are in none of the counts above.",
            ]
            lines += [
                f"- {s['tests']} test(s) skipped: {_cell(s['reason'])}" for s in tests["skipped"]
            ]
    else:
        lines.append(f"Not run: {live['reason']}.")

    zap = p["zap"]
    lines += ["", f"## {PART_TITLES['zap']}", ""]
    if zap["ran"]:
        lines += [
            f"Scan mode: {zap['mode'] or 'not recorded'}. WARN-only: alerts are reported and do not "
            "fail the run. Rules are ZAP's rule outcomes; alerts are counted by risk after the "
            "triage in `scripts/security/zap-rules.conf`.",
            "",
            "| Target | PASS | WARN | FAIL | IGNORE | INFO | High | Medium | Low | Informational |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for t in zap["targets"]:
            if t["ran"]:
                r, a = t["rules"], t["alerts"]
                lines.append(
                    f"| {_cell(t['target'])} | {r['PASS']} | {r['WARN']} | {r['FAIL']} | {r['IGNORE']} "
                    f"| {r['INFO']} | {a['High']} | {a['Medium']} | {a['Low']} | {a['Informational']} |"
                )
            else:
                lines.append(
                    f"| {_cell(t['target'])} | not run: {_cell(t['reason'])} " + "| - " * 8 + "|"
                )
    else:
        lines.append(f"Not run: {zap['reason']}.")

    lines += ["", "## Not run in this snapshot", ""]
    missing = [
        f"- {PART_TITLES[part]}: {p[part]['reason']}." for part in PARTS if not p[part]["ran"]
    ]
    if zap["ran"] and zap["mode"] != "active":
        missing.append("- ZAP active scan: not run (the snapshot's scan is passive).")
    lines += missing or ["- Every part ran."]
    lines += ["", "## What these tests do not exercise", ""]
    lines += [f"- {item}" for item in snapshot["notExercised"]]
    return "\n".join(lines) + "\n"


def _headline(part: str, body: dict) -> str:
    if not body["ran"]:
        return "not run"
    if part == "srt":
        t = body["total"]
        gated = {s: sum(body["byPriority"][p][s] for p in ("Critical", "High")) for s in t}
        return (
            f"Critical and High: {gated['Open']} open, {gated['reopened']} reopened, "
            f"{gated['suppressed']} suppressed; all priorities: {t['Open']} open, "
            f"{t['suppressed']} suppressed"
        )
    if part == "static-roles":
        return (
            f"{body['outcome']}: {body['passed']} passed, {body['failed'] + body['errors']} failed"
        )
    if part == "live-rbac":
        t = body["totals"]
        return f"{t['pass']} passed, {t['fail']} failed, {t['not-run']} not run"
    ran = [t for t in body["targets"] if t["ran"]]
    high = sum(t["alerts"]["High"] for t in ran)
    medium = sum(t["alerts"]["Medium"] for t in ran)
    return f"{len(ran)} of {len(body['targets'])} targets scanned; High {high}, Medium {medium}"


class UnhandledFailures(ValueError):
    """The snapshot records failures, and nobody has said they were handled."""


def failures(snapshot: dict) -> list[str]:
    """The parts whose counts include a failure: a failed check or test, or a ZAP FAIL rule."""
    p = snapshot["parts"]
    found = []
    static = p["static-roles"]
    if static["ran"] and static["outcome"] != "passed":  # a failure, or no tests at all
        found.append("static role checks")
    live = p["live-rbac"]
    if live["ran"] and (live["totals"]["fail"] or (live.get("tests") or {}).get("failed")):
        found.append("live access tests")
    zap = p["zap"]
    if zap["ran"] and any(t["ran"] and t["rules"]["FAIL"] for t in zap["targets"]):
        found.append("ZAP")
    return found


def write(
    snapshot: dict,
    forbidden: set[str],
    out_root: Path,
    replace: bool = False,
    allow_failures: bool = False,
) -> Path:
    """Check both files, then write them to ``<out_root>/<version>/``. Nothing is written on a refusal.

    A snapshot with a failure is refused unless ``allow_failures``: report the
    failure through the channel in CONTRIBUTING.md first, and publish once it is
    fixed or assessed.
    """
    failed = failures(snapshot)
    if failed and not allow_failures:
        raise UnhandledFailures(
            "refusing to write the snapshot: it records failures in "
            + ", ".join(failed)
            + "; handle them first (see the curate-security-results skill), then pass "
            "--allow-failures (ALLOW_FAILURES=1)"
        )
    version = snapshot["version"]
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]*", version):
        raise UnsafeSnapshot(
            "refusing to write the snapshot: the version is not a plain version string"
        )
    markdown = to_markdown(snapshot)
    as_json = json.dumps(snapshot, indent=2) + "\n"
    assert_public_safe(markdown, forbidden)
    assert_public_safe(as_json, forbidden)
    target = out_root / version
    if target.exists() and not replace:
        raise FileExistsError(
            f"{target} exists; pass --replace to overwrite this version's snapshot"
        )
    target.mkdir(parents=True, exist_ok=True)
    (target / "summary.md").write_text(markdown, encoding="utf-8")
    (target / "summary.json").write_text(as_json, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--version", default=None, help="default: the VERSION file")
    parser.add_argument("--date", default=None, help="default: today (UTC)")
    parser.add_argument("--stack-version", default="", help="the tested stack's version, if known")
    parser.add_argument("--srt-issues", default=str(DEFAULTS["srt"]))
    parser.add_argument("--suppressions", default=str(DEFAULTS["suppressions"]))
    parser.add_argument("--static-junit", default=str(DEFAULTS["static-roles"]))
    parser.add_argument("--access-report", default=str(DEFAULTS["live-rbac"]))
    parser.add_argument("--access-junit", default=str(DEFAULTS["live-junit"]))
    parser.add_argument("--zap-summary", default=str(DEFAULTS["zap"]))
    parser.add_argument(
        "--commit",
        action="append",
        default=[],
        metavar="PART=SHA",
        help=f"the commit a part's tests ran from ({', '.join(PARTS)}); repeatable",
    )
    parser.add_argument(
        "--skip", action="append", default=[], choices=PARTS, help="record a part as not run"
    )
    parser.add_argument("--out", default=str(SNAPSHOT_ROOT))
    parser.add_argument("--replace", action="store_true", help="overwrite this version's snapshot")
    parser.add_argument(
        "--allow-failures",
        action="store_true",
        help="write a snapshot that records failures (only once they are handled)",
    )
    args = parser.parse_args(argv)

    commits = {}
    for item in args.commit:
        part, _, sha = item.partition("=")
        if part not in PARTS or not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            parser.error(f"--commit takes PART=SHA with PART one of {', '.join(PARTS)}")
        commits[part] = sha
    sources: dict[str, Path | None] = {
        "srt": Path(args.srt_issues),
        "static-roles": Path(args.static_junit),
        "live-rbac": Path(args.access_report),
        "zap": Path(args.zap_summary),
    }
    for part in args.skip:
        sources[part] = None
    snapshot, forbidden = build(
        sources,
        version=args.version or repo_version(),
        date=args.date or datetime.now(timezone.utc).date().isoformat(),
        stack_version=args.stack_version,
        commits=commits,
        not_run={part: "left out of this snapshot" for part in args.skip},
        suppressions=Path(args.suppressions),
        live_junit=Path(args.access_junit),
    )
    try:
        target = write(snapshot, forbidden, Path(args.out), args.replace, args.allow_failures)
    except (UnsafeSnapshot, UnhandledFailures, FileExistsError) as err:
        print(err, file=sys.stderr)
        return 1
    print(f"Wrote {target / 'summary.md'} and summary.json. Read both before committing them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
