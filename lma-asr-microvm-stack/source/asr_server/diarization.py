"""Streaming speaker diarization ("who spoke") layered onto the ASR timeline.

Answers *who* said each transcript segment, on the **same audio timeline** the
recogniser already decodes, so speaker labels cannot drift away from the text.

Why not ``sherpa_onnx.OfflineSpeakerDiarization``
------------------------------------------------
sherpa-onnx ships a pyannote-segmentation + clustering pipeline, but its only
entrypoint is ``OfflineSpeakerDiarization.process(samples)``, which requires the
**entire utterance up front** and re-clusters every speaker from scratch on each
call. That is incompatible with this project's contract: the server streams
``partial``/``final`` messages as audio arrives (``design.md`` §5), and re-running
a whole-file clustering pass per chunk would be quadratic in session length *and*
would let previously reported speaker labels change retroactively.

This module therefore uses the **incremental** half of the same toolbox — a
speaker-embedding extractor (NVIDIA TitaNet / WeSpeaker / 3D-Speaker export)
plus online assignment against a per-session registry of speaker centroids.
Embedding one segment costs ~10 ms per second of audio on CPU (RTF ~0.01,
measured with ``nemo_en_titanet_small.onnx``), so it is comfortably real-time
next to the Nemotron decode.

Zero-drift by construction
--------------------------
The pairing problem called out in the project discussion — matching an external
diariser's turns against a separately-produced transcript, where mismatched VAD,
network batching, and retroactive word fixups make the two timelines diverge —
is avoided structurally here: :class:`DiarizingRecognizer` **decorates** an
existing :class:`~asr_server.recognizer.Recognizer` and attributes a speaker to
each segment using *the very same PCM samples and the very same segment
boundaries the recogniser itself produced*. There is no second VAD, no second
clock, and no alignment heuristic to get wrong.

Design fit
----------
* Implements the same :class:`~asr_server.recognizer.Recognizer` /
  :class:`~asr_server.recognizer.RecognizerEngine` interfaces, so it composes
  with **either** engine (streaming Nemotron or the VAD-segmented offline
  ``accurate`` path) and the WebSocket server drives it through one seam.
* The heavy embedding model is built ONCE and shared (:class:`DiarizingEngine`);
  each session gets only a cheap per-connection :class:`SpeakerRegistry`, so
  speaker identities never leak between connections.
* Speaker assignment is **pure stdlib** (cosine similarity over running
  centroids) and lives in :class:`SpeakerRegistry`; only embedding *extraction*
  touches native code, behind the tiny :class:`SpeakerEmbedder` protocol.

Testability without model weights
---------------------------------
Mirrors :mod:`asr_server.vad` / :mod:`asr_server.recognizer`: the recogniser
depends only on the :class:`SpeakerEmbedder` protocol and takes it by injection,
so tests supply a scripted fake embedder and exercise the whole assignment
policy with no ``sherpa_onnx`` / ``numpy`` wheels and no weights. Production uses
:func:`create_sherpa_embedder`, which lazily imports ``sherpa_onnx`` only when a
real extractor is actually constructed (fail-closed until provisioned).
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import sys
import threading
from array import array
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from asr_server.recognizer import (
    DEFAULT_MODEL_DIR,
    Event,
    Recognizer,
    RecognizerEngine,
    SessionConfig,
    WordTiming,
)
from asr_server.segmentation import (
    TurnDetector,
    build_segmentation_config,
    create_onnx_backend,
)

_LOG = logging.getLogger(__name__)

__all__ = [
    "SpeakerEmbedder",
    "SpeakerRegistry",
    "pcm16_to_float32",
    "SpeakerEmbedderConfig",
    "DiarizationConfig",
    "DiarizingRecognizer",
    "DiarizingEngine",
    "build_diarization_config",
    "create_sherpa_embedder",
    "create_diarizing_engine",
    "diarization_enabled",
]

# Speaker-embedding model default filename + env override, resolved under the
# model dir (alongside the ASR weights baked into the image).
_EMBEDDING_MODEL_ENV = "ASR_SPEAKER_MODEL"
_EMBEDDING_MODEL_FILENAME = "speaker_embedding.onnx"

# Turns diarization on for the whole server process (one baked config per image,
# mirroring how ``$ASR_ENGINE`` selects which recogniser was baked). Unset (or
# ``auto``) means "enable iff the speaker model was actually baked in" — see
# :func:`diarization_enabled` for why that is the default.
_DIARIZE_ENV = "ASR_DIARIZE"

# Explicit truthy / falsey spellings for ``$ASR_DIARIZE``; anything else (including
# unset and the literal ``auto``) selects presence-based auto-detection.
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSEY = frozenset({"0", "false", "no", "off"})

# 16-bit signed PCM full-scale magnitude (normalise samples to [-1, 1)). Kept in
# lock-step with the sibling engine modules (same wire contract); duplicated
# rather than importing a private symbol across modules.
_INT16_FULL_SCALE = 32768.0

# Speaker labels are ``spk_0``, ``spk_1``, ... in first-heard order. Stable for
# the life of a session and never renumbered retroactively.
_SPEAKER_PREFIX = "spk_"


# --- Injected embedding surface ---------------------------------------------


class SpeakerEmbedder(Protocol):
    """Minimal speaker-embedding surface the diariser drives.

    Wraps one (possibly shared) embedding model. Kept intentionally tiny so a
    scripted fake can stand in for the real extractor in tests (no weights).
    """

    @property
    def dim(self) -> int:
        """Dimension of the embedding vectors this embedder produces."""
        ...

    def embed(self, sample_rate: int, samples: Sequence[float]) -> list[float]:
        """Return one fixed-length speaker embedding for ``samples`` in [-1, 1)."""
        ...


# --- PCM helpers ------------------------------------------------------------


def pcm16_to_float32(pcm: bytes) -> list[float]:
    """Decode little-endian 16-bit signed PCM bytes to floats in [-1, 1).

    Pure stdlib (no numpy) so the diariser stays importable and testable without
    the ARM inference wheels installed. Public because ``embed`` mode in
    :mod:`asr_server.ws_server` decodes calibration segments the same way, and
    reaching into another module's private helper is worse than naming this one.
    """
    if len(pcm) % 2 != 0:
        raise ValueError("PCM byte length must be even for 16-bit samples")
    samples = array("h")
    samples.frombytes(pcm)
    # ``array('h')`` uses native byte order; the wire format is little-endian.
    if sys.byteorder != "little":
        samples.byteswap()
    return [s / _INT16_FULL_SCALE for s in samples]


# Retained for the module's own historical callers and tests.
_pcm16_to_float32 = pcm16_to_float32


# --- Speaker registry (pure stdlib) -----------------------------------------


def _l2_norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(v * v for v in vector))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity in ``[-1, 1]``; ``0.0`` if either vector is degenerate."""
    na = _l2_norm(a)
    nb = _l2_norm(b)
    if na == 0.0 or nb == 0.0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    return dot / (na * nb)


class SpeakerRegistry:
    """Per-session online speaker identity store — the assignment policy.

    Holds one running **centroid** per speaker heard so far and assigns an
    incoming embedding to the closest centroid whose cosine similarity clears
    ``threshold``; otherwise it mints a new speaker. Deliberately pure stdlib (no
    numpy, no native clustering) so the whole policy — the part that decides what
    the client actually sees — is unit-testable without model weights.

    Why a running centroid rather than a single enrolment vector: a speaker's
    embeddings vary across utterances (loudness, phonetic content, channel), so
    averaging every accepted embedding into the centroid tightens the identity as
    the conversation proceeds and keeps later matches stable. (sherpa-onnx's own
    ``SpeakerEmbeddingManager`` cannot do this — its ``add()`` refuses an existing
    name, so a centroid could only be refreshed by remove-then-re-add; tracking it
    directly is both cheaper and testable.)

    ``max_speakers`` bounds the identity count for a known conversation size (a
    2-party call sets ``max_speakers=2``). Once the cap is reached a new embedding
    is force-assigned to its best match rather than minting an unbounded stream of
    spurious speakers — bounded memory and bounded label churn. ``0`` means
    unbounded (discover speakers as they appear).

    Labels are ``spk_0``, ``spk_1``, ... in **first-heard order**, assigned once
    and never renumbered, so a label already sent to a client stays valid.
    """

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        max_speakers: int = 0,
        require_corroboration: bool = False,
    ) -> None:
        if not -1.0 <= threshold <= 1.0:
            raise ValueError("threshold must be in [-1, 1] (cosine similarity)")
        if max_speakers < 0:
            raise ValueError("max_speakers must be >= 0 (0 means unbounded)")
        self._threshold = threshold
        self._max_speakers = max_speakers
        self._require_corroboration = require_corroboration
        # A single dissimilar embedding is not evidence of a new person. Held here
        # until a second one agrees with it (see assign).
        self._pending: list[float] | None = None
        # Per speaker: the component-wise SUM of accepted embeddings plus the
        # count, so the centroid is a true running mean without storing history.
        self._sums: dict[str, list[float]] = {}
        self._counts: dict[str, int] = {}
        # Insertion order of labels doubles as first-heard order.
        self._labels: list[str] = []

    @property
    def num_speakers(self) -> int:
        """How many distinct speakers have been identified this session."""
        return len(self._labels)

    @property
    def speakers(self) -> list[str]:
        """Speaker labels in first-heard order."""
        return list(self._labels)

    def centroid(self, speaker: str) -> list[float]:
        """The current mean embedding for ``speaker`` (empty if unknown)."""
        total = self._sums.get(speaker)
        count = self._counts.get(speaker, 0)
        if total is None or count == 0:
            return []
        return [v / count for v in total]

    def score(self, speaker: str, embedding: Sequence[float]) -> float:
        """Cosine similarity of ``embedding`` to ``speaker``'s centroid."""
        return _cosine(embedding, self.centroid(speaker))

    def assign(self, embedding: Sequence[float]) -> str:
        """Return the speaker label for ``embedding``, minting one if needed.

        Picks the best-scoring existing centroid. That match wins if it clears
        ``threshold``. Otherwise a new speaker is minted, subject to two guards:

        * ``max_speakers``, when set, is a hard promise about how many identities
          can appear — at the cap the best match wins regardless of score.
        * ``require_corroboration`` withholds the first dissimilar embedding
          (see :meth:`_mint_with_corroboration`).
        """
        if len(embedding) == 0:
            raise ValueError("embedding must be non-empty")

        best_label: str | None = None
        best_score = -2.0  # below the cosine minimum, so any real score wins
        for label in self._labels:
            score = self.score(label, embedding)
            if score > best_score:
                best_score = score
                best_label = label

        if best_label is not None and (
            best_score >= self._threshold
            or (self._max_speakers and len(self._labels) >= self._max_speakers)
        ):
            self._pending = None
            self._accumulate(best_label, embedding)
            return best_label

        if best_label is not None and self._require_corroboration:
            corroborated = self._mint_with_corroboration(embedding)
            if not corroborated:
                # Attribute to the closest existing speaker for now WITHOUT folding
                # this embedding into that centroid: it may well belong to someone
                # new, and polluting the centroid would hurt both identities.
                return best_label

        return self._mint(embedding)

    def _mint_with_corroboration(self, embedding: Sequence[float]) -> bool:
        """Whether a second dissimilar-but-self-consistent embedding has arrived.

        One embedding that matches nobody is weak evidence: a short or noisy
        utterance from a *known* speaker scores just as low. Measured on a real
        single-speaker meeting, that produced eight identities for one person. So
        the first outlier is only remembered; a new speaker is minted when a later
        outlier resembles it, which is what a genuinely new voice looks like.

        The cost is that a real new speaker's first utterance is attributed to the
        closest existing speaker.

        MEASURED, and the reason this is OFF by default: replaying real meeting
        audio through this registry, corroboration cut a single speaker from 8
        identities to 2 at the (too high) 0.5 threshold — but with the threshold set
        correctly for the embedder it changed nothing on that meeting and dropped
        two-speaker attribution purity from 100% to 80%, and at 0.5 it merged two
        genuinely different speakers into one label. Set the threshold for your
        embedder first; reach for this only when embeddings are known to be noisy
        (narrowband audio, a weaker embedder) and phantom speakers persist.
        """
        pending = self._pending
        if pending is not None and _cosine(embedding, pending) >= self._threshold:
            self._pending = None
            return True
        self._pending = list(embedding)
        return False

    def _mint(self, embedding: Sequence[float]) -> str:
        label = f"{_SPEAKER_PREFIX}{len(self._labels)}"
        self._labels.append(label)
        self._sums[label] = [0.0] * len(embedding)
        self._counts[label] = 0
        self._accumulate(label, embedding)
        self._pending = None
        return label

    def _accumulate(self, speaker: str, embedding: Sequence[float]) -> None:
        """Fold ``embedding`` into ``speaker``'s running centroid."""
        total = self._sums[speaker]
        if len(total) != len(embedding):
            # A dimension change mid-session means a different model produced this
            # vector; refuse rather than silently corrupt the centroid.
            raise ValueError(
                f"embedding dimension {len(embedding)} does not match "
                f"speaker {speaker!r} centroid dimension {len(total)}"
            )
        for i, value in enumerate(embedding):
            total[i] += value
        self._counts[speaker] += 1


# --- Config -----------------------------------------------------------------


@dataclass
class SpeakerEmbedderConfig:
    """Path + runtime knobs for a sherpa-onnx speaker-embedding model (NFR5).

    Defaults target the embedding export laid down under ``models/`` by
    ``scripts/download_model.sh --engine speaker``.
    """

    model: Path
    sample_rate: int = 16000
    num_threads: int = 1
    provider: str = "cpu"


@dataclass
class DiarizationConfig:
    """Diarization knobs, resolved from env + the negotiated session config.

    ``threshold`` is the cosine similarity above which an embedding is treated as
    an already-known speaker. Higher values split more eagerly (more speakers,
    risk of splitting one person across labels); lower values merge more eagerly
    (fewer speakers, risk of collapsing two people). ``0.5`` is the sherpa-onnx
    speaker-identification default and a sane starting point for 16 kHz speech.

    ``max_speakers`` caps the identity count (``0`` = unbounded); set it when the
    conversation size is known (e.g. ``2`` for an agent/customer call).

    ``min_segment_ms`` is the shortest span worth embedding: speaker embeddings
    computed from a very short slice are unreliable, so a shorter segment reuses
    the previous segment's speaker (conversational continuity) rather than
    inventing a spurious identity from noise.
    """

    embedder: SpeakerEmbedderConfig
    threshold: float = 0.5
    max_speakers: int = 0
    min_segment_ms: int = 400
    # Off by default: measured on real meeting audio, it only helps when the
    # threshold is wrong for the embedder, and at a too-high threshold it merged
    # two different speakers into one. See SpeakerRegistry.assign.
    require_corroboration: bool = False
    # Split one endpointed utterance into a row per speaker turn, using the baked
    # segmentation model. Inert when that model is absent.
    split_on_speaker_change: bool = True
    min_turn_ms: int = 700
    # Close a row as soon as a speaker change is confirmed, instead of waiting for
    # endpointing silence. Endpointing is what previously separated speakers, so two
    # people talking without a gap shared one row until somebody stopped for
    # ``endpointing_ms``; with this on, the speaker change is the primary boundary
    # and the pause is only a backstop.
    live_turn_cut: bool = True
    # How often to look for a boundary in the open utterance, in audio time. Each
    # check is one segmentation-model window, so this bounds the added inference.
    turn_cut_interval_ms: int = 1000
    # Close a row after this much unbroken speech even with no boundary found, so a
    # monologue does not sit as one unlabelled block. 0 disables it.
    max_open_segment_ms: int = 20000


def diarization_enabled(speaker_model: str | Path | None = None) -> bool:
    """Whether this server process should run speaker diarization.

    Resolution order:

    1. An explicit ``$ASR_DIARIZE`` of ``1``/``true``/``yes``/``on`` forces it ON,
       and ``0``/``false``/``no``/``off`` forces it OFF. Forcing it ON without a
       baked model is a deliberate fail-loud: engine construction then raises a
       clear ``RuntimeError`` at startup rather than silently degrading.
    2. Otherwise (unset, empty, or ``auto``) diarization is enabled **iff the
       speaker-embedding model is actually present on disk**.

    Presence-based auto-detection is the default because the MicroVM image build
    API (``create-microvm-image``) accepts only a code artifact, base image, build
    role, and resources — there is **no** ``--build-arg`` passthrough, so a build
    ARG in the Dockerfile cannot be set per build. Whether the model was staged
    into the build context is therefore the only signal that actually survives to
    the remote build, and it is exactly the signal that matters: an image either
    contains the speaker model or it does not. ``scripts/build_image.sh --diarize``
    stages it; nothing else needs to be threaded through.
    """
    raw = os.environ.get(_DIARIZE_ENV, "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSEY:
        return False
    model = (
        Path(speaker_model)
        if speaker_model is not None
        else build_diarization_config().embedder.model
    )
    return model.is_file()


def build_diarization_config(
    *,
    model_dir: str | Path | None = None,
    sample_rate: int = 16000,
    speaker_model: str | Path | None = None,
    threshold: float | None = None,
    max_speakers: int | None = None,
    min_segment_ms: int | None = None,
    require_corroboration: bool | None = None,
    split_on_speaker_change: bool | None = None,
    live_turn_cut: bool | None = None,
    turn_cut_interval_ms: int | None = None,
    max_open_segment_ms: int | None = None,
    num_threads: int | None = None,
) -> DiarizationConfig:
    """Resolve a :class:`DiarizationConfig` from env + optional overrides (NFR5).

    Mirrors :func:`asr_server.recognizer.build_model_config` so the ASR server and
    the build-time warmup resolve the embedding model identically (no drift by
    construction). Precedence (highest first): the explicit argument, then the
    matching env var (``ASR_SPEAKER_MODEL`` / ``ASR_SPEAKER_THRESHOLD`` /
    ``ASR_MAX_SPEAKERS`` / ``ASR_MIN_SEGMENT_MS`` / ``ASR_NUM_THREADS``), then the default —
    ``<model_dir>/speaker_embedding.onnx`` for the model, with ``model_dir``
    falling back to ``$ASR_MODEL_DIR`` then :data:`DEFAULT_MODEL_DIR`.
    """
    root = (
        Path(model_dir)
        if model_dir is not None
        else Path(os.environ.get("ASR_MODEL_DIR", DEFAULT_MODEL_DIR))
    )
    if speaker_model is not None:
        model_path = Path(speaker_model)
    else:
        env_value = os.environ.get(_EMBEDDING_MODEL_ENV)
        model_path = Path(env_value) if env_value else root / _EMBEDDING_MODEL_FILENAME

    if num_threads is None:
        num_threads = int(os.environ.get("ASR_NUM_THREADS", "1"))
    if threshold is None:
        threshold = float(os.environ.get("ASR_SPEAKER_THRESHOLD", "0.5"))
    if max_speakers is None:
        max_speakers = int(os.environ.get("ASR_MAX_SPEAKERS", "0"))
    if min_segment_ms is None:
        min_segment_ms = int(
            os.environ.get("ASR_MIN_SEGMENT_MS", str(DiarizationConfig.min_segment_ms))
        )
    if require_corroboration is None:
        require_corroboration = (
            os.environ.get("ASR_REQUIRE_CORROBORATION", "0").strip().lower() in _TRUTHY
        )
    if split_on_speaker_change is None:
        split_on_speaker_change = (
            os.environ.get("ASR_SPLIT_ON_SPEAKER_CHANGE", "1").strip().lower() in _TRUTHY
        )
    if live_turn_cut is None:
        live_turn_cut = os.environ.get("ASR_LIVE_TURN_CUT", "1").strip().lower() in _TRUTHY
    if turn_cut_interval_ms is None:
        turn_cut_interval_ms = int(
            os.environ.get("ASR_TURN_CUT_INTERVAL_MS", str(DiarizationConfig.turn_cut_interval_ms))
        )
    if max_open_segment_ms is None:
        max_open_segment_ms = int(
            os.environ.get("ASR_MAX_OPEN_SEGMENT_MS", str(DiarizationConfig.max_open_segment_ms))
        )

    return DiarizationConfig(
        embedder=SpeakerEmbedderConfig(
            model=model_path,
            sample_rate=sample_rate,
            num_threads=num_threads,
        ),
        threshold=threshold,
        max_speakers=max_speakers,
        min_segment_ms=min_segment_ms,
        require_corroboration=require_corroboration,
        split_on_speaker_change=split_on_speaker_change,
        live_turn_cut=live_turn_cut,
        turn_cut_interval_ms=turn_cut_interval_ms,
        max_open_segment_ms=max_open_segment_ms,
    )


@dataclass(frozen=True)
class _SegmentPart:
    """One speaker turn carved out of a finalized segment."""

    start: float | None
    end: float | None
    text: str
    words: list[WordTiming]
    samples: list[float]


# --- Diarizing recogniser (decorator) ---------------------------------------


class DiarizingRecognizer(Recognizer):
    """Wraps a :class:`~asr_server.recognizer.Recognizer` and labels each segment.

    Transparently forwards PCM to the inner recogniser and stamps a ``speaker``
    onto the events it emits. Because attribution reuses the inner recogniser's
    own ``start``/``end`` and the same buffered samples, the speaker label and the
    text are derived from one timeline — the drift that plagues bolting a separate
    diariser onto a separate ASR service simply cannot arise here.

    Per event kind:

    * ``final`` — the segment is closed, so its exact ``[start, end)`` samples are
      sliced from the rolling buffer, embedded once, and assigned a speaker via
      the per-session :class:`SpeakerRegistry`. This is the authoritative label.
    * ``partial`` — the segment is still open and its audio is incomplete, so no
      embedding is computed (embedding a fragment would be both wasteful and
      unstable). The partial carries the **provisional** label of the current
      speaker (the last one identified), letting a UI colour interim text
      immediately; the following ``final`` confirms or corrects it.

    A segment shorter than ``min_segment_ms`` is not embedded: it inherits the
    previous speaker rather than minting an identity from an unreliable vector.
    An embedding failure is **never fatal** — the event passes through with no
    speaker (``None``) so a diarization fault degrades to plain transcription
    rather than dropping the transcript.
    """

    def __init__(
        self,
        inner: Recognizer,
        embedder: SpeakerEmbedder,
        *,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        max_speakers: int = 0,
        min_segment_ms: int = 400,
        max_buffer_ms: int = 30000,
        require_corroboration: bool = False,
        turn_detector: TurnDetector | None = None,
        split_on_speaker_change: bool = True,
        live_turn_cut: bool = True,
        turn_cut_interval_ms: int = 1000,
        max_open_segment_ms: int = 20000,
        min_turn_ms: int = 700,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if min_segment_ms < 0:
            raise ValueError("min_segment_ms must be >= 0")
        if max_buffer_ms <= 0:
            raise ValueError("max_buffer_ms must be positive")
        self._inner = inner
        self._embedder = embedder
        self._sample_rate = sample_rate
        self._registry = SpeakerRegistry(
            threshold=threshold,
            max_speakers=max_speakers,
            require_corroboration=require_corroboration,
        )
        self._min_segment_samples = sample_rate * min_segment_ms // 1000
        # Hard ceiling on retained audio. A ``final`` prunes everything up to its
        # end, so this only bites if the inner engine never finalizes (e.g. a very
        # long unbroken utterance) — bounding it keeps a fixed-memory MicroVM safe
        # (R7 spirit) at the cost of attributing an over-long segment from its tail.
        self._max_buffer_samples = sample_rate * max_buffer_ms // 1000

        # Rolling float buffer of audio not yet consumed by a labelled segment,
        # with the absolute sample index of ``_buffer[0]`` so event timestamps
        # (seconds on the inner recogniser's timeline) slice back to samples.
        self._buffer: list[float] = []
        self._buffer_base = 0
        # Most recently identified speaker: labels provisional partials and covers
        # segments too short to embed.
        self._current_speaker: str | None = None
        self._turn_detector = turn_detector if split_on_speaker_change else None
        # Splitting one inner segment into several finals consumes extra wire
        # segment numbers, so outbound numbering runs ahead of the inner
        # recogniser's by this much. Partials are renumbered with it too, which is
        # what keeps a partial and the first final of its segment on the same row.
        self._segment_offset = 0

        self._live_turn_cut = live_turn_cut
        self._turn_cut_interval = turn_cut_interval_ms / 1000.0
        self._max_open_segment = max_open_segment_ms / 1000.0
        # How long a change must persist before it counts. Shorter than this is a
        # back-channel ("mhm") or a model flicker, and cutting on one produces
        # unreadable one-word rows.
        self._min_turn = min_turn_ms / 1000.0
        # How much of the OPEN inner segment has already been emitted as finals by a
        # live cut: the absolute time cut to, and the text sent. The inner recogniser
        # keeps reporting the whole segment, so both are needed to emit only what is
        # new — the time to slice audio and words, the text to shorten partials.
        self._committed_end: float | None = None
        self._committed_text = ""
        # Audio time of the last boundary search, so the segmentation model runs about
        # once per turn_cut_interval_ms rather than on every hypothesis change.
        self._last_cut_check = 0.0

    @property
    def registry(self) -> SpeakerRegistry:
        """The per-session speaker registry (identities heard so far)."""
        return self._registry

    @property
    def current_speaker(self) -> str | None:
        """The most recently identified speaker, if any."""
        return self._current_speaker

    def accept_pcm(self, pcm: bytes) -> list[Event]:
        # Decode first so a malformed frame raises before any state is touched
        # (the server maps ValueError to a BAD_ENCODING wire error).
        samples = pcm16_to_float32(pcm)
        events = self._inner.accept_pcm(pcm)
        self._buffer.extend(samples)
        self._trim_buffer()
        # Look for a cut BEFORE labelling this chunk's events: a cut consumes a wire
        # segment number, and doing it first means the partial that follows already
        # carries the new number and the shortened text, instead of being written to
        # the old row and then shrinking when the cut lands on top of it.
        cuts = self._maybe_cut_open_segment()
        return cuts + self._label_all(events)

    def flush(self) -> list[Event]:
        return self._label_all(self._inner.flush())

    def _label_all(self, events: Sequence[Event]) -> list[Event]:
        labelled: list[Event] = []
        for event in events:
            labelled.extend(self._label(event))
        return labelled

    def _label(self, event: Event) -> list[Event]:
        """Stamp speakers onto one inner event, splitting it on a speaker change."""
        if event.kind != "final":
            text = self._uncommitted_text(event.text)
            if not text:
                # Everything in this hypothesis has already been emitted by a cut.
                return []
            # Segment still open, so no embedding and NO speaker: a partial used to
            # carry the last identified speaker, which named the previous person
            # until the final corrected it. Leaving it unset shows the plain channel
            # name instead, which is honest about not knowing yet.
            return [
                replace(
                    event,
                    segment=event.segment + self._segment_offset,
                    text=text,
                    speaker=None,
                )
            ]

        event = self._uncommitted_final(event)
        if event is None:
            self._reset_commit()
            return []

        segment = self._slice(event.start, event.end)
        # Consume the segment's audio: segments are monotonic and non-overlapping,
        # so nothing at or before this end can be needed again (bounds memory).
        if event.end is not None:
            self._prune_before(event.end)

        parts = self._parts(event, segment)
        labelled: list[Event] = []
        for index, part in enumerate(parts):
            speaker = self._identify(part.samples)
            if speaker is not None:
                self._current_speaker = speaker
            labelled.append(
                replace(
                    event,
                    segment=event.segment + self._segment_offset + index,
                    start=part.start,
                    end=part.end,
                    text=part.text,
                    words=part.words or None,
                    speaker=speaker,
                )
            )
        self._segment_offset += len(parts) - 1
        self._reset_commit()
        return labelled

    # --- Cutting a row before the utterance ends ----------------------------

    def _now(self) -> float:
        """Absolute audio time of the end of the buffer, in seconds."""
        return (self._buffer_base + len(self._buffer)) / self._sample_rate

    def _reset_commit(self) -> None:
        self._committed_end = None
        self._committed_text = ""
        self._last_cut_check = 0.0

    def _uncommitted_text(self, text: str) -> str:
        """``text`` with any already-emitted prefix removed.

        The inner recogniser reports the whole open segment every time, so after a
        cut its hypothesis still starts with text that is already on a settled row.
        A prefix match is enough and costs nothing; when the decoder revises the
        prefix the match fails and the whole hypothesis is shown, which is better
        than dropping words on the assumption it did not.
        """
        if not self._committed_text:
            return text
        if text.startswith(self._committed_text):
            return text[len(self._committed_text) :].strip()
        return text

    def _uncommitted_final(self, event: Event) -> Event | None:
        """The part of a closed segment not already emitted by a live cut."""
        if self._committed_end is None:
            return event
        words = [word for word in (event.words or []) if word.s >= self._committed_end]
        text = self._uncommitted_text(event.text)
        if not text and not words:
            return None
        return replace(
            event,
            start=self._committed_end,
            text=text or " ".join(word.w for word in words),
            words=words or None,
        )

    def _maybe_cut_open_segment(self) -> list[Event]:
        """Close a row now if the speaker has changed inside the open utterance.

        Endpointing is what used to separate speakers, so two people talking without
        a gap shared one row until somebody paused for ``endpointing_ms``. Here the
        speaker change is the boundary and the pause is only a backstop.

        Deliberately done in this layer rather than by forcing the recogniser to
        endpoint early: resetting the decoder mid-utterance costs it the left context
        it is using to decode, and text quality is already the weaker half of this
        engine. Cutting here leaves the decode untouched.
        """
        if not self._live_turn_cut or self._turn_detector is None:
            return []
        words = self._inner.current_words()
        if len(words) < 2:
            return []

        start = self._committed_end if self._committed_end is not None else words[0].s
        now = self._now()
        open_sec = now - start
        min_segment_sec = self._min_segment_samples / self._sample_rate
        # Both sides of a cut have to be long enough to embed, or the cut produces a
        # row nobody can attribute.
        if open_sec < 2 * min_segment_sec:
            return []
        if now - self._last_cut_check < self._turn_cut_interval:
            return []
        self._last_cut_check = now

        cut = self._find_live_cut(start, now, open_sec, min_segment_sec)
        if cut is None:
            return []
        return self._commit(words, start, cut)

    def _find_live_cut(
        self, start: float, now: float, open_sec: float, min_segment_sec: float
    ) -> float | None:
        """Absolute time to cut the open segment at, or None to keep waiting."""
        samples = self._slice(start, now)
        if not samples:
            return None
        assert self._turn_detector is not None  # guarded by the caller
        try:
            detected = self._turn_detector.detect_samples(samples)
        except Exception:  # noqa: BLE001 - detection must never break transcription
            _LOG.warning("live turn detection failed at %.2fs", now, exc_info=True)
            return None

        # A boundary is only actionable once enough audio has followed it to show the
        # change persisted, and once enough precedes it to embed. Take the LATEST such
        # boundary so as much as possible settles in one row.
        usable = [
            boundary
            for boundary in detected.boundaries
            if boundary >= min_segment_sec and (open_sec - boundary) >= self._min_turn
        ]
        if usable:
            return start + max(usable)

        # No boundary, but the row cannot stay open forever: a long monologue would
        # sit as one unlabelled block. Settle what is already in the past.
        if self._max_open_segment > 0 and open_sec >= self._max_open_segment:
            return start + self._max_open_segment
        return None

    def _commit(self, words: list[WordTiming], start: float, cut: float) -> list[Event]:
        """Emit the open segment up to ``cut`` as a final, and record it as sent."""
        prefix = [word for word in words if word.s >= start and word.e <= cut]
        if not prefix:
            return []
        end = prefix[-1].e
        samples = self._slice(start, end)
        if len(samples) < self._min_segment_samples:
            return []

        speaker = self._identify(samples)
        if speaker is not None:
            self._current_speaker = speaker
        text = " ".join(word.w for word in prefix)
        segment = self._inner.current_segment() + self._segment_offset

        self._committed_end = end
        self._committed_text = f"{self._committed_text} {text}".strip()
        self._segment_offset += 1
        _LOG.info(
            "live turn cut at %.2fs: emitting %d word(s) as segment %d (speaker %s)",
            end,
            len(prefix),
            segment,
            speaker,
        )
        return [
            Event(
                kind="final",
                segment=segment,
                text=text,
                start=start,
                end=end,
                words=prefix,
                speaker=speaker,
            )
        ]



    def _parts(self, event: Event, segment: list[float]) -> list[_SegmentPart]:
        """One part per speaker turn in the closed segment.

        Cuts snap to word boundaries, so no word is split across two rows, and a
        cut that would leave a part too short to embed is dropped rather than
        producing a row nobody can attribute.
        """
        whole = _SegmentPart(
            start=event.start,
            end=event.end,
            text=event.text,
            words=list(event.words or []),
            samples=segment,
        )
        words = event.words or []
        if self._turn_detector is None:
            return [whole]
        span = f"{event.start or 0.0:.2f}-{event.end or 0.0:.2f}"
        if len(words) < 2 or event.start is None or event.end is None:
            _LOG.info(
                "turn detection skipped: segment %s has %d word timing(s); text cannot "
                "be cut without them",
                span,
                len(words),
            )
            return [whole]
        if len(segment) < 2 * self._min_segment_samples:
            _LOG.info(
                "turn detection skipped: segment %s is %.2fs, under 2x min_segment (%.2fs)",
                span,
                len(segment) / self._sample_rate,
                2 * self._min_segment_samples / self._sample_rate,
            )
            return [whole]

        try:
            detected = self._turn_detector.detect_samples(segment)
        except Exception:  # noqa: BLE001 - detection must never break transcription
            _LOG.warning("turn detection failed for segment %s", span, exc_info=True)
            return [whole]
        if not detected.boundaries:
            _LOG.info(
                "turn detection: segment %s (%.2fs, %d words) - one speaker, %d overlap span(s)",
                span,
                len(segment) / self._sample_rate,
                len(words),
                len(detected.overlaps),
            )
            return [whole]

        cuts: list[int] = []
        for boundary in detected.boundaries:
            absolute = event.start + boundary
            index = min(
                range(1, len(words)),
                key=lambda position: abs(words[position].s - absolute),
            )
            if index not in cuts:
                cuts.append(index)
        cuts.sort()

        groups: list[list[WordTiming]] = []
        previous = 0
        for cut in cuts:
            groups.append(words[previous:cut])
            previous = cut
        groups.append(words[previous:])

        parts: list[_SegmentPart] = []
        for index, group in enumerate(groups):
            if not group:
                continue
            start = event.start if index == 0 else group[0].s
            end = event.end if index == len(groups) - 1 else group[-1].e
            samples = self._sub_samples(segment, event.start, start, end)
            too_short = len(samples) < self._min_segment_samples and len(groups) > 1
            if too_short and parts:
                merged = parts[-1]
                parts[-1] = _SegmentPart(
                    start=merged.start,
                    end=end,
                    text=f"{merged.text} {' '.join(word.w for word in group)}".strip(),
                    words=merged.words + group,
                    samples=self._sub_samples(segment, event.start, merged.start, end),
                )
                continue
            parts.append(
                _SegmentPart(
                    start=start,
                    end=end,
                    text=" ".join(word.w for word in group),
                    words=group,
                    samples=samples,
                )
            )
        _LOG.info(
            "turn detection: segment %s (%.2fs, %d words) - %d boundary(ies) at %s, "
            "%d overlap span(s), emitting %d row(s)",
            span,
            len(segment) / self._sample_rate,
            len(words),
            len(detected.boundaries),
            [round(value, 2) for value in detected.boundaries],
            len(detected.overlaps),
            len(parts) or 1,
        )
        return parts or [whole]

    def _sub_samples(
        self, segment: list[float], segment_start: float | None, start: float, end: float
    ) -> list[float]:
        base = segment_start or 0.0
        first = max(0, round((start - base) * self._sample_rate))
        last = min(len(segment), round((end - base) * self._sample_rate))
        return segment[first:max(first, last)]

    def _identify(self, samples: list[float]) -> str | None:
        """Embed one span's audio and resolve it to a speaker label."""
        if len(samples) < self._min_segment_samples:
            # Too short for a trustworthy embedding: keep the conversation's
            # current speaker rather than inventing one from noise.
            return self._current_speaker
        try:
            embedding = self._embedder.embed(self._sample_rate, samples)
        except Exception:  # noqa: BLE001 - diarization must never break transcription
            return None
        if not embedding:
            return None
        try:
            return self._registry.assign(embedding)
        except ValueError:
            # A dimension mismatch (wrong/changed model) must not kill the stream.
            return None

    def _slice(self, start: float | None, end: float | None) -> list[float]:
        """Return the buffered samples spanning ``[start, end)`` seconds."""
        if start is None or end is None or end <= start:
            return []
        start_idx = max(0, round(start * self._sample_rate) - self._buffer_base)
        end_idx = round(end * self._sample_rate) - self._buffer_base
        start_idx = min(start_idx, len(self._buffer))
        end_idx = min(max(end_idx, start_idx), len(self._buffer))
        return self._buffer[start_idx:end_idx]

    def _prune_before(self, t: float) -> None:
        """Drop buffered samples before absolute time ``t`` (seconds)."""
        self._prune_to_index(round(t * self._sample_rate) - self._buffer_base)

    def _prune_to_index(self, buf_idx: int) -> None:
        """Drop ``_buffer[:buf_idx]`` and advance the buffer base accordingly."""
        if buf_idx <= 0:
            return
        buf_idx = min(buf_idx, len(self._buffer))
        del self._buffer[:buf_idx]
        self._buffer_base += buf_idx

    def _trim_buffer(self) -> None:
        """Enforce the retained-audio ceiling (see ``max_buffer_ms``)."""
        excess = len(self._buffer) - self._max_buffer_samples
        if excess > 0:
            self._prune_to_index(excess)


# --- Shared engine ----------------------------------------------------------


class DiarizingEngine(RecognizerEngine):
    """Shared engine decorator: adds speaker labels to any inner engine.

    Composes rather than replaces — it wraps the already-selected streaming or
    ``accurate`` engine, so diarization is orthogonal to which recogniser was
    baked into the image and both paths gain speaker labels through one code path.

    The embedding model is built ONCE (shared, like the ASR model) and every
    :meth:`new_session` mints only a fresh :class:`SpeakerRegistry`, so speaker
    identities are strictly per connection and never leak between sessions, while
    the weights are never re-allocated per connection.

    ``threshold``/``max_speakers`` are genuinely per-session: they live in that
    per-session registry, not in the shared ONNX graph, so a connection may
    negotiate its own conversation size (e.g. ``max_speakers=2``) with no engine
    rebuild. ``sample_rate`` validation stays the inner engine's business.
    """

    def __init__(
        self,
        inner: RecognizerEngine,
        embedder: SpeakerEmbedder,
        config: DiarizationConfig,
        turn_detector: TurnDetector | None = None,
    ) -> None:
        self._inner = inner
        self._embedder = embedder
        self._config = config
        self._turn_detector = turn_detector

    @property
    def inner(self) -> RecognizerEngine:
        """The wrapped recogniser engine (streaming or ``accurate``)."""
        return self._inner

    @property
    def embedder(self) -> SpeakerEmbedder:
        """The shared speaker embedder, for calibration (``embed`` mode).

        Deriving an operating point requires embeddings of known-speaker audio, and
        this process is the only place the embedding model exists. Exposing it keeps
        the statistics — which decide what a client sees — outside the MicroVM where
        they are testable without weights.
        """
        return self._embedder

    @property
    def turn_detector(self) -> TurnDetector | None:
        """The shared speaker-turn detector, when one was baked into the image."""
        return self._turn_detector

    def new_session(self, config: SessionConfig | None = None) -> Recognizer:
        # Delegate first: the inner engine owns sample-rate/endpointing validation
        # and raises SessionConfigError, which must surface unchanged.
        inner_session = self._inner.new_session(config)
        threshold = self._config.threshold
        max_speakers = self._config.max_speakers
        min_segment_ms = self._config.min_segment_ms
        require_corroboration = self._config.require_corroboration
        split_on_speaker_change = self._config.split_on_speaker_change
        live_turn_cut = self._config.live_turn_cut
        turn_cut_interval_ms = self._config.turn_cut_interval_ms
        max_open_segment_ms = self._config.max_open_segment_ms
        sample_rate = self._config.embedder.sample_rate
        if config is not None:
            sample_rate = config.sample_rate
            if config.speaker_threshold is not None:
                threshold = config.speaker_threshold
            if config.max_speakers is not None:
                max_speakers = config.max_speakers
            if config.min_segment_ms is not None:
                min_segment_ms = config.min_segment_ms
            if config.require_corroboration is not None:
                require_corroboration = config.require_corroboration
            if config.split_on_speaker_change is not None:
                split_on_speaker_change = config.split_on_speaker_change
            if config.live_turn_cut is not None:
                live_turn_cut = config.live_turn_cut
            if config.turn_cut_interval_ms is not None:
                turn_cut_interval_ms = config.turn_cut_interval_ms
            if config.max_open_segment_ms is not None:
                max_open_segment_ms = config.max_open_segment_ms
        return DiarizingRecognizer(
            inner_session,
            self._embedder,
            sample_rate=sample_rate,
            threshold=threshold,
            max_speakers=max_speakers,
            min_segment_ms=min_segment_ms,
            require_corroboration=require_corroboration,
            turn_detector=self._turn_detector,
            split_on_speaker_change=split_on_speaker_change,
            live_turn_cut=live_turn_cut,
            turn_cut_interval_ms=turn_cut_interval_ms,
            max_open_segment_ms=max_open_segment_ms,
            min_turn_ms=self._config.min_turn_ms,
        )


# --- Real sherpa-onnx embedder (lazily loaded) ------------------------------


def _load_sherpa() -> Any:
    """Import ``sherpa_onnx`` lazily with a clear error if the wheel is absent."""
    try:
        import sherpa_onnx  # type: ignore[import-untyped]
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "sherpa-onnx is not installed. Install the aarch64 wheel pinned in "
            "requirements.txt to construct a real speaker embedder."
        ) from exc
    return sherpa_onnx


def _load_numpy() -> Any:
    """Import ``numpy`` lazily; only the real embedder needs it."""
    try:
        import numpy
    except ModuleNotFoundError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "numpy is not installed. Install the aarch64 wheel pinned in "
            "requirements.txt to construct a real speaker embedder."
        ) from exc
    return numpy


class _SherpaSpeakerEmbedder:
    """Adapts a **shared** sherpa-onnx ``SpeakerEmbeddingExtractor`` to the protocol.

    The extractor (heavy: ONNX sessions) is shared across every session; each
    :meth:`embed` call drives a fresh per-utterance stream, matching how the
    sibling backends use a shared recognizer with per-call streams. A shared
    ``lock`` serialises the extractor-touching calls so concurrent sessions'
    worker threads never drive it in parallel (design Unchanged #4).
    """

    def __init__(self, extractor: Any, *, lock: Any = None) -> None:
        self._extractor = extractor
        self._lock = lock if lock is not None else contextlib.nullcontext()
        self._dim = int(extractor.dim)

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, sample_rate: int, samples: Sequence[float]) -> list[float]:
        np = _load_numpy()
        with self._lock:
            stream = self._extractor.create_stream()
            stream.accept_waveform(sample_rate, np.asarray(samples, dtype=np.float32))
            stream.input_finished()
            return [float(v) for v in self._extractor.compute(stream)]


def _require_speaker_model(config: SpeakerEmbedderConfig) -> None:
    """Fail closed with a clear error if the embedding model file is missing."""
    if not Path(config.model).is_file():
        raise RuntimeError(
            f"speaker embedding model is missing: {config.model}. Bake it into the "
            "image (scripts/download_model.sh --engine speaker) or set "
            "ASR_SPEAKER_MODEL to the model file."
        )


def create_sherpa_embedder(
    config: SpeakerEmbedderConfig, *, lock: Any = None
) -> SpeakerEmbedder:
    """Construct a real sherpa-onnx speaker embedder from an on-disk ONNX model.

    Fails closed with a ``RuntimeError`` if ``sherpa_onnx`` is unavailable *or* the
    model file is missing, so a bad ``ASR_SPEAKER_MODEL`` path surfaces at startup
    (or build-time warmup) rather than mid-session.
    """
    sherpa = _load_sherpa()
    _require_speaker_model(config)
    extractor_config = sherpa.SpeakerEmbeddingExtractorConfig(
        model=str(config.model),
        num_threads=config.num_threads,
        provider=config.provider,
    )
    try:
        extractor = sherpa.SpeakerEmbeddingExtractor(extractor_config)
    except Exception as exc:
        raise RuntimeError(
            f"failed to load speaker embedding model from {config.model!s}: {exc}"
        ) from exc
    return _SherpaSpeakerEmbedder(extractor, lock=lock)


def create_diarizing_engine(
    inner: RecognizerEngine, config: DiarizationConfig
) -> DiarizingEngine:
    """Wrap ``inner`` with diarization, building the embedding model ONCE.

    Called eagerly at server startup so the embedding weights are resident before
    any connection arrives and are reused across all sessions. Fails closed with a
    ``RuntimeError`` (missing wheel / model file) so startup fails loudly rather
    than a session dying silently mid-connection.
    """
    embedder = create_sherpa_embedder(config.embedder, lock=threading.Lock())
    return DiarizingEngine(inner, embedder, config, turn_detector=create_turn_detector(config))


def create_turn_detector(config: DiarizationConfig) -> TurnDetector | None:
    """Build the shared speaker-turn detector, or ``None`` when it is unavailable.

    Absent weights are not an error: turn detection is an image-build decision, and
    without it the engine keeps its previous behaviour of one speaker per endpointed
    utterance. A load failure is logged and degraded for the same reason.
    """
    if not config.split_on_speaker_change:
        return None
    segmentation = replace(
        build_segmentation_config(),
        sample_rate=config.embedder.sample_rate,
        min_turn_ms=config.min_turn_ms,
        num_threads=config.embedder.num_threads,
    )
    if not segmentation.model_path.is_file():
        _LOG.info(
            "speaker-turn detection disabled: no segmentation model at %s",
            segmentation.model_path,
        )
        return None
    try:
        detector = TurnDetector(create_onnx_backend(segmentation), segmentation)
    except Exception:  # noqa: BLE001 - degrade to one speaker per utterance
        _LOG.warning("speaker-turn detection unavailable; continuing without it", exc_info=True)
        return None
    _LOG.info(
        "speaker-turn detection enabled: %s (window %.1fs, min turn %dms)",
        segmentation.model_path,
        segmentation.window_sec,
        segmentation.min_turn_ms,
    )
    return detector
