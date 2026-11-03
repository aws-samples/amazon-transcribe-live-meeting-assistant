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


def normalise(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text).lower()
    return WORD_RE.findall(folded)


def word_error_rate(reference: list[str], hypothesis: list[str]) -> float:
    if not reference:
        return 0.0 if not hypothesis else 1.0
    previous = list(range(len(hypothesis) + 1))
    for i, ref_word in enumerate(reference, start=1):
        current = [i] + [0] * len(hypothesis)
        for j, hyp_word in enumerate(hypothesis, start=1):
            current[j] = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ref_word != hyp_word),
            )
        previous = current
    return previous[-1] / len(reference)


def transcript_text(segments: list[Segment], channel: str | None = None) -> str:
    return " ".join(s.text for s in segments if channel is None or s.channel == channel)


def render(segments: list[Segment]) -> str:
    return "\n".join(f"[{s.start:8.1f}] {s.channel:6} {s.speaker}: {s.text}" for s in segments)


def summarise(segments: list[Segment], reference: str | None) -> dict[str, Any]:
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
        out["wer"] = word_error_rate(normalise(reference), words)
    return out
