# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Scenario 2 — Historical backfill.

Fabricates N synthetic meetings distributed across the last Y days so we can
stress-test:

* the Meeting List paginator + RBAC filter,
* the date-range picker (``listCallsDateRange`` / ``getCallCount`` GSIs),
* the transcript detail page with realistic segment counts,
* **Bedrock summary throughput + quota handling** (default),
* optionally the RBAC share-matrix when we distribute ownership across
  N synthetic users.

Default driver: Kinesis injection (see :mod:`lma_load.drivers.kinesis_injector`).
The CallDataStream happily accepts backdated ``CreatedAt`` values and
``call_event_processor`` faithfully forwards them into DynamoDB, so each
fabricated meeting has the same shape as a genuine past meeting.

### Why we emit in three phases

Early versions queued [START, segs..., END] for one meeting into a single
PutRecords batch. The downstream Lambda consumes records concurrently via
``asyncio.gather``, so END could race ahead of createCall or of the segments
— producing meetings stuck in "In Progress", zero-duration rows, or
summary-errored rows depending on which race won.

The fix is to emit one phase at a time, flushing and pausing between phases,
so the Lambda sees START events fully resolved in DDB before any segment
lands, and all segments landed before END triggers the Bedrock orchestrator::

    phase 1: STARTs   →  createCall for every meeting
       (sleep 2s — lets the event-source-mapping drain and invoke the Lambda)
    phase 2: SEGMENTS →  addTranscriptSegment × N × M
       (sleep 2s)
    phase 3: ENDs     →  updateCallStatus(ENDED) + summary orchestrator triggers
       (optionally followed by ADD_SUMMARY for --skip-summary path)

For 200 meetings that's three flushes totalling ~5 s wall-clock, which is
well within the throughput target for the scenario.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from lma_load.auth.cognito import SyntheticUserPool
from lma_load.drivers.kinesis_injector import (
    KinesisInjector,
    make_backfill_specs,
)
from lma_load.quota_probe import fetch_quotas, render_summary
from lma_load.run_context import RunContext, estimate_backfill_cost, synthetic_owner
from lma_load.stack_info import LMAStackInfo

logger = logging.getLogger(__name__)
console = Console()


# Inter-phase pause. Must be >= the event-source-mapping's BatchingWindow
# (typically 1–2s) so the next phase's records land on a fresh Lambda
# invocation rather than piling into the same batch as the previous phase.
PHASE_SLEEP_S = 2.0


@dataclass
class BackfillParams:
    meetings: int
    days_back: int
    user_count: int = 1          # distribute ownership across N owners
    email_prefix: str | None = None
    email_domain: str | None = None
    # When True AND email_prefix/email_domain are set, actually create `user_count`
    # real Cognito users and use their usernames (emails) as the Owner values.
    # Without this, `--users N` just spreads ownership across synthetic owner
    # strings that don't correspond to any real user — fine for exercising the
    # Meeting List paginator, but useless for RBAC-at-scale testing because
    # no real user can log in as those owners.
    #
    # When False and email_prefix/email_domain are set, we still skip Cognito
    # and synthesize owner strings (backward-compatible with pre-RBAC callers).
    create_cognito_users: bool = False
    admin_fraction: float = 0.2  # only used when create_cognito_users=True
    # Default is now **with-summary** — we want Bedrock to run for every
    # meeting so the scenario exercises Bedrock throughput and quota
    # handling per the original requirements.
    skip_summary: bool = False
    direct_ddb: bool = False     # future: bypass Kinesis via batch-write


def run(ctx: RunContext, stack: LMAStackInfo, params: BackfillParams) -> dict[str, Any]:
    """Main entry point for ``lma load backfill``."""
    ctx.assert_not_prod()
    _validate(stack, params)

    # 1) Pre-flight quota + cost preview
    cost = estimate_backfill_cost(params.meetings, include_summary=not params.skip_summary)
    console.rule(f"[bold]Backfill Scenario — {ctx.run_id}")
    console.print(f"[dim]{ctx.summary()}[/]")

    if not params.skip_summary and params.meetings >= 50:
        console.print(
            "[yellow]⚠ Bedrock summary is enabled (--with-summary is default). "
            f"Expect ~{params.meetings} Bedrock invocations over the next few minutes; "
            "watch your model's RPM quota. Pass --skip-summary to bypass.[/]"
        )

    quotas = fetch_quotas(ctx.region, ctx.profile)
    ceiling: dict[str, float] = {"kinesis_shards_per_stream": 1}
    if not params.skip_summary:
        ceiling["bedrock_claude_rpm"] = float(params.meetings)
    render_summary(quotas, cost_estimate=cost, ceiling_check=ceiling)

    ctx.confirm_large_scale("inject backfill meetings", params.meetings, threshold=1000)

    # 2) Determine owners. Two modes:
    #
    #   (a) synthetic (default) — `loadtest-<run_id>-u0001`, `…u0002`, etc.
    #       Fast (no Cognito calls). Exercises list paginator + RBAC filter
    #       but NOT real-user RBAC because no real Cognito user can log in
    #       under these owner names.
    #
    #   (b) real Cognito users (when --create-users is set AND
    #       --email-prefix/--email-domain are supplied). We provision
    #       `user_count` users in the stack's User Pool up-front, then use
    #       their email addresses as the Owner values written to DDB. This
    #       is the RBAC-at-scale path: the provisioned users (and password)
    #       are written to `cognito-users.json` in results-dir so you can
    #       log in as them and verify the Meeting List is RBAC-filtered.
    cognito_users: list[Any] = []
    if params.create_cognito_users:
        if not (params.email_prefix and params.email_domain):
            raise ValueError(
                "--create-users requires --email-prefix and --email-domain"
            )
        if not (stack.user_pool_id and stack.user_pool_client_id):
            raise RuntimeError(
                "Could not resolve Cognito UserPoolId + UserPoolClientId from the "
                "stack. --create-users needs these to provision real users."
            )
        provisioner = SyntheticUserPool(
            user_pool_id=stack.user_pool_id,
            user_pool_client_id=stack.user_pool_client_id,
            region=ctx.region,
            run_id=ctx.run_id,
            profile=ctx.profile,
        )
        cognito_users = provisioner.provision(
            count=params.user_count,
            email_prefix=params.email_prefix,
            email_domain=params.email_domain,
            admin_fraction=params.admin_fraction,
        )
        owners = [u.username for u in cognito_users]
        # Persist the provisioned users (incl. password) so a human can log
        # in and verify RBAC-filtered views. Same file shape the rbac
        # scenario writes.
        ctx.write_json(
            "cognito-users.json",
            [
                {
                    "username": u.username,
                    "email": u.email,
                    "password": u.password,
                    "is_admin": u.is_admin,
                    "idx": u.idx,
                }
                for u in cognito_users
            ],
        )
        console.print(
            f"[cyan]✔ Provisioned {len(owners)} real Cognito users "
            f"({sum(1 for u in cognito_users if u.is_admin)} admin, "
            f"{len(owners) - sum(1 for u in cognito_users if u.is_admin)} regular). "
            f"Credentials saved to {ctx.results_dir}/cognito-users.json[/]"
        )
        logger.info("Using %d real Cognito-backed owners", len(owners))
    else:
        owners = [synthetic_owner(ctx.run_id, i) for i in range(1, params.user_count + 1)]
        logger.info("Using %d synthetic (non-Cognito) owners", len(owners))

    # 3) Build the spec list deterministically from the run-id.
    specs = make_backfill_specs(
        count=params.meetings,
        run_id=ctx.run_id,
        days_back=params.days_back,
        owners=owners,
        include_synthetic_summary=params.skip_summary,
    )
    ctx.write_json("backfill-specs.json", [
        {"call_id": s.call_id, "owner": s.owner, "created_at": s.created_at.isoformat()}
        for s in specs[:1000]  # cap preview dump at 1k entries
    ])

    # 4) Inject in three phases so START → segments → END ordering is
    # guaranteed across the Lambda event-source-mapping boundary.
    injector = KinesisInjector(
        stream_name=stack.call_data_stream_name,
        region=ctx.region,
        profile=ctx.profile,
    )
    if ctx.dry_run:
        total_events = sum(
            1 + s.segment_count + 1 + (1 if s.summary_text else 0) for s in specs
        )
        console.print(
            f"[yellow]DRY-RUN: would inject {len(specs)} meetings across 3 phases "
            f"(~{total_events} Kinesis records, "
            f"synthetic-summary={'yes' if params.skip_summary else 'no (Bedrock runs)'} )[/]"
        )
        return {"run_id": ctx.run_id, "meetings": 0, "dry_run": True}

    start = time.monotonic()
    phase_counts: dict[str, int] = {}
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        # Phase 1 — STARTs
        task = progress.add_task(
            f"Phase 1/3: emitting {len(specs)} START events to "
            f"{stack.call_data_stream_name}",
            total=None,
        )
        phase_counts["start"] = injector.emit_phase("START", specs)
        progress.update(task, description=(
            f"Phase 1/3 done ({phase_counts['start']} records); "
            f"pausing {PHASE_SLEEP_S}s for createCall to settle"
        ))
        time.sleep(PHASE_SLEEP_S)

        # Phase 2 — segments (usually the biggest)
        progress.update(task, description=(
            f"Phase 2/3: emitting ~{sum(s.segment_count for s in specs)} "
            f"ADD_TRANSCRIPT_SEGMENT events"
        ))
        phase_counts["segments"] = injector.emit_phase("SEGMENTS", specs)
        progress.update(task, description=(
            f"Phase 2/3 done ({phase_counts['segments']} records); "
            f"pausing {PHASE_SLEEP_S}s"
        ))
        time.sleep(PHASE_SLEEP_S)

        # Phase 3 — ENDs (+ ADD_SUMMARY if --skip-summary was used)
        progress.update(task, description=(
            "Phase 3/3: emitting END events"
            + (" + synthetic ADD_SUMMARY" if params.skip_summary else "")
        ))
        phase_counts["end"] = injector.emit_phase("END", specs, run_id=ctx.run_id)
        progress.update(task, description=(
            f"Phase 3/3 done ({phase_counts['end']} records)"
        ))

    elapsed = time.monotonic() - start
    injector.flush()

    total_records = injector.stats["emitted"]
    result = {
        "run_id": ctx.run_id,
        "meetings": len(specs),
        "days_back": params.days_back,
        "owners": len(owners),
        "with_summary": not params.skip_summary,
        "phase_counts": phase_counts,
        "kinesis_records": injector.stats,
        "elapsed_s": round(elapsed, 2),
        "throughput_per_s": round(len(specs) / elapsed, 2) if elapsed > 0 else None,
    }
    ctx.write_json("backfill-result.json", result)
    console.print(
        f"[green]✅ Backfill complete[/] — {len(specs)} meetings "
        f"({total_records} records) in {elapsed:.1f}s "
        f"({result['throughput_per_s']}/s)"
    )
    if not params.skip_summary:
        console.print(
            "[dim]Bedrock summary orchestrator is running asynchronously. "
            "Summaries will populate over the next few minutes.[/]"
        )
    console.print(f"[dim]Results saved to {ctx.results_dir}[/]")
    return result


# ---------------------------------------------------------------------------
# Pre-flight validation
# ---------------------------------------------------------------------------
def _validate(stack: LMAStackInfo, params: BackfillParams) -> None:
    if not stack.call_data_stream_name:
        raise RuntimeError(
            "Could not resolve CallDataStream name from stack outputs / resources. "
            "Verify the stack was deployed successfully."
        )
    if params.meetings <= 0:
        raise ValueError("--meetings must be > 0")
    if params.days_back <= 0:
        raise ValueError("--days must be > 0")
    if params.direct_ddb:
        raise NotImplementedError(
            "--direct-ddb is reserved for a future release; use the default "
            "Kinesis path for now."
        )
