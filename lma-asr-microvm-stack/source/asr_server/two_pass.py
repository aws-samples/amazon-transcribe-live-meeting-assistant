"""Two-pass engine: a streaming preview of the open utterance, replaced at its close.

The offline ("accurate") recogniser owns segmentation and the final text. While an
utterance is open, a streaming recogniser fed the same audio supplies the live row;
the offline decode then arrives as the final under the same segment number.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from asr_server.offline_recognizer import OfflineModelConfig, create_sherpa_offline_engine
from asr_server.recognizer import (
    DEFAULT_MODEL_DIR,
    Event,
    Recognizer,
    RecognizerEngine,
    SessionConfig,
    SherpaModelConfig,
    WordTiming,
    create_sherpa_engine,
)

_LOG = logging.getLogger(__name__)

PREVIEW_SUBDIR = "preview"
_BYTES_PER_SAMPLE = 2


def preview_model_dir(model_dir: str | Path | None = None) -> Path:
    """Where the preview model's files live, beside the authority's."""
    root = (
        Path(model_dir)
        if model_dir is not None
        else Path(os.environ.get("ASR_MODEL_DIR", DEFAULT_MODEL_DIR))
    )
    return root / PREVIEW_SUBDIR


class UtteranceRecognizer(Protocol):
    """The offline recogniser's surface the two-pass composite relies on."""

    def accept_pcm(self, pcm: bytes) -> list[Event]: ...

    def flush(self) -> list[Event]: ...

    def current_segment(self) -> int: ...

    def open_segment_start(self) -> float | None: ...


class TwoPassRecognizer(Recognizer):
    """Emits streaming partials for the open utterance and the offline final at its close.

    A fresh preview session is started for every utterance and fed the audio since
    the utterance began, so a preview never shows text from the utterance before it.
    Word timings are not exposed, so rows are cut only when an utterance closes.
    """

    def __init__(
        self,
        authority: UtteranceRecognizer,
        preview_factory: Callable[[], Recognizer],
        *,
        sample_rate: int = 16000,
        onset_buffer_ms: int = 2000,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if onset_buffer_ms <= 0:
            raise ValueError("onset_buffer_ms must be positive")
        self._authority = authority
        self._preview_factory = preview_factory
        self._sample_rate = sample_rate
        self._onset_buffer_bytes = sample_rate * onset_buffer_ms // 1000 * _BYTES_PER_SAMPLE
        self._recent = bytearray()
        self._recent_base = 0
        self._total_bytes = 0
        self._preview: Recognizer | None = None
        self._preview_finals: list[str] = []
        self._preview_partial = ""
        self._preview_text = ""

    def accept_pcm(self, pcm: bytes) -> list[Event]:
        if not pcm:
            return []
        self._remember(pcm)
        events = [event for event in self._authority.accept_pcm(pcm) if event.kind == "final"]
        if events:
            self._drop_preview()
        open_start = self._authority.open_segment_start()
        if open_start is None:
            self._drop_preview()
            return events
        if self._preview is None:
            self._preview = self._preview_factory()
            preview_events = self._preview.accept_pcm(self._since(open_start))
        else:
            preview_events = self._preview.accept_pcm(pcm)
        for event in preview_events:
            if event.kind == "final":
                self._preview_finals.append(event.text)
                self._preview_partial = ""
            else:
                self._preview_partial = event.text
        text = " ".join(
            part for part in [*self._preview_finals, self._preview_partial] if part
        ).strip()
        if text and text != self._preview_text:
            self._preview_text = text
            events.append(
                Event(
                    kind="partial",
                    segment=self._authority.current_segment(),
                    text=text,
                    start=open_start,
                    end=self._total_bytes / _BYTES_PER_SAMPLE / self._sample_rate,
                )
            )
        return events

    def flush(self) -> list[Event]:
        events = [event for event in self._authority.flush() if event.kind == "final"]
        self._drop_preview()
        return events

    def current_segment(self) -> int:
        return self._authority.current_segment()

    def current_words(self) -> list[WordTiming]:
        return []

    def _remember(self, pcm: bytes) -> None:
        self._recent.extend(pcm)
        self._total_bytes += len(pcm)
        excess = len(self._recent) - self._onset_buffer_bytes
        if excess > 0:
            del self._recent[:excess]
            self._recent_base += excess

    def _since(self, start_seconds: float) -> bytes:
        offset = round(start_seconds * self._sample_rate) * _BYTES_PER_SAMPLE
        begin = max(0, offset - self._recent_base)
        return bytes(self._recent[begin:])

    def _drop_preview(self) -> None:
        self._preview = None
        self._preview_finals = []
        self._preview_partial = ""
        self._preview_text = ""


class TwoPassEngine(RecognizerEngine):
    """One offline engine for the finals, one streaming engine for the previews."""

    def __init__(
        self,
        authority: RecognizerEngine,
        preview: RecognizerEngine,
        *,
        sample_rate: int = 16000,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        self._authority = authority
        self._preview = preview
        self._sample_rate = sample_rate

    @property
    def authority(self) -> RecognizerEngine:
        return self._authority

    @property
    def preview(self) -> RecognizerEngine:
        return self._preview

    def new_session(self, config: SessionConfig | None = None) -> Recognizer:
        session = cast("UtteranceRecognizer", self._authority.new_session(config))
        sample_rate = config.sample_rate if config is not None else self._sample_rate
        preview_config = SessionConfig(sample_rate=sample_rate)
        return TwoPassRecognizer(
            session,
            lambda: self._preview.new_session(preview_config),
            sample_rate=sample_rate,
        )


def create_two_pass_engine(
    offline_config: OfflineModelConfig, preview_config: SherpaModelConfig
) -> TwoPassEngine:
    """Build both shared engines once, at server startup."""
    engine = TwoPassEngine(
        create_sherpa_offline_engine(offline_config),
        create_sherpa_engine(preview_config),
        sample_rate=offline_config.sample_rate,
    )
    _LOG.info(
        "two-pass engine ready: authority=%s preview=%s",
        offline_config.encoder,
        preview_config.encoder,
    )
    return engine
