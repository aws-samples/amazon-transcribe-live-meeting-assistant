#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The rules of a meeting's preparation, with no AWS in them.

A meeting carries four inputs beside its agenda, stored on the meeting row
(``c#<CallId>``) and, until a Virtual Participant starts, on its row:

``Labels``
    Up to 20 key/value tags, so another system can find its meetings.
``Audience``
    ``internal``, ``external`` or absent (unspecified).
``MeetingContext``
    A Markdown briefing for the assistant and the summary.
``Checklist``
    Up to 100 items the meeting should cover, each with a status and evidence.

Everything that decides something about them lives here -- every limit, the
merge when a checklist is replaced, the rule that a status a user set is never
overwritten by the assistant, the removal of quotes when the transcript is
deleted, and the ``{checklist}`` text of a summary prompt -- so the resolver
that writes them, the Virtual Participant form's validation, the transcript
deletion function and the summary function all decide alike. See
docs/meeting-context-and-checklist.md.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Kept free of boto3 and of lma_config so that any function, test or script can
# import the rules.

MAX_LABELS = 20
LABEL_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
MAX_LABEL_VALUE_CHARS = 256

AUDIENCES = ("internal", "external")

MAX_CONTEXT_CHARS = 20000

MAX_CHECKLIST_ITEMS = 100
ITEM_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
MAX_ITEM_TEXT_CHARS = 500
KINDS = ("question", "topic", "risk", "confirm", "decision", "action")
PRIORITIES = ("must", "should", "could")
DEFAULT_PRIORITY = "should"
MAX_ITEM_SOURCE_CHARS = 256
STATUSES = ("open", "covered", "partial", "deferred", "not_applicable")
DEFAULT_STATUS = "open"
STATUS_SOURCE_USER = "user"
STATUS_SOURCE_ASSISTANT = "assistant"
STATUS_SOURCES = (STATUS_SOURCE_USER, STATUS_SOURCE_ASSISTANT)
MAX_NOTE_CHARS = 1000
MAX_QUOTE_CHARS = 300
MAX_SEGMENT_ID_CHARS = 128

# Server limits beyond the per-field ones, so that a checklist always fits on
# the meeting row (DynamoDB's 400 KB item limit, shared with the summary, the
# speaker names and the consent record) and in one subscription message.
MAX_EVIDENCE_PER_ITEM = 5
MAX_CHECKLIST_BYTES = 128 * 1024

# The fields of a checklist item that say what it is, as a caller gives them
# when it sets or replaces the checklist, and the ones that say how far it got,
# which only an item update changes.
DEFINITION_FIELDS = ("ItemId", "Text", "Kind", "Priority", "Source")
PROGRESS_FIELDS = ("Status", "StatusSource", "Evidence", "Note", "UpdatedBy", "UpdatedAt")


class PreparationError(ValueError):
    """An input that breaks one of the rules; the message says which."""


class StatusSetByUser(PreparationError):
    """The assistant tried to change an item whose status a user set."""


class ItemNotFound(PreparationError):
    """An item update names an item the checklist does not have."""


# ── small checks ─────────────────────────────────────────────────────────────


def _text(value: Any, name: str, limit: int, *, required: bool = False) -> Optional[str]:
    """A string field: None when absent or blank (unless required), else the text."""
    if value is None:
        if required:
            raise PreparationError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise PreparationError(f"{name} must be a string")
    if len(value) > limit:
        raise PreparationError(f"{name} is longer than {limit} characters")
    if not value.strip():
        if required:
            raise PreparationError(f"{name} is required")
        return None
    return value


def _one_of(
    value: Any, allowed: Sequence[str], name: str, default: Optional[str] = None
) -> Optional[str]:
    if value is None or value == "":
        return default
    if value not in allowed:
        raise PreparationError(f"{name} must be one of {', '.join(allowed)}")
    return value


# ── labels, audience, context ────────────────────────────────────────────────


def normalize_labels(value: Any) -> List[Dict[str, str]]:
    """The labels as stored: a list of ``{"Key", "Value"}``, ``[]`` for none.

    Keys are unique; a key repeated is refused rather than one of its values
    chosen, so a caller never has a label silently dropped.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise PreparationError("Labels must be a list of Key and Value pairs")
    if len(value) > MAX_LABELS:
        raise PreparationError(f"A meeting has at most {MAX_LABELS} labels")
    labels: List[Dict[str, str]] = []
    seen = set()
    for label in value:
        if not isinstance(label, Mapping):
            raise PreparationError("Each label must have a Key and a Value")
        key = label.get("Key")
        if not isinstance(key, str) or not LABEL_KEY.fullmatch(key):
            raise PreparationError(
                "A label Key is 1 to 63 characters of lower-case letters, digits, '.', '_' and '-', "
                "starting with a letter or digit"
            )
        if key in seen:
            raise PreparationError(f"The label Key {key} is given more than once")
        seen.add(key)
        label_value = label.get("Value")
        if not isinstance(label_value, str):
            raise PreparationError(f"The label {key} must have a Value")
        if len(label_value) > MAX_LABEL_VALUE_CHARS:
            raise PreparationError(
                f"The Value of label {key} is longer than {MAX_LABEL_VALUE_CHARS} characters"
            )
        labels.append({"Key": key, "Value": label_value})
    return labels


def normalize_audience(value: Any) -> Optional[str]:
    """``internal``, ``external``, or None for unspecified (an empty value clears it)."""
    return _one_of(value, AUDIENCES, "Audience")


def normalize_context(value: Any) -> str:
    """The meeting context as stored; ``""`` clears it."""
    text = _text(value, "MeetingContext", MAX_CONTEXT_CHARS)
    return text or ""


# ── the checklist ────────────────────────────────────────────────────────────


def normalize_definition(item: Any, position: int) -> Dict[str, Any]:
    """One item as a caller defines it: ItemId, Text, Kind, Priority and Source."""
    where = f"Checklist item {position}"
    if not isinstance(item, Mapping):
        raise PreparationError(f"{where} must be an object")
    item_id = item.get("ItemId")
    if not isinstance(item_id, str) or not ITEM_ID.fullmatch(item_id):
        raise PreparationError(
            f"{where}: ItemId is 1 to 64 characters of letters, digits, '.', '_', ':' and '-'"
        )
    where = f"Checklist item {item_id}"
    text = _text(item.get("Text"), f"{where}: Text", MAX_ITEM_TEXT_CHARS, required=True)
    kind = item.get("Kind")
    if kind not in KINDS:
        raise PreparationError(f"{where}: Kind must be one of {', '.join(KINDS)}")
    priority = _one_of(item.get("Priority"), PRIORITIES, f"{where}: Priority", DEFAULT_PRIORITY)
    source = item.get("Source")
    if source is not None:
        if not isinstance(source, str):
            raise PreparationError(f"{where}: Source must be a string")
        if len(source) > MAX_ITEM_SOURCE_CHARS:
            raise PreparationError(
                f"{where}: Source is longer than {MAX_ITEM_SOURCE_CHARS} characters"
            )
    defined: Dict[str, Any] = {"ItemId": item_id, "Text": text, "Kind": kind, "Priority": priority}
    # Returned unchanged: an opaque reference of the caller's, an empty one kept.
    if source is not None:
        defined["Source"] = source
    return defined


def normalize_definitions(value: Any) -> List[Dict[str, Any]]:
    """A checklist as a caller gives it, checked: at most 100 items, unique ItemIds."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise PreparationError("Checklist must be a list of items")
    if len(value) > MAX_CHECKLIST_ITEMS:
        raise PreparationError(f"A checklist has at most {MAX_CHECKLIST_ITEMS} items")
    items = [normalize_definition(item, position) for position, item in enumerate(value, start=1)]
    seen = set()
    for item in items:
        if item["ItemId"] in seen:
            raise PreparationError(f"The ItemId {item['ItemId']} is used by more than one item")
        seen.add(item["ItemId"])
    return items


def new_item(definition: Mapping[str, Any], updated_by: str, now: str) -> Dict[str, Any]:
    """A checklist item that has just been added: open, with no evidence or note."""
    return {
        **{key: definition[key] for key in DEFINITION_FIELDS if key in definition},
        "Status": DEFAULT_STATUS,
        "StatusSource": None,
        "Evidence": [],
        "Note": None,
        "UpdatedBy": updated_by,
        "UpdatedAt": now,
    }


def _definition_of(item: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: item.get(key) for key in DEFINITION_FIELDS}


def check_replace_keeps_user_statuses(
    existing: Optional[Iterable[Mapping[str, Any]]],
    definitions: Sequence[Mapping[str, Any]],
    *,
    reset_statuses: bool,
) -> None:
    """Refuse an assistant's replace that would undo what a user set.

    A status a user set is changed by a user alone, so an ``assistant`` replace
    of the checklist may not start such an item again (``reset_statuses``),
    leave it out, or change what it is (its text, kind, priority or source).
    The whole replace is refused, with nothing written, rather than applied
    with some items quietly kept. A user's replace is not checked.
    """
    wanted = {definition["ItemId"]: definition for definition in definitions}
    for item in existing or []:
        if not isinstance(item, Mapping) or item.get("StatusSource") != STATUS_SOURCE_USER:
            continue
        item_id = item.get("ItemId")
        if reset_statuses:
            reason = "starts it again as open"
        elif item_id not in wanted:
            reason = "leaves it out"
        elif _definition_of(item) != _definition_of(new_item(wanted[item_id], "", "")):
            reason = "changes it"
        else:
            continue
        raise StatusSetByUser(
            f"The status of checklist item {item_id} was set by a user, and this replace {reason}; "
            "only a user can do that"
        )


def merge_checklist(
    existing: Optional[Iterable[Mapping[str, Any]]],
    definitions: Sequence[Mapping[str, Any]],
    *,
    reset_statuses: bool,
    updated_by: str,
    now: str,
) -> List[Dict[str, Any]]:
    """The checklist after it is replaced with ``definitions`` (already normalized).

    The new list's order and definitions are the caller's. An item whose ItemId
    was already present keeps its Status, StatusSource, Evidence and Note --
    and, when its definition is unchanged, who changed it last and when --
    unless ``reset_statuses``, which starts every item again as open.
    """
    previous = {item.get("ItemId"): item for item in (existing or []) if isinstance(item, Mapping)}
    merged = []
    for definition in definitions:
        before = previous.get(definition["ItemId"])
        if before is None or reset_statuses:
            merged.append(new_item(definition, updated_by, now))
            continue
        item = new_item(definition, updated_by, now)
        for key in ("Status", "StatusSource", "Evidence", "Note"):
            item[key] = before.get(key)
        item["Status"] = item["Status"] if item["Status"] in STATUSES else DEFAULT_STATUS
        item["Evidence"] = list(item["Evidence"] or [])
        if _definition_of(before) == _definition_of(item):
            item["UpdatedBy"] = before.get("UpdatedBy")
            item["UpdatedAt"] = before.get("UpdatedAt")
        merged.append(item)
    return merged


def normalize_evidence(value: Any, *, quotes_allowed: bool) -> List[Dict[str, Any]]:
    """A list of evidence entries, checked. Quotes are dropped when not allowed.

    An entry points at the transcript: a segment id, a time in seconds from the
    meeting's start, and a quote of at most 300 characters. At least one of the
    three is needed. When the meeting's transcript has been deleted, a quote is
    transcript content that is no longer kept, so it is not stored.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise PreparationError("Evidence must be a list")
    entries = []
    for position, entry in enumerate(value, start=1):
        where = f"Evidence {position}"
        if not isinstance(entry, Mapping):
            raise PreparationError(f"{where} must be an object")
        segment_id = _text(entry.get("SegmentId"), f"{where}: SegmentId", MAX_SEGMENT_ID_CHARS)
        start_time = entry.get("StartTime")
        if start_time is not None:
            if isinstance(start_time, bool) or not isinstance(start_time, (int, float)):
                raise PreparationError(f"{where}: StartTime must be a number of seconds")
            if not math.isfinite(float(start_time)) or start_time < 0:
                raise PreparationError(f"{where}: StartTime must be zero or more seconds")
            start_time = float(start_time)
        quote = _text(entry.get("Quote"), f"{where}: Quote", MAX_QUOTE_CHARS)
        if not quotes_allowed:
            quote = None
        if segment_id is None and start_time is None and quote is None:
            if not quotes_allowed and entry.get("Quote"):
                # Only a quote, and the transcript it came from is gone.
                continue
            raise PreparationError(f"{where} needs a SegmentId, a StartTime or a Quote")
        entries.append({"SegmentId": segment_id, "StartTime": start_time, "Quote": quote})
    return entries


def newest_evidence(entries: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """At most MAX_EVIDENCE_PER_ITEM entries, keeping the newest (the last given)."""
    return [dict(entry) for entry in list(entries)[-MAX_EVIDENCE_PER_ITEM:]]


def apply_item_update(
    items: Sequence[Mapping[str, Any]],
    update: Mapping[str, Any],
    *,
    status_source: str,
    updated_by: str,
    now: str,
    quotes_allowed: bool,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """The checklist after one item's status, note or evidence changes, and the item.

    ``update`` names the item (``ItemId``) and any of ``Status``, ``Note`` (an
    empty note clears it), ``Evidence`` (replaces the item's evidence) and
    ``AddEvidence`` (added after it). Either way the newest five entries are
    kept. ``status_source`` is who is making the change: ``user`` or
    ``assistant``. A status a user set is never changed by the assistant, so
    an assistant's update to such an item is refused as a whole.
    """
    if status_source not in STATUS_SOURCES:
        raise PreparationError(f"StatusSource must be one of {', '.join(STATUS_SOURCES)}")
    item_id = update.get("ItemId")
    if not isinstance(item_id, str) or not item_id:
        raise PreparationError("ItemId is required")
    changes = [
        key for key in ("Status", "Note", "Evidence", "AddEvidence") if update.get(key) is not None
    ]
    if not changes:
        raise PreparationError("Give a Status, a Note or Evidence to change")
    updated = [dict(item) for item in items]
    index = next((i for i, item in enumerate(updated) if item.get("ItemId") == item_id), None)
    if index is None:
        raise ItemNotFound(f"The checklist has no item {item_id}")
    item = updated[index]
    if status_source == STATUS_SOURCE_ASSISTANT and item.get("StatusSource") == STATUS_SOURCE_USER:
        raise StatusSetByUser(
            f"The status of checklist item {item_id} was set by a user, so only a user can change it"
        )
    if update.get("Status") is not None:
        if update["Status"] not in STATUSES:
            # An empty status is refused too: every item always has one.
            raise PreparationError(f"Status must be one of {', '.join(STATUSES)}")
        item["Status"] = update["Status"]
        item["StatusSource"] = status_source
    if update.get("Note") is not None:
        item["Note"] = _text(update["Note"], "Note", MAX_NOTE_CHARS)
    evidence = list(item.get("Evidence") or [])
    if update.get("Evidence") is not None:
        evidence = normalize_evidence(update["Evidence"], quotes_allowed=quotes_allowed)
    if update.get("AddEvidence") is not None:
        evidence += normalize_evidence(update["AddEvidence"], quotes_allowed=quotes_allowed)
    item["Evidence"] = newest_evidence(evidence)
    item["UpdatedBy"] = updated_by
    item["UpdatedAt"] = now
    return updated, item


def checklist_bytes(items: Sequence[Mapping[str, Any]]) -> int:
    """The stored size of a checklist, near enough: its JSON in UTF-8."""
    return len(json.dumps(list(items), ensure_ascii=False, default=str).encode("utf-8"))


def check_checklist_size(items: Sequence[Mapping[str, Any]]) -> None:
    if checklist_bytes(items) > MAX_CHECKLIST_BYTES:
        raise PreparationError(
            f"The checklist would be larger than {MAX_CHECKLIST_BYTES // 1024} KB; "
            "shorten its items, notes or evidence"
        )


def strip_quotes(items: Optional[Iterable[Mapping[str, Any]]]) -> Tuple[List[Dict[str, Any]], bool]:
    """The checklist with every evidence quote removed, and whether any was.

    Used when the meeting's transcript is deleted: a quote is transcript
    content. The segment id and the time are kept, so the evidence still says
    where in the meeting it was.
    """
    stripped = []
    changed = False
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        copy = dict(item)
        evidence = []
        for entry in item.get("Evidence") or []:
            if not isinstance(entry, Mapping):
                continue
            kept = dict(entry)
            if kept.get("Quote") is not None:
                kept["Quote"] = None
                changed = True
            evidence.append(kept)
        copy["Evidence"] = evidence
        stripped.append(copy)
    return stripped, changed


# ── text for a prompt ────────────────────────────────────────────────────────


def format_elapsed(seconds: Any) -> str:
    """Seconds from the meeting's start as m:ss, or h:mm:ss past an hour."""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def render_checklist(
    items: Optional[Iterable[Mapping[str, Any]]], *, with_notes: bool = True
) -> str:
    """The checklist as numbered lines, one per item with its status; "" when empty.

    For example ``2. [partial] (must, question) Who owns the budget? Evidence at 12:04.``
    This is the text of the ``{checklist}`` summary placeholder. The meeting
    assistant's checklist tool builds its own lines
    (lma-meetingassist-setup-stack/src/meeting_preparation.py), with the
    transcript times in the form its other tools use.
    """
    lines = []
    for number, item in enumerate((i for i in (items or []) if isinstance(i, Mapping)), start=1):
        status = item.get("Status") if item.get("Status") in STATUSES else DEFAULT_STATUS
        priority = item.get("Priority") or DEFAULT_PRIORITY
        kind = item.get("Kind") or "topic"
        text = " ".join(str(item.get("Text") or "").split())
        line = f"{number}. [{status}] ({priority}, {kind}) {text}"
        times = [
            format_elapsed(entry.get("StartTime"))
            for entry in item.get("Evidence") or []
            if isinstance(entry, Mapping) and entry.get("StartTime") is not None
        ]
        if times:
            line += f" Evidence at {', '.join(times)}."
        note = " ".join(str(item.get("Note") or "").split())
        if with_notes and note:
            line += f" Note: {note}"
        lines.append(line)
    return "\n".join(lines)
