"""Unit tests for the VAD-segmented offline recognizer.

The ``accurate`` engine is an **offline** transducer run **VAD-segmented** so it
emits **one ``final`` per utterance** (R4.4), plus — because R4.4 permits it — a
single synthetic ``partial`` at each segment close. Because the runtime fails closed
(no model weights, no ``sherpa_onnx`` / ``onnxruntime`` / ``numpy`` wheels), these
tests drive :class:`SherpaOfflineRecognizer` through a real
:class:`~asr_server.vad.VadGate` backed by a scripted VAD backend (reused from the
VAD tests) plus a scripted :class:`OfflineDecoderBackend` — no weights needed, the
interface is identical to the real sherpa-onnx offline recogniser.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from asr_server.offline_recognizer import (
    OfflineDecoderBackend,
    OfflineModelConfig,
    OfflineResult,
    SherpaOfflineEngine,
    SherpaOfflineRecognizer,
    _require_offline_model_files,
    build_offline_model_config,
    create_sherpa_offline_backend,
)
from asr_server.recognizer import (
    Event,
    Recognizer,
    SessionConfig,
    SessionConfigError,
    WordTiming,
)
from asr_server.vad import SileroVadConfig, VadGate

# Reuse the scripted fake VAD backend the VAD tests exercise.
from tests.test_vad import ScriptedVadBackend

SAMPLE_RATE = 16000
FRAME_MS = 32
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 512 samples / 1024 bytes


# --- Scripted fake offline backend ------------------------------------------


class ScriptedOfflineBackend(OfflineDecoderBackend):
    """A fake :class:`OfflineDecoderBackend` returning pre-scripted decodes.

    ``results`` is consumed one :class:`OfflineResult` per :meth:`decode` call.
    Once exhausted an empty result sticks. Records every call's sample payload so
    a test can assert the exact samples a VAD segment handed to the decoder.
    """

    def __init__(self, results: Sequence[OfflineResult]) -> None:
        self._results = list(results)
        self._idx = -1
        self.calls: list[list[float]] = []
        self.sample_rates: list[int] = []

    def decode(self, sample_rate: int, samples: Sequence[float]) -> OfflineResult:
        self.calls.append(list(samples))
        self.sample_rates.append(sample_rate)
        self._idx += 1
        if self._idx < len(self._results):
            return self._results[self._idx]
        return OfflineResult(text="")


def _protocol_conformance_check() -> OfflineDecoderBackend:
    return ScriptedOfflineBackend([])


# --- Helpers ----------------------------------------------------------------


def _frames(pcm_frames: int, fill: int = 1234) -> bytes:
    """A whole number of frames of constant PCM (fill lets a test tag segments)."""
    n = FRAME_SAMPLES * pcm_frames
    return struct.pack("<" + "h" * n, *([fill] * n))


def _gate(probs: Sequence[float], **kwargs: object) -> VadGate:
    """A real VadGate driven by a scripted backend (32 ms frames)."""
    return VadGate(
        ScriptedVadBackend(probs),
        sample_rate=SAMPLE_RATE,
        frame_ms=FRAME_MS,
        **kwargs,  # type: ignore[arg-type]
    )


def always_speech_gate() -> VadGate:
    """A gate that reports speech every frame (segments close only on flush).

    ``min_speech_ms=0`` confirms the segment on the first frame so even a short
    synthetic warmup tone produces one segment to decode.
    """
    return _gate([0.9], min_speech_ms=0, min_silence_ms=0, speech_pad_ms=0)


def _clean_gate(probs: Sequence[float]) -> VadGate:
    """A gate with zeroed rules/padding so segment spans map to exact frames."""
    return _gate(probs, min_speech_ms=0, min_silence_ms=0, speech_pad_ms=0)


# --- Interface --------------------------------------------------------------


def test_is_recognizer_subclass() -> None:
    assert issubclass(SherpaOfflineRecognizer, Recognizer)


def test_scripted_offline_backend_satisfies_interface() -> None:
    backend = _protocol_conformance_check()
    assert isinstance(backend, OfflineDecoderBackend)


def test_invalid_sample_rate_rejected() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        SherpaOfflineRecognizer(always_speech_gate(), ScriptedOfflineBackend([]), sample_rate=0)


# --- Core: final-per-utterance (R4.4) ---------------------------------------


def test_final_per_utterance_with_synthetic_partial() -> None:
    """Core acceptance: two VAD segments → two finals, each preceded by a partial.

    speech/silence/speech/silence over four 32 ms frames closes two utterances;
    each is decoded offline and emitted as a monotonic ``final`` (R4.4), with the
    permitted single synthetic ``partial`` at segment close carrying the same text.
    """
    gate = _clean_gate([0.9, 0.0, 0.9, 0.0])
    backend = ScriptedOfflineBackend(
        [OfflineResult(text="first utterance"), OfflineResult(text="second utterance")]
    )
    rec = SherpaOfflineRecognizer(gate, backend, sample_rate=SAMPLE_RATE)

    events: list[Event] = []
    for i in range(4):
        events.extend(rec.accept_pcm(_frames(1, fill=100 + i)))

    assert [(e.kind, e.segment, e.text) for e in events] == [
        ("partial", 0, "first utterance"),
        ("final", 0, "first utterance"),
        ("partial", 1, "second utterance"),
        ("final", 1, "second utterance"),
    ]
    # Two utterances decoded offline, each exactly one 512-sample frame.
    assert len(backend.calls) == 2
    assert all(len(c) == FRAME_SAMPLES for c in backend.calls)
    assert backend.sample_rates == [SAMPLE_RATE, SAMPLE_RATE]


def test_final_start_end_span_the_segment() -> None:
    """The ``final`` start/end match the VAD segment span (real audio, end > start)."""
    gate = _clean_gate([0.9, 0.0])
    backend = ScriptedOfflineBackend([OfflineResult(text="hello")])
    rec = SherpaOfflineRecognizer(gate, backend, sample_rate=SAMPLE_RATE)

    events: list[Event] = []
    for _ in range(2):
        events.extend(rec.accept_pcm(_frames(1)))

    final = next(e for e in events if e.kind == "final")
    frame_s = FRAME_MS / 1000.0
    assert final.start is not None and final.end is not None
    assert final.start == pytest.approx(0.0, abs=1e-6)
    assert final.end == pytest.approx(frame_s, abs=1e-6)  # closes at the 2nd frame's start
    assert final.end > final.start


def test_emit_segment_partial_false_suppresses_partial() -> None:
    """With ``emit_segment_partial=False`` only the ``final`` is emitted (still R4.4)."""
    gate = _clean_gate([0.9, 0.0])
    backend = ScriptedOfflineBackend([OfflineResult(text="only final")])
    rec = SherpaOfflineRecognizer(
        gate, backend, sample_rate=SAMPLE_RATE, emit_segment_partial=False
    )

    events: list[Event] = []
    for _ in range(2):
        events.extend(rec.accept_pcm(_frames(1)))

    assert [e.kind for e in events] == ["final"]
    assert events[0].text == "only final"


def test_silence_produces_no_events() -> None:
    """Silence yields no VAD segment, so neither partial nor final (R5.1)."""
    gate = _clean_gate([0.0, 0.0, 0.0])
    backend = ScriptedOfflineBackend([OfflineResult(text="should-not-decode")])
    rec = SherpaOfflineRecognizer(gate, backend, sample_rate=SAMPLE_RATE)

    events: list[Event] = []
    for _ in range(3):
        events.extend(rec.accept_pcm(_frames(1)))
    assert events == []
    assert backend.calls == []  # decoder never invoked on silence


def test_flush_finalizes_open_segment() -> None:
    """Speech still open at end-of-audio is decoded on flush (utterance not dropped)."""
    gate = always_speech_gate()
    backend = ScriptedOfflineBackend([OfflineResult(text="trailing words")])
    rec = SherpaOfflineRecognizer(gate, backend, sample_rate=SAMPLE_RATE)

    n_frames = 3
    accept_events: list[Event] = []
    for _ in range(n_frames):
        accept_events.extend(rec.accept_pcm(_frames(1)))
    assert accept_events == []  # segment still open, nothing decoded yet

    flush_events = rec.flush()
    finals = [e for e in flush_events if e.kind == "final"]
    assert len(finals) == 1
    assert finals[0].text == "trailing words"
    assert finals[0].segment == 0
    # Flush closes at the last processed sample: end == total audio consumed.
    assert finals[0].end == pytest.approx(n_frames * FRAME_MS / 1000.0, abs=1e-6)
    assert len(backend.calls) == 1
    assert len(backend.calls[0]) == n_frames * FRAME_SAMPLES


def test_empty_decode_is_suppressed_and_consumes_no_segment() -> None:
    """A segment the decoder returns empty for emits nothing and skips no segment.

    Mirrors the streaming path's empty-final suppression: a non-speech blip that
    tripped the VAD but decodes to "" must not emit an empty final nor burn a
    segment number, so the next real utterance is segment 0.
    """
    gate = _clean_gate([0.9, 0.0, 0.9, 0.0])
    backend = ScriptedOfflineBackend(
        [OfflineResult(text="   "), OfflineResult(text="real words")]
    )
    rec = SherpaOfflineRecognizer(gate, backend, sample_rate=SAMPLE_RATE)

    events: list[Event] = []
    for _ in range(4):
        events.extend(rec.accept_pcm(_frames(1)))

    # First (empty) segment produced nothing; the real one is segment 0.
    assert [(e.kind, e.segment, e.text) for e in events] == [
        ("partial", 0, "real words"),
        ("final", 0, "real words"),
    ]
    assert len(backend.calls) == 2  # decoder was still called for both segments


def test_correct_samples_handed_to_decoder() -> None:
    """The decoder receives exactly the segment's own samples, not the whole buffer."""
    gate = _clean_gate([0.9, 0.0, 0.9, 0.0])
    backend = ScriptedOfflineBackend(
        [OfflineResult(text="a"), OfflineResult(text="b")]
    )
    rec = SherpaOfflineRecognizer(gate, backend, sample_rate=SAMPLE_RATE)

    # Distinct fill per frame so the extracted segment samples are identifiable.
    fills = [111, 0, 333, 0]
    for i in range(4):
        rec.accept_pcm(_frames(1, fill=fills[i]))

    assert len(backend.calls) == 2
    # Segment 1 = frame 0 (fill 111); segment 2 = frame 2 (fill 333).
    assert backend.calls[0][0] == pytest.approx(111 / 32768.0, abs=1e-6)
    assert backend.calls[1][0] == pytest.approx(333 / 32768.0, abs=1e-6)
    assert all(len(c) == FRAME_SAMPLES for c in backend.calls)


def test_word_timings_offset_to_absolute_timeline() -> None:
    """Segment-relative word timings are offset onto the absolute session timeline."""
    # Segment 2 begins at t = 2 frames; its (0-based) word times must be shifted.
    gate = _clean_gate([0.9, 0.0, 0.9, 0.0])
    seg2_words = [WordTiming(w="hello", s=0.0, e=0.01), WordTiming(w="world", s=0.01, e=0.02)]
    backend = ScriptedOfflineBackend(
        [OfflineResult(text="first"), OfflineResult(text="hello world", words=seg2_words)]
    )
    rec = SherpaOfflineRecognizer(gate, backend, sample_rate=SAMPLE_RATE)

    events: list[Event] = []
    for _ in range(4):
        events.extend(rec.accept_pcm(_frames(1)))

    seg2_final = [e for e in events if e.kind == "final" and e.segment == 1][0]
    seg_start = 2 * FRAME_MS / 1000.0  # 0.064
    assert seg2_final.words is not None
    assert seg2_final.words[0] == WordTiming(w="hello", s=seg_start, e=seg_start + 0.01)
    assert seg2_final.words[1] == WordTiming(w="world", s=seg_start + 0.01, e=seg_start + 0.02)


# --- PCM handling -----------------------------------------------------------


def test_odd_length_pcm_rejected() -> None:
    rec = SherpaOfflineRecognizer(always_speech_gate(), ScriptedOfflineBackend([]))
    with pytest.raises(ValueError, match="even"):
        rec.accept_pcm(b"\x00\x00\x00")


def test_empty_pcm_chunk_is_noop() -> None:
    backend = ScriptedOfflineBackend([OfflineResult(text="x")])
    rec = SherpaOfflineRecognizer(always_speech_gate(), backend)
    assert rec.accept_pcm(b"") == []
    assert backend.calls == []


# --- Config resolution (NFR5) -----------------------------------------------


def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ASR_MODEL_DIR",
        "ASR_MODEL_TOKENS",
        "ASR_MODEL_ENCODER",
        "ASR_MODEL_DECODER",
        "ASR_MODEL_JOINER",
        "ASR_VAD_MODEL",
        "ASR_NUM_THREADS",
    ):
        monkeypatch.delenv(var, raising=False)


def test_build_offline_config_derives_files_from_model_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    cfg = build_offline_model_config(model_dir="/opt/models", num_threads=2)
    assert cfg.tokens == Path("/opt/models/tokens.txt")
    assert cfg.encoder == Path("/opt/models/encoder.onnx")
    assert cfg.decoder == Path("/opt/models/decoder.onnx")
    assert cfg.joiner == Path("/opt/models/joiner.onnx")
    assert cfg.vad.model == Path("/opt/models/silero_vad.onnx")
    assert cfg.num_threads == 2
    assert cfg.vad.num_threads == 2


def test_build_offline_config_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("ASR_MODEL_ENCODER", "/custom/enc.int8.onnx")
    monkeypatch.setenv("ASR_VAD_MODEL", "/custom/vad.onnx")
    cfg = build_offline_model_config(model_dir="/opt/models")
    assert cfg.encoder == Path("/custom/enc.int8.onnx")
    assert cfg.vad.model == Path("/custom/vad.onnx")
    # Non-overridden files still derive from the model dir.
    assert cfg.tokens == Path("/opt/models/tokens.txt")


def test_build_offline_config_cli_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("ASR_VAD_MODEL", "/from/env.onnx")
    cfg = build_offline_model_config(model_dir="/opt/models", vad_model="/from/cli.onnx")
    assert cfg.vad.model == Path("/from/cli.onnx")


def test_build_offline_config_num_threads_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    assert build_offline_model_config(model_dir="/opt/models").num_threads == 1
    monkeypatch.setenv("ASR_NUM_THREADS", "4")
    assert build_offline_model_config(model_dir="/opt/models").num_threads == 4


def test_build_offline_config_endpointing_maps_to_vad_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``endpointing_ms`` drives the VAD trailing-silence (how a pause closes a segment)."""
    _clear_model_env(monkeypatch)
    cfg = build_offline_model_config(model_dir="/opt/models", endpointing_ms=800)
    assert cfg.vad.min_silence_ms == 800


# --- Shared engine / per-session negotiated config --------------------------


def _offline_engine(
    *, sample_rate: int = SAMPLE_RATE, min_silence_ms: int = 100
) -> SherpaOfflineEngine:
    """A ``SherpaOfflineEngine`` over stand-in model/VAD objects (no weights).

    ``new_session`` builds a per-session ``VadGate`` (pure Python, backed by a
    numpy-only Silero wrapper) and a decode backend over the shared objects, but
    never invokes the ONNX graph, so plain ``object()`` stand-ins suffice to
    exercise the config-threading behaviour.
    """
    cfg = OfflineModelConfig(
        tokens=Path("tokens.txt"),
        encoder=Path("encoder.onnx"),
        decoder=Path("decoder.onnx"),
        joiner=Path("joiner.onnx"),
        vad=SileroVadConfig(model=Path("silero_vad.onnx"), min_silence_ms=min_silence_ms),
        sample_rate=sample_rate,
    )
    return SherpaOfflineEngine(object(), object(), cfg)


def test_offline_new_session_applies_negotiated_endpointing_to_vad() -> None:
    """The negotiated ``endpointing_ms`` reaches this session's VAD gate.

    For the offline engine, endpointing lives only in the per-session ``VadGate``
    (``min_silence_ms``), so it CAN vary per session against the shared engine.
    Thread a :class:`SessionConfig` with a distinct ``endpointing_ms`` and assert
    the per-session gate uses it — not the engine's build-time default.

    Teeth: under the pre-fix wiring ``new_session`` took no config, so every gate
    used the startup default (100 ms here); this assertion fails there (600 != 100)
    and passes with the fix.
    """
    engine = _offline_engine(min_silence_ms=100)
    rec = engine.new_session(SessionConfig(sample_rate=SAMPLE_RATE, endpointing_ms=600))
    assert isinstance(rec, SherpaOfflineRecognizer)
    # The per-session gate's trailing-silence reflects the negotiated 600 ms.
    assert rec._vad._min_silence_s == pytest.approx(0.6)


def test_offline_new_session_defaults_to_engine_endpointing_when_unset() -> None:
    """``endpointing_ms=None`` leaves the engine's build-time VAD silence in place."""
    engine = _offline_engine(min_silence_ms=250)
    rec = engine.new_session(SessionConfig(sample_rate=SAMPLE_RATE, endpointing_ms=None))
    assert isinstance(rec, SherpaOfflineRecognizer)
    assert rec._vad._min_silence_s == pytest.approx(0.25)
    # No config at all behaves the same (standalone / test use).
    rec2 = engine.new_session()
    assert isinstance(rec2, SherpaOfflineRecognizer)
    assert rec2._vad._min_silence_s == pytest.approx(0.25)


def test_offline_new_session_rejects_incompatible_sample_rate() -> None:
    """A sample_rate the shared graph/VAD can't honour is rejected, not ignored.

    ``sample_rate`` is baked into the shared offline ONNX graph and the shared
    Silero session, so it cannot vary per session; the engine must raise
    :class:`SessionConfigError` (→ fatal BAD_CONFIG) rather than segment/decode at
    the wrong rate.
    """
    engine = _offline_engine(sample_rate=SAMPLE_RATE)
    with pytest.raises(SessionConfigError, match="sample_rate"):
        engine.new_session(SessionConfig(sample_rate=8000))


# --- Fail-closed / lazy-import contract -------------------------------------


def test_require_offline_model_files_fails_when_missing(tmp_path: Path) -> None:
    """Missing transducer/VAD files fail with a clear, file-naming error."""
    cfg = OfflineModelConfig(
        tokens=tmp_path / "tokens.txt",
        encoder=tmp_path / "encoder.onnx",
        decoder=tmp_path / "decoder.onnx",
        joiner=tmp_path / "joiner.onnx",
        vad=SileroVadConfig(model=tmp_path / "silero_vad.onnx"),
    )
    with pytest.raises(RuntimeError, match="ASR model files are missing") as exc:
        _require_offline_model_files(cfg)
    # The Silero VAD model is checked alongside the transducer files.
    assert "vad" in str(exc.value)


def test_require_offline_model_files_passes_when_all_present(tmp_path: Path) -> None:
    for name in ("tokens.txt", "encoder.onnx", "decoder.onnx", "joiner.onnx", "silero_vad.onnx"):
        (tmp_path / name).write_bytes(b"stub")
    cfg = OfflineModelConfig(
        tokens=tmp_path / "tokens.txt",
        encoder=tmp_path / "encoder.onnx",
        decoder=tmp_path / "decoder.onnx",
        joiner=tmp_path / "joiner.onnx",
        vad=SileroVadConfig(model=tmp_path / "silero_vad.onnx"),
    )
    _require_offline_model_files(cfg)  # must not raise


def test_create_offline_backend_fails_closed_without_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the sherpa-onnx wheel, constructing a real backend fails cleanly.

    Stubs ``_load_sherpa`` to simulate the missing wheel so the fail-closed path
    ACTUALLY runs even when ``sherpa_onnx`` is installed (previously it skipped,
    leaving the offline engine's fail-closed contract unverified).
    """
    import asr_server.offline_recognizer as offline_mod

    def _missing_wheel() -> None:
        raise RuntimeError(
            "sherpa-onnx is not installed. Install the aarch64 wheel pinned in "
            "requirements.txt to construct a real offline recogniser."
        )

    monkeypatch.setattr(offline_mod, "_load_sherpa", _missing_wheel)
    cfg = OfflineModelConfig(
        tokens=tmp_path / "tokens.txt",
        encoder=tmp_path / "encoder.onnx",
        decoder=tmp_path / "decoder.onnx",
        joiner=tmp_path / "joiner.onnx",
        vad=SileroVadConfig(model=tmp_path / "silero_vad.onnx"),
    )
    with pytest.raises(RuntimeError, match="sherpa-onnx is not installed"):
        create_sherpa_offline_backend(cfg)


def test_module_import_is_dependency_free() -> None:
    """Importing the module must not require ``sherpa_onnx``, ``onnxruntime`` or ``numpy``.

    Proves the *import* itself is inert (lazy backend loading), so the module can
    be imported inside the MicroVM/tooling before the ARM inference wheels exist.
    """
    code = (
        "import sys; "
        "sys.modules['sherpa_onnx'] = None; "
        "sys.modules['onnxruntime'] = None; "
        "sys.modules['numpy'] = None; "
        "import asr_server.offline_recognizer as o; "
        "assert hasattr(o, 'SherpaOfflineRecognizer'); "
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
