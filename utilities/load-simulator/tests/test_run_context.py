# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Unit tests for RunContext safety rails & cost estimates."""

from __future__ import annotations

import pytest

from lma_load.run_context import (
    RunContext,
    estimate_backfill_cost,
    estimate_concurrent_cost,
    make_run_id,
    synthetic_owner,
)


def test_run_id_is_unique_and_sortable():
    a = make_run_id()
    b = make_run_id()
    assert a != b
    # Format: lt-YYYYMMDDTHHMMSS-<hex6>
    assert a.startswith("lt-") and len(a) > 15


def test_synthetic_owner_format():
    assert synthetic_owner("lt-xyz", 42) == "loadtest-lt-xyz-u0042"


def test_assert_not_prod_rejects_prod_names(tmp_path):
    ctx = RunContext(
        stack_name="my-prod-stack",
        region="us-east-1",
        results_dir=tmp_path,
    )
    with pytest.raises(RuntimeError, match="production hint"):
        ctx.assert_not_prod()


def test_assert_not_prod_allows_safe_names(tmp_path):
    ctx = RunContext(stack_name="LMA", region="us-east-1", results_dir=tmp_path)
    ctx.assert_not_prod()  # should not raise


def test_confirm_large_scale_noops_under_threshold(tmp_path):
    ctx = RunContext(stack_name="LMA", region="us-east-1", results_dir=tmp_path)
    ctx.confirm_large_scale("create users", 10, threshold=100)  # no prompt


def test_estimate_concurrent_cost_ws():
    est = estimate_concurrent_cost("websocket", meetings=10, duration_min=5)
    assert est["total_usd"] > 0
    assert est["transcribe_usd"] > 0


def test_estimate_concurrent_cost_kinesis_is_free():
    est = estimate_concurrent_cost("kinesis", meetings=100, duration_min=10)
    assert est["transcribe_usd"] == 0


def test_estimate_backfill_cost_without_summary():
    est = estimate_backfill_cost(50_000, include_summary=False)
    assert est["bedrock_usd"] == 0
    assert est["kinesis_usd"] >= 0


def test_estimate_backfill_cost_with_summary_scales():
    est = estimate_backfill_cost(10_000, include_summary=True)
    assert est["bedrock_usd"] > 0
