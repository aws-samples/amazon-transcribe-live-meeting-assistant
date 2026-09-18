# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Measure a speaker embedder's diarization operating point from a recording.

This is the developer tool behind every ``speakerThreshold`` in ``catalog.json``.
Deployments never run it: the operating point is measured once per speaker model,
recorded on its bundle, and baked into the ASR image - because the right value is
specific to the embedder and to the segmentation policy (endpointing silence and the
minimum utterance floor), and a guessed or borrowed number fragments one person into
several or merges several into one.

Input is a **two-channel WAV with one speaker per channel**. That channel separation
is the ground truth: pairs within a channel are the same person, pairs across
channels are different people, and a threshold has to sit between those two
distributions. Any sample rate is accepted (resampled to 16 kHz); 16-bit PCM only.

    cd lma-asr-microvm-stack/source
    .venv/bin/python -m scripts.calibrate --wav two-speakers.wav \\
        --speaker-model /path/to/nemo_en_titanet_small.onnx

Statistics come first, then a recommendation: the threshold is placed at the
midpoint of the gap between the same-speaker 5th percentile and the different-speaker
95th percentile, and raised above the highest different-speaker score actually
observed so no measured pair would have merged. Overlapping distributions produce
``"confidence": "unusable"`` and no number, which is more useful than a wrong one.

Choose the two voices deliberately: the result can only be as demanding as the
hardest pair in the sample, so two people of the same gender and accent make a
better control than a man and a woman. Real meeting audio beats synthetic speech.
"""

from __future__ import annotations

import argparse
import array
import json
import math
import sys
import wave
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
FRAME_MS = 20
# Without a hangover this long, normal speech never forms a one-second run: the
# sub-200 ms gaps between words end it.
HANGOVER_MS = 300
MIN_SEGMENT_SEC = 1.0
MAX_SEGMENT_SEC = 12.0
# A frame counts as one channel's speech only when it is clearly louder than the
# other channel too, so a stretch of cross-talk lands in neither distribution.
DOMINANCE_RATIO = 3.0
SPEECH_RMS = 150.0
# Short utterances embed unreliably; pairs involving one are compared against
# long-only pairs to decide whether to recommend a minimum utterance floor.
SHORT_UTTERANCE_SEC = 2.5
RECOMMENDED_MIN_SEGMENT_MS = 2500


@dataclass(frozen=True)
class Segment:
    channel: int
    start_sec: float
    end_sec: float
    pcm: bytes

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


@dataclass(frozen=True)
class Embedded:
    channel: int
    duration_sec: float
    vector: tuple[float, ...]


@dataclass
class OperatingPoint:
    same_speaker_pairs: int
    different_speaker_pairs: int
    same_speaker_p5: float
    same_speaker_median: float
    different_speaker_median: float
    different_speaker_p95: float
    different_speaker_max: float
    separation: float
    confidence: str
    speaker_threshold: float | None = None
    min_segment_ms: int | None = None
    notes: list[str] = field(default_factory=list)


# --- audio -----------------------------------------------------------------


def read_stereo_wav(path: Path) -> tuple[bytes, bytes, int]:
    """Return (channel 0 PCM, channel 1 PCM, sample rate) for a 16-bit stereo WAV."""
    with wave.open(str(path), "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        if channels != 2:
            raise ValueError(
                f"{path} has {channels} channel(s); calibration needs a two-channel recording "
                "with one speaker per channel, because that separation is the ground truth"
            )
        if width != BYTES_PER_SAMPLE:
            raise ValueError(f"{path} is not 16-bit PCM (sample width {width})")
        frames = wav.readframes(wav.getnframes())
    samples = array.array("h")
    samples.frombytes(frames[: len(frames) - len(frames) % (BYTES_PER_SAMPLE * 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    return samples[0::2].tobytes(), samples[1::2].tobytes(), rate


def resample_pcm16(pcm: bytes, source_rate: int, target_rate: int = SAMPLE_RATE) -> bytes:
    """Linear-interpolation resample. Good enough for a speaker embedding at 16 kHz."""
    if source_rate == target_rate:
        return pcm
    source = array.array("h")
    source.frombytes(pcm[: len(pcm) - len(pcm) % BYTES_PER_SAMPLE])
    if len(source) < 2:
        return b""
    step = source_rate / target_rate
    count = int(math.floor((len(source) - 1) / step)) + 1
    out = array.array("h", bytes(count * BYTES_PER_SAMPLE))
    for i in range(count):
        position = i * step
        index = int(position)
        fraction = position - index
        nxt = source[index + 1] if index + 1 < len(source) else source[index]
        value = source[index] * (1.0 - fraction) + nxt * fraction
        out[i] = max(-32768, min(32767, int(round(value))))
    return out.tobytes()


def frame_rms(pcm: bytes, sample_rate: int = SAMPLE_RATE, frame_ms: int = FRAME_MS) -> list[float]:
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % BYTES_PER_SAMPLE])
    per_frame = sample_rate * frame_ms // 1000
    frames = len(samples) // per_frame
    out: list[float] = []
    for f in range(frames):
        chunk = samples[f * per_frame : (f + 1) * per_frame]
        out.append(math.sqrt(sum(v * v for v in chunk) / per_frame))
    return out


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def dominant_segments(
    own_pcm: bytes, other_pcm: bytes, channel: int, sample_rate: int = SAMPLE_RATE
) -> list[Segment]:
    """Stretches where ``own`` clearly dominates ``other`` (dominance, not silence).

    Both channels carry room noise, and a segment containing both voices would
    corrupt both distributions, so a frame is attributed only when it is above a
    speech floor AND at least DOMINANCE_RATIO louder than the other channel.
    """
    own = frame_rms(own_pcm, sample_rate)
    other = frame_rms(other_pcm, sample_rate)
    frames = min(len(own), len(other))
    if frames == 0:
        return []
    speaking = [v for v in own[:frames] if v > SPEECH_RMS]
    if not speaking:
        return []
    floor = _median(speaking) * 0.5
    hangover_frames = HANGOVER_MS // FRAME_MS
    frame_bytes = sample_rate * FRAME_MS // 1000 * BYTES_PER_SAMPLE

    segments: list[Segment] = []
    run_start: int | None = None
    quiet = 0

    def close(end_frame: int) -> None:
        nonlocal run_start, quiet
        if run_start is None:
            return
        start_sec = run_start * FRAME_MS / 1000
        end_sec = min(end_frame * FRAME_MS / 1000, start_sec + MAX_SEGMENT_SEC)
        if end_sec - start_sec >= MIN_SEGMENT_SEC:
            segments.append(
                Segment(
                    channel,
                    start_sec,
                    end_sec,
                    own_pcm[
                        run_start * frame_bytes : int(end_sec * 1000 / FRAME_MS) * frame_bytes
                    ],
                )
            )
        run_start, quiet = None, 0

    for f in range(frames):
        dominant = own[f] > floor and own[f] > DOMINANCE_RATIO * max(other[f], 1.0)
        if dominant:
            if run_start is None:
                run_start = f
            quiet = 0
        elif run_start is not None:
            quiet += 1
            if quiet > hangover_frames:
                close(f - quiet)
    close(frames)
    return segments


def spread(items: Sequence[Segment], limit: int) -> list[Segment]:
    """Up to ``limit`` items spaced evenly across the recording, not the first N."""
    if len(items) <= limit:
        return list(items)
    if limit <= 1:
        return [items[0]]
    return [items[round(i * (len(items) - 1) / (limit - 1))] for i in range(limit)]


# --- statistics -----------------------------------------------------------


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile, matching the rule the shipped thresholds were derived with."""
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def derive_operating_point(segments: Sequence[Embedded]) -> OperatingPoint:
    """Turn embeddings of channel-labelled segments into an operating point.

    The threshold is placed between the two distributions rather than at either
    edge: above what different speakers reach, below what the same speaker scores.
    When they overlap there is no such point, and saying so is more useful than
    returning a number that will fragment or merge speakers.
    """
    same: list[float] = []
    different: list[float] = []
    same_short: list[float] = []
    same_long: list[float] = []
    for i in range(len(segments)):
        for j in range(i + 1, len(segments)):
            score = cosine(segments[i].vector, segments[j].vector)
            if segments[i].channel == segments[j].channel:
                same.append(score)
                shorter = min(segments[i].duration_sec, segments[j].duration_sec)
                (same_short if shorter < SHORT_UTTERANCE_SEC else same_long).append(score)
            else:
                different.append(score)

    point = OperatingPoint(
        same_speaker_pairs=len(same),
        different_speaker_pairs=len(different),
        same_speaker_p5=percentile(same, 0.05),
        same_speaker_median=percentile(same, 0.5),
        different_speaker_median=percentile(different, 0.5),
        different_speaker_p95=percentile(different, 0.95),
        different_speaker_max=max(different) if different else float("nan"),
        separation=float("nan"),
        confidence="unusable",
    )
    if len(same) < 3 or len(different) < 3:
        point.notes.append(
            "Not enough isolated speech to compare: needs several utterances on each "
            "channel from a recording where both sides spoke."
        )
        return point

    same_floor = point.same_speaker_p5
    different_ceiling = point.different_speaker_p95
    point.separation = same_floor - different_ceiling
    if point.separation <= 0:
        point.notes.append(
            "The same-speaker and different-speaker scores overlap, so no threshold "
            "separates them on this audio. This is what a mismatched embedder looks like; "
            "it can also mean narrowband audio or heavy cross-talk."
        )
        return point

    # Midpoint of the gap: a 40%-of-gap rule gave 0.286 on a meeting where two similar
    # voices outside the sample scored 0.25-0.31 and merged.
    threshold = round(different_ceiling + point.separation * 0.5, 3)

    # p95 leaves a tail; with a small sample that tail is one or two pairs. Clear the
    # highest OBSERVED different-speaker score too, so the guarantee is concrete.
    if threshold <= point.different_speaker_max:
        clear_of_max = round(point.different_speaker_max + 0.005, 3)
        if clear_of_max >= same_floor:
            point.notes.append(
                "The highest different-speaker score reaches into the same-speaker range, "
                "so no threshold clears every observed pair. More audio, or a different "
                "speaker model, is needed."
            )
            return point
        point.notes.append(
            "Raised above the highest observed different-speaker score "
            f"({point.different_speaker_max:.3f}) so no measured pair would merge."
        )
        threshold = clear_of_max

    point.confidence = "good" if point.separation >= 0.1 else "weak"
    if point.confidence == "weak":
        point.notes.append(
            f"The gap between the distributions is narrow ({point.separation:.3f}), so the "
            "threshold is sensitive to the audio it was measured on."
        )

    if len(same_short) >= 3 and len(same_long) >= 3:
        gap = percentile(same_long, 0.5) - percentile(same_short, 0.5)
        if gap >= 0.05:
            point.min_segment_ms = RECOMMENDED_MIN_SEGMENT_MS
            point.notes.append(
                f"Utterances under {SHORT_UTTERANCE_SEC}s scored {gap:.2f} lower against the "
                "same speaker than longer ones, so shorter segments are best left to inherit "
                "the current speaker."
            )
    else:
        point.notes.append(
            "Not enough of a mix of short and long utterances to judge a minimum utterance "
            "length; keep the bundle's existing value."
        )

    point.notes.append(
        f"Measured over {point.same_speaker_pairs} same-speaker and "
        f"{point.different_speaker_pairs} different-speaker pairs."
    )
    point.speaker_threshold = threshold
    return point


# --- pipeline -------------------------------------------------------------


def collect_segments(wav: Path, max_per_channel: int) -> tuple[list[Segment], int, float]:
    """Dominant segments from both channels, resampled to 16 kHz, spread over the file."""
    ch0, ch1, rate = read_stereo_wav(wav)
    ch0, ch1 = resample_pcm16(ch0, rate), resample_pcm16(ch1, rate)
    seconds = min(len(ch0), len(ch1)) / (SAMPLE_RATE * BYTES_PER_SAMPLE)
    picked: list[Segment] = []
    for channel, own, other in ((0, ch0, ch1), (1, ch1, ch0)):
        picked.extend(spread(dominant_segments(own, other, channel), max_per_channel))
    return picked, rate, seconds


def embed_segments(segments: Sequence[Segment], embedder) -> list[Embedded]:  # noqa: ANN001
    from asr_server.diarization import pcm16_to_float32

    return [
        Embedded(
            s.channel,
            s.duration_sec,
            tuple(embedder.embed(SAMPLE_RATE, pcm16_to_float32(s.pcm))),
        )
        for s in segments
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.calibrate",
        description="Measure a speaker embedder's operating point from a two-channel recording.",
    )
    parser.add_argument(
        "--wav", required=True, type=Path, help="16-bit stereo WAV, one speaker per channel"
    )
    parser.add_argument(
        "--speaker-model", required=True, type=Path, help="speaker-embedding ONNX model"
    )
    parser.add_argument("--max-segments-per-channel", type=int, default=12)
    parser.add_argument("--num-threads", type=int, default=2)
    parser.add_argument("--json", action="store_true", help="machine-readable output only")
    args = parser.parse_args(argv)

    from asr_server.diarization import SpeakerEmbedderConfig, create_sherpa_embedder

    segments, source_rate, seconds = collect_segments(args.wav, args.max_segments_per_channel)
    per_channel = {c: sum(1 for s in segments if s.channel == c) for c in (0, 1)}
    if min(per_channel.values()) == 0:
        print(
            json.dumps(
                {
                    "error": "one channel carries no isolated speech; both speakers must talk",
                    "segments_per_channel": per_channel,
                }
            )
        )
        return 2

    embedder = create_sherpa_embedder(
        SpeakerEmbedderConfig(
            model=args.speaker_model, sample_rate=SAMPLE_RATE, num_threads=args.num_threads
        )
    )
    point = derive_operating_point(embed_segments(segments, embedder))
    report = {
        "speaker_model": str(args.speaker_model),
        "wav": str(args.wav),
        "source_sample_rate": source_rate,
        "audio_seconds_analysed": round(seconds, 1),
        "segments_embedded": per_channel,
        "result": asdict(point),
    }
    if args.json:
        print(json.dumps(report))
    else:
        print(json.dumps(report, indent=2))
        if point.speaker_threshold is not None:
            print(
                f"\nRecommendation: speakerThreshold={point.speaker_threshold} "
                f"({point.confidence} separation {point.separation:.3f})"
                + (f", minSegmentMs={point.min_segment_ms}" if point.min_segment_ms else "")
            )
        else:
            print("\nNo usable threshold on this recording; see notes.")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
