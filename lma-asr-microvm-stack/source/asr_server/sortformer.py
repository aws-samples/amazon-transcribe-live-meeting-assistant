# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
from __future__ import annotations

import contextlib
import logging
import math
import os
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from asr_server.recognizer import (
    Event,
    Recognizer,
    RecognizerEngine,
    SessionConfig,
    WordTiming,
)

_LOG = logging.getLogger(__name__)

DIARIZER_KIND_ENV = "ASR_DIARIZER_KIND"
DIARIZER_KIND_SORTFORMER = "sortformer"
_MODEL_ENV = "ASR_DIARIZER_MODEL"
_MODEL_FILE_ENV = "ASR_DIARIZER_MODEL_FILE"
DIARIZER_SUBDIR = "diarizer"

SAMPLE_RATE = 16000
HOP = 160
N_FFT = 512
WIN = 400
N_MELS = 128
PREEMPHASIS = 0.97
LOG_GUARD = 2.0**-24
SUBSAMPLING = 8
NUM_SPEAKERS = 8
HIDDEN = 512
FRAME_SEC = HOP / SAMPLE_RATE


def sortformer_enabled() -> bool:
    return os.environ.get(DIARIZER_KIND_ENV, "") == DIARIZER_KIND_SORTFORMER


def default_model_path(model_dir: str | Path | None = None) -> Path:
    explicit = os.environ.get(_MODEL_ENV)
    if explicit:
        return Path(explicit)
    root = Path(model_dir or os.environ.get("ASR_MODEL_DIR", "/opt/models"))
    name = Path(os.environ.get(_MODEL_FILE_ENV, "") or "model_quantized.onnx").name
    return root / DIARIZER_SUBDIR / name


def _hz_to_mel(freq: np.ndarray) -> np.ndarray:
    f_sp = 200.0 / 3
    mels = freq / f_sp
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = math.log(6.4) / 27.0
    return np.where(
        freq >= min_log_hz,
        min_log_mel + np.log(np.maximum(freq, 1e-10) / min_log_hz) / logstep,
        mels,
    )


def _mel_to_hz(mels: np.ndarray) -> np.ndarray:
    f_sp = 200.0 / 3
    freqs = f_sp * mels
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = math.log(6.4) / 27.0
    return np.where(mels >= min_log_mel, min_log_hz * np.exp(logstep * (mels - min_log_mel)), freqs)


def slaney_mel_filters(
    sample_rate: int = SAMPLE_RATE, n_fft: int = N_FFT, n_mels: int = N_MELS
) -> np.ndarray:
    fft_freqs = np.linspace(0.0, sample_rate / 2.0, 1 + n_fft // 2)
    mel_points = np.linspace(
        _hz_to_mel(np.array(0.0)), _hz_to_mel(np.array(sample_rate / 2.0)), n_mels + 2
    )
    mel_f = _mel_to_hz(mel_points)
    fdiff = np.diff(mel_f)
    ramps = mel_f[:, None] - fft_freqs[None, :]
    lower = -ramps[:-2] / fdiff[:-1, None]
    upper = ramps[2:] / fdiff[1:, None]
    weights = np.maximum(0.0, np.minimum(lower, upper))
    weights *= (2.0 / (mel_f[2 : n_mels + 2] - mel_f[:n_mels]))[:, None]
    return weights.astype(np.float32)


class LogMelFrontEnd:

    def __init__(self) -> None:
        window = np.hanning(WIN)
        left = (N_FFT - WIN) // 2
        self._window = np.zeros(N_FFT, dtype=np.float32)
        self._window[left : left + WIN] = window.astype(np.float32)
        self._filters = slaney_mel_filters()
        self._buffer = np.zeros(N_FFT // 2, dtype=np.float32)
        self._buffer_base = -(N_FFT // 2)
        self._received = 0
        self._previous = 0.0
        self._next_frame = 0

    @property
    def frames_emitted(self) -> int:
        return self._next_frame

    def push(self, samples: np.ndarray) -> np.ndarray:
        if samples.size == 0:
            return np.zeros((0, N_MELS), dtype=np.float32)
        x = samples.astype(np.float32)
        emphasised = np.empty_like(x)
        if self._received == 0:
            emphasised[0] = x[0]
        else:
            emphasised[0] = x[0] - PREEMPHASIS * self._previous
        emphasised[1:] = x[1:] - PREEMPHASIS * x[:-1]
        self._previous = float(x[-1])
        self._received += x.size
        self._buffer = np.concatenate([self._buffer, emphasised])
        last = (self._received - N_FFT // 2) // HOP
        return self._emit(last)

    def flush(self) -> np.ndarray:
        total = self._received // HOP
        self._buffer = np.concatenate([self._buffer, np.zeros(N_FFT, dtype=np.float32)])
        return self._emit(total - 1)

    def _emit(self, last_frame: int) -> np.ndarray:
        count = last_frame - self._next_frame + 1
        if count <= 0:
            return np.zeros((0, N_MELS), dtype=np.float32)
        start = self._next_frame * HOP - N_FFT // 2 - self._buffer_base
        idx = start + np.arange(count)[:, None] * HOP + np.arange(N_FFT)[None, :]
        frames = self._buffer[idx] * self._window
        power = np.abs(np.fft.rfft(frames, n=N_FFT, axis=1)) ** 2
        mel = np.log(power.astype(np.float32) @ self._filters.T + LOG_GUARD)
        self._next_frame += count
        keep_from = self._next_frame * HOP - N_FFT // 2 - self._buffer_base
        if keep_from > 0:
            self._buffer = self._buffer[keep_from:]
            self._buffer_base += keep_from
        return mel.astype(np.float32)


@dataclass
class SpeakerCacheConfig:
    fifo_length: int = 264
    speaker_cache_length: int = 264
    speaker_cache_update_period: int = 222
    silence_frames_per_speaker: int = 1
    prediction_score_threshold: float = 0.25
    latest_frames_score_boost: float = 0.05
    min_positive_scores_rate: float = 0.5
    strong_boost_rate: float = 0.75
    weak_boost_rate: float = 1.5


class SpeakerCache:

    def __init__(self, config: SpeakerCacheConfig | None = None) -> None:
        self.config = config or SpeakerCacheConfig()
        cfg = self.config
        budget = cfg.speaker_cache_length // NUM_SPEAKERS - cfg.silence_frames_per_speaker
        self._min_positive = math.floor(budget * cfg.min_positive_scores_rate)
        self._strong = math.floor(budget * cfg.strong_boost_rate)
        self._weak = math.floor(budget * cfg.weak_boost_rate)
        self.embeds = np.zeros((0, HIDDEN), dtype=np.float32)
        self.probs = np.zeros((0, NUM_SPEAKERS), dtype=np.float32)
        self.fifo = np.zeros((0, HIDDEN), dtype=np.float32)
        self.compressed = False

    def cached(self) -> np.ndarray:
        return np.concatenate([self.embeds, self.fifo])

    def update(
        self,
        step_embeds: np.ndarray,
        step_logits: np.ndarray,
        silence: np.ndarray,
        num_chunk_frames: int,
    ) -> None:
        cfg = self.config
        num_cache, num_fifo = len(self.embeds), len(self.fifo)
        sig = 1.0 / (1.0 + np.exp(-step_logits))
        usable = len(sig) // SUBSAMPLING * SUBSAMPLING
        probs = sig[:usable].reshape(-1, SUBSAMPLING, NUM_SPEAKERS).mean(axis=1)
        chunk = step_embeds[num_cache + num_fifo : num_cache + num_fifo + num_chunk_frames]
        fifo = np.concatenate([self.fifo, chunk])
        popped = 0
        if len(fifo) > cfg.fifo_length:
            overflow = len(fifo) - cfg.fifo_length
            popped = min(max(cfg.speaker_cache_update_period, overflow), len(fifo))
        if popped:
            fifo_probs = probs[num_cache : num_cache + len(fifo)]
            stored = self.probs if self.compressed else probs[:num_cache]
            cache_embeds = np.concatenate([self.embeds, fifo[:popped]])
            cache_probs = np.concatenate([stored, fifo_probs[:popped]])
            fifo = fifo[popped:]
            if len(cache_embeds) > cfg.speaker_cache_length:
                cache_embeds, cache_probs = self._compress(cache_embeds, cache_probs, silence)
                self.compressed = True
            self.embeds = cache_embeds.astype(np.float32)
            self.probs = cache_probs.astype(np.float32)
        self.fifo = fifo.astype(np.float32)

    def _scores(self, probs: np.ndarray) -> np.ndarray:
        thr = self.config.prediction_score_threshold
        log_p = np.log(np.clip(probs, thr, None))
        log_c = np.log(np.clip(1.0 - probs, thr, None))
        scores = log_p - log_c + log_c.sum(axis=-1, keepdims=True) - math.log(0.5)
        speech = probs > 0.5
        scores = np.where(speech, scores, -np.inf)
        positive = scores > 0
        enough = positive.sum(axis=0, keepdims=True) >= self._min_positive
        return np.where(~positive & speech & enough, -np.inf, scores)

    @staticmethod
    def _boost(scores: np.ndarray, count: int, boost: float) -> np.ndarray:
        if count <= 0:
            return scores
        count = min(count, scores.shape[0])
        top = np.argpartition(-scores, count - 1, axis=0)[:count]
        out = scores.copy()
        for speaker in range(scores.shape[1]):
            out[top[:, speaker], speaker] += boost
        return out

    def _compress(
        self, embeds: np.ndarray, probs: np.ndarray, silence: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        cfg = self.config
        num_frames = len(probs)
        scores = self._scores(probs)
        scores[cfg.speaker_cache_length :] += cfg.latest_frames_score_boost
        scores = self._boost(scores, self._strong, -2.0 * math.log(0.5))
        scores = self._boost(scores, self._weak, -math.log(0.5))
        scores = np.concatenate(
            [scores, np.full((cfg.silence_frames_per_speaker, NUM_SPEAKERS), np.inf)]
        )
        embeds = np.concatenate([embeds, silence[None, :]])
        probs = np.concatenate([probs, np.zeros((1, NUM_SPEAKERS), dtype=probs.dtype)])
        num_scored = num_frames + cfg.silence_frames_per_speaker
        sentinel = num_scored * NUM_SPEAKERS
        flat = scores.T.reshape(-1)
        keep = cfg.speaker_cache_length
        top = np.argpartition(-flat, keep - 1)[:keep]
        top = np.sort(np.where(flat[top] == -np.inf, sentinel, top))
        frames = np.where(top == sentinel, num_frames, np.minimum(top % num_scored, num_frames))
        return embeds[frames], probs[frames]


class SortformerBackend(Protocol):
    def step(
        self, features: np.ndarray, cached: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]: ...


class OnnxSortformerBackend:

    def __init__(self, session: Any, *, lock: Any = None) -> None:
        self._session = session
        self._lock = lock if lock is not None else contextlib.nullcontext()

    @classmethod
    def from_path(cls, path: str | Path, *, num_threads: int = 1) -> OnnxSortformerBackend:
        if not Path(path).is_file():
            raise RuntimeError(f"diarization model not found at {path}")
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = num_threads
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(
            str(path), options, providers=["CPUExecutionProvider"]
        )
        return cls(session, lock=threading.Lock())

    def step(
        self, features: np.ndarray, cached: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        num_embeds = math.ceil(len(features) / SUBSAMPLING)
        feeds = {
            "input_features": features[None, :, :].astype(np.float32),
            "cached_embeds": cached[None, :, :].astype(np.float32),
            "attention_mask": np.ones((1, len(cached) + num_embeds), dtype=np.int64),
        }
        with self._lock:
            logits, chunk_embeds, silence = self._session.run(None, feeds)
        return logits[0], chunk_embeds[0], silence


@dataclass
class SortformerConfig:
    lookahead_frames: int = 4
    max_chunk_frames: int = 125
    idle_step_frames: int = 125
    history_seconds: float = 120.0
    min_turn_seconds: float = 0.5
    num_threads: int = 1
    slow_step_seconds: float = 2.0


class SortformerSession:

    def __init__(self, backend: SortformerBackend, config: SortformerConfig | None = None) -> None:
        self._backend = backend
        self._config = config or SortformerConfig()
        self._front_end = LogMelFrontEnd()
        self._cache = SpeakerCache()
        self._pending = np.zeros((0, N_MELS), dtype=np.float32)
        self._pending_base = 0
        self._probs = np.zeros((0, NUM_SPEAKERS), dtype=np.float32)
        self._probs_base = 0
        self._silence: np.ndarray | None = None
        self.steps = 0

    @property
    def labelled_until(self) -> float:
        return (self._probs_base + len(self._probs)) * FRAME_SEC

    def push(self, samples: np.ndarray) -> None:
        mel = self._front_end.push(samples)
        if len(mel):
            self._pending = np.concatenate([self._pending, mel])
        idle = self._config.idle_step_frames + self._config.lookahead_frames
        while len(self._pending) >= idle * SUBSAMPLING:
            self._step(self._config.idle_step_frames, self._config.lookahead_frames)

    def process_available(self) -> None:
        lookahead = self._config.lookahead_frames
        while True:
            chunk = len(self._pending) // SUBSAMPLING - lookahead
            if chunk <= 0:
                return
            self._step(min(chunk, self._config.max_chunk_frames), lookahead)

    def finish(self) -> None:
        mel = self._front_end.flush()
        if len(mel):
            self._pending = np.concatenate([self._pending, mel])
        while len(self._pending):
            chunk = math.ceil(len(self._pending) / SUBSAMPLING)
            self._step(min(chunk, self._config.max_chunk_frames), 0)

    def activity(self, start: float, end: float) -> np.ndarray:
        first = max(int(start / FRAME_SEC) - self._probs_base, 0)
        last = min(int(math.ceil(end / FRAME_SEC)) - self._probs_base, len(self._probs))
        if last <= first:
            return np.zeros((0, NUM_SPEAKERS), dtype=np.float32)
        return self._probs[first:last]

    def speaker_between(self, start: float, end: float) -> int | None:
        window = self.activity(start, end)
        if not len(window):
            window = self.activity(start - 0.5, end + 0.5)
        if not len(window):
            return None
        active = (window > 0.5).sum(axis=0)
        if active.max() > 0:
            return int(active.argmax())
        mean = window.mean(axis=0)
        return int(mean.argmax()) if mean.max() > 0.2 else None

    def _step(self, chunk_frames: int, lookahead: int) -> None:
        began = time.monotonic()
        total_embeds = min(chunk_frames + lookahead, math.ceil(len(self._pending) / SUBSAMPLING))
        mel = self._pending[: total_embeds * SUBSAMPLING]
        chunk_frames = min(chunk_frames, total_embeds)
        cached = self._cache.cached()
        logits, chunk_embeds, silence = self._backend.step(mel, cached)
        self._silence = silence
        step_embeds = np.concatenate([cached, chunk_embeds])
        self._cache.update(step_embeds, logits, silence, chunk_frames)
        start = len(cached) * SUBSAMPLING
        chunk_mel = min(chunk_frames * SUBSAMPLING, len(self._pending))
        probs = 1.0 / (1.0 + np.exp(-logits[start : start + chunk_mel]))
        self._probs = np.concatenate([self._probs, probs.astype(np.float32)])
        self._pending = self._pending[chunk_mel:]
        self._pending_base += chunk_mel
        self.steps += 1
        elapsed = time.monotonic() - began
        if elapsed > self._config.slow_step_seconds:
            _LOG.warning(
                "sortformer step %d took %.1fs for %d frames (%d cached)",
                self.steps,
                elapsed,
                chunk_frames,
                len(cached),
            )
        excess = len(self._probs) - int(self._config.history_seconds / FRAME_SEC)
        if excess > 0:
            self._probs = self._probs[excess:]
            self._probs_base += excess


class SortformerRecognizer(Recognizer):

    def __init__(
        self,
        inner: Recognizer,
        diarizer: SortformerSession,
        *,
        sample_rate: int = SAMPLE_RATE,
        min_turn_seconds: float = 0.5,
    ) -> None:
        if sample_rate != SAMPLE_RATE:
            raise ValueError("the diarization model is built for 16 kHz audio")
        self._inner = inner
        self._diarizer = diarizer
        self._min_turn = min_turn_seconds
        self._offset = 0
        self._labels: dict[int, str] = {}
        self._failed = False

    def accept_pcm(self, pcm: bytes) -> list[Event]:
        events = self._inner.accept_pcm(pcm)
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        self._diarize(self._diarizer.push, samples)
        return self._label_all(events)

    def flush(self) -> list[Event]:
        events = self._inner.flush()
        self._diarize(self._diarizer.finish)
        return self._label_all(events)

    def _diarize(self, step: Any, *args: Any) -> None:
        if self._failed:
            return
        try:
            step(*args)
        except Exception:
            self._failed = True
            _LOG.exception(
                "sortformer diarizer failed; rows carry no speaker labels from here on"
            )

    def current_segment(self) -> int:
        return self._inner.current_segment() + self._offset

    def current_words(self) -> list[WordTiming]:
        return []

    def _label_all(self, events: Sequence[Event]) -> list[Event]:
        out: list[Event] = []
        for event in events:
            if event.kind != "final":
                out.append(replace(event, segment=event.segment + self._offset, speaker=None))
                continue
            self._diarize(self._diarizer.process_available)
            if self._failed:
                out.append(replace(event, segment=event.segment + self._offset, speaker=None))
                continue
            try:
                out.extend(self._label_final(event))
            except Exception:
                self._failed = True
                _LOG.exception(
                    "sortformer labelling failed; rows carry no speaker labels from here on"
                )
                out.append(replace(event, segment=event.segment + self._offset, speaker=None))
        return out

    def _label(self, index: int | None) -> str | None:
        if index is None:
            return None
        if index not in self._labels:
            self._labels[index] = f"spk_{len(self._labels)}"
        return self._labels[index]

    def _label_final(self, event: Event) -> list[Event]:
        start = event.start if event.start is not None else 0.0
        end = event.end if event.end is not None else start
        words = list(event.words or [])
        if not words:
            speaker = self._label(self._diarizer.speaker_between(start, end))
            return [replace(event, segment=event.segment + self._offset, speaker=speaker)]
        runs = self._runs(words)
        rows: list[Event] = []
        for index, (speaker, group) in enumerate(runs):
            rows.append(
                replace(
                    event,
                    segment=event.segment + self._offset + index,
                    start=start if index == 0 else group[0].s,
                    end=end if index == len(runs) - 1 else group[-1].e,
                    text=" ".join(word.w for word in group),
                    words=group,
                    speaker=self._label(speaker),
                )
            )
        self._offset += len(runs) - 1
        return rows

    def _runs(self, words: list[WordTiming]) -> list[tuple[int | None, list[WordTiming]]]:
        speakers = [
            self._diarizer.speaker_between(word.s, max(word.e, word.s + FRAME_SEC))
            for word in words
        ]
        for index, speaker in enumerate(speakers):
            if speaker is None:
                speakers[index] = next(
                    (s for s in reversed(speakers[:index]) if s is not None),
                    next((s for s in speakers[index + 1 :] if s is not None), None),
                )
        runs: list[tuple[int | None, list[WordTiming]]] = []
        for word, speaker in zip(words, speakers, strict=True):
            if runs and runs[-1][0] == speaker:
                runs[-1][1].append(word)
            else:
                runs.append((speaker, [word]))
        changed = True
        while changed and len(runs) > 1:
            changed = False
            for index, (_, group) in enumerate(runs):
                if group[-1].e - group[0].s >= self._min_turn or len(group) >= 3:
                    continue
                target = index - 1 if index > 0 else index + 1
                merged = runs[target][1] + group if target < index else group + runs[target][1]
                merged.sort(key=lambda word: word.s)
                runs[target] = (runs[target][0], merged)
                del runs[index]
                changed = True
                break
        merged_runs: list[tuple[int | None, list[WordTiming]]] = []
        for speaker, group in runs:
            if merged_runs and merged_runs[-1][0] == speaker:
                merged_runs[-1][1].extend(group)
            else:
                merged_runs.append((speaker, group))
        return merged_runs


class SortformerEngine(RecognizerEngine):

    def __init__(
        self,
        inner: RecognizerEngine,
        backend: SortformerBackend,
        config: SortformerConfig | None = None,
    ) -> None:
        self._inner = inner
        self._backend = backend
        self._config = config or SortformerConfig()

    @property
    def inner(self) -> RecognizerEngine:
        return self._inner

    def new_session(self, config: SessionConfig | None = None) -> Recognizer:
        session = self._inner.new_session(config)
        if config is not None and config.diarize is False:
            _LOG.info("speaker labels off: sortformer diarizer not attached")
            return session
        sample_rate = config.sample_rate if config is not None else SAMPLE_RATE
        _LOG.info(
            "sortformer diarizer ready: speakers<=%d lookahead=%d frames max_chunk=%d frames",
            NUM_SPEAKERS,
            self._config.lookahead_frames,
            self._config.max_chunk_frames,
        )
        return SortformerRecognizer(
            session,
            SortformerSession(self._backend, self._config),
            sample_rate=sample_rate,
            min_turn_seconds=self._config.min_turn_seconds,
        )


def build_sortformer_config() -> SortformerConfig:
    return SortformerConfig(num_threads=int(os.environ.get("ASR_NUM_THREADS", "1")))


def create_sortformer_engine(
    inner: RecognizerEngine, config: SortformerConfig | None = None
) -> SortformerEngine:
    config = config or build_sortformer_config()
    path = default_model_path()
    backend = OnnxSortformerBackend.from_path(path, num_threads=config.num_threads)
    _LOG.info("sortformer diarization enabled: model=%s threads=%d", path, config.num_threads)
    return SortformerEngine(inner, backend, config)


def warm_sortformer(model_dir: str | Path | None = None, *, num_threads: int = 1) -> int:
    path = default_model_path(model_dir)
    backend = OnnxSortformerBackend.from_path(path, num_threads=num_threads)
    session = SortformerSession(backend)
    rng = np.random.default_rng(0)
    session.push((rng.standard_normal(SAMPLE_RATE * 3) * 0.05).astype(np.float32))
    session.finish()
    return session.steps
