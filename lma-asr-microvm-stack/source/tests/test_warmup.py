"""Unit tests for the build-time snapshot warmup.

The warmup exits successfully with the model
resident and the log confirms it warm. Because the runtime fails closed (no model
weights, no ``sherpa_onnx`` wheel), these tests drive :func:`run` / :func:`warmup`
through the same scripted fake :class:`DecoderBackend` the recogniser tests use
— an injected recogniser factory stands in for the real sherpa-onnx engine, so
the exact ``accept_pcm``/``flush`` decode path runs with no weights.

Snapshot safety (design §7 / API doc §6 / R9.2) is asserted directly: the
synthetic waveform is deterministic (byte-identical across runs), so the build
step generates no per-MicroVM-unique material.
"""

from __future__ import annotations

import dataclasses
import logging
import struct
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from asr_protocol import Config
from asr_server import ws_server
from asr_server.offline_recognizer import (
    OfflineModelConfig,
    OfflineResult,
    SherpaOfflineRecognizer,
)
from asr_server.recognizer import (
    Event,
    Recognizer,
    SherpaModelConfig,
    SherpaOnlineRecognizer,
)
from asr_server.warmup import (
    OfflineRecognizerFactory,
    RecognizerFactory,
    _parse_args,
    config_from_args,
    run,
    synthetic_pcm,
    warmup,
)

# Reuse the scripted fake backends the recogniser tests exercise.
from tests.test_offline_recognizer import ScriptedOfflineBackend, always_speech_gate
from tests.test_recognizer import ScriptedBackend

SAMPLE_RATE = 16000


# --- Helpers ----------------------------------------------------------------


def _factory(script: Sequence[tuple[str, bool]]) -> RecognizerFactory:
    """A recognizer factory that ignores config and returns a scripted engine."""

    def make(_config: SherpaModelConfig) -> Recognizer:
        return SherpaOnlineRecognizer(ScriptedBackend(list(script)), sample_rate=SAMPLE_RATE)

    return make


def _offline_factory(decodes: Sequence[tuple[str, str]]) -> OfflineRecognizerFactory:
    """An offline factory returning a scripted VAD-segmented engine (no weights).

    ``decodes`` are (segment_text -> hypothesis) pairs; the VAD always reports
    speech, so the synthetic warmup tone segments and decodes at least once.
    """

    def make(_config: OfflineModelConfig) -> Recognizer:
        results = [OfflineResult(text=hyp) for _seg, hyp in decodes]
        return SherpaOfflineRecognizer(
            always_speech_gate(),
            ScriptedOfflineBackend(results),
            sample_rate=SAMPLE_RATE,
        )

    return make


class _CountingRecognizer(Recognizer):
    """Minimal recogniser recording exactly how much PCM it was fed."""

    def __init__(self) -> None:
        self.total_bytes = 0
        self.accept_calls = 0
        self.flush_calls = 0

    def accept_pcm(self, pcm: bytes) -> list[Event]:
        self.accept_calls += 1
        self.total_bytes += len(pcm)
        return []

    def flush(self) -> list[Event]:
        self.flush_calls += 1
        return []


# --- synthetic_pcm ----------------------------------------------------------


def test_synthetic_pcm_is_deterministic_snapshot_safe() -> None:
    """R9.2 / API doc §6: build-time audio must be byte-identical across runs."""
    a = synthetic_pcm(500, SAMPLE_RATE)
    b = synthetic_pcm(500, SAMPLE_RATE)
    assert a == b  # no RNG, no per-run uniqueness


def test_synthetic_pcm_shape_and_encoding() -> None:
    """500 ms @ 16 kHz mono 16-bit = 8000 samples = 16000 bytes, all in range."""
    pcm = synthetic_pcm(500, SAMPLE_RATE)
    assert len(pcm) == SAMPLE_RATE // 2 * 2  # 8000 samples * 2 bytes
    assert len(pcm) % 2 == 0
    samples = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
    assert all(-32768 <= s <= 32767 for s in samples)
    assert any(s != 0 for s in samples)  # a tone, not silence


def test_synthetic_pcm_rejects_nonpositive() -> None:
    with pytest.raises(ValueError, match="duration_ms"):
        synthetic_pcm(0, SAMPLE_RATE)
    with pytest.raises(ValueError, match="sample_rate"):
        synthetic_pcm(500, 0)


# --- warmup drives the decode path ------------------------------------------


def test_warmup_feeds_full_buffer_in_chunks_then_flushes() -> None:
    rec = _CountingRecognizer()
    warmup(rec, duration_ms=500, sample_rate=SAMPLE_RATE, chunk_ms=20)

    # 500 ms / 20 ms = 25 chunks, each 320 samples * 2 bytes = 640 bytes.
    assert rec.accept_calls == 25
    assert rec.flush_calls == 1
    assert rec.total_bytes == len(synthetic_pcm(500, SAMPLE_RATE))


def test_warmup_returns_event_count() -> None:
    """A scripted engine yields a partial then a final on the tone → events counted."""
    rec = SherpaOnlineRecognizer(
        ScriptedBackend([("warm", False), ("warm up", True)]),
        sample_rate=SAMPLE_RATE,
    )
    events = warmup(rec, duration_ms=100, sample_rate=SAMPLE_RATE, chunk_ms=20)
    assert events >= 1  # decode path ran and produced events


# --- run() CLI body ---------------------------------------------------------


def test_run_exits_zero_and_logs_warm(caplog: pytest.LogCaptureFixture) -> None:
    """Acceptance: run exits 0 and the log confirms the model warm."""
    with caplog.at_level(logging.INFO, logger="asr_server.warmup"):
        code = run(
            ["--model", "/opt/models", "--duration-ms", "100"],
            recognizer_factory=_factory([("hello", True)]),
        )
    assert code == 0
    assert any("warm" in r.getMessage().lower() for r in caplog.records)


def test_run_returns_nonzero_when_model_load_fails() -> None:
    """A failing factory (e.g. missing wheel / weights) fails the build (exit 1)."""

    def boom(_config: SherpaModelConfig) -> Recognizer:
        raise RuntimeError("sherpa-onnx is not installed")

    code = run(["--model", "/opt/models"], recognizer_factory=boom)
    assert code == 1


def test_run_warms_accurate_engine_via_offline_factory() -> None:
    """The accurate engine warms through the offline factory.

    Uses an injected offline factory (a scripted offline recogniser) so the
    build-time decode path runs with no weights, and asserts the STREAMING factory
    is never touched for ``--engine accurate``.
    """
    streaming_calls: list[object] = []

    def _streaming(_config: SherpaModelConfig) -> Recognizer:
        streaming_calls.append(_config)
        raise AssertionError("streaming factory must not be used for --engine accurate")

    code = run(
        ["--model", "/opt/models", "--engine", "accurate", "--duration-ms", "100"],
        recognizer_factory=_streaming,
        offline_recognizer_factory=_offline_factory([("hi there", "hi there")]),
    )
    assert code == 0
    assert streaming_calls == []


def test_run_accurate_engine_nonzero_when_offline_load_fails() -> None:
    """A failing offline factory (missing wheel/weights) fails the build (exit 1)."""

    def boom(_config: OfflineModelConfig) -> Recognizer:
        raise RuntimeError("sherpa-onnx is not installed")

    code = run(
        ["--model", "/opt/models", "--engine", "accurate"],
        offline_recognizer_factory=boom,
    )
    assert code == 1


# --- config resolution (NFR5) -----------------------------------------------


def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ASR_MODEL_DIR",
        "ASR_MODEL_TOKENS",
        "ASR_MODEL_ENCODER",
        "ASR_MODEL_DECODER",
        "ASR_MODEL_JOINER",
        "ASR_NUM_THREADS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_config_from_args_derives_files_from_model_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    cfg = config_from_args(_parse_args(["--model", "/opt/models", "--num-threads", "3"]))
    assert cfg.tokens == Path("/opt/models/tokens.txt")
    assert cfg.encoder == Path("/opt/models/encoder.onnx")
    assert cfg.decoder == Path("/opt/models/decoder.onnx")
    assert cfg.joiner == Path("/opt/models/joiner.onnx")
    assert cfg.num_threads == 3


def test_config_from_args_env_overrides_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("ASR_MODEL_ENCODER", "/custom/enc.int8.onnx")
    cfg = config_from_args(_parse_args(["--model", "/opt/models"]))
    assert cfg.encoder == Path("/custom/enc.int8.onnx")
    # Non-overridden files still derive from the model dir.
    assert cfg.tokens == Path("/opt/models/tokens.txt")


def test_config_from_args_cli_flag_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("ASR_MODEL_ENCODER", "/from/env.onnx")
    cfg = config_from_args(_parse_args(["--model", "/opt/models", "--encoder", "/from/cli.onnx"]))
    assert cfg.encoder == Path("/from/cli.onnx")


# --- parity with the server's recogniser construction (reviewer issue #1) ----


def test_warmup_num_threads_default_matches_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """num_threads must default to $ASR_NUM_THREADS or 1 — NOT os.cpu_count()."""
    _clear_model_env(monkeypatch)
    cfg = config_from_args(_parse_args([]))
    assert cfg.num_threads == 1

    monkeypatch.setenv("ASR_NUM_THREADS", "4")
    cfg_env = config_from_args(_parse_args([]))
    assert cfg_env.num_threads == 4


def test_warmup_config_matches_server_default_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot parity: warmup builds the SAME config the server does at startup.

    Both go through ``recognizer.build_model_config``; assert the warmup CLI
    default and the server engine factory's config are field-for-field identical.
    The server now builds its shared engine ONCE at startup via
    ``create_sherpa_engine(build_model_config())`` (no per-connection config), which
    must still match what the build-time warmup warmed the snapshot with.
    """
    _clear_model_env(monkeypatch)

    # Capture the config the server's startup engine factory hands to the engine.
    captured: list[SherpaModelConfig] = []

    def _capture(config: SherpaModelConfig) -> object:
        captured.append(config)
        return object()

    monkeypatch.setattr(ws_server, "create_sherpa_engine", _capture)
    ws_server._default_engine_factory()
    server_cfg = captured[0]

    warmup_cfg = config_from_args(_parse_args([]))

    # Whole-object equality: SherpaModelConfig is a dataclass, so ``==`` compares
    # EVERY field (feature_dim, provider, decoding_method, enable_endpoint_detection,
    # rule1/rule2/rule3, model_files, …). This makes ANY future field drift between
    # the warmup and the server factory fail here — not just the handful below.
    assert warmup_cfg == server_cfg
    assert dataclasses.asdict(warmup_cfg) == dataclasses.asdict(server_cfg)

    # Spot-check the fields the reviewer called out explicitly, for a readable
    # failure if the whole-object assert ever trips.
    for field in ("tokens", "encoder", "decoder", "joiner", "sample_rate", "num_threads"):
        assert getattr(warmup_cfg, field) == getattr(server_cfg, field), field
    # endpointing_ms → rule2 mapping is identical (both use Config's default).
    assert warmup_cfg.rule2_min_trailing_silence == server_cfg.rule2_min_trailing_silence
    assert warmup_cfg.rule2_min_trailing_silence == Config().endpointing_ms / 1000.0


def test_warmup_endpointing_ms_maps_to_rule2(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    cfg = config_from_args(_parse_args(["--endpointing-ms", "800"]))
    assert cfg.rule2_min_trailing_silence == pytest.approx(0.8)


# --- import inertness + fail-closed CLI -------------------------------------


def test_module_import_is_dependency_free() -> None:
    """Importing warmup must not require ``sherpa_onnx`` or ``numpy`` (lazy backend).

    Mirrors the recogniser's inertness test: the build tooling imports the
    module before the ARM inference wheels exist.
    """
    code = (
        "import sys; "
        "sys.modules['sherpa_onnx'] = None; "
        "sys.modules['numpy'] = None; "
        "import asr_server.warmup as w; "
        "assert hasattr(w, 'warmup') and hasattr(w, 'run'); "
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_cli_fails_closed_without_wheel_via_default_factory() -> None:
    """The warmup CLI with no sherpa-onnx wheel exits non-zero, not a crash.

    Drives the REAL default factory (``_default_recognizer_factory`` →
    ``SherpaOnlineRecognizer.from_model`` → ``create_sherpa_backend`` →
    ``_load_sherpa``) in a subprocess so the CLI exit-code path is exercised. The
    subprocess pre-binds ``sys.modules['sherpa_onnx'] = None`` so importing the
    wheel raises ``ModuleNotFoundError`` — the exact absent-wheel condition —
    regardless of whether ``sherpa_onnx`` is installed in the test environment
    (previously this test SKIPPED when the wheel was present, leaving the CLI
    fail-closed contract unverified).
    """
    # Simulate the absent wheel (sys.modules[...] = None → ModuleNotFoundError on
    # import), then run the CLI body and propagate its exit code to the process.
    code = (
        "import sys; "
        "sys.modules['sherpa_onnx'] = None; "
        "from asr_server.warmup import run; "
        "sys.exit(run(['--model', '/nonexistent-model', '--duration-ms', '50']))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode == 1
    # The fail-closed path logged the actionable cause (streamed to stderr).
    combined = (result.stderr + result.stdout).lower()
    assert "warmup failed" in combined
    assert "sherpa-onnx is not installed" in combined


def test_warmup_fails_closed_when_wheel_present_but_model_files_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wheel available but model files absent → fail via ``_require_model_files``.

    Complements the missing-*wheel* case above. Drives the REAL default factory
    (``_default_recognizer_factory`` → ``SherpaOnlineRecognizer.from_model`` →
    ``create_sherpa_backend``), stubbing only ``_load_sherpa`` so the wheel
    appears installed. With the model dir pointing at a nonexistent path, the
    run must exit non-zero, surface the actionable "missing model files" message,
    and emit NO false "model resident and warm" success log.
    """
    import asr_server.recognizer as recognizer

    _clear_model_env(monkeypatch)
    # Simulate the aarch64 wheel being importable so control reaches the
    # model-file existence gate rather than the missing-wheel branch.
    monkeypatch.setattr(recognizer, "_load_sherpa", lambda: object())

    with caplog.at_level(logging.INFO, logger="asr_server.warmup"):
        # No recognizer_factory override → exercises the real production path.
        code = run(["--model", "/nonexistent-model", "--duration-ms", "50"])

    assert code == 1
    # Actionable message names the missing files (via the logged traceback).
    assert "ASR model files are missing" in caplog.text
    assert "warmup failed" in caplog.text.lower()
    # No false success: the "warm" completion log must NOT have fired.
    assert "model resident and warm" not in caplog.text
    assert not any(
        "resident and warm" in r.getMessage() for r in caplog.records
    )
