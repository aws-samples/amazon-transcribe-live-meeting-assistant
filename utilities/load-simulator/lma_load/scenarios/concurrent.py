# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Scenarios 1 + 4 (merged) — Concurrent meetings.

Runs N simultaneous meetings for Y duration, using one of four drivers:

* ``kinesis`` — synthetic segments onto CallDataStream; ~$0, no Transcribe
* ``upload`` — presigned S3 PUT of a WAV fixture → batch Transcribe
* ``websocket`` — real streaming WebSocket with looped WAV frames
* ``vp`` — headless Chromium at /#/embed?component=vp-loader&autoStart=true
  round-robin'd across operator-provided real meeting IDs

Implementation detail: the kinesis driver runs synchronously and simulates the
"duration" by spreading segment CreatedAt timestamps across the requested
window (the meeting appears "live" in the UI but the scenario finishes fast).
The websocket, upload, and vp drivers are truly asyncio-concurrent.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from rich.console import Console

from lma_load.drivers.kinesis_injector import (
    KinesisInjector,
    SyntheticMeetingSpec,
)
from lma_load.quota_probe import fetch_quotas, render_summary
from lma_load.run_context import RunContext, estimate_concurrent_cost, synthetic_owner
from lma_load.stack_info import LMAStackInfo

logger = logging.getLogger(__name__)
console = Console()


@dataclass
class ConcurrentParams:
    driver: str                     # "kinesis" | "upload" | "websocket" | "vp"
    meetings: int
    duration_s: float               # per-meeting wall-clock seconds
    concurrency: int = 0            # 0 → same as meetings (no throttle)
    ramp_s: float = 30.0            # spread the starts across this many seconds
    jitter_s: float = 2.0           # additional per-meeting random jitter
    # Driver-specific knobs
    wav_path: str | None = None     # ws + upload
    meeting_ids_file: str | None = None  # vp
    user_pool_size: int = 0         # ws — number of synthetic users to mint
    email_prefix: str | None = None
    email_domain: str | None = None

    def __post_init__(self) -> None:
        if self.concurrency <= 0:
            self.concurrency = self.meetings


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run(ctx: RunContext, stack: LMAStackInfo, params: ConcurrentParams) -> dict[str, Any]:
    """Main entry for ``lma load concurrent``."""
    ctx.assert_not_prod()
    _validate(stack, params)

    # Pre-flight: cost & quota preview
    dur_min = params.duration_s / 60.0
    cost = estimate_concurrent_cost(params.driver, params.meetings, dur_min)
    console.rule(f"[bold]Concurrent Scenario ({params.driver}) — {ctx.run_id}")
    console.print(f"[dim]{ctx.summary()}[/]")

    ceiling: dict[str, float] = {}
    if params.driver in ("websocket", "vp"):
        # Each meeting → 2 Transcribe streaming channels (stereo).
        ceiling["transcribe_streaming_concurrent"] = params.meetings * 2
    elif params.driver == "upload":
        ceiling["transcribe_batch_concurrent"] = params.meetings

    quotas = fetch_quotas(ctx.region, ctx.profile)
    render_summary(quotas, cost_estimate=cost, ceiling_check=ceiling)

    ctx.confirm_large_scale(
        f"drive {params.meetings} concurrent '{params.driver}' meetings",
        params.meetings,
        threshold=50,
    )

    driver_fn = _select_driver(params.driver)
    logger.info("Selected driver: %s", params.driver)

    if ctx.dry_run:
        console.print(f"[yellow]DRY-RUN: would drive {params.meetings} meetings via {params.driver}[/]")
        return {"run_id": ctx.run_id, "driver": params.driver, "dry_run": True}

    start = time.monotonic()
    results = asyncio.run(driver_fn(ctx, stack, params))
    elapsed = time.monotonic() - start

    ok = sum(1 for r in results if r.get("status") == "ok")
    err = len(results) - ok
    summary = {
        "run_id": ctx.run_id,
        "driver": params.driver,
        "requested": params.meetings,
        "succeeded": ok,
        "failed": err,
        "elapsed_s": round(elapsed, 2),
        "results": results[:500],   # cap at 500 for the report
    }
    ctx.write_json("concurrent-result.json", summary)

    console.print(
        f"[green]✅ Concurrent run complete[/] — "
        f"{ok}/{params.meetings} succeeded, {err} failed in {elapsed:.1f}s"
    )
    console.print(f"[dim]Results saved to {ctx.results_dir}[/]")
    return summary


# ---------------------------------------------------------------------------
# Driver selection
# ---------------------------------------------------------------------------
def _select_driver(name: str) -> Callable:
    if name == "kinesis":
        return _drive_kinesis
    if name == "websocket":
        # Late-import so the optional dep isn't required when unused.
        from lma_load.drivers.ws_streaming_driver import drive_websocket
        return drive_websocket
    if name == "upload":
        from lma_load.drivers.upload_driver import drive_upload
        return drive_upload
    if name == "vp":
        from lma_load.drivers.vp_loader_driver import drive_vp
        return drive_vp
    raise ValueError(f"Unknown driver: {name!r}")


def _validate(stack: LMAStackInfo, params: ConcurrentParams) -> None:
    missing: list[str] = []
    if params.driver == "kinesis":
        if not stack.call_data_stream_name:
            missing.append("call_data_stream_name")
    if params.driver == "websocket":
        if not stack.ws_endpoint:
            missing.append("ws_endpoint")
        if not stack.user_pool_id or not stack.user_pool_client_id:
            missing.append("user_pool_id/client_id")
    if params.driver == "upload":
        if not stack.appsync_graphql_url:
            missing.append("appsync_graphql_url")
        if not stack.recordings_bucket:
            missing.append("recordings_bucket")
        if not stack.user_pool_id:
            missing.append("user_pool_id")
    if params.driver == "vp":
        # API-only driver: needs AppSync URL + caller IAM creds, not a
        # CloudFront origin. The LMAClient resolves AppSync lazily.
        if not params.meeting_ids_file:
            raise ValueError(
                "--meeting-ids-file is required for the vp driver "
                "(see README for the YAML schema)."
            )
    if missing:
        raise RuntimeError(
            f"Stack is missing required resource(s) for driver={params.driver}: "
            f"{', '.join(missing)}"
        )


# ---------------------------------------------------------------------------
# Kinesis concurrent driver (no audio, no Transcribe — smoke test)
# ---------------------------------------------------------------------------
async def _drive_kinesis(
    ctx: RunContext, stack: LMAStackInfo, params: ConcurrentParams
) -> list[dict]:
    """Fabricate N simultaneous live-style meetings by streaming segments to
    Kinesis in real-time pacing. Appears "live" in the UI.

    This is not ``asyncio``-bound (Kinesis PutRecord is sync) but we
    run each meeting's lifecycle in a thread-pool so we can hit the requested
    concurrency against a single shared injector.
    """
    injector = KinesisInjector(
        stream_name=stack.call_data_stream_name,
        region=ctx.region,
        profile=ctx.profile,
    )
    owner = synthetic_owner(ctx.run_id, 1)

    loop = asyncio.get_running_loop()
    semaphore = asyncio.Semaphore(params.concurrency)
    rng = random.Random(ctx.run_id)

    async def _one(idx: int) -> dict:
        # Ramp-in: spread starts across ramp_s.
        await asyncio.sleep(
            (idx / max(params.meetings, 1)) * params.ramp_s
            + rng.random() * params.jitter_s
        )
        call_id = f"loadtest-{ctx.run_id}-live-{idx:05d} - {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}"
        spec = SyntheticMeetingSpec(
            call_id=call_id,
            owner=owner,
            created_at=datetime.now(timezone.utc),
            duration_s=params.duration_s,
            segment_count=max(3, int(params.duration_s / 15)),
        )
        t0 = time.monotonic()
        async with semaphore:
            # Emit START immediately so the meeting appears live.
            start_events = list(_split_events(spec))
            # 1) START
            await loop.run_in_executor(
                None, lambda: injector.emit_meeting_immediate(start_events[0], spec.call_id)
            )
            # 2) Segments, real-time paced
            for ev in start_events[1:-1]:
                await asyncio.sleep(params.duration_s / max(len(start_events) - 2, 1))
                await loop.run_in_executor(
                    None, lambda ev=ev: injector.emit_meeting_immediate(ev, spec.call_id)
                )
            # 3) END
            await loop.run_in_executor(
                None, lambda: injector.emit_meeting_immediate(start_events[-1], spec.call_id)
            )
        return {
            "callId": call_id,
            "status": "ok",
            "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
        }

    tasks = [asyncio.create_task(_one(i)) for i in range(1, params.meetings + 1)]
    out: list[dict] = []
    for t in asyncio.as_completed(tasks):
        try:
            out.append(await t)
        except Exception as err:  # noqa: BLE001
            logger.exception("Kinesis driver meeting failed: %s", err)
            out.append({"status": "error", "error": str(err)})
    injector.flush()
    return out


def _split_events(spec: SyntheticMeetingSpec) -> list[dict]:
    """Build the START → segments → END sequence for a single meeting."""
    from lma_load.drivers.kinesis_injector import _build_events  # noqa: WPS433
    return list(_build_events(spec))


# Monkey-patch: give KinesisInjector a sync single-event helper used by
# _drive_kinesis to emit one event at a time with real-time pacing.
def _emit_single(self, event: dict, partition_key: str) -> None:  # noqa: D401
    self._buffer(partition_key, event)
    self.flush()


KinesisInjector.emit_meeting_immediate = _emit_single  # type: ignore[attr-defined]
