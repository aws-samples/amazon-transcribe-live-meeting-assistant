"""Unit tests for the streaming recognizer wrapper.

The acceptance check feeds a 16 kHz/16-bit WAV in 20 ms chunks and asserts that
evolving partials appear, followed by a final. Because the runtime fails closed (no
model weights, no ``sherpa_onnx`` wheel), these tests drive
:class:`SherpaOnlineRecognizer` through a scripted fake :class:`DecoderBackend`
instead of a real engine — the interface is identical.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import wave
from array import array
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from asr_server.recognizer import (
    DecoderBackend,
    Event,
    Recognizer,
    RecognizerEngine,
    SessionConfig,
    SessionConfigError,
    SherpaModelConfig,
    SherpaOnlineEngine,
    SherpaOnlineRecognizer,
    WordTiming,
    _require_model_files,
    _SherpaBackend,
    create_sherpa_backend,
    create_sherpa_engine,
)

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000  # 320 samples / 640 bytes


# --- Scripted fake backend --------------------------------------------------


class ScriptedBackend:
    """A fake :class:`DecoderBackend` whose hypothesis grows per decode call.

    ``script`` is a list of (text, is_endpoint) pairs consumed one per
    ``decode()``. Once exhausted the last text sticks and no further endpoints
    fire (models the tail before ``flush``). This lets a test express an exact
    partial/final trajectory without any model weights.
    """

    def __init__(self, script: Sequence[tuple[str, bool]]) -> None:
        self._script = list(script)
        self._idx = -1
        self._text = ""
        self._endpoint = False
        self.samples_seen = 0
        self.last_samples: list[float] = []
        self.input_finished_called = False
        self.reset_count = 0

    def accept_waveform(self, sample_rate: int, samples: Sequence[float]) -> None:
        self.samples_seen += len(samples)
        self.last_samples = list(samples)

    def decode(self) -> None:
        self._idx += 1
        if self._idx < len(self._script):
            self._text, self._endpoint = self._script[self._idx]
        else:
            self._endpoint = False  # nothing new; hold last text

    def current_text(self) -> str:
        return self._text

    def is_endpoint(self) -> bool:
        return self._endpoint

    def reset(self) -> None:
        self.reset_count += 1
        self._text = ""
        self._endpoint = False

    def input_finished(self) -> None:
        self.input_finished_called = True

    def enqueue(self, step: tuple[str, bool]) -> None:
        """Append a (text, is_endpoint) step, e.g. to reveal a tail at flush."""
        self._script.append(step)


def _protocol_conformance_check() -> DecoderBackend:
    # Purely to assert ScriptedBackend structurally satisfies the Protocol.
    return ScriptedBackend([])


# --- Helpers ----------------------------------------------------------------


def _silence_chunk() -> bytes:
    return b"\x00\x00" * CHUNK_SAMPLES


def _tone_chunk(amplitude: int = 8000) -> bytes:
    # A non-zero chunk; exact contents are irrelevant to the fake backend but
    # exercises the real int16->float conversion path.
    return struct.pack("<" + "h" * CHUNK_SAMPLES, *([amplitude] * CHUNK_SAMPLES))


def _write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)


def feed(recognizer, chunks: int = 1, amplitude: int = 8000):  # noqa: ANN001, ANN202
    """Push ``chunks`` of audio and return every event emitted."""
    events = []
    for _ in range(chunks):
        events.extend(recognizer.accept_pcm(_tone_chunk(amplitude)))
    return events


# --- Tests ------------------------------------------------------------------


def test_recognizer_is_abstract_interface() -> None:
    assert issubclass(SherpaOnlineRecognizer, Recognizer)
    with pytest.raises(TypeError):
        Recognizer()  # type: ignore[abstract]


def test_scripted_backend_satisfies_protocol() -> None:
    backend = _protocol_conformance_check()
    assert isinstance(backend, ScriptedBackend)


def test_evolving_partials_then_final() -> None:
    """Core acceptance: growing partials for a segment, then a final at endpoint."""
    script = [
        ("let", False),
        ("let's", False),
        ("let's meet", False),
        ("let's meet at noon", True),  # endpoint fires -> final
    ]
    backend = ScriptedBackend(script)
    rec = SherpaOnlineRecognizer(backend, sample_rate=SAMPLE_RATE)

    events: list[Event] = []
    for _ in script:
        events.extend(rec.accept_pcm(_tone_chunk()))

    partials = [e for e in events if e.kind == "partial"]
    finals = [e for e in events if e.kind == "final"]

    # Evolving partials: text strictly grows across updates.
    assert [p.text for p in partials] == [
        "let",
        "let's",
        "let's meet",
        "let's meet at noon",
    ]
    assert all(p.segment == 0 for p in partials)

    # Exactly one final, carrying the last hypothesis and an end timestamp.
    assert len(finals) == 1
    assert finals[0].kind == "final"
    assert finals[0].segment == 0
    assert finals[0].text == "let's meet at noon"
    assert finals[0].start == pytest.approx(0.0)
    assert finals[0].end is not None and finals[0].end > 0.0
    assert backend.reset_count == 1


def test_identical_text_is_deduped() -> None:
    """Repeated identical hypotheses do not emit duplicate partials (design §5.3)."""
    script = [("hello", False), ("hello", False), ("hello world", False)]
    backend = ScriptedBackend(script)
    rec = SherpaOnlineRecognizer(backend)

    events: list[Event] = []
    for _ in script:
        events.extend(rec.accept_pcm(_tone_chunk()))

    assert [e.text for e in events if e.kind == "partial"] == ["hello", "hello world"]


def test_segment_counter_is_monotonic_across_utterances() -> None:
    """Each endpoint advances the segment; partials never reuse a finalized one."""
    script = [
        ("one", True),  # segment 0 final
        ("two", True),  # segment 1 final
    ]
    backend = ScriptedBackend(script)
    rec = SherpaOnlineRecognizer(backend)

    events: list[Event] = []
    for _ in script:
        events.extend(rec.accept_pcm(_tone_chunk()))

    finals = [e for e in events if e.kind == "final"]
    assert [f.segment for f in finals] == [0, 1]
    assert [f.text for f in finals] == ["one", "two"]


def test_silence_produces_no_events() -> None:
    """Empty hypotheses (silence) yield neither partials nor finals."""
    backend = ScriptedBackend([("", False), ("", False)])
    rec = SherpaOnlineRecognizer(backend)

    events: list[Event] = []
    for _ in range(2):
        events.extend(rec.accept_pcm(_silence_chunk()))
    assert events == []


def test_flush_finalizes_pending_segment() -> None:
    """A hypothesis with no endpoint is finalized on flush (end-of-audio)."""
    backend = ScriptedBackend([("tail words", False)])
    rec = SherpaOnlineRecognizer(backend)

    accept_events = rec.accept_pcm(_tone_chunk())
    assert [e.kind for e in accept_events] == ["partial"]

    flush_events = rec.flush()
    assert backend.input_finished_called
    assert len(flush_events) == 1
    assert flush_events[0].kind == "final"
    assert flush_events[0].text == "tail words"
    assert flush_events[0].segment == 0


def test_flush_with_nothing_pending_is_empty() -> None:
    backend = ScriptedBackend([])
    rec = SherpaOnlineRecognizer(backend)
    assert rec.flush() == []


def test_flush_eos_only_hypothesis_has_start_before_end() -> None:
    """A hypothesis that first appears only at EOS must not collapse to start == end.

    Reproduces the case where the backend stays empty through every chunk and
    only yields text once ``input_finished`` triggers the tail decode. The final
    must still span the audio that was consumed (end > start).
    """
    # Backend is silent while audio streams; the tail decode (flush) reveals text.
    backend = ScriptedBackend([("", False), ("", False), ("", False)])
    rec = SherpaOnlineRecognizer(backend, sample_rate=SAMPLE_RATE)

    n_chunks = 3
    accept_events: list[Event] = []
    for _ in range(n_chunks):
        accept_events.extend(rec.accept_pcm(_tone_chunk()))
    assert accept_events == []  # nothing emitted mid-stream

    # The tail hypothesis surfaces only when the stream ends (flush's decode).
    backend.enqueue(("okay let's go", False))
    flush_events = rec.flush()
    assert len(flush_events) == 1
    final = flush_events[0]
    assert final.kind == "final"
    assert final.text == "okay let's go"
    assert final.start is not None and final.end is not None
    # The bug: start was pinned to EOS elapsed, collapsing the segment. Enforce
    # the invariant that a non-empty utterance spans real audio.
    assert final.end > final.start
    assert final.start == pytest.approx(0.0, abs=1e-6)  # segment began at audio start
    assert final.end == pytest.approx(n_chunks * CHUNK_MS / 1000.0, abs=1e-6)


def test_second_segment_eos_final_anchors_to_its_own_audio_start() -> None:
    """After an endpoint reset, an EOS-only final anchors to the 2nd segment's start.

    Guards the ``_seg_audio_start`` reset: the second segment's start must be the
    time its audio began, not 0.0 and not the EOS instant.
    """
    # Chunk 1: segment 0 text + endpoint (finalizes, resets).
    # Chunks 2-3: silent; text for segment 1 only surfaces at flush.
    backend = ScriptedBackend([("first", True), ("", False), ("", False)])
    rec = SherpaOnlineRecognizer(backend, sample_rate=SAMPLE_RATE)

    events: list[Event] = []
    for _ in range(3):
        events.extend(rec.accept_pcm(_tone_chunk()))

    # Reveal segment 1's tail hypothesis at flush.
    backend.enqueue(("second thought", False))
    events.extend(rec.flush())

    finals = [e for e in events if e.kind == "final"]
    assert [f.text for f in finals] == ["first", "second thought"]
    assert [f.segment for f in finals] == [0, 1]

    seg1 = finals[1]
    assert seg1.start is not None and seg1.end is not None
    chunk_s = CHUNK_MS / 1000.0
    # Segment 1's audio began at the 2nd chunk (t = 1 chunk), ended at 3 chunks.
    assert seg1.start == pytest.approx(chunk_s, abs=1e-6)
    assert seg1.end == pytest.approx(3 * chunk_s, abs=1e-6)
    assert seg1.end > seg1.start


def test_flush_truly_empty_emits_nothing() -> None:
    """Audio consumed but the backend never produced text -> flush emits no final."""
    backend = ScriptedBackend([("", False), ("", False)])
    rec = SherpaOnlineRecognizer(backend, sample_rate=SAMPLE_RATE)
    for _ in range(2):
        assert rec.accept_pcm(_tone_chunk()) == []
    assert rec.flush() == []


def test_end_to_end_wav_in_20ms_chunks(tmp_path: Path) -> None:
    """Acceptance shape: read a real 16k/16-bit WAV, stream it in 20 ms chunks."""
    # Build a WAV: ~200 ms of tone. The scripted backend reveals text as chunks
    # arrive, so the trajectory below mirrors what a real streaming model does.
    n_chunks = 10
    pcm = _tone_chunk() * n_chunks
    wav_path = tmp_path / "utterance.wav"
    _write_wav(wav_path, pcm)

    # Script one growing token every couple of chunks, endpoint on the last.
    script: list[tuple[str, bool]] = [
        ("", False),
        ("schedule", False),
        ("schedule the", False),
        ("schedule the", False),
        ("schedule the meeting", False),
        ("schedule the meeting", False),
        ("schedule the meeting for", False),
        ("schedule the meeting for", False),
        ("schedule the meeting for friday", False),
        ("schedule the meeting for friday", True),
    ]
    backend = ScriptedBackend(script)
    rec = SherpaOnlineRecognizer(backend, sample_rate=SAMPLE_RATE)

    with wave.open(str(wav_path), "rb") as wav:
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getsampwidth() == 2
        assert wav.getnchannels() == 1
        frames = wav.readframes(wav.getnframes())

    events: list[Event] = []
    for offset in range(0, len(frames), CHUNK_SAMPLES * 2):
        chunk = frames[offset : offset + CHUNK_SAMPLES * 2]
        events.extend(rec.accept_pcm(chunk))
    events.extend(rec.flush())

    partials = [e for e in events if e.kind == "partial"]
    finals = [e for e in events if e.kind == "final"]

    # Partials evolve (strictly lengthen), then exactly one final closes segment 0.
    partial_texts = [p.text for p in partials]
    assert partial_texts == sorted(partial_texts, key=len)
    assert len(partial_texts) >= 3
    assert len(finals) == 1
    assert finals[0].text == "schedule the meeting for friday"
    assert finals[0].segment == 0
    assert finals[0].end == pytest.approx(n_chunks * CHUNK_MS / 1000.0, abs=1e-6)


def test_odd_length_pcm_rejected() -> None:
    rec = SherpaOnlineRecognizer(ScriptedBackend([]))
    with pytest.raises(ValueError, match="even"):
        rec.accept_pcm(b"\x00\x00\x00")  # 3 bytes -> not a whole 16-bit sample


def test_pcm_conversion_normalization_contract() -> None:
    """int16 LE -> float32 normalization: divide by 32768 (full-scale), stay in [-1, 1].

    Proves the exact values the backend receives, not merely that samples flowed.
    """
    backend = ScriptedBackend([("x", False)])
    rec = SherpaOnlineRecognizer(backend)
    # max positive, negative half-scale, zero, and full-scale negative (-1.0).
    pcm = array("h", [32767, -16384, 0, -32768]).tobytes()
    rec.accept_pcm(pcm)

    got = backend.last_samples
    assert len(got) == 4
    assert got[0] == pytest.approx(32767 / 32768, abs=1e-6)  # ~0.99997
    assert got[1] == pytest.approx(-0.5, abs=1e-9)
    assert got[2] == 0.0
    # Contract: normalise by 32768, so -32768 -> exactly -1.0 (the range floor).
    assert got[3] == pytest.approx(-1.0, abs=1e-9)
    # Every produced sample lands within [-1, 1].
    assert all(-1.0 <= s <= 1.0 for s in got)


def test_empty_pcm_chunk_is_noop() -> None:
    backend = ScriptedBackend([("should-not-appear", False)])
    rec = SherpaOnlineRecognizer(backend)
    assert rec.accept_pcm(b"") == []
    assert backend.samples_seen == 0


def test_invalid_sample_rate_rejected() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        SherpaOnlineRecognizer(ScriptedBackend([]), sample_rate=0)


def test_word_timing_is_engine_internal() -> None:
    """WordTiming is a plain engine dataclass, decoupled from the wire Word model."""
    wt = WordTiming(w="noon", s=12.7, e=13.0)
    assert (wt.w, wt.s, wt.e) == ("noon", 12.7, 13.0)
    ev = Event(kind="final", segment=0, text="noon", start=12.7, end=13.0, words=[wt])
    assert ev.words == [wt]


# --- Shared engine / per-session stream split (fix-recognizer-construction-crash) --


class _FakeSherpaOnlineModel:
    """A stand-in for the heavy sherpa-onnx ``OnlineRecognizer`` (no weights).

    Records how many streams it created so a test can assert one engine hands out
    one lightweight stream per session (never a new model per session). Each stream
    is a distinct object so streams can be checked to be independent.
    """

    def __init__(self) -> None:
        self.streams_created = 0

    def create_stream(self) -> object:
        self.streams_created += 1
        return object()


def test_engine_is_recognizer_engine_subclass() -> None:
    assert issubclass(SherpaOnlineEngine, RecognizerEngine)
    with pytest.raises(TypeError):
        RecognizerEngine()  # type: ignore[abstract]


def test_engine_new_session_returns_independent_streams() -> None:
    """One shared model hands out N lightweight per-session recognizers/streams.

    Proves the fix's core: the heavy model (``_FakeSherpaOnlineModel``) is created
    ONCE, and each ``new_session`` allocates only a fresh stream on it — never a new
    model. Under the OLD wiring a full model was built per connection.
    """
    model = _FakeSherpaOnlineModel()
    engine = SherpaOnlineEngine(model, sample_rate=SAMPLE_RATE)

    sessions = [engine.new_session() for _ in range(3)]

    # Each session is a real streaming recogniser, and all share the ONE model.
    assert all(isinstance(s, SherpaOnlineRecognizer) for s in sessions)
    assert model.streams_created == 3  # one stream per session, not a new model
    # Distinct recogniser objects (independent per-session decoder state).
    assert len({id(s) for s in sessions}) == 3


def test_streaming_new_session_accepts_matching_negotiated_config() -> None:
    """A session negotiating the engine's own sample_rate/endpointing is accepted.

    Threads a :class:`SessionConfig` through ``new_session`` (the config the
    handshake negotiated). When it matches what the shared engine was built with,
    a normal per-session stream is handed out — the config actually reaches the
    stream instead of being ignored.
    """
    model = _FakeSherpaOnlineModel()
    engine = SherpaOnlineEngine(model, sample_rate=SAMPLE_RATE, endpointing_ms=1200)
    rec = engine.new_session(SessionConfig(sample_rate=SAMPLE_RATE, endpointing_ms=1200))
    assert isinstance(rec, SherpaOnlineRecognizer)
    assert model.streams_created == 1


def test_streaming_new_session_rejects_incompatible_sample_rate() -> None:
    """A sample_rate the shared ONNX graph can't honour is rejected, not ignored.

    ``sample_rate`` is baked into the shared recognizer at build time, so a session
    negotiating a different rate cannot be decoded against the shared engine. The
    engine must raise :class:`SessionConfigError` (→ fatal BAD_CONFIG at the wire)
    rather than silently decode at the built-in rate — the regression this guards.

    Teeth: under the pre-fix wiring ``new_session`` took no config and always built
    a stream at the startup default, so an 8 kHz session would silently decode at
    16 kHz; this assertion (raises) fails there and passes with the fix.
    """
    model = _FakeSherpaOnlineModel()
    engine = SherpaOnlineEngine(model, sample_rate=SAMPLE_RATE)
    with pytest.raises(SessionConfigError, match="sample_rate"):
        engine.new_session(SessionConfig(sample_rate=8000))
    # No stream was allocated for the rejected session.
    assert model.streams_created == 0


def test_streaming_new_session_rejects_incompatible_endpointing() -> None:
    """Endpointing differing from the baked rule2 is rejected (baked into the graph).

    The streaming endpoint rules are fixed at engine-build time, so a session
    negotiating a *different* ``endpointing_ms`` cannot be honoured against the
    shared engine; reject it rather than echo it in ``ready`` and decode with the
    baked value.
    """
    model = _FakeSherpaOnlineModel()
    engine = SherpaOnlineEngine(model, sample_rate=SAMPLE_RATE, endpointing_ms=1200)
    with pytest.raises(SessionConfigError, match="endpointing"):
        engine.new_session(SessionConfig(sample_rate=SAMPLE_RATE, endpointing_ms=800))
    assert model.streams_created == 0


def test_create_sherpa_engine_bakes_endpointing_for_validation() -> None:
    """``create_sherpa_engine`` records the baked endpointing so it can validate.

    The endpointing baked into rule2 (ms) is derived from the model config so
    ``new_session`` can compare a negotiated value against reality. Build the engine
    with a fake model factory (no wheel) and assert a session at the SAME endpointing
    is accepted while a different one is rejected.
    """
    cfg = SherpaModelConfig(
        tokens=Path("tokens.txt"),
        encoder=Path("encoder.onnx"),
        decoder=Path("decoder.onnx"),
        joiner=Path("joiner.onnx"),
        sample_rate=SAMPLE_RATE,
    )
    cfg.rule2_min_trailing_silence = 1.2  # 1200 ms

    import asr_server.recognizer as rec_mod

    fake_model = _FakeSherpaOnlineModel()
    original = rec_mod._build_online_recognizer

    def fake_build(config: SherpaModelConfig) -> Any:
        _ = config
        return fake_model

    rec_mod._build_online_recognizer = fake_build
    try:
        engine = create_sherpa_engine(cfg)
    finally:
        rec_mod._build_online_recognizer = original

    # Matching endpointing (1200 ms) is honoured; a different value is rejected.
    engine.new_session(SessionConfig(sample_rate=SAMPLE_RATE, endpointing_ms=1200))
    with pytest.raises(SessionConfigError, match="endpointing"):
        engine.new_session(SessionConfig(sample_rate=SAMPLE_RATE, endpointing_ms=600))


def test_engine_rejects_nonpositive_sample_rate() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        SherpaOnlineEngine(_FakeSherpaOnlineModel(), sample_rate=0)


def _stub_missing_wheel() -> None:
    """Stand in for ``_load_sherpa`` when the wheel is *absent*.

    Raises the exact ``RuntimeError`` the real :func:`_load_sherpa` raises on a
    ``ModuleNotFoundError`` so the fail-closed path can be exercised regardless of
    whether ``sherpa_onnx`` happens to be installed in the test environment.
    """
    raise RuntimeError(
        "sherpa-onnx is not installed. Install the aarch64 wheel pinned in "
        "requirements.txt to construct a real streaming recogniser."
    )


def test_create_sherpa_engine_fails_closed_without_wheel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building the shared engine without the sherpa-onnx wheel fails cleanly.

    The startup engine build must fail closed (RuntimeError) so ``serve_asr`` can
    exit non-zero rather than bind an unusable server. Stubs ``_load_sherpa`` to
    simulate the missing wheel so this ACTUALLY runs even when ``sherpa_onnx`` is
    installed (previously it skipped, leaving the fail-closed contract unverified).
    """
    import asr_server.recognizer as rec_mod

    monkeypatch.setattr(rec_mod, "_load_sherpa", _stub_missing_wheel)
    cfg = SherpaModelConfig(
        tokens=Path("tokens.txt"),
        encoder=Path("encoder.onnx"),
        decoder=Path("decoder.onnx"),
        joiner=Path("joiner.onnx"),
    )
    with pytest.raises(RuntimeError, match="sherpa-onnx is not installed"):
        create_sherpa_engine(cfg)


def test_create_sherpa_backend_fails_closed_without_wheel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the sherpa-onnx wheel, constructing a real backend fails cleanly.

    Exercises the fail-closed path unconditionally by stubbing ``_load_sherpa``
    (see :func:`test_create_sherpa_engine_fails_closed_without_wheel`).
    """
    import asr_server.recognizer as rec_mod

    monkeypatch.setattr(rec_mod, "_load_sherpa", _stub_missing_wheel)
    cfg = SherpaModelConfig(
        tokens=Path("tokens.txt"),
        encoder=Path("encoder.onnx"),
        decoder=Path("decoder.onnx"),
        joiner=Path("joiner.onnx"),
    )
    with pytest.raises(RuntimeError, match="sherpa-onnx is not installed"):
        create_sherpa_backend(cfg)


def test_require_model_files_fails_closed_when_missing(tmp_path: Path) -> None:
    """A present wheel but missing model files must fail with a clear error.

    Guards the case the reviewer flagged: previously only the missing-wheel path
    raised cleanly, so a bad ``ASR_MODEL_*`` path / unbuilt model dir would blow
    up opaquely deep inside sherpa-onnx. ``_require_model_files`` surfaces exactly
    which files are missing before the engine is constructed.
    """
    cfg = SherpaModelConfig(
        tokens=tmp_path / "tokens.txt",
        encoder=tmp_path / "encoder.onnx",
        decoder=tmp_path / "decoder.onnx",
        joiner=tmp_path / "joiner.onnx",
    )
    with pytest.raises(RuntimeError, match="ASR model files are missing"):
        _require_model_files(cfg)


def test_require_model_files_passes_when_all_present(tmp_path: Path) -> None:
    """All four files present → no error (the file-existence gate is satisfied)."""
    for name in ("tokens.txt", "encoder.onnx", "decoder.onnx", "joiner.onnx"):
        (tmp_path / name).write_bytes(b"stub")
    cfg = SherpaModelConfig(
        tokens=tmp_path / "tokens.txt",
        encoder=tmp_path / "encoder.onnx",
        decoder=tmp_path / "decoder.onnx",
        joiner=tmp_path / "joiner.onnx",
    )
    _require_model_files(cfg)  # must not raise


def test_require_model_files_names_each_missing_file(tmp_path: Path) -> None:
    """The error names the specific missing file(s), not just a generic failure."""
    (tmp_path / "tokens.txt").write_bytes(b"stub")
    (tmp_path / "encoder.onnx").write_bytes(b"stub")
    cfg = SherpaModelConfig(
        tokens=tmp_path / "tokens.txt",
        encoder=tmp_path / "encoder.onnx",
        decoder=tmp_path / "decoder.onnx",  # missing
        joiner=tmp_path / "joiner.onnx",  # missing
    )
    with pytest.raises(RuntimeError, match="decoder") as exc:
        _require_model_files(cfg)
    assert "joiner" in str(exc.value)
    assert "tokens" not in str(exc.value)  # present file not listed


def test_module_import_is_dependency_free() -> None:
    """Importing the module must not require ``sherpa_onnx`` or ``numpy``.

    Distinct from the fail-closed construction test above: this proves the
    *import* itself is inert (lazy backend loading), so the module can be
    imported inside the MicroVM/tooling before the ARM inference wheels exist.
    Runs in a fresh interpreter with both modules blocked (set to ``None`` so
    any accidental top-level ``import`` raises ``ImportError``).
    """
    code = (
        "import sys; "
        "sys.modules['sherpa_onnx'] = None; "
        "sys.modules['numpy'] = None; "
        "import asr_server.recognizer as r; "
        "assert hasattr(r, 'SherpaOnlineRecognizer'); "
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

# --- Word timings (streaming path) ------------------------------------------


class WordScriptedBackend(ScriptedBackend):
    """Scripted backend that also reports per-token tokens and timestamps."""

    def __init__(self, script, tokens, timestamps) -> None:  # noqa: ANN001
        super().__init__(script)
        self._tokens = list(tokens)
        self._timestamps = list(timestamps)

    def current_words(self):  # noqa: ANN202
        from asr_server.recognizer import words_from_tokens

        return words_from_tokens(self._tokens, self._timestamps)


def test_a_final_carries_word_timings_when_the_backend_reports_tokens() -> None:
    backend = WordScriptedBackend(
        [("hello world", False), ("hello world", True)],
        ["\u2581hello", "\u2581wor", "ld"],
        [0.10, 0.60, 0.75],
    )
    recognizer = SherpaOnlineRecognizer(backend, sample_rate=SAMPLE_RATE)

    events = feed(recognizer, chunks=2)
    final = [event for event in events if event.kind == "final"][-1]

    assert final.words is not None
    assert [word.w for word in final.words] == ["hello", "world"]
    # Anchored to the segment's own start: a streaming decoder's timestamps restart
    # per segment, so they are used for spacing, not as absolute session times.
    assert final.words[0].s == final.start
    assert final.words[1].s > final.words[0].s


def test_a_backend_without_word_timings_still_finalizes() -> None:
    backend = ScriptedBackend([("hi", True)])
    recognizer = SherpaOnlineRecognizer(backend, sample_rate=SAMPLE_RATE)

    events = feed(recognizer, chunks=1)
    final = [event for event in events if event.kind == "final"][-1]

    assert final.words is None
    assert final.text == "hi"


def test_word_reconstruction_refuses_mismatched_arrays() -> None:
    from asr_server.recognizer import words_from_tokens

    assert words_from_tokens(["\u2581a", "\u2581b"], [0.1]) == []
    assert words_from_tokens([], []) == []


def test_the_sherpa_backend_reads_tokens_from_the_detailed_result() -> None:
    """get_result() is a plain string; the timings are on get_result_all().

    Asking the string for .tokens returned nothing and silently disabled word
    timings — and with them every feature that needs to cut a segment at a time.
    """
    from asr_server.recognizer import _SherpaBackend

    class DetailedResult:
        text = "hello world"
        tokens = ["\u2581hello", "\u2581world"]
        timestamps = [0.2, 0.8]

    class FakeRecognizer:
        def get_result(self, stream):  # noqa: ANN001, ANN202 - mirrors sherpa's API
            return DetailedResult.text

        def get_result_all(self, stream):  # noqa: ANN001, ANN202
            return DetailedResult()

    words = _SherpaBackend(FakeRecognizer(), object()).current_words()

    assert [word.w for word in words] == ["hello", "world"]
    assert words[0].s == 0.2


def test_the_sherpa_backend_falls_back_to_the_token_accessors() -> None:
    class OlderRecognizer:
        def get_result(self, stream):  # noqa: ANN001, ANN202
            return "hi there"

        def tokens(self, stream):  # noqa: ANN001, ANN202
            return ["\u2581hi", "\u2581there"]

        def timestamps(self, stream):  # noqa: ANN001, ANN202
            return [0.1, 0.5]

    words = _SherpaBackend(OlderRecognizer(), object()).current_words()

    assert [word.w for word in words] == ["hi", "there"]


def test_a_recognizer_without_timings_yields_no_words() -> None:
    class TextOnly:
        def get_result(self, stream):  # noqa: ANN001, ANN202
            return "just text"

    assert _SherpaBackend(TextOnly(), object()).current_words() == []


def test_word_reconstruction_handles_the_space_prefixed_tokens_sherpa_returns() -> None:
    """Observed from a real decode: the marker arrives as a leading space.

    tokens come back as [' THE', ' YE', 'LL', 'OW', ...], so keying only off the
    SentencePiece marker glued a whole utterance into one word - and a single word
    can never be cut at a speaker turn.
    """
    from asr_server.recognizer import words_from_tokens

    words = words_from_tokens(
        [" THE", " YE", "LL", "OW", " LA", "M", "P", "S"],
        [2.04, 2.20, 2.28, 2.36, 2.52, 2.64, 2.68, 2.76],
    )

    assert [word.w for word in words] == ["THE", "YELLOW", "LAMPS"]
    assert words[1].s == 2.20
    assert words[1].e == 2.36
    assert words[2].s == 2.52
