# Copyright (c) 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests of security_results.py (make security-results)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import security_results  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_offline_the_plan_is_srt_and_the_static_role_checks() -> None:
    steps = security_results.plan(None, None, "python3")
    assert [s.name for s in steps] == ["srt", "static-roles"]
    assert steps[0].command == ["make", "srt-setup", "srt-scan"]


def test_with_a_stack_the_plan_adds_the_live_checks_for_that_stack_and_region() -> None:
    steps = security_results.plan("LMA-dev", "us-west-2", "python3")
    assert [s.name for s in steps] == ["srt", "static-roles", "live-rbac", "zap"]
    assert steps[2].command == ["make", "stacktest-rbac", "STACK=LMA-dev", "REGION=us-west-2"]
    assert steps[3].command == ["make", "stacktest-zap", "STACK=LMA-dev", "REGION=us-west-2"]
    assert "ZAP_ACTIVE=1" not in steps[3].command


def test_every_static_role_test_path_exists() -> None:
    for path in security_results.STATIC_ROLE_TESTS:
        assert (REPO_ROOT / path).exists(), path


def test_the_counts_are_read_from_each_live_reports_summary(tmp_path: Path) -> None:
    (tmp_path / "scratch/api-test-results").mkdir(parents=True)
    (tmp_path / "scratch/api-test-results/report.json").write_text(
        json.dumps({"totals": {"pass": 10, "fail": 1, "not-run": 2}})
    )
    (tmp_path / "scratch/zap-reports/latest").mkdir(parents=True)
    (tmp_path / "scratch/zap-reports/latest/summary.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "target": "graphql",
                        "ran": True,
                        "counts": {"High": 0, "Medium": 3, "Low": 4},
                    },
                    {"target": "mcp", "ran": False},
                ]
            }
        )
    )
    rbac = security_results.Step("live-rbac", [])
    zap = security_results.Step("zap", [])
    assert security_results.read_counts(rbac, tmp_path) == "10 checks passed, 1 failed, 2 not run"
    assert (
        security_results.read_counts(zap, tmp_path)
        == "graphql: High 0, Medium 3, Low 4; mcp: not run"
    )
    assert security_results.read_counts(security_results.Step("srt", []), tmp_path) == ""
    assert security_results.read_counts(rbac, tmp_path / "nothing-here") == ""


def test_the_summary_names_every_step_and_its_outcome() -> None:
    passed = security_results.Step("srt", [], ".srt/", status=0, seconds=12.4)
    failed = security_results.Step(
        "live-rbac", [], "scratch/api-test-results/report.md", status=1, detail="3 failed"
    )
    pending = security_results.Step("zap", [])
    text = security_results.to_markdown([passed, failed, pending], "now", "LMA-dev")
    assert "| srt | passed | 12 |  | .srt/ |" in text
    assert "| live-rbac | failed | 0 | 3 failed |" in text
    assert "| zap | not run |" in text
    assert "offline only" in security_results.to_markdown([passed], "now", None)
