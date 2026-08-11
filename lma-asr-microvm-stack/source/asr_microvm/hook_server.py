# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Lambda MicroVMs lifecycle hook server for the ASR image.

The container entrypoint. It supervises ``asr_server.ws_server`` rather than
being it, because the two build-time hooks decide the launch performance of
every MicroVM created from the image:

``/ready``
    Returning 200 is the instant Lambda takes the Firecracker snapshot. So this
    only returns 200 once the ASR server is listening AND a full decode has run
    through it, which puts the model weights and the ONNX arenas in the snapshot.
    503 asks Lambda to retry.

``/validate``
    Lambda samples which snapshot pages the workload touches here and prefetches
    them on later launches, so this replays the same decode from the snapshot. A
    failure is logged but still reported 200: a missed prefetch costs startup
    latency, whereas failing validate fails the whole image build.

The runtime hooks are no-ops: an ASR MicroVM needs no per-launch configuration
(every session negotiates its own config over the WebSocket), so nothing has to
happen between resume and the first client connecting.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"
HOOK_PORT = int(os.environ.get("HOOK_PORT", "9000"))
ASR_PORT = int(os.environ.get("ASR_PORT", "8080"))
MODEL_DIR = Path(os.environ.get("ASR_MODEL_DIR", "/opt/models"))
WARM_WAV = MODEL_DIR / "test_wavs" / "warm.wav"
SPEAKER_MODEL = Path(
    os.environ.get("ASR_SPEAKER_MODEL", str(MODEL_DIR / "speaker_embedding.onnx"))
)

BOOT_TIMEOUT_S = float(os.environ.get("ASR_BOOT_TIMEOUT_S", "540"))
EXERCISE_TIMEOUT_S = float(os.environ.get("ASR_EXERCISE_TIMEOUT_S", "180"))
WARM_TONE_MS = int(os.environ.get("ASR_WARM_TONE_MS", "2000"))
SAMPLE_RATE = 16000
CHUNK_MS = 100
BYTES_PER_SAMPLE = 2
# Pace the warm audio instead of flooding it. The server's ingest queue is bounded
# (64 frames) and drops frames rather than growing without bound, so an unpaced
# send overruns it: the first build logged "backpressure=3 dropped=2", which both
# corrupts the warm transcript and under-samples the pages /validate is supposed to
# make Lambda prefetch. 25 ms per 100 ms frame is 4x real time — fast enough to keep
# the hook quick, slow enough that the queue never fills.
WARM_PACING_S = float(os.environ.get("ASR_WARM_PACING_S", "0.025"))


def log(message: str) -> None:
    print(f"[hooks] {message}", flush=True)


def port_open(port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def warm_pcm() -> bytes:
    if WARM_WAV.is_file():
        try:
            with wave.open(str(WARM_WAV), "rb") as wav:
                if (
                    wav.getnchannels() == 1
                    and wav.getsampwidth() == BYTES_PER_SAMPLE
                    and wav.getframerate() == SAMPLE_RATE
                ):
                    return wav.readframes(wav.getnframes())
                log(
                    f"{WARM_WAV} is not 16 kHz mono 16-bit; using a synthetic tone instead"
                )
        except (OSError, wave.Error) as exc:
            log(f"could not read {WARM_WAV} ({exc}); using a synthetic tone instead")

    from asr_server.warmup import synthetic_pcm

    return synthetic_pcm(WARM_TONE_MS, SAMPLE_RATE)


def exercise_asr(timeout_s: float = EXERCISE_TIMEOUT_S) -> bool:
    """Stream warm audio through the real WebSocket path. True if it completed."""
    from websockets.sync.client import connect

    pcm = warm_pcm()
    chunk_bytes = SAMPLE_RATE * CHUNK_MS // 1000 * BYTES_PER_SAMPLE
    config = {
        "type": "config",
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "interim_results": True,
        "diarize": SPEAKER_MODEL.is_file(),
    }
    started = time.monotonic()
    finals = 0
    try:
        with connect(f"ws://127.0.0.1:{ASR_PORT}/", open_timeout=timeout_s) as ws:
            ws.send(json.dumps(config))
            for offset in range(0, len(pcm), chunk_bytes):
                ws.send(pcm[offset : offset + chunk_bytes])
                if WARM_PACING_S > 0:
                    time.sleep(WARM_PACING_S)
            ws.send(json.dumps({"type": "eos"}))
            while True:
                remaining = timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    log("warm exercise timed out waiting for termination")
                    return False
                message = json.loads(ws.recv(timeout=remaining))
                kind = message.get("type")
                if kind == "final":
                    finals += 1
                elif kind == "error":
                    log(f"warm exercise error from server: {message}")
                    return False
                elif kind == "termination":
                    log(
                        f"warm exercise complete in {time.monotonic() - started:.1f}s "
                        f"({message.get('audio_seconds', 0):.1f}s audio, {finals} final(s), "
                        f"diarize={config['diarize']})"
                    )
                    return True
    except Exception as exc:  # noqa: BLE001 - any failure means "not warm yet"
        log(f"warm exercise failed: {exc}")
        return False


@dataclass
class HookDeps:
    """Injected surface so the hook logic is testable without a real MicroVM."""

    spawn_asr: Callable[[], subprocess.Popen]
    asr_listening: Callable[[], bool]
    exercise: Callable[[], bool]
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic
    log: Callable[[str], None] = log


@dataclass
class HookState:
    spawned: bool = False
    warm: bool = False
    validated: bool = False
    terminated: bool = False
    hooks_seen: list[str] = field(default_factory=list)


class Hooks:
    def __init__(self, deps: HookDeps, *, boot_timeout_s: float = BOOT_TIMEOUT_S) -> None:
        self._deps = deps
        self._boot_timeout_s = boot_timeout_s
        self._asr: subprocess.Popen | None = None
        self.state = HookState()

    def on_ready(self) -> int:
        self.state.hooks_seen.append("ready")
        if not self.state.spawned:
            self._deps.log("starting the ASR server")
            self._asr = self._deps.spawn_asr()
            self.state.spawned = True
        if not self._wait_for_listener():
            self._deps.log("ASR server is not listening yet")
            return 503
        if not self.state.warm:
            self.state.warm = self._deps.exercise()
        self._deps.log(f"ready warm={self.state.warm}")
        return 200 if self.state.warm else 503

    def on_validate(self) -> int:
        self.state.hooks_seen.append("validate")
        if not self._deps.asr_listening():
            self._deps.log("ASR server is not listening; cannot sample snapshot pages")
            return 503
        self.state.validated = self._deps.exercise()
        self._deps.log(f"validate workloadExercised={self.state.validated}")
        return 200

    def on_run(self) -> int:
        self.state.hooks_seen.append("run")
        return 200

    def on_suspend(self) -> int:
        self.state.hooks_seen.append("suspend")
        return 200

    def on_resume(self) -> int:
        self.state.hooks_seen.append("resume")
        return 200

    def on_terminate(self) -> int:
        self.state.hooks_seen.append("terminate")
        self.state.terminated = True
        if self._asr is not None and self._asr.poll() is None:
            self._deps.log("signalling the ASR server to exit")
            self._asr.send_signal(signal.SIGTERM)
        return 200

    def dispatch(self, hook: str) -> int:
        handlers = {
            "ready": self.on_ready,
            "validate": self.on_validate,
            "run": self.on_run,
            "suspend": self.on_suspend,
            "resume": self.on_resume,
            "terminate": self.on_terminate,
        }
        handler = handlers.get(hook)
        if handler is None:
            self._deps.log(f"unknown hook '{sanitize_for_log(hook)}'")
            return 200
        return handler()

    def _wait_for_listener(self) -> bool:
        deadline = self._deps.now() + self._boot_timeout_s
        while self._deps.now() < deadline:
            if self._deps.asr_listening():
                return True
            if self._asr is not None and self._asr.poll() is not None:
                self._deps.log(f"ASR server exited with code {self._asr.returncode}")
                return False
            self._deps.sleep(1.0)
        return False


def sanitize_for_log(value: str, max_length: int = 64) -> str:
    clean = "".join(character if character.isprintable() else "?" for character in value)
    return clean if len(clean) <= max_length else f"{clean[:max_length]}…"


def hook_name_from_path(path: str | None) -> str | None:
    if not path:
        return None
    clean = path.split("?")[0].rstrip("/")
    if not clean.startswith(HOOK_PREFIX):
        return None
    name = clean[len(HOOK_PREFIX) :].lstrip("/")
    return name or None


def spawn_asr() -> subprocess.Popen:
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user input
        [sys.executable, "-m", "asr_server.ws_server"],
        cwd=os.environ.get("ASR_APP_DIR", "/app"),
    )


def make_handler(hooks: Hooks) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, status: int, body: dict) -> None:
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            hook = hook_name_from_path(self.path)
            if hook is None:
                self._respond(200, {"ok": True, "state": vars(hooks.state)})
                return
            try:
                status = hooks.dispatch(hook)
            except Exception as exc:  # noqa: BLE001 - a hook must always answer
                log(f"hook {sanitize_for_log(hook)} raised: {exc}")
                status = 500
            self._respond(status, {"hook": hook, "status": status})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._handle()

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._handle()

        def log_message(self, format: str, *args: object) -> None:
            log(f"{self.address_string()} {format % args}")

    return Handler


def main() -> None:
    hooks = Hooks(
        HookDeps(
            spawn_asr=spawn_asr,
            asr_listening=lambda: port_open(ASR_PORT),
            exercise=exercise_asr,
        )
    )
    server = ThreadingHTTPServer(("0.0.0.0", HOOK_PORT), make_handler(hooks))  # noqa: S104
    log(f"listening on :{HOOK_PORT}{HOOK_PREFIX}")
    server.serve_forever()


if __name__ == "__main__":
    main()
