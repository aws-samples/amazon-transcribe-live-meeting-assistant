# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Summary profiles, and the structured action items a profile can ask for.

A **summary profile** is a named set of summary sections. ``default`` is the
Transcript Summary prompts (the configuration section
``summary_prompt_templates``), which every meeting is summarised with unless it
names another profile in its ``SummaryProfile``; the others are the entries of
the ``summary_profiles`` section. Each entry holds:

* ``label`` and ``description`` -- what people choosing it see;
* ``prompts`` -- "N#Title" prompts, in the form of the Transcript Summary ones;
* ``structuredActionItems`` -- whether the summariser makes one more model call
  for the meeting's action items as data (:func:`validate_action_items`).

Who uses this module: the summariser picks the prompts, the meeting
preparation resolver checks a profile name before storing it, and the MCP
server lists the profiles. Pure Python with no dependencies, like the rest of
this layer. See docs/transcript-summarization.md#summary-profiles.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

#: The profile that is the Transcript Summary prompts.
DEFAULT_PROFILE = "default"
DEFAULT_LABEL = "Default"
DEFAULT_DESCRIPTION = "The Transcript Summary sections on the Configuration page."

#: A profile name: lower-case letters, digits and '-', as the section's schema
#: holds its keys to.
PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

#: The most action items kept from one summary.
MAX_ACTION_ITEMS = 50
MAX_ACTION_TEXT = 500
MAX_OWNER = 200
MAX_DUE_TEXT = 100
#: How far past the last segment's end an item's time may be and still be kept.
TIME_SLACK_SECONDS = 5.0

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FENCED = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


class UnknownSummaryProfile(ValueError):
    """A profile name that is well formed but names no profile."""


class MalformedActionItems(ValueError):
    """A model answer that is not a JSON list of action items."""


# ── profile names and entries ───────────────────────────────────────────────


def normalize_profile_name(value: Any) -> Optional[str]:
    """A stored profile name: None for none (null, empty or ``default``).

    Raises ValueError for anything else that is not a profile name.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("A summary profile must be a profile name")
    name = value.strip()
    if not name or name == DEFAULT_PROFILE:
        return None
    if not PROFILE_NAME.match(name):
        raise ValueError(
            "A summary profile name is 1 to 63 lower-case letters, digits and '-', "
            "starting with a letter or digit"
        )
    return name


def profile_entries(section: Any) -> Dict[str, Dict[str, Any]]:
    """The named profiles of a ``summary_profiles`` section, by name.

    An entry under ``default`` or a malformed name, or one that is not an
    object, is left out: ``default`` is always the Transcript Summary prompts.
    """
    if not isinstance(section, Mapping):
        return {}
    return {
        name: dict(entry)
        for name, entry in section.items()
        if isinstance(name, str)
        and name != DEFAULT_PROFILE
        and PROFILE_NAME.match(name)
        and isinstance(entry, Mapping)
    }


def profile_listing(section: Any) -> List[Dict[str, Any]]:
    """Every profile a meeting may name, ``default`` first, then by name."""
    listing = [
        {
            "name": DEFAULT_PROFILE,
            "label": DEFAULT_LABEL,
            "description": DEFAULT_DESCRIPTION,
            "structuredActionItems": False,
        }
    ]
    for name, entry in sorted(profile_entries(section).items()):
        label = entry.get("label")
        description = entry.get("description")
        listing.append(
            {
                "name": name,
                "label": label.strip() if isinstance(label, str) and label.strip() else name,
                "description": description if isinstance(description, str) else "",
                "structuredActionItems": entry.get("structuredActionItems") is True,
            }
        )
    return listing


def require_profile(name: Any, section: Any) -> Optional[str]:
    """A profile name checked against the configuration: None for ``default``.

    Raises ValueError for a malformed name and UnknownSummaryProfile for one the
    configuration does not hold, naming the profiles that exist.
    """
    normalized = normalize_profile_name(name)
    if normalized is None:
        return None
    entries = profile_entries(section)
    if normalized not in entries:
        known = ", ".join([DEFAULT_PROFILE, *sorted(entries)])
        raise UnknownSummaryProfile(
            f"There is no summary profile {normalized!r}; the profiles are: {known}"
        )
    return normalized


def profile_prompts(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """A profile's prompts, as the Transcript Summary section stores its own."""
    prompts = entry.get("prompts")
    return dict(prompts) if isinstance(prompts, Mapping) else {}


# ── structured action items ─────────────────────────────────────────────────

#: The prompt of the extra model call. {timed_transcript} is filled with the
#: meeting's timed transcript, as the summary sections are.
ACTION_ITEMS_PROMPT = (
    "The following is the transcript of a meeting, with the time of each line from the "
    "start of the meeting. Lines that begin with [chat] were typed in the meeting chat "
    "rather than spoken.\n<transcript>\n{timed_transcript}\n</transcript>\n\n"
    "List every action item agreed or assigned in the meeting, as a JSON array and nothing "
    "else: no preamble, no explanation and no Markdown. Each element is an object with "
    "exactly these keys:\n"
    '- "text": what is to be done, in one sentence;\n'
    '- "owner": the name of the person who is to do it, exactly as it appears in the '
    "transcript, or null when the transcript does not say who. Never guess an owner from "
    "a role or from who spoke most;\n"
    '- "dueDate": the date it is due as YYYY-MM-DD, only when the transcript states a '
    "date that can be written that way, otherwise null;\n"
    '- "dueText": how the due date was said, for example "end of next week", or null when '
    "no due date was said;\n"
    '- "startTime": the time in seconds from the start of the meeting of the line where '
    "the action was agreed, read from the [MM:SS] or [H:MM:SS] mark of that line.\n"
    "If there are no action items, answer with an empty array: []"
)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] if text else None


def _iso_date(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not ISO_DATE.match(value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


def parse_action_items(answer: Any) -> List[Any]:
    """The JSON list in a model answer, allowing a fenced block around it.

    Raises MalformedActionItems for an answer that holds no JSON list.
    """
    if not isinstance(answer, str) or not answer.strip():
        raise MalformedActionItems("the answer is empty")
    candidates = [answer.strip()]
    fenced = FENCED.search(answer)
    if fenced:
        candidates.insert(0, fenced.group(1).strip())
    start, end = answer.find("["), answer.rfind("]")
    if 0 <= start < end:
        candidates.append(answer[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, list):
            return parsed
    raise MalformedActionItems("the answer is not a JSON array")


def _owner_named(owner: str, names: Sequence[str], transcript: str) -> bool:
    """Whether an owner appears as said: among the speakers, or in the transcript text."""
    folded = owner.casefold()
    if any(folded == str(name).casefold() for name in names if name):
        return True
    return bool(re.search(r"(?<!\w)" + re.escape(folded) + r"(?!\w)", transcript.casefold()))


def nearest_segment(
    start_time: float, segments: Sequence[Mapping[str, Any]]
) -> Optional[Mapping[str, Any]]:
    """The segment a time falls in, else the last one starting before it."""
    best: Optional[Mapping[str, Any]] = None
    best_start = float("-inf")
    for segment in segments:
        begins = _number(segment.get("startTime"))
        if begins is None:
            continue
        ends = _number(segment.get("endTime"))
        if begins <= start_time <= (ends if ends is not None else begins):
            return segment
        if best_start <= begins <= start_time:
            best, best_start = segment, begins
    return best


def validate_action_items(
    answer: Any,
    segments: Sequence[Mapping[str, Any]] = (),
    speaker_names: Iterable[str] = (),
    transcript: str = "",
) -> List[Dict[str, Any]]:
    """The model's action items, checked one by one.

    ``segments`` are the meeting's segments as ``{segmentId, startTime,
    endTime}``, ``speaker_names`` its speakers as named, and ``transcript`` its
    text. An element without text is dropped; every other field that does not
    check out is set to null rather than kept:

    * ``owner`` must be a speaker's name or appear in the transcript -- a name
      as said, never one the model supplied;
    * ``dueDate`` must be a real date written YYYY-MM-DD;
    * ``startTime`` must fall within the meeting, and ``segmentId`` is the
      segment at that time, found here, not taken from the model.

    Raises MalformedActionItems when the answer holds no JSON list.
    """
    raw = parse_action_items(answer)
    names = [str(name) for name in speaker_names or [] if name]
    text = transcript if isinstance(transcript, str) else ""
    rows = [s for s in segments or [] if isinstance(s, Mapping)]
    ends = [_number(s.get("endTime")) or _number(s.get("startTime")) or 0.0 for s in rows]
    last = max(ends) if ends else None
    items: List[Dict[str, Any]] = []
    for element in raw:
        if not isinstance(element, Mapping):
            continue
        action = _text(element.get("text"), MAX_ACTION_TEXT)
        if not action:
            continue
        owner = _text(element.get("owner"), MAX_OWNER)
        if owner and not _owner_named(owner, names, text):
            owner = None
        start = _number(element.get("startTime"))
        if start is not None and (
            start < 0 or (last is not None and start > last + TIME_SLACK_SECONDS)
        ):
            start = None
        segment = nearest_segment(start, rows) if start is not None else None
        items.append(
            {
                "text": action,
                "owner": owner,
                "dueDate": _iso_date(element.get("dueDate")),
                "dueText": _text(element.get("dueText"), MAX_DUE_TEXT),
                "startTime": start,
                "segmentId": str(segment.get("segmentId")) if segment else None,
            }
        )
        if len(items) >= MAX_ACTION_ITEMS:
            break
    return items


def stored_action_items(items: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Validated action items as the Call stores them (Call.ActionItems)."""
    return [
        {
            "Text": item["text"],
            "Owner": item.get("owner"),
            "DueDate": item.get("dueDate"),
            "DueText": item.get("dueText"),
            "StartTime": item.get("startTime"),
            "SegmentId": item.get("segmentId"),
        }
        for item in items
    ]


def action_items_output(stored: Any) -> List[Dict[str, Any]]:
    """Call.ActionItems as the MCP tools and the export name them (camelCase)."""
    out = []
    for item in stored or []:
        if not isinstance(item, Mapping) or not item.get("Text"):
            continue
        out.append(
            {
                "text": item.get("Text"),
                "owner": item.get("Owner") or None,
                "dueDate": item.get("DueDate") or None,
                "dueText": item.get("DueText") or None,
                "startTime": _number(item.get("StartTime")),
                "segmentId": item.get("SegmentId") or None,
            }
        )
    return out


__all__ = [
    "ACTION_ITEMS_PROMPT",
    "DEFAULT_PROFILE",
    "MAX_ACTION_ITEMS",
    "MalformedActionItems",
    "PROFILE_NAME",
    "UnknownSummaryProfile",
    "action_items_output",
    "nearest_segment",
    "normalize_profile_name",
    "parse_action_items",
    "profile_entries",
    "profile_listing",
    "profile_prompts",
    "require_profile",
    "stored_action_items",
    "validate_action_items",
]
