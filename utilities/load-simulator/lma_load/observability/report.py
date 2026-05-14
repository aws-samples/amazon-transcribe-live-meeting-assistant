# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Turn a scenario's JSON results into a human-readable ``summary.md``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_summary(results_dir: Path, run_id: str) -> Path:
    """Scan ``results_dir`` for known result files and render a markdown summary."""
    lines: list[str] = [
        f"# LMA Load Simulator Report — {run_id}",
        "",
        "Auto-generated summary of the load-test artifacts in this directory.",
        "",
    ]

    _maybe_section(lines, results_dir / "backfill-result.json", _fmt_backfill)
    _maybe_section(lines, results_dir / "concurrent-result.json", _fmt_concurrent)
    _maybe_section(lines, results_dir / "rbac-latencies.json", _fmt_rbac)
    _maybe_section(lines, results_dir / "cleanup-counts.json", _fmt_cleanup)

    lines += [
        "",
        "## Files in this run",
        "",
        *(f"- `{p.name}`" for p in sorted(results_dir.iterdir()) if p.is_file()),
    ]

    out = results_dir / "summary.md"
    out.write_text("\n".join(lines))
    return out


def _maybe_section(lines: list[str], path: Path, formatter) -> None:
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return
    lines.extend(formatter(data))


# ── Formatters ────────────────────────────────────────────────


def _fmt_backfill(d: dict[str, Any]) -> list[str]:
    return [
        "## Backfill",
        "",
        f"- Synthetic meetings injected: **{d.get('meetings')}**",
        f"- Distributed across last **{d.get('days_back')}** day(s)",
        f"- Synthetic owners: {d.get('owners')}",
        f"- Kinesis records (emitted / failed): "
        f"{d.get('kinesis_records', {}).get('emitted', 0)} / "
        f"{d.get('kinesis_records', {}).get('failed', 0)}",
        f"- Elapsed: {d.get('elapsed_s')}s "
        f"(throughput ≈ {d.get('throughput_per_s')}/s)",
        "",
    ]


def _fmt_concurrent(d: dict[str, Any]) -> list[str]:
    return [
        "## Concurrent",
        "",
        f"- Driver: **{d.get('driver')}**",
        f"- Requested meetings: {d.get('requested')}",
        f"- Succeeded / failed: **{d.get('succeeded')}** / {d.get('failed')}",
        f"- Elapsed: {d.get('elapsed_s')}s",
        "",
    ]


def _fmt_rbac(d: Any) -> list[str]:
    if not isinstance(d, list):
        return []
    return [
        "## RBAC Latency",
        "",
        f"- Total queries: {len(d)}",
        f"- Sample row: `{json.dumps(d[0], default=str)}`" if d else "",
        "",
    ]


def _fmt_cleanup(d: dict[str, Any]) -> list[str]:
    return [
        "## Cleanup",
        "",
        f"- Cognito users: {d.get('cognito_users')}",
        f"- VP records: {d.get('vp_records')}",
        f"- DDB meeting rows: {d.get('ddb_meetings')}",
        f"- S3 objects: {d.get('s3_objects')}",
        f"- Non-fatal errors: {len(d.get('errors') or [])}",
        "",
    ]
