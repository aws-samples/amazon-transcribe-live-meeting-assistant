# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests of curate_snapshot.py: the counts it publishes, and what it refuses to publish."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import curate_snapshot as cs  # noqa: E402
import security_results  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
# Built from parts, so this file holds no literal account-id-shaped number.
ACCOUNT = "1234" * 3
HOST = "abcd1234.appsync-api.example-region.amazonaws.com"
ALERT = "Frobnicator Policy Misconfigured Widely"


def write_reports(root: Path, *, not_run_detail: str = "the stack has no such feature") -> dict:
    """One raw report per part, holding the identifiers the snapshot must drop."""
    srt = root / "issues.json"
    srt.write_text(
        json.dumps(
            [
                {"priority": "High", "status": "Open", "issue": "A finding text nobody may read"},
                {"priority": "High", "status": "suppressed", "suppressionReason": "Reviewed."},
                {"priority": "Low", "status": "suppressed", "suppressionReason": ""},
                {"priority": "Medium", "status": "resolved"},
            ]
        )
    )
    suppressions = root / "suppressions.json"
    suppressions.write_text(json.dumps([{}, {}]))
    junit = root / "static.xml"
    junit.write_text(
        '<testsuites><testsuite name="pytest" tests="10" failures="1" errors="0" skipped="2" '
        'timestamp="2026-11-24T10:00:00"/></testsuites>'
    )
    live = root / "report.json"
    live.write_text(
        json.dumps(
            {
                "stack": "Secret-Stack-Name",
                "region": "example-region-1",
                "generatedAt": "2026-11-25T01:45:37+00:00",
                "totals": {"pass": 5, "fail": 1, "not-run": 1},
                "suites": {
                    "AppSync role matrix": {"pass": 4, "fail": 1, "not-run": 0},
                    "Other entry points": {"pass": 1, "fail": 0, "not-run": 1},
                },
                "checks": [
                    {
                        "suite": "AppSync role matrix",
                        "subject": "Mutation.someOperationName",
                        "caller": "User",
                        "outcome": "fail",
                        "observed": "answered with something private",
                        "detail": "",
                    },
                    {
                        "suite": "Other entry points",
                        "subject": "GET /some/path with no token",
                        "caller": "anonymous",
                        "outcome": "not-run",
                        "detail": not_run_detail,
                    },
                ],
            }
        )
    )
    zap_dir = root / "zap"
    zap_dir.mkdir()
    zap = zap_dir / "summary.json"
    zap.write_text(
        json.dumps(
            {
                "stack": "Secret-Stack-Name",
                "generatedAt": "2026-11-25T01:44:05+00:00",
                "mode": "passive",
                "results": [
                    {
                        "target": "graphql",
                        "url": f"https://{HOST}/graphql",
                        "mode": "passive",
                        "ran": True,
                        "counts": {"High": 0, "Medium": 2, "Low": 1, "Informational": 3},
                        "tally": {
                            "WARN-NEW": 2,
                            "WARN-INPROG": 1,
                            "FAIL-NEW": 0,
                            "IGNORE": 1,
                            "PASS": 100,
                        },
                        "alerts": [{"name": ALERT, "sampleUrls": [f"https://{HOST}/x"]}],
                        "reports": ["/home/someone/scratch/graphql-report.json"],
                    },
                    {
                        "target": "mcp",
                        "mode": "passive",
                        "ran": False,
                        "skipped": "the stack has no MCP server",
                    },
                ],
            }
        )
    )
    (zap_dir / "web-report.json").write_text(
        json.dumps(
            {
                "site": [
                    {
                        "@name": "https://web.example.net",
                        "alerts": [{"alert": "Another Alert Title"}],
                    }
                ]
            }
        )
    )
    return {
        "sources": {"srt": srt, "static-roles": junit, "live-rbac": live, "zap": zap},
        "suppressions": suppressions,
    }


def build(tmp_path: Path, **kwargs):
    reports = write_reports(tmp_path, **kwargs)
    return cs.build(
        reports["sources"],
        version="9.9.9",
        date="2026-11-25",
        stack_version="9.9.9",
        commits={"live-rbac": "f" * 40},
        suppressions=reports["suppressions"],
    )


# ── what is published ──────────────────────────────────────────────────────


def test_the_snapshot_holds_the_counts_of_every_part(tmp_path: Path) -> None:
    snapshot, forbidden = build(tmp_path)
    parts = snapshot["parts"]
    assert parts["srt"]["byPriority"]["High"] == {
        "Open": 1,
        "reopened": 0,
        "suppressed": 1,
        "resolved": 0,
    }
    assert parts["srt"]["suppressedWithRecordedDecision"] == 1
    assert parts["srt"]["suppressedWithoutReason"] == 1
    assert parts["srt"]["trackedDecisions"] == 2
    assert {"passed": 7, "failed": 1, "skipped": 2, "outcome": "failed"}.items() <= parts[
        "static-roles"
    ].items()
    assert parts["live-rbac"]["totals"] == {"pass": 5, "fail": 1, "not-run": 1}
    assert parts["live-rbac"]["notRunReasons"] == [
        {"suite": "Other entry points", "reason": "the stack has no such feature", "checks": 1}
    ]
    graphql, mcp = parts["zap"]["targets"]
    assert graphql["rules"] == {"PASS": 100, "WARN": 3, "FAIL": 0, "IGNORE": 1, "INFO": 0}
    assert graphql["alerts"] == {"High": 0, "Medium": 2, "Low": 1, "Informational": 3}
    assert mcp == {
        "target": "mcp",
        "mode": "passive",
        "ran": False,
        "reason": "the stack has no MCP server",
    }
    assert parts["live-rbac"]["commit"] == "f" * 40 and parts["zap"]["commit"] == "not recorded"
    assert {
        ALERT,
        "Another Alert Title",
        "Secret-Stack-Name",
        "Mutation.someOperationName",
    } <= forbidden


def test_the_written_snapshot_names_no_identifier_from_the_raw_reports(tmp_path: Path) -> None:
    snapshot, forbidden = build(tmp_path)
    target = cs.write(snapshot, forbidden, tmp_path / "out", allow_failures=True)
    assert sorted(p.name for p in target.iterdir()) == ["summary.json", "summary.md"]
    for name in ("summary.md", "summary.json"):
        text = (target / name).read_text()
        for secret in (
            ALERT,
            "Another Alert Title",
            HOST,
            "Secret-Stack-Name",
            "example-region-1",
            "A finding text nobody may read",
            "Mutation.someOperationName",
            "answered with something private",
            "/home/someone",
        ):
            assert secret not in text, (name, secret)
    markdown = (target / "summary.md").read_text()
    assert "| graphql | 100 | 3 | 0 | 1 | 0 | 0 | 2 | 1 | 3 |" in markdown
    assert "| **Total** | 5 | 1 | 1 |" in markdown
    assert "ZAP active scan: not run" in markdown
    assert "Critical and High: 1 open, 0 reopened, 1 suppressed" in markdown
    assert "Tests skipped as a whole: not recorded" in markdown


def test_tests_skipped_as_a_whole_are_listed_with_their_reason(tmp_path: Path) -> None:
    reports = write_reports(tmp_path)
    junit = tmp_path / "live.xml"
    junit.write_text(
        "<testsuites><testsuite>"
        '<testcase name="a"/>'
        '<testcase name="b"><skipped message="the stack serves no viewer path">'
        "integ-tests/test_x.py:1: the stack serves no viewer path</skipped></testcase>"
        '<testcase name="c"><failure message="x"/></testcase>'
        "</testsuite></testsuites>"
    )
    snapshot, _ = cs.build(reports["sources"], version="9.9.9", date="2026-11-25", live_junit=junit)
    assert snapshot["parts"]["live-rbac"]["tests"] == {
        "tests": 3,
        "failed": 1,
        "skipped": [{"reason": "the stack serves no viewer path", "tests": 1}],
    }
    markdown = cs.to_markdown(snapshot)
    assert "Tests run: 3; failed: 1; skipped as a whole: 1." in markdown
    assert "- 1 test(s) skipped: the stack serves no viewer path" in markdown


def test_a_part_with_no_report_is_recorded_as_not_run(tmp_path: Path) -> None:
    snapshot, _ = cs.build(
        {"srt": None, "static-roles": tmp_path / "absent.xml"},
        version="9.9.9",
        date="2026-11-25",
        not_run={"srt": "left out of this snapshot"},
    )
    assert snapshot["parts"]["srt"] == {"ran": False, "reason": "left out of this snapshot"}
    for part in ("static-roles", "live-rbac", "zap"):
        assert snapshot["parts"][part]["ran"] is False
    markdown = cs.to_markdown(snapshot)
    assert (
        "- SRT (static analysis, dependencies, infrastructure as code): left out of this snapshot."
        in markdown
    )
    assert snapshot["stackVersion"] == "not recorded"


# ── what it refuses ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "detail",
    [
        f"see https://{HOST}/graphql",
        f"the role arn:aws:iam::{ACCOUNT}:role/x is absent",
        f"account {ACCOUNT} has no such feature",
        f"{ALERT} was reported",
        "the CSP: script-src unsafe-inline rule",
        f"served from {HOST}",
        "written to /home/someone/report.json",
        "user someone@example.com has no access",
    ],
    ids=[
        "url",
        "arn",
        "account-id",
        "alert-name",
        "alert-wording",
        "hostname",
        "local-path",
        "email",
    ],
)
def test_the_curator_refuses_a_snapshot_with_an_identifier_and_writes_nothing(
    tmp_path: Path, detail: str
) -> None:
    snapshot, forbidden = build(tmp_path, not_run_detail=detail)
    out = tmp_path / "out"
    with pytest.raises(cs.UnsafeSnapshot) as refused:
        cs.write(snapshot, forbidden, out, allow_failures=True)
    assert not out.exists()
    # The refusal names the kind of problem, never the matched text.
    assert detail not in str(refused.value) and ACCOUNT not in str(refused.value)


@pytest.mark.parametrize(
    "text",
    [
        "https://example.org/a",
        "wss://host.example.net/socket",
        f"arn:aws-us-gov:s3:::bucket-{ACCOUNT}",
        f"{ACCOUNT}",
        "us-west-2_AbCdEf123",
        "AKIA" + "A" * 16,
        "Strict-Transport-Security Header Not Set",
        "plugin 10055",
    ],
)
def test_the_guard_rejects_each_identifier_shape(text: str) -> None:
    with pytest.raises(cs.UnsafeSnapshot):
        cs.assert_public_safe(f"before {text} after")


def test_the_guard_rejects_strings_taken_from_the_raw_reports() -> None:
    with pytest.raises(cs.UnsafeSnapshot):
        cs.assert_public_safe("an ordinary sentence about Secret-Stack-Name", {"Secret-Stack-Name"})
    cs.assert_public_safe("an ordinary sentence", {"Admin"})  # too short to be told from a word


def test_the_guard_accepts_versions_dates_commits_and_repository_paths() -> None:
    cs.assert_public_safe(
        "0.4.1.dev1 on 2026-11-25 at `1234567890123456789012345678901234567890`, "
        "see docs/security-testing.md and .srt/suppressions.json; 14380 suppressed"
    )


def test_an_existing_version_is_not_overwritten_unless_asked(tmp_path: Path) -> None:
    snapshot, forbidden = build(tmp_path)
    out = tmp_path / "out"
    cs.write(snapshot, forbidden, out, allow_failures=True)
    with pytest.raises(FileExistsError):
        cs.write(snapshot, forbidden, out, allow_failures=True)
    cs.write(snapshot, forbidden, out, replace=True, allow_failures=True)


def test_a_version_that_is_not_a_plain_version_string_is_refused(tmp_path: Path) -> None:
    snapshot, forbidden = build(tmp_path)
    snapshot["version"] = "../escape"
    with pytest.raises(cs.UnsafeSnapshot):
        cs.write(snapshot, forbidden, tmp_path / "out", allow_failures=True)


def test_the_cli_refuses_with_exit_status_1(tmp_path: Path, capsys) -> None:
    reports = write_reports(tmp_path, not_run_detail=f"see https://{HOST}/")
    s = reports["sources"]
    status = cs.main(
        [
            "--version", "9.9.9", "--date", "2026-11-25", "--out", str(tmp_path / "out"),
            "--srt-issues", str(s["srt"]), "--suppressions", str(reports["suppressions"]),
            "--static-junit", str(s["static-roles"]), "--access-report", str(s["live-rbac"]),
            "--zap-summary", str(s["zap"]), "--allow-failures",
        ]
    )  # fmt: skip
    assert status == 1 and not (tmp_path / "out").exists()
    err = capsys.readouterr().err
    assert HOST not in err and "refusing" in err


def test_a_snapshot_with_failures_is_refused_until_they_are_allowed(tmp_path: Path) -> None:
    snapshot, forbidden = build(tmp_path)
    assert cs.failures(snapshot) == ["static role checks", "live access tests"]
    out = tmp_path / "out"
    with pytest.raises(cs.UnhandledFailures) as refused:
        cs.write(snapshot, forbidden, out)
    assert "static role checks, live access tests" in str(refused.value)
    assert not out.exists()
    assert cs.write(snapshot, forbidden, out, allow_failures=True).is_dir()


def test_a_zap_fail_rule_counts_as_a_failure(tmp_path: Path) -> None:
    snapshot, _ = build(tmp_path)
    snapshot["parts"]["static-roles"] = {"ran": False, "reason": "x"}
    snapshot["parts"]["live-rbac"] = {"ran": False, "reason": "x"}
    assert cs.failures(snapshot) == []
    snapshot["parts"]["zap"]["targets"][0]["rules"]["FAIL"] = 1
    assert cs.failures(snapshot) == ["ZAP"]


def test_a_live_junit_file_from_another_run_is_not_used(tmp_path: Path) -> None:
    reports = write_reports(tmp_path)
    junit = tmp_path / "live.xml"
    junit.write_text('<testsuite><testcase name="a"><skipped message="m"/></testcase></testsuite>')
    report = reports["sources"]["live-rbac"]
    old = report.stat().st_mtime - cs.SAME_RUN_SECONDS - 60
    os.utime(junit, (old, old))
    snapshot, _ = cs.build(reports["sources"], version="9.9.9", date="2026-11-25", live_junit=junit)
    assert snapshot["parts"]["live-rbac"]["tests"] is None
    assert "not recorded (no JUnit file from the same run)" in cs.to_markdown(snapshot)


def test_the_scope_list_matches_the_docs_section_it_cites() -> None:
    doc = (REPO_ROOT / "docs" / "security-testing.md").read_text(encoding="utf-8")
    section = doc.split("## What these tests do not exercise", 1)[1].split("\n## ", 1)[0]
    bullets = [line for line in section.splitlines() if line.startswith("- ")]
    assert len(bullets) == len(cs.NOT_EXERCISED)
    for marker in (
        "Subscriptions",
        "ASR MicroVM",
        "browser extension",
        "Token revocation",
        "Recording links",
        "ZAP sends HTTP requests only",
    ):
        assert marker.lower() in section.lower(), marker
        assert any(marker.lower() in item.lower() for item in cs.NOT_EXERCISED), marker


# ── the committed snapshots ────────────────────────────────────────────────


def committed_snapshots() -> list[Path]:
    return sorted(p for p in (REPO_ROOT / "security" / "test-results").glob("*") if p.is_dir())


@pytest.mark.parametrize("folder", committed_snapshots(), ids=lambda p: p.name)
def test_every_committed_snapshot_is_public_safe_and_counts_only(folder: Path) -> None:
    assert sorted(p.name for p in folder.iterdir()) == ["summary.json", "summary.md"]
    for name in ("summary.md", "summary.json"):
        cs.assert_public_safe((folder / name).read_text(encoding="utf-8"))
    snapshot = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    assert snapshot["version"] == folder.name
    assert set(snapshot["parts"]) == set(cs.PARTS)
    for target in snapshot["parts"]["zap"].get("targets") or []:
        assert set(target) <= {"target", "mode", "ran", "rules", "alerts", "reason"}
    assert (folder / "summary.md").read_text(encoding="utf-8") == cs.to_markdown(snapshot)


# ── security_results.py hands only this run's reports to the curator ───────


def test_a_snapshot_from_a_run_reads_only_the_reports_this_run_wrote(tmp_path: Path) -> None:
    (tmp_path / ".srt").mkdir()
    (tmp_path / ".srt" / "issues.json").write_text("[]")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "static-roles.xml").write_text("<testsuite tests='1'/>")
    srt = security_results.Step("srt", [], status=0, started=0.0)
    static = security_results.Step("static-roles", [], status=0, started=0.0)
    stale = security_results.Step("static-roles", [], status=1, started=4_102_444_800.0)  # 2100
    sources, commits, not_run = security_results.snapshot_inputs(
        [srt, static], "a" * 40, out_dir, tmp_path
    )
    assert sources["srt"] == tmp_path / ".srt" / "issues.json"
    assert sources["static-roles"] == out_dir / "static-roles.xml"
    assert sources["live-rbac"] is None and not_run["live-rbac"] == "no stack was given"
    assert commits == {"srt": "a" * 40, "static-roles": "a" * 40}
    sources, _, not_run = security_results.snapshot_inputs([srt, stale], "", out_dir, tmp_path)
    assert sources["static-roles"] is None
    assert not_run["static-roles"] == "the step wrote no report in this run"


def test_the_live_junit_file_is_used_only_when_this_run_wrote_it(tmp_path: Path) -> None:
    (tmp_path / "live-rbac.xml").write_text("<testsuite/>")
    ran = security_results.Step("live-rbac", [], status=0, started=0.0)
    later = security_results.Step("live-rbac", [], status=0, started=4_102_444_800.0)
    assert security_results.fresh_live_junit([ran], tmp_path) == tmp_path / "live-rbac.xml"
    assert security_results.fresh_live_junit([later], tmp_path) is None
    assert security_results.fresh_live_junit([], tmp_path) is None


def test_the_stack_version_is_read_from_the_template_description() -> None:
    assert (
        security_results.version_of(
            "Amazon Transcribe Live Meeting Assist - LMA (v0.4.1.dev1) (SO9497)"
        )
        == "0.4.1.dev1"
    )
    assert security_results.version_of("no version here") == ""


def test_the_static_role_step_writes_a_junit_file_for_the_snapshot(tmp_path: Path) -> None:
    static = security_results.plan(None, None, "python3", tmp_path)[1]
    assert f"--junitxml={tmp_path / 'static-roles.xml'}" in static.command
