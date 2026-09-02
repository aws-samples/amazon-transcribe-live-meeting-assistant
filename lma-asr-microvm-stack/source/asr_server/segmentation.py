# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Speaker-turn and overlap detection with pyannote segmentation 3.0 (ONNX).

Endpointing closes an utterance on silence, so two people speaking without a gap
between them land in one segment and get one speaker label. This module finds the
turn boundaries *inside* such a segment so it can be split.

It returns boundary times, not speaker identities: identity stays with the
embedder and the per-session registry. That way nothing has to reconcile this
model's per-window local speaker numbering, which is arbitrary and not comparable
across windows.

See docs/microvm-asr.md, "Splitting a segment on a speaker change".
"""

from __future__ import annotations

import os
import sys
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "POWERSET",
    "SegmentationConfig",
    "SegmentationBackend",
    "SegmentationResult",
    "TurnDetector",
    "create_onnx_backend",
    "build_segmentation_config",
]

DEFAULT_MODEL_PATH = Path(os.environ.get("ASR_MODEL_DIR", "/opt/models")) / "segmentation.onnx"

POWERSET: tuple[frozenset[int], ...] = (
    frozenset(),
    frozenset({0}),
    frozenset({1}),
    frozenset({2}),
    frozenset({0, 1}),
    frozenset({0, 2}),
    frozenset({1, 2}),
)

_BYTES_PER_SAMPLE = 2
_INT16_SCALE = 32768.0


@dataclass(frozen=True)
class SegmentationConfig:
    """Operating point for turn detection."""

    sample_rate: int = 16000
    window_sec: float = 10.0
    # A change must persist this long to count. Shorter than this is a back-channel
    # ("mhm") or a model flicker, and splitting on it produces unreadable rows.
    min_turn_ms: int = 700
    model_path: Path = DEFAULT_MODEL_PATH
    num_threads: int = 1

    @property
    def window_samples(self) -> int:
        return int(self.window_sec * self.sample_rate)


@dataclass(frozen=True)
class SegmentationResult:
    """Where the speaker changes, and where voices overlap."""

    boundaries: tuple[float, ...]
    overlaps: tuple[tuple[float, float], ...]
    frame_sec: float

    @property
    def has_change(self) -> bool:
        return len(self.boundaries) > 0


class SegmentationBackend(Protocol):
    """Runs the segmentation graph over one window of float samples."""

    def run(self, samples: Sequence[float]) -> Sequence[Sequence[float]]:
        """Return per-frame scores over the 7 powerset classes."""
        ...


def pcm16_to_float32(pcm: bytes) -> array:
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % _BYTES_PER_SAMPLE)])
    if sys.byteorder == "big":
        samples.byteswap()
    return array("f", (value / _INT16_SCALE for value in samples))


def _active_sets(scores: Sequence[Sequence[float]]) -> list[frozenset[int]]:
    active: list[frozenset[int]] = []
    for frame in scores:
        best = 0
        best_score = float("-inf")
        for index, value in enumerate(frame[: len(POWERSET)]):
            if value > best_score:
                best_score = value
                best = index
        active.append(POWERSET[best])
    return active


def _runs(active: Sequence[frozenset[int]]) -> list[tuple[int, int, frozenset[int]]]:
    runs: list[tuple[int, int, frozenset[int]]] = []
    start = 0
    for index in range(1, len(active) + 1):
        if index == len(active) or active[index] != active[start]:
            runs.append((start, index, active[start]))
            start = index
    return runs


class TurnDetector:
    """Finds speaker-change boundaries in a stretch of one audio channel.

    The backend is injected so the boundary logic is testable without weights,
    matching :mod:`asr_server.vad`.
    """

    def __init__(
        self, backend: SegmentationBackend, config: SegmentationConfig | None = None
    ) -> None:
        self._backend = backend
        self._config = config or SegmentationConfig()

    @property
    def config(self) -> SegmentationConfig:
        return self._config

    def detect(self, pcm: bytes) -> SegmentationResult:
        samples = pcm16_to_float32(pcm)
        return self.detect_samples(samples)

    def detect_samples(self, samples: Sequence[float]) -> SegmentationResult:
        config = self._config
        window = config.window_samples
        if len(samples) == 0 or window <= 0:
            return SegmentationResult((), (), 0.0)

        hop = max(1, window // 2)
        boundaries: list[float] = []
        overlaps: list[tuple[float, float]] = []
        frame_sec = 0.0
        offsets = [0]
        while offsets[-1] + window < len(samples):
            offsets.append(offsets[-1] + hop)

        for index, offset in enumerate(offsets):
            chunk = list(samples[offset : offset + window])
            real_sec = len(chunk) / config.sample_rate
            if len(chunk) < window:
                chunk.extend([0.0] * (window - len(chunk)))
            scores = self._backend.run(chunk)
            if not scores:
                continue
            frame_sec = config.window_sec / len(scores)
            keep_from, keep_to = self._trust_region(index, len(offsets), config.window_sec)

            active = _active_sets(scores)
            for start, end, speakers in _runs(active):
                start_sec = start * frame_sec
                end_sec = min(end * frame_sec, real_sec)
                if end_sec <= start_sec:
                    continue
                if len(speakers) > 1:
                    absolute = offset / config.sample_rate
                    if keep_from <= start_sec < keep_to:
                        overlaps.append((absolute + start_sec, absolute + end_sec))

            for boundary in self._boundaries(active, frame_sec, real_sec):
                if keep_from <= boundary < keep_to:
                    boundaries.append(offset / config.sample_rate + boundary)

        merged = self._merge(sorted(boundaries), frame_sec)
        return SegmentationResult(tuple(merged), tuple(sorted(overlaps)), frame_sec)

    def _trust_region(self, index: int, count: int, window_sec: float) -> tuple[float, float]:
        """Which part of a window's output to believe.

        Windows overlap by half, and this model is least reliable at a window's
        edges, where a turn is only partly visible. Taking the middle half of each
        window (and the leading/trailing quarter of the first/last) means every
        instant is judged once, by the window that saw the most context around it.
        """
        if count == 1:
            return (0.0, window_sec)
        quarter = window_sec / 4
        if index == 0:
            return (0.0, 3 * quarter)
        if index == count - 1:
            return (quarter, window_sec)
        return (quarter, 3 * quarter)

    def _boundaries(
        self, active: Sequence[frozenset[int]], frame_sec: float, real_sec: float
    ) -> list[float]:
        min_frames = max(1, int((self._config.min_turn_ms / 1000.0) / max(frame_sec, 1e-9)))
        speech_runs = [
            (start, end, speakers)
            for start, end, speakers in _runs(active)
            if len(speakers) == 1 and (end - start) >= min_frames and start * frame_sec < real_sec
        ]

        boundaries: list[float] = []
        for previous, current in zip(speech_runs, speech_runs[1:], strict=False):
            if previous[2] == current[2]:
                continue
            gap_start, gap_end = previous[1], current[0]
            if gap_end > gap_start:
                # Cut in the middle of the silence between the two turns, which is
                # where a word gap is, rather than clipping either speaker.
                cut = (gap_start + gap_end) / 2 * frame_sec
            else:
                cut = gap_start * frame_sec
            if 0.0 < cut < real_sec:
                boundaries.append(cut)
        return boundaries

    def _merge(self, boundaries: Sequence[float], frame_sec: float) -> list[float]:
        tolerance = max(frame_sec * 2, self._config.min_turn_ms / 2000.0)
        merged: list[float] = []
        for boundary in boundaries:
            if merged and boundary - merged[-1] < tolerance:
                continue
            merged.append(boundary)
        return merged


def create_onnx_backend(config: SegmentationConfig) -> SegmentationBackend:
    """Real backend over onnxruntime. Fails closed when the model is absent."""
    if not config.model_path.is_file():
        raise RuntimeError(
            f"segmentation model not found at {config.model_path}: build the image with "
            "AsrSegmentationModelId set, or disable turn splitting"
        )
    try:
        import numpy as np
        import onnxruntime
    except ImportError as exc:  # pragma: no cover - provisioning failure
        raise RuntimeError(f"onnxruntime is unavailable: {exc}") from exc

    options = onnxruntime.SessionOptions()
    options.log_severity_level = 3
    options.intra_op_num_threads = config.num_threads
    session = onnxruntime.InferenceSession(
        str(config.model_path), sess_options=options, providers=["CPUExecutionProvider"]
    )
    input_name = session.get_inputs()[0].name

    class _OnnxBackend:
        def run(self, samples: Sequence[float]) -> Sequence[Sequence[float]]:
            batch = np.asarray(samples, dtype=np.float32).reshape(1, 1, -1)
            output: Any = session.run(None, {input_name: batch})[0]
            return output[0].tolist()

    return _OnnxBackend()


def build_segmentation_config() -> SegmentationConfig:
    return SegmentationConfig(
        sample_rate=int(os.environ.get("ASR_SAMPLE_RATE", "16000")),
        window_sec=float(os.environ.get("ASR_SEGMENTATION_MODEL_WINDOW_SEC", "10.0")),
        min_turn_ms=int(os.environ.get("ASR_MIN_TURN_MS", "700")),
        model_path=Path(
            os.environ.get("ASR_SEGMENTATION_MODEL_PATH", str(DEFAULT_MODEL_PATH))
        ),
        num_threads=int(os.environ.get("ASR_SEGMENTATION_THREADS", "1")),
    )
