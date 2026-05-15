# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Pre-flight quota & cost probe.

Before every scenario we surface:

1. Relevant Service Quotas (Transcribe concurrent streaming / batch jobs,
   Bedrock on-demand model quotas, ECS tasks).
2. A best-effort cost estimate (see run_context.estimate_*).
3. A warning if the requested load exceeds the quota.

All of this is advisory — we don't block the run, we just print a summary so
the operator can decide to proceed, request a quota increase, or reduce scale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError
from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)
console = Console()

# Service-quota codes (most relevant to LMA load tests). These are stable
# public identifiers from the AWS Service Quotas catalogue.
_QUOTA_CODES = {
    "transcribe_streaming_concurrent": ("transcribe", "L-DCB614FB"),   # concurrent streams
    "transcribe_batch_concurrent":     ("transcribe", "L-2CFA8AED"),   # concurrent batch jobs
    # NOTE: Bedrock per-model quotas are dynamic; we report the key ones by
    # probing them at runtime when possible. L-BA0F4B31 = Claude on-demand RPM.
    "bedrock_claude_rpm":               ("bedrock",   "L-BA0F4B31"),
    # ECS Fargate tasks per service (default 5000).
    "ecs_tasks_per_service":            ("ecs",       "L-46212D82"),
    # Kinesis shards per stream (default 500 in most regions).
    "kinesis_shards_per_stream":        ("kinesis",   "L-986EFE72"),
}


@dataclass
class QuotaRow:
    service: str
    name: str
    code: str
    value: float | None
    adjustable: bool | None
    error: str | None = None


def fetch_quotas(region: str, profile: str | None = None) -> list[QuotaRow]:
    """Call service-quotas for each known code. Returns a list of QuotaRow.

    Quotas that fail to resolve (auth, not-supported, throttling) are returned
    with ``value=None`` and an ``error`` string so the caller can render them
    without aborting the run.
    """
    session_kwargs: dict[str, Any] = {"region_name": region}
    if profile:
        session_kwargs["profile_name"] = profile
    sq = boto3.Session(**session_kwargs).client("service-quotas")

    rows: list[QuotaRow] = []
    for name, (service, code) in _QUOTA_CODES.items():
        try:
            resp = sq.get_service_quota(ServiceCode=service, QuotaCode=code)
            q = resp["Quota"]
            rows.append(
                QuotaRow(
                    service=service,
                    name=name,
                    code=code,
                    value=float(q.get("Value", 0)),
                    adjustable=bool(q.get("Adjustable")),
                )
            )
        except ClientError as err:
            rows.append(
                QuotaRow(
                    service=service,
                    name=name,
                    code=code,
                    value=None,
                    adjustable=None,
                    error=err.response.get("Error", {}).get("Code", str(err)),
                )
            )
    return rows


def render_summary(
    rows: list[QuotaRow],
    cost_estimate: dict | None = None,
    ceiling_check: dict[str, float] | None = None,
) -> None:
    """Print a rich-table summary of quotas + cost + optional ceiling check.

    ``ceiling_check`` maps a quota ``name`` → requested load, so we can print
    a RED warning if requested load exceeds the quota.
    """
    table = Table(title="Pre-flight Quota & Cost Preview", show_lines=False)
    table.add_column("Service")
    table.add_column("Quota")
    table.add_column("Limit", justify="right")
    table.add_column("Requested", justify="right")
    table.add_column("Status")

    for r in rows:
        requested = ""
        status = "[green]ok[/]"
        if r.error:
            status = f"[yellow]probe-failed ({r.error})[/]"
        if ceiling_check and r.name in ceiling_check:
            requested = f"{ceiling_check[r.name]:.0f}"
            if r.value is not None and ceiling_check[r.name] > r.value:
                status = f"[red]EXCEEDS LIMIT ({r.value:.0f})[/]"
        table.add_row(
            r.service,
            r.name,
            "-" if r.value is None else f"{r.value:.0f}",
            requested,
            status,
        )
    console.print(table)

    if cost_estimate:
        cost_table = Table(title="Estimated Cost (list prices, USD)")
        cost_table.add_column("Item")
        cost_table.add_column("Value", justify="right")
        for k, v in cost_estimate.items():
            cost_table.add_row(str(k), str(v))
        console.print(cost_table)
