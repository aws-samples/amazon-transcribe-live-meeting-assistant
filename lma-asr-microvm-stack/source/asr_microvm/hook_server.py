# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Lambda MicroVMs lifecycle hook server for the ASR image.

The container entrypoint. It supervises ``asr_server.ws_server`` rather than
being it, because the two build-time hooks decide the launch performance of
every MicroVM created from the image:

``/ready``
    Returning 200 is the instant Lambda takes the Firecracker snapshot. So this
    only returns 200 once the ASR server is listening AND the warm-up has run
    through it, which puts the model weights and the ONNX arenas in the snapshot.
    503 asks Lambda to retry. Each request is answered within about ten seconds:
    loading the model can take minutes, so the hook does not hold a request
    open for it. The warm-up runs in the background once the server listens,
    and a later request is answered 200 when it has finished.

    The warm-up has two parts. First the server decodes about 20 s of audio on
    every model it holds, handed straight to the offline model rather than
    through the VAD (requested over a pipe, see :class:`WarmDecodeChannel`): a
    session only decodes what the VAD accepts as speech, and a bundle without a
    sample clip has only a synthetic tone to stream. Then warm audio is streamed
    through the real WebSocket path, as a client would.

``/validate``
    Lambda samples which snapshot pages the workload touches here and prefetches
    them on later launches, so this replays the same warm-up from the snapshot. A
    failure is logged but still reported 200: a missed prefetch costs startup
    latency, whereas failing validate fails the whole image build.

The runtime hooks are no-ops: an ASR MicroVM needs no per-launch configuration
(every session negotiates its own config over the WebSocket), so nothing has to
happen between resume and the first client connecting.
"""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
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
SPEAKER_MODEL = Path(os.environ.get("ASR_SPEAKER_MODEL", str(MODEL_DIR / "speaker_embedding.onnx")))
DIARIZER_DIR = MODEL_DIR / "diarizer"


def speaker_labels_baked() -> bool:
    return SPEAKER_MODEL.is_file() or any(DIARIZER_DIR.glob("*.onnx"))


# How long one /ready request waits for the ASR server to start listening
# before answering 503 for Lambda to ask again.
BOOT_TIMEOUT_S = float(os.environ.get("ASR_READY_REQUEST_WAIT_S", "10"))
EXERCISE_TIMEOUT_S = float(os.environ.get("ASR_EXERCISE_TIMEOUT_S", "180"))
WARM_TONE_MS = int(os.environ.get("ASR_WARM_TONE_MS", "2000"))
SAMPLE_RATE = 16000
CHUNK_MS = 100
BYTES_PER_SAMPLE = 2
# Pace the warm audio in real time rather than flooding it. The server's ingest
# queue is bounded (64 frames) and drops frames rather than growing without bound,
# so an unpaced send overruns it: builds logged "backpressure=3 dropped=2", and 4x
# real time still dropped a frame because the bundled WAV is 67 frames against a
# 64-slot queue. Dropped audio corrupts the warm transcript and under-samples the
# pages /validate exists to make Lambda prefetch, so the exercise sends at the rate
# a real client would. The decode runs in the background, not on a /ready request,
# so pacing only lengthens the warm-up (~7s); the image's 900s
# ReadyTimeoutInSeconds covers the whole series of /ready retries around it.
WARM_PACING_S = float(os.environ.get("ASR_WARM_PACING_S", str(CHUNK_MS / 1000)))
# How long to wait for the server's warm-up decode. A cold full-precision model has
# taken 44 s for 20 s of audio on a fresh MicroVM; this leaves room for that and
# for the exercise inside the 900 s ReadyTimeout and ValidateTimeout.
WARM_DECODE_TIMEOUT_S = float(os.environ.get("ASR_WARM_DECODE_TIMEOUT_S", "600"))
# Must match asr_server.ws_server.WARM_CONTROL_ENV (not imported: that module loads
# the inference stack, which the hook server does not need).
WARM_CONTROL_ENV = "ASR_WARM_CONTROL_FDS"


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
                log(f"{WARM_WAV} is not 16 kHz mono 16-bit; using a synthetic tone instead")
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
        "diarize": speaker_labels_baked(),
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


class WarmDecodeChannel:
    """Asks the ASR server for its warm-up decode over a pair of pipes.

    The server reads ``warm <id>`` lines and answers each with one JSON line (see
    ``asr_server.ws_server.serve_warm_requests``). Pipes rather than a socket, so
    nothing outside the MicroVM can request a decode, and the reply says when the
    decode has finished rather than leaving the hook to guess.
    """

    def __init__(self) -> None:
        self._request_fd: int | None = None
        self._reply_fd: int | None = None
        self._buffer = b""
        self._next_id = 0

    def spawn(self) -> subprocess.Popen:
        server_reads, hook_writes = os.pipe()
        hook_reads, server_writes = os.pipe()
        env = {**os.environ, WARM_CONTROL_ENV: f"{server_reads},{server_writes}"}
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user input
                [sys.executable, "-m", "asr_server.ws_server"],
                cwd=os.environ.get("ASR_APP_DIR", "/app"),
                env=env,
                pass_fds=(server_reads, server_writes),
            )
        finally:
            os.close(server_reads)
            os.close(server_writes)
        self.attach(hook_writes, hook_reads)
        return process

    def attach(self, request_fd: int, reply_fd: int) -> None:
        """Use these pipe ends to reach the server (``spawn`` calls it; so do tests)."""
        self._request_fd, self._reply_fd = request_fd, reply_fd

    def warm(self, timeout_s: float = WARM_DECODE_TIMEOUT_S) -> bool:
        """Run the server's warm-up decode; True once it reports success."""
        if self._request_fd is None or self._reply_fd is None:
            log("warm-up decode skipped: the ASR server was not started by this hook")
            return True
        self._next_id += 1
        request_id = str(self._next_id)
        started = time.monotonic()
        try:
            os.write(self._request_fd, f"warm {request_id}\n".encode())
            while True:
                reply = self._read_reply(self._reply_fd, started + timeout_s)
                if reply is None:
                    log(f"warm-up decode did not finish within {timeout_s:.0f}s")
                    return False
                if reply.get("id") == request_id:
                    break
                # The reply to an earlier request that timed out here: skip it.
        except OSError as exc:
            log(f"warm-up decode request failed: {exc}")
            return False
        if not reply.get("ok"):
            log(f"warm-up decode failed: {reply.get('error', 'unknown error')}")
            return False
        log(
            f"warm-up decode complete in {time.monotonic() - started:.1f}s "
            f"({reply.get('decodes', 0)} decode(s) of {reply.get('audio', '?')})"
        )
        return True

    def _read_reply(self, reply_fd: int, deadline: float) -> dict | None:
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            readable, _, _ = select.select([reply_fd], [], [], remaining)
            if not readable:
                return None
            chunk = os.read(reply_fd, 65536)
            if not chunk:
                raise OSError("the ASR server closed the warm-up pipe")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        try:
            parsed = json.loads(line)
        except ValueError:
            return {"id": None}
        return parsed if isinstance(parsed, dict) else {"id": None}


@dataclass
class HookDeps:
    """Injected surface so the hook logic is testable without a real MicroVM."""

    spawn_asr: Callable[[], subprocess.Popen]
    asr_listening: Callable[[], bool]
    exercise: Callable[[], bool]
    # The server's warm-up decode, run before each exercise. True when it succeeded.
    warm_decode: Callable[[], bool] = lambda: True
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic
    log: Callable[[str], None] = log
    # Runs the warm-up decode off the request thread.
    run_in_background: Callable[[Callable[[], None]], None] = lambda work: threading.Thread(
        target=work, name="asr-warm-up", daemon=True
    ).start()


@dataclass
class HookState:
    spawned: bool = False
    warm: bool = False
    warm_decoded: bool = False
    validated: bool = False
    terminated: bool = False
    hooks_seen: list[str] = field(default_factory=list)


class Hooks:
    def __init__(self, deps: HookDeps, *, boot_timeout_s: float = BOOT_TIMEOUT_S) -> None:
        self._deps = deps
        self._boot_timeout_s = boot_timeout_s
        self._asr: subprocess.Popen | None = None
        self.state = HookState()
        # The hook server answers each request on its own thread, and Lambda
        # calls /ready again until it gets 200. One warm-up at a time: a request
        # that arrives while one runs is answered 503 and starts nothing.
        self._warmup = threading.Lock()
        self._warming = False

    def on_ready(self) -> int:
        with self._warmup:
            return self._ready()

    def _ready(self) -> int:
        self.state.hooks_seen.append("ready")
        if not self.state.spawned:
            self._deps.log("starting the ASR server")
            self._asr = self._deps.spawn_asr()
            self.state.spawned = True
        if not self._wait_for_listener():
            self._deps.log("ASR server is not listening yet")
            return 503
        if not self.state.warm and not self._warming:
            self._warming = True
            self._deps.run_in_background(self._warm_up)
        if self.state.warm:
            self._deps.log("ready warm=True")
            return 200
        self._deps.log("ready: warm-up decode in progress")
        return 503

    def _warm_up(self) -> None:
        try:
            # The decode first, so the exercise that follows runs on a warm model.
            # A failed decode leaves the image unwarmed: answer 503 so Lambda asks
            # again and the next /ready retries it.
            self.state.warm_decoded = self._deps.warm_decode()
            self.state.warm = self.state.warm_decoded and self._deps.exercise()
        finally:
            self._warming = False

    def on_validate(self) -> int:
        with self._warmup:
            return self._validate()

    def _validate(self) -> int:
        self.state.hooks_seen.append("validate")
        if not self._deps.asr_listening():
            self._deps.log("ASR server is not listening; cannot sample snapshot pages")
            return 503
        decoded = self._deps.warm_decode()
        exercised = self._deps.exercise()
        self.state.validated = decoded and exercised
        self._deps.log(
            f"validate workloadExercised={self.state.validated} "
            f"(warm-up decode={decoded}, exercise={exercised})"
        )
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
    channel = WarmDecodeChannel()
    hooks = Hooks(
        HookDeps(
            spawn_asr=channel.spawn,
            asr_listening=lambda: port_open(ASR_PORT),
            exercise=exercise_asr,
            warm_decode=channel.warm,
        )
    )
    server = ThreadingHTTPServer(("0.0.0.0", HOOK_PORT), make_handler(hooks))  # noqa: S104
    log(f"listening on :{HOOK_PORT}{HOOK_PREFIX}")
    server.serve_forever()


if __name__ == "__main__":
    main()
