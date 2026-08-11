"""Unit tests for streaming speaker diarization.

The acceptance checks are: **each finalized segment carries a speaker label**,
**the same voice maps to the same label across segments**, **a different voice
mints a new label**, and — the property that motivated doing diarization inside
the ASR runtime at all — **the speaker is derived from the same segment
boundaries as the transcript**, so labels cannot drift from the text.

Because the runtime is fail-closed (no model weights, no ``sherpa_onnx`` /
``numpy`` wheels), these tests drive :class:`DiarizingRecognizer` through a
scripted fake :class:`SpeakerEmbedder` that returns pre-set embedding vectors —
the interface is identical to the real sherpa-onnx extractor. The assignment
policy itself (:class:`SpeakerRegistry`) is pure stdlib, so it is tested directly
on vectors with no audio at all.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from pathlib import Path

import pytest
from asr_server.diarization import (
    DiarizationConfig,
    DiarizingEngine,
    DiarizingRecognizer,
    SpeakerEmbedder,
    SpeakerEmbedderConfig,
    SpeakerRegistry,
    build_diarization_config,
    diarization_enabled,
)
from asr_server.recognizer import (
    Event,
    Recognizer,
    RecognizerEngine,
    SessionConfig,
    SessionConfigError,
)

SAMPLE_RATE = 16000

# Two orthogonal unit vectors stand in for two distinct voices: cosine similarity
# between them is 0.0 (far below any sane threshold) while each matches itself at
# 1.0, so speaker identity is unambiguous without needing real embeddings.
VOICE_A = [1.0, 0.0, 0.0, 0.0]
VOICE_B = [0.0, 1.0, 0.0, 0.0]
# Close to VOICE_A (cos ≈ 0.98): the same person on a different utterance.
VOICE_A_VARIANT = [0.98, 0.2, 0.0, 0.0]


# --- Scripted fakes ---------------------------------------------------------


class ScriptedEmbedder:
    """A fake :class:`SpeakerEmbedder` returning pre-scripted embedding vectors.

    ``embeddings`` is consumed one value per :meth:`embed` call. Once exhausted the
    last vector sticks (models a steady speaker). This lets a test express an exact
    sequence of voices without any model weights. ``fail_on`` makes the Nth call
    (0-based) raise, exercising the "diarization must never break transcription"
    contract.
    """

    def __init__(
        self,
        embeddings: Sequence[Sequence[float]],
        *,
        fail_on: int | None = None,
    ) -> None:
        self._embeddings = [list(v) for v in embeddings]
        self._idx = -1
        self.calls = 0
        self.sample_counts: list[int] = []
        self._fail_on = fail_on

    @property
    def dim(self) -> int:
        return len(self._embeddings[0]) if self._embeddings else 0

    def embed(self, sample_rate: int, samples: Sequence[float]) -> list[float]:
        call = self.calls
        self.calls += 1
        self.sample_counts.append(len(samples))
        if self._fail_on is not None and call == self._fail_on:
            raise RuntimeError("scripted embedder failure")
        self._idx += 1
        if self._idx < len(self._embeddings):
            return list(self._embeddings[self._idx])
        return list(self._embeddings[-1]) if self._embeddings else []


class ScriptedRecognizer(Recognizer):
    """A fake inner :class:`Recognizer` emitting pre-scripted events per call.

    ``per_chunk`` supplies the events returned by successive ``accept_pcm`` calls
    (an empty list means "no events for this chunk"); ``on_flush`` supplies the
    events returned by ``flush``. Records the PCM it received so a test can prove
    the decorator forwards audio unchanged.
    """

    def __init__(
        self,
        per_chunk: Sequence[Sequence[Event]] = (),
        *,
        on_flush: Sequence[Event] = (),
    ) -> None:
        self._per_chunk = [list(e) for e in per_chunk]
        self._on_flush = list(on_flush)
        self._idx = -1
        self.chunks: list[bytes] = []
        self.flushed = False

    def accept_pcm(self, pcm: bytes) -> list[Event]:
        self.chunks.append(pcm)
        self._idx += 1
        if self._idx < len(self._per_chunk):
            return list(self._per_chunk[self._idx])
        return []

    def flush(self) -> list[Event]:
        self.flushed = True
        return list(self._on_flush)


class ScriptedEngine(RecognizerEngine):
    """A fake inner :class:`RecognizerEngine` recording the configs it received."""

    def __init__(
        self,
        recognizer: Recognizer | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self._recognizer = recognizer if recognizer is not None else ScriptedRecognizer()
        self._raises = raises
        self.session_configs: list[SessionConfig | None] = []

    def new_session(self, config: SessionConfig | None = None) -> Recognizer:
        self.session_configs.append(config)
        if self._raises is not None:
            raise self._raises
        return self._recognizer


def _embedder_protocol_conformance_check() -> SpeakerEmbedder:
    # Purely to assert ScriptedEmbedder structurally satisfies the Protocol.
    return ScriptedEmbedder([VOICE_A])


# --- Helpers ----------------------------------------------------------------


def _pcm(seconds: float, *, value: int = 1234) -> bytes:
    """``seconds`` of arbitrary non-zero 16-bit LE mono PCM (contents irrelevant)."""
    n = int(SAMPLE_RATE * seconds)
    return struct.pack("<" + "h" * n, *([value] * n))


def _final(segment: int, text: str, start: float, end: float) -> Event:
    return Event(kind="final", segment=segment, text=text, start=start, end=end)


def _partial(segment: int, text: str, start: float) -> Event:
    return Event(kind="partial", segment=segment, text=text, start=start)


def _recognizer(
    inner: ScriptedRecognizer,
    embedder: ScriptedEmbedder,
    **kwargs: object,
) -> DiarizingRecognizer:
    return DiarizingRecognizer(
        inner,
        embedder,
        sample_rate=SAMPLE_RATE,
        **kwargs,  # type: ignore[arg-type]
    )


# --- Protocol conformance ---------------------------------------------------


def test_scripted_embedder_satisfies_protocol() -> None:
    embedder = _embedder_protocol_conformance_check()
    assert isinstance(embedder, ScriptedEmbedder)


# --- SpeakerRegistry: the assignment policy ---------------------------------


def test_first_embedding_mints_speaker_zero() -> None:
    """The first voice heard becomes ``spk_0``."""
    registry = SpeakerRegistry()
    assert registry.assign(VOICE_A) == "spk_0"
    assert registry.num_speakers == 1
    assert registry.speakers == ["spk_0"]


def test_same_voice_maps_to_same_speaker() -> None:
    """Core acceptance: a repeated voice re-uses its label, not a new one."""
    registry = SpeakerRegistry()
    assert registry.assign(VOICE_A) == "spk_0"
    assert registry.assign(VOICE_A) == "spk_0"
    assert registry.assign(VOICE_A_VARIANT) == "spk_0"  # same person, cos ~0.98
    assert registry.num_speakers == 1


def test_different_voice_mints_new_speaker() -> None:
    """Core acceptance: a dissimilar voice becomes a distinct speaker."""
    registry = SpeakerRegistry()
    assert registry.assign(VOICE_A) == "spk_0"
    assert registry.assign(VOICE_B) == "spk_1"
    assert registry.num_speakers == 2


def test_speakers_alternate_back_to_earlier_label() -> None:
    """A conversation returning to an earlier speaker re-uses that label."""
    registry = SpeakerRegistry()
    labels = [
        registry.assign(v) for v in (VOICE_A, VOICE_B, VOICE_A, VOICE_B, VOICE_A)
    ]
    assert labels == ["spk_0", "spk_1", "spk_0", "spk_1", "spk_0"]
    assert registry.num_speakers == 2


def test_max_speakers_caps_identity_count() -> None:
    """``max_speakers`` forces a third voice onto its closest existing label."""
    registry = SpeakerRegistry(threshold=0.9, max_speakers=2)
    assert registry.assign(VOICE_A) == "spk_0"
    assert registry.assign(VOICE_B) == "spk_1"
    # A third, unrelated voice would normally mint spk_2; the cap forces a match.
    third = [0.0, 0.0, 1.0, 0.0]
    assert registry.assign(third) in {"spk_0", "spk_1"}
    assert registry.num_speakers == 2


def test_threshold_governs_splitting() -> None:
    """A high threshold splits a near-match into a second speaker; a low one merges."""
    strict = SpeakerRegistry(threshold=0.999)
    strict.assign(VOICE_A)
    assert strict.assign(VOICE_A_VARIANT) == "spk_1"  # cos ~0.98 < 0.999 → split

    lenient = SpeakerRegistry(threshold=0.5)
    lenient.assign(VOICE_A)
    assert lenient.assign(VOICE_A_VARIANT) == "spk_0"  # merged


def test_centroid_is_a_running_mean_of_accepted_embeddings() -> None:
    """Accepted embeddings are averaged into the speaker's centroid."""
    registry = SpeakerRegistry()
    registry.assign([2.0, 0.0])
    registry.assign([0.0, 2.0])  # cos 0.0 → new speaker, not folded into spk_0
    assert registry.centroid("spk_0") == [2.0, 0.0]
    # Two similar vectors DO average together.
    registry2 = SpeakerRegistry(threshold=0.0)
    registry2.assign([2.0, 0.0])
    registry2.assign([0.0, 2.0])
    assert registry2.num_speakers == 1
    assert registry2.centroid("spk_0") == [1.0, 1.0]


def test_unknown_speaker_centroid_is_empty() -> None:
    assert SpeakerRegistry().centroid("spk_9") == []


def test_empty_embedding_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        SpeakerRegistry().assign([])


def test_dimension_change_is_rejected() -> None:
    """A vector of a different width than the centroid must not corrupt it."""
    registry = SpeakerRegistry(threshold=-1.0)  # force a match to reach the fold
    registry.assign([1.0, 0.0])
    with pytest.raises(ValueError, match="dimension"):
        registry.assign([1.0, 0.0, 0.0])


@pytest.mark.parametrize("threshold", [-1.5, 1.5])
def test_invalid_threshold_is_rejected(threshold: float) -> None:
    with pytest.raises(ValueError, match="threshold"):
        SpeakerRegistry(threshold=threshold)


def test_negative_max_speakers_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_speakers"):
        SpeakerRegistry(max_speakers=-1)


def test_degenerate_zero_vector_scores_zero() -> None:
    """A zero vector has no direction, so similarity is 0.0 rather than NaN."""
    registry = SpeakerRegistry()
    registry.assign(VOICE_A)
    assert registry.score("spk_0", [0.0, 0.0, 0.0, 0.0]) == 0.0


# --- DiarizingRecognizer: labelling the transcript --------------------------


def test_final_carries_speaker_label() -> None:
    """Core acceptance: a finalized segment is labelled with its speaker."""
    inner = ScriptedRecognizer([[_final(0, "hello", 0.0, 1.0)]])
    embedder = ScriptedEmbedder([VOICE_A])
    rec = _recognizer(inner, embedder)

    (event,) = rec.accept_pcm(_pcm(1.0))

    assert event.kind == "final"
    assert event.speaker == "spk_0"
    assert event.text == "hello"  # transcript passes through untouched


def test_two_speakers_across_segments_are_distinguished() -> None:
    """Core acceptance: alternating voices yield alternating labels."""
    inner = ScriptedRecognizer(
        [
            [_final(0, "hi", 0.0, 1.0)],
            [_final(1, "hello", 1.0, 2.0)],
            [_final(2, "bye", 2.0, 3.0)],
        ]
    )
    embedder = ScriptedEmbedder([VOICE_A, VOICE_B, VOICE_A])
    rec = _recognizer(inner, embedder)

    speakers: list[str | None] = []
    for _ in range(3):
        events = rec.accept_pcm(_pcm(1.0))
        speakers.extend(e.speaker for e in events)

    assert speakers == ["spk_0", "spk_1", "spk_0"]
    assert rec.registry.num_speakers == 2


def test_speaker_is_derived_from_the_segment_own_audio() -> None:
    """Zero-drift: the embedder receives exactly the segment's own samples.

    This is the property that makes in-runtime diarization better than pairing an
    external diariser with a separate transcript: the label is computed from the
    SAME span the transcript covers, so the two cannot diverge.
    """
    # One 3s chunk; the inner engine finalizes only [1.0, 2.0).
    inner = ScriptedRecognizer([[_final(0, "middle", 1.0, 2.0)]])
    embedder = ScriptedEmbedder([VOICE_A])
    rec = _recognizer(inner, embedder)

    rec.accept_pcm(_pcm(3.0))

    # Exactly 1.0s of audio (16000 samples) was embedded — the segment span.
    assert embedder.sample_counts == [SAMPLE_RATE]


def test_partial_carries_provisional_speaker_and_is_not_embedded() -> None:
    """Partials reuse the current speaker; only finals trigger an embedding."""
    inner = ScriptedRecognizer(
        [
            [_final(0, "hi", 0.0, 1.0)],
            [_partial(1, "and then", 1.0)],
        ]
    )
    embedder = ScriptedEmbedder([VOICE_A])
    rec = _recognizer(inner, embedder)

    rec.accept_pcm(_pcm(1.0))
    (partial,) = rec.accept_pcm(_pcm(1.0))

    assert partial.kind == "partial"
    assert partial.speaker == "spk_0"  # provisional, from the last known speaker
    assert embedder.calls == 1  # the partial did NOT cost an embedding


def test_partial_before_any_speaker_has_no_label() -> None:
    """A partial arriving before any final has no speaker to attribute yet."""
    inner = ScriptedRecognizer([[_partial(0, "hel", 0.0)]])
    rec = _recognizer(inner, ScriptedEmbedder([VOICE_A]))

    (partial,) = rec.accept_pcm(_pcm(0.5))

    assert partial.speaker is None
    assert partial.text == "hel"


def test_short_segment_reuses_current_speaker_without_embedding() -> None:
    """A segment below ``min_segment_ms`` inherits the speaker, unembedded.

    Embedding a very short slice yields an unreliable vector, which would invent a
    spurious identity; conversational continuity is the better default.
    """
    inner = ScriptedRecognizer(
        [
            [_final(0, "hello there", 0.0, 1.0)],
            [_final(1, "yes", 1.0, 1.1)],  # 100 ms → below the 400 ms floor
        ]
    )
    embedder = ScriptedEmbedder([VOICE_A, VOICE_B])
    rec = _recognizer(inner, embedder, min_segment_ms=400)

    rec.accept_pcm(_pcm(1.0))
    (second,) = rec.accept_pcm(_pcm(1.0))

    assert second.speaker == "spk_0"  # inherited, not the scripted VOICE_B
    assert embedder.calls == 1
    assert rec.registry.num_speakers == 1


def test_embedder_failure_degrades_to_no_speaker_not_a_crash() -> None:
    """A diarization fault must never break transcription (speaker just goes None)."""
    inner = ScriptedRecognizer([[_final(0, "still transcribed", 0.0, 1.0)]])
    embedder = ScriptedEmbedder([VOICE_A], fail_on=0)
    rec = _recognizer(inner, embedder)

    (event,) = rec.accept_pcm(_pcm(1.0))

    assert event.text == "still transcribed"  # transcript survives
    assert event.speaker is None


def test_flush_labels_the_trailing_segment() -> None:
    """A segment finalized at EOS is labelled like any other."""
    inner = ScriptedRecognizer(on_flush=[_final(0, "last words", 0.0, 1.0)])
    rec = _recognizer(inner, ScriptedEmbedder([VOICE_A]))

    rec.accept_pcm(_pcm(1.0))
    (event,) = rec.flush()

    assert inner.flushed
    assert event.speaker == "spk_0"


def test_pcm_is_forwarded_to_the_inner_recognizer_unchanged() -> None:
    """The decorator is transparent to the transcription path."""
    inner = ScriptedRecognizer()
    rec = _recognizer(inner, ScriptedEmbedder([VOICE_A]))
    chunk = _pcm(0.5)

    assert rec.accept_pcm(chunk) == []
    assert inner.chunks == [chunk]


def test_odd_length_pcm_raises_before_touching_the_inner_engine() -> None:
    """A malformed frame surfaces as ValueError (server maps it to BAD_ENCODING)."""
    inner = ScriptedRecognizer()
    rec = _recognizer(inner, ScriptedEmbedder([VOICE_A]))

    with pytest.raises(ValueError, match="even"):
        rec.accept_pcm(b"\x01\x02\x03")
    assert inner.chunks == []


def test_current_speaker_tracks_the_latest_identification() -> None:
    inner = ScriptedRecognizer(
        [[_final(0, "a", 0.0, 1.0)], [_final(1, "b", 1.0, 2.0)]]
    )
    rec = _recognizer(inner, ScriptedEmbedder([VOICE_A, VOICE_B]))

    assert rec.current_speaker is None
    rec.accept_pcm(_pcm(1.0))
    assert rec.current_speaker == "spk_0"
    rec.accept_pcm(_pcm(1.0))
    assert rec.current_speaker == "spk_1"


def test_buffer_is_bounded_by_max_buffer_ms() -> None:
    """Retained audio stays bounded even if the inner engine never finalizes.

    A fixed-memory MicroVM must not grow without bound during one long utterance.
    """
    inner = ScriptedRecognizer()  # emits nothing: no final ever prunes the buffer
    rec = _recognizer(inner, ScriptedEmbedder([VOICE_A]), max_buffer_ms=1000)

    for _ in range(5):
        rec.accept_pcm(_pcm(1.0))

    # 5s fed, at most 1s retained.
    assert len(rec._buffer) <= SAMPLE_RATE  # noqa: SLF001 - bounded-memory invariant


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"sample_rate": 0}, "sample_rate"),
        ({"min_segment_ms": -1}, "min_segment_ms"),
        ({"max_buffer_ms": 0}, "max_buffer_ms"),
    ],
)
def test_invalid_recognizer_config_is_rejected(
    kwargs: dict[str, int], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        DiarizingRecognizer(
            ScriptedRecognizer(),
            ScriptedEmbedder([VOICE_A]),
            **kwargs,
        )


# --- DiarizingEngine: the shared-engine decorator ---------------------------


def _config(**kwargs: object) -> DiarizationConfig:
    base = DiarizationConfig(
        embedder=SpeakerEmbedderConfig(
            model=Path("unused.onnx"), sample_rate=SAMPLE_RATE
        ),
    )
    for key, value in kwargs.items():
        setattr(base, key, value)
    return base


def test_engine_hands_out_diarizing_sessions() -> None:
    inner = ScriptedEngine()
    engine = DiarizingEngine(inner, ScriptedEmbedder([VOICE_A]), _config())

    session = engine.new_session(SessionConfig(sample_rate=SAMPLE_RATE))

    assert isinstance(session, DiarizingRecognizer)
    assert engine.inner is inner


def test_engine_delegates_config_to_the_inner_engine() -> None:
    """The inner engine still sees the negotiated config (it owns validation)."""
    inner = ScriptedEngine()
    engine = DiarizingEngine(inner, ScriptedEmbedder([VOICE_A]), _config())
    config = SessionConfig(sample_rate=SAMPLE_RATE, endpointing_ms=900)

    engine.new_session(config)

    assert inner.session_configs == [config]


def test_inner_session_config_error_propagates_unchanged() -> None:
    """A ``SessionConfigError`` must still reach the server as a fatal BAD_CONFIG."""
    inner = ScriptedEngine(raises=SessionConfigError("bad sample_rate"))
    engine = DiarizingEngine(inner, ScriptedEmbedder([VOICE_A]), _config())

    with pytest.raises(SessionConfigError, match="bad sample_rate"):
        engine.new_session(SessionConfig(sample_rate=8000))


def test_sessions_do_not_share_speaker_identities() -> None:
    """Speaker labels are per connection: a new session starts from ``spk_0``.

    Leaking identities across connections would attribute one caller's speech to
    another caller's label.
    """
    embedder = ScriptedEmbedder([VOICE_A, VOICE_B])
    engine = DiarizingEngine(ScriptedEngine(), embedder, _config())

    first = engine.new_session()
    second = engine.new_session()

    assert isinstance(first, DiarizingRecognizer)
    assert isinstance(second, DiarizingRecognizer)
    assert first.registry is not second.registry
    assert second.registry.num_speakers == 0


def test_session_overrides_threshold_and_max_speakers() -> None:
    """Per-session diarization knobs are honoured without an engine rebuild."""
    inner_rec = ScriptedRecognizer(
        [[_final(0, "a", 0.0, 1.0)], [_final(1, "b", 1.0, 2.0)]]
    )
    embedder = ScriptedEmbedder([VOICE_A, VOICE_B])
    engine = DiarizingEngine(
        ScriptedEngine(inner_rec),
        embedder,
        _config(threshold=0.5, max_speakers=0),
    )

    # Negotiate a 1-speaker conversation: the second, different voice must be
    # forced onto spk_0 rather than minting spk_1.
    session = engine.new_session(
        SessionConfig(sample_rate=SAMPLE_RATE, max_speakers=1)
    )
    assert isinstance(session, DiarizingRecognizer)
    session.accept_pcm(_pcm(1.0))
    session.accept_pcm(_pcm(1.0))

    assert session.registry.num_speakers == 1


# --- Config resolution (NFR5) -----------------------------------------------


def test_build_diarization_config_defaults_under_model_dir() -> None:
    config = build_diarization_config(model_dir="/opt/models")
    assert str(config.embedder.model) == "/opt/models/speaker_embedding.onnx"
    assert config.threshold == 0.5
    assert config.max_speakers == 0


def test_build_diarization_config_explicit_override_wins() -> None:
    config = build_diarization_config(
        model_dir="/opt/models",
        speaker_model="/custom/titanet.onnx",
        threshold=0.7,
        max_speakers=2,
        num_threads=4,
    )
    assert str(config.embedder.model) == "/custom/titanet.onnx"
    assert config.embedder.num_threads == 4
    assert config.threshold == 0.7
    assert config.max_speakers == 2


def test_build_diarization_config_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASR_SPEAKER_MODEL", "/env/spk.onnx")
    monkeypatch.setenv("ASR_SPEAKER_THRESHOLD", "0.62")
    monkeypatch.setenv("ASR_MAX_SPEAKERS", "3")
    monkeypatch.setenv("ASR_NUM_THREADS", "2")

    config = build_diarization_config()

    assert str(config.embedder.model) == "/env/spk.onnx"
    assert config.threshold == pytest.approx(0.62)
    assert config.max_speakers == 3
    assert config.embedder.num_threads == 2


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_diarization_explicitly_forced_on(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit truthy ``ASR_DIARIZE`` wins even with no model on disk.

    Forcing it on without a model is a deliberate fail-loud: engine construction
    raises at startup rather than quietly serving unlabelled transcripts.
    """
    monkeypatch.setenv("ASR_DIARIZE", value)
    assert diarization_enabled("/nonexistent/speaker.onnx") is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off"])
def test_diarization_explicitly_forced_off(
    value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit falsey ``ASR_DIARIZE`` wins even when the model IS present."""
    model = tmp_path / "speaker_embedding.onnx"
    model.write_bytes(b"present")
    monkeypatch.setenv("ASR_DIARIZE", value)
    assert diarization_enabled(model) is False


@pytest.mark.parametrize("value", ["", "auto", "maybe"])
def test_diarization_auto_detects_present_model(
    value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without an explicit setting, a baked speaker model turns diarization ON.

    Presence is the switch because the MicroVM image build API takes no build
    args, so "was the model staged into the image" is the only signal that
    survives to the remote build.
    """
    model = tmp_path / "speaker_embedding.onnx"
    model.write_bytes(b"present")
    monkeypatch.setenv("ASR_DIARIZE", value)
    assert diarization_enabled(model) is True


def test_diarization_auto_detects_absent_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without an explicit setting and no model, diarization stays OFF."""
    monkeypatch.delenv("ASR_DIARIZE", raising=False)
    assert diarization_enabled(tmp_path / "missing.onnx") is False


def test_diarization_auto_uses_configured_model_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Auto-detection resolves the model through the normal config path.

    Proves the switch honours ``$ASR_SPEAKER_MODEL`` rather than a hardcoded
    location, so the image's configured path is what actually gets checked.
    """
    monkeypatch.delenv("ASR_DIARIZE", raising=False)
    model = tmp_path / "titanet.onnx"
    monkeypatch.setenv("ASR_SPEAKER_MODEL", str(model))
    assert diarization_enabled() is False
    model.write_bytes(b"present")
    assert diarization_enabled() is True


# --- Fail-closed real backend ----------------------------------------------


def test_create_sherpa_embedder_fails_closed_on_missing_model() -> None:
    """The real embedder fails CLOSED rather than half-initialising.

    Two independent preconditions must hold to build a real extractor: the
    ``sherpa_onnx`` wheel must be importable AND the model file must exist. Either
    one missing raises ``RuntimeError`` with an actionable message, so a bad
    ``ASR_SPEAKER_MODEL`` path or an unprovisioned image surfaces at startup /
    build-time warmup instead of a session dying mid-connection. The assertion
    accepts either message so the test is meaningful whether or not the (large,
    platform-specific) wheel happens to be installed in the running environment.
    """
    from asr_server.diarization import create_sherpa_embedder

    config = SpeakerEmbedderConfig(model=Path("/nonexistent/speaker.onnx"))
    with pytest.raises(
        RuntimeError, match="speaker embedding model is missing|sherpa-onnx is not installed"
    ):
        create_sherpa_embedder(config)

