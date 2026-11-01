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

By default the VP row is owned by the calling IAM principal and the VP
joins with a fresh browser profile and no stored Zoom sign-in. Pass
``as_user=`` to launch it for an LMA user instead: the SDK looks the user
up in the stack's Cognito user pool and sends the same owner and
``userSub`` / ``userZoomSub`` values the Web UI sends for that user.
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
    LmaUser,
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
    SharedWith
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
        self._user_pool_id: str | None = None

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
        user_name: str | None = None,
        wait: bool = True,
        timeout_s: float = 120.0,
        poll_interval_s: float = 2.0,
        as_user: str | None = None,
        use_zoom_credentials: bool | None = None,
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
                as the Zoom/Teams participant name). Defaults to the
                user's email when ``as_user`` is given, otherwise
                ``"loadtest@lma"``.
            wait: When ``True`` (default), poll ``getVirtualParticipant``
                until status != ``INITIALIZING`` or timeout_s elapses.
            timeout_s: Max seconds to wait for launch detection.
            poll_interval_s: Seconds between getVirtualParticipant polls.
            as_user: Email or Cognito user name of an LMA user to launch
                the VP for. The row is owned by that user (so it appears
                in their Virtual Participants list), and the VP uses the
                user's saved browser profile. Requires
                ``cognito-idp:AdminGetUser`` and ``cognito-idp:ListUsers``
                on the stack's user pool.
            use_zoom_credentials: Zoom only, requires ``as_user``.
                ``None`` (default) signs in with the user's stored Zoom
                credentials when they have saved some; ``True`` requires
                them; ``False`` joins without signing in. Checking for
                stored credentials requires ``secretsmanager:DescribeSecret``
                on ``<stack>/zoom-credentials/*``.

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
        if use_zoom_credentials is not None and not as_user:
            raise LMAVirtualParticipantError(
                "use_zoom_credentials requires as_user: stored Zoom "
                "credentials belong to an LMA user."
            )
        if use_zoom_credentials and platform != "ZOOM":
            raise LMAVirtualParticipantError(
                "use_zoom_credentials only applies to ZOOM meetings."
            )

        t0 = time.monotonic()

        # Resolve the LMA user before writing anything, so an unknown or
        # disabled user leaves no row behind.
        user: LmaUser | None = None
        user_zoom_sub = ""
        if as_user:
            user = self.resolve_user(as_user)
            if platform == "ZOOM" and use_zoom_credentials is not False:
                present = self.has_zoom_credentials(
                    user.sub, required=bool(use_zoom_credentials)
                )
                if present:
                    user_zoom_sub = user.sub
                elif use_zoom_credentials:
                    raise LMAVirtualParticipantError(
                        f"LMA user {user.username!r} has no stored Zoom "
                        f"credentials. Save them in the LMA UI first, or "
                        f"launch without use_zoom_credentials."
                    )
        if user_name is None:
            user_name = (user.email or user.username) if user else "loadtest@lma"

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
        if user:
            # Honoured by the resolver for IAM callers only; it becomes the
            # row's Owner, which is what the list/get resolvers compare
            # against the signed-in user's Cognito user name.
            vp_input["owner"] = user.username
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
                user_sub=user.sub if user else "",
                user_zoom_sub=user_zoom_sub,
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
            owner=user.username if user else None,
            user_sub=user.sub if user else None,
            zoom_credentials_used=bool(user_zoom_sub),
        )

    def resolve_user(self, email_or_username: str) -> LmaUser:
        """Look up an LMA user in the stack's Cognito user pool.

        Tries the value as a Cognito user name first, then as an email
        address. Uses the caller's IAM credentials
        (``cognito-idp:AdminGetUser``, ``cognito-idp:ListUsers``).

        Raises:
            LMAVirtualParticipantError: when the user does not exist, is
                disabled, or the email matches more than one user.
            LMAConfigurationError: when the user pool cannot be found or
                the lookup is not permitted.
        """
        ident = (email_or_username or "").strip()
        if not ident:
            raise LMAVirtualParticipantError("as_user must not be empty.")
        pool_id = self.user_pool_id
        cognito = self._client.session.client("cognito-idp")

        record: dict[str, Any] | None = None
        try:
            resp = cognito.admin_get_user(UserPoolId=pool_id, Username=ident)
            record = {
                "Username": resp.get("Username"),
                "Attributes": resp.get("UserAttributes") or [],
                "Enabled": resp.get("Enabled", True),
            }
        except Exception as err:  # noqa: BLE001
            if _error_code(err) != "UserNotFoundException":
                raise _lookup_error("cognito-idp:AdminGetUser", pool_id, err) from err

        if record is None:
            # Cognito filter strings are quoted; refuse values that would
            # change the filter rather than trying to escape them.
            if '"' in ident or "\\" in ident:
                raise LMAVirtualParticipantError(
                    f"LMA user {ident!r} not found in user pool {pool_id}."
                )
            try:
                resp = cognito.list_users(
                    UserPoolId=pool_id, Filter=f'email = "{ident}"', Limit=2
                )
            except Exception as err:  # noqa: BLE001
                raise _lookup_error("cognito-idp:ListUsers", pool_id, err) from err
            users = resp.get("Users") or []
            if len(users) > 1:
                raise LMAVirtualParticipantError(
                    f"More than one LMA user has the email {ident!r}; "
                    f"pass the Cognito user name instead."
                )
            if not users:
                raise LMAVirtualParticipantError(
                    f"LMA user {ident!r} not found in user pool {pool_id}."
                )
            record = users[0]

        attrs = {a.get("Name"): a.get("Value") for a in record.get("Attributes") or []}
        username = record.get("Username") or ""
        sub = attrs.get("sub") or ""
        if not username or not sub:
            raise LMAVirtualParticipantError(
                f"Cognito returned no user name or sub for {ident!r}."
            )
        if record.get("Enabled") is False:
            raise LMAVirtualParticipantError(
                f"LMA user {username!r} is disabled."
            )
        return LmaUser(username=username, sub=sub, email=attrs.get("email"))

    @property
    def user_pool_id(self) -> str:
        """The LMA Cognito user pool id, read from the root stack outputs."""
        if self._user_pool_id:
            return self._user_pool_id
        try:
            outputs = {k: o.value for k, o in self._client.stack.outputs().items()}
        except Exception as err:  # noqa: BLE001
            raise LMAConfigurationError(
                f"Could not read stack outputs to find the user pool: {err}"
            ) from err
        pool_id = _extract_user_pool_id(outputs)
        if not pool_id:
            raise LMAConfigurationError(
                "Could not determine the Cognito user pool id from stack "
                "outputs. Expected CognitoUserPoolTokenIssuerUrl or "
                "LocalUITestingEnv with VITE_USER_POOL_ID=<id>."
            )
        self._user_pool_id = pool_id
        return pool_id

    def has_zoom_credentials(self, user_sub: str, required: bool = False) -> bool:
        """Whether the LMA user has stored Zoom credentials.

        Checks for the ``<stack>/zoom-credentials/<sub>`` secret the LMA UI
        writes (``secretsmanager:DescribeSecret``; the secret value is not
        read). When ``required`` is ``False`` a failed check is logged and
        treated as "no credentials", so the VP still joins without signing
        in; when ``True`` it raises.

        The secret-name layout and the ``DeletedDate`` check match
        ``zoom_credentials_manager`` (which writes the secret) and the MCP
        server's ``tools/user_helper.py``; keep the three in step.
        """
        secret_name = f"{self._client._require_stack()}/zoom-credentials/{user_sub}"
        sm = self._client.session.client("secretsmanager")
        try:
            described = sm.describe_secret(SecretId=secret_name)
        except Exception as err:  # noqa: BLE001
            code = _error_code(err)
            if code in ("ResourceNotFoundException", "InvalidRequestException"):
                return False
            if required:
                raise LMAConfigurationError(
                    f"Could not check for stored Zoom credentials ({code or 'error'}); "
                    f"the caller needs secretsmanager:DescribeSecret on "
                    f"<stack>/zoom-credentials/*."
                ) from err
            logger.warning(
                "Could not check for stored Zoom credentials (%s); "
                "the VP will join without signing in to Zoom.",
                code or type(err).__name__,
            )
            return False
        return not described.get("DeletedDate")

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
        user_sub: str = "",
        user_zoom_sub: str = "",
    ) -> tuple[str | None, str | None]:
        """StartSyncExecution on the LMA VP scheduler state machine.

        The input shape matches the Web UI's launch — the scribe
        container's task role handles AppSync callbacks itself, so we
        can send empty tokens here. ``userSub`` becomes the container's
        ``LMA_USER_SUB`` (saved browser profile); a non-empty
        ``userZoomSub`` makes the state machine pass the user's stored
        Zoom credentials secret name.
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
                "userSub": user_sub,
                "userZoomSub": user_zoom_sub,
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

def _error_code(err: Exception) -> str:
    """The AWS error code of a botocore ClientError, else ''."""
    response = getattr(err, "response", None) or {}
    return str((response.get("Error") or {}).get("Code") or "")


def _lookup_error(action: str, pool_id: str, err: Exception) -> LMAConfigurationError:
    return LMAConfigurationError(
        f"Could not look up the LMA user in user pool {pool_id}; the "
        f"caller needs {action} on it: {err}"
    )


def _extract_user_pool_id(raw_outputs: dict[str, str]) -> str | None:
    """Find the Cognito user pool id in a dict of root-stack outputs."""
    issuer = (raw_outputs.get("CognitoUserPoolTokenIssuerUrl") or "").strip().rstrip("/")
    if issuer:
        candidate = issuer.rsplit("/", 1)[-1]
        if "_" in candidate:
            return candidate
    for pair in (raw_outputs.get("LocalUITestingEnv") or "").split():
        key, _, value = pair.partition("=")
        if key == "VITE_USER_POOL_ID" and value.strip():
            return value.strip()
    return None


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
