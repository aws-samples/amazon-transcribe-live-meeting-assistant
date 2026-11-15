# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Tests for the pre-snapshot warm-up decode and the per-decode timing line.

The image's snapshot is taken when ``/ready`` answers 200, so the warm-up must have
run the offline model on an utterance as long as the VAD's longest segment by then,
whatever the bundle and whether or not the warm-up audio is recognised as speech.
The timing line is how a live run shows whether a slow decode was computing or
waiting.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from asr_microvm.hook_server import WarmDecodeChannel
from asr_server import recognizer as recognizer_module
from asr_server import ws_server
from asr_server.offline_recognizer import (
    OfflineDecoderBackend,
    OfflineModelConfig,
    OfflineResult,
    SherpaOfflineEngine,
    SherpaOfflineRecognizer,
)
from asr_server.recognizer import Event, Recognizer, RecognizerEngine, SessionConfig
from asr_server.sortformer import SortformerEngine
from asr_server.two_pass import TwoPassEngine, TwoPassRecognizer
from asr_server.vad import SileroVadConfig, VadGate
from asr_server.warmup import WARM_DECODE_SECONDS, speech_like_pcm

from tests.test_vad import ScriptedVadBackend

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512
TIMING = re.compile(
    r"(?P<model>\w+) decode: audio=(?P<audio>[\d.]+)s wall=[\d.]+s cpu=[\d.]+s "
    r"\([\d.]+ cores\) lock_wait=\S+ first_in_process=(?P<first_process>True|False) "
    r"first_in_session=(?P<first_session>True|False) warm_up=(?P<warm>True|False)"
)


@pytest.fixture(autouse=True)
def _fresh_process_counters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recognizer_module, "_FIRST_DECODES", set())


def _timing_lines(caplog: pytest.LogCaptureFixture) -> list[re.Match[str]]:
    return [m for r in caplog.records if (m := TIMING.search(r.getMessage())) is not None]


class FakeStream:
    def __init__(self) -> None:
        self.samples: Any = None
        self.result = type("Result", (), {"text": "hello", "tokens": [], "timestamps": []})()

    def accept_waveform(self, sample_rate: int, samples: Any) -> None:
        self.sample_rate = sample_rate
        self.samples = samples


class FakeSherpaOfflineRecognizer:
    """Stands in for sherpa-onnx's OfflineRecognizer; records each decoded stream."""

    def __init__(self) -> None:
        self.decoded: list[FakeStream] = []

    def create_stream(self) -> FakeStream:
        return FakeStream()

    def decode_stream(self, stream: FakeStream) -> None:
        self.decoded.append(stream)


def _offline_config() -> OfflineModelConfig:
    return OfflineModelConfig(
        tokens=Path("tokens.txt"),
        encoder=Path("encoder.onnx"),
        decoder=Path("decoder.onnx"),
        joiner=Path("joiner.onnx"),
        vad=SileroVadConfig(model=Path("silero_vad.onnx")),
    )


# --- The offline engine's warm-up decode ------------------------------------


def test_offline_warm_up_decodes_the_whole_clip_without_the_vad(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sherpa = FakeSherpaOfflineRecognizer()
    # The VAD session is a bare object: the warm-up must not touch it.
    engine = SherpaOfflineEngine(sherpa, object(), _offline_config())
    pcm = speech_like_pcm(WARM_DECODE_SECONDS)

    with caplog.at_level(logging.INFO, logger="asr_server"):
        assert engine.warm_up(pcm) == 1

    assert len(sherpa.decoded) == 1
    decoded = sherpa.decoded[0]
    assert decoded.sample_rate == SAMPLE_RATE
    # One utterance as long as the VAD's longest segment, not a fraction of it.
    assert len(decoded.samples) == int(WARM_DECODE_SECONDS * SAMPLE_RATE)
    assert len(decoded.samples) >= 20 * SAMPLE_RATE
    assert max(abs(float(v)) for v in decoded.samples) > 0.05
    [line] = _timing_lines(caplog)
    assert line["model"] == "offline"
    assert float(line["audio"]) == pytest.approx(WARM_DECODE_SECONDS, abs=0.01)
    assert line["warm"] == "True"
    assert line["first_process"] == "True"


def test_offline_warm_up_of_no_audio_decodes_nothing() -> None:
    sherpa = FakeSherpaOfflineRecognizer()
    engine = SherpaOfflineEngine(sherpa, object(), _offline_config())

    assert engine.warm_up(b"") == 0
    assert sherpa.decoded == []


class CountingEngine(RecognizerEngine):
    def __init__(self) -> None:
        self.warmed: list[int] = []

    def new_session(self, config: SessionConfig | None = None) -> Recognizer:
        raise AssertionError("the warm-up must not open a session on this engine")

    def warm_up(self, pcm: bytes, **_: object) -> int:
        self.warmed.append(len(pcm))
        return 1


def test_two_pass_warm_up_runs_the_authority_and_the_preview() -> None:
    authority, preview = CountingEngine(), CountingEngine()
    pcm = speech_like_pcm(1.0)

    assert TwoPassEngine(authority, preview).warm_up(pcm) == 2
    assert authority.warmed == [len(pcm)]
    assert preview.warmed == [len(pcm)]


def test_a_diarizing_wrapper_warms_the_engine_it_wraps() -> None:
    inner = CountingEngine()
    engine = SortformerEngine(inner, backend=object())  # type: ignore[arg-type]

    assert engine.warm_up(b"\x00\x01" * 10) == 1
    assert inner.warmed == [20]


def test_an_engine_without_a_model_warms_nothing() -> None:
    class Bare(RecognizerEngine):
        def new_session(self, config: SessionConfig | None = None) -> Recognizer:
            raise AssertionError

    assert Bare().warm_up(b"\x00\x00") == 0


# --- The per-decode timing line ----------------------------------------------


class OneTextBackend(OfflineDecoderBackend):
    def __init__(self) -> None:
        self.last_lock_wait_s = 0.25

    def decode(self, sample_rate: int, samples: Sequence[float]) -> OfflineResult:
        return OfflineResult(text="words")


def _speech_then_silence_gate() -> VadGate:
    # 10 speech frames, 5 silent ones, repeated: one closed segment per 15 frames.
    probs = ([0.9] * 10 + [0.0] * 5) * 4
    return VadGate(
        ScriptedVadBackend(probs),
        sample_rate=SAMPLE_RATE,
        frame_ms=32,
        min_speech_ms=0,
        min_silence_ms=0,
        speech_pad_ms=0,
    )


def test_every_session_decode_logs_its_timing(caplog: pytest.LogCaptureFixture) -> None:
    session = SherpaOfflineRecognizer(_speech_then_silence_gate(), OneTextBackend())
    frame = b"\x10\x00" * FRAME_SAMPLES

    with caplog.at_level(logging.INFO, logger="asr_server"):
        for _ in range(30):
            session.accept_pcm(frame)

    lines = _timing_lines(caplog)
    assert len(lines) == 2
    assert [m["first_session"] for m in lines] == ["True", "False"]
    assert [m["first_process"] for m in lines] == ["True", "False"]
    assert all(m["model"] == "offline" and m["warm"] == "False" for m in lines)
    assert float(lines[0]["audio"]) == pytest.approx(10 * FRAME_SAMPLES / SAMPLE_RATE, abs=0.04)
    assert "lock_wait=0.250s" in lines[0].string


class FakeAuthority:
    """An utterance opens on the first chunk and stays open."""

    def __init__(self) -> None:
        self.fed = 0

    def accept_pcm(self, pcm: bytes) -> list[Event]:
        self.fed += len(pcm)
        return []

    def flush(self) -> list[Event]:
        return []

    def current_segment(self) -> int:
        return 0

    def open_segment_start(self) -> float | None:
        return 0.0


class EchoPreview(Recognizer):
    def accept_pcm(self, pcm: bytes) -> list[Event]:
        return [Event(kind="partial", segment=0, text="hi", start=0.0, end=0.1)]

    def flush(self) -> list[Event]:
        return []


def test_the_preview_logs_its_first_decode_in_each_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = TwoPassRecognizer(FakeAuthority(), EchoPreview)
    chunk = b"\x00\x00" * 1600

    with caplog.at_level(logging.INFO, logger="asr_server"):
        for _ in range(5):
            session.accept_pcm(chunk)

    [line] = _timing_lines(caplog)
    assert line["model"] == "preview"
    assert line["first_session"] == "True"
    assert line["warm"] == "False"


# --- The server's warm-up control pipe ----------------------------------------


def test_run_warm_up_hands_the_engine_twenty_seconds_of_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASR_MODEL_DIR", str(tmp_path))
    monkeypatch.delenv("ASR_WARM_DECODE_S", raising=False)
    engine = CountingEngine()

    reply = ws_server.run_warm_up(engine)

    assert reply["ok"] is True
    assert reply["decodes"] == 1
    assert engine.warmed == [int(WARM_DECODE_SECONDS * SAMPLE_RATE) * 2]
    assert "synthetic" in str(reply["audio"])


def _serve(engine: RecognizerEngine, warm: Any = ws_server.run_warm_up) -> WarmDecodeChannel:
    server_reads, hook_writes = os.pipe()
    hook_reads, server_writes = os.pipe()
    threading.Thread(
        target=ws_server.serve_warm_requests,
        args=(engine, server_reads, server_writes, warm),
        daemon=True,
    ).start()
    channel = WarmDecodeChannel()
    channel.attach(hook_writes, hook_reads)
    return channel


def test_the_hook_runs_the_server_warm_up_over_the_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ASR_MODEL_DIR", str(tmp_path))
    engine = CountingEngine()
    channel = _serve(engine)

    assert channel.warm(timeout_s=10) is True
    assert channel.warm(timeout_s=10) is True
    assert len(engine.warmed) == 2


def test_a_failed_server_warm_up_is_reported_as_not_warm() -> None:
    def failing(engine: RecognizerEngine) -> dict[str, object]:
        raise RuntimeError("decode failed")

    channel = _serve(CountingEngine(), warm=failing)

    assert channel.warm(timeout_s=10) is False


def test_a_slow_warm_up_times_out_and_its_late_reply_is_skipped() -> None:
    release = threading.Event()
    calls: list[int] = []

    def slow_then_fast(engine: RecognizerEngine) -> dict[str, object]:
        calls.append(1)
        if len(calls) == 1:
            release.wait(5)
        return {"ok": True, "decodes": 1, "audio": "x"}

    channel = _serve(CountingEngine(), warm=slow_then_fast)

    assert channel.warm(timeout_s=0.2) is False
    release.set()
    # The first request's reply arrives first and must not be taken for this one's.
    assert channel.warm(timeout_s=10) is True
    assert len(calls) == 2


def test_the_hook_and_the_server_name_the_same_pipe_variable() -> None:
    from asr_microvm import hook_server

    assert hook_server.WARM_CONTROL_ENV == ws_server.WARM_CONTROL_ENV


def test_without_a_spawned_server_the_warm_up_is_skipped() -> None:
    assert WarmDecodeChannel().warm(timeout_s=1) is True


def test_the_server_serves_warm_requests_only_when_given_its_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ws_server.WARM_CONTROL_ENV, raising=False)
    assert ws_server.start_warm_control(CountingEngine()) is None
    monkeypatch.setenv(ws_server.WARM_CONTROL_ENV, "not-fds")
    assert ws_server.start_warm_control(CountingEngine()) is None
