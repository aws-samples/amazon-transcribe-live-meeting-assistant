# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Deterministic cleanup of everything a load-simulator run created.

Matches synthetic resources by the ``run-id=<runId>`` marker we embed in
every callId / Owner / username, then deletes them in the right order:

1. **Cognito users** — admin_delete_user for every user whose username
   contains ``loadtest-<runId>-`` (case-insensitive, because Cognito
   normalises usernames to lower-case on intake).
2. **Virtual Participants** — endVirtualParticipant (or direct ECS StopTask
   fallback) for any VP whose ``meetingName`` contains the run-id.
3. **DynamoDB rows** — delete every row whose ``PK`` *or* ``SK`` contains
   ``run-id=<runId>`` (the canonical marker is embedded by
   ``_stable_call_id`` in :mod:`lma_load.drivers.kinesis_injector`). This
   captures every shape the event-processor writes, including:

   * the call row (``PK=c#<callId>``),
   * the transcript segment rows (``PK=trs#<callId>``, ``SK=s#<uuid>``),
   * any upload-job rows (``PK=uj#<callId>``),
   * and the meeting-list shard rows
     (``PK=cls#<date>#s#<shard>``, ``SK=ts#<ts>#id#<callId>``) which
     back the UI meeting-list GSI — **the callId lives only in the SK
     here**, so a PK-only scan would miss one row per meeting and the
     Meeting List would still show stale entries after cleanup.

   The scan ignores ``Owner`` altogether so it works equally well for
   real-Cognito owners and synthetic ``loadtest-…`` owner strings
   (transcript segments and ``cls#`` rows don't carry an Owner at all).

4. **S3 objects** — delete any remaining fixtures under
   ``lma-uploads-pending/loadtest-<runId>-*`` (the lifecycle rule would
   eventually clean these, but we don't want to wait 7 days).

Supports a ``--dry-run`` mode that prints what would be deleted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError
from rich.console import Console
from rich.table import Table

from lma_load.auth.cognito import SyntheticUserPool
from lma_load.run_context import RunContext
from lma_load.stack_info import LMAStackInfo

logger = logging.getLogger(__name__)
console = Console()


@dataclass
class CleanupCounts:
    cognito_users: int = 0
    vp_records: int = 0
    ddb_meetings: int = 0
    s3_objects: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.errors = self.errors or []


def run(
    ctx: RunContext,
    stack: LMAStackInfo,
    run_id_filter: str | None = None,
) -> CleanupCounts:
    """Clean up all synthetic resources tagged with ``run_id_filter``.

    If ``run_id_filter`` is ``None`` we fall back to ``ctx.run_id``. Pass
    the literal string ``"*"`` to clean up EVERY load-simulator artifact
    ever created in this stack.
    """
    run_id = run_id_filter or ctx.run_id
    is_all = run_id == "*"
    counts = CleanupCounts()

    console.rule(f"[bold]Cleanup — run_id={'ALL' if is_all else run_id}")
    if ctx.dry_run:
        console.print("[yellow]DRY RUN — no deletions will happen[/]")

    # 1) Cognito users
    if stack.user_pool_id:
        _clean_cognito(ctx, stack, None if is_all else run_id, counts)
    else:
        logger.warning("No user_pool_id — skipping Cognito cleanup")

    # 2) Virtual Participants
    if stack.vp_registry_table:
        _clean_vps(ctx, stack, None if is_all else run_id, counts)

    # 3) DDB meetings (via EventSourcing table scan)
    if stack.event_sourcing_table:
        _clean_ddb(ctx, stack, None if is_all else run_id, counts)

    # 4) S3 orphans (uploads-pending prefix)
    if stack.recordings_bucket:
        _clean_s3(ctx, stack, None if is_all else run_id, counts)

    # Render summary.
    t = Table(title="Cleanup Summary")
    t.add_column("Resource")
    t.add_column("Deleted", justify="right")
    t.add_row("Cognito users",    str(counts.cognito_users))
    t.add_row("VP records",       str(counts.vp_records))
    t.add_row("DDB meeting rows", str(counts.ddb_meetings))
    t.add_row("S3 objects",       str(counts.s3_objects))
    console.print(t)
    if counts.errors:
        console.print(f"[yellow]{len(counts.errors)} non-fatal errors — see log[/]")

    ctx.write_json("cleanup-counts.json", counts.__dict__)
    return counts


# ---------------------------------------------------------------------------
# Cognito
# ---------------------------------------------------------------------------
def _clean_cognito(
    ctx: RunContext, stack: LMAStackInfo, run_id: str | None, counts: CleanupCounts
) -> None:
    pool = SyntheticUserPool(
        user_pool_id=stack.user_pool_id,
        user_pool_client_id=stack.user_pool_client_id or "",
        region=ctx.region,
        run_id=run_id or "",
        profile=ctx.profile,
    )
    victims = pool.list_synthetic_users(run_id)
    console.print(f"Cognito users to delete: [bold]{len(victims)}[/]")
    if not ctx.dry_run and victims:
        counts.cognito_users = pool.delete_synthetic_users(run_id)
    elif ctx.dry_run:
        counts.cognito_users = len(victims)


# ---------------------------------------------------------------------------
# Virtual Participants
# ---------------------------------------------------------------------------
def _clean_vps(
    ctx: RunContext, stack: LMAStackInfo, run_id: str | None, counts: CleanupCounts
) -> None:
    session_kwargs: dict[str, Any] = {"region_name": ctx.region}
    if ctx.profile:
        session_kwargs["profile_name"] = ctx.profile
    ddb = boto3.Session(**session_kwargs).resource("dynamodb")
    table = ddb.Table(stack.vp_registry_table)
    marker = f"loadtest-{run_id}" if run_id else "loadtest-"

    victims: list[str] = []
    try:
        paginator = table.meta.client.get_paginator("scan")
        for page in paginator.paginate(
            TableName=stack.vp_registry_table,
            # Best-effort filter — VP table shape varies by LMA version
            FilterExpression="contains(#n, :m)",
            ExpressionAttributeNames={"#n": "meetingName"},
            ExpressionAttributeValues={":m": marker},
        ):
            for item in page.get("Items", []):
                vp_id = item.get("id", {}).get("S") if isinstance(item.get("id"), dict) else item.get("id")
                if vp_id:
                    victims.append(vp_id)
    except ClientError as err:
        logger.warning("VP scan failed: %s", err)

    console.print(f"VP records to delete: [bold]{len(victims)}[/]")
    if ctx.dry_run:
        counts.vp_records = len(victims)
        return
    for vp_id in victims:
        try:
            table.delete_item(Key={"id": vp_id})
            counts.vp_records += 1
        except ClientError as err:
            counts.errors.append(f"VP {vp_id}: {err}")


# ---------------------------------------------------------------------------
# DynamoDB meetings
# ---------------------------------------------------------------------------
def _clean_ddb(
    ctx: RunContext, stack: LMAStackInfo, run_id: str | None, counts: CleanupCounts
) -> None:
    """Scan the EventSourcing table for rows whose ``PK`` *or* ``SK``
    contains ``run-id=<runId>`` (for specific runs) or the generic
    ``run-id=lt-`` marker (for ``--target-run-id '*'``) and delete them in
    batches of 25.

    The canonical marker for load-simulator rows is the ``run-id=<runId>``
    suffix we embed in the callId (see
    :func:`lma_load.drivers.kinesis_injector._stable_call_id`). That callId
    shows up in ``PK`` for most rows (``c#<callId>``, ``trs#<callId>``,
    ``uj#<callId>``) **but in the SK for the meeting-list shard rows**
    (``PK=cls#<date>#s#<shard>``, ``SK=ts#<ts>#id#<callId>``). Those
    ``cls#`` rows are what the UI's meeting-list GSI queries — if we miss
    them, the Meeting List still shows stale synthetic meetings after
    cleanup even though the underlying ``c#`` rows are gone.

    Scanning on ``contains(PK, :m) OR contains(SK, :m)`` picks up every
    row shape the event-processor writes regardless of ``Owner`` (real
    Cognito emails vs synthetic ``loadtest-…`` strings; transcript-segment
    and ``cls#`` rows don't carry an ``Owner`` at all).
    """
    session_kwargs: dict[str, Any] = {"region_name": ctx.region}
    if ctx.profile:
        session_kwargs["profile_name"] = ctx.profile
    client = boto3.Session(**session_kwargs).client("dynamodb")

    if run_id:
        # Match the exact per-run marker embedded in every load-simulator
        # callId → PK / SK. contains() is required because PK/SK have
        # prefixes like "c#" / "trs#" / "cls#<date>#s#<shard>" ahead of
        # the callId.
        marker = f"run-id={run_id}"
    else:
        # --target-run-id '*' → match any load-simulator run. Every
        # synthetic callId starts with "loadtest meeting-" and embeds
        # "run-id=lt-" so that's a reliable universal marker.
        marker = "run-id=lt-"

    paginator = client.get_paginator("scan")
    victims: list[dict] = []
    try:
        for page in paginator.paginate(
            TableName=stack.event_sourcing_table,
            FilterExpression="contains(#pk, :m) OR contains(#sk, :m)",
            ExpressionAttributeNames={"#pk": "PK", "#sk": "SK"},
            ExpressionAttributeValues={":m": {"S": marker}},
            ProjectionExpression="PK, SK",
        ):
            victims.extend(page.get("Items", []))
    except ClientError as err:
        logger.warning("EventSourcing scan failed: %s", err)
        counts.errors.append(f"ddb scan: {err}")
        return

    console.print(f"DDB rows to delete: [bold]{len(victims)}[/]")
    if ctx.dry_run:
        counts.ddb_meetings = len(victims)
        return

    # BatchWriteItem caps at 25 per call.
    for chunk in _chunks(victims, 25):
        req = {
            stack.event_sourcing_table: [
                {"DeleteRequest": {"Key": {"PK": v["PK"], "SK": v["SK"]}}}
                for v in chunk
            ]
        }
        try:
            client.batch_write_item(RequestItems=req)
            counts.ddb_meetings += len(chunk)
        except ClientError as err:
            counts.errors.append(f"ddb batch_write: {err}")
        time.sleep(0.05)  # gentle pacing


# ---------------------------------------------------------------------------
# S3 orphan cleanup
# ---------------------------------------------------------------------------
def _clean_s3(
    ctx: RunContext, stack: LMAStackInfo, run_id: str | None, counts: CleanupCounts
) -> None:
    session_kwargs: dict[str, Any] = {"region_name": ctx.region}
    if ctx.profile:
        session_kwargs["profile_name"] = ctx.profile
    s3 = boto3.Session(**session_kwargs).client("s3")
    prefix_marker = f"loadtest-{run_id}-" if run_id else "loadtest-"
    pending_prefix = stack.uploads_pending_prefix or "lma-uploads-pending/"
    prefix = f"{pending_prefix}{prefix_marker}"

    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=stack.recordings_bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            keys.append(obj["Key"])
    console.print(f"S3 objects to delete: [bold]{len(keys)}[/]")
    if ctx.dry_run:
        counts.s3_objects = len(keys)
        return

    for chunk in _chunks(keys, 1000):
        try:
            s3.delete_objects(
                Bucket=stack.recordings_bucket,
                Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
            )
            counts.s3_objects += len(chunk)
        except ClientError as err:
            counts.errors.append(f"s3 delete: {err}")


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]
