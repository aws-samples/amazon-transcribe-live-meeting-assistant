# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Tests for the Lambda MicroVMs lifecycle hook server.

The hooks decide the launch performance of every MicroVM built from the image, so
what matters here is the contract: /ready must NOT report 200 until the model is
loaded and a decode has run (that is when the snapshot is taken), a retried
/ready must not spawn a second server, and /validate must report success even
when its page-sampling exercise fails, because failing it fails the image build.
"""

from __future__ import annotations

import signal

import pytest
from asr_microvm.hook_server import (
    HookDeps,
    Hooks,
    hook_name_from_path,
    sanitize_for_log,
    warm_pcm,
)


class FakeProcess:
    """Minimal subprocess.Popen stand-in."""

    def __init__(self, exit_code: int | None = None) -> None:
        self.returncode = exit_code
        self.signals: list[int] = []

    def poll(self) -> int | None:
        return self.returncode

    def send_signal(self, number: int) -> None:
        self.signals.append(number)


class Harness:
    def __init__(
        self,
        *,
        listening_after: int = 0,
        exercise_result: bool = True,
        process: FakeProcess | None = None,
    ) -> None:
        self.spawns = 0
        self.exercises = 0
        self.probes = 0
        self.sleeps: list[float] = []
        self.clock = 0.0
        self.messages: list[str] = []
        self._listening_after = listening_after
        self._exercise_result = exercise_result
        self.process = process if process is not None else FakeProcess()

        self.deps = HookDeps(
            spawn_asr=self._spawn,
            asr_listening=self._listening,
            exercise=self._exercise,
            sleep=self._sleep,
            now=lambda: self.clock,
            log=self.messages.append,
        )

    def _spawn(self):
        self.spawns += 1
        return self.process

    def _listening(self) -> bool:
        self.probes += 1
        return self.probes > self._listening_after

    def _exercise(self) -> bool:
        self.exercises += 1
        return self._exercise_result

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds


def test_ready_starts_the_server_and_reports_200_once_warm() -> None:
    harness = Harness()
    hooks = Hooks(harness.deps)

    assert hooks.on_ready() == 200
    assert harness.spawns == 1
    assert harness.exercises == 1
    assert hooks.state.warm is True


def test_ready_waits_for_the_listener_before_exercising() -> None:
    harness = Harness(listening_after=3)
    hooks = Hooks(harness.deps)

    assert hooks.on_ready() == 200
    assert harness.sleeps == [1.0, 1.0, 1.0]
    assert harness.exercises == 1


def test_ready_reports_503_when_the_server_never_listens() -> None:
    harness = Harness(listening_after=10_000)
    hooks = Hooks(harness.deps, boot_timeout_s=5)

    assert hooks.on_ready() == 503
    assert harness.exercises == 0
    assert hooks.state.warm is False


def test_ready_reports_503_when_the_server_exits() -> None:
    harness = Harness(listening_after=10_000, process=FakeProcess(exit_code=1))
    hooks = Hooks(harness.deps, boot_timeout_s=60)

    assert hooks.on_ready() == 503
    # Gives up on the dead child instead of waiting out the whole timeout.
    assert harness.sleeps == []


def test_ready_reports_503_when_the_warm_decode_fails() -> None:
    harness = Harness(exercise_result=False)
    hooks = Hooks(harness.deps)

    # 503 asks Lambda to retry rather than snapshotting a server that has not
    # decoded anything yet.
    assert hooks.on_ready() == 503
    assert hooks.state.warm is False


def test_a_retried_ready_neither_respawns_nor_rewarms() -> None:
    harness = Harness()
    hooks = Hooks(harness.deps)

    assert hooks.on_ready() == 200
    assert hooks.on_ready() == 200
    assert harness.spawns == 1
    assert harness.exercises == 1


def test_validate_exercises_the_workload_again_from_the_snapshot() -> None:
    harness = Harness()
    hooks = Hooks(harness.deps)
    hooks.on_ready()

    assert hooks.on_validate() == 200
    assert harness.exercises == 2
    assert hooks.state.validated is True


def test_validate_still_reports_200_when_the_exercise_fails() -> None:
    harness = Harness(exercise_result=False)
    hooks = Hooks(harness.deps)

    # A missed prefetch costs startup latency; failing validate fails the build.
    assert hooks.on_validate() == 200
    assert hooks.state.validated is False


def test_validate_reports_503_when_nothing_is_listening() -> None:
    harness = Harness(listening_after=10_000)
    hooks = Hooks(harness.deps)

    assert hooks.on_validate() == 503
    assert harness.exercises == 0


def test_run_suspend_and_resume_are_no_ops() -> None:
    hooks = Hooks(Harness().deps)

    assert hooks.on_run() == 200
    assert hooks.on_suspend() == 200
    assert hooks.on_resume() == 200


def test_terminate_signals_the_server() -> None:
    harness = Harness()
    hooks = Hooks(harness.deps)
    hooks.on_ready()

    assert hooks.on_terminate() == 200
    assert harness.process.signals == [signal.SIGTERM]
    assert hooks.state.terminated is True


def test_terminate_is_safe_before_the_server_starts() -> None:
    harness = Harness()
    hooks = Hooks(harness.deps)

    assert hooks.on_terminate() == 200
    assert harness.process.signals == []


def test_dispatch_routes_known_hooks_and_ignores_unknown_ones() -> None:
    harness = Harness()
    hooks = Hooks(harness.deps)

    assert hooks.dispatch("ready") == 200
    assert hooks.dispatch("wat") == 200
    assert hooks.state.hooks_seen == ["ready"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/aws/lambda-microvms/runtime/v1/ready", "ready"),
        ("/aws/lambda-microvms/runtime/v1/validate/", "validate"),
        ("/aws/lambda-microvms/runtime/v1/run?x=1", "run"),
        ("/aws/lambda-microvms/runtime/v1", None),
        ("/aws/lambda-microvms/runtime/v1/", None),
        ("/health", None),
        ("", None),
        (None, None),
    ],
)
def test_hook_name_from_path(path: str | None, expected: str | None) -> None:
    assert hook_name_from_path(path) == expected


def test_sanitize_for_log_strips_control_characters_and_truncates() -> None:
    assert sanitize_for_log("ready\nfake 200") == "ready?fake 200"
    assert sanitize_for_log("x" * 100, max_length=8) == "xxxxxxxx…"


def test_warm_pcm_falls_back_to_a_deterministic_tone_without_a_wav() -> None:
    pcm = warm_pcm()

    assert len(pcm) > 0
    assert len(pcm) % 2 == 0
    assert pcm == warm_pcm()
