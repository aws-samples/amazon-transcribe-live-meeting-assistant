# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for MicroVM termination in the Virtual Participant manager.

Why this file exists: ending a meeting had NO effect on a MicroVM-hosted VP. The
manager only knew ecs:StopTask, and a MicroVM registry row has no taskArn, so
termination silently no-opped and the MicroVM ran until its 8-hour duration
ceiling. One was found alive 93 minutes after a failed Zoom join.

No AWS calls: the MicroVM client is stubbed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

MANAGER_DIR = (
    Path(__file__).resolve().parents[2]
    / "lma-ai-stack"
    / "source"
    / "lambda_functions"
    / "virtual_participant_manager"
)
sys.path.insert(0, str(MANAGER_DIR))

from microvm_client import MicrovmError  # noqa: E402
from microvm_manager import MicrovmManager  # noqa: E402


class _FakeClient:
    """Records terminate calls and replays a scripted sequence of outcomes."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def terminate_microvm(self, microvm_identifier: str) -> dict:
        self.calls.append(microvm_identifier)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return {}


def _manager(outcomes: list[object]) -> tuple[MicrovmManager, _FakeClient]:
    manager = MicrovmManager()
    client = _FakeClient(outcomes)
    manager._client = client  # noqa: SLF001 - injecting the stub is the point
    return manager, client


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries sleep 2s each; tests must not actually wait."""
    monkeypatch.setattr("microvm_manager.time.sleep", lambda _s: None)


def test_terminates_the_microvm_recorded_in_the_registry() -> None:
    manager, client = _manager([None])
    assert manager.terminate_microvm("microvm-abc", "vp-1") is True
    assert client.calls == ["microvm-abc"]


def test_retries_a_transient_502_rather_than_leaking_the_microvm() -> None:
    """TerminateMicrovm was observed returning a transient 502 Bad Gateway.

    It returned an HTML error page (not a modelled error) while GetMicrovm on the
    same URI succeeded, and an immediate retry worked. Giving up on the first
    failure would leak a MicroVM for up to 8 hours.
    """
    manager, client = _manager(
        [MicrovmError("DELETE failed (502): <html>...", status=502), None]
    )
    assert manager.terminate_microvm("microvm-abc", "vp-1") is True
    assert len(client.calls) == 2


def test_already_terminated_counts_as_success() -> None:
    """A 404 means the goal is met.

    The MicroVM may have hit its own duration limit or been terminated by an
    earlier attempt; reporting failure would block the registry cleanup and
    leave a stale row behind.
    """
    manager, _ = _manager([MicrovmError("not found", status=404)])
    assert manager.terminate_microvm("microvm-gone", "vp-1") is True


def test_reports_failure_after_exhausting_retries() -> None:
    """Must return False so the caller does not delete the registry row.

    Keeping the row is what makes a later retry (or a human) able to find the
    MicroVM that is still billing.
    """
    errors = [MicrovmError("boom", status=500) for _ in range(6)]
    manager, client = _manager(errors)
    assert manager.terminate_microvm("microvm-abc", "vp-1") is False
    assert len(client.calls) == 4, "should stop after the configured attempts"


def test_missing_microvm_id_is_a_failure_not_a_crash() -> None:
    """A registry row with neither taskArn nor microvmId is a bug, but the
    manager must still go on to update the VP's status."""
    manager, client = _manager([None])
    assert manager.terminate_microvm("", "vp-1") is False
    assert client.calls == []


def test_unexpected_errors_do_not_propagate() -> None:
    """The status update after termination matters more than the termination.

    If this raised, the VP would be left showing ACTIVE forever in the UI.
    """
    manager, _ = _manager([RuntimeError("something unmodelled")])
    assert manager.terminate_microvm("microvm-abc", "vp-1") is False


def test_manager_dispatches_on_microvmid_before_taskarn() -> None:
    """The dispatch is on the registry contents, not a launch-type setting.

    A stack whose VPLaunchType changed mid-meeting must still terminate the VPs
    that are already running under the previous host.
    """
    index_src = (MANAGER_DIR / "index.py").read_text()
    assert 'microvm_id = task_details.get("microvmId")' in index_src
    assert "if microvm_id:" in index_src
    assert "self.microvm_manager.terminate_microvm(" in index_src
    # The ECS path must stay in the else branch so an ECS VP is unaffected.
    assert "self.ecs_manager.stop_vp_task_by_arn(" in index_src
