# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Virtual Participant operations (IAM-authenticated).

Mirrors what the LMA Web UI's ``EmbedVpLoader`` component does, but using
SigV4 and the Python SDK. The lifecycle is:

1. ``createVirtualParticipant`` (AppSync mutation, ``@aws_iam``) writes a
   row in the ``VirtualParticipantTable`` with status ``INITIALIZING``.
2. ``stepfunctions:StartSyncExecution`` is called on the LMA VP scheduler
   Express state machine to place an ECS task that joins the meeting.
3. (Optionally) poll ``getVirtualParticipant`` until the scribe container
   reports the first non-``INITIALIZING`` status update — this is the
   most reliable way to detect ECS capacity failures since the state
   machine uses a non-``.sync`` ``ecs:runTask`` integration and SUCCEEDS
   even on placement failure.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from lma_sdk.exceptions import (
    LMAConfigurationError,
    LMATimeoutError,
    LMAVirtualParticipantError,
)
from lma_sdk.models.virtual_participant import (
    TERMINAL_VP_STATES,
    VpLaunchResult,
    VpRow,
    VpStatus,
)

if TYPE_CHECKING:
    from lma_sdk.client import LMAClient

logger = logging.getLogger(__name__)


# ── GraphQL documents (copied verbatim from EmbedVpLoader.jsx) ────

_CREATE_MUTATION = """
mutation CreateVirtualParticipant($input: CreateVirtualParticipantInput!) {
  createVirtualParticipant(input: $input) {
    id
    meetingName
    meetingPlatform
    meetingId
    status
    createdAt
    updatedAt
    owner
    Owner
    CallId
    vncEndpoint
    vncPort
    vncReady
  }
}
""".strip()

_GET_QUERY = """
query GetVirtualParticipant($id: ID!) {
  getVirtualParticipant(id: $id) {
    id
    meetingName
    meetingPlatform
    meetingId
    meetingTime
    scheduledFor
    isScheduled
    scheduleId
    status
    createdAt
    updatedAt
    owner
    Owner
    SharedWith
    CallId
    vncEndpoint
    vncPort
    vncReady
  }
}
""".strip()

_END_MUTATION = """
mutation EndVirtualParticipant($input: EndVirtualParticipantInput!) {
  endVirtualParticipant(input: $input) {
    id
    status
    updatedAt
  }
}
""".strip()

_LIST_QUERY = """
query ListVirtualParticipants {
  listVirtualParticipants {
    id
    meetingName
    meetingPlatform
    meetingId
    status
    createdAt
    updatedAt
    owner
    CallId
  }
}
""".strip()


class VirtualParticipantOperations:
    """VP CRUD + launch namespace available as ``client.vp``."""

    def __init__(self, client: LMAClient) -> None:
        self._client = client
        self._scheduler_arn: str | None = None

    # ── Scheduler ARN resolution ──────────────────────────────

    @property
    def scheduler_state_machine_arn(self) -> str:
        """Find the LMA VP scheduler Step Function ARN.

        Convention: the AISTACK names the state machine
        ``{root-stack-name}-LMAVirtualParticipantScheduler`` (see
        ``lma-ai-stack.yaml``).  If the root stack doesn't expose this
        as an Output, we construct the ARN from account + region +
        stack name — which is what LMA's own Lambdas rely on.
        """
        if self._scheduler_arn:
            return self._scheduler_arn

        # First look for a Stack Output with the canonical name.
        try:
            outputs = self._client.stack.outputs()
            for key in (
                "LMAVirtualParticipantSchedulerStateMachine",
                "VirtualParticipantSchedulerStateMachine",
                "VirtualParticipantSchedulerStateMachineArn",
            ):
                if key in outputs and outputs[key].value:
                    self._scheduler_arn = outputs[key].value
                    return self._scheduler_arn
        except Exception as err:  # noqa: BLE001
            logger.debug("Could not read scheduler ARN from stack outputs: %s", err)

        # Fall back to convention-based construction.
        sts = self._client.session.client("sts")
        try:
            account_id = sts.get_caller_identity()["Account"]
        except Exception as err:  # noqa: BLE001
            raise LMAConfigurationError(
                f"Could not determine AWS account ID for scheduler ARN: {err}"
            ) from err

        region = self._client.region
        stack_name = self._client._require_stack()
        arn = (
            f"arn:aws:states:{region}:{account_id}:stateMachine:"
            f"{stack_name}-LMAVirtualParticipantScheduler"
        )

        # Verify the state machine actually exists — raise a clean
        # error early if not, rather than failing mysteriously during
        # StartSyncExecution.
        sfn = self._client.session.client("stepfunctions")
        try:
            sfn.describe_state_machine(stateMachineArn=arn)
        except Exception as err:  # noqa: BLE001
            raise LMAConfigurationError(
                f"VP scheduler state machine not found at expected ARN "
                f"{arn!r}. Either the stack {stack_name!r} does not exist "
                f"in region {region!r}, or the VP scheduler is not "
                f"deployed. Underlying error: {err}"
            ) from err

        self._scheduler_arn = arn
        return arn

    # ── Public API ────────────────────────────────────────────

    def create(
        self,
        meeting_name: str,
        platform: str,
        meeting_id: str,
        meeting_password: str = "",
        user_name: str = "loadtest@lma",
        wait: bool = True,
        timeout_s: float = 120.0,
        poll_interval_s: float = 2.0,
    ) -> VpLaunchResult:
        """Create and launch a Virtual Participant.

        Performs the same two-step flow as the Web UI:

        1. ``createVirtualParticipant`` mutation to write the registry row.
        2. ``StartSyncExecution`` on the LMA VP scheduler state machine.

        Args:
            meeting_name: Display name of the meeting (e.g. "Weekly sync").
            platform: ``ZOOM`` | ``TEAMS`` | ``CHIME`` | ``WEBEX``.
            meeting_id: Meeting ID (spaces stripped automatically).
            meeting_password: Meeting password (empty string if none).
            user_name: Display name the scribe reports as (shows up
                as the Zoom/Teams participant name).
            wait: When ``True`` (default), poll ``getVirtualParticipant``
                until status != ``INITIALIZING`` or timeout_s elapses.
            timeout_s: Max seconds to wait for launch detection.
            poll_interval_s: Seconds between getVirtualParticipant polls.

        Returns:
            VpLaunchResult with the VP id, status, callId (if assigned),
            and timing information.

        Raises:
            LMAVirtualParticipantError: on any failure — AppSync error,
                SFN FAILED status, placement failure, or (if wait=True)
                failure to launch within the timeout.
        """
        platform = (platform or "").upper().strip()
        if platform not in {"ZOOM", "TEAMS", "CHIME", "WEBEX"}:
            raise LMAVirtualParticipantError(
                f"Invalid platform {platform!r}. "
                f"Must be one of: ZOOM, TEAMS, CHIME, WEBEX."
            )

        meeting_id_stripped = "".join((meeting_id or "").split())
        if not meeting_name or not meeting_id_stripped:
            raise LMAVirtualParticipantError(
                "meeting_name and meeting_id are required."
            )

        t0 = time.monotonic()

        # 1) AppSync createVirtualParticipant
        logger.info(
            "Creating VP for meeting %r (%s, id=%s)",
            meeting_name, platform, meeting_id_stripped,
        )
        vp_input = {
            "meetingName": meeting_name,
            "meetingPlatform": platform,
            "meetingId": meeting_id_stripped,
            "meetingPassword": meeting_password or "",
            "status": "INITIALIZING",
        }
        try:
            data = self._client.appsync.graphql(
                query=_CREATE_MUTATION,
                variables={"input": vp_input},
            )
        except Exception as err:  # noqa: BLE001
            raise LMAVirtualParticipantError(
                f"createVirtualParticipant failed: {err}"
            ) from err

        vp = (data or {}).get("createVirtualParticipant") or {}
        vp_id = vp.get("id")
        if not vp_id:
            raise LMAVirtualParticipantError(
                "createVirtualParticipant returned no id",
                details={"response": data},
            )
        status = vp.get("status") or "INITIALIZING"
        call_id = vp.get("CallId")

        # 2) Step Functions: StartSyncExecution. On failure (SFN FAILED
        # or non-empty failures[]) best-effort end the orphan VP row so
        # it doesn't sit forever in INITIALIZING in the UI.
        try:
            sfn_execution_arn, sfn_status = self._start_scheduler(
                vp_id=vp_id,
                meeting_name=meeting_name,
                platform=platform,
                meeting_id=meeting_id_stripped,
                meeting_password=meeting_password,
                user_name=user_name,
            )
        except LMAVirtualParticipantError:
            self._best_effort_end(vp_id, reason="SDK: scheduler failed")
            raise

        # 3) Optionally poll for launch detection. On timeout also
        # best-effort end the orphan to keep the UI clean.
        waited = False
        if wait:
            try:
                final = self._wait_for_launch(vp_id, timeout_s, poll_interval_s)
            except (LMATimeoutError, LMAVirtualParticipantError):
                self._best_effort_end(
                    vp_id, reason="SDK: VP never left INITIALIZING"
                )
                raise
            status = final.status or status
            call_id = final.CallId or call_id
            waited = True

        elapsed_ms = (time.monotonic() - t0) * 1000.0
        return VpLaunchResult(
            id=vp_id,
            status=status or "INITIALIZING",
            call_id=call_id,
            meeting_name=meeting_name,
            meeting_platform=platform,
            meeting_id=meeting_id_stripped,
            elapsed_ms=round(elapsed_ms, 1),
            waited_for_launch=waited,
            sfn_execution_arn=sfn_execution_arn,
            sfn_status=sfn_status,
        )

    def get(self, vp_id: str) -> VpRow:
        """Fetch a Virtual Participant row by id."""
        data = self._client.appsync.graphql(
            query=_GET_QUERY, variables={"id": vp_id}
        )
        row = (data or {}).get("getVirtualParticipant")
        if not row:
            raise LMAVirtualParticipantError(
                f"VP {vp_id!r} not found",
                details={"response": data},
            )
        return VpRow.model_validate(row)

    def end(
        self,
        vp_id: str,
        reason: str = "SDK requested termination",
        ended_by: str = "SDK",
    ) -> VpRow:
        """End a running Virtual Participant."""
        data = self._client.appsync.graphql(
            query=_END_MUTATION,
            variables={
                "input": {
                    "id": vp_id,
                    "endReason": reason,
                    "endedBy": ended_by,
                },
            },
        )
        row = (data or {}).get("endVirtualParticipant")
        if not row:
            raise LMAVirtualParticipantError(
                f"endVirtualParticipant returned no row for {vp_id!r}",
                details={"response": data},
            )
        return VpRow.model_validate(row)

    def list(self) -> list[VpRow]:
        """List all visible Virtual Participants for the calling IAM principal."""
        data = self._client.appsync.graphql(query=_LIST_QUERY)
        rows = (data or {}).get("listVirtualParticipants") or []
        return [VpRow.model_validate(r) for r in rows if r]

    def wait_for_launch(
        self,
        vp_id: str,
        timeout_s: float = 120.0,
        poll_interval_s: float = 2.0,
    ) -> VpRow:
        """Poll ``getVirtualParticipant`` until status leaves INITIALIZING
        or the timeout elapses.

        Returns the last VpRow observed (never None).

        Raises:
            LMATimeoutError: when no status change is observed in
                ``timeout_s`` seconds.
            LMAVirtualParticipantError: when status becomes ``FAILED``.
        """
        return self._wait_for_launch(vp_id, timeout_s, poll_interval_s)

    # ── Internals ─────────────────────────────────────────────

    def _best_effort_end(self, vp_id: str, reason: str) -> None:
        """Attempt to end a VP row, swallowing any errors.

        Used during ``create(wait=True)`` after launch failure so
        orphan ``INITIALIZING`` rows don't accumulate in the UI when
        ECS placement fails or the scribe never starts.
        """
        try:
            self.end(vp_id, reason=reason, ended_by="SDK-cleanup")
            logger.info(
                "Auto-ended orphan VP %s (reason=%r)", vp_id, reason,
            )
        except Exception as err:  # noqa: BLE001
            logger.debug(
                "Best-effort end for VP %s failed: %s", vp_id, err,
            )

    def _start_scheduler(
        self,
        vp_id: str,
        meeting_name: str,
        platform: str,
        meeting_id: str,
        meeting_password: str,
        user_name: str,
    ) -> tuple[str | None, str | None]:
        """StartSyncExecution on the LMA VP scheduler state machine.

        The input shape is verbatim from EmbedVpLoader.jsx — the scribe
        container's task role handles AppSync callbacks itself, so we
        can send empty tokens here.
        """
        sfn = self._client.session.client("stepfunctions")
        state_machine_arn = self.scheduler_state_machine_arn

        sfn_input = {
            "apiInfo": {"httpMethod": "POST"},
            "data": {
                "meetingPlatform": platform,
                "meetingID": meeting_id,
                "meetingPassword": meeting_password or "",
                "meetingName": meeting_name,
                "meetingTime": "",
                "userName": user_name,
                "virtualParticipantId": vp_id,
                "accessToken": "",
                "idToken": "",
                "rereshToken": "",  # sic: matches UI spelling
            },
        }

        try:
            resp = sfn.start_sync_execution(
                stateMachineArn=state_machine_arn,
                input=json.dumps(sfn_input),
            )
        except Exception as err:  # noqa: BLE001
            raise LMAVirtualParticipantError(
                f"StartSyncExecution on {state_machine_arn} failed: {err}"
            ) from err

        status = resp.get("status")
        execution_arn = resp.get("executionArn")
        output = resp.get("output") or "{}"

        if status == "FAILED":
            detail = _extract_sfn_error(output) or resp.get("cause") or status
            raise LMAVirtualParticipantError(
                f"VP scheduler FAILED: {detail}",
                details={"output": output, "cause": resp.get("cause"),
                         "error": resp.get("error")},
            )

        # Some placement failures land inside the JSON output even on
        # SUCCEEDED (e.g. RESOURCE:MEMORY with empty task ARN).
        try:
            parsed = json.loads(output) if isinstance(output, str) else output
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            failures = parsed.get("failures") or []
            if failures:
                # Only treat as error if no task was actually started.
                tasks = parsed.get("tasks") or []
                if not tasks:
                    raise LMAVirtualParticipantError(
                        f"ECS placement failure: {failures}",
                        details={"output": parsed},
                    )

        logger.info(
            "VP scheduler started (status=%s, executionArn=%s)",
            status, execution_arn,
        )
        return execution_arn, status

    def _wait_for_launch(
        self,
        vp_id: str,
        timeout_s: float,
        poll_interval_s: float,
    ) -> VpRow:
        """Poll until VP transitions out of INITIALIZING."""
        deadline = time.monotonic() + timeout_s
        last: VpRow | None = None
        while time.monotonic() < deadline:
            try:
                row = self.get(vp_id)
            except Exception as err:  # noqa: BLE001
                logger.debug("getVirtualParticipant poll failed: %s", err)
                time.sleep(poll_interval_s)
                continue
            last = row
            status_str = (row.status or "").upper()
            if status_str and status_str != VpStatus.INITIALIZING.value:
                if status_str == VpStatus.FAILED.value:
                    raise LMAVirtualParticipantError(
                        f"VP {vp_id} reached FAILED state during launch",
                        details={"row": row.model_dump()},
                    )
                try:
                    if VpStatus(status_str) in TERMINAL_VP_STATES:
                        logger.info(
                            "VP %s reached terminal %s during launch wait",
                            vp_id, status_str,
                        )
                except ValueError:
                    pass
                return row
            time.sleep(poll_interval_s)

        if last is None:
            raise LMATimeoutError(
                f"Timed out waiting for VP {vp_id!r} — no status "
                f"available after {timeout_s}s"
            )
        raise LMATimeoutError(
            f"VP {vp_id!r} still INITIALIZING after {timeout_s}s "
            f"(last status: {last.status}). ECS task may have failed "
            f"to place — check the VP scheduler state machine execution.",
            details={"row": last.model_dump()},
        )


# ── Helpers ───────────────────────────────────────────────────

def _extract_sfn_error(output: Any) -> str | None:
    """Best-effort extraction of a human-readable error from SFN output."""
    if not output:
        return None
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            return output[:500]
    if isinstance(output, dict):
        for k in ("errorMessage", "error", "Cause", "cause"):
            v = output.get(k)
            if v:
                return str(v)
    return None
