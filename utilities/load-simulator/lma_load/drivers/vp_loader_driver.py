# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Virtual Participant driver — API-only load generator.

Drives N concurrent Virtual Participants against the LMA VP scheduler
using pure AWS API calls (AppSync SigV4 + Step Functions
``StartSyncExecution``). No browser, no Cognito sign-in — the operator's
local AWS credentials must have the IAM permissions documented in
``README.md``:

* ``appsync:GraphQL`` on the LMA AppSync API
* ``states:StartSyncExecution`` on the VP scheduler state machine
* ``states:DescribeStateMachine`` (used for ARN resolution fallback)
* ``cloudformation:DescribeStacks`` (to resolve the AppSync URL)

The driver rotates through a pool of real meeting invitations (Zoom /
Teams / Chime / Webex) provided via ``--meeting-ids-file meetings.yaml``,
which mirrors how an operator would populate the Web UI's "Join Now"
form.  Each meeting gets its own VP; after ``--duration`` seconds each
VP is automatically ended via ``endVirtualParticipant`` (best-effort) so
the scribe containers don't linger past the requested duration. If the
end call fails, the error is captured in the result record and the VP
can still be cleaned up with ``lma vp end --id <vp-id>`` or via the
``lma load cleanup`` scenario.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from lma_load.run_context import RunContext
from lma_load.stack_info import LMAStackInfo
from lma_sdk import LMAClient
from lma_sdk.exceptions import LMAError

logger = logging.getLogger(__name__)


@dataclass
class MeetingInvite:
    name: str
    platform: str      # ZOOM | TEAMS | CHIME | WEBEX
    id: str
    password: str = ""


def _load_meetings_yaml(path: Path) -> list[MeetingInvite]:
    data = yaml.safe_load(path.read_text())
    meetings_raw = data.get("meetings") if isinstance(data, dict) else data
    if not meetings_raw:
        raise ValueError(f"No meetings found in {path}")
    out: list[MeetingInvite] = []
    for m in meetings_raw:
        out.append(
            MeetingInvite(
                name=str(m.get("name", "LoadTest Meeting")),
                platform=str(m["platform"]).upper(),
                id=str(m["id"]),
                password=str(m.get("password") or ""),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Driver entry
# ---------------------------------------------------------------------------
async def drive_vp(ctx: RunContext, stack: LMAStackInfo, params) -> list[dict]:
    if not params.meeting_ids_file:
        raise RuntimeError("--meeting-ids-file is required for the vp driver.")

    invites = _load_meetings_yaml(
        Path(params.meeting_ids_file).expanduser().resolve()
    )
    logger.info("Loaded %d meeting invites; will round-robin", len(invites))

    if params.email_prefix or params.email_domain:
        logger.info(
            "Note: --email-prefix / --email-domain are no-ops for the API-only "
            "vp driver (the caller's AWS credentials are used directly)."
        )

    client = LMAClient(
        stack_name=ctx.stack_name,
        region=ctx.region,
        profile=ctx.profile,
    )
    # Warm up — surface config errors (missing IAM perms, wrong stack name)
    # before we fan out into asyncio tasks.
    try:
        _ = client.appsync.graphql_url
        _ = client.vp.scheduler_state_machine_arn
    except LMAError as err:
        raise RuntimeError(
            f"VP driver configuration error: {err}"
        ) from err

    invites_cycle = itertools.cycle(invites)
    semaphore = asyncio.Semaphore(params.concurrency)
    rng = random.Random(ctx.run_id)
    loop = asyncio.get_event_loop()

    async def _one(idx: int) -> dict:
        # Ramp-in: spread starts across ramp_s plus a small jitter.
        await asyncio.sleep(
            (idx / max(params.meetings, 1)) * params.ramp_s
            + rng.random() * params.jitter_s
        )
        invite = next(invites_cycle)
        # Make every VP's display name unique so they don't collide in
        # the Web UI's Virtual Participants list, and so the cleanup
        # scenario can filter by ``meetingName contains <run_id>``.
        unique_meeting_name = (
            f"{invite.name} [{ctx.run_id} #{idx:04d}]"
        )
        # ``user_name`` is what the scribe container sends to Zoom /
        # Teams / Chime / Webex as its display name inside the meeting —
        # so when many VPs join the same room we need globally-unique
        # names, not just "LoadTest VP #1" that collides across runs.
        unique_user_name = f"LoadTest VP {ctx.run_id}-{idx:04d}"
        t0 = time.monotonic()
        async with semaphore:
            try:
                result = await loop.run_in_executor(
                    None,
                    lambda: client.vp.create(
                        meeting_name=unique_meeting_name,
                        platform=invite.platform,
                        meeting_id=invite.id,
                        meeting_password=invite.password,
                        user_name=unique_user_name,
                        wait=True,
                        timeout_s=120.0,
                    ),
                )
            except Exception as err:  # noqa: BLE001
                return {
                    "status": "error",
                    "error": str(err),
                    "meeting": unique_meeting_name,
                    "invite": invite.name,
                    "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
                }

            # Hold the VP in the meeting for the requested duration,
            # then end it so it doesn't linger past --duration. The
            # end call is best-effort: failures are logged and surfaced
            # in the result record, but don't flip the status to error.
            ended = False
            end_error: str | None = None
            try:
                try:
                    await asyncio.sleep(params.duration_s)
                finally:
                    try:
                        await loop.run_in_executor(
                            None,
                            lambda: client.vp.end(
                                result.id,
                                reason=(
                                    f"LoadTest duration "
                                    f"({params.duration_s:.0f}s) elapsed"
                                ),
                                ended_by="lma-load-simulator",
                            ),
                        )
                        ended = True
                    except Exception as err:  # noqa: BLE001
                        end_error = str(err)
                        logger.warning(
                            "Failed to end VP %s: %s", result.id, err,
                        )
            except asyncio.CancelledError:
                # Propagate cancellation after best-effort end above.
                raise

            out_rec: dict = {
                "status": "ok",
                "vpId": result.id,
                "callId": result.call_id,
                "vpStatus": result.status,
                "meeting": unique_meeting_name,
                "invite": invite.name,
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
                "launch_ms": result.elapsed_ms,
                "ended": ended,
            }
            if end_error:
                out_rec["end_error"] = end_error
            return out_rec

    tasks = [
        asyncio.create_task(_one(i))
        for i in range(1, params.meetings + 1)
    ]
    out: list[dict] = []
    for t in asyncio.as_completed(tasks):
        try:
            out.append(await t)
        except Exception as err:  # noqa: BLE001
            logger.exception("vp driver meeting failed: %s", err)
            out.append({"status": "error", "error": str(err)})
    return out
