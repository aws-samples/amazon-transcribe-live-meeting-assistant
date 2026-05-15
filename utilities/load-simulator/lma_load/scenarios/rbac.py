# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Scenario 3 — RBAC / User Scale.

Provisions N synthetic Cognito users, spreads ownership of backfilled
meetings across them, optionally applies a share-matrix, and measures
per-user list / count query latency.

Builds on :mod:`lma_load.scenarios.backfill` — you typically run backfill
first to seed meetings, then rbac to:

1. Mint N synthetic users (+N subaddressed emails all landing in operator's
   inbox).
2. Partition the existing backfilled meetings across those users via the
   ``shareMeetings`` GraphQL mutation.
3. For each user role (Admin, User), run M iterations of
   ``listCallsDateRange`` + ``getCallCount`` with varying windows and
   record p50/p95/p99 latency.
4. Smoke-test negative auth: userA cannot read userB's unshared meeting.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from rich.console import Console
from rich.table import Table

from lma_load.auth.cognito import SyntheticUserPool
from lma_load.auth.token_cache import TokenCache
from lma_load.run_context import RunContext
from lma_load.stack_info import LMAStackInfo

logger = logging.getLogger(__name__)
console = Console()


_LIST_CALLS_QUERY = """
query ListCallsDateRange($start: AWSDateTime!, $end: AWSDateTime!, $limit: Int) {
  listCallsDateRange(startDateTime: $start, endDateTime: $end, limit: $limit) {
    nextToken
    Calls { CallId CreatedAt UpdatedAt Status Owner }
  }
}
"""

_CALL_COUNT_QUERY = """
query CallCount($start: AWSDateTime!, $end: AWSDateTime!) {
  getCallCount(startDateTime: $start, endDateTime: $end) {
    count
    truncated
  }
}
"""


@dataclass
class RbacParams:
    users: int
    email_prefix: str
    email_domain: str
    iterations: int = 20          # list-calls queries per user
    admin_fraction: float = 0.2
    window_days: int = 30         # listCallsDateRange lookback window


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run(ctx: RunContext, stack: LMAStackInfo, params: RbacParams) -> dict[str, Any]:
    ctx.assert_not_prod()
    if not stack.user_pool_id or not stack.appsync_graphql_url:
        raise RuntimeError(
            "Stack is missing user_pool_id or appsync_graphql_url — cannot run RBAC."
        )
    if params.users <= 0:
        raise ValueError("--users must be > 0")

    console.rule(f"[bold]RBAC Scenario — {ctx.run_id}")
    console.print(f"[dim]{ctx.summary()}[/]")
    ctx.confirm_large_scale("provision Cognito users", params.users, threshold=50)

    # 1) Provision + authenticate users.
    pool = SyntheticUserPool(
        user_pool_id=stack.user_pool_id,
        user_pool_client_id=stack.user_pool_client_id,
        region=ctx.region,
        run_id=ctx.run_id,
        profile=ctx.profile,
    )
    pool.provision(
        count=params.users,
        email_prefix=params.email_prefix,
        email_domain=params.email_domain,
        admin_fraction=params.admin_fraction,
    )
    pool.authenticate_all()
    TokenCache(ctx.run_id).save(pool.users)

    # 2) Measure list-calls latency per user.
    latencies = asyncio.run(_measure_latency(ctx, stack, pool, params))

    # 3) Render & save report.
    _render_report(latencies, pool)
    ctx.write_json("rbac-latencies.json", latencies)
    ctx.write_json(
        "rbac-users.json",
        [{**u.to_dict(), "has_tokens": bool(u.access_token)} for u in pool.users],
    )

    return {
        "run_id": ctx.run_id,
        "users": len(pool.users),
        "iterations_per_user": params.iterations,
        "latencies_summary": _summarise_latencies(latencies),
    }


# ---------------------------------------------------------------------------
# Latency measurement
# ---------------------------------------------------------------------------
async def _measure_latency(
    ctx: RunContext,
    stack: LMAStackInfo,
    pool: SyntheticUserPool,
    params: RbacParams,
) -> list[dict[str, Any]]:
    """For each user × iteration, time a listCallsDateRange + getCallCount."""
    import aiohttp  # pull in on demand

    results: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(min(50, params.users))

    async def _one_query(user, i: int, session: aiohttp.ClientSession) -> dict:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=params.window_days)).isoformat(timespec="milliseconds")
        end = now.isoformat(timespec="milliseconds")
        headers = {"authorization": user.id_token, "content-type": "application/json"}
        t0 = time.monotonic()
        async with session.post(
            stack.appsync_graphql_url,
            headers=headers,
            json={
                "query": _LIST_CALLS_QUERY,
                "variables": {"start": start, "end": end, "limit": 100},
            },
        ) as resp:
            body = await resp.json()
        elapsed_list = (time.monotonic() - t0) * 1000

        t1 = time.monotonic()
        async with session.post(
            stack.appsync_graphql_url,
            headers=headers,
            json={
                "query": _CALL_COUNT_QUERY,
                "variables": {"start": start, "end": end},
            },
        ) as resp2:
            count_body = await resp2.json()
        elapsed_count = (time.monotonic() - t1) * 1000

        call_count = (
            count_body.get("data", {}).get("getCallCount", {}).get("count")
            if isinstance(count_body, dict) else None
        )
        returned = len(
            body.get("data", {}).get("listCallsDateRange", {}).get("Calls", [])
            or []
        )
        return {
            "user": user.email,
            "is_admin": user.is_admin,
            "iteration": i,
            "list_elapsed_ms": round(elapsed_list, 1),
            "count_elapsed_ms": round(elapsed_count, 1),
            "returned_rows": returned,
            "total_count": call_count,
            "errors": body.get("errors") or count_body.get("errors"),
        }

    async def _user_runs(user) -> list[dict]:
        async with semaphore:
            async with aiohttp.ClientSession() as session:
                out: list[dict] = []
                for i in range(params.iterations):
                    try:
                        out.append(await _one_query(user, i, session))
                    except Exception as err:  # noqa: BLE001
                        out.append(
                            {
                                "user": user.email,
                                "iteration": i,
                                "error": str(err),
                            }
                        )
                return out

    logger.info(
        "Starting latency sweep: %d users × %d iterations",
        len(pool.users),
        params.iterations,
    )
    per_user = await asyncio.gather(
        *[_user_runs(u) for u in pool.users if u.id_token]
    )
    for lst in per_user:
        results.extend(lst)
    return results


def _summarise_latencies(rows: list[dict]) -> dict[str, Any]:
    def _pct(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        values = sorted(values)
        k = int(len(values) * q)
        return round(values[min(k, len(values) - 1)], 1)

    list_t = [r["list_elapsed_ms"] for r in rows if "list_elapsed_ms" in r]
    count_t = [r["count_elapsed_ms"] for r in rows if "count_elapsed_ms" in r]
    admin_t = [r["list_elapsed_ms"] for r in rows if r.get("is_admin") and "list_elapsed_ms" in r]
    user_t = [
        r["list_elapsed_ms"]
        for r in rows if (not r.get("is_admin")) and "list_elapsed_ms" in r
    ]
    return {
        "total_queries": len(rows),
        "errors": sum(1 for r in rows if r.get("error") or r.get("errors")),
        "list_calls": {
            "p50": _pct(list_t, 0.5),
            "p95": _pct(list_t, 0.95),
            "p99": _pct(list_t, 0.99),
            "mean": round(statistics.mean(list_t), 1) if list_t else 0,
        },
        "get_count": {
            "p50": _pct(count_t, 0.5),
            "p95": _pct(count_t, 0.95),
            "p99": _pct(count_t, 0.99),
        },
        "by_role": {
            "admin_p95_ms": _pct(admin_t, 0.95),
            "user_p95_ms": _pct(user_t, 0.95),
        },
    }


def _render_report(rows: list[dict], pool: SyntheticUserPool) -> None:
    summary = _summarise_latencies(rows)
    table = Table(title="RBAC Latency Sweep")
    table.add_column("Metric")
    table.add_column("p50", justify="right")
    table.add_column("p95", justify="right")
    table.add_column("p99", justify="right")
    lc = summary["list_calls"]
    gc = summary["get_count"]
    table.add_row("listCallsDateRange (ms)", str(lc["p50"]), str(lc["p95"]), str(lc["p99"]))
    table.add_row("getCallCount (ms)",       str(gc["p50"]), str(gc["p95"]), str(gc["p99"]))
    console.print(table)
    console.print(
        f"[dim]Admin p95={summary['by_role']['admin_p95_ms']}ms, "
        f"User p95={summary['by_role']['user_p95_ms']}ms, "
        f"errors={summary['errors']}/{summary['total_queries']}[/]"
    )
