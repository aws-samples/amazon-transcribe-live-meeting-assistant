# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Run-context: per-invocation state shared across scenarios.

Encapsulates the ``runId`` tag that every synthetic object carries, the
output directory for results, and safety-rail checks (prod-stack detection,
large-scale confirmations, cost estimates).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Tags & conventions
# ---------------------------------------------------------------------------
LOAD_TEST_RUN_TAG = "LoadTestRunId"
OWNER_PREFIX = "loadtest"  # every synthetic meeting's Owner starts with this


def make_run_id(prefix: str = "lt") -> str:
    """Generate a timestamped, sortable run-id (``lt-20260510T131456-abc123``)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    rand = uuid.uuid4().hex[:6]
    return f"{prefix}-{stamp}-{rand}"


def synthetic_owner(run_id: str, user_idx: int) -> str:
    """Deterministic Owner string used for meetings created by user N.

    Kept separate from the Cognito username so we can tag meetings *and*
    correlate them back to a specific synthetic user.
    """
    return f"{OWNER_PREFIX}-{run_id}-u{user_idx:04d}"


# ---------------------------------------------------------------------------
# RunContext
# ---------------------------------------------------------------------------
PROD_HINTS = ("prod", "production", "prd")


@dataclass
class RunContext:
    """Runtime context passed into every scenario.

    Attributes:
        stack_name: Target CloudFormation stack.
        region: AWS region.
        profile: AWS CLI profile (optional).
        run_id: Unique tag for this invocation (used for cleanup).
        results_dir: Where per-run artifacts (reports, CSVs, logs) are written.
        dry_run: If True, drivers should log what they *would* do and skip
            any AWS-mutating calls.
        force: Bypass interactive safety-rail prompts.
    """

    stack_name: str
    region: str
    run_id: str = field(default_factory=make_run_id)
    profile: str | None = None
    results_dir: Path = field(default_factory=lambda: Path("./results"))
    dry_run: bool = False
    force: bool = False

    # Populated lazily by StackInfo.resolve()
    outputs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.results_dir = Path(self.results_dir).resolve() / self.run_id
        self.results_dir.mkdir(parents=True, exist_ok=True)

    # ── Safety rails ──────────────────────────────────────────

    def assert_not_prod(self) -> None:
        """Refuse to run against anything that looks like a production stack."""
        lowered = (self.stack_name or "").lower()
        if any(hint in lowered for hint in PROD_HINTS):
            raise RuntimeError(
                f"Refusing to run load-simulator against stack {self.stack_name!r} — "
                f"name contains a production hint ({', '.join(PROD_HINTS)}). "
                "If you're sure, rename or re-run with --force."
            )

    def confirm_large_scale(self, what: str, n: int, threshold: int = 100) -> None:
        """Prompt before creating/deleting more than ``threshold`` synthetic objects."""
        if n <= threshold or self.force:
            return
        resp = input(f"About to {what} {n} objects. Proceed? [y/N]: ").strip().lower()
        if resp not in ("y", "yes"):
            raise RuntimeError(f"User aborted {what} (requested={n}, threshold={threshold})")

    # ── Artifact helpers ──────────────────────────────────────

    def write_json(self, name: str, data: Any) -> Path:
        path = self.results_dir / name
        path.write_text(json.dumps(data, default=str, indent=2))
        return path

    def log_path(self, name: str) -> Path:
        return self.results_dir / name

    # ── Introspection ─────────────────────────────────────────

    def summary(self) -> str:
        return (
            f"runId={self.run_id} stack={self.stack_name} region={self.region} "
            f"profile={self.profile or '-'} dry_run={self.dry_run} "
            f"results={self.results_dir}"
        )


# ---------------------------------------------------------------------------
# Cost / scale estimation helpers
# ---------------------------------------------------------------------------
# Public list prices (USD, us-east-1, as of Q4-2025) — callers should treat
# these as rough estimates, not invoicing data. Exposed so quota_probe.py can
# print a pre-flight cost preview.
TRANSCRIBE_STREAMING_PER_MIN = 0.024  # tier-1 standard streaming, per channel
TRANSCRIBE_BATCH_PER_MIN = 0.024
BEDROCK_SUMMARY_PER_CALL_EST = 0.02  # very rough; depends on model & token count
KINESIS_PUT_PER_MILLION = 0.014  # PUT payload units


def estimate_concurrent_cost(
    driver: str,
    meetings: int,
    duration_min: float,
    stereo: bool = True,
) -> dict:
    """Best-effort pre-flight cost estimate for ``lma load concurrent``."""
    channels = 2 if stereo else 1
    minutes_total = meetings * duration_min * channels
    transcribe = 0.0
    if driver in ("websocket", "vp"):
        transcribe = minutes_total * TRANSCRIBE_STREAMING_PER_MIN
    elif driver == "upload":
        transcribe = minutes_total * TRANSCRIBE_BATCH_PER_MIN
    bedrock = meetings * BEDROCK_SUMMARY_PER_CALL_EST
    return {
        "driver": driver,
        "meetings": meetings,
        "duration_min": duration_min,
        "transcribe_usd": round(transcribe, 2),
        "bedrock_usd": round(bedrock, 2),
        "total_usd": round(transcribe + bedrock, 2),
        "note": "List-price estimate — actual billing may differ.",
    }


def estimate_backfill_cost(meetings: int, include_summary: bool) -> dict:
    """Pre-flight estimate for ``lma load backfill``."""
    kinesis = (meetings / 1_000_000) * KINESIS_PUT_PER_MILLION
    bedrock = meetings * BEDROCK_SUMMARY_PER_CALL_EST if include_summary else 0.0
    return {
        "meetings": meetings,
        "include_summary": include_summary,
        "kinesis_usd": round(kinesis, 4),
        "bedrock_usd": round(bedrock, 2),
        "total_usd": round(kinesis + bedrock, 2),
        "note": "List-price estimate — actual billing may differ.",
    }
