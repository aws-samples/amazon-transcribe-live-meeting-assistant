"""WebSocket server on port 8080 for the streaming ASR pipeline.

Runs **inside the MicroVM**. Speaks the wire protocol in ``design.md`` §5:

* **Handshake (R2):** config arrives as WS URL **query params** and/or an optional
  first ``config`` **text** frame; the two are merged (frame overrides query),
  defaults are applied once, and the effective config is echoed in a ``ready``
  message carrying a CSPRNG ``session_id`` (R9.2).
* **Audio ingest (R3):** **binary** frames are 16 kHz/16-bit/mono/LE PCM; **text**
  frames are JSON control messages (``config``/``eos``). The frame *type* (bytes
  vs str) disambiguates them unambiguously (R3.3) — no in-band sniffing.
* **Output (R4):** PCM is fed to a :class:`~asr_server.recognizer.Recognizer`,
  whose engine events are mapped to ``partial``/``final`` wire messages by
  :class:`~asr_server.protocol_adapter.ProtocolAdapter`.
* **Keepalive / close (R6):** WS ping keepalive (~5 s) is handled by the
  ``websockets`` layer via ``ping_interval`` (see :func:`serve_asr`); on ``eos``
  the recogniser is flushed, a ``termination`` summary is sent, and the socket
  closes.
* **Backpressure (R7):** inbound PCM is handed to a **bounded** queue drained by a
  separate consumer task. When the queue is full the ingest task blocks up to
  ``max_wait_ms`` (stops reading the socket → TCP backpressure); if the consumer
  is still wedged, the frame is *dropped* rather than buffered without bound, so
  a fast sender against a fixed-memory MicroVM can never OOM the process.

Testability
-----------
Like the sibling engine modules, the session logic drives a tiny injected surface
rather than a concrete socket: :class:`WebSocketConnection` (the ``recv``/``send``/
``close`` methods used) and a shared ``engine`` (a :class:`RecognizerEngine`, so a
scripted fake engine can stand in without model weights). The engine is built once
by an injected ``engine_factory`` in :func:`serve_asr`; each session acquires a
lightweight per-connection stream from it via ``engine.new_session()``. The real
entrypoint wraps ``websockets.serve`` with an adapter; unit tests drive
:class:`AsrSession` with a fake connection, and the acceptance test streams over a
real loopback socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import resource
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import NamedTuple, Protocol
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from asr_protocol import (
    Config,
    Error,
    Partial,
    Ready,
    ServerMessage,
    Termination,
)
from pydantic import ValidationError
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from asr_server.diarization import (
    DiarizingEngine,
    build_diarization_config,
    create_diarizing_engine,
    diarization_enabled,
)
from asr_server.offline_recognizer import (
    build_offline_model_config,
    create_sherpa_offline_engine,
)
from asr_server.protocol_adapter import ProtocolAdapter, WireMessage
from asr_server.recognizer import (
    Recognizer,
    RecognizerEngine,
    SessionConfig,
    SessionConfigError,
    build_model_config,
    create_sherpa_engine,
)

__all__ = [
    "DEFAULT_PORT",
    "ServerConfig",
    "WebSocketConnection",
    "EngineFactory",
    "AsrSession",
    "serve_asr",
    "main",
]

DEFAULT_PORT = 8080

# Diagnostic logger for the streaming session. Uses Python ``logging`` (NOT print)
# so records flow to stdout and into the MicroVM's CloudWatch log group under the
# execution role (API doc §5). ``ASR_LOG_LEVEL`` (default INFO) gates verbosity:
# DEBUG turns on a throttled heartbeat used to localise a "connects but no
# transcription" failure to ingest vs recognizer vs emit.
_LOG = logging.getLogger("asr_server.ws_server")

# Log a diagnostic running total at most this often (seconds), so per-frame chatter
# (100 ms frames → 10/s) collapses to a steady heartbeat even at DEBUG.
_DIAGNOSTIC_LOG_INTERVAL_S = 1.0

# 16-bit PCM: two bytes per sample. Used to convert byte counts to audio seconds.
_BYTES_PER_SAMPLE = 2

# Config fields settable via the URL query string (``type`` is not client-set).
_QUERY_CONFIG_FIELDS = frozenset(Config.model_fields) - {"type"}

# Wire error codes (design §5.2 / §9). Only ``BAD_ENCODING`` is enumerated in the
# design; the rest are descriptive and stable for clients to branch on.
_CODE_BAD_CONFIG = "BAD_CONFIG"
_CODE_BAD_MESSAGE = "BAD_MESSAGE"
_CODE_BAD_ENCODING = "BAD_ENCODING"
_CODE_INTERNAL = "INTERNAL"


# --- Injected surfaces ------------------------------------------------------


class WebSocketConnection(Protocol):
    """Minimal WebSocket surface :class:`AsrSession` drives — one per connection.

    Kept intentionally tiny (the subset of the ``websockets`` connection API the
    session actually uses) so a scripted fake can stand in for a real socket in
    tests. ``recv`` returns ``str`` for a text frame and ``bytes`` for a binary
    frame, and raises a ``ConnectionClosed``-style error when the peer goes away.
    """

    async def recv(self) -> str | bytes:
        """Receive the next frame: ``str`` (text) or ``bytes`` (binary)."""
        ...

    async def send(self, message: str) -> None:
        """Send one text frame to the client."""
        ...

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close the connection with a normal-closure code by default."""
        ...


# Builds the heavy, shared recognizer ENGINE once at server startup (not per
# connection — that duplicate allocation is the OOM this fix removes). Parameterless
# because it runs before any client connects, reusing the model resident from the
# build-time warmup snapshot; each connection then acquires a lightweight per-session
# stream via ``engine.new_session()``. Injected so tests supply a fake engine and
# production supplies a real sherpa-onnx engine.
EngineFactory = Callable[[], RecognizerEngine]


@dataclass
class ServerConfig:
    """Server runtime knobs (config-driven, NFR5 — no hardcoded magic).

    ``keepalive_interval_s`` drives the ``websockets`` ping keepalive (R6.1).
    ``keepalive_timeout_s`` is the pong deadline: ``None`` (the default) DISABLES
    the server-side timeout so a busy/slow event loop (cold model load, a long
    decode on a Graviton core) is never falsely torn down with a ``1011 keepalive
    ping timeout`` — the loop-starvation failure this task targets. A finite value
    re-enables the deadline (useful to reap genuinely dead peers) once the loop is
    known to stay responsive.
    ``max_queue_size`` + ``max_wait_ms`` bound the inbound audio queue (R7): the
    ingest task waits at most ``max_wait_ms`` to enqueue a full queue before
    dropping the frame, so memory stays bounded on a fixed-tier MicroVM.
    """

    host: str = "0.0.0.0"  # noqa: S104 - MicroVM serves on all interfaces behind the proxy
    port: int = DEFAULT_PORT
    keepalive_interval_s: float = 5.0
    keepalive_timeout_s: float | None = None
    max_queue_size: int = 64
    max_wait_ms: int = 2000

    @classmethod
    def from_env(cls) -> ServerConfig:
        """Build from ``ASR_*`` env vars, falling back to the defaults (NFR5)."""
        return cls(
            host=os.environ.get("ASR_HOST", cls.host),
            port=int(os.environ.get("ASR_PORT", cls.port)),
            keepalive_interval_s=float(
                os.environ.get("ASR_KEEPALIVE_S", cls.keepalive_interval_s)
            ),
            # Unset or empty ``ASR_KEEPALIVE_TIMEOUT_S`` → None (timeout disabled);
            # a numeric value re-enables a finite pong deadline.
            keepalive_timeout_s=_optional_float_env(
                "ASR_KEEPALIVE_TIMEOUT_S", cls.keepalive_timeout_s
            ),
            max_queue_size=int(os.environ.get("ASR_MAX_QUEUE", cls.max_queue_size)),
            max_wait_ms=int(os.environ.get("ASR_MAX_WAIT_MS", cls.max_wait_ms)),
        )


def _optional_float_env(name: str, default: float | None) -> float | None:
    """Read a float ``ASR_*`` env var that may be intentionally unset.

    An unset or empty value yields ``default`` (so ``None`` stays ``None`` — the
    keepalive timeout stays disabled); a non-empty value is parsed as a float. A
    non-numeric value falls back to ``default`` rather than crashing the server on
    a typo'd env var (mirrors ``_configure_logging``'s tolerant level parsing).
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


class _Rss(NamedTuple):
    """Resident-set snapshot in MB: ``current_mb`` (live) and ``peak_mb`` (high-water)."""

    current_mb: float
    peak_mb: float


def _read_rss_mb() -> _Rss:
    """Snapshot process RSS in MB — the platform exposes no MemoryUtilization metric,
    so the CloudWatch logs are the only path to the memory floor / under-load climb.

    ``current_mb`` is the LIVE resident size read from ``/proc/self/status`` ``VmRSS``
    (reported in kB → MB). ``peak_mb`` is the high-water mark from
    ``resource.getrusage(RUSAGE_SELF).ru_maxrss``, which on **Linux** is in KILOBYTES
    (÷1024 → MB) — the guest is Linux so that unit is correct here; do NOT "fix" this
    to bytes (that's the macOS/BSD convention) or the peak will read ~1000× low.

    Memory instrumentation must NEVER crash the server, so this never raises: if
    ``/proc/self/status`` is missing or unparsable, ``current_mb`` falls back to the
    ``ru_maxrss`` peak (the only number we still have) rather than propagating.
    """
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0  # Linux: kB
    current_mb = peak_mb  # fallback if /proc/self/status is unavailable/unparsable
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    current_mb = float(line.split()[1]) / 1024.0  # VmRSS is in kB
                    break
    except (OSError, ValueError, IndexError):
        pass  # keep the ru_maxrss fallback — never crash on instrumentation
    return _Rss(current_mb=current_mb, peak_mb=peak_mb)


def _configure_logging() -> None:
    """Route ``asr_server`` logs to stdout at ``$ASR_LOG_LEVEL`` (default INFO).

    Called once at process start. The runtime forwards stdout/stderr to the
    MicroVM's CloudWatch log group under the execution role (API doc §5), so a
    ``StreamHandler`` is all that's needed for the diagnostic logs to land there.
    An unrecognised level string falls back to INFO rather than crashing the
    server on a typo'd env var. Configures the ``asr_server`` package logger (not
    the root) so this stays scoped to our modules and idempotent across restarts.
    """
    level_name = os.environ.get("ASR_LOG_LEVEL", "INFO").upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):  # unknown name → getLevelName returns a str
        level = logging.INFO
    pkg_logger = logging.getLogger("asr_server")
    pkg_logger.setLevel(level)
    pkg_logger.propagate = False
    if not pkg_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        pkg_logger.addHandler(handler)


class _ConfigError(Exception):
    """A handshake config/message error to report as a fatal ``error`` frame."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- Config negotiation -----------------------------------------------------


def _parse_query(path: str) -> dict[str, str]:
    """Extract known config fields from a request path's query string (R2.1).

    Values are raw strings (e.g. ``sample_rate=16000``); pydantic coerces them
    when the merged :class:`Config` is validated. Unknown params are ignored and
    the last value wins for a repeated key.
    """
    parsed = parse_qs(urlsplit(path).query)
    return {k: v[-1] for k, v in parsed.items() if k in _QUERY_CONFIG_FIELDS and v}


def _build_config(query: dict[str, str], frame: dict[str, object] | None) -> Config:
    """Merge query params with an optional config frame; validate once (R2.3).

    The explicit ``config`` frame (only the keys the client actually sent) takes
    precedence over query params; defaults are then applied by the model so the
    ``ready`` echo is fully populated. A validation failure (e.g. an unsupported
    ``encoding``) surfaces as a :class:`_ConfigError` the caller reports and closes on.
    """
    merged: dict[str, object] = {**query, **(frame or {})}
    try:
        return Config.model_validate(merged)
    except ValidationError as exc:
        raise _ConfigError(_CODE_BAD_CONFIG, f"invalid config: {exc}") from exc


# --- Session ----------------------------------------------------------------


class AsrSession:
    """Drives one client connection through the streaming ASR pipeline.

    Owns the per-session recogniser, protocol adapter, bounded audio queue, and
    backpressure/observability counters. Instantiate per connection and ``await``
    :meth:`run`. The connection and recogniser are injected so the whole pipeline
    is exercisable with fakes (no real socket, no model weights).
    """

    def __init__(
        self,
        conn: WebSocketConnection,
        *,
        path: str = "/",
        engine: RecognizerEngine,
        server_config: ServerConfig | None = None,
        session_id: str | None = None,
    ) -> None:
        self._conn = conn
        self._path = path
        # The heavy model, built ONCE at startup and shared across all sessions.
        # This session only acquires a lightweight per-connection stream from it
        # (``engine.new_session()``) — no per-connection model allocation.
        self._engine = engine
        self._config = server_config if server_config is not None else ServerConfig()
        # Session id ownership (R9.2). When ``session_id`` is injected — the wired
        # case, where ``lifecycle_hooks./run`` is the single per-MicroVM owner that
        # minted it via CSPRNG after start — the session surfaces *that* id in
        # ``ready``. When ``None`` (standalone use / unit tests with no hook layer)
        # the session mints its own ``uuid4().hex`` so it stays self-contained.
        # This keeps exactly one runtime session_id source once the HTTP hook layer
        # wires the two together (future Phase 3 AWS-deploy work).
        self._session_id = session_id if session_id is not None else uuid4().hex

        # Populated during the handshake / pipeline setup.
        self._sample_rate = 16000
        # When False (client set ``interim_results=false``) the server withholds
        # every ``partial`` and sends only ``final``s (R4.1). Defaults to True so
        # partials stream unless explicitly disabled.
        self._interim_results = True
        self._recognizer: Recognizer | None = None
        self._adapter = ProtocolAdapter()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=self._config.max_queue_size
        )

        # Observability counters (R10).
        self._audio_bytes = 0
        self.backpressure_events = 0
        self.frames_dropped = 0

        # Diagnostic counters (logging only — no behaviour change). These localise a
        # "connects but no transcription" failure across the four suspect stages:
        #   _bin_frames_in / _bytes_in  → did audio actually ARRIVE at the server? (hyp b)
        #   _recognizer_events          → did the recognizer/VAD produce events?  (hyp c)
        #   _messages_emitted           → did adapted messages go out on the wire? (hyp d)
        self._bin_frames_in = 0
        self._bytes_in = 0
        self._recognizer_events = 0
        self._messages_emitted = 0
        # Loop-time anchor for the ~1s diagnostic heartbeat (see _log_diagnostic_tick).
        self._last_diagnostic_log_t = 0.0
        # A fatal *application* error was reported to the client over a live
        # socket (bad config/message/encoding, engine fault): flush + final + close
        # follow, but no ``termination`` (design §10).
        self._fatal = False
        # The peer's socket is known-dead (a send/recv hit ``ConnectionClosed``):
        # nothing more can go on the wire, so finalization is bookkeeping-only.
        self._peer_gone = False

    @property
    def session_id(self) -> str:
        """The session id echoed in ``ready`` (stable for the connection).

        The injected lifecycle-minted id when wired (``lifecycle_hooks./run`` is the
        single CSPRNG owner), else a locally minted ``uuid4().hex`` for standalone use.
        """
        return self._session_id

    @property
    def audio_seconds(self) -> float:
        """Audio seconds actually processed (feeds ``termination.audio_seconds``)."""
        return self._audio_bytes / _BYTES_PER_SAMPLE / self._sample_rate

    @property
    def segments(self) -> int:
        """Real segments finalized this session (feeds ``termination.segments``)."""
        return self._adapter.segments

    async def run(self) -> None:
        """Handshake, stream audio, then flush → ``termination`` → close."""
        graceful = False
        ending = "setup_error"
        try:
            try:
                config, first_pcm, ended = await self._handshake()
            except _ConfigError as exc:
                ending = "handshake_error"
                await self._send_error(exc.code, exc.message)
                await self._safe_close()
                return

            # Defense in depth (R2.2): the wire model already constrains ``channels``
            # to mono, but re-check here so the ingest path can never feed a non-mono
            # stream into the mono recogniser and silently garble it — surface a clean
            # fatal error instead. ``channels`` is a ``Literal[1]`` statically, so read
            # it through ``int()`` to keep this runtime guard meaningful.
            if int(config.channels) != 1:
                ending = "bad_channels"
                await self._send_error(
                    _CODE_BAD_ENCODING,
                    f"only mono (channels=1) is supported, got channels={config.channels}",
                )
                await self._safe_close()
                return

            self._sample_rate = config.sample_rate
            self._interim_results = config.interim_results
            self._adapter = ProtocolAdapter(word_timestamps=config.word_timestamps)
            # Session start (INFO): the negotiated config every downstream diagnostic is
            # read against. ``$ASR_ENGINE`` selects which engine's model is baked per image
            # (streaming default / accurate) — logging it disambiguates recognizer behaviour.
            _LOG.info(
                "session %s start: sample_rate=%d interim_results=%s engine=%s "
                "word_timestamps=%s endpointing_ms=%s diarize=%s max_speakers=%s",
                self._session_id,
                config.sample_rate,
                config.interim_results,
                os.environ.get("ASR_ENGINE", "streaming"),
                config.word_timestamps,
                config.endpointing_ms,
                config.diarize,
                config.max_speakers,
            )
            try:
                # Acquire only a lightweight per-session STREAM from the shared,
                # already-built engine — NOT a new model. The heavy sherpa-onnx
                # ``OnlineRecognizer`` + ONNX sessions were constructed ONCE at
                # server startup (``serve_asr``), reusing the model resident from
                # the build-time warmup snapshot; re-allocating a full recogniser on
                # every connection was the OOM/native-hang that killed the loop and
                # let the client reap the socket with a 1011 (``start`` logged, no
                # SUMMARY). ``new_session`` is cheap, but still bracket + offload it
                # so any future regression is observable and never blocks the loop.
                _LOG.info("session %s acquiring recognizer…", self._session_id)
                acquire_start = asyncio.get_running_loop().time()
                # Thread the negotiated per-session config through to the stream so
                # the recogniser actually decodes with what ``ready`` echoed (offline
                # endpointing varies per session; a sample_rate the shared graph
                # can't honour is rejected below), rather than silently falling back
                # to the engine's startup defaults.
                session_config = SessionConfig(
                    sample_rate=config.sample_rate,
                    endpointing_ms=config.endpointing_ms,
                    # Diarization knobs live in the per-session speaker registry
                    # (not the shared graph), so they can be honoured per session.
                    speaker_threshold=config.speaker_threshold,
                    max_speakers=config.max_speakers,
                    min_segment_ms=config.min_segment_ms,
                    require_corroboration=config.require_corroboration,
                )
                self._recognizer = await asyncio.to_thread(
                    self._engine.new_session, session_config
                )
                elapsed = asyncio.get_running_loop().time() - acquire_start
                _LOG.info(
                    "session %s recognizer ready in %.2fs", self._session_id, elapsed
                )
            except SessionConfigError as exc:
                # The client negotiated a config the shared engine cannot honour
                # (e.g. a sample_rate baked into the ONNX graph). Reject cleanly with
                # a fatal BAD_CONFIG rather than decoding with the wrong config.
                ending = "bad_session_config"
                await self._send_error(_CODE_BAD_CONFIG, str(exc))
                await self._safe_close()
                return
            except Exception as exc:  # noqa: BLE001 - report any engine init failure to the client
                ending = "recognizer_init_failed"
                await self._send_error(_CODE_INTERNAL, f"recognizer init failed: {exc}")
                await self._safe_close()
                return

            if ended:  # client sent ``eos`` as its first frame — nothing to stream.
                graceful = True
                ending = "graceful"
                await self._finalize_and_terminate()
                return

            consumer = asyncio.create_task(self._consume())
            try:
                if first_pcm is not None:
                    await self._enqueue(first_pcm)
                graceful = await self._ingest()
            finally:
                await self._queue.put(None)  # sentinel: let the consumer drain and stop
                await consumer

            if self._fatal:
                ending = "fatal"
                # A fatal error was already reported on the (still-live) socket; flush
                # whatever exists as ``final`` and close, but send no ``termination``
                # (the stream ended in error, not a clean end-of-stream — design §10).
                # Sends are self-guarding: if the socket is actually dead they no-op.
                await self._finalize_stream()
                await self._safe_close()
            elif graceful:
                ending = "graceful"
                # Client signalled ``eos`` on a live socket: flush, emit trailing
                # ``final``s, send ``termination``, close (R6.3).
                await self._finalize_and_terminate()
            else:
                ending = "disconnect"
                # Abrupt/unexpected disconnect: the peer is gone (``recv`` raised
                # ``ConnectionClosed``), so we CANNOT send ``termination`` or any
                # trailing ``final``. Still flush the recogniser for server-side
                # correctness — draining trailing decoder state keeps the
                # segment/audio-seconds bookkeeping complete for metrics (R10) — but
                # discard the messages. Reconnect/resume is the router's job (R6.2).
                await self._drain_for_bookkeeping()
        finally:
            self._log_session_summary(ending)

    # --- handshake ----------------------------------------------------------

    def _effective_config(self, config: Config) -> Config:
        """Reconcile the requested config with what this server can actually do.

        ``diarize`` is the one field a client can ask for that the server may be
        unable to honour: speaker labels require a speaker-embedding model baked
        into the image (``$ASR_DIARIZE``), which is a per-image build decision, not
        a per-connection one. Rather than silently accepting the flag and then
        returning ``speaker: null`` on every transcript — leaving the client to
        guess whether that means "one speaker" or "not supported" — the flag is
        downgraded here so ``ready.effective_config`` states the truth. Asking for
        diarization on a non-diarizing build is therefore a documented no-op, not
        an error: transcription still works, just without speaker labels.
        """
        if config.diarize and not isinstance(self._engine, DiarizingEngine):
            _LOG.warning(
                "session %s requested diarize=true but this server has no speaker "
                "model baked in (set ASR_DIARIZE + bake the embedding model); "
                "continuing without speaker labels",
                self._session_id,
            )
            return config.model_copy(update={"diarize": False})
        return config

    async def _handshake(self) -> tuple[Config, bytes | None, bool]:
        """Negotiate config and send ``ready``; peek the first frame.

        Returns ``(config, first_pcm, ended)`` where ``first_pcm`` is a binary
        first frame that must not be lost (fed into the pipeline before the ingest
        loop) and ``ended`` is ``True`` when the first frame was ``eos``.
        """
        query = _parse_query(self._path)
        first_pcm: bytes | None = None
        ended = False
        frame_config: dict[str, object] | None = None

        try:
            frame = await self._conn.recv()
        except ConnectionClosed:
            # Closed before any frame: still echo the query-only effective config.
            config = self._effective_config(_build_config(query, None))
            await self._send(Ready(effective_config=config, session_id=self._session_id))
            return config, None, True

        if isinstance(frame, bytes):
            first_pcm = frame
        else:
            frame_config, ended = _classify_first_text(frame)

        config = self._effective_config(_build_config(query, frame_config))
        await self._send(Ready(effective_config=config, session_id=self._session_id))
        return config, first_pcm, ended

    # --- ingest (producer) --------------------------------------------------

    async def _ingest(self) -> bool:
        """Read frames until ``eos`` or the socket closes; enqueue PCM (R3).

        Returns ``True`` if the stream ended gracefully via ``eos`` and ``False``
        if the connection dropped. Binary frames are enqueued (with backpressure);
        a text ``eos`` ends the stream; any other/invalid text is a fatal
        ``BAD_MESSAGE`` error.
        """
        try:
            while True:
                frame = await self._conn.recv()
                if isinstance(frame, bytes):
                    await self._enqueue(frame)
                    continue
                frame_config, ended = _classify_first_text(frame)
                if ended:
                    return True
                # A second ``config`` mid-stream is ignored (config is negotiated
                # once at open, R2.1); genuinely bad text raised above already.
        except ConnectionClosed:
            # Abrupt disconnect: the stream ended without ``eos``. Report it as
            # non-graceful; ``_peer_gone`` is left to the send path — any queued
            # work the consumer is still draining should attempt its sends (and
            # will flip ``_peer_gone`` itself if the socket is truly dead).
            return False
        except _ConfigError as exc:
            await self._send_error(exc.code, exc.message)
            self._fatal = True
            return False

    async def _enqueue(self, pcm: bytes) -> None:
        """Hand one PCM frame to the bounded queue, applying backpressure (R7).

        Fast path is a non-blocking put. On a full queue we block up to
        ``max_wait_ms`` (stops reading the socket, so TCP backpressure slows the
        sender); if the consumer is still behind after that, the frame is dropped
        rather than buffered without bound — memory stays fixed, no OOM.
        """
        # Diagnostic (logging only): count every binary frame that ARRIVES here, so a
        # run can distinguish "mic sent no audio" (b) from "audio never reached the
        # server" — this is the last point before the queue. A zero-length or
        # odd-length frame (16-bit PCM must be even) is malformed and would decode to
        # silence/garbage, so warn once per occurrence rather than let it vanish.
        self._bin_frames_in += 1
        self._bytes_in += len(pcm)
        if len(pcm) == 0 or len(pcm) % _BYTES_PER_SAMPLE != 0:
            _LOG.warning(
                "session %s: suspicious PCM frame #%d len=%d bytes "
                "(expected non-zero even length for 16-bit PCM)",
                self._session_id,
                self._bin_frames_in,
                len(pcm),
            )
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            self.backpressure_events += 1
            try:
                await asyncio.wait_for(
                    self._queue.put(pcm), timeout=self._config.max_wait_ms / 1000.0
                )
            except TimeoutError:
                self.frames_dropped += 1
        self._log_diagnostic_tick()

    def _log_diagnostic_tick(self) -> None:
        """Emit a DEBUG heartbeat at most every ``_DIAGNOSTIC_LOG_INTERVAL_S``.

        DEBUG must not devolve into synchronous per-frame or per-message writes on
        the event loop hot path, so the ingest/recognizer/emit counters are only
        surfaced as a collapsed ~1s running total. INFO stays summary-only.
        """
        if not _LOG.isEnabledFor(logging.DEBUG):
            return
        now = asyncio.get_running_loop().time()
        if now - self._last_diagnostic_log_t < _DIAGNOSTIC_LOG_INTERVAL_S:
            return
        self._last_diagnostic_log_t = now
        _LOG.debug(
            "session %s diag: frames_in=%d bytes_in=%d (~%.1fs audio) "
            "recognizer_events=%d messages_emitted=%d backpressure=%d dropped=%d",
            self._session_id,
            self._bin_frames_in,
            self._bytes_in,
            self._bytes_in / _BYTES_PER_SAMPLE / self._sample_rate,
            self._recognizer_events,
            self._messages_emitted,
            self.backpressure_events,
            self.frames_dropped,
        )

    def _log_session_summary(self, ending: str) -> None:
        """Emit the end-of-session INFO summary for every exit branch.

        Carries current RSS so the summary doubles as the under-load memory trace:
        one datapoint per session end, so the climb under concurrent sessions is
        readable from the existing SUMMARY log path (no separate heartbeat/timer)
        and an OOM near a 4GB ceiling is attributable to load (R10).
        """
        rss = _read_rss_mb()
        _LOG.info(
            "session %s end (%s): frames_in=%d bytes_in=%d (~%.1fs audio) "
            "recognizer_events=%d messages_emitted=%d segments=%d "
            "backpressure=%d dropped=%d interim_results=%s peer_gone=%s "
            "rss_current=%.1fMB rss_peak=%.1fMB",
            self._session_id,
            ending,
            self._bin_frames_in,
            self._bytes_in,
            self._bytes_in / _BYTES_PER_SAMPLE / self._sample_rate,
            self._recognizer_events,
            self._messages_emitted,
            self.segments,
            self.backpressure_events,
            self.frames_dropped,
            self._interim_results,
            self._peer_gone,
            rss.current_mb,
            rss.peak_mb,
        )

    # --- consume ------------------------------------------------------------

    async def _consume(self) -> None:
        """Drain the queue: decode PCM → adapt events → send wire messages.

        Runs until the ``None`` sentinel. A recogniser ``ValueError`` (e.g. an
        odd-length PCM frame) is a client ``BAD_ENCODING``; any other failure is a
        fatal ``INTERNAL`` error. Either sets ``_fatal`` and stops; :meth:`run`
        then flushes what exists and closes (design §10).
        """
        assert self._recognizer is not None  # set before the consumer starts
        while True:
            item = await self._queue.get()
            if item is None:
                return
            try:
                # sherpa-onnx decode is CPU-heavy and BLOCKS: run it off the event
                # loop so the websockets keepalive ping/pong handler and outbound
                # partials/finals keep flowing on the CPU-constrained MicroVM
                # (otherwise the loop stalls per chunk → client ping timeout → 1011).
                # Safe without a lock: this single consumer task awaits each decode
                # before pulling the next item, so accept_pcm never overlaps itself,
                # and every flush() runs only after ``await consumer`` in run().
                events = await asyncio.to_thread(self._recognizer.accept_pcm, item)
            except ValueError as exc:
                await self._send_error(_CODE_BAD_ENCODING, str(exc))
                self._fatal = True
                return
            except Exception as exc:  # noqa: BLE001 - surface engine faults as a fatal error
                await self._send_error(_CODE_INTERNAL, f"recognizer failed: {exc}")
                self._fatal = True
                return
            self._audio_bytes += len(item)
            # Diagnostic (logging only): count raw recognizer/VAD events BEFORE the
            # adapter maps them. If bytes are arriving (see ingest heartbeat) but this
            # stays at zero, the recognizer/VAD produced nothing (hyp c) — vs. events
            # produced but adapted messages not emitted (hyp d, see _emit).
            if events:
                self._recognizer_events += len(events)
                self._log_diagnostic_tick()
            await self._emit(self._adapter.adapt(events))

    # --- finalize -----------------------------------------------------------

    async def _finalize_and_terminate(self) -> None:
        """Flush the recogniser, emit finals, send ``termination``, then close (R6.3)."""
        await self._finalize_stream()
        await self._send(
            Termination(audio_seconds=self.audio_seconds, segments=self.segments)
        )
        await self._safe_close()

    async def _finalize_stream(self) -> None:
        """Flush the recogniser's tail segment and send any resulting finals."""
        if self._recognizer is None:
            return
        try:
            # Flush is CPU-heavy sherpa-onnx work too — run it off the event loop
            # (same rationale as accept_pcm). Safe without a lock: every flush()
            # runs only after ``await consumer`` in run(), so the consumer's last
            # accept_pcm has already resolved and cannot overlap this flush.
            events = await asyncio.to_thread(self._recognizer.flush)
        except Exception:  # noqa: BLE001 - a flush failure must not mask termination/close
            return
        await self._emit(self._adapter.adapt(events))

    async def _drain_for_bookkeeping(self) -> None:
        """Flush the recogniser after an abrupt disconnect, sending nothing (R6.3).

        The peer is gone, so no ``final``/``termination`` can go on the wire. We
        still run the recogniser's ``flush`` through the adapter so any trailing
        segment is counted (``segments``/``audio_seconds`` feed metrics, R10) and
        the engine releases its per-segment state; the produced messages are
        discarded. A flush failure is swallowed — there is nothing left to close.
        """
        if self._recognizer is None:
            return
        try:
            # Off the event loop like the other decode calls: even with the peer
            # gone, a blocking flush would stall every *other* connection sharing
            # this loop. Safe without a lock — runs after ``await consumer``.
            events = await asyncio.to_thread(self._recognizer.flush)
        except Exception:  # noqa: BLE001 - no live socket to report to; bookkeeping only
            return
        self._adapter.adapt(events)  # advances segment bookkeeping; output discarded

    async def _emit(self, messages: list[WireMessage]) -> None:
        """Send adapted wire messages, withholding ``partial``s when disabled (R4.1).

        The adapter still runs its dedupe / monotonic-segment bookkeeping over the
        full event stream; this seam merely drops interim ``partial`` frames from
        the wire when the client negotiated ``interim_results=false`` (``final``s,
        which carry the committed transcript, always go out).
        """
        for message in messages:
            if not self._interim_results and isinstance(message, Partial):
                continue
            # Diagnostic (logging only): count each message that actually goes on the
            # wire. Combined with the throttled heartbeat and end-of-session summary,
            # this still localises emit-vs-transport issues without synchronous
            # per-message writes on the event loop hot path.
            self._messages_emitted += 1
            await self._send(message)
        if messages:
            self._log_diagnostic_tick()

    # --- I/O helpers --------------------------------------------------------

    async def _send(self, message: ServerMessage) -> None:
        """Serialize a server message and send it as one text frame.

        A ``ConnectionClosed`` mid-send means the peer is gone: mark it so
        finalization becomes bookkeeping-only and no further sends are attempted.
        """
        if self._peer_gone:
            return
        try:
            await self._conn.send(message.model_dump_json())
        except ConnectionClosed:
            self._peer_gone = True

    async def _send_error(self, code: str, message: str) -> None:
        await self._send(Error(code=code, message=message, fatal=True))

    async def _safe_close(self) -> None:
        with contextlib.suppress(ConnectionClosed):
            await self._conn.close()


def _classify_first_text(frame: str) -> tuple[dict[str, object] | None, bool]:
    """Classify a control text frame → ``(config_fields, is_eos)``.

    Returns the raw config field dict (only the keys the client sent, so query
    defaults are not clobbered) for a ``config`` frame, or ``(None, True)`` for an
    ``eos`` frame. Malformed JSON or an unknown ``type`` is a fatal ``BAD_MESSAGE``.
    """
    try:
        data = json.loads(frame)
    except json.JSONDecodeError as exc:
        raise _ConfigError(_CODE_BAD_MESSAGE, f"invalid control frame: {exc}") from exc
    if not isinstance(data, dict):
        raise _ConfigError(_CODE_BAD_MESSAGE, "control frame must be a JSON object")
    mtype = data.get("type")
    if mtype == "eos":
        return None, True
    if mtype == "config":
        return {k: v for k, v in data.items() if k != "type"}, False
    raise _ConfigError(_CODE_BAD_MESSAGE, f"unexpected control frame type: {mtype!r}")


# --- Production entrypoint --------------------------------------------------


def _default_engine_factory() -> RecognizerEngine:
    """Build the real, shared recogniser ENGINE for the baked engine (NFR5).

    Called ONCE at server startup (``serve_asr``), not per connection: the heavy
    model is allocated exactly once and reused across all sessions, reusing the
    model resident from the build-time warmup snapshot rather than duplicating it
    on every WebSocket open (the OOM this fix removes).

    The engine is selected by ``$ASR_ENGINE`` (``streaming`` default, or
    ``accurate``) — one engine's model is baked per MicroVM image (recipe doc §4),
    so the server reads which one was baked rather than switching per connection.

    * ``streaming`` → the frame-synchronous :class:`SherpaOnlineEngine` (Nemotron);
      ``endpointing_ms`` maps onto the backend's rule2 trailing silence.
    * ``accurate`` → the VAD-segmented offline :class:`SherpaOfflineEngine`
      (Parakeet TDT, R4.4); ``endpointing_ms`` maps onto the VAD trailing silence.

    Model paths and thread count come from the environment (no hardcoded absolute
    paths). Config resolution is delegated to the shared ``build_*_model_config``
    so the runtime engine and the build-time snapshot warmup (``warmup.py``) stay in
    lockstep, both built from env/defaults. The negotiated per-connection config is
    then threaded through ``AsrSession`` → ``engine.new_session(SessionConfig(...))``:
    the accurate engine applies ``endpointing_ms`` to the per-session VAD gate, while
    the streaming engine bakes its endpoint rules and ``sample_rate`` into the shared
    graph and rejects a session that negotiates an incompatible value (fatal
    ``BAD_CONFIG``) rather than decoding with the wrong config. Fails closed
    (``RuntimeError``) until the model + aarch64 wheels are provisioned — ``serve_asr``
    surfaces that at startup (fail closed, non-zero exit) rather than dying silently.
    """
    engine = os.environ.get("ASR_ENGINE", "streaming")
    if engine == "accurate":
        offline_config = build_offline_model_config()
        inner: RecognizerEngine = create_sherpa_offline_engine(offline_config)
    else:
        model_config = build_model_config()
        inner = create_sherpa_engine(model_config)

    # Speaker diarization is a DECORATOR over whichever engine was baked, so both
    # the streaming and accurate paths gain speaker labels through one code path
    # and the label is always derived from the same samples/segments as the text.
    # ``$ASR_DIARIZE`` gates it because the speaker-embedding model must have been
    # baked into the image; building it here (once, at startup) keeps the weights
    # resident and off the per-connection path.
    if diarization_enabled():
        diarization_config = build_diarization_config()
        _LOG.info(
            "diarization enabled: speaker_model=%s threshold=%.2f max_speakers=%d",
            diarization_config.embedder.model,
            diarization_config.threshold,
            diarization_config.max_speakers,
        )
        return create_diarizing_engine(inner, diarization_config)
    return inner


async def serve_asr(
    server_config: ServerConfig | None = None,
    engine_factory: EngineFactory = _default_engine_factory,
) -> None:
    """Serve the ASR WebSocket forever on the configured host/port.

    Builds the heavy recogniser ENGINE **once, eagerly, on this (main) thread
    BEFORE** ``serve()`` accepts any connection. This is the core of the fix: the
    model is allocated a single time (reusing the warm-snapshot resident model)
    instead of once per connection, so a second full allocation can no longer
    OOM-kill the 8 GB VM, and native model init runs on the main thread at startup
    rather than off-loop mid-session. If construction raises, the exception
    propagates out of ``serve_asr`` (``main`` exits non-zero) — fail closed, no
    un-usable server. Each connection then acquires only a lightweight per-session
    stream from this shared engine.

    ``websockets`` sends a ping every ``keepalive_interval_s`` (~5 s, R6.1). The
    pong deadline is ``cfg.keepalive_timeout_s`` — ``None`` by default, which
    DISABLES the server-side keepalive timeout so a momentarily loop-bound server
    (a slow decode) is never falsely killed with a ``1011``. This is defence in
    depth: the primary fix is building the engine once at startup and offloading
    all blocking native work (per-session stream acquisition + per-chunk decode +
    flush) off the loop so pings are answered promptly; the disabled timeout
    ensures a transient stall can't tear the socket down before that work yields.
    NOTE: this governs the *server's* own keepalive only — a client with its own
    ping/timeout will still reap the socket if the loop stalls, which is exactly
    why building once + the offloads matter.
    """
    cfg = server_config if server_config is not None else ServerConfig()

    # Eager, one-time engine build on the main thread before accepting connections.
    _LOG.info("acquiring recognizer…")
    build_start = asyncio.get_running_loop().time()
    engine = engine_factory()
    _LOG.info(
        "recognizer ready in %.2fs", asyncio.get_running_loop().time() - build_start
    )

    # Startup memory FLOOR: the baked-warm model is now resident and no session has
    # connected yet, so this is the idle resident baseline the 8GB→4GB sizing call is
    # made against. Emitted BEFORE serve() accepts so nothing has grown it (R10). The
    # platform exposes no MemoryUtilization metric — this log line is the only source.
    floor = _read_rss_mb()
    _LOG.info(
        "startup memory floor: rss_current=%.1fMB rss_peak=%.1fMB (model resident, "
        "pre-accept baseline)",
        floor.current_mb,
        floor.peak_mb,
    )

    async def handler(conn: ServerConnection) -> None:
        request = conn.request
        path = request.path if request is not None else "/"
        session = AsrSession(
            conn,
            path=path,
            engine=engine,
            server_config=cfg,
        )
        await session.run()

    async with serve(
        handler,
        cfg.host,
        cfg.port,
        ping_interval=cfg.keepalive_interval_s,
        ping_timeout=cfg.keepalive_timeout_s,
    ) as server:
        await server.serve_forever()


def main() -> None:
    """Console entrypoint: serve using env-driven config (``CMD`` in the image).

    A startup engine-build failure (missing wheel / model files) propagates out of
    ``asyncio.run`` as an unhandled exception → non-zero process exit (fail closed),
    logged before the traceback so CloudWatch shows the actionable cause.
    """
    _configure_logging()  # route asr_server logs to stdout → CloudWatch (API doc §5)
    _LOG.info("ASR WebSocket server starting on port %d", ServerConfig.from_env().port)
    try:
        asyncio.run(serve_asr(ServerConfig.from_env()))
    except Exception:
        _LOG.exception("ASR server failed to start (engine construction error)")
        raise


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
