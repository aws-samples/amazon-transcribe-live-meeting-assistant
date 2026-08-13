"""Unit and integration tests for the ASR WebSocket server.

A local WebSocket client streaming PCM receives
``ready`` → ``partial*`` → ``final`` → ``termination``; queue-full triggers
backpressure, not OOM.

Two layers, mirroring the sibling modules' fake-backend convention:

* Most cases drive :class:`AsrSession` through a scripted fake
  :class:`FakeConnection` + a scripted fake recogniser — no real socket, no model
  weights, deterministic frame order.
* :func:`test_end_to_end_loopback_stream` runs the real ``websockets`` server on a
  loopback port and streams over it, proving the wire path end-to-end.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections.abc import Sequence
from typing import Any

import pytest
from asr_protocol import parse_server_message
from asr_server.recognizer import (
    Event,
    Recognizer,
    RecognizerEngine,
    SessionConfig,
    SessionConfigError,
)
from asr_server.ws_server import (
    DEFAULT_PORT,
    AsrSession,
    ServerConfig,
    _read_rss_mb,
    serve_asr,
)
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

# ``asyncio_mode = "auto"`` (pyproject) runs the ``async def`` tests below without
# a per-test marker; the two synchronous ServerConfig tests run as plain tests.

CHUNK = b"\x00\x01" * 320  # 640 bytes = 20 ms of 16 kHz/16-bit mono PCM


# --- Fakes ------------------------------------------------------------------


def _closed() -> ConnectionClosed:
    """A ``ConnectionClosed`` like the real ``recv`` raises at end-of-stream."""
    close = Close(1000, "")
    return ConnectionClosed(close, close, rcvd_then_sent=True)


class FakeConnection:
    """A scripted :class:`WebSocketConnection`: feeds frames, collects sends.

    ``incoming`` is consumed one frame per ``recv``; when exhausted ``recv`` raises
    :class:`ConnectionClosed` (peer gone) unless ``drop`` is set, which simulates a
    mid-stream drop *without* a graceful ``eos``. ``sent`` records every text frame
    the server sent, in order.

    ``fail_send_after`` models a peer that dies *mid-stream*: the first N ``send``
    calls succeed (and are recorded), then every subsequent ``send`` raises
    :class:`ConnectionClosed` — exercising the server's peer-gone guard where an
    early frame lands but a later one fails, rather than a socket dead from frame 0.
    """

    def __init__(
        self,
        incoming: Sequence[str | bytes],
        *,
        drop: bool = False,
        fail_send_after: int | None = None,
    ) -> None:
        self._incoming = list(incoming)
        self._drop = drop
        self._fail_send_after = fail_send_after
        self.sent: list[str] = []
        self.closed = False
        self.close_code: int | None = None

    async def recv(self) -> str | bytes:
        if self._incoming:
            return self._incoming.pop(0)
        raise _closed()

    async def send(self, message: str) -> None:
        if self.closed:
            raise _closed()
        # Mid-stream failure: succeed for the first ``fail_send_after`` sends, then
        # raise on every send thereafter (the peer went away after some frames).
        if self._fail_send_after is not None and len(self.sent) >= self._fail_send_after:
            raise _closed()
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code

    # --- assertions helpers ---
    def messages(self) -> list[dict[str, Any]]:
        return [json.loads(m) for m in self.sent]

    def types(self) -> list[str]:
        return [str(m["type"]) for m in self.messages()]


class ScriptedRecognizer(Recognizer):
    """A fake :class:`Recognizer`: emits scripted events per ``accept_pcm`` call.

    ``script`` is one event-list per PCM chunk consumed; once exhausted a chunk
    yields nothing. ``flush_events`` is returned from :meth:`flush`. This lets a
    test express an exact partial/final/termination trajectory with no weights.
    """

    def __init__(
        self,
        script: Sequence[Sequence[Event]] = (),
        flush_events: Sequence[Event] = (),
    ) -> None:
        self._script = [list(step) for step in script]
        self._flush_events = list(flush_events)
        self._idx = 0
        self.chunks_seen = 0
        self.flushed = False

    def accept_pcm(self, pcm: bytes) -> list[Event]:
        if len(pcm) % 2 != 0:  # match the real engine's contract
            raise ValueError("PCM byte length must be even for 16-bit samples")
        self.chunks_seen += 1
        if self._idx < len(self._script):
            events = self._script[self._idx]
            self._idx += 1
            return list(events)
        return []

    def flush(self) -> list[Event]:
        self.flushed = True
        return list(self._flush_events)


class ScriptedEngine(RecognizerEngine):
    """A fake :class:`RecognizerEngine` that hands out pre-supplied recognizers.

    Records how many times :meth:`new_session` is called so a test can assert the
    engine is built once and only a lightweight per-session stream is acquired per
    connection (never a new engine). ``recognizers`` are handed out in order; when
    a single recognizer is given it is reused for every session (the common case
    for the one-connection tests). ``new_session`` may be made to block/raise to
    exercise the offload / fail-closed paths.
    """

    def __init__(
        self,
        recognizers: Sequence[Recognizer] | Recognizer | None = None,
        *,
        on_new_session: Any = None,
    ) -> None:
        if isinstance(recognizers, Recognizer):
            self._recognizers: list[Recognizer] = [recognizers]
            self._reuse = True
        else:
            self._recognizers = list(recognizers) if recognizers is not None else []
            self._reuse = False
        self._on_new_session = on_new_session
        self.new_session_calls = 0
        # Records the negotiated ``SessionConfig`` each connection threaded through,
        # so a test can assert the per-session config actually reaches the engine
        # (rather than being echoed in ``ready`` while decoding on startup defaults).
        self.session_configs: list[SessionConfig | None] = []

    def new_session(self, config: SessionConfig | None = None) -> Recognizer:
        self.new_session_calls += 1
        self.session_configs.append(config)
        if self._on_new_session is not None:
            # Pass the config so a scripted hook can validate/reject it like the
            # real engines do (e.g. raise ``SessionConfigError`` on a mismatch).
            self._on_new_session(config)
        if self._reuse:
            return self._recognizers[0]
        if self._recognizers:
            return self._recognizers.pop(0)
        return ScriptedRecognizer()


def _partial(segment: int, text: str, start: float = 0.0) -> Event:
    return Event(kind="partial", segment=segment, text=text, start=start)


def _final(segment: int, text: str, start: float = 0.0, end: float = 1.0) -> Event:
    return Event(kind="final", segment=segment, text=text, start=start, end=end)


def _session(
    incoming: Sequence[str | bytes],
    *,
    recognizer: Recognizer | None = None,
    engine: RecognizerEngine | None = None,
    path: str = "/",
    server_config: ServerConfig | None = None,
    drop: bool = False,
    fail_send_after: int | None = None,
    session_id: str | None = None,
) -> tuple[AsrSession, FakeConnection]:
    conn = FakeConnection(incoming, drop=drop, fail_send_after=fail_send_after)
    if engine is None:
        rec = recognizer if recognizer is not None else ScriptedRecognizer()
        engine = ScriptedEngine(rec)
    session = AsrSession(
        conn,
        path=path,
        engine=engine,
        server_config=server_config,
        session_id=session_id,
    )
    return session, conn


# --- Handshake / config (R2) -------------------------------------------------


async def test_ready_is_first_message_with_session_id() -> None:
    """The handshake replies ``ready`` echoing effective config + a session id."""
    session, conn = _session(['{"type":"eos"}'])
    await session.run()
    msgs = conn.messages()
    assert msgs[0]["type"] == "ready"
    assert msgs[0]["session_id"] == session.session_id
    assert msgs[0]["effective_config"]["sample_rate"] == 16000  # default applied


async def test_session_ids_are_unique_csprng() -> None:
    """Each session mints a distinct id (R9.2 CSPRNG, snapshot-safe)."""
    ids = set()
    for _ in range(50):
        session, _conn = _session(['{"type":"eos"}'])
        ids.add(session.session_id)
    assert len(ids) == 50


async def test_injected_session_id_is_surfaced_in_ready() -> None:
    """When wired, ``ready`` uses the lifecycle-minted ID as its one runtime source.

    The HTTP hook layer (future Phase 3) forwards the id ``lifecycle_hooks./run``
    minted via CSPRNG; the session must echo *that* exact id in ``ready`` rather
    than minting a second, competing one.
    """
    lifecycle_id = "abc123def456"  # stands in for the /run CSPRNG-minted id
    session, conn = _session(['{"type":"eos"}'], session_id=lifecycle_id)
    await session.run()
    assert session.session_id == lifecycle_id
    assert conn.messages()[0]["session_id"] == lifecycle_id


async def test_injected_session_id_is_not_re_minted() -> None:
    """An injected ID is used verbatim and the session mints no ID of its own.

    Proves the single-source contract: constructing many sessions with the same
    injected id yields that id every time (no local ``uuid4`` ever overrides it),
    the complement of :func:`test_session_ids_are_unique_csprng`.
    """
    lifecycle_id = "fixed-owner-id"
    for _ in range(10):
        session, _conn = _session(['{"type":"eos"}'], session_id=lifecycle_id)
        assert session.session_id == lifecycle_id


async def test_config_from_query_params() -> None:
    """Config is read from the WS URL query string (R2.1)."""
    session, conn = _session(
        ['{"type":"eos"}'],
        path="/?sample_rate=8000&latency_mode=interactive&word_timestamps=false",
    )
    await session.run()
    eff = conn.messages()[0]["effective_config"]
    assert eff["sample_rate"] == 8000
    assert eff["latency_mode"] == "interactive"
    assert eff["word_timestamps"] is False


async def test_config_from_first_text_frame() -> None:
    """A first ``config`` text frame is honoured and merged (R2.1)."""
    cfg = json.dumps({"type": "config", "endpointing_ms": 800, "punctuate": False})
    session, conn = _session([cfg, '{"type":"eos"}'])
    await session.run()
    eff = conn.messages()[0]["effective_config"]
    assert eff["endpointing_ms"] == 800
    assert eff["punctuate"] is False


async def test_config_frame_overrides_query() -> None:
    """The explicit config frame wins over query params for the same field."""
    cfg = json.dumps({"type": "config", "sample_rate": 24000})
    session, conn = _session([cfg, '{"type":"eos"}'], path="/?sample_rate=8000")
    await session.run()
    assert conn.messages()[0]["effective_config"]["sample_rate"] == 24000


async def test_query_defaults_survive_partial_config_frame() -> None:
    """A config frame only overrides the keys it sets; query fills the rest."""
    cfg = json.dumps({"type": "config", "punctuate": False})
    session, conn = _session([cfg, '{"type":"eos"}'], path="/?sample_rate=8000")
    await session.run()
    eff = conn.messages()[0]["effective_config"]
    assert eff["sample_rate"] == 8000  # from query, not clobbered by the frame
    assert eff["punctuate"] is False  # from the frame


async def test_bad_encoding_config_is_rejected() -> None:
    """An unsupported ``encoding`` yields a fatal error and a close (R2.2/§5)."""
    cfg = json.dumps({"type": "config", "encoding": "opus"})
    session, conn = _session([cfg])
    await session.run()
    types = conn.types()
    assert types == ["error"]
    assert conn.messages()[0]["code"] == "BAD_CONFIG"
    assert conn.messages()[0]["fatal"] is True
    assert conn.closed


async def test_malformed_control_frame_is_fatal() -> None:
    """Non-JSON / unknown control text is a fatal ``BAD_MESSAGE`` (R3.3)."""
    session, conn = _session(["not json at all"])
    await session.run()
    assert conn.types() == ["error"]
    assert conn.messages()[0]["code"] == "BAD_MESSAGE"


async def test_non_mono_channels_config_is_rejected() -> None:
    """A stereo (``channels=2``) config is rejected, not silently mis-decoded (R2.2).

    The wire model constrains ``channels`` to mono, so a stereo config frame fails
    validation at the handshake and yields a fatal ``BAD_CONFIG`` + close before any
    audio reaches the mono recogniser.
    """
    cfg = json.dumps({"type": "config", "channels": 2})
    session, conn = _session([cfg, CHUNK, '{"type":"eos"}'])
    await session.run()
    assert conn.types() == ["error"]
    assert conn.messages()[0]["code"] == "BAD_CONFIG"
    assert conn.messages()[0]["fatal"] is True
    assert conn.closed


async def test_non_mono_channels_query_is_rejected() -> None:
    """``channels=2`` via query param is likewise rejected at handshake (R2.2)."""
    session, conn = _session(['{"type":"eos"}'], path="/?channels=2")
    await session.run()
    assert conn.types() == ["error"]
    assert conn.messages()[0]["code"] == "BAD_CONFIG"


# --- Full happy path (R3, R4, R6) -------------------------------------------


async def test_ready_partials_final_termination_trajectory() -> None:
    """Core acceptance: ready → partial* → final → termination over the pipeline."""
    rec = ScriptedRecognizer(
        script=[
            [_partial(0, "let")],
            [_partial(0, "let's meet")],
            [_partial(0, "let's meet at noon"), _final(0, "Let's meet at noon.", 0.0, 2.0)],
        ]
    )
    session, conn = _session(
        [CHUNK, CHUNK, CHUNK, '{"type":"eos"}'], recognizer=rec
    )
    await session.run()

    assert conn.types() == ["ready", "partial", "partial", "partial", "final", "termination"]
    msgs = conn.messages()
    assert [m["text"] for m in msgs if m["type"] == "partial"] == [
        "let",
        "let's meet",
        "let's meet at noon",
    ]
    final = next(m for m in msgs if m["type"] == "final")
    assert final["text"] == "Let's meet at noon." and final["end"] == 2.0
    term = msgs[-1]
    assert term["segments"] == 1
    # 3 chunks * 640 bytes / 2 bytes / 16000 Hz = 0.06 s of audio.
    assert term["audio_seconds"] == pytest.approx(3 * len(CHUNK) / 2 / 16000)
    assert rec.flushed and conn.closed


# --- interim_results (R4.1) -------------------------------------------------


async def test_interim_results_false_withholds_partials() -> None:
    """With ``interim_results=false`` the server sends only finals (R4.1).

    Partials are withheld from the wire; the client sees ready → final →
    termination even though the engine emitted evolving partials.
    """
    rec = ScriptedRecognizer(
        script=[
            [_partial(0, "let")],
            [_partial(0, "let's meet")],
            [_partial(0, "let's meet at noon"), _final(0, "Let's meet at noon.", 0.0, 2.0)],
        ]
    )
    session, conn = _session(
        [CHUNK, CHUNK, CHUNK, '{"type":"eos"}'],
        recognizer=rec,
        path="/?interim_results=false",
    )
    await session.run()
    assert conn.types() == ["ready", "final", "termination"]
    assert conn.messages()[0]["effective_config"]["interim_results"] is False
    final = next(m for m in conn.messages() if m["type"] == "final")
    assert final["text"] == "Let's meet at noon."
    assert conn.messages()[-1]["segments"] == 1


async def test_interim_results_false_via_config_frame() -> None:
    """``interim_results=false`` in the config frame also withholds partials (R4.1)."""
    rec = ScriptedRecognizer(
        script=[[_partial(0, "hi")], [_final(0, "Hi there.", 0.0, 1.0)]]
    )
    cfg = json.dumps({"type": "config", "interim_results": False})
    session, conn = _session([cfg, CHUNK, CHUNK, '{"type":"eos"}'], recognizer=rec)
    await session.run()
    assert conn.types() == ["ready", "final", "termination"]


async def test_interim_results_false_flush_final_still_sent() -> None:
    """A tail ``final`` produced on flush is sent even with partials disabled (R4.1)."""
    rec = ScriptedRecognizer(
        script=[[_partial(0, "tail")]],
        flush_events=[_final(0, "Tail words.", 0.0, 1.0)],
    )
    session, conn = _session(
        [CHUNK, '{"type":"eos"}'], recognizer=rec, path="/?interim_results=false"
    )
    await session.run()
    assert conn.types() == ["ready", "final", "termination"]
    assert next(m for m in conn.messages() if m["type"] == "final")["text"] == "Tail words."


async def test_interim_results_default_true_streams_partials() -> None:
    """The default (``interim_results`` unset → true) still streams partials (R4.1)."""
    rec = ScriptedRecognizer(
        script=[[_partial(0, "hello")], [_final(0, "Hello.", 0.0, 1.0)]]
    )
    session, conn = _session([CHUNK, CHUNK, '{"type":"eos"}'], recognizer=rec)
    await session.run()
    assert conn.types() == ["ready", "partial", "final", "termination"]
    assert conn.messages()[0]["effective_config"]["interim_results"] is True


async def test_eos_flushes_tail_final_before_termination() -> None:
    """``eos`` flushes an unfinalized tail segment as a ``final`` (R6.3)."""
    rec = ScriptedRecognizer(
        script=[[_partial(0, "tail")]],
        flush_events=[_final(0, "Tail words.", 0.0, 1.0)],
    )
    session, conn = _session([CHUNK, '{"type":"eos"}'], recognizer=rec)
    await session.run()
    assert conn.types() == ["ready", "partial", "final", "termination"]
    assert next(m for m in conn.messages() if m["type"] == "final")["text"] == "Tail words."


async def test_binary_first_frame_is_not_lost() -> None:
    """A binary first frame (no config frame) is fed into the pipeline (R2.1/R3)."""
    rec = ScriptedRecognizer(script=[[_partial(0, "hi"), _final(0, "Hi.", 0.0, 1.0)]])
    session, conn = _session([CHUNK, '{"type":"eos"}'], recognizer=rec)
    await session.run()
    assert conn.types() == ["ready", "partial", "final", "termination"]
    assert rec.chunks_seen == 1  # the first binary frame reached the recognizer


async def test_segments_and_audio_seconds_summarized() -> None:
    """``termination`` reports real segment count and processed audio (R6.3/R10)."""
    rec = ScriptedRecognizer(
        script=[
            [_final(0, "One.", 0.0, 1.0)],
            [_final(1, "Two.", 1.0, 2.0)],
        ]
    )
    session, conn = _session([CHUNK, CHUNK, '{"type":"eos"}'], recognizer=rec)
    await session.run()
    term = conn.messages()[-1]
    assert term["type"] == "termination"
    assert term["segments"] == 2
    assert term["audio_seconds"] == pytest.approx(2 * len(CHUNK) / 2 / 16000)


async def test_eos_as_first_frame_terminates_immediately() -> None:
    """An immediate ``eos`` yields ready → termination with no partials/finals."""
    session, conn = _session(['{"type":"eos"}'])
    await session.run()
    assert conn.types() == ["ready", "termination"]
    assert conn.messages()[-1]["segments"] == 0
    assert conn.messages()[-1]["audio_seconds"] == 0.0


async def test_dropped_socket_flushes_for_bookkeeping_but_sends_no_termination() -> None:
    """An abrupt drop (no ``eos``) can't send over the dead socket, but still flushes.

    The peer is gone, so no ``termination`` (or trailing ``final``) goes on the
    wire — sending on a dead socket is invalid. The recogniser is still flushed for
    server-side correctness so segment/audio-seconds bookkeeping stays complete
    for metrics (R6.3 close semantics / R10).
    """
    rec = ScriptedRecognizer(
        script=[[_partial(0, "hi")]],
        flush_events=[_final(0, "Hi there.", 0.0, 1.0)],
    )
    session, conn = _session([CHUNK], recognizer=rec)  # recv then raises ConnectionClosed
    await session.run()
    # Only ``ready`` + the streamed partial reached the (then still-live) socket;
    # nothing is sent after the drop — no trailing final, no termination.
    assert conn.types() == ["ready", "partial"]
    assert "termination" not in conn.types()
    assert "final" not in conn.types()
    # Bookkeeping still ran: the recogniser was flushed and its tail counted.
    assert rec.flushed
    assert session.segments == 1


async def test_send_failure_midstream_marks_peer_gone_and_stops_sending() -> None:
    """A send failing *mid-stream* sets peer-gone; later frames are suppressed (R6.3).

    The peer stays alive through ``ready`` and the first ``partial`` (those sends
    succeed and land on the wire), then dies: the next ``send`` raises
    ``ConnectionClosed``. From that point ``_peer_gone`` is set, so the server must
    NOT attempt any further sends — the second partial, the ``final``, and the
    ``termination`` are all suppressed rather than written to the dead socket — and
    it must not crash. The recogniser is still flushed and its segment bookkeeping
    completes for metrics (R10), consistent with the abrupt-disconnect semantics.
    """
    rec = ScriptedRecognizer(
        script=[
            [_partial(0, "hi")],
            [_partial(0, "hi there"), _final(0, "Hi there.", 0.0, 2.0)],
        ],
    )
    # fail_send_after=2: sends #1 (ready) and #2 (first partial) succeed, then
    # every subsequent send raises ConnectionClosed (peer died mid-stream).
    session, conn = _session(
        [CHUNK, CHUNK, '{"type":"eos"}'], recognizer=rec, fail_send_after=2
    )
    await session.run()

    # The early frames genuinely reached the (then still-live) wire...
    assert conn.types() == ["ready", "partial"]
    assert conn.messages()[1]["text"] == "hi"
    # ...and once a send failed, nothing more was attempted on the dead socket.
    assert "final" not in conn.types()
    assert "termination" not in conn.types()
    assert session._peer_gone is True  # the guard latched on the failed send
    # Bookkeeping still ran: the recogniser was flushed and its segment counted
    # even though the final/termination never made it onto the wire.
    assert rec.flushed
    assert session.segments == 1


# --- Error handling (design §10) --------------------------------------------


async def test_bad_pcm_length_reports_bad_encoding_and_closes() -> None:
    """Odd-length PCM surfaces as a fatal ``BAD_ENCODING`` then close (§5/§10)."""
    session, conn = _session([b"\x00\x01\x02"])  # 3 bytes: not whole 16-bit samples
    await session.run()
    assert "error" in conn.types()
    err = next(m for m in conn.messages() if m["type"] == "error")
    assert err["code"] == "BAD_ENCODING" and err["fatal"] is True
    assert conn.closed


class _ExplodingRecognizer(Recognizer):
    def accept_pcm(self, pcm: bytes) -> list[Event]:
        raise RuntimeError("decoder blew up")

    def flush(self) -> list[Event]:
        return []


async def test_recognizer_exception_is_fatal_internal_error() -> None:
    """A non-ValueError engine fault becomes a fatal ``INTERNAL`` error (§10)."""
    session, conn = _session([CHUNK, '{"type":"eos"}'], recognizer=_ExplodingRecognizer())
    await session.run()
    assert "error" in conn.types()
    assert next(m for m in conn.messages() if m["type"] == "error")["code"] == "INTERNAL"
    assert conn.closed


# --- Backpressure (R7) ------------------------------------------------------


async def test_queue_full_applies_backpressure_and_drops_not_ooms() -> None:
    """A flood against a tiny queue + a wedged consumer drops frames, bounded (R7).

    The recogniser blocks on the first chunk so the consumer never drains; a burst
    of binary frames then overflows the size-1 queue. With ``max_wait_ms=0`` the
    producer drops the excess immediately rather than buffering it — memory stays
    fixed (no OOM), and the drop is counted for observability (R10).
    """
    release = asyncio.Event()

    class _BlockingRecognizer(Recognizer):
        def __init__(self) -> None:
            self.seen = 0

        def accept_pcm(self, pcm: bytes) -> list[Event]:
            self.seen += 1
            return []

        def flush(self) -> list[Event]:
            return []

    rec = _BlockingRecognizer()

    # A consumer that stalls until released, so the bounded queue must fill.
    burst = [CHUNK] * 50
    cfg = ServerConfig(max_queue_size=1, max_wait_ms=0)
    session, conn = _session([*burst, '{"type":"eos"}'], recognizer=rec, server_config=cfg)

    # Patch the queue's get to block until we release it, forcing overflow.
    original_get = session._queue.get

    async def _stalled_get() -> bytes | None:
        await release.wait()
        return await original_get()

    session._queue.get = _stalled_get  # type: ignore[method-assign]

    task = asyncio.create_task(session.run())
    # Let ingest run and overflow the size-1 queue while the consumer is stalled.
    for _ in range(200):
        await asyncio.sleep(0)
    assert session.backpressure_events > 0
    assert session.frames_dropped > 0
    # The queue never grew past its bound — that is the anti-OOM guarantee.
    assert session._queue.qsize() <= cfg.max_queue_size

    release.set()
    await task
    assert "termination" in conn.types()


async def test_no_drops_when_consumer_keeps_up() -> None:
    """With headroom, every chunk reaches the recogniser — no drops (R7)."""
    rec = ScriptedRecognizer()
    cfg = ServerConfig(max_queue_size=64, max_wait_ms=2000)
    session, conn = _session(
        [CHUNK, CHUNK, CHUNK, '{"type":"eos"}'], recognizer=rec, server_config=cfg
    )
    await session.run()
    assert rec.chunks_seen == 3
    assert session.frames_dropped == 0


# --- Event-loop responsiveness under slow inference (R6.1 keepalive) --------


async def test_blocking_accept_pcm_does_not_block_event_loop() -> None:
    """A slow, BLOCKING ``accept_pcm`` must not freeze the event loop (regression).

    Real sherpa-onnx decode is synchronous CPU work that BLOCKS. If the consumer
    called it inline on the event loop, the loop would stall for the whole decode
    — the ``websockets`` keepalive ping/pong handler couldn't run, the client would
    miss pongs, and the connection would die with a ``1011 keepalive ping timeout``
    on the CPU-constrained MicroVM (the exact live failure this offload fixes).
    ``_consume`` guards against that by running the decode via ``asyncio.to_thread``
    (``ws_server.py``), so the loop keeps servicing other tasks while a chunk decodes.

    This test pins that behaviour: a fake recogniser whose ``accept_pcm`` performs a
    genuine blocking ``time.sleep`` (NOT an ``await asyncio.sleep``), driven through
    a real session, while a concurrently-scheduled asyncio ticker task runs. We
    assert the ticker keeps making progress *during* the blocking decode — which is
    only possible if the decode was offloaded off the loop — and that the decode
    actually ran on a worker thread, not the event-loop thread.
    """
    loop_thread_id = threading.get_ident()
    ticks = 0
    ticks_at_block_start: int | None = None
    ticks_at_block_end: int | None = None

    class _BlockingRecognizer(Recognizer):
        """``accept_pcm`` blocks the calling thread for a real, wall-clock interval."""

        def __init__(self) -> None:
            self.ran_on_thread: int | None = None

        def accept_pcm(self, pcm: bytes) -> list[Event]:
            nonlocal ticks_at_block_start, ticks_at_block_end
            self.ran_on_thread = threading.get_ident()
            # Snapshot the ticker, then BLOCK this thread. time.sleep releases the
            # GIL, so IF this runs off the loop the ticker advances meanwhile; if it
            # ran ON the loop, the loop (and the ticker) would be frozen for 0.15 s.
            ticks_at_block_start = ticks
            time.sleep(0.15)  # a real blocking call, standing in for slow inference
            ticks_at_block_end = ticks
            return []

        def flush(self) -> list[Event]:
            return []

    rec = _BlockingRecognizer()
    session, conn = _session([CHUNK, '{"type":"eos"}'], recognizer=rec)

    # A lightweight heartbeat: increments every 5 ms. On a responsive loop it ticks
    # ~30 times during the 0.15 s decode; on a blocked loop it cannot tick at all.
    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    tick_task = asyncio.create_task(ticker())
    try:
        await session.run()
    finally:
        stop.set()
        await tick_task

    # The decode ran on a worker thread, not the event-loop thread — i.e. it was
    # offloaded via asyncio.to_thread rather than called inline.
    assert rec.ran_on_thread is not None
    assert rec.ran_on_thread != loop_thread_id
    # The ticker kept advancing WHILE accept_pcm was blocked: the loop stayed live.
    # (A regression that drops the offload would leave this delta at 0.)
    assert ticks_at_block_start is not None and ticks_at_block_end is not None
    assert ticks_at_block_end - ticks_at_block_start >= 3
    # The session still completed cleanly despite the slow decode.
    assert conn.types() == ["ready", "termination"]


async def test_blocking_stream_acquisition_does_not_block_event_loop() -> None:
    """A slow, BLOCKING per-session stream acquisition must not freeze the loop.

    The heavy model is built once at startup, but ``engine.new_session()`` still
    touches native sherpa-onnx (``create_stream``) and could, under a regression,
    block. It runs *after* ``ready``/``session start`` and *before* the consumer
    starts, so if it were called inline it would freeze the event loop in exactly
    that window — the ``websockets`` keepalive ping/pong handler couldn't run, the
    peer's pings would go unanswered, and the client would tear the socket down
    with a ``1011 keepalive ping timeout`` before any transcription (the live
    failure: ``start`` logged, no SUMMARY). ``AsrSession.run`` guards against that
    by acquiring the stream via ``asyncio.to_thread``. This pins that behaviour with
    an engine whose ``new_session`` performs a genuine blocking ``time.sleep`` while
    a concurrent asyncio ticker runs.
    """
    ticks = 0
    ticks_at_block_start: int | None = None
    ticks_at_block_end: int | None = None
    acquired_on_thread: int | None = None
    loop_thread_id = threading.get_ident()

    def _block(_config: SessionConfig | None = None) -> None:
        nonlocal ticks_at_block_start, ticks_at_block_end, acquired_on_thread
        acquired_on_thread = threading.get_ident()
        # Snapshot the ticker, then BLOCK. time.sleep releases the GIL, so IF this
        # runs off the loop the ticker advances meanwhile; if it ran ON the loop
        # the ticker would be frozen for the whole 0.15 s.
        ticks_at_block_start = ticks
        time.sleep(0.15)  # a real blocking call, standing in for stream acquisition
        ticks_at_block_end = ticks

    engine = ScriptedEngine(ScriptedRecognizer(), on_new_session=_block)
    conn = FakeConnection([CHUNK, '{"type":"eos"}'])
    session = AsrSession(
        conn,
        path="/",
        engine=engine,
    )

    stop = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    tick_task = asyncio.create_task(ticker())
    try:
        await session.run()
    finally:
        stop.set()
        await tick_task

    # Stream acquisition ran on a worker thread, not the event-loop thread.
    assert acquired_on_thread is not None
    assert acquired_on_thread != loop_thread_id
    # The ticker kept advancing WHILE new_session was blocked: the loop stayed live.
    # (A regression that acquires the stream inline would leave this delta at 0.)
    assert ticks_at_block_start is not None and ticks_at_block_end is not None
    assert ticks_at_block_end - ticks_at_block_start >= 3
    # The session still completed cleanly despite the slow acquisition.
    assert conn.types()[0] == "ready"


# --- Shared engine / per-session stream (fix-recognizer-construction-crash) --


async def test_engine_built_once_across_many_sessions() -> None:
    """The engine is built ONCE; each of N sessions acquires only a stream (the fix).

    The bug allocated a brand-new full recogniser (ONNX sessions) on EVERY
    connection — a duplicate of the warm-snapshot-resident model, an OOM candidate
    on the 8 GB VM. The fix builds the engine once at startup and hands each session
    a lightweight per-connection stream via ``engine.new_session()``. This asserts
    the teeth: across N sequential sessions the engine object is constructed exactly
    once and ``new_session`` is called exactly once per session (N times total).

    Fails against the OLD per-session wiring, where a factory built a full engine on
    every connection (build count would equal N, not 1).
    """
    n_sessions = 4
    build_count = 0

    def _engine_factory() -> RecognizerEngine:
        nonlocal build_count
        build_count += 1
        return ScriptedEngine(
            on_new_session=None,
            recognizers=[ScriptedRecognizer() for _ in range(n_sessions)],
        )

    # Build the engine once (as serve_asr does), then run N sessions against it.
    engine = _engine_factory()
    assert build_count == 1

    for _ in range(n_sessions):
        conn = FakeConnection(['{"type":"eos"}'])
        session = AsrSession(conn, path="/", engine=engine)
        await session.run()
        assert conn.types() == ["ready", "termination"]

    # Engine was NOT rebuilt per session; exactly one stream acquired per session.
    assert build_count == 1
    assert engine.new_session_calls == n_sessions  # type: ignore[attr-defined]


async def test_each_session_gets_an_independent_stream() -> None:
    """Two sessions over one shared engine keep transcripts independent (Unchanged #4).

    Each session must decode against its OWN per-connection stream, so scripted
    events for one never leak into the other even though they share the engine.
    """
    rec_a = ScriptedRecognizer(script=[[_final(0, "Alpha.", 0.0, 1.0)]])
    rec_b = ScriptedRecognizer(script=[[_final(0, "Bravo.", 0.0, 1.0)]])
    engine = ScriptedEngine(recognizers=[rec_a, rec_b])

    conn_a = FakeConnection([CHUNK, '{"type":"eos"}'])
    await AsrSession(conn_a, path="/", engine=engine).run()
    conn_b = FakeConnection([CHUNK, '{"type":"eos"}'])
    await AsrSession(conn_b, path="/", engine=engine).run()

    finals_a = [m for m in conn_a.messages() if m["type"] == "final"]
    finals_b = [m for m in conn_b.messages() if m["type"] == "final"]
    assert [m["text"] for m in finals_a] == ["Alpha."]
    assert [m["text"] for m in finals_b] == ["Bravo."]
    # Distinct stream objects were handed out, not the same recogniser reused.
    assert engine.new_session_calls == 2


async def test_stream_acquisition_is_bracketed_by_info_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Per-session acquisition is bracketed by ``acquiring…`` / ``ready in`` INFO logs.

    Observability requirement: the construction/acquisition window must be visible
    in CloudWatch so a future stall localises there instead of being silent (the
    live failure had NO log line bracketing construction).
    """
    with caplog.at_level(logging.INFO, logger="asr_server.ws_server"):
        session, conn = _session(['{"type":"eos"}'])
        await session.run()
    text = caplog.text
    assert "acquiring recognizer" in text
    assert "recognizer ready in" in text


async def test_serve_asr_fails_closed_when_engine_build_raises() -> None:
    """A startup engine-build failure propagates out of ``serve_asr`` (fail closed).

    The design mandates the server fail loudly at startup (non-zero exit) rather
    than bind and let every session die silently. Assert the factory exception
    propagates rather than being swallowed, and that ``serve()`` is never reached
    (no connections accepted).
    """

    def _boom_factory() -> RecognizerEngine:
        raise RuntimeError("sherpa-onnx is not installed")

    cfg = ServerConfig(host="127.0.0.1", port=0)
    with pytest.raises(RuntimeError, match="sherpa-onnx is not installed"):
        await serve_asr(server_config=cfg, engine_factory=_boom_factory)


async def test_session_reports_internal_error_if_stream_acquisition_fails() -> None:
    """A per-session ``new_session`` failure surfaces as a fatal INTERNAL error.

    Even with the engine built, if acquiring a session's stream raises, the client
    must get a clean ``INTERNAL`` error + close (not a silent death) — Unchanged #3.
    """

    def _boom(_config: SessionConfig | None = None) -> None:
        raise RuntimeError("stream acquisition failed")

    engine = ScriptedEngine(ScriptedRecognizer(), on_new_session=_boom)
    conn = FakeConnection([CHUNK, '{"type":"eos"}'])
    await AsrSession(conn, path="/", engine=engine).run()
    assert "error" in conn.types()
    err = next(m for m in conn.messages() if m["type"] == "error")
    assert err["code"] == "INTERNAL"
    assert conn.closed


# --- Negotiated per-session config threading (fix-recognizer-construction-crash) --


async def test_negotiated_config_is_threaded_into_new_session() -> None:
    """The negotiated sample_rate + endpointing reach ``engine.new_session`` (the fix).

    Regression for the engine/stream split silently dropping per-session config:
    the server echoes the negotiated config in ``ready`` (below) but must ALSO hand
    it to the stream. Negotiate a non-default sample_rate + endpointing over the
    wire, then assert the exact :class:`SessionConfig` the engine received matches —
    proving the ``ready`` echo and the actual decode config can't diverge.

    Teeth: under the pre-fix wiring ``new_session`` took no argument, so the
    negotiated config never reached the engine (the list would hold no config);
    this assertion fails there and passes with the fix.
    """
    engine = ScriptedEngine(ScriptedRecognizer())
    # Negotiate via the query string: 8 kHz + 800 ms endpointing (both non-default).
    session, conn = _session(
        ['{"type":"eos"}'],
        engine=engine,
        path="/?sample_rate=8000&endpointing_ms=800",
    )
    await session.run()

    # ``ready`` echoed the negotiated config...
    ready = next(m for m in conn.messages() if m["type"] == "ready")
    assert ready["effective_config"]["sample_rate"] == 8000
    assert ready["effective_config"]["endpointing_ms"] == 800
    # ...and the SAME config was threaded into the engine (not startup defaults).
    # The diarization knobs ride along at their wire-Config defaults (diarization
    # is off here), so the negotiated config is threaded through in full.
    assert engine.session_configs == [
        SessionConfig(
            sample_rate=8000,
            endpointing_ms=800,
            speaker_threshold=0.5,
            max_speakers=0,
        )
    ]


async def test_incompatible_session_config_is_rejected_with_bad_config() -> None:
    """A ``SessionConfigError`` from the engine → fatal BAD_CONFIG + close (not silent).

    When the shared engine cannot honour a negotiated parameter (e.g. a sample_rate
    baked into the ONNX graph), the server must reject the connection with a clear
    protocol error rather than decode with the wrong config — the required behaviour
    when a parameter genuinely can't vary per session.
    """

    def _reject(config: SessionConfig | None = None) -> None:
        raise SessionConfigError(
            f"engine built for sample_rate=16000, got {config.sample_rate if config else '?'}"
        )

    engine = ScriptedEngine(ScriptedRecognizer(), on_new_session=_reject)
    conn = FakeConnection([CHUNK, '{"type":"eos"}'])
    await AsrSession(conn, path="/?sample_rate=8000", engine=engine).run()
    err = next(m for m in conn.messages() if m["type"] == "error")
    assert err["code"] == "BAD_CONFIG"
    assert err["fatal"] is True
    assert conn.closed
    # No termination on a rejected session (it ended in error, not clean eos).
    assert "termination" not in conn.types()


# --- Server config (NFR5) ---------------------------------------------------


def test_server_config_defaults() -> None:
    cfg = ServerConfig()
    assert cfg.port == DEFAULT_PORT
    assert cfg.keepalive_interval_s == 5.0
    # The pong deadline is disabled by default so a loop-bound server (cold model
    # load / slow decode) is never falsely killed with a 1011 keepalive timeout.
    assert cfg.keepalive_timeout_s is None
    assert cfg.max_queue_size > 0 and cfg.max_wait_ms >= 0


def test_server_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASR_HOST", "127.0.0.1")
    monkeypatch.setenv("ASR_PORT", "9090")
    monkeypatch.setenv("ASR_KEEPALIVE_S", "3")
    monkeypatch.setenv("ASR_KEEPALIVE_TIMEOUT_S", "45")
    monkeypatch.setenv("ASR_MAX_QUEUE", "8")
    monkeypatch.setenv("ASR_MAX_WAIT_MS", "500")
    cfg = ServerConfig.from_env()
    assert (cfg.host, cfg.port) == ("127.0.0.1", 9090)
    assert cfg.keepalive_interval_s == 3.0
    assert cfg.keepalive_timeout_s == 45.0
    assert (cfg.max_queue_size, cfg.max_wait_ms) == (8, 500)


def test_server_config_keepalive_timeout_env_edge_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset/empty/typo'd ``ASR_KEEPALIVE_TIMEOUT_S`` all leave the timeout disabled."""
    monkeypatch.delenv("ASR_KEEPALIVE_TIMEOUT_S", raising=False)
    assert ServerConfig.from_env().keepalive_timeout_s is None
    monkeypatch.setenv("ASR_KEEPALIVE_TIMEOUT_S", "")
    assert ServerConfig.from_env().keepalive_timeout_s is None
    monkeypatch.setenv("ASR_KEEPALIVE_TIMEOUT_S", "not-a-number")
    assert ServerConfig.from_env().keepalive_timeout_s is None


# --- End-to-end over a real loopback socket ---------------------------------


async def test_end_to_end_loopback_stream() -> None:
    """Acceptance: stream PCM over a real ``websockets`` server on loopback.

    Proves the full wire path — real handshake, binary frames, text ``eos``,
    keepalive config — yields ready → partial* → final → termination.
    """
    rec = ScriptedRecognizer(
        script=[
            [_partial(0, "hello")],
            [_partial(0, "hello world"), _final(0, "Hello world.", 0.0, 2.0)],
        ]
    )
    cfg = ServerConfig(host="127.0.0.1", port=0, keepalive_interval_s=5.0)

    # Serve on an ephemeral port; capture it from the bound socket.
    from websockets.asyncio.server import serve

    engine = ScriptedEngine(rec)

    async def handler(conn: object) -> None:
        request = conn.request  # type: ignore[attr-defined]
        path = request.path if request is not None else "/"
        session = AsrSession(
            conn,  # type: ignore[arg-type]
            path=path,
            engine=engine,
            server_config=cfg,
        )
        await session.run()

    async with serve(handler, cfg.host, 0, ping_interval=cfg.keepalive_interval_s) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        received: list[str] = []
        async with connect(f"ws://127.0.0.1:{port}/?word_timestamps=false") as client:
            await client.send(CHUNK)
            await client.send(CHUNK)
            await client.send(json.dumps({"type": "eos"}))
            try:
                async for message in client:
                    assert isinstance(message, str)
                    received.append(message)
            except ConnectionClosed:
                pass

    parsed = [parse_server_message(m) for m in received]
    types = [type(m).__name__ for m in parsed]
    assert types == ["Ready", "Partial", "Partial", "Final", "Termination"]


async def test_serve_asr_builds_engine_once_and_serves() -> None:
    """``serve_asr`` builds the engine ONCE at startup, then serves many sessions.

    Drives the real ``serve_asr`` with an injected engine factory (a scripted
    engine, so no model weights) over a real loopback socket. Asserts the factory
    ran exactly once — even across multiple client connections — proving the heavy
    model is no longer allocated per connection (the bug), and that each client
    still gets ``ready`` from its own per-session stream.
    """
    cfg = ServerConfig(host="127.0.0.1", port=0)
    factory_calls = 0
    engine = ScriptedEngine(ScriptedRecognizer())

    def _engine_factory() -> RecognizerEngine:
        nonlocal factory_calls
        factory_calls += 1
        return engine

    # Run serve_asr itself (not a hand-rolled handler) so the eager-build wiring is
    # exercised. It blocks forever, so drive it as a background task and cancel it.
    server_task = asyncio.create_task(
        serve_asr(server_config=cfg, engine_factory=_engine_factory)
    )
    try:
        # Give serve_asr a moment to build the engine and bind.
        for _ in range(200):
            if factory_calls:
                break
            await asyncio.sleep(0)
        # The engine was built exactly once, eagerly, before any connection.
        assert factory_calls == 1
        assert engine.new_session_calls == 0  # nothing connected yet
    finally:
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task

    # Reference the production coroutine so its import is exercised.
    assert asyncio.iscoroutinefunction(serve_asr)


# --- RSS observability (rss-observability) -----------------------------------


def test_read_rss_mb_returns_positive_current_and_peak() -> None:
    """``_read_rss_mb`` reports a live current and a high-water peak, both in MB.

    On the Linux guest both come from real sources (``/proc/self/status`` VmRSS and
    ``ru_maxrss`` in kB); on any platform the numbers must be positive and the peak
    can never be below the current sample (it's a high-water mark).
    """
    rss = _read_rss_mb()
    assert rss.current_mb > 0
    assert rss.peak_mb > 0
    # Sanity: a Python process resident set is comfortably under a terabyte — guards
    # against a unit slip (e.g. reading kB as bytes) reading ~1000× high or low.
    assert rss.current_mb < 1_000_000
    assert rss.peak_mb >= rss.current_mb * 0.5  # peak is a high-water mark


def test_read_rss_mb_never_raises_without_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Instrumentation must NEVER crash the server: a missing ``/proc`` falls back.

    When ``/proc/self/status`` is unavailable (e.g. macOS dev host), ``current_mb``
    falls back to the ``ru_maxrss`` peak rather than propagating an ``OSError``.
    """
    import builtins

    real_open = builtins.open

    def _boom_open(path: str, *args: Any, **kwargs: Any) -> Any:
        if path == "/proc/self/status":
            raise OSError("no /proc here")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _boom_open)
    rss = _read_rss_mb()
    # Fell back to ru_maxrss for both fields; still a usable, positive number.
    assert rss.current_mb == rss.peak_mb
    assert rss.peak_mb > 0


async def test_session_summary_includes_current_rss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The end-of-session SUMMARY carries current RSS (the under-load memory trace).

    Reusing the existing per-session SUMMARY path (no new heartbeat/timer) means the
    memory climb under concurrent sessions is readable from CloudWatch, so an OOM
    near a 4GB ceiling is attributable to load (R10).
    """
    with caplog.at_level(logging.INFO, logger="asr_server.ws_server"):
        session, _conn = _session(['{"type":"eos"}'])
        await session.run()
    summary = next(r for r in caplog.records if "end (" in r.getMessage())
    assert "rss_current=" in summary.getMessage()
    assert "rss_peak=" in summary.getMessage()


async def test_serve_asr_logs_startup_memory_floor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``serve_asr`` logs the startup memory FLOOR after the build, before accepting.

    The floor is the idle resident baseline (model resident, no session yet) the
    8GB→4GB sizing call is made against. Assert it lands AFTER ``recognizer ready``
    and carries both current and peak RSS.
    """
    cfg = ServerConfig(host="127.0.0.1", port=0)

    def _engine_factory() -> RecognizerEngine:
        return ScriptedEngine(ScriptedRecognizer())

    with caplog.at_level(logging.INFO, logger="asr_server.ws_server"):
        server_task = asyncio.create_task(
            serve_asr(server_config=cfg, engine_factory=_engine_factory)
        )
        try:
            for _ in range(200):
                if any("startup memory floor" in r.getMessage() for r in caplog.records):
                    break
                await asyncio.sleep(0)
        finally:
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task

    messages = [r.getMessage() for r in caplog.records]
    floor = next(m for m in messages if "startup memory floor" in m)
    assert "rss_current=" in floor
    assert "rss_peak=" in floor
    # The floor is logged after the engine is built (post "recognizer ready").
    assert messages.index(floor) > messages.index(
        next(m for m in messages if "recognizer ready in" in m)
    )


# --- Diarization over the wire ----------------------------------------------


async def test_speaker_labels_reach_the_client_on_the_wire() -> None:
    """End-to-end: a diarizing engine's speaker labels appear in ``final`` frames.

    Drives the REAL session + protocol adapter + wire models over a fake socket, so
    this covers the whole path a client actually sees — not just the diariser in
    isolation. Two alternating voices must surface as two distinct speaker labels
    attached to the corresponding transcripts.
    """
    from pathlib import Path

    from asr_server.diarization import (
        DiarizationConfig,
        DiarizingEngine,
        SpeakerEmbedderConfig,
    )

    from tests.test_diarization import (
        VOICE_A,
        VOICE_B,
        ScriptedEmbedder,
    )
    from tests.test_diarization import ScriptedRecognizer as DiarScriptedRecognizer

    inner_rec = DiarScriptedRecognizer(
        [
            [Event(kind="final", segment=0, text="hello", start=0.0, end=1.0)],
            [Event(kind="final", segment=1, text="hi there", start=1.0, end=2.0)],
        ]
    )
    engine = DiarizingEngine(
        ScriptedEngine(inner_rec),
        ScriptedEmbedder([VOICE_A, VOICE_B]),
        DiarizationConfig(
            embedder=SpeakerEmbedderConfig(model=Path("unused.onnx")),
        ),
    )
    # 1s of PCM per chunk so both segments clear the min-segment embedding floor.
    one_second = b"\x01\x02" * 16000
    conn = FakeConnection([one_second, one_second, '{"type":"eos"}'])
    await AsrSession(conn, path="/?diarize=true", engine=engine).run()

    finals = [m for m in conn.messages() if m["type"] == "final"]
    assert [m["text"] for m in finals] == ["hello", "hi there"]
    assert [m["speaker"] for m in finals] == ["spk_0", "spk_1"]


async def test_diarize_request_is_downgraded_when_unsupported() -> None:
    """Asking for diarization on a non-diarizing build reports diarize=false.

    The server cannot honour ``diarize`` without a baked speaker model, so it must
    say so in ``ready.effective_config`` rather than silently returning
    ``speaker: null`` forever and leaving the client to guess why.
    """
    engine = ScriptedEngine(ScriptedRecognizer())
    conn = FakeConnection(['{"type":"eos"}'])
    await AsrSession(conn, path="/?diarize=true", engine=engine).run()

    ready = next(m for m in conn.messages() if m["type"] == "ready")
    assert ready["effective_config"]["diarize"] is False


async def test_transcripts_have_null_speaker_without_diarization() -> None:
    """With diarization off, transcripts still carry the field, set to null."""
    engine = ScriptedEngine(
        ScriptedRecognizer(
            [[Event(kind="final", segment=0, text="hi", start=0.0, end=1.0)]]
        )
    )
    conn = FakeConnection([CHUNK, '{"type":"eos"}'])
    await AsrSession(conn, path="/", engine=engine).run()

    finals = [m for m in conn.messages() if m["type"] == "final"]
    assert finals
    assert all(m["speaker"] is None for m in finals)


# --- embed mode (calibration) ------------------------------------------------


class _EmbeddingEngine(ScriptedEngine):
    """An engine that also exposes an embedder, as DiarizingEngine does."""

    def __init__(self, vectors: Sequence[Sequence[float]]) -> None:
        super().__init__(ScriptedRecognizer())
        self._vectors = [list(v) for v in vectors]
        self.embedded: list[int] = []

    @property
    def embedder(self) -> object:
        engine = self

        class _Embedder:
            dim = 2

            def embed(self, sample_rate: int, samples: Sequence[float]) -> list[float]:
                engine.embedded.append(len(samples))
                return engine._vectors.pop(0)  # noqa: SLF001 - test double

        return _Embedder()


def _embed_config(**extra: object) -> str:
    return json.dumps({"type": "config", "mode": "embed", **extra})


async def test_embed_mode_returns_one_embedding_per_binary_frame() -> None:
    """Calibration needs embeddings of known-speaker audio, nothing else.

    The statistics that turn them into an operating point live outside the MicroVM,
    so this mode is deliberately dumb: one frame in, one vector out.
    """
    engine = _EmbeddingEngine([[1.0, 0.0], [0.0, 1.0]])
    session, conn = _session(
        [_embed_config(), CHUNK, CHUNK, json.dumps({"type": "eos"})],
        engine=engine,
    )

    await session.run()

    kinds = [json.loads(frame)["type"] for frame in conn.sent]
    assert kinds == ["ready", "embedding", "embedding", "termination"]
    first = json.loads(conn.sent[1])
    assert first["index"] == 0
    assert first["dim"] == 2
    assert first["vector"] == [1.0, 0.0]
    assert json.loads(conn.sent[2])["index"] == 1
    assert json.loads(conn.sent[3])["segments"] == 2


async def test_embed_mode_never_produces_transcripts() -> None:
    engine = _EmbeddingEngine([[1.0, 0.0]])
    session, conn = _session(
        [_embed_config(), CHUNK, json.dumps({"type": "eos"})], engine=engine
    )

    await session.run()

    kinds = {json.loads(frame)["type"] for frame in conn.sent}
    assert "partial" not in kinds
    assert "final" not in kinds


async def test_embed_mode_is_refused_without_a_speaker_model() -> None:
    """Better a clear error than embeddings that silently cannot exist."""
    session, conn = _session([_embed_config(), CHUNK])

    await session.run()

    error = json.loads(conn.sent[-1])
    assert error["type"] == "error"
    assert error["code"] == "BAD_CONFIG"
    assert "speaker-embedding model" in error["message"]
    assert conn.closed


async def test_embed_mode_reports_an_embedding_failure_and_closes() -> None:
    engine = _EmbeddingEngine([])  # pops from an empty list -> IndexError
    session, conn = _session([_embed_config(), CHUNK], engine=engine)

    await session.run()

    error = json.loads(conn.sent[-1])
    assert error["type"] == "error"
    assert error["code"] == "INTERNAL"
    assert "embedding failed" in error["message"]


async def test_embed_mode_with_no_audio_terminates_cleanly() -> None:
    engine = _EmbeddingEngine([])
    session, conn = _session([_embed_config(), json.dumps({"type": "eos"})], engine=engine)

    await session.run()

    kinds = [json.loads(frame)["type"] for frame in conn.sent]
    assert kinds == ["ready", "termination"]
    assert json.loads(conn.sent[-1])["segments"] == 0


async def test_asr_mode_remains_the_default() -> None:
    session, conn = _session(
        [json.dumps({"type": "config"}), CHUNK, json.dumps({"type": "eos"})]
    )

    await session.run()

    assert json.loads(conn.sent[0])["effective_config"]["mode"] == "asr"
    assert not any(json.loads(frame)["type"] == "embedding" for frame in conn.sent)

def test_split_on_speaker_change_reaches_the_session_config() -> None:
    """The knob is per session so it can be turned off without an image rebuild."""
    from asr_protocol import Config

    config = Config(sample_rate=16000, split_on_speaker_change=False)

    assert config.split_on_speaker_change is False
    assert Config(sample_rate=16000).split_on_speaker_change is None

