# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""The judgement behind deriving a meeting's agenda from its transcript.

An agenda is derived only when somebody in the meeting actually stated one --
"today we'll cover the budget, hiring and the Q3 plan" -- and never inferred
from the topics that happen to come up. Two things hold that line:

* the prompt asks for a stated agenda and for the words it was stated in, and
  says plainly that "none" is the expected answer for most meetings; and
* the answer is accepted only if that quoted evidence is actually in the
  transcript, and each item is kept only if it is drawn from that quote, so an
  agenda the model made up -- from nothing, or from later discussion -- has
  nothing to point at and is discarded.

No AWS here; index.py is the plumbing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

#: Channels that carry what people said. The assistant's own answers are left
#: out: an agenda the assistant repeated back is not one the meeting stated.
SPEECH_CHANNELS = frozenset({"AGENT", "CALLER"})

#: Upper bound on the transcript text handed to the model: roughly the first half
#: hour of conversation. Agendas are stated at the start of a meeting, so a long
#: meeting is cut from the end, keeping its opening. Bounded this tightly because
#: the on-demand pass answers an AppSync request, which AppSync gives up on after
#: 30 seconds, and the model call is most of that time. A pass that outlasts it
#: still finishes and stores what it finds; the page is told through onUpdateCall.
MAX_TRANSCRIPT_CHARS = 30000

#: Bounds on what is stored, so one answer cannot fill the meeting record. The
#: item count is generous for any real agenda; the total matches the
#: updateCallAgenda resolver's limit.
MAX_ITEMS = 25
MAX_ITEM_CHARS = 200
MAX_AGENDA_CHARS = 10000

#: Shortest quoted evidence accepted. A two-word "quote" matches almost any
#: transcript and would let an invented agenda through.
MIN_EVIDENCE_CHARS = 12


def format_elapsed(seconds: Any) -> str:
    """Seconds from the start of the meeting as [MM:SS] (or [H:MM:SS])."""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "[--:--]"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"[{hours}:{minutes:02d}:{secs:02d}]" if hours else f"[{minutes:02d}:{secs:02d}]"


def _seconds(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_transcript(segments: Iterable[Mapping[str, Any]]) -> str:
    """Final speech segments in start order, one "[MM:SS] Speaker: text" line each,
    cut to MAX_TRANSCRIPT_CHARS from the end."""
    rows = []
    for segment in segments:
        if segment.get("IsPartial") or segment.get("Channel") not in SPEECH_CHANNELS:
            continue
        text = str(segment.get("Transcript") or "").strip()
        if not text:
            continue
        start = _seconds(segment.get("StartTime"))
        rows.append(
            (_seconds(segment.get("EndTime")), start, segment.get("Speaker") or "Unknown", text)
        )
    # By start time, the time each line shows, so the times read forwards where
    # two channels overlap.
    rows.sort(key=lambda row: (row[1], row[0]))
    transcript = "\n".join(
        f"{format_elapsed(start)} {speaker}: {text}" for _end, start, speaker, text in rows
    )
    return transcript[:MAX_TRANSCRIPT_CHARS]


def build_prompt(transcript: str) -> str:
    """The extraction prompt. The transcript is the only meeting content in it."""
    return f"""You read meeting transcripts and report whether the meeting's AGENDA was stated.

An agenda is stated when someone in the meeting explicitly says what the meeting will
cover - for example "the agenda today is...", "we have three things to get through:
...", "first we'll do X, then Y, and finish with Z", or reads out an agenda.

Topics that merely come up in conversation are NOT an agenda. Do not infer an agenda
from what was discussed, from the meeting's purpose, or from your own knowledge. Most
meetings never state one; "not stated" is the expected and correct answer for them.

Return only a JSON object, with no other text:
{{"agenda_stated": true or false,
  "items": ["first item", "second item", ...],
  "evidence": "the exact words from the transcript where the agenda was stated"}}

Rules:
- If no agenda was stated, return {{"agenda_stated": false, "items": [], "evidence": ""}}.
- "items" lists the stated agenda items in the order given, each a short phrase in the
  speaker's own terms. Include only items that were stated as part of the agenda.
- "evidence" must be copied exactly from the transcript text (without the time and
  speaker name), and must contain the statement of the agenda.

<transcript>
{transcript}
</transcript>"""


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


_LINE_PREFIX = re.compile(r"^\[[^\]]*\] [^:]*: ", re.MULTILINE)


def evidence_is_in_transcript(evidence: Any, transcript: str) -> bool:
    """Whether the quoted evidence really is in what was said.

    Compared against the spoken text alone -- each line's time and speaker
    removed -- so a statement that ran across two segments still matches, and
    with case, punctuation and spacing ignored, since a model copying a quote
    routinely normalises those. Anything else it changed fails the check.
    """
    if not isinstance(evidence, str):
        return False
    quote = _normalise(evidence)
    spoken = _normalise(_LINE_PREFIX.sub("", transcript))
    return len(quote) >= MIN_EVIDENCE_CHARS and quote in spoken


# Words too common to show that an item came from the quoted statement.
_FILLER = frozenset(
    "the and for with our about then next first last also will well lets let's "
    "this that these those from into over under item items agenda today talk "
    "discuss discussion cover review update updates".split()
)


def _stems(text: str) -> List[str]:
    # The first five letters: enough to match a plural or an inflection that
    # keeps them ("budget" and "budgets", "hiring" and "hirings"), not a real
    # stemmer -- "hire" and "hiring" do not meet.
    return [word[:5] for word in _normalise(text).split()]


def item_is_in_evidence(item: str, evidence: str) -> bool:
    """Whether an agenda item was drawn from the quoted statement of the agenda.

    At least half of the item's content words (three letters or more, common
    filler left out) must appear in the quote; an item with no content words --
    "Q3", say -- must appear in it whole. That allows the short paraphrase an
    item list naturally is ("the budget" from "first, the budget") and refuses
    an item taken from anywhere else.
    """
    quoted = set(_stems(evidence))
    words = _normalise(item).split()
    content = [w[:5] for w in words if len(w) >= 3 and w not in _FILLER]
    if not content:
        return bool(words) and all(w[:5] in quoted for w in words)
    found = sum(1 for stem in content if stem in quoted)
    return found * 2 >= len(content)


def _json_object(raw: str) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        match = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group())
        except ValueError:
            return None
    return parsed if isinstance(parsed, dict) else None


def parse_model_response(raw: str, transcript: str) -> Optional[List[str]]:
    """The stated agenda's items, or None when no agenda was stated.

    None as well for an answer that cannot be read or whose evidence is not in
    the transcript: storing nothing is always the safe outcome here, because an
    agenda the meeting never had would then be what the assistant and the
    summary measure the meeting against.
    """
    answer = _json_object(raw)
    if not answer or answer.get("agenda_stated") is not True:
        return None
    evidence = answer.get("evidence")
    if not evidence_is_in_transcript(evidence, transcript):
        return None
    items = []
    for item in answer.get("items") or []:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split())[:MAX_ITEM_CHARS]
        if text and text not in items and item_is_in_evidence(text, evidence):
            items.append(text)
        if len(items) >= MAX_ITEMS:
            break
    return items or None


def agenda_text(items: List[str]) -> str:
    """The stored form: one item per line, within the resolver's limit."""
    return "\n".join(items)[:MAX_AGENDA_CHARS]


def has_agenda(meeting: Optional[Mapping[str, Any]]) -> bool:
    return bool(str((meeting or {}).get("Agenda") or "").strip())
