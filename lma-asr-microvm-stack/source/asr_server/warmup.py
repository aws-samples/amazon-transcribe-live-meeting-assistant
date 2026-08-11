"""Build-time model warmup for MicroVM snapshots.

Loads the real recogniser and pushes a short buffer of **synthetic** audio
through ``accept_pcm``/``flush`` so a full decode path executes at ``docker
build`` time. The Firecracker snapshot taken afterwards therefore captures the
model already resident and warm in RAM (API doc §6), so ``/resume`` and the
first real utterance start warm instead of paying a cold model load.

Invoked from the image build::

    python -m asr_server.warmup --model /opt/models

Configuration is entirely path, flag, and environment driven. There are no hardcoded
absolute model paths; the model directory (and, if needed, each individual
model file) is supplied by argument or the same ``ASR_MODEL_*`` environment
variables the server reads (``ws_server._default_recognizer_factory``).

Snapshot safety: this is a *build-time* step and must stay
deterministic. It generates NO per-MicroVM-unique material (session IDs,
secrets, crypto) — the synthetic waveform is a fixed sine tone. All such
unique values are minted after start in the ``/run`` lifecycle hook.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import struct
import sys
from collections.abc import Callable, Sequence

from asr_protocol import Config as _Config

from asr_server.offline_recognizer import (
    OfflineModelConfig,
    SherpaOfflineRecognizer,
    build_offline_model_config,
)
from asr_server.recognizer import (
    DEFAULT_MODEL_DIR,
    Recognizer,
    SherpaModelConfig,
    SherpaOnlineRecognizer,
    build_model_config,
)

__all__ = [
    "RecognizerFactory",
    "OfflineRecognizerFactory",
    "synthetic_pcm",
    "warmup",
    "config_from_args",
    "offline_config_from_args",
    "run",
    "main",
]

_LOG = logging.getLogger("asr_server.warmup")

# Synthetic warmup waveform: a fraction of a second of a fixed low-frequency
# tone at 16 kHz/16-bit/mono. A tone (rather than pure silence) exercises the
# feature front-end + decoder more realistically while staying deterministic.
_DEFAULT_DURATION_MS = 500
_TONE_HZ = 220.0
_TONE_AMPLITUDE = 8000  # well within int16 range; no clipping
# Feed the buffer in small chunks so multiple ``accept_pcm`` calls run, matching
# how the live stream drives the recogniser (20 ms is the server's chunk size).
_CHUNK_MS = 20
_INT16_BYTES = 2

# Builds a real recogniser from a resolved model config; injected in tests.
RecognizerFactory = Callable[[SherpaModelConfig], Recognizer]
# Sibling factory for the offline (accurate) engine, from its own config type.
OfflineRecognizerFactory = Callable[[OfflineModelConfig], Recognizer]


def synthetic_pcm(
    duration_ms: int,
    sample_rate: int,
    *,
    freq_hz: float = _TONE_HZ,
    amplitude: int = _TONE_AMPLITUDE,
) -> bytes:
    """Generate deterministic 16-bit LE mono PCM for a sine tone.

    Deterministic by construction (no RNG): the same arguments always yield the
    same bytes, so the warmup is snapshot-safe.
    """
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    n = int(sample_rate * duration_ms / 1000)
    step = 2.0 * math.pi * freq_hz / sample_rate
    samples = [int(amplitude * math.sin(step * i)) for i in range(n)]
    return struct.pack("<" + "h" * n, *samples)


def warmup(
    recognizer: Recognizer,
    *,
    duration_ms: int = _DEFAULT_DURATION_MS,
    sample_rate: int = 16000,
    chunk_ms: int = _CHUNK_MS,
) -> int:
    """Push synthetic audio through the recogniser to force a full decode.

    Feeds the buffer in ``chunk_ms`` slices via ``accept_pcm`` then ``flush``,
    mirroring the live streaming path. Returns the number of decode events
    produced (informational only — a warm model may legitimately emit zero on a
    meaningless tone; what matters is that the decode path ran without error).
    """
    pcm = synthetic_pcm(duration_ms, sample_rate)
    chunk_bytes = max(1, sample_rate * chunk_ms // 1000) * _INT16_BYTES

    _LOG.info(
        "warmup: feeding %d ms of synthetic %d Hz audio (%d bytes, %d ms chunks)",
        duration_ms,
        sample_rate,
        len(pcm),
        chunk_ms,
    )

    events = 0
    for offset in range(0, len(pcm), chunk_bytes):
        events += len(recognizer.accept_pcm(pcm[offset : offset + chunk_bytes]))
    events += len(recognizer.flush())

    _LOG.info("warmup complete: model resident and warm (%d decode event(s))", events)
    return events


def config_from_args(args: argparse.Namespace) -> SherpaModelConfig:
    """Resolve a :class:`SherpaModelConfig` from CLI arguments and environment.

    Delegates to :func:`recognizer.build_model_config` — the single source of
    truth shared with ``ws_server._default_recognizer_factory`` — so the warmed
    snapshot and the runtime server resolve an *identical* recogniser config
    (same model paths, same ``num_threads`` default, same ``endpointing_ms``
    mapping). ``args.endpointing_ms`` defaults to the wire ``Config`` default, so
    warmup warms exactly what the server builds for a default session.
    """
    return build_model_config(
        model_dir=args.model,
        sample_rate=args.sample_rate,
        endpointing_ms=args.endpointing_ms,
        tokens=args.tokens,
        encoder=args.encoder,
        decoder=args.decoder,
        joiner=args.joiner,
        num_threads=args.num_threads,
    )


def offline_config_from_args(args: argparse.Namespace) -> OfflineModelConfig:
    """Resolve an :class:`OfflineModelConfig` for the ``accurate`` engine.

    Delegates to :func:`offline_recognizer.build_offline_model_config` — the
    single source of truth shared with ``ws_server``'s offline factory — so the
    warmed snapshot and the runtime server resolve an *identical* offline engine
    config (same transducer + Silero VAD paths, same ``num_threads`` default, same
    ``endpointing_ms`` → VAD trailing-silence mapping).
    """
    return build_offline_model_config(
        model_dir=args.model,
        sample_rate=args.sample_rate,
        endpointing_ms=args.endpointing_ms,
        tokens=args.tokens,
        encoder=args.encoder,
        decoder=args.decoder,
        joiner=args.joiner,
        vad_model=args.vad_model,
        num_threads=args.num_threads,
    )


def _default_recognizer_factory(config: SherpaModelConfig) -> Recognizer:
    """Build the real streaming recogniser (fails closed without the wheel)."""
    return SherpaOnlineRecognizer.from_model(config)


def _default_offline_recognizer_factory(config: OfflineModelConfig) -> Recognizer:
    """Build the real offline (accurate) recogniser (fails closed without the wheel)."""
    return SherpaOfflineRecognizer.from_model(config)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m asr_server.warmup",
        description=(
            "Load the ASR model and run a dummy inference so the MicroVM "
            "snapshot captures it warm in RAM."
        ),
    )
    parser.add_argument(
        "-m",
        "--model",
        default=None,
        help=f"model directory (default: $ASR_MODEL_DIR or {DEFAULT_MODEL_DIR})",
    )
    parser.add_argument(
        "--engine",
        choices=("streaming", "accurate"),
        default=os.environ.get("ASR_ENGINE", "streaming"),
        help="engine to warm (default: $ASR_ENGINE or streaming)",
    )
    parser.add_argument(
        "--num-threads",
        type=int,
        default=None,
        help="ONNX Runtime threads (default: $ASR_NUM_THREADS or 1, matching the server)",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=16000,
        help="synthetic audio sample rate in Hz (default: %(default)s)",
    )
    parser.add_argument(
        "--endpointing-ms",
        type=int,
        default=_Config().endpointing_ms,
        help=(
            "trailing-silence endpointing in ms mapped onto rule2 (default: the "
            "wire Config default, %(default)s, matching a default server session)"
        ),
    )
    parser.add_argument(
        "--duration-ms",
        type=int,
        default=_DEFAULT_DURATION_MS,
        help="synthetic audio duration in ms (default: %(default)s)",
    )
    # Per-file overrides; default to <model>/<file> when omitted (see build_model_config).
    parser.add_argument("--tokens", default=None, help="override path to tokens.txt")
    parser.add_argument("--encoder", default=None, help="override path to encoder .onnx")
    parser.add_argument("--decoder", default=None, help="override path to decoder .onnx")
    parser.add_argument("--joiner", default=None, help="override path to joiner .onnx")
    parser.add_argument(
        "--vad-model",
        default=None,
        help="override path to the Silero VAD .onnx (accurate engine only)",
    )
    return parser.parse_args(list(argv))


def run(
    argv: Sequence[str],
    *,
    recognizer_factory: RecognizerFactory = _default_recognizer_factory,
    offline_recognizer_factory: OfflineRecognizerFactory = (
        _default_offline_recognizer_factory
    ),
) -> int:
    """CLI body: build the selected engine's recogniser and warm it.

    Dispatches on ``--engine``: ``streaming`` warms the frame-synchronous
    :class:`SherpaOnlineRecognizer`; ``accurate`` warms the VAD-segmented offline
    :class:`SherpaOfflineRecognizer`. Both drive the identical synthetic
    decode path via :func:`warmup`. Returns a process exit code.
    """
    args = _parse_args(argv)

    try:
        recognizer = _build_recognizer(
            args, recognizer_factory, offline_recognizer_factory
        )
        warmup(
            recognizer,
            duration_ms=args.duration_ms,
            sample_rate=args.sample_rate,
        )
    except Exception:  # noqa: BLE001 - any load/decode failure must fail the build
        _LOG.exception("warmup failed: model did not load or dummy inference errored")
        return 1

    return 0


def _build_recognizer(
    args: argparse.Namespace,
    recognizer_factory: RecognizerFactory,
    offline_recognizer_factory: OfflineRecognizerFactory,
) -> Recognizer:
    """Resolve the config and build the recognizer for the selected engine."""
    if args.engine == "accurate":
        offline_config = offline_config_from_args(args)
        _LOG.info(
            "warming 'accurate' engine: tokens=%s encoder=%s decoder=%s joiner=%s "
            "vad=%s threads=%d vad_min_silence=%dms",
            offline_config.tokens,
            offline_config.encoder,
            offline_config.decoder,
            offline_config.joiner,
            offline_config.vad.model,
            offline_config.num_threads,
            offline_config.vad.min_silence_ms,
        )
        return offline_recognizer_factory(offline_config)

    config = config_from_args(args)
    _LOG.info(
        "warming 'streaming' engine: tokens=%s encoder=%s decoder=%s joiner=%s "
        "threads=%d rule2_silence=%.3fs",
        config.tokens,
        config.encoder,
        config.decoder,
        config.joiner,
        config.num_threads,
        config.rule2_min_trailing_silence,
    )
    return recognizer_factory(config)


def main() -> None:
    """Console entrypoint (``RUN python -m asr_server.warmup`` in the image)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
