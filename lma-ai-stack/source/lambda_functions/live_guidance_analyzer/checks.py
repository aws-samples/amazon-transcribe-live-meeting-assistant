#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Fact checks and lookups: the judgement, with no AWS in it.

Two guidance types use tools, and both are off until an administrator turns
them on in Meeting Assistant › Live guidance:

* ``fact_check`` -- a specific factual claim made in the meeting (a number,
  price, date, version, limit, or what a product does or does not do) is checked
  against the deployment's sources, and published only when they contradict it
  ("Possibly inaccurate") or materially qualify it ("Worth checking").
* ``lookup`` -- a product, acronym, ticket, document, internal term or role the
  people in the meeting seem not to know, or that matters to an open item, is
  answered in a paragraph with its source.

The live guidance run finds the candidates, as part of the call it already
makes (the ``checks`` of its report, asked for only when a type is on); the
checks here hold each one to what the run was shown, as the rest of its answer
is held. Candidates wait on the run's state row until a research run takes a
batch: the meeting assistant function, invoked with ``action:
guidance_research``, runs one read-only agent over it and returns what the
sources say (lma-meetingassist-setup-stack/src/guidance_research.py). What
comes back is turned into guidance here: only verdicts worth publishing, above
the confidence threshold, with at least one source a tool returned, written
from the finding's fields rather than taken from the model as Markdown.

Every candidate is keyed by its kind and subject (``fact_check:claim:<slug>``,
``lookup:term:<slug>``), which is also its dedup key, and a candidate whose
key, or whose wording, matches one already checked or waiting is not checked
again. A published item someone who runs the meeting dismissed keeps its key
from being published again, and is named to the run as dismissed. See
docs/live-guidance.md.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pure

KINDS = ("fact_check", "lookup")
#: Verdicts that are published, by kind; ``supported`` only with Show
#: consistent claims on.
PUBLISHED_VERDICTS = {
    "fact_check": ("contradicted", "qualified"),
    "lookup": ("answered",),
}
SEVERITY = {
    "contradicted": "warning",
    "qualified": "suggestion",
    "supported": "info",
    "answered": "info",
}
TITLE_PREFIX = {
    "contradicted": "Possibly inaccurate",
    "qualified": "Worth checking",
    "supported": "Consistent with sources",
    "answered": "Lookup",
}
CONFIDENCE_WORDS = ((0.85, "high"), (0.7, "medium"), (0.0, "low"))

#: Candidates accepted from one run's answer. More than one research run takes:
#: the rest wait in the queue for the next.
MAX_PER_ANSWER = 5
#: Candidates waiting for research on the research row; a candidate that finds
#: the queue full is dropped, and logged as such.
MAX_PENDING = 10
#: Entries of the drop log kept on the research row, newest kept.
MAX_DROP_LOG = 50
#: Candidates one research run takes.
MAX_PER_RESEARCH = 3
#: Checked candidates remembered on the research row, newest kept.
MAX_CHECKED = 200
#: Already-checked candidates named to the run, newest first.
MAX_SHOWN_DONE = 30
MAX_CLAIM_CHARS = 300
MAX_SUMMARY_CHARS = 500
MAX_TITLE_SOURCE_CHARS = 120
MAX_BODY_CHARS = 1500
#: Two claims whose content words overlap this much are the same claim.
SIMILAR_OVERLAP = 0.6
#: Transcript lines sent with a candidate: before it, and after it.
CONTEXT_BEFORE = 3
CONTEXT_AFTER = 1

# The run's instructions and answer schema for candidates, defined beside the
# rest of the request in pure.py.
CHECKS_INSTRUCTIONS = pure.CHECKS_INSTRUCTIONS
CHECKS_RULE = pure.CHECKS_RULE
CHECKS_SCHEMA = pure.CHECKS_SCHEMA

_WORD = re.compile(r"[a-z0-9]+")
_STOP_WORDS = frozenset(
    "a an and are as at be by does doesn't for from has have in is it its of on or "
    "our support supports that the their this to was we with x y".split()
)


# ── candidates ───────────────────────────────────────────────────────────────


@dataclass
class Check:  # pylint: disable=too-many-instance-attributes
    """One candidate for research, as found by a run."""

    kind: str
    subject: str
    claim: str
    quote: str
    segment_id: str
    speaker: str
    start: float
    why: str
    confidence: float
    key: str = ""
    found_at: float = 0.0

    def to_record(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_record(cls, record: Any) -> Optional["Check"]:
        if not isinstance(record, Mapping) or record.get("kind") not in KINDS:
            return None
        try:
            return cls(
                kind=str(record["kind"]),
                subject=str(record.get("subject") or ""),
                claim=str(record.get("claim") or ""),
                quote=str(record.get("quote") or ""),
                segment_id=str(record.get("segment_id") or ""),
                speaker=str(record.get("speaker") or ""),
                start=float(record.get("start") or 0),
                why=str(record.get("why") or ""),
                confidence=float(record.get("confidence") or 0),
                key=str(record.get("key") or ""),
                found_at=float(record.get("found_at") or 0),
            )
        except (TypeError, ValueError):
            return None


def check_key(kind: str, subject: str) -> str:
    """The candidate's key and dedup key: ``fact_check:claim:<slug>`` or ``lookup:term:<slug>``."""
    noun = "claim" if kind == "fact_check" else "term"
    return f"{kind}:{noun}:{pure.subject_slug(subject) or 'general'}"


def _words(text: str) -> frozenset:
    return frozenset(w for w in _WORD.findall(text.lower()) if w not in _STOP_WORDS)


def similar(first: str, second: str) -> bool:
    """Whether two claims say the same thing: their content words overlap by SIMILAR_OVERLAP."""
    a, b = _words(first), _words(second)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= SIMILAR_OVERLAP


#: Why a candidate a run reported is not researched (the drop log's reasons).
REASON_KIND = "kind not on"
REASON_SEGMENT = "segment not shown"
REASON_THRESHOLD = "below the confidence threshold"
REASON_INCOMPLETE = "no subject, claim or quote"
REASON_QUOTE = "quote not in its segment"
REASON_PER_ANSWER = "over the run's limit"
REASON_DUPLICATE = "already checked or waiting"
REASON_SIMILAR = "worded like one already checked or waiting"
REASON_MEETING_CAP = "claims and terms per meeting reached"
REASON_QUEUE_FULL = "queue full"
REASON_RUNS_CAP = "research runs per meeting reached"


@dataclass
class Drop:
    """A candidate that is not researched, and why."""

    kind: str
    subject: str
    reason: str
    at: float

    def to_record(self) -> Dict[str, Any]:
        return asdict(self)


def parse_checks(  # pylint: disable=too-many-arguments
    entries: Any,
    names: Mapping[str, pure.Segment],
    kinds: Sequence[str],
    threshold: float,
    now: float,
) -> Tuple[List[Check], List[Drop]]:
    """The run's candidates that hold up, and the ones dropped with their reasons.

    A candidate must be of a kind that is on, name a segment the run was shown,
    and quote words that are in that segment; it must have a subject and a
    claim, and a confidence at or above ``threshold``. At most MAX_PER_ANSWER
    are kept, and one per key.
    """
    checks: List[Check] = []
    drops: List[Drop] = []
    keys = set()
    for entry in entries if isinstance(entries, list) else []:
        check, reason = _checked(entry, names, kinds, threshold, now)
        if check is not None and check.key in keys:
            check, reason = None, REASON_DUPLICATE
        elif check is not None and len(checks) >= MAX_PER_ANSWER:
            check, reason = None, REASON_PER_ANSWER
        if check is None:
            raw = entry if isinstance(entry, Mapping) else {}
            subject = " ".join(str(raw.get("subject") or "").split())[:80]
            drops.append(Drop(str(raw.get("kind") or ""), subject, reason, now))
            continue
        keys.add(check.key)
        checks.append(check)
    return checks, drops


def _checked(  # pylint: disable=too-many-arguments,too-many-return-statements
    entry: Any,
    names: Mapping[str, pure.Segment],
    kinds: Sequence[str],
    threshold: float,
    now: float,
) -> Tuple[Optional[Check], str]:
    if not isinstance(entry, Mapping) or entry.get("kind") not in kinds:
        return None, REASON_KIND
    confidence = pure._confidence(entry.get("confidence"))  # pylint: disable=protected-access
    segment = names.get(str(entry.get("segment") or "").strip())
    subject = " ".join(str(entry.get("subject") or "").split())[:80]
    claim = " ".join(str(entry.get("claim") or "").split())[:MAX_CLAIM_CHARS]
    quote = " ".join(str(entry.get("quote") or "").split())[: pure.MAX_QUOTE_CHARS]
    if confidence is None or confidence < threshold:
        return None, REASON_THRESHOLD
    if segment is None:
        return None, REASON_SEGMENT
    if not pure.subject_slug(subject) or not claim or not quote:
        return None, REASON_INCOMPLETE
    # The quote is the meeting's own words, so it must be in the segment cited.
    if " ".join(quote.lower().split()) not in " ".join(segment.text.lower().split()):
        return None, REASON_QUOTE
    kind = str(entry["kind"])
    check = Check(
        kind=kind,
        subject=subject,
        claim=claim,
        quote=quote,
        segment_id=segment.segment_id,
        speaker=segment.speaker,
        start=segment.start,
        why=" ".join(str(entry.get("why") or "").split())[:MAX_CLAIM_CHARS],
        confidence=confidence,
        key=check_key(kind, subject),
        found_at=now,
    )
    return check, ""


def admit(  # pylint: disable=too-many-arguments
    checks: Iterable[Check],
    checked: Mapping[str, Mapping[str, Any]],
    pending: Sequence[Check],
    meeting_room: int,
    now: float,
) -> Tuple[List[Check], List[Drop]]:
    """The candidates to add to the queue, and those dropped with their reasons.

    Dropped: one whose key is already checked or waiting, one worded like
    another already checked or waiting, then any beyond ``meeting_room`` (what
    the per-meeting cap still allows) and beyond the queue's own room
    (MAX_PENDING less what waits).
    """
    earlier = [str(entry.get("claim") or "") for entry in checked.values()]
    earlier += [check.claim for check in pending]
    keys = set(checked) | {check.key for check in pending}
    kept: List[Check] = []
    drops: List[Drop] = []
    queue_room = MAX_PENDING - len(pending)
    for check in checks:
        if check.key in keys:
            reason = REASON_DUPLICATE
        elif any(similar(check.claim, claim) for claim in earlier):
            reason = REASON_SIMILAR
        elif len(kept) >= meeting_room:
            reason = REASON_MEETING_CAP
        elif len(kept) >= queue_room:
            reason = REASON_QUEUE_FULL
        else:
            keys.add(check.key)
            earlier.append(check.claim)
            kept.append(check)
            continue
        drops.append(Drop(check.kind, check.subject, reason, now))
    return kept, drops


def fresh(
    checks: Iterable[Check],
    checked: Mapping[str, Mapping[str, Any]],
    pending: Sequence[Check],
) -> List[Check]:
    """The candidates not already checked or waiting, by key or by wording."""
    kept, _drops = admit(checks, checked, pending, MAX_PENDING + 1000, 0.0)
    return kept


def drop_counts(drops: Iterable[Drop]) -> Dict[str, int]:
    """How many candidates were dropped for each reason, for the log."""
    counts: Dict[str, int] = {}
    for drop in drops:
        counts[drop.reason] = counts.get(drop.reason, 0) + 1
    return counts


def render_done(checked: Mapping[str, Mapping[str, Any]], pending: Sequence[Check]) -> str:
    """The checks the run is told not to report again, newest first."""
    entries = sorted(checked.values(), key=lambda e: float(e.get("at") or 0), reverse=True)
    lines = [
        f"- {check.kind} subject={check.subject}: {check.claim} (waiting)" for check in pending
    ]
    for entry in entries:
        note = " (dismissed: report nothing like it)" if entry.get("dismissed") else ""
        lines.append(
            f"- {entry.get('kind')} subject={entry.get('subject')}: {entry.get('claim')}{note}"
        )
    return "\n".join(lines[:MAX_SHOWN_DONE]) or "(none yet)"


def context_lines(segments: Sequence[pure.Segment], segment_id: str) -> str:
    """The transcript around a candidate's segment, as ``[m:ss] Speaker: text`` lines."""
    index = next((i for i, s in enumerate(segments) if s.segment_id == segment_id), None)
    if index is None:
        return ""
    around = segments[max(0, index - CONTEXT_BEFORE) : index + CONTEXT_AFTER + 1]
    return "\n".join(f"[{pure.format_elapsed(s.start)}] {s.speaker}: {s.text}" for s in around)


def research_candidates(
    checks: Sequence[Check], segments: Sequence[pure.Segment]
) -> List[Dict[str, Any]]:
    """The candidates as a research request names them, ``C1``, ``C2``, ..."""
    return [
        {
            "id": f"C{number}",
            "kind": check.kind,
            "claim": check.claim,
            "quote": check.quote,
            "speaker": check.speaker,
            "why": check.why,
            "context": context_lines(segments, check.segment_id),
        }
        for number, check in enumerate(checks, start=1)
    ]


# ── what is published ────────────────────────────────────────────────────────

_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"(?:https?|ftp|javascript|data|mailto):\S*", re.IGNORECASE)
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]<>#|~!])")


def plain(text: Any, limit: int) -> str:
    """Model or source text as inert Markdown: no links, no addresses, no markup, cut to ``limit``.

    The finding's text is written by a model that read web pages and other
    sources, so it is never trusted as Markdown: a link it holds is reduced to
    its words, a bare address is removed, and Markdown's own characters are
    escaped, so it renders as the words it says. The only links in a published
    item are its sources', added after this.
    """
    text = _MD_LINK.sub(r"\1", " ".join(str(text or "").split()))
    text = _URL.sub("", text)
    text = " ".join(text.split())
    text = text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
    return _MD_SPECIAL.sub(r"\\\1", text)


#: A source address longer than this is shown by its title when the body is long.
MAX_SHOWN_URL_CHARS = 300


def _link(source: Mapping[str, Any], title_only: bool = False) -> str:
    """A source as ``[title](<url>) (via)``, or by its title alone.

    ``title_only`` names a source whose address is longer than
    MAX_SHOWN_URL_CHARS by its title, to make room in a long body.
    """
    title = plain(source.get("title"), MAX_TITLE_SOURCE_CHARS) or "Source"
    via = plain(source.get("via"), 40)
    url = str(source.get("url") or "")
    linkable = url.startswith(("https://", "http://")) and " " not in url
    if title_only and len(url) > MAX_SHOWN_URL_CHARS:
        linkable = False
    named = f"[{title}](<{url}>)" if linkable else title
    return f"{named} ({via})" if via else named


def _body(  # pylint: disable=too-many-arguments
    check: "Check", summary: str, said: str, source_line: str, confidence: float
) -> str:
    """A published item's body, from its parts."""
    if check.kind == "fact_check":
        parts = [
            f"> “{plain(check.quote, pure.MAX_QUOTE_CHARS)}” — {said}",
            f"**What the sources say:** {summary}",
            source_line,
            f"Confidence: {confidence_words(confidence)}",
        ]
    else:
        parts = [
            summary,
            source_line,
            f"Mentioned by {said}. Confidence: {confidence_words(confidence)}",
        ]
    return "\n\n".join(parts)


def confidence_words(confidence: float) -> str:
    word = next(word for floor, word in CONFIDENCE_WORDS if confidence >= floor)
    return f"{word} ({confidence:.2f})"


@dataclass
class Publication:  # pylint: disable=too-many-instance-attributes
    type: str
    title: str
    body: str
    severity: str
    dedup_key: str
    refs: List[str]
    confidence: float
    verdict: str
    copy_text: str

    def actions(self) -> List[Dict[str, Any]]:
        return [
            {
                "label": "Copy",
                "kind": "copy",
                "payload": {"text": self.copy_text[: pure.MAX_COPY_CHARS]},
            }
        ]


def publication(  # pylint: disable=too-many-arguments,too-many-locals
    check: Check,
    finding: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    threshold: float,
    show_supported: bool = False,
) -> Optional[Publication]:
    """The guidance item a finding makes, or None when it is not worth publishing.

    Published: a fact check the sources contradict or qualify (or support, with
    ``show_supported``), and a lookup they answer; at or above ``threshold``,
    with at least one source the research run listed, and a summary.
    """
    verdict = str(finding.get("verdict") or "")
    allowed = PUBLISHED_VERDICTS.get(check.kind, ())
    if show_supported and check.kind == "fact_check":
        allowed = (*allowed, "supported")
    confidence = pure._confidence(finding.get("confidence"))  # pylint: disable=protected-access
    if verdict not in allowed or confidence is None or confidence < threshold:
        return None
    cited = [
        sources[s] for s in finding.get("sources") or [] if isinstance(s, str) and s in sources
    ]
    summary = plain(finding.get("summary"), MAX_SUMMARY_CHARS)
    if not cited or not summary:
        return None
    subject = " ".join(check.subject.split())
    title = pure._cut(f"{TITLE_PREFIX[verdict]}: {subject}", pure.MAX_TITLE_CHARS)  # pylint: disable=protected-access
    said = f"{plain(check.speaker, 60) or 'Someone'}, {pure.format_elapsed(check.start)}"
    body = ""
    # The quote, what the sources say and the confidence always stay: what
    # gives way when the body is long is the sources, first their long
    # addresses (named by title), then the later sources. Nothing is cut
    # through, so no link is ever half-published.
    for count in range(min(3, len(cited)), 0, -1):
        for short in (False, True):
            source_line = "**Sources:** " + " · ".join(
                _link(source, title_only=short) for source in cited[:count]
            )
            body = _body(check, summary, said, source_line, confidence)
            if len(body) <= MAX_BODY_CHARS:
                break
        if len(body) <= MAX_BODY_CHARS:
            break
    copy_lines = [str(finding.get("summary") or "").strip()]
    copy_lines += [str(s.get("url") or s.get("title") or "") for s in cited[:3]]
    return Publication(
        type=check.kind,
        title=title,
        body=body,
        severity=SEVERITY[verdict],
        dedup_key=check.key,
        refs=[f"segment:{check.segment_id}"],
        confidence=confidence,
        verdict=verdict,
        copy_text="\n".join(line for line in copy_lines if line),
    )


def remember(
    checked: Dict[str, Dict[str, Any]],
    check: Check,
    now: float,
    verdict: str,
    published: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """The research row's record of a checked candidate, newest MAX_CHECKED kept.

    ``published`` is the NotificationId of the item it made, if any, so a
    dismissal of that item can be read back as this candidate's.
    """
    checked[check.key] = {
        "kind": check.kind,
        "subject": check.subject,
        "claim": check.claim,
        "verdict": verdict,
        "at": now,
        "notificationId": published,
        "dismissed": bool((checked.get(check.key) or {}).get("dismissed")),
    }
    if len(checked) > MAX_CHECKED:
        newest = sorted(checked.items(), key=lambda kv: float(kv[1].get("at") or 0))
        checked = dict(newest[-MAX_CHECKED:])
    return checked


def mark_dismissed(checked: Dict[str, Dict[str, Any]], dismissed_ids: Iterable[str]) -> int:
    """Mark the checked candidates whose published item was dismissed; how many changed."""
    ids = set(dismissed_ids)
    changed = 0
    for entry in checked.values():
        if entry.get("notificationId") in ids and not entry.get("dismissed"):
            entry["dismissed"] = True
            changed += 1
    return changed
