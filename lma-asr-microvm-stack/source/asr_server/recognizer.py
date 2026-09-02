# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""sherpa-onnx ``OnlineRecognizer`` wrapper for streaming ASR.

Defines the ``Recognizer`` interface shared by the streaming and accurate
engines (design.md §4)::

    accept_pcm(self, pcm: bytes) -> list[Event]
    flush(self)               -> list[Event]

and implements :class:`SherpaOnlineRecognizer`, which drives a frame-synchronous
sherpa-onnx ``OnlineRecognizer`` (the Nemotron cache-aware FastConformer-RNNT
streaming model per ``docs/asr-model-selection-and-recipe.md`` §1/§2).

``Event`` is the **engine-internal** incremental recognition event. It is
deliberately *not* the ``asr_protocol`` wire model: the protocol adapter
maps these engine events onto the ``Partial``/``Final`` wire messages, so this
module never imports the wire schema and the two can evolve independently.

Testability without model weights
---------------------------------
The runtime fails closed when model files are unavailable, so the
``sherpa_onnx`` / ``numpy`` wheels need not be importable here. The recogniser
therefore depends only on the small :class:`DecoderBackend` protocol and takes a
backend by injection. Tests supply a scripted fake backend; production uses
:func:`create_sherpa_backend`, which lazily imports ``sherpa_onnx`` only when a
real engine is actually constructed.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from abc import ABC, abstractmethod
from array import array
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

__all__ = [
    "WordTiming",
    "Event",
    "Recognizer",
    "RecognizerEngine",
    "SessionConfig",
    "SessionConfigError",
    "DecoderBackend",
    "SherpaModelConfig",
    "SherpaOnlineRecognizer",
    "SherpaOnlineEngine",
    "build_model_config",
    "create_sherpa_backend",
    "create_sherpa_engine",
]

# Default model directory baked into the MicroVM image (design §7 COPYs the
# model into ``/opt/models``). Overridable via ``--model`` / ``ASR_MODEL_DIR``.
DEFAULT_MODEL_DIR = "/opt/models"

# Standard filenames a sherpa-onnx streaming transducer export ships, resolved
# relative to the model dir unless an explicit path / ``ASR_MODEL_*`` env
# overrides them.
_MODEL_FILES: tuple[tuple[str, str, str], ...] = (
    ("tokens", "ASR_MODEL_TOKENS", "tokens.txt"),
    ("encoder", "ASR_MODEL_ENCODER", "encoder.onnx"),
    ("decoder", "ASR_MODEL_DECODER", "decoder.onnx"),
    ("joiner", "ASR_MODEL_JOINER", "joiner.onnx"),
)

# 16-bit signed PCM: full-scale magnitude used to normalise samples to [-1, 1).
_INT16_FULL_SCALE = 32768.0


# --- Engine-internal events -------------------------------------------------


@dataclass(frozen=True)
class WordTiming:
    """Per-word timing for a finalized segment (engine-internal, not wire)."""

    w: str
    s: float
    e: float


@dataclass(frozen=True)
class Event:
    """One incremental recognition event emitted by a :class:`Recognizer`.

    ``kind`` is ``"partial"`` for an evolving hypothesis mid-segment and
    ``"final"`` when the segment is finalized at an endpoint (or on flush).
    ``segment`` is a session-monotonic counter. ``start``/``end`` are audio
    timestamps in seconds; ``end`` is only set on ``final`` events. ``words``
    carries optional per-word timings on a ``final`` when the engine provides
    them (the streaming path leaves it ``None``; the offline path can populate it).

    ``speaker`` is the diarized speaker label (e.g. ``"spk_0"``) when speaker
    diarization is enabled, else ``None``. The recognisers themselves never set
    it — :class:`asr_server.diarization.DiarizingRecognizer` decorates an engine
    and stamps it on, deriving the label from the *same* samples and segment
    boundaries carried here so the speaker can never drift from the text.
    """

    kind: Literal["partial", "final"]
    segment: int
    text: str
    start: float | None = None
    end: float | None = None
    words: list[WordTiming] | None = None
    speaker: str | None = None


# --- Per-session negotiated config -----------------------------------------


@dataclass(frozen=True)
class SessionConfig:
    """Per-connection recogniser knobs negotiated during the handshake.

    Carries only the subset of the wire :class:`asr_protocol.Config` that shapes
    decode behaviour, decoupled from the wire schema so the engine layer never
    imports it (mirrors :class:`Event` vs the wire messages). :meth:`RecognizerEngine.new_session`
    takes one so the negotiated config actually reaches the per-session stream,
    rather than being echoed in ``ready`` while decoding runs on startup defaults.

    * ``sample_rate`` — the client's PCM sample rate. It is baked into the shared
      ONNX graph at engine-build time, so it **cannot vary per session** against a
      shared engine: :meth:`RecognizerEngine.new_session` validates it and raises
      :class:`SessionConfigError` on a mismatch rather than silently decoding at the
      wrong rate (design "Per-session config" §).
    * ``endpointing_ms`` — trailing-silence endpointing. For the offline/accurate
      engine this varies per session (applied to the per-session VAD gate). For the
      streaming engine it is baked into the shared recognizer's endpoint rules and
      so cannot vary per session; ``None`` means "use the engine's built-in value".
    * ``speaker_threshold`` / ``max_speakers`` / ``min_segment_ms`` /
      ``require_corroboration`` — speaker-diarization knobs, honoured only when
      diarization is enabled (:mod:`asr_server.diarization`). Unlike the two above,
      these genuinely DO vary per session: they live in the per-session speaker
      registry rather than in any shared ONNX graph, so a connection can declare its
      own conversation size (e.g. ``max_speakers=2`` for a two-party call) with no
      engine rebuild. ``None`` means "use the engine's default".

      They are per-session precisely so the operating point can be tuned without
      rebuilding an image: the correct threshold is empirical and specific to the
      speaker model, so it has to be changeable in seconds, not minutes.
    * ``live_turn_cut`` / ``turn_cut_interval_ms`` / ``max_open_segment_ms`` — when
      to close a row before the utterance ends, so a speaker change (rather than a
      pause) is what separates rows. Per-session for the same reason as above.
    """

    sample_rate: int = 16000
    endpointing_ms: int | None = None
    speaker_threshold: float | None = None
    max_speakers: int | None = None
    min_segment_ms: int | None = None
    require_corroboration: bool | None = None
    split_on_speaker_change: bool | None = None
    live_turn_cut: bool | None = None
    turn_cut_interval_ms: int | None = None
    max_open_segment_ms: int | None = None


class SessionConfigError(ValueError):
    """A negotiated per-session config is incompatible with the shared engine.

    Raised by :meth:`RecognizerEngine.new_session` when a parameter baked into the
    shared engine at build time (e.g. ``sample_rate``) cannot be honoured for this
    connection. The server maps it to a fatal ``BAD_CONFIG`` wire error and closes,
    rather than decoding with the wrong config (design "Per-session config" §).
    """


# --- Interfaces -------------------------------------------------------------


class Recognizer(ABC):
    """Per-session decode surface for the streaming and accurate paths (design §4).

    One instance drives **one utterance stream**: it carries the per-session
    decoder state (segment counter, in-progress hypothesis) and is cheap to
    create. The heavy, shared model lives in a :class:`RecognizerEngine`, which
    hands out a fresh ``Recognizer`` per connection via :meth:`new_session`.
    """

    @abstractmethod
    def accept_pcm(self, pcm: bytes) -> list[Event]:
        """Feed one chunk of 16 kHz/16-bit/mono/LE PCM, return any new events."""

    @abstractmethod
    def flush(self) -> list[Event]:
        """Signal end-of-audio; finalize any in-progress segment."""

    def current_segment(self) -> int:
        """Index of the segment still open, i.e. the next ``final``'s number.

        Needed so a decorator that emits its own extra finals can number them on the
        same sequence the inner recogniser is using.
        """
        return 0

    def current_words(self) -> list[WordTiming]:
        """Word timings for the segment still OPEN, when the engine has them.

        Needed to cut a row at a speaker change before the utterance ends: text can
        only be split at a word boundary, and until the segment closes its words are
        not on any event. Asked for on demand — and rarely — rather than attached to
        every partial, because partials fire on each hypothesis change while a cut is
        considered about once a second.

        Defaults to none, which disables live cutting for engines that cannot report
        mid-utterance timings (the offline path decodes only at silence).
        """
        return []


class RecognizerEngine(ABC):
    """The heavy, shared ASR model — built ONCE, reused across all sessions.

    An engine owns the expensive resources (ONNX sessions, onnxruntime arenas)
    that must not be re-allocated per connection: duplicating a full model on
    every WebSocket open is the OOM-on-an-8GB-VM failure this seam fixes. The
    engine is constructed eagerly at server startup, reusing the model already
    resident from the build-time warmup snapshot, and each connection acquires a
    lightweight per-session :class:`Recognizer` via :meth:`new_session`.
    """

    @abstractmethod
    def new_session(self, config: SessionConfig | None = None) -> Recognizer:
        """Return a fresh per-connection :class:`Recognizer` sharing this engine.

        Cheap: allocates only per-utterance decoder state (a new sherpa-onnx
        stream + adapter counters), never a new model. Sessions returned by the
        same engine keep their transcripts independent (design Unchanged #4).

        ``config`` carries the connection's negotiated :class:`SessionConfig` so
        per-session-safe parameters (e.g. the offline engine's ``endpointing_ms``)
        reach this session's stream. A parameter baked into the shared engine at
        build time that the client negotiated incompatibly (e.g. a ``sample_rate``
        different from the built graph) raises :class:`SessionConfigError` — the
        server rejects the connection rather than decoding with the wrong config.
        ``None`` uses the engine's build-time defaults (standalone / test use).
        """


class DecoderBackend(Protocol):
    """Minimal decoding surface the recogniser drives — one backend per session.

    Wraps a single sherpa-onnx recognizer + stream. Kept intentionally tiny so a
    scripted fake can stand in for the real engine in tests (no weights needed).
    """

    def accept_waveform(self, sample_rate: int, samples: Sequence[float]) -> None:
        """Hand normalised float samples in [-1, 1) to the decoder."""

    def decode(self) -> None:
        """Drain all frames the backend is currently ready to decode."""

    def current_text(self) -> str:
        """Best hypothesis text decoded so far for the active segment."""

    def is_endpoint(self) -> bool:
        """Whether the backend has detected an utterance endpoint."""

    def reset(self) -> None:
        """Start a fresh segment after an endpoint (clears decoded text)."""

    def input_finished(self) -> None:
        """Mark the audio stream complete so the tail can be decoded."""

    def current_words(self) -> list[WordTiming]:
        """Per-word timings for the active segment, when the backend has them.

        Optional: a backend without this method yields finals with no word timings,
        which disables features that need to cut a segment at a time (see
        :mod:`asr_server.segmentation`).
        """


# SentencePiece word-boundary marker: sherpa-onnx transducer tokens use '▁' to mark
# the start of a new word.
WORD_BOUNDARY = "▁"


def words_from_tokens(
    tokens: Sequence[str], timestamps: Sequence[float]
) -> list[WordTiming]:
    """Reconstruct word timings from sherpa per-token tokens + timestamps.

    Best-effort: returns ``[]`` when the arrays do not line up, so a backend that
    reports tokens without timings degrades to no word timings rather than to wrong
    ones.
    """
    if not tokens or len(tokens) != len(timestamps):
        return []
    words: list[WordTiming] = []
    cur = ""
    cur_start = 0.0
    cur_end = 0.0
    for tok, ts in zip(tokens, timestamps, strict=False):
        piece = tok.replace(WORD_BOUNDARY, " ")
        # sherpa returns the SentencePiece marker already converted to a space
        # (' THE', ' YE', 'LL', ...), so a leading space marks a word start too.
        # Keying only off the marker glued every utterance into a single "word".
        starts_word = tok.startswith(WORD_BOUNDARY) or tok.startswith(" ")
        if starts_word and cur.strip():
            words.append(WordTiming(w=cur.strip(), s=cur_start, e=cur_end))
            cur = ""
        if not cur.strip():
            cur_start = ts
        cur += piece
        cur_end = ts
    if cur.strip():
        words.append(WordTiming(w=cur.strip(), s=cur_start, e=cur_end))
    return words


def anchor_words(words: Sequence[WordTiming], start: float) -> list[WordTiming]:
    """Re-base word timings so the first word starts at ``start``.

    A streaming decoder's timestamps are relative to its own stream, which is reset
    per segment, so they are used for spacing within the segment and anchored to the
    segment's own start rather than trusted as absolute session times.
    """
    if not words:
        return []
    offset = start - words[0].s
    return [WordTiming(w=word.w, s=word.s + offset, e=word.e + offset) for word in words]


# --- PCM helpers ------------------------------------------------------------


def _pcm16_to_float32(pcm: bytes) -> list[float]:
    """Decode little-endian 16-bit signed PCM bytes to floats in [-1, 1).

    Pure stdlib (no numpy) so the engine stays importable and testable without
    the ARM inference wheels installed.
    """
    if len(pcm) % 2 != 0:
        raise ValueError("PCM byte length must be even for 16-bit samples")
    samples = array("h")
    samples.frombytes(pcm)
    # ``array('h')`` uses native byte order; the wire format is little-endian.
    if sys.byteorder != "little":
        samples.byteswap()
    return [s / _INT16_FULL_SCALE for s in samples]


# --- Streaming recogniser ---------------------------------------------------


class SherpaOnlineRecognizer(Recognizer):
    """Frame-synchronous streaming recogniser over a :class:`DecoderBackend`.

    Emits an evolving ``partial`` whenever the hypothesis text changes, and a
    ``final`` when the backend reports an endpoint (or on :meth:`flush`). Keeps a
    session-monotonic ``segment`` counter and never re-emits a finalized segment.
    """

    def __init__(self, backend: DecoderBackend, *, sample_rate: int = 16000) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self._backend = backend
        self._sample_rate = sample_rate
        self._segment = 0
        self._elapsed = 0.0  # audio seconds consumed so far
        self._seg_start: float | None = None  # audio time the hypothesis first appeared
        # Audio time the current segment's audio began (set on its first chunk),
        # used to give an EOS-only hypothesis a sensible start < end.
        self._seg_audio_start: float | None = None
        self._last_text = ""
        self._pending = False  # unfinalized text exists in the current segment

    @classmethod
    def from_model(cls, config: SherpaModelConfig) -> SherpaOnlineRecognizer:
        """Build a recogniser backed by a real sherpa-onnx ``OnlineRecognizer``."""
        backend = create_sherpa_backend(config)
        return cls(backend, sample_rate=config.sample_rate)

    def accept_pcm(self, pcm: bytes) -> list[Event]:
        samples = _pcm16_to_float32(pcm)
        if not samples:
            return []
        chunk_start = self._elapsed
        if self._seg_audio_start is None:
            self._seg_audio_start = chunk_start
        self._backend.accept_waveform(self._sample_rate, samples)
        self._backend.decode()

        events: list[Event] = []
        text = self._backend.current_text().strip()
        if text:
            if self._seg_start is None:
                self._seg_start = chunk_start
            if text != self._last_text:  # dedupe identical consecutive hypotheses
                self._last_text = text
                self._pending = True
                events.append(
                    Event(
                        kind="partial",
                        segment=self._segment,
                        text=text,
                        start=self._seg_start,
                    )
                )

        self._elapsed += len(samples) / self._sample_rate

        if self._backend.is_endpoint():
            events.extend(self._finalize(end=self._elapsed))
            self._backend.reset()
        return events

    def current_segment(self) -> int:
        return self._segment

    def current_words(self) -> list[WordTiming]:
        if self._seg_start is None:
            return []
        return self._words(self._seg_start)

    def flush(self) -> list[Event]:
        self._backend.input_finished()
        self._backend.decode()
        text = self._backend.current_text().strip()
        if text and text != self._last_text:
            if self._seg_start is None:
                # Hypothesis first surfaced only at EOS: anchor its start to when
                # this segment's audio began (not ``_elapsed``, which is the EOS
                # instant) so the final spans real audio (end > start).
                self._seg_start = (
                    self._seg_audio_start if self._seg_audio_start is not None else 0.0
                )
            self._last_text = text
            self._pending = True
        return self._finalize(end=self._elapsed)

    def _finalize(self, *, end: float) -> list[Event]:
        """Promote the current segment's pending hypothesis to a ``final``."""
        if not self._pending:
            self._reset_segment()
            return []
        start = self._seg_start if self._seg_start is not None else 0.0
        event = Event(
            kind="final",
            segment=self._segment,
            text=self._last_text,
            # Guaranteed non-None for a pending segment; fall back to 0.0 rather
            # than ``end`` so a non-empty final never collapses to start == end.
            start=start,
            end=end,
            words=self._words(start) or None,
        )
        self._segment += 1
        self._reset_segment()
        return [event]

    def _words(self, start: float) -> list[WordTiming]:
        reader = getattr(self._backend, "current_words", None)
        if reader is None:
            return []
        try:
            return anchor_words(reader(), start)
        except Exception:  # noqa: BLE001 - word timings are optional detail
            return []

    def _reset_segment(self) -> None:
        self._seg_start = None
        self._seg_audio_start = None
        self._last_text = ""
        self._pending = False


# --- Real sherpa-onnx backend (lazily loaded) -------------------------------


@dataclass
class SherpaModelConfig:
    """Paths + runtime knobs for a sherpa-onnx streaming transducer model.

    Defaults target the pinned Nemotron streaming int8 model laid down in
    ``models/`` by ``scripts/download_model.sh``.
    """

    tokens: Path
    encoder: Path
    decoder: Path
    joiner: Path
    sample_rate: int = 16000
    feature_dim: int = 80
    num_threads: int = 1
    provider: str = "cpu"
    decoding_method: str = "greedy_search"
    enable_endpoint_detection: bool = True
    # Endpointing rules in seconds, derived from ``Config.endpointing_ms``.
    rule1_min_trailing_silence: float = 2.4
    rule2_min_trailing_silence: float = 1.2
    rule3_min_utterance_length: float = 20.0
    model_files: list[Path] = field(default_factory=list)


def build_model_config(
    *,
    model_dir: str | Path | None = None,
    sample_rate: int = 16000,
    endpointing_ms: int | None = None,
    tokens: str | Path | None = None,
    encoder: str | Path | None = None,
    decoder: str | Path | None = None,
    joiner: str | Path | None = None,
    num_threads: int | None = None,
) -> SherpaModelConfig:
    """Resolve a :class:`SherpaModelConfig` from env + optional overrides (NFR5).

    Single source of truth for how the ASR server *and* the build-time warmup
    turn ``ASR_MODEL_*`` env vars into a model config, so the snapshot is warmed
    with the exact same recogniser configuration the server instantiates at
    runtime (no drift by construction).

    Per-file resolution precedence (highest first): the explicit ``tokens`` /
    ``encoder`` / ``decoder`` / ``joiner`` override, then the matching
    ``ASR_MODEL_*`` env var, then ``<model_dir>/<default filename>``. ``model_dir``
    itself falls back to ``$ASR_MODEL_DIR`` then :data:`DEFAULT_MODEL_DIR`.
    ``num_threads`` falls back to ``$ASR_NUM_THREADS`` then ``1``. When
    ``endpointing_ms`` is given it maps onto ``rule2_min_trailing_silence``
    (seconds), matching the wire ``Config.endpointing_ms`` semantics.
    """
    root = Path(model_dir) if model_dir is not None else Path(
        os.environ.get("ASR_MODEL_DIR", DEFAULT_MODEL_DIR)
    )
    overrides = {"tokens": tokens, "encoder": encoder, "decoder": decoder, "joiner": joiner}

    resolved: dict[str, Path] = {}
    for key, env_key, filename in _MODEL_FILES:
        override = overrides[key]
        if override is not None:
            resolved[key] = Path(override)
        else:
            env_value = os.environ.get(env_key)
            resolved[key] = Path(env_value) if env_value else root / filename

    if num_threads is None:
        num_threads = int(os.environ.get("ASR_NUM_THREADS", "1"))

    config = SherpaModelConfig(
        tokens=resolved["tokens"],
        encoder=resolved["encoder"],
        decoder=resolved["decoder"],
        joiner=resolved["joiner"],
        sample_rate=sample_rate,
        num_threads=num_threads,
    )
    if endpointing_ms is not None:
        config.rule2_min_trailing_silence = endpointing_ms / 1000.0
    return config


def _load_sherpa() -> Any:
    """Import ``sherpa_onnx`` lazily with a clear error if the wheel is absent."""
    try:
        import sherpa_onnx  # type: ignore[import-untyped]
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "sherpa-onnx is not installed. Install the aarch64 wheel pinned in "
            "requirements.txt to construct a real streaming recogniser."
        ) from exc
    return sherpa_onnx


def _load_numpy() -> Any:
    """Import ``numpy`` lazily; only the real backend needs it."""
    try:
        import numpy
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "numpy is not installed. Install the aarch64 wheel pinned in "
            "requirements.txt to construct a real streaming recogniser."
        ) from exc
    return numpy


class _SherpaBackend:
    """Adapts a **shared** sherpa-onnx ``OnlineRecognizer`` + one per-session stream.

    The ``OnlineRecognizer`` (heavy: ONNX sessions) is shared across every session
    the engine hands out; only the ``stream`` is per session. sherpa-onnx supports
    many independent streams on one recognizer, but decode calls funnel through the
    shared recognizer object, so the engine passes a shared ``lock`` that serialises
    the recognizer-touching calls (``decode``/``current_text``/``is_endpoint``/
    ``reset``). This is the documented concurrency guarantee: multiple sessions may
    decode, but never truly in parallel on the same recognizer, so their streams
    can't corrupt one another (design Unchanged #4). A standalone backend built via
    :func:`create_sherpa_backend` (warmup / lifecycle ``/run``) owns its recognizer
    outright and passes ``lock=None`` (no contention, no locking overhead).
    """

    def __init__(self, recognizer: Any, stream: Any, *, lock: Any = None) -> None:
        self._rec = recognizer
        self._stream = stream
        self._lock = lock if lock is not None else contextlib.nullcontext()

    def accept_waveform(self, sample_rate: int, samples: Sequence[float]) -> None:
        # Operates on the per-session stream only (no shared recognizer state), so
        # it needs no lock — the waveform lands on this session's stream buffer.
        np = _load_numpy()
        self._stream.accept_waveform(sample_rate, np.asarray(samples, dtype=np.float32))

    def decode(self) -> None:
        with self._lock:
            while self._rec.is_ready(self._stream):
                self._rec.decode_stream(self._stream)

    def current_text(self) -> str:
        with self._lock:
            return str(self._rec.get_result(self._stream))

    def is_endpoint(self) -> bool:
        with self._lock:
            return bool(self._rec.is_endpoint(self._stream))

    def current_words(self) -> list[WordTiming]:
        # get_result() returns a plain string; the tokens and per-token timestamps
        # live on the object from get_result_all(), with tokens()/timestamps() as the
        # older accessors. Asking the string for .tokens yields nothing, which
        # silently disabled every feature that needs word timings.
        with self._lock:
            tokens, timestamps = self._token_timings()
        if not tokens or not timestamps:
            return []
        return words_from_tokens(list(tokens), [float(value) for value in timestamps])

    def _token_timings(self) -> tuple[list[str], list[float]]:
        detailed = getattr(self._rec, "get_result_all", None)
        if detailed is not None:
            result = detailed(self._stream)
            return (
                list(getattr(result, "tokens", []) or []),
                list(getattr(result, "timestamps", []) or []),
            )
        tokens_of = getattr(self._rec, "tokens", None)
        times_of = getattr(self._rec, "timestamps", None)
        if callable(tokens_of) and callable(times_of):
            return list(tokens_of(self._stream) or []), list(times_of(self._stream) or [])
        return [], []

    def reset(self) -> None:
        with self._lock:
            self._rec.reset(self._stream)

    def input_finished(self) -> None:
        # Stream-local finalize flag; no shared recognizer state touched.
        self._stream.input_finished()


class SherpaOnlineEngine(RecognizerEngine):
    """Shared streaming engine: one sherpa-onnx model, many per-session streams.

    Built ONCE (eagerly, at server startup) via :func:`create_sherpa_engine` so
    the model resident from the build-time warmup snapshot is reused rather than
    re-allocated on every WebSocket connection — the OOM/hang this seam fixes.
    Each :meth:`new_session` mints a lightweight per-connection
    :class:`SherpaOnlineRecognizer` over a fresh sherpa-onnx stream; all sessions
    share this engine's recognizer (guarded by a lock, see :class:`_SherpaBackend`).
    """

    def __init__(
        self,
        recognizer: Any,
        *,
        sample_rate: int = 16000,
        endpointing_ms: int | None = None,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self._rec = recognizer
        self._sample_rate = sample_rate
        # The endpoint rules (rule2 trailing silence) are baked into the shared
        # ``OnlineRecognizer`` at build time, so ``endpointing_ms`` cannot vary per
        # session against a shared engine. Retain the build-time value to validate
        # a client that negotiates a *different* one (see :meth:`new_session`).
        self._endpointing_ms = endpointing_ms
        # Serialises the shared recognizer's decode calls across concurrent
        # sessions' worker threads (each session's decode is offloaded via
        # ``asyncio.to_thread`` in ws_server, so two sessions can race here).
        self._lock: Any = threading.Lock()

    def new_session(self, config: SessionConfig | None = None) -> Recognizer:
        if config is not None:
            # ``sample_rate`` and the endpoint rules are baked into the shared ONNX
            # graph / recognizer at build time; they cannot be re-derived per session
            # without rebuilding the engine (the exact allocation this fix removes).
            # Reject an incompatible negotiation loudly instead of silently decoding
            # at the wrong config (design "Per-session config" §).
            if config.sample_rate != self._sample_rate:
                raise SessionConfigError(
                    f"streaming engine is built for sample_rate={self._sample_rate}; "
                    f"cannot decode a session negotiated at sample_rate="
                    f"{config.sample_rate} against the shared engine"
                )
            if (
                config.endpointing_ms is not None
                and self._endpointing_ms is not None
                and config.endpointing_ms != self._endpointing_ms
            ):
                raise SessionConfigError(
                    f"streaming engine's endpointing is baked at "
                    f"{self._endpointing_ms} ms; cannot honour a session negotiated "
                    f"at endpointing_ms={config.endpointing_ms} against the shared "
                    "engine (use the accurate engine for per-session endpointing)"
                )
        stream = self._rec.create_stream()
        backend = _SherpaBackend(self._rec, stream, lock=self._lock)
        return SherpaOnlineRecognizer(backend, sample_rate=self._sample_rate)


def _require_model_files(config: SherpaModelConfig) -> None:
    """Fail closed with a clear error if any required model file is missing.

    The wheel being importable does not imply the weights were baked into the
    image. Check up front so a bad ``ASR_MODEL_*`` path or an unbuilt model dir
    surfaces as an actionable ``RuntimeError`` here (build-time warmup / server
    startup) rather than an opaque error from deep inside sherpa-onnx.
    """
    required = {
        "tokens": config.tokens,
        "encoder": config.encoder,
        "decoder": config.decoder,
        "joiner": config.joiner,
    }
    missing = [f"{name} ({path})" for name, path in required.items() if not Path(path).is_file()]
    if missing:
        raise RuntimeError(
            "ASR model files are missing: "
            + ", ".join(missing)
            + ". Bake the int8 model into the image (scripts/download_model.sh) "
            "or set the ASR_MODEL_* paths to the model files."
        )


def _build_online_recognizer(config: SherpaModelConfig) -> Any:
    """Construct the heavy sherpa-onnx ``OnlineRecognizer`` (ONNX sessions) once.

    Fails closed with a ``RuntimeError`` if ``sherpa_onnx`` is unavailable *or*
    any required model file is missing (until the model + wheels are provisioned).
    Shared by :func:`create_sherpa_backend` (a standalone single-stream backend)
    and :func:`create_sherpa_engine` (the shared multi-session engine).
    """
    sherpa = _load_sherpa()
    _require_model_files(config)
    return sherpa.OnlineRecognizer.from_transducer(
        tokens=str(config.tokens),
        encoder=str(config.encoder),
        decoder=str(config.decoder),
        joiner=str(config.joiner),
        num_threads=config.num_threads,
        sample_rate=config.sample_rate,
        feature_dim=config.feature_dim,
        provider=config.provider,
        decoding_method=config.decoding_method,
        enable_endpoint_detection=config.enable_endpoint_detection,
        rule1_min_trailing_silence=config.rule1_min_trailing_silence,
        rule2_min_trailing_silence=config.rule2_min_trailing_silence,
        rule3_min_utterance_length=config.rule3_min_utterance_length,
    )


def create_sherpa_backend(config: SherpaModelConfig) -> DecoderBackend:
    """Construct a real sherpa-onnx streaming backend from on-disk model files.

    Builds a recognizer that owns its single stream (no sharing, no lock) — used
    by the build-time warmup and the lifecycle ``/run`` warm-stream path, where
    exactly one stream is driven. Fails closed with a ``RuntimeError`` if
    ``sherpa_onnx`` is unavailable *or* any required model file is missing.
    """
    recognizer = _build_online_recognizer(config)
    stream = recognizer.create_stream()
    return _SherpaBackend(recognizer, stream)


def create_sherpa_engine(config: SherpaModelConfig) -> SherpaOnlineEngine:
    """Construct the shared streaming engine — the heavy model, built ONCE.

    Called eagerly at server startup so the model resident from the build-time
    warmup snapshot is reused across all sessions, never re-allocated per
    connection. Each connection then acquires a lightweight per-session stream via
    :meth:`SherpaOnlineEngine.new_session`. Fails closed with a ``RuntimeError``
    (missing wheel / model files) so startup can fail loudly rather than a session
    dying silently mid-connection.
    """
    recognizer = _build_online_recognizer(config)
    # Record the endpointing (ms) actually baked into the recognizer's rule2
    # trailing-silence so ``new_session`` can reject a session that negotiates a
    # different value against this shared, fixed-rule engine.
    baked_endpointing_ms = round(config.rule2_min_trailing_silence * 1000)
    return SherpaOnlineEngine(
        recognizer,
        sample_rate=config.sample_rate,
        endpointing_ms=baked_endpointing_ms,
    )
