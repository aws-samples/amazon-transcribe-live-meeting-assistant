"""Unit tests for the Silero VAD gate.

The acceptance check is twofold: **silence produces no partials** (here, no
events at all) and **speech onsets are detected within one chunk**. Because the runtime
is fail-closed (no model weights, no ``onnxruntime`` / ``numpy`` wheels), these
tests drive :class:`VadGate` through a scripted fake :class:`VadBackend` that
returns pre-set per-frame probabilities — the interface is identical to the real
Silero backend.
"""

from __future__ import annotations

import struct
import subprocess
import sys
from array import array
from collections.abc import Sequence
from pathlib import Path

import pytest
from asr_server.vad import (
    SileroVadConfig,
    SpeechSegment,
    VadBackend,
    VadEvent,
    VadGate,
    create_silero_backend,
    to_segments,
)

SAMPLE_RATE = 16000
FRAME_MS = 32
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 512 samples / 1024 bytes


# --- Scripted fake backend --------------------------------------------------


class ScriptedVadBackend:
    """A fake :class:`VadBackend` returning pre-scripted per-frame probabilities.

    ``probs`` is consumed one value per :meth:`probability` call. Once exhausted
    the last probability sticks (models a steady tail). This lets a test express
    an exact speech/silence trajectory without any model weights.
    """

    def __init__(self, probs: Sequence[float]) -> None:
        self._probs = list(probs)
        self._idx = -1
        self.frames_seen = 0
        self.last_frame: list[float] = []
        self.reset_count = 0

    def probability(self, frame: Sequence[float]) -> float:
        self.frames_seen += 1
        self.last_frame = list(frame)
        self._idx += 1
        if self._idx < len(self._probs):
            return self._probs[self._idx]
        return self._probs[-1] if self._probs else 0.0

    def reset(self) -> None:
        self.reset_count += 1
        self._idx = -1


def _protocol_conformance_check() -> VadBackend:
    # Purely to assert ScriptedVadBackend structurally satisfies the Protocol.
    return ScriptedVadBackend([])


# --- Helpers ----------------------------------------------------------------


def _frames(pcm_frames: int) -> bytes:
    """A whole number of frames of arbitrary non-zero PCM (contents irrelevant)."""
    n = FRAME_SAMPLES * pcm_frames
    return struct.pack("<" + "h" * n, *([1234] * n))


def _gate(probs: Sequence[float], **kwargs: object) -> tuple[VadGate, ScriptedVadBackend]:
    backend = ScriptedVadBackend(probs)
    gate = VadGate(backend, sample_rate=SAMPLE_RATE, frame_ms=FRAME_MS, **kwargs)  # type: ignore[arg-type]
    return gate, backend


# --- Tests ------------------------------------------------------------------


def test_scripted_backend_satisfies_protocol() -> None:
    backend = _protocol_conformance_check()
    assert isinstance(backend, ScriptedVadBackend)


def test_silence_produces_no_events() -> None:
    """Core acceptance: silence never emits an event, so no partials downstream."""
    gate, backend = _gate([0.0, 0.01, 0.0, 0.02, 0.0])
    events: list[VadEvent] = []
    for _ in range(5):
        events.extend(gate.accept_pcm(_frames(1)))
    assert events == []
    assert not gate.is_speech
    assert backend.frames_seen == 5


def test_speech_onset_detected_within_one_frame() -> None:
    """Core acceptance: with no minimum-speech gate, onset fires on the first
    frame at/above threshold (detected within one chunk)."""
    gate, _ = _gate([0.0, 0.9], min_speech_ms=0)
    first = gate.accept_pcm(_frames(1))
    assert first == []  # silent frame: nothing
    second = gate.accept_pcm(_frames(1))
    assert [e.kind for e in second] == ["speech_start"]
    assert gate.is_speech


def test_speech_onset_within_one_chunk_of_many_frames() -> None:
    """A single multi-frame chunk still surfaces the onset (within that chunk)."""
    # One 3-frame chunk: silence, speech, speech. Onset detected inside the chunk.
    gate, _ = _gate([0.0, 0.8, 0.85], min_speech_ms=0)
    events = gate.accept_pcm(_frames(3))
    assert [e.kind for e in events] == ["speech_start"]


def test_speech_start_timestamp_is_padded_and_clamped() -> None:
    """Onset time is padded back by ``speech_pad_ms`` and never negative."""
    frame_s = FRAME_MS / 1000.0
    # First frame is already speech -> unpadded start is 0.0, clamp keeps it 0.0.
    gate, _ = _gate([0.9], speech_pad_ms=30, min_speech_ms=0)
    ev = gate.accept_pcm(_frames(1))
    assert ev[0].kind == "speech_start"
    assert ev[0].t == pytest.approx(0.0)

    # Onset on the 3rd frame with 30 ms pad: start = 2*frame - 0.030, > 0.
    gate2, _ = _gate([0.0, 0.0, 0.9], speech_pad_ms=30, min_speech_ms=0)
    ev2 = gate2.accept_pcm(_frames(3))
    assert ev2[0].t == pytest.approx(2 * frame_s - 0.030, abs=1e-6)


def test_speech_end_after_trailing_silence() -> None:
    """A run of speech then sustained silence closes the segment once rules met."""
    # min_silence 0 and min_speech 0 so end fires on the first sub-neg frame.
    gate, _ = _gate([0.9, 0.9, 0.0], min_silence_ms=0, min_speech_ms=0)
    events: list[VadEvent] = []
    for _ in range(3):
        events.extend(gate.accept_pcm(_frames(1)))
    kinds = [e.kind for e in events]
    assert kinds == ["speech_start", "speech_end"]
    assert not gate.is_speech


def test_min_silence_holds_segment_open_through_brief_dip() -> None:
    """A dip shorter than ``min_silence_ms`` must not close the segment."""
    frame_s = FRAME_MS / 1000.0
    # min_silence spans ~2 frames. One dip frame then speech again -> no end.
    min_silence = int(frame_s * 1000 * 1.5)
    gate, _ = _gate(
        [0.9, 0.9, 0.0, 0.9, 0.9],
        min_silence_ms=min_silence,
        min_speech_ms=0,
    )
    events: list[VadEvent] = []
    for _ in range(5):
        events.extend(gate.accept_pcm(_frames(1)))
    assert [e.kind for e in events] == ["speech_start"]  # never closed
    assert gate.is_speech


def test_speech_run_shorter_than_min_speech_is_rejected() -> None:
    """A speech blip shorter than ``min_speech_ms`` produces NO segment at all.

    Correct semantics: ``min_speech_ms`` is a minimum-duration gate on speech, not
    a delay on closing. A 1-frame blip that drops back to silence before the
    minimum is reached must emit neither ``speech_start`` nor ``speech_end``.
    """
    frame_s = FRAME_MS / 1000.0
    # Require >= 3 frames of contiguous speech; the blip is only 1 frame.
    min_speech = int(round(frame_s * 1000 * 3))
    gate, backend = _gate(
        [0.9, 0.0, 0.0, 0.0, 0.0],
        min_silence_ms=0,
        min_speech_ms=min_speech,
    )
    events: list[VadEvent] = []
    for _ in range(5):
        events.extend(gate.accept_pcm(_frames(1)))
    assert events == []  # blip discarded: no start, no end
    assert not gate.is_speech
    assert backend.frames_seen == 5


def test_speech_run_meeting_min_speech_is_reported() -> None:
    """A run that MEETS ``min_speech_ms`` is confirmed: start emitted retroactively
    at the true onset, then end after ``min_silence_ms`` of trailing silence."""
    frame_s = FRAME_MS / 1000.0
    min_speech = int(round(frame_s * 1000 * 3))  # >= 3 contiguous speech frames
    # 3 speech frames (meets minimum) then silence closes it (min_silence 0).
    gate, _ = _gate(
        [0.9, 0.9, 0.9, 0.0],
        min_silence_ms=0,
        min_speech_ms=min_speech,
        speech_pad_ms=0,
    )
    events: list[VadEvent] = []
    for _ in range(4):
        events.extend(gate.accept_pcm(_frames(1)))
    assert [e.kind for e in events] == ["speech_start", "speech_end"]
    start, end = events
    # speech_start is retroactive to the true onset (frame 0), not the confirmation
    # instant (frame 2's end), so the segment spans the real speech.
    assert start.t == pytest.approx(0.0, abs=1e-6)
    # Speech ran frames 0-2; silence begins at frame 3 (t = 3 * frame).
    assert end.t == pytest.approx(3 * frame_s, abs=1e-6)
    assert end.t > start.t


def test_negative_threshold_hysteresis_keeps_speech_in_gray_zone() -> None:
    """Probabilities between neg_threshold and threshold hold the segment open."""
    # threshold 0.5 -> neg_threshold 0.35 by default. A 0.4 frame is "gray".
    gate, _ = _gate([0.9, 0.4, 0.4, 0.4], min_silence_ms=0, min_speech_ms=0)
    events: list[VadEvent] = []
    for _ in range(4):
        events.extend(gate.accept_pcm(_frames(1)))
    assert [e.kind for e in events] == ["speech_start"]  # gray zone -> no end
    assert gate.is_speech


def test_flush_closes_open_segment() -> None:
    """End-of-audio with speech in progress emits a closing ``speech_end``."""
    gate, backend = _gate([0.9, 0.9], min_speech_ms=0)
    for _ in range(2):
        gate.accept_pcm(_frames(1))
    assert gate.is_speech
    flushed = gate.flush()
    assert [e.kind for e in flushed] == ["speech_end"]
    # Ends at the last processed sample (2 frames of audio).
    assert flushed[0].t == pytest.approx(2 * FRAME_MS / 1000.0, abs=1e-6)
    assert not gate.is_speech
    assert backend.reset_count == 1


def test_flush_with_no_open_segment_is_empty() -> None:
    gate, _ = _gate([0.0, 0.0])
    for _ in range(2):
        gate.accept_pcm(_frames(1))
    assert gate.flush() == []


def test_reset_clears_state_and_backend() -> None:
    gate, backend = _gate([0.9, 0.9], min_speech_ms=0)
    gate.accept_pcm(_frames(2))
    assert gate.is_speech
    gate.reset()
    assert not gate.is_speech
    assert backend.reset_count == 1
    # After reset the gate is reusable and timestamps restart from 0.
    ev = gate.accept_pcm(_frames(1))  # backend tail prob sticks at 0.9
    assert [e.kind for e in ev] == ["speech_start"]
    assert ev[0].t == pytest.approx(0.0)


def test_partial_frames_are_buffered_across_chunks() -> None:
    """Chunks that don't align to the frame size are reassembled, not dropped."""
    gate, backend = _gate([0.9], min_speech_ms=0)
    # Feed half a frame, then the other half: exactly one frame gets scored.
    half = struct.pack("<" + "h" * (FRAME_SAMPLES // 2), *([1000] * (FRAME_SAMPLES // 2)))
    assert gate.accept_pcm(half) == []
    assert backend.frames_seen == 0  # not enough for a frame yet
    events = gate.accept_pcm(half)
    assert backend.frames_seen == 1
    assert [e.kind for e in events] == ["speech_start"]


def test_sub_frame_tail_is_scored_only_when_complete() -> None:
    """A leftover sub-frame tail is never scored (shorter than the VAD window)."""
    gate, backend = _gate([0.9, 0.9])
    # 1.5 frames: one frame scored, half a frame buffered.
    one_and_half = struct.pack(
        "<" + "h" * (FRAME_SAMPLES + FRAME_SAMPLES // 2),
        *([1000] * (FRAME_SAMPLES + FRAME_SAMPLES // 2)),
    )
    gate.accept_pcm(one_and_half)
    assert backend.frames_seen == 1  # the trailing half-frame was not scored


def test_to_segments_pairs_events() -> None:
    events = [
        VadEvent(kind="speech_start", t=1.0),
        VadEvent(kind="speech_end", t=2.5),
        VadEvent(kind="speech_start", t=4.0),
        VadEvent(kind="speech_end", t=5.0),
    ]
    assert to_segments(events) == [
        SpeechSegment(start=1.0, end=2.5),
        SpeechSegment(start=4.0, end=5.0),
    ]


def test_to_segments_ignores_unclosed_trailing_start() -> None:
    """A dangling ``speech_start`` (speech still open) yields no segment."""
    events = [
        VadEvent(kind="speech_start", t=1.0),
        VadEvent(kind="speech_end", t=2.0),
        VadEvent(kind="speech_start", t=3.0),  # never closed
    ]
    assert to_segments(events) == [SpeechSegment(start=1.0, end=2.0)]


def test_events_are_frozen() -> None:
    ev = VadEvent(kind="speech_start", t=1.0)
    seg = SpeechSegment(start=0.0, end=1.0)
    with pytest.raises(AttributeError):
        ev.t = 2.0  # type: ignore[misc]
    with pytest.raises(AttributeError):
        seg.start = 2.0  # type: ignore[misc]


# --- Constructor validation -------------------------------------------------


def test_invalid_sample_rate_rejected() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        VadGate(ScriptedVadBackend([]), sample_rate=0)


def test_invalid_frame_ms_rejected() -> None:
    with pytest.raises(ValueError, match="frame_ms"):
        VadGate(ScriptedVadBackend([]), frame_ms=0)


def test_threshold_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match="threshold"):
        VadGate(ScriptedVadBackend([]), threshold=1.5)


def test_negative_rule_values_rejected() -> None:
    with pytest.raises(ValueError, match="must be >= 0"):
        VadGate(ScriptedVadBackend([]), min_silence_ms=-1)


def test_neg_threshold_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match="neg_threshold"):
        VadGate(ScriptedVadBackend([]), threshold=0.5, neg_threshold=0.9)


# --- PCM handling -----------------------------------------------------------


def test_odd_length_pcm_rejected() -> None:
    gate, _ = _gate([0.0])
    with pytest.raises(ValueError, match="even"):
        gate.accept_pcm(b"\x00\x00\x00")  # 3 bytes -> not a whole 16-bit sample


def test_empty_pcm_chunk_is_noop() -> None:
    gate, backend = _gate([0.9])
    assert gate.accept_pcm(b"") == []
    assert backend.frames_seen == 0


def test_pcm_conversion_normalization_contract() -> None:
    """int16 LE -> float32: divide by 32768 (full-scale), stay in [-1, 1]."""
    # One frame's worth of known samples so the fake backend receives them.
    values = [32767, -16384, 0, -32768] + [0] * (FRAME_SAMPLES - 4)
    gate, backend = _gate([0.0])
    gate.accept_pcm(array("h", values).tobytes())
    got = backend.last_frame
    assert len(got) == FRAME_SAMPLES
    assert got[0] == pytest.approx(32767 / 32768, abs=1e-6)
    assert got[1] == pytest.approx(-0.5, abs=1e-9)
    assert got[2] == 0.0
    assert got[3] == pytest.approx(-1.0, abs=1e-9)  # -32768 -> exactly -1.0
    assert all(-1.0 <= s <= 1.0 for s in got)


# --- Fail-closed / lazy-import contract -------------------------------------


def test_create_silero_backend_fails_closed_without_wheel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the onnxruntime wheel, constructing a real backend fails cleanly.

    Stubs ``_load_onnxruntime`` to simulate the missing wheel so the fail-closed
    path ACTUALLY runs even when ``onnxruntime`` is installed (previously it
    skipped, leaving the VAD backend's fail-closed contract unverified).
    """
    import asr_server.vad as vad_mod

    def _missing_wheel() -> None:
        raise RuntimeError(
            "onnxruntime is not installed. Install the aarch64 wheel pinned in "
            "requirements.txt to construct a real Silero VAD backend."
        )

    monkeypatch.setattr(vad_mod, "_load_onnxruntime", _missing_wheel)
    cfg = SileroVadConfig(model=Path("silero_vad.onnx"))
    with pytest.raises(RuntimeError, match="onnxruntime is not installed"):
        create_silero_backend(cfg)


def test_create_silero_backend_wraps_model_load_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing/invalid model surfaces as a clear RuntimeError, not a raw trace.

    Stubs ``_load_onnxruntime`` with a fake ``ort`` whose ``InferenceSession``
    raises (as it would for a missing/corrupt model file), so the wrapping logic
    in ``create_silero_session`` is exercised regardless of whether ``onnxruntime``
    is installed — previously this SKIPPED when the wheel was absent, leaving the
    error-wrapping contract unverified.
    """
    import asr_server.vad as vad_mod

    class _FakeSessionOptions:
        # ``create_silero_session`` sets inter_op_num_threads / intra_op_num_threads
        # on this; a plain settable object suffices.
        pass

    class _FakeOrt:
        SessionOptions = _FakeSessionOptions

        @staticmethod
        def InferenceSession(*_args: object, **_kwargs: object) -> object:
            # Model file missing/invalid → onnxruntime raises deep in native code;
            # ``create_silero_session`` must wrap it in an actionable RuntimeError.
            raise OSError("No such file or directory")

    monkeypatch.setattr(vad_mod, "_load_onnxruntime", lambda: _FakeOrt)
    cfg = SileroVadConfig(model=Path("/nonexistent/silero_vad.onnx"))
    with pytest.raises(RuntimeError, match="failed to load Silero VAD model"):
        create_silero_backend(cfg)


def test_module_import_is_dependency_free() -> None:
    """Importing the module must not require ``onnxruntime`` or ``numpy``.

    Proves the *import* itself is inert (lazy backend loading), so the module can
    be imported inside the MicroVM/tooling before the ARM inference wheels exist.
    Runs in a fresh interpreter with both modules blocked (set to ``None`` so any
    accidental top-level ``import`` raises ``ImportError``).
    """
    code = (
        "import sys; "
        "sys.modules['onnxruntime'] = None; "
        "sys.modules['numpy'] = None; "
        "import asr_server.vad as v; "
        "assert hasattr(v, 'VadGate'); "
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
