"""Unit tests for the MicroVM runtime lifecycle hooks.

The ``/run`` ``/resume`` ``/suspend`` and ``/terminate`` handlers work, and IDs
are unique across simulated starts.

Following the sibling modules' convention (``test_ws_server`` / ``test_warmup``),
the hooks drive small injected surfaces so no model weights and no real MicroVM
are needed: a scripted fake recogniser, an injected session-id factory (whose
CSPRNG-uniqueness is asserted directly), and a fake monotonic clock (so the
active-vs-suspended cost metrics are deterministic).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest
from asr_server.lifecycle_hooks import (
    LifecycleHooks,
    LifecycleState,
    Phase,
    SessionIdFactory,
    _default_session_id,
)
from asr_server.recognizer import Event, Recognizer

# --- Fakes ------------------------------------------------------------------


class ScriptedRecognizer(Recognizer):
    """A fake :class:`Recognizer` recording flushes — no weights needed.

    ``flush_raises`` makes :meth:`flush` raise, exercising the hooks' best-effort
    flush guard (a flush failure must not block suspend/terminate).
    """

    def __init__(self, *, flush_raises: bool = False) -> None:
        self.flush_calls = 0
        self._flush_raises = flush_raises

    def accept_pcm(self, pcm: bytes) -> list[Event]:  # pragma: no cover - unused here
        return []

    def flush(self) -> list[Event]:
        self.flush_calls += 1
        if self._flush_raises:
            raise RuntimeError("flush blew up")
        return []


class FakeClock:
    """A monotonic clock the tests advance by hand for deterministic metrics."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _hooks(
    *,
    recognizer: Recognizer | None = None,
    session_id_factory: SessionIdFactory | None = None,
    clock: FakeClock | None = None,
) -> tuple[LifecycleHooks, ScriptedRecognizer]:
    """Build a :class:`LifecycleHooks` wired to fakes; return it + the recogniser."""
    rec = recognizer if recognizer is not None else ScriptedRecognizer()
    hooks = LifecycleHooks(
        recognizer_factory=lambda: rec,
        session_id_factory=session_id_factory or _default_session_id,
        clock=clock or FakeClock(),
    )
    return hooks, rec  # type: ignore[return-value]


# --- /run: CSPRNG session id + fresh stream (R9.2) --------------------------


def test_run_mints_session_id_and_starts_stream() -> None:
    hooks, rec = _hooks()
    result = hooks.run()
    assert result.ok
    assert result.hook == "/run"
    assert result.phase is Phase.RUNNING
    assert result.session_id is not None
    assert hooks.state.session_id == result.session_id
    assert hooks.state.recognizer is rec


def test_created_snapshot_has_no_session_id_until_run() -> None:
    """Snapshot safety (R9.2): a fresh (CREATED) VM carries NO per-VM value."""
    hooks, _rec = _hooks()
    assert hooks.state.phase is Phase.CREATED
    assert hooks.state.session_id is None  # nothing baked into the snapshot


def test_session_ids_unique_across_simulated_starts() -> None:
    """Acceptance: unique IDs across simulated MicroVM starts (R9.2 CSPRNG).

    Each fresh MicroVM is a new :class:`LifecycleHooks` over the default (real,
    ``uuid4``-based) CSPRNG id factory; 100 simulated ``/run`` starts must yield
    100 distinct ids.
    """
    ids = set()
    for _ in range(100):
        hooks = LifecycleHooks(recognizer_factory=lambda: ScriptedRecognizer())
        result = hooks.run()
        assert result.session_id is not None
        ids.add(result.session_id)
    assert len(ids) == 100


def test_default_session_id_is_csprng_hex_and_unique() -> None:
    """The default id factory yields uuid4 hex (32 hex chars), unique per call."""
    ids = {_default_session_id() for _ in range(100)}
    assert len(ids) == 100
    for sid in ids:
        assert len(sid) == 32
        int(sid, 16)  # pure hex, matching ws_server's uuid4().hex


def test_run_is_rejected_when_already_running() -> None:
    """A second /run must NOT silently re-mint a live session's id."""
    hooks, _rec = _hooks()
    first = hooks.run()
    again = hooks.run()
    assert not again.ok
    assert again.phase is Phase.RUNNING
    assert hooks.state.session_id == first.session_id  # id unchanged


def test_run_failure_stays_created_and_reports_detail() -> None:
    """A failing recogniser build fails /run cleanly; VM never reaches RUNNING."""

    def boom() -> Recognizer:
        raise RuntimeError("sherpa-onnx is not installed")

    hooks = LifecycleHooks(recognizer_factory=boom)
    result = hooks.run()
    assert not result.ok
    assert result.phase is Phase.CREATED
    assert result.session_id is None
    assert "sherpa-onnx is not installed" in result.detail
    assert hooks.state.recognizer is None


# --- /resume: re-arm, id persists -------------------------------------------


def test_resume_rearms_and_keeps_session_id() -> None:
    """A resume is the SAME session — the id must persist, not re-mint (design §7)."""
    clock = FakeClock()
    hooks, _rec = _hooks(clock=clock)
    hooks.run()
    original_id = hooks.state.session_id
    hooks.suspend()
    result = hooks.resume()
    assert result.ok
    assert result.phase is Phase.RUNNING
    assert result.session_id == original_id  # NOT re-minted
    assert hooks.state.metrics.resumes == 1


def test_resume_rejected_when_not_suspended() -> None:
    hooks, _rec = _hooks()
    hooks.run()  # RUNNING, not SUSPENDED
    result = hooks.resume()
    assert not result.ok
    assert result.phase is Phase.RUNNING


# --- /suspend: flush ---------------------------------------------------------


def test_suspend_flushes_and_transitions() -> None:
    hooks, rec = _hooks()
    hooks.run()
    result = hooks.suspend()
    assert result.ok
    assert result.phase is Phase.SUSPENDED
    assert rec.flush_calls == 1
    assert hooks.state.metrics.suspends == 1


def test_suspend_flush_failure_does_not_block_suspend() -> None:
    """A flush error is swallowed — the suspend still completes (best-effort)."""
    rec = ScriptedRecognizer(flush_raises=True)
    hooks, _rec = _hooks(recognizer=rec)
    hooks.run()
    result = hooks.suspend()
    assert result.ok
    assert result.phase is Phase.SUSPENDED
    assert rec.flush_calls == 1


def test_suspend_rejected_when_not_running() -> None:
    hooks, _rec = _hooks()
    result = hooks.suspend()  # still CREATED
    assert not result.ok
    assert result.phase is Phase.CREATED


# --- /terminate: cleanup + metrics ------------------------------------------


def test_terminate_flushes_releases_and_transitions() -> None:
    hooks, rec = _hooks()
    hooks.run()
    result = hooks.terminate()
    assert result.ok
    assert result.phase is Phase.TERMINATED
    assert rec.flush_calls == 1
    assert hooks.state.recognizer is None  # stream/model handles released


def test_terminate_from_suspended() -> None:
    """Terminate is valid straight from SUSPENDED (idle reclaim path, R8.3)."""
    hooks, _rec = _hooks()
    hooks.run()
    hooks.suspend()
    result = hooks.terminate()
    assert result.ok
    assert result.phase is Phase.TERMINATED


def test_terminate_is_idempotent() -> None:
    hooks, rec = _hooks()
    hooks.run()
    hooks.terminate()
    again = hooks.terminate()
    assert again.ok
    assert again.phase is Phase.TERMINATED
    assert rec.flush_calls == 1  # not flushed again after release


def test_terminate_before_run_is_safe() -> None:
    """Terminate from CREATED (never ran) must not crash and needs no clock."""
    hooks, _rec = _hooks()
    result = hooks.terminate()
    assert result.ok
    assert result.phase is Phase.TERMINATED


# --- Cost metrics: active vs suspended seconds (R10.2) -----------------------


def test_active_and_suspended_seconds_split() -> None:
    """The cost split banks RUNNING time to active, SUSPENDED time to suspended."""
    clock = FakeClock()
    hooks, _rec = _hooks(clock=clock)
    hooks.run()  # t=0, RUNNING
    clock.advance(10.0)
    hooks.suspend()  # banks 10s active
    clock.advance(30.0)
    hooks.resume()  # banks 30s suspended
    clock.advance(5.0)
    hooks.terminate()  # banks 5s active

    m = hooks.state.metrics
    assert m.active_seconds == pytest.approx(15.0)
    assert m.suspended_seconds == pytest.approx(30.0)
    assert m.suspends == 1
    assert m.resumes == 1


def test_terminate_logs_metrics_summary(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    hooks, _rec = _hooks(clock=clock)
    hooks.run()
    clock.advance(2.0)
    with caplog.at_level(logging.INFO, logger="asr_server.lifecycle_hooks"):
        hooks.terminate()
    assert any("cleaned up" in r.getMessage() for r in caplog.records)
    assert any("active=" in r.getMessage() for r in caplog.records)


# --- dispatch ---------------------------------------------------------------


def test_dispatch_routes_full_lifecycle() -> None:
    clock = FakeClock()
    hooks, rec = _hooks(clock=clock)
    assert hooks.dispatch("/run").phase is Phase.RUNNING
    assert hooks.dispatch("/suspend").phase is Phase.SUSPENDED
    assert hooks.dispatch("/resume").phase is Phase.RUNNING
    assert hooks.dispatch("/terminate").phase is Phase.TERMINATED
    assert rec.flush_calls >= 1


def test_dispatch_unknown_hook_is_failed_not_raised() -> None:
    hooks, _rec = _hooks()
    result = hooks.dispatch("/bogus")
    assert not result.ok
    assert "unknown lifecycle hook" in result.detail
    assert result.hook == "/bogus"


# --- injected state ----------------------------------------------------------


def test_state_can_be_injected() -> None:
    """A caller may seed the shared state (e.g. to resume from a known phase)."""
    state = LifecycleState(phase=Phase.CREATED)
    hooks = LifecycleHooks(recognizer_factory=lambda: ScriptedRecognizer(), state=state)
    assert hooks.state is state
    hooks.run()
    assert state.phase is Phase.RUNNING


# --- import inertness (mirrors warmup/recognizer) ----------------------------


def test_module_import_is_dependency_free() -> None:
    """Importing the hooks must not require ``sherpa_onnx`` / ``numpy`` (lazy backend).

    The build tooling imports the module before the ARM inference wheels exist.
    """
    code = (
        "import sys; "
        "sys.modules['sherpa_onnx'] = None; "
        "sys.modules['numpy'] = None; "
        "import asr_server.lifecycle_hooks as h; "
        # Instantiate + mint a CSPRNG id without ever touching the (stubbed-out)
        # inference wheels — the id-mint path is what must stay dependency-free.
        "hooks = h.LifecycleHooks(recognizer_factory=object); "
        "assert h._default_session_id() and hooks.state.session_id is None; "
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
