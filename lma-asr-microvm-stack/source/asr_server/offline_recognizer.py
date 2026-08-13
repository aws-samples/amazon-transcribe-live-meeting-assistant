"""Offline VAD-segmented recognizer for the ``accurate`` engine.

The ``accurate`` engine is the higher-accuracy **fallback** from
``docs/asr-model-selection-and-recipe.md`` §1/§2 and ``design.md`` §4: an
**offline** transducer (Parakeet TDT 0.6B v2 int8) driven **VAD-segmented** so it
still fits the streaming WebSocket contract. Unlike the frame-synchronous
streaming primary (:class:`asr_server.recognizer.SherpaOnlineRecognizer`), an
offline transducer buffers a whole utterance and decodes at silence, so the only
way to "stream" it is to segment the audio with Silero VAD and decode each closed
utterance offline. It therefore emits **one ``final`` per utterance** (R4.4) and
— because R4.4 permits it — a single synthetic ``partial`` at segment close so
clients with ``interim_results`` on still see the committed text as an interim.

Same :class:`~asr_server.recognizer.Recognizer` interface as the streaming path
(``accept_pcm(bytes) -> list[Event]``, ``flush() -> list[Event]``), so
:mod:`asr_server.ws_server` / :mod:`asr_server.warmup` drive both engines through
one seam and the protocol adapter maps the events onto the wire schema.

This module composes two engine-internal collaborators — a
:class:`asr_server.vad.VadGate` (segmentation) and an
:class:`OfflineDecoderBackend` (per-utterance decode) — and holds a rolling PCM
buffer so the samples for each VAD segment can be handed to the offline decoder.
It never imports the ``asr_protocol`` wire schema (the adapter owns that seam).

Testability without model weights
---------------------------------
Mirrors :mod:`asr_server.recognizer` / :mod:`asr_server.vad`: the recogniser
depends only on the small :class:`OfflineDecoderBackend` protocol (plus the
already-injectable :class:`~asr_server.vad.VadGate`) and takes both by injection.
Tests supply a scripted fake VAD backend and a scripted offline backend — no
weights, no ``sherpa_onnx`` / ``onnxruntime`` / ``numpy`` wheels needed. Production
uses :func:`create_sherpa_offline_backend`, which lazily imports ``sherpa_onnx``
only when a real engine is actually constructed (fail-closed until provisioned).
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from array import array
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from asr_server.recognizer import (
    DEFAULT_MODEL_DIR,
    Event,
    Recognizer,
    RecognizerEngine,
    SessionConfig,
    SessionConfigError,
    WordTiming,
    words_from_tokens,
)
from asr_server.vad import (
    SileroVadConfig,
    VadGate,
    create_silero_session,
    make_silero_backend,
)

__all__ = [
    "OfflineResult",
    "OfflineDecoderBackend",
    "OfflineModelConfig",
    "SherpaOfflineRecognizer",
    "SherpaOfflineEngine",
    "build_offline_model_config",
    "create_sherpa_offline_backend",
    "create_sherpa_offline_engine",
]

# Standard filenames a sherpa-onnx offline transducer export ships (Parakeet TDT),
# resolved relative to the model dir unless an explicit path / ``ASR_MODEL_*`` env
# overrides them. Deliberately the SAME env var names the streaming path uses
# (:data:`asr_server.recognizer._MODEL_FILES`): only ONE engine's model is baked
# per MicroVM image (recipe doc §4 sizing pins the tier by footprint), so the
# server and warmup resolve the single baked model through one set of env vars.
_MODEL_FILES: tuple[tuple[str, str, str], ...] = (
    ("tokens", "ASR_MODEL_TOKENS", "tokens.txt"),
    ("encoder", "ASR_MODEL_ENCODER", "encoder.onnx"),
    ("decoder", "ASR_MODEL_DECODER", "decoder.onnx"),
    ("joiner", "ASR_MODEL_JOINER", "joiner.onnx"),
)

# Silero VAD model default filename + its env override, resolved under the model
# dir (the offline engine needs BOTH the transducer AND the VAD model to segment).
_VAD_MODEL_ENV = "ASR_VAD_MODEL"
_VAD_MODEL_FILENAME = "silero_vad.onnx"

# 16-bit signed PCM full-scale magnitude (normalise samples to [-1, 1)). Kept in
# lock-step with :mod:`asr_server.recognizer` / :mod:`asr_server.vad` (same wire
# contract); duplicated rather than importing a private symbol across modules.
_INT16_FULL_SCALE = 32768.0


# --- Offline decode backend -------------------------------------------------


@dataclass(frozen=True)
class OfflineResult:
    """One offline decode of a single utterance (engine-internal, not wire).

    ``text`` is the best hypothesis for the segment. ``words`` carries optional
    per-word timings whose ``s``/``e`` are **relative to the segment start**
    (0-based); :class:`SherpaOfflineRecognizer` offsets them onto the absolute
    session timeline. Empty ``words`` means the backend produced no word timings.
    """

    text: str
    words: list[WordTiming] = field(default_factory=list)


class OfflineDecoderBackend:
    """Minimal offline decode surface the recogniser drives — one per session.

    A structural protocol (kept as a plain base with a ``NotImplementedError``
    default so a scripted fake can stand in without weights). Wraps a single
    sherpa-onnx ``OfflineRecognizer``; :meth:`decode` is called once per VAD
    segment with that segment's normalised float samples and returns the decode.
    """

    def decode(self, sample_rate: int, samples: Sequence[float]) -> OfflineResult:
        """Decode one utterance's float samples in [-1, 1) to an :class:`OfflineResult`."""
        raise NotImplementedError


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


# --- Offline recogniser -----------------------------------------------------


class SherpaOfflineRecognizer(Recognizer):
    """VAD-segmented offline recognizer for the ``accurate`` engine.

    Feeds each incoming PCM chunk through a :class:`~asr_server.vad.VadGate` to
    find utterance boundaries while retaining the corresponding samples in a
    rolling buffer. When the gate closes a segment (``speech_end``), the segment's
    samples are handed to the offline decoder and the hypothesis is emitted as a
    ``final`` (optionally preceded by one synthetic ``partial`` — R4.4). Keeps a
    session-monotonic ``segment`` counter and never re-emits a finalized segment.

    Silence yields no VAD segment, so no events (matching the streaming path's
    "silence produces no partials"). :meth:`flush` closes any segment still open
    at end-of-audio and decodes it, so a trailing utterance is never dropped.
    """

    def __init__(
        self,
        vad_gate: VadGate,
        backend: OfflineDecoderBackend,
        *,
        sample_rate: int = 16000,
        emit_segment_partial: bool = True,
        max_idle_ms: int = 2000,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if max_idle_ms < 0:
            raise ValueError("max_idle_ms must be >= 0")
        self._vad = vad_gate
        self._backend = backend
        self._sample_rate = sample_rate
        self._emit_segment_partial = emit_segment_partial
        # While no segment is open, retain at most this much trailing audio so a
        # long pre-speech / inter-utterance silence can't grow the buffer without
        # bound (R7 spirit). Must comfortably exceed the gate's ``min_speech_ms +
        # speech_pad_ms`` so a future segment's retroactive padded onset still has
        # its samples buffered — ``from_model`` sizes it from the VAD config.
        self._max_idle_samples = sample_rate * max_idle_ms // 1000

        self._segment = 0
        # Rolling buffer of float samples not yet consumed by a decoded segment,
        # with the absolute sample index of ``_buffer[0]`` so VAD timestamps
        # (seconds on the same fed-sample timeline) can be sliced back to samples.
        self._buffer: list[float] = []
        self._buffer_base = 0  # absolute index of _buffer[0]
        self._total_samples = 0  # absolute samples fed so far (drives the timeline)
        self._seg_start_t: float | None = None  # open segment's start time, if any

    @classmethod
    def from_model(cls, config: OfflineModelConfig) -> SherpaOfflineRecognizer:
        """Build a recogniser backed by a real sherpa-onnx ``OfflineRecognizer`` + Silero VAD."""
        vad_gate = VadGate.from_model(config.vad)
        backend = create_sherpa_offline_backend(config)
        # Retain enough idle history to cover the gate's retroactive padded onset
        # (min_speech + pad), with a safety margin, so idle pruning never truncates
        # a real segment.
        max_idle_ms = (config.vad.min_speech_ms + config.vad.speech_pad_ms) * 2 + 1000
        return cls(
            vad_gate,
            backend,
            sample_rate=config.sample_rate,
            emit_segment_partial=config.emit_segment_partial,
            max_idle_ms=max_idle_ms,
        )

    def accept_pcm(self, pcm: bytes) -> list[Event]:
        samples = _pcm16_to_float32(pcm)
        if not samples:
            return []
        self._buffer.extend(samples)
        self._total_samples += len(samples)
        events = self._handle_vad_events(self._vad.accept_pcm(pcm))
        # No segment open (silence / between utterances): cap retained history so a
        # long pause can't grow the buffer without bound. Keeps the most recent
        # ``_max_idle_samples`` so a not-yet-confirmed onset's samples survive.
        if self._seg_start_t is None and len(self._buffer) > self._max_idle_samples:
            self._prune_to_index(len(self._buffer) - self._max_idle_samples)
        return events

    def flush(self) -> list[Event]:
        # ``VadGate.flush`` closes any open segment (emitting a final ``speech_end``)
        # and resets the gate; decode that trailing utterance so it is not dropped.
        return self._handle_vad_events(self._vad.flush())

    def _handle_vad_events(self, vad_events: Sequence[Any]) -> list[Event]:
        events: list[Event] = []
        for ve in vad_events:
            if ve.kind == "speech_start":
                # New segment opens: its samples begin here, so anything earlier is
                # never needed again — prune the buffer up to the (padded) onset.
                self._seg_start_t = ve.t
                self._prune_before(ve.t)
            elif self._seg_start_t is not None:  # speech_end closes the open segment
                events.extend(self._decode_segment(self._seg_start_t, ve.t))
                self._seg_start_t = None
        return events

    def _decode_segment(self, start_t: float, end_t: float) -> list[Event]:
        """Decode one closed VAD segment offline and build its ``final`` (+partial)."""
        start_idx = max(0, round(start_t * self._sample_rate))
        end_idx = round(end_t * self._sample_rate)
        buf_start = max(0, start_idx - self._buffer_base)
        buf_end = min(len(self._buffer), max(buf_start, end_idx - self._buffer_base))
        segment_samples = self._buffer[buf_start:buf_end]

        # Consume everything up to the segment end; segments are monotonic and
        # non-overlapping, so those samples can never be needed again (bounds memory).
        self._prune_to_index(buf_end)

        if not segment_samples:
            return []
        result = self._backend.decode(self._sample_rate, segment_samples)
        text = result.text.strip()
        if not text:
            # No hypothesis for this segment (e.g. non-speech that tripped the VAD):
            # emit nothing and do not consume a segment number, mirroring the
            # streaming path's empty-final suppression.
            return []

        events: list[Event] = []
        if self._emit_segment_partial:
            # R4.4: an offline engine MAY emit a single ``partial`` at segment close.
            events.append(
                Event(kind="partial", segment=self._segment, text=text, start=start_t)
            )
        # Word timings from the decode are segment-relative; offset onto the
        # absolute session timeline so ``final`` word times line up with start/end.
        words = (
            [WordTiming(w=wt.w, s=wt.s + start_t, e=wt.e + start_t) for wt in result.words]
            if result.words
            else None
        )
        events.append(
            Event(
                kind="final",
                segment=self._segment,
                text=text,
                start=start_t,
                end=end_t,
                words=words,
            )
        )
        self._segment += 1
        return events

    def _prune_before(self, t: float) -> None:
        """Drop buffered samples before absolute time ``t`` (seconds)."""
        idx = max(0, round(t * self._sample_rate))
        self._prune_to_index(idx - self._buffer_base)

    def _prune_to_index(self, buf_idx: int) -> None:
        """Drop ``_buffer[:buf_idx]`` and advance the buffer base accordingly."""
        if buf_idx <= 0:
            return
        buf_idx = min(buf_idx, len(self._buffer))
        del self._buffer[:buf_idx]
        self._buffer_base += buf_idx


# --- Config -----------------------------------------------------------------


@dataclass
class OfflineModelConfig:
    """Paths + runtime knobs for the VAD-segmented offline transducer engine.

    Bundles the offline transducer model files (Parakeet TDT 0.6B v2 int8) with a
    nested :class:`~asr_server.vad.SileroVadConfig` for the segmentation gate, so
    the whole ``accurate`` engine is configured from one object. Defaults target
    the fallback model laid down under ``models/`` by ``scripts/download_model.sh
    --engine fallback``.
    """

    tokens: Path
    encoder: Path
    decoder: Path
    joiner: Path
    vad: SileroVadConfig
    sample_rate: int = 16000
    feature_dim: int = 80
    num_threads: int = 1
    provider: str = "cpu"
    decoding_method: str = "greedy_search"
    # NeMo TDT transducer model type for sherpa-onnx's OfflineRecognizer. # verify
    model_type: str = "nemo_transducer"
    # Emit one synthetic ``partial`` at each segment close (R4.4 permits it).
    emit_segment_partial: bool = True


def build_offline_model_config(
    *,
    model_dir: str | Path | None = None,
    sample_rate: int = 16000,
    endpointing_ms: int | None = None,
    tokens: str | Path | None = None,
    encoder: str | Path | None = None,
    decoder: str | Path | None = None,
    joiner: str | Path | None = None,
    vad_model: str | Path | None = None,
    num_threads: int | None = None,
) -> OfflineModelConfig:
    """Resolve an :class:`OfflineModelConfig` from env + optional overrides (NFR5).

    Mirrors :func:`asr_server.recognizer.build_model_config` so the ``accurate``
    engine resolves its model the same way the streaming engine does — the ASR
    server and the build-time warmup share this one builder, so the snapshot is
    warmed with the exact recogniser configuration the server instantiates
    (no drift by construction).

    Per-file precedence (highest first): the explicit ``tokens`` / ``encoder`` /
    ``decoder`` / ``joiner`` / ``vad_model`` override, then the matching
    ``ASR_MODEL_*`` / ``ASR_VAD_MODEL`` env var, then ``<model_dir>/<default>``.
    ``model_dir`` falls back to ``$ASR_MODEL_DIR`` then :data:`DEFAULT_MODEL_DIR`;
    ``num_threads`` to ``$ASR_NUM_THREADS`` then ``1``. When ``endpointing_ms`` is
    given it drives the VAD's trailing-silence (``min_silence_ms``) so "they
    stopped talking" closes a segment at the wire ``Config.endpointing_ms`` (R5.2).
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

    if vad_model is not None:
        vad_path = Path(vad_model)
    else:
        vad_env = os.environ.get(_VAD_MODEL_ENV)
        vad_path = Path(vad_env) if vad_env else root / _VAD_MODEL_FILENAME

    if num_threads is None:
        num_threads = int(os.environ.get("ASR_NUM_THREADS", "1"))

    vad_config = SileroVadConfig(
        model=vad_path,
        sample_rate=sample_rate,
        num_threads=num_threads,
        provider="cpu",
    )
    if endpointing_ms is not None:
        vad_config.min_silence_ms = endpointing_ms

    return OfflineModelConfig(
        tokens=resolved["tokens"],
        encoder=resolved["encoder"],
        decoder=resolved["decoder"],
        joiner=resolved["joiner"],
        vad=vad_config,
        sample_rate=sample_rate,
        num_threads=num_threads,
    )


# --- Real sherpa-onnx offline backend (lazily loaded) -----------------------


def _load_sherpa() -> Any:
    """Import ``sherpa_onnx`` lazily with a clear error if the wheel is absent."""
    try:
        import sherpa_onnx  # type: ignore[import-untyped]
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "sherpa-onnx is not installed. Install the aarch64 wheel pinned in "
            "requirements.txt to construct a real offline recogniser."
        ) from exc
    return sherpa_onnx


def _load_numpy() -> Any:
    """Import ``numpy`` lazily; only the real backend needs it."""
    try:
        import numpy
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "numpy is not installed. Install the aarch64 wheel pinned in "
            "requirements.txt to construct a real offline recogniser."
        ) from exc
    return numpy


_words_from_tokens = words_from_tokens


class _SherpaOfflineBackend(OfflineDecoderBackend):
    """Adapts a **shared** sherpa-onnx ``OfflineRecognizer`` to the decode surface.

    The ``OfflineRecognizer`` (heavy: ONNX sessions) is shared across every session
    the engine hands out; decodes funnel through it on a fresh per-utterance stream.
    A shared ``lock`` serialises ``decode_stream`` across concurrent sessions'
    worker threads so the shared recognizer is never driven in parallel (the
    documented concurrency guarantee, design Unchanged #4). A standalone backend
    (warmup / ``/run``) owns its recognizer and passes ``lock=None``.
    """

    def __init__(self, recognizer: Any, *, lock: Any = None) -> None:
        self._rec = recognizer
        self._lock = lock if lock is not None else contextlib.nullcontext()

    def decode(self, sample_rate: int, samples: Sequence[float]) -> OfflineResult:
        np = _load_numpy()
        # Each utterance decodes on a fresh stream (offline transducers are
        # stateless across utterances). # verify sherpa OfflineRecognizer API.
        stream = self._rec.create_stream()
        stream.accept_waveform(sample_rate, np.asarray(samples, dtype=np.float32))
        with self._lock:
            self._rec.decode_stream(stream)
        result = stream.result
        text = str(getattr(result, "text", ""))
        tokens = list(getattr(result, "tokens", []) or [])
        timestamps = list(getattr(result, "timestamps", []) or [])
        return OfflineResult(text=text, words=_words_from_tokens(tokens, timestamps))


def _require_offline_model_files(config: OfflineModelConfig) -> None:
    """Fail closed with a clear error if any required model file is missing.

    Checks the offline transducer files AND the Silero VAD model up front (the
    ``accurate`` engine needs both), so a bad ``ASR_MODEL_*`` / ``ASR_VAD_MODEL``
    path or an unbuilt model dir surfaces as an actionable ``RuntimeError`` at
    build-time warmup / server startup rather than opaquely inside sherpa-onnx.
    """
    required = {
        "tokens": config.tokens,
        "encoder": config.encoder,
        "decoder": config.decoder,
        "joiner": config.joiner,
        "vad": config.vad.model,
    }
    missing = [f"{name} ({path})" for name, path in required.items() if not Path(path).is_file()]
    if missing:
        raise RuntimeError(
            "ASR model files are missing: "
            + ", ".join(missing)
            + ". Bake the offline (accurate) model + Silero VAD into the image "
            "(scripts/download_model.sh --engine fallback) or set the "
            "ASR_MODEL_* / ASR_VAD_MODEL paths to the model files."
        )


def _build_offline_recognizer(config: OfflineModelConfig) -> Any:
    """Construct the heavy sherpa-onnx ``OfflineRecognizer`` (ONNX sessions) once.

    Fails closed with a ``RuntimeError`` if ``sherpa_onnx`` is unavailable *or* any
    required model file is missing. Shared by :func:`create_sherpa_offline_backend`
    (standalone) and :func:`create_sherpa_offline_engine` (the shared engine).
    """
    sherpa = _load_sherpa()
    _require_offline_model_files(config)
    return sherpa.OfflineRecognizer.from_transducer(
        tokens=str(config.tokens),
        encoder=str(config.encoder),
        decoder=str(config.decoder),
        joiner=str(config.joiner),
        num_threads=config.num_threads,
        sample_rate=config.sample_rate,
        feature_dim=config.feature_dim,
        decoding_method=config.decoding_method,
        model_type=config.model_type,
        provider=config.provider,
    )


def create_sherpa_offline_backend(config: OfflineModelConfig) -> OfflineDecoderBackend:
    """Construct a real sherpa-onnx offline transducer backend from on-disk files.

    Standalone single-session path (warmup / lifecycle ``/run``): owns its
    recognizer, no sharing, no lock. Fails closed with a ``RuntimeError`` if
    ``sherpa_onnx`` is unavailable *or* any required model file is missing.
    """
    return _SherpaOfflineBackend(_build_offline_recognizer(config))


class SherpaOfflineEngine(RecognizerEngine):
    """Shared ``accurate`` engine: one offline transducer + Silero VAD, reused.

    Built ONCE (eagerly, at server startup) via :func:`create_sherpa_offline_engine`
    so the resident warmed model is reused, never re-allocated per connection.
    Each :meth:`new_session` mints a fresh :class:`SherpaOfflineRecognizer` with its
    own :class:`~asr_server.vad.VadGate` (independent recurrent state over the shared
    Silero session) and its own PCM buffer/segment counters, so transcripts stay
    independent; decodes funnel through the shared recognizer under a lock. The
    negotiated per-session ``endpointing_ms`` is applied to that per-session gate
    (``min_silence_ms``) — no shared-engine rebuild; ``sample_rate`` is baked into
    the shared graph and validated (rejected on mismatch, design "Per-session config").
    """

    def __init__(
        self,
        offline_recognizer: Any,
        vad_session: Any,
        config: OfflineModelConfig,
    ) -> None:
        self._offline_recognizer = offline_recognizer
        self._vad_session = vad_session
        self._config = config
        # Serialises the shared offline recognizer's ``decode_stream`` across
        # concurrent sessions' worker threads (see :class:`_SherpaOfflineBackend`).
        self._lock: Any = threading.Lock()
        # Match ``SherpaOfflineRecognizer.from_model``'s idle-history sizing so a
        # per-session recogniser buffers enough to cover the padded VAD onset.
        self._max_idle_ms = (
            config.vad.min_speech_ms + config.vad.speech_pad_ms
        ) * 2 + 1000

    def new_session(self, config: SessionConfig | None = None) -> Recognizer:
        # ``sample_rate`` is baked into the shared offline ONNX graph AND the shared
        # Silero VAD session; it cannot vary per session against a shared engine, so
        # reject an incompatible negotiation rather than decoding at the wrong rate
        # (design "Per-session config" §). ``endpointing_ms`` DOES vary per session:
        # it lives only in the per-session ``VadGate`` (min_silence_ms), so apply the
        # negotiated value here — no shared-engine rebuild, no ONNX reallocation.
        min_silence_ms = self._config.vad.min_silence_ms
        if config is not None:
            if config.sample_rate != self._config.sample_rate:
                raise SessionConfigError(
                    f"accurate engine is built for sample_rate="
                    f"{self._config.sample_rate}; cannot decode a session negotiated "
                    f"at sample_rate={config.sample_rate} against the shared engine"
                )
            if config.endpointing_ms is not None:
                min_silence_ms = config.endpointing_ms

        vad_backend = make_silero_backend(
            self._vad_session, sample_rate=self._config.vad.sample_rate
        )
        vad_gate = VadGate(
            vad_backend,
            sample_rate=self._config.vad.sample_rate,
            frame_ms=self._config.vad.frame_ms,
            threshold=self._config.vad.threshold,
            neg_threshold=self._config.vad.neg_threshold,
            min_silence_ms=min_silence_ms,
            min_speech_ms=self._config.vad.min_speech_ms,
            speech_pad_ms=self._config.vad.speech_pad_ms,
        )
        backend = _SherpaOfflineBackend(self._offline_recognizer, lock=self._lock)
        return SherpaOfflineRecognizer(
            vad_gate,
            backend,
            sample_rate=self._config.sample_rate,
            emit_segment_partial=self._config.emit_segment_partial,
            max_idle_ms=self._max_idle_ms,
        )


def create_sherpa_offline_engine(config: OfflineModelConfig) -> SherpaOfflineEngine:
    """Construct the shared ``accurate`` engine — the heavy model, built ONCE.

    Called eagerly at server startup so the resident warmed transducer + VAD are
    reused across all sessions, never re-allocated per connection. Fails closed
    with a ``RuntimeError`` (missing wheel / model files) so startup fails loudly
    rather than a session dying silently mid-connection.
    """
    offline_recognizer = _build_offline_recognizer(config)
    vad_session = create_silero_session(config.vad)
    return SherpaOfflineEngine(offline_recognizer, vad_session, config)
