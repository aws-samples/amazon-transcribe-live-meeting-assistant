"""MicroVM runtime lifecycle hook handlers.

Runs **inside the MicroVM** under the **execution role** (API doc §5: the runtime
hooks ``/run`` ``/resume`` ``/suspend`` ``/terminate`` run under the execution
role; the build-time hooks ``/ready`` ``/validate`` run under the build role and
are not this module's concern). The MicroVM platform invokes one hook per
lifecycle transition against the long-running server process, so the four
handlers share one :class:`LifecycleState` that persists across calls.

Why this module exists — snapshot safety (API doc §6, R9.2)
-----------------------------------------------------------
The Firecracker snapshot captured at build time is **byte-identical across every
MicroVM launched from an image version**. Any value that must be unique per
MicroVM — session IDs, secrets, crypto material — therefore MUST NOT be baked
into the snapshot; it MUST be generated **after start**, in the ``/run`` hook,
using a CSPRNG. This is the flip side of ``warmup.py``: warmup deliberately
generates *no* unique material (a fixed sine tone) so the snapshot stays
deterministic, and ``/run`` mints the per-MicroVM uniqueness once the VM is live.

The four hooks (design §7)
--------------------------
* ``/run``      — mint a CSPRNG ``session_id`` and initialise a fresh recogniser
                  stream. This is the **only** place per-MicroVM-unique values
                  are created (R9.2). Called exactly once, from a fresh snapshot.
* ``/resume``   — re-arm the idle/keepalive bookkeeping after a snapshot resume;
                  the ``session_id`` persists (same MicroVM, same session).
* ``/suspend``  — flush volatile recogniser buffers before the suspend snapshot.
* ``/terminate``— final flush + cleanup, and emit lifecycle/cost metrics (R10).

Testability (matching the sibling modules' conventions)
-------------------------------------------------------
Like ``ws_server`` (injected ``WebSocketConnection`` + ``recognizer_factory``) and
``warmup`` (injected ``RecognizerFactory``), the hooks drive small injected
surfaces so unit tests need no model weights and no real MicroVM: a
``recognizer_factory`` (a scripted fake recogniser stands in), a
``session_id_factory`` (asserted CSPRNG-unique across simulated starts), and a
``clock`` (a fake clock makes the active/suspended-seconds metrics deterministic).
Importing this module pulls in no ``sherpa_onnx`` / ``numpy`` — the real backend
is loaded lazily only when the default factory actually builds a recogniser.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4

from asr_server.recognizer import (
    Recognizer,
    SherpaOnlineRecognizer,
    build_model_config,
)

__all__ = [
    "Phase",
    "LifecycleMetrics",
    "LifecycleState",
    "HookResult",
    "RecognizerFactory",
    "SessionIdFactory",
    "Clock",
    "LifecycleHooks",
]

_LOG = logging.getLogger("asr_server.lifecycle_hooks")

# The four runtime hook paths the MicroVM platform invokes (API doc §5/§6). Used
# by :meth:`LifecycleHooks.dispatch` to route an inbound hook call by path.
HOOK_RUN = "/run"
HOOK_RESUME = "/resume"
HOOK_SUSPEND = "/suspend"
HOOK_TERMINATE = "/terminate"


# --- Injected surfaces ------------------------------------------------------

# Builds the per-MicroVM recogniser stream in ``/run``. Parameterless because the
# hook fires before any client connects (the wire ``Config`` is negotiated later,
# per connection, by ``ws_server``); ``/run`` warms a default stream. Injected so
# tests supply a scripted fake and production supplies a real sherpa-onnx engine.
RecognizerFactory = Callable[[], Recognizer]

# Mints the per-MicroVM-unique session id. MUST be a CSPRNG (R9.2). Injected so
# tests can assert uniqueness / substitute a deterministic id.
SessionIdFactory = Callable[[], str]

# Monotonic seconds source for the active-vs-suspended cost metrics (R10.2).
# Injected so tests drive it with a fake clock; production uses ``time.monotonic``.
Clock = Callable[[], float]


def _default_session_id() -> str:
    """Mint a CSPRNG session id (R9.2), matching ``ws_server``'s approach.

    ``uuid4`` draws its randomness from ``os.urandom`` — a CSPRNG — so the id is
    unpredictable and unique per call, exactly what a per-MicroVM value must be
    (API doc §6). Uses ``uuid4().hex`` to match ``ws_server.AsrSession`` so ids
    look identical whichever layer mints them.
    """
    return uuid4().hex


def _default_recognizer_factory() -> Recognizer:
    """Build the real streaming recogniser from ``ASR_MODEL_*`` env (NFR5).

    Delegates config resolution to :func:`recognizer.build_model_config` — the
    same source of truth the server factory and build-time warmup use — so the
    stream ``/run`` initialises matches what the snapshot was warmed with. Fails
    closed (``RuntimeError``) until the model + aarch64 wheels are provisioned;
    :meth:`LifecycleHooks.run` reports that as a failed hook.
    """
    return SherpaOnlineRecognizer.from_model(build_model_config())


# --- State ------------------------------------------------------------------


class Phase(str, Enum):
    """The MicroVM lifecycle phase this process is in.

    ``CREATED`` is the pre-``/run`` state a fresh snapshot resumes into — note
    ``session_id`` is ``None`` here, the observable proof that no per-MicroVM
    value was baked into the snapshot (R9.2). Transitions:
    ``CREATED →(run)→ RUNNING ⇄(suspend/resume)⇄ SUSPENDED``, and any non-terminal
    phase ``→(terminate)→ TERMINATED``.
    """

    CREATED = "created"
    RUNNING = "running"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"


@dataclass
class LifecycleMetrics:
    """Cost/observability counters emitted at ``/terminate`` (R10.1, R10.2).

    ``active_seconds`` / ``suspended_seconds`` are the cost-relevant split (R10.2:
    "$0 compute while suspended" — API doc §7); ``suspends`` / ``resumes`` count
    the lifecycle-hook events (R10.1).
    """

    active_seconds: float = 0.0
    suspended_seconds: float = 0.0
    suspends: int = 0
    resumes: int = 0


@dataclass
class LifecycleState:
    """Mutable per-MicroVM lifecycle state shared across the four hook calls.

    Held by one :class:`LifecycleHooks` instance for the life of the process (the
    hooks are separate inbound calls to the same long-running server). ``phase``
    gates which transitions are legal; ``session_id`` is ``None`` until ``/run``
    mints it (R9.2).
    """

    phase: Phase = Phase.CREATED
    session_id: str | None = None
    recognizer: Recognizer | None = None
    metrics: LifecycleMetrics = field(default_factory=LifecycleMetrics)
    # Monotonic timestamp the current phase began, used to accumulate the
    # active/suspended split; ``None`` until ``/run`` starts the clock.
    phase_since: float | None = None
    # Monotonic timestamp of the last liveness re-arm (``/run`` / ``/resume``),
    # the idle-clock anchor the router's idle-policy conceptually keys off (R8.3).
    last_activity: float | None = None


@dataclass
class HookResult:
    """The outcome of one hook invocation, returned to the platform/caller.

    ``ok`` is ``False`` when the hook could not perform its transition (a failed
    ``/run`` recogniser build, or an illegal transition for the current phase);
    the caller can surface that as a hook failure. ``session_id`` and ``phase``
    echo the post-hook state for logging/observability.
    """

    hook: str
    ok: bool
    phase: Phase
    session_id: str | None = None
    detail: str = ""


# --- Hooks ------------------------------------------------------------------


class LifecycleHooks:
    """The ``/run`` ``/resume`` ``/suspend`` ``/terminate`` handlers over one state.

    Instantiate **once** per process (the state must persist across the separate
    hook calls) and route inbound calls to :meth:`dispatch` (or the named methods
    directly). All dependencies are injected so the whole lifecycle is exercisable
    with fakes — no model weights, no real MicroVM, deterministic metrics.
    """

    def __init__(
        self,
        *,
        recognizer_factory: RecognizerFactory = _default_recognizer_factory,
        session_id_factory: SessionIdFactory = _default_session_id,
        clock: Clock = time.monotonic,
        state: LifecycleState | None = None,
    ) -> None:
        self._recognizer_factory = recognizer_factory
        self._session_id_factory = session_id_factory
        self._clock = clock
        self.state = state if state is not None else LifecycleState()

    # --- /run ---------------------------------------------------------------

    def run(self) -> HookResult:
        """Handle ``/run``: mint the CSPRNG session id + init a fresh stream (R9.2).

        This is the sole point per-MicroVM-unique material is created, and it runs
        **after** the VM starts — never at build/snapshot time (API doc §6). Fires
        once from a fresh (``CREATED``) snapshot; a second ``/run`` is rejected so a
        live session's id can never be silently re-minted.
        """
        if self.state.phase is not Phase.CREATED:
            return self._reject(HOOK_RUN, f"/run is only valid from {Phase.CREATED.value}")

        # Mint the unique id FIRST so it exists even if the recogniser build fails
        # — the failure detail can then be correlated to this MicroVM's session.
        session_id = self._session_id_factory()
        try:
            recognizer = self._recognizer_factory()
        except Exception as exc:  # noqa: BLE001 - a failed stream init must fail /run cleanly
            _LOG.exception("/run: recogniser init failed for session %s", session_id)
            # Stay in CREATED (no partial transition) so the platform sees the VM
            # never reached RUNNING; report the failure for the build/run log.
            return HookResult(
                hook=HOOK_RUN,
                ok=False,
                phase=self.state.phase,
                session_id=None,
                detail=f"recognizer init failed: {exc}",
            )

        now = self._clock()
        self.state.session_id = session_id
        self.state.recognizer = recognizer
        self.state.phase = Phase.RUNNING
        self.state.phase_since = now
        self.state.last_activity = now
        _LOG.info("/run: session %s started (recogniser warm)", session_id)
        return self._result(HOOK_RUN)

    # --- /resume ------------------------------------------------------------

    def resume(self) -> HookResult:
        """Handle ``/resume``: re-arm idle/keepalive bookkeeping after a resume.

        The suspended snapshot restores this exact process — so the ``session_id``
        and recogniser persist (a resume is NOT a new session, and must NOT re-mint
        the id). We only account the suspended interval to the cost split (R10.2)
        and reset the idle-clock anchor. The WS ping keepalive itself is owned by
        the ``websockets`` layer (``ws_server.serve_asr``); "re-arm" here means
        resetting the liveness timestamp the idle policy keys off (R8.3).
        """
        if self.state.phase is not Phase.SUSPENDED:
            return self._reject(HOOK_RESUME, f"/resume is only valid from {Phase.SUSPENDED.value}")

        now = self._clock()
        self._accumulate(now)  # bank the suspended interval before flipping phase
        self.state.phase = Phase.RUNNING
        self.state.phase_since = now
        self.state.last_activity = now
        self.state.metrics.resumes += 1
        _LOG.info("/resume: session %s re-armed", self.state.session_id)
        return self._result(HOOK_RESUME)

    # --- /suspend -----------------------------------------------------------

    def suspend(self) -> HookResult:
        """Handle ``/suspend``: flush volatile recogniser buffers before snapshot.

        Draining the recogniser's pending decode state keeps the suspend snapshot
        from capturing half-formed volatile buffers. The flush is best-effort — a
        flush failure must not block the suspend (mirrors ``ws_server`` swallowing
        flush errors during finalization); the emitted events are discarded because
        no client is attached at idle-suspend time.
        """
        if self.state.phase is not Phase.RUNNING:
            return self._reject(HOOK_SUSPEND, f"/suspend is only valid from {Phase.RUNNING.value}")

        self._flush_recognizer()
        now = self._clock()
        self._accumulate(now)  # bank the active interval before flipping phase
        self.state.phase = Phase.SUSPENDED
        self.state.phase_since = now
        self.state.metrics.suspends += 1
        _LOG.info("/suspend: session %s flushed and suspended", self.state.session_id)
        return self._result(HOOK_SUSPEND)

    # --- /terminate ---------------------------------------------------------

    def terminate(self) -> HookResult:
        """Handle ``/terminate``: final flush + cleanup, emit lifecycle metrics (R10).

        Idempotent-safe from any non-terminal phase (a terminate may arrive while
        RUNNING or already SUSPENDED). Banks the final interval to the right side of
        the cost split, flushes and releases the recogniser, and logs the
        active/suspended-seconds summary. A second ``/terminate`` is a no-op success.
        """
        if self.state.phase is Phase.TERMINATED:
            return self._result(HOOK_TERMINATE)  # idempotent: already cleaned up

        self._flush_recognizer()
        self._accumulate(self._clock())  # bank the final interval (active or suspended)
        self.state.recognizer = None  # release the stream / model handles
        self.state.phase = Phase.TERMINATED
        self.state.phase_since = None
        m = self.state.metrics
        _LOG.info(
            "/terminate: session %s cleaned up — active=%.3fs suspended=%.3fs "
            "suspends=%d resumes=%d",
            self.state.session_id,
            m.active_seconds,
            m.suspended_seconds,
            m.suspends,
            m.resumes,
        )
        return self._result(HOOK_TERMINATE)

    # --- dispatch -----------------------------------------------------------

    def dispatch(self, hook: str) -> HookResult:
        """Route an inbound hook call by its path (``/run`` … ``/terminate``).

        Lets an HTTP glue layer forward the hook path straight through without a
        four-way branch of its own. An unknown path is reported as a failed hook
        rather than raising, so a malformed platform call can't crash the process.
        """
        handler = {
            HOOK_RUN: self.run,
            HOOK_RESUME: self.resume,
            HOOK_SUSPEND: self.suspend,
            HOOK_TERMINATE: self.terminate,
        }.get(hook)
        if handler is None:
            return HookResult(
                hook=hook,
                ok=False,
                phase=self.state.phase,
                session_id=self.state.session_id,
                detail=f"unknown lifecycle hook: {hook!r}",
            )
        return handler()

    # --- helpers ------------------------------------------------------------

    def _accumulate(self, now: float) -> None:
        """Bank the elapsed time in the current phase to the cost split (R10.2).

        RUNNING time accrues to ``active_seconds``; SUSPENDED time to
        ``suspended_seconds`` (which bills as $0 compute — API doc §7). No-ops if
        the phase clock was never started (e.g. terminate before run).
        """
        since = self.state.phase_since
        if since is None:
            return
        elapsed = max(0.0, now - since)
        if self.state.phase is Phase.RUNNING:
            self.state.metrics.active_seconds += elapsed
        elif self.state.phase is Phase.SUSPENDED:
            self.state.metrics.suspended_seconds += elapsed

    def _flush_recognizer(self) -> None:
        """Best-effort drain of the recogniser's pending buffers (events discarded)."""
        recognizer = self.state.recognizer
        if recognizer is None:
            return
        try:
            recognizer.flush()
        except Exception:  # noqa: BLE001 - a flush failure must not block suspend/terminate
            _LOG.warning("recogniser flush failed during lifecycle transition", exc_info=True)

    def _result(self, hook: str) -> HookResult:
        """Build a success :class:`HookResult` echoing the post-hook state."""
        return HookResult(
            hook=hook,
            ok=True,
            phase=self.state.phase,
            session_id=self.state.session_id,
        )

    def _reject(self, hook: str, detail: str) -> HookResult:
        """Build a failed :class:`HookResult` for an illegal transition (no state change)."""
        _LOG.warning("%s rejected in phase %s: %s", hook, self.state.phase.value, detail)
        return HookResult(
            hook=hook,
            ok=False,
            phase=self.state.phase,
            session_id=self.state.session_id,
            detail=detail,
        )
