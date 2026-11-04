# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
"""Read a benchmark meeting's transcript back and compare it with a reference."""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

WORD_RE = re.compile(r"[\w']+")


@dataclass
class Segment:
    channel: str
    speaker: str
    start: float
    end: float
    text: str


def read_segments(ddb, table: str, call_id: str) -> list[Segment]:
    segments: list[Segment] = []
    kwargs: dict[str, Any] = {
        "TableName": table,
        "KeyConditionExpression": "PK = :pk",
        "FilterExpression": "IsPartial = :false AND Channel IN (:agent, :caller)",
        "ExpressionAttributeValues": {
            ":pk": {"S": f"trs#{call_id}"},
            ":false": {"BOOL": False},
            ":agent": {"S": "AGENT"},
            ":caller": {"S": "CALLER"},
        },
    }
    while True:
        page = ddb.query(**kwargs)
        for item in page.get("Items", []):
            segments.append(
                Segment(
                    channel=item.get("Channel", {}).get("S", ""),
                    speaker=item.get("Speaker", {}).get("S", ""),
                    start=float(item.get("StartTime", {}).get("N", "0")),
                    end=float(item.get("EndTime", {}).get("N", "0")),
                    text=item.get("Transcript", {}).get("S", ""),
                )
            )
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    segments.sort(key=lambda s: (s.start, s.channel))
    return segments


def wait_for_segments(
    ddb, table: str, call_id: str, settle_s: float = 60, poll_s: float = 15
) -> tuple[list[Segment], bool]:
    best: list[Segment] = []
    previous = -1
    stable_since = time.monotonic()
    shrank = False
    while True:
        segments = read_segments(ddb, table, call_id)
        if len(segments) < len(best):
            shrank = True
        if len(segments) > len(best):
            best = segments
        if len(segments) != previous:
            previous, stable_since = len(segments), time.monotonic()
        elif time.monotonic() - stable_since >= settle_s:
            return best, shrank
        time.sleep(poll_s)


FILLERS = frozenset(
    {"um", "uh", "uhm", "erm", "er", "ah", "eh", "hmm", "hm", "mm", "mhm", "mm-hmm"}
)
EQUIVALENTS = {"ok": "okay", "okey": "okay", "o.k": "okay"}
ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
TENS = ["_", "_", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
REFERENCE_CHANNELS = {
    "0": "CALLER",
    "a": "CALLER",
    "caller": "CALLER",
    "meeting": "CALLER",
    "system": "CALLER",
    "1": "AGENT",
    "b": "AGENT",
    "agent": "AGENT",
    "mic": "AGENT",
    "microphone": "AGENT",
}


def _number_words(n: int) -> list[str]:
    if n < 20:
        return [ONES[n]]
    if n < 100:
        return [TENS[n // 10]] + ([ONES[n % 10]] if n % 10 else [])
    rest = _number_words(n % 100) if n % 100 else []
    return [ONES[n // 100], "hundred"] + rest


def normalise(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).lower().replace("\u2019", "'")
    words: list[str] = []
    for word in WORD_RE.findall(folded):
        word = word.strip("'")
        if not word or word in FILLERS:
            continue
        if word.isdigit() and len(word) <= 3:
            words += _number_words(int(word))
            continue
        word = EQUIVALENTS.get(word, word)
        words.append(re.sub(r"isation(s?)$", r"ization\1", word))
    return words


def align(reference: list[str], hypothesis: list[str]) -> dict[str, Any]:
    previous = [(j, 0, 0, j) for j in range(len(hypothesis) + 1)]
    for i, ref_word in enumerate(reference, start=1):
        current = [(i, 0, i, 0)]
        for j, hyp_word in enumerate(hypothesis, start=1):
            diag = previous[j - 1]
            miss = previous[j]
            extra = current[j - 1]
            changed = ref_word != hyp_word
            current.append(
                min(
                    (diag[0] + changed, diag[1] + changed, diag[2], diag[3]),
                    (miss[0] + 1, miss[1], miss[2] + 1, miss[3]),
                    (extra[0] + 1, extra[1], extra[2], extra[3] + 1),
                )
            )
        previous = current
    errors, swapped, missing, extra = previous[-1]
    total = len(reference)
    return {
        "reference_words": total,
        "hypothesis_words": len(hypothesis),
        "wer": errors / total if total else (0.0 if not hypothesis else 1.0),
        "swapped": swapped / total if total else 0.0,
        "missing": missing / total if total else 0.0,
        "extra": extra / total if total else 0.0,
    }


def word_error_rate(reference: list[str], hypothesis: list[str]) -> float:
    return align(reference, hypothesis)["wer"]


@dataclass
class Reference:
    segments: list[Segment]
    by_channel: bool

    def words(self, channel: str | None = None) -> list[str]:
        return normalise(transcript_text(self.segments, channel))

    def speakers(self, channel: str) -> int:
        return len({s.speaker for s in self.segments if s.channel == channel and s.speaker})


def parse_stm(text: str) -> Reference:
    segments: list[Segment] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(";;"):
            continue
        fields = line.split(maxsplit=5)
        if len(fields) < 5:
            raise ValueError(f"STM line needs at least 5 fields: {line!r}")
        channel = REFERENCE_CHANNELS.get(fields[1].lower())
        if channel is None:
            raise ValueError(
                f"STM channel {fields[1]!r} is not 0/1, A/B, meeting/mic or CALLER/AGENT"
            )
        words = fields[5] if len(fields) > 5 else ""
        if words.startswith("<"):
            words = words.split(">", 1)[1] if ">" in words else ""
        segments.append(
            Segment(channel, fields[2], float(fields[3]), float(fields[4]), words.strip())
        )
    segments.sort(key=lambda s: (s.channel, s.start))
    return Reference(segments, by_channel=True)


def load_reference(path) -> Reference:
    from pathlib import Path

    text = Path(path).read_text(encoding="utf-8")
    if str(path).lower().endswith(".stm"):
        return parse_stm(text)
    return Reference([Segment("", "", 0.0, 0.0, text)], by_channel=False)


def score(segments: list[Segment], reference: Reference) -> dict[str, Any]:
    if not reference.by_channel:
        chronological = sorted(segments, key=lambda s: s.start)
        return {"all": align(reference.words(), normalise(transcript_text(chronological)))}
    ordered = sorted(segments, key=lambda s: (s.channel, s.start))
    out: dict[str, Any] = {}
    for channel in ("CALLER", "AGENT"):
        ref_words = reference.words(channel)
        if ref_words:
            out[channel] = align(ref_words, normalise(transcript_text(ordered, channel)))
            out[channel]["reference_speakers"] = reference.speakers(channel)
    return out


def transcript_text(segments: list[Segment], channel: str | None = None) -> str:
    return " ".join(s.text for s in segments if channel is None or s.channel == channel)


def render(segments: list[Segment]) -> str:
    return "\n".join(f"[{s.start:8.1f}] {s.channel:6} {s.speaker}: {s.text}" for s in segments)


def summarise(segments: list[Segment], reference: Reference | None) -> dict[str, Any]:
    words = normalise(transcript_text(segments))
    out: dict[str, Any] = {
        "rows": len(segments),
        "words": len(words),
        "speakers": {
            channel: sorted({s.speaker for s in segments if s.channel == channel})
            for channel in sorted({s.channel for s in segments})
        },
    }
    if reference is not None:
        out["accuracy"] = score(segments, reference)
    return out
