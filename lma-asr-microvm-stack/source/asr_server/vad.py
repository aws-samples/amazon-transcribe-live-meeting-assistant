# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Silero VAD gate backed by ONNX Runtime.

Gates the audio stream so **silence produces no partials** and **speech onsets
are detected within one chunk**.
The gate consumes 16 kHz/16-bit/mono/LE PCM, runs a Silero VAD over fixed-size
frames, and emits engine-internal :class:`VadEvent` transitions
(``speech_start`` / ``speech_end``) with audio timestamps. Endpointing and the
protocol adapter consume these; the offline engine pairs them into
:class:`SpeechSegment` spans via :func:`to_segments`.

These are **engine-internal** types, deliberately *not* the ``asr_protocol``
wire models: this module never imports the wire schema so the two can evolve
independently, mirroring :mod:`asr_server.recognizer`.

Testability without model weights
---------------------------------
The runtime fails closed when model weights are unavailable, so the
``onnxruntime`` / ``numpy`` wheels need not be importable here. The gate depends
only on the small :class:`VadBackend` protocol and takes a backend by injection.
Tests supply a scripted fake backend; production uses
:func:`create_silero_backend`, which lazily imports ``onnxruntime`` only when a
real session is actually constructed.
"""

from __future__ import annotations

import sys
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

__all__ = [
    "VadEvent",
    "SpeechSegment",
    "VadBackend",
    "SileroVadConfig",
    "VadGate",
    "create_silero_backend",
    "create_silero_session",
    "make_silero_backend",
    "to_segments",
]

# 16-bit signed PCM: full-scale magnitude used to normalise samples to [-1, 1).
# Kept in lock-step with :mod:`asr_server.recognizer` (same wire contract); the
# helper is duplicated rather than importing a private symbol across modules.
_INT16_FULL_SCALE = 32768.0

# Silero's end-of-speech hysteresis: once triggered, speech is considered to have
# stopped only when the probability drops this far below ``threshold`` (matches
# the upstream ``VADIterator`` default), avoiding flapping around the threshold.
_NEG_THRESHOLD_MARGIN = 0.15


# --- Engine-internal result types -------------------------------------------


@dataclass(frozen=True)
class VadEvent:
    """A speech/silence transition emitted by :class:`VadGate` (engine-internal).

    ``kind`` is ``"speech_start"`` when speech onset is detected and
    ``"speech_end"`` when trailing silence closes a segment. ``t`` is the audio
    timestamp in seconds (padded by ``speech_pad_ms`` and clamped to ``>= 0``).
    """

    kind: Literal["speech_start", "speech_end"]
    t: float


@dataclass(frozen=True)
class SpeechSegment:
    """A closed span of speech in seconds (engine-internal, not the wire model).

    Produced by :func:`to_segments` for the VAD-segmented offline path (R4.4).
    """

    start: float
    end: float


# --- Backend interface ------------------------------------------------------


class VadBackend(Protocol):
    """Minimal Silero VAD surface the gate drives — one backend per session.

    Wraps a single stateful ONNX session. Kept intentionally tiny so a scripted
    fake can stand in for the real model in tests (no weights needed).
    """

    def probability(self, frame: Sequence[float]) -> float:
        """Return the speech probability in ``[0, 1]`` for one float32 frame."""

    def reset(self) -> None:
        """Clear the model's recurrent state to begin a fresh stream."""


# --- PCM helpers ------------------------------------------------------------


def _pcm16_to_float32(pcm: bytes) -> list[float]:
    """Decode little-endian 16-bit signed PCM bytes to floats in [-1, 1).

    Pure stdlib (no numpy) so the gate stays importable and testable without the
    ARM inference wheels installed.
    """
    if len(pcm) % 2 != 0:
        raise ValueError("PCM byte length must be even for 16-bit samples")
    samples = array("h")
    samples.frombytes(pcm)
    # ``array('h')`` uses native byte order; the wire format is little-endian.
    if sys.byteorder != "little":
        samples.byteswap()
    return [s / _INT16_FULL_SCALE for s in samples]


# --- VAD gate ---------------------------------------------------------------


class VadGate:
    """Frame-synchronous speech/silence gate over a :class:`VadBackend`.

    Config-driven (NFR5): the constructor takes the frame size, threshold, and
    the trailing-silence / minimum-speech / padding rules — no hardcoded paths.
    Incoming PCM is buffered and sliced into ``frame_samples``-sized frames; each
    frame's probability drives a small hysteresis state machine:

    * ``speech_start`` fires once at least ``min_speech_ms`` of **contiguous**
      speech (frames at/above ``threshold``) has accumulated. A run shorter than
      ``min_speech_ms`` is a spurious blip and is **rejected outright** — no
      ``speech_start`` (and hence no ``speech_end``) is emitted for it. The event
      timestamp is the *true onset* (padded back by ``speech_pad_ms`` and clamped
      to ``>= 0``): it is emitted **retroactively** so downstream spans the real
      speech, even though the event only surfaces once the run is confirmed. An
      onset is thus detected within one chunk of sustained speech.
    * ``speech_end`` fires once the probability has stayed below the negative
      threshold (``threshold - 0.15``) for at least ``min_silence_ms`` —
      hysteresis so momentary dips don't chop a confirmed segment.

    Silence (probabilities below threshold with no active segment) produces no
    events at all, so downstream never emits partials for it.
    """

    def __init__(
        self,
        backend: VadBackend,
        *,
        sample_rate: int = 16000,
        frame_ms: int = 32,
        threshold: float = 0.5,
        neg_threshold: float | None = None,
        min_silence_ms: int = 100,
        min_speech_ms: int = 250,
        speech_pad_ms: int = 30,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if frame_ms <= 0:
            raise ValueError("frame_ms must be positive")
        frame_samples = sample_rate * frame_ms // 1000
        if frame_samples <= 0:
            raise ValueError("frame_ms is too small for the sample rate (0 samples/frame)")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be in [0, 1]")
        if min_silence_ms < 0 or min_speech_ms < 0 or speech_pad_ms < 0:
            raise ValueError("min_silence_ms, min_speech_ms and speech_pad_ms must be >= 0")

        resolved_neg = threshold - _NEG_THRESHOLD_MARGIN if neg_threshold is None else neg_threshold
        if not 0.0 <= resolved_neg <= threshold:
            raise ValueError("neg_threshold must be in [0, threshold]")

        self._backend = backend
        self._sample_rate = sample_rate
        self._frame_samples = frame_samples
        self._threshold = threshold
        self._neg_threshold = resolved_neg
        self._min_silence_s = min_silence_ms / 1000.0
        self._min_speech_s = min_speech_ms / 1000.0
        self._speech_pad_s = speech_pad_ms / 1000.0

        self._buffer: list[float] = []
        self._processed_samples = 0  # samples pulled into frames (drives timestamps)
        self._triggered = False  # a segment has been confirmed and is open
        self._seg_start_t = 0.0  # audio time the active segment began (unpadded)
        # Onset of the in-progress *candidate* speech run, before it has met
        # ``min_speech_ms``. Distinct from ``_seg_start_t``: a candidate that dies
        # short of the minimum is discarded and never becomes a segment.
        self._candidate_start_t: float | None = None
        self._silence_start_t: float | None = None  # onset of the pending trailing silence

    @classmethod
    def from_model(cls, config: SileroVadConfig) -> VadGate:
        """Build a gate backed by a real Silero VAD ONNX session."""
        backend = create_silero_backend(config)
        return cls(
            backend,
            sample_rate=config.sample_rate,
            frame_ms=config.frame_ms,
            threshold=config.threshold,
            neg_threshold=config.neg_threshold,
            min_silence_ms=config.min_silence_ms,
            min_speech_ms=config.min_speech_ms,
            speech_pad_ms=config.speech_pad_ms,
        )

    @property
    def is_speech(self) -> bool:
        """Whether a speech segment is currently open."""
        return self._triggered

    def accept_pcm(self, pcm: bytes) -> list[VadEvent]:
        """Feed one chunk of 16 kHz/16-bit/mono/LE PCM, return any new events.

        Chunks need not align to the frame size: leftover samples are buffered
        and combined with the next chunk.
        """
        samples = _pcm16_to_float32(pcm)
        if not samples:
            return []
        self._buffer.extend(samples)
        events: list[VadEvent] = []
        n = self._frame_samples
        while len(self._buffer) >= n:
            frame = self._buffer[:n]
            del self._buffer[:n]
            events.extend(self._process_frame(frame))
        return events

    def flush(self) -> list[VadEvent]:
        """Signal end-of-audio: close any open segment and reset for reuse.

        The sub-frame tail still buffered (< one frame) is discarded — it is
        shorter than the VAD window and cannot be scored.
        """
        events: list[VadEvent] = []
        if self._triggered:
            # No trailing pad past real audio: end at the last processed sample.
            events.append(
                VadEvent(kind="speech_end", t=self._processed_samples / self._sample_rate)
            )
        self.reset()
        return events

    def reset(self) -> None:
        """Clear gate state and the backend's recurrent state for a new stream."""
        self._buffer.clear()
        self._processed_samples = 0
        self._triggered = False
        self._seg_start_t = 0.0
        self._candidate_start_t = None
        self._silence_start_t = None
        self._backend.reset()

    def _process_frame(self, frame: list[float]) -> list[VadEvent]:
        prob = self._backend.probability(frame)
        frame_start_t = self._processed_samples / self._sample_rate
        self._processed_samples += len(frame)
        frame_end_t = self._processed_samples / self._sample_rate

        if prob >= self._threshold:
            return self._on_speech_frame(frame_start_t, frame_end_t)
        return self._on_nonspeech_frame(prob, frame_start_t, frame_end_t)

    def _on_speech_frame(self, frame_start_t: float, frame_end_t: float) -> list[VadEvent]:
        self._silence_start_t = None  # speech (re)confirmed: cancel pending end
        if self._triggered:
            return []
        # Accumulating a candidate run: record its true onset on the first frame.
        if self._candidate_start_t is None:
            self._candidate_start_t = frame_start_t
        # Confirm the segment only once contiguous speech meets ``min_speech_ms``.
        # ``speech_start`` is then emitted retroactively at the true (padded) onset
        # so downstream spans the real speech, not the confirmation instant.
        if frame_end_t - self._candidate_start_t >= self._min_speech_s:
            self._triggered = True
            self._seg_start_t = self._candidate_start_t
            start = max(0.0, self._candidate_start_t - self._speech_pad_s)
            self._candidate_start_t = None
            return [VadEvent(kind="speech_start", t=start)]
        return []

    def _on_nonspeech_frame(
        self, prob: float, frame_start_t: float, frame_end_t: float
    ) -> list[VadEvent]:
        if prob < self._neg_threshold:
            # An unconfirmed candidate that drops out short of ``min_speech_ms`` is
            # a spurious blip: discard it silently (no speech_start/speech_end).
            if not self._triggered:
                self._candidate_start_t = None
                return []
            if self._silence_start_t is None:
                self._silence_start_t = frame_start_t
            if frame_end_t - self._silence_start_t >= self._min_silence_s:
                end = self._silence_start_t + self._speech_pad_s
                self._triggered = False
                self._silence_start_t = None
                return [VadEvent(kind="speech_end", t=end)]

        # Gray zone (neg_threshold <= prob < threshold): hold a confirmed segment
        # open (pending silence timer keeps running); an unconfirmed candidate also
        # holds, since its run has not cleanly dropped below the negative threshold.
        return []


def to_segments(events: Sequence[VadEvent]) -> list[SpeechSegment]:
    """Pair ``speech_start``/``speech_end`` events into closed :class:`SpeechSegment`s.

    Feeds the VAD-segmented offline engine (R4.4), which decodes one utterance
    per segment. A trailing ``speech_start`` with no matching ``speech_end``
    (i.e. speech still open — the caller has not flushed) is ignored.
    """
    segments: list[SpeechSegment] = []
    start: float | None = None
    for event in events:
        if event.kind == "speech_start":
            start = event.t
        elif start is not None:  # speech_end closing an open segment
            segments.append(SpeechSegment(start=start, end=event.t))
            start = None
    return segments


# --- Real Silero backend (lazily loaded) ------------------------------------


@dataclass
class SileroVadConfig:
    """Path + runtime knobs for a Silero VAD ONNX model (config-driven, NFR5).

    ``frame_ms`` defaults to 32 ms which is 512 samples at 16 kHz — Silero v5's
    required window size; override it (with ``model``) for other variants.
    """

    model: Path
    sample_rate: int = 16000
    frame_ms: int = 32
    threshold: float = 0.5
    neg_threshold: float | None = None
    min_silence_ms: int = 100
    min_speech_ms: int = 250
    speech_pad_ms: int = 30
    num_threads: int = 1
    provider: str = "cpu"


def _load_onnxruntime() -> Any:
    """Import ``onnxruntime`` lazily with a clear error if the wheel is absent."""
    try:
        import onnxruntime  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "onnxruntime is not installed. Install the aarch64 wheel pinned in "
            "requirements.txt to construct a real Silero VAD backend."
        ) from exc
    return onnxruntime


def _load_numpy() -> Any:
    """Import ``numpy`` lazily; only the real backend needs it."""
    try:
        import numpy
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "numpy is not installed. Install the aarch64 wheel pinned in "
            "requirements.txt to construct a real Silero VAD backend."
        ) from exc
    return numpy


class _SileroBackend:
    """Adapts a Silero VAD ONNX session to :class:`VadBackend` (one stream)."""

    def __init__(self, session: Any, *, sample_rate: int) -> None:
        np = _load_numpy()
        self._session = session
        self._np = np
        self._sr = np.array(sample_rate, dtype=np.int64)
        # Silero v5 unified recurrent state; shape [2, 1, 128]. # verify vs model.
        self._zero_state = np.zeros((2, 1, 128), dtype=np.float32)
        self._state = self._zero_state

    def probability(self, frame: Sequence[float]) -> float:
        np = self._np
        x = np.asarray(frame, dtype=np.float32).reshape(1, -1)
        # Graph output order is (probability, next_state). # verify vs model I/O.
        prob, self._state = self._session.run(
            None, {"input": x, "state": self._state, "sr": self._sr}
        )
        return float(np.asarray(prob).reshape(-1)[0])

    def reset(self) -> None:
        self._state = self._zero_state


def create_silero_session(config: SileroVadConfig) -> Any:
    """Construct the heavy Silero VAD ONNX ``InferenceSession`` once (shareable).

    The ONNX session (weights + arenas) is the expensive, stateless resource; the
    per-stream recurrent state lives in the :class:`_SileroBackend` wrapper. Split
    out from :func:`create_silero_backend` so the shared offline engine can build
    the session ONCE at startup and wrap a fresh backend per connection via
    :func:`make_silero_backend`, rather than re-loading the model per session.

    Raises ``RuntimeError`` if ``onnxruntime`` is unavailable or the model file is
    missing/invalid (fail-closed until the model + wheels are provisioned).
    """
    ort = _load_onnxruntime()
    session_options = ort.SessionOptions()
    session_options.inter_op_num_threads = config.num_threads
    session_options.intra_op_num_threads = config.num_threads
    providers = (
        ["CPUExecutionProvider"] if config.provider == "cpu" else [config.provider]
    )
    try:
        return ort.InferenceSession(
            str(config.model), sess_options=session_options, providers=providers
        )
    except Exception as exc:
        # A missing or invalid model file surfaces as onnxruntime's own exception
        # types (or a bare OSError); wrap them in a uniform, actionable error so
        # callers fail closed with a clear message rather than an opaque C++ trace.
        raise RuntimeError(
            f"failed to load Silero VAD model from {config.model!s}: {exc}"
        ) from exc


def make_silero_backend(session: Any, *, sample_rate: int) -> VadBackend:
    """Wrap a (possibly shared) Silero ONNX session in a fresh per-stream backend.

    Each backend carries its own recurrent state (``_state``), so multiple backends
    over one shared session decode independent streams without cross-talk.
    """
    return _SileroBackend(session, sample_rate=sample_rate)


def create_silero_backend(config: SileroVadConfig) -> VadBackend:
    """Construct a real Silero VAD backend from an on-disk ONNX model.

    Builds the session and wraps one backend — the standalone single-stream path
    (warmup / lifecycle ``/run``). Raises ``RuntimeError`` if ``onnxruntime`` is
    unavailable or the model file is missing/invalid (fail-closed).
    """
    session = create_silero_session(config)
    return make_silero_backend(session, sample_rate=config.sample_rate)
