# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Tools: a meeting's preparation -- its context, checklist, labels and audience.

* get_meeting_context / set_meeting_context
* get_meeting_checklist / set_meeting_checklist / update_checklist_item
* update_meeting_metadata (labels and audience)

Each reads the meeting's row (tools/access.py), so it answers the callers getCall
answers: an Admin, the meeting's owner, or a user it is shared with. Each write
is the mutation the meeting page makes (updateCallMeetingContext,
updateCallChecklist, updateCallChecklistItem, updateCallLabels and
updateCallAudience), answered by the meeting preparation resolver, which holds
the same rule. This function's IAM role is a backend caller there, so the write
names who it is for: ``UpdatedBy`` is the MCP caller and ``StatusSource`` is
``user``, because an MCP client acts for its user. See
docs/meeting-context-and-checklist.md.

The MCP arguments are camelCase: labels are an object of key to value, and a
checklist item is ``{itemId, text, kind, priority, source}``. They are checked
here with the rules the resolver applies (lma_meeting_preparation), so a client
is told which limit an input breaks before anything is sent.
"""

from typing import Any, Dict, List, Mapping, Optional

from lma_meeting_preparation import rules  # pylint: disable=import-error

from tools.access import Caller, require_meeting
from tools.appsync_client import graphql
from tools.call_fields import with_call_fields

UPDATE_CONTEXT = with_call_fields(
    """
mutation UpdateCallMeetingContext($input: UpdateCallMeetingContextInput!) {
  updateCallMeetingContext(input: $input) { __SUBSCRIBED_CALL_FIELDS__ }
}
"""
)

UPDATE_CHECKLIST = with_call_fields(
    """
mutation UpdateCallChecklist($input: UpdateCallChecklistInput!) {
  updateCallChecklist(input: $input) { __SUBSCRIBED_CALL_FIELDS__ }
}
"""
)

UPDATE_ITEM = with_call_fields(
    """
mutation UpdateCallChecklistItem($input: UpdateCallChecklistItemInput!) {
  updateCallChecklistItem(input: $input) { __SUBSCRIBED_CALL_FIELDS__ }
}
"""
)

UPDATE_LABELS = with_call_fields(
    """
mutation UpdateCallLabels($input: UpdateCallLabelsInput!) {
  updateCallLabels(input: $input) { __SUBSCRIBED_CALL_FIELDS__ }
}
"""
)

UPDATE_AUDIENCE = with_call_fields(
    """
mutation UpdateCallAudience($input: UpdateCallAudienceInput!) {
  updateCallAudience(input: $input) { __SUBSCRIBED_CALL_FIELDS__ }
}
"""
)


def _checked(check, *args, **kwargs):
    """Run one of the rules, reporting a broken one as the tools' ValueError."""
    try:
        return check(*args, **kwargs)
    except rules.PreparationError as error:
        raise ValueError(str(error)) from error


# ── the MCP shapes ───────────────────────────────────────────────────────────


def labels_input(value: Any) -> List[Dict[str, str]]:
    """``{key: value}`` as the stored ``[{Key, Value}]``, checked; ``{}`` clears them."""
    if value is None:
        return []
    if not isinstance(value, Mapping):
        raise ValueError("labels must be an object of label keys to string values")
    pairs = [{"Key": key, "Value": label} for key, label in value.items()]
    return _checked(rules.normalize_labels, pairs)


def labels_output(value: Any) -> Dict[str, str]:
    """The stored ``[{Key, Value}]`` as ``{key: value}``."""
    out: Dict[str, str] = {}
    for label in value or []:
        if isinstance(label, Mapping) and isinstance(label.get("Key"), str):
            out[label["Key"]] = str(label.get("Value") or "")
    return out


def audience_input(value: Any) -> Optional[str]:
    """``internal``, ``external``, or None (an empty string clears it)."""
    if value is not None and not isinstance(value, str):
        raise ValueError("audience must be internal or external")
    return _checked(rules.normalize_audience, (value or "").strip().lower() or None)


def context_input(value: Any) -> str:
    """The meeting context, checked; an empty string clears it."""
    if not isinstance(value, str):
        raise ValueError("context must be a string; pass an empty string to clear it")
    return _checked(rules.normalize_context, value)


ITEM_FIELDS = {"itemId": "ItemId", "text": "Text", "kind": "Kind", "priority": "Priority"}


def checklist_input(value: Any) -> List[Dict[str, Any]]:
    """``[{itemId, text, kind, priority?, source?}]`` as checklist definitions, checked."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("checklist must be a list of items")
    items = []
    for position, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"Checklist item {position} must be an object")
        unknown = sorted(set(item) - {*ITEM_FIELDS, "source"})
        if unknown:
            raise ValueError(
                f"Checklist item {position} has unknown fields {', '.join(unknown)}; an item "
                "has itemId, text, kind, priority and source"
            )
        defined = {stored: item.get(name) for name, stored in ITEM_FIELDS.items()}
        if "source" in item:
            defined["Source"] = item["source"]
        items.append(defined)
    return _checked(rules.normalize_definitions, items)


def evidence_output(entries: Any) -> List[Dict[str, Any]]:
    """An item's evidence, camelCase."""
    return [
        {
            "segmentId": entry.get("SegmentId"),
            "startTime": _number(entry.get("StartTime")),
            "quote": entry.get("Quote"),
        }
        for entry in entries or []
        if isinstance(entry, Mapping)
    ]


def _number(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def item_output(item: Mapping[str, Any]) -> Dict[str, Any]:
    """One checklist item as LMA stores it, camelCase."""
    return {
        "itemId": item.get("ItemId"),
        "text": item.get("Text"),
        "kind": item.get("Kind"),
        "priority": item.get("Priority") or rules.DEFAULT_PRIORITY,
        "source": item.get("Source"),
        "status": item.get("Status") or rules.DEFAULT_STATUS,
        "statusSource": item.get("StatusSource"),
        "evidence": evidence_output(item.get("Evidence")),
        "note": item.get("Note"),
        "updatedBy": item.get("UpdatedBy"),
        "updatedAt": item.get("UpdatedAt"),
    }


def checklist_output(call: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "items": [item_output(item) for item in call.get("Checklist") or [] if item],
        "updatedAt": call.get("ChecklistUpdatedAt"),
    }


def _call_id(meeting: Mapping[str, Any], meeting_id: Any) -> str:
    return meeting.get("CallId") or str(meeting_id).strip()


def _write(query: str, name: str, inputs: Dict[str, Any]) -> Dict[str, Any]:
    return graphql(query, {"input": inputs}).get(name) or {}


# ── meeting context ──────────────────────────────────────────────────────────


def _context_output(call_id: str, call: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "meetingId": call_id,
        "context": call.get("MeetingContext") or "",
        "updatedBy": call.get("MeetingContextUpdatedBy"),
        "updatedAt": call.get("MeetingContextUpdatedAt"),
    }


def get_context(caller: Caller, meeting_id: Any) -> Dict[str, Any]:
    """get_meeting_context."""
    meeting = require_meeting(meeting_id, caller)
    return _context_output(_call_id(meeting, meeting_id), meeting)


def set_context(caller: Caller, meeting_id: Any, context: Any) -> Dict[str, Any]:
    """set_meeting_context: replace the meeting context; an empty string clears it."""
    text = context_input(context)
    meeting = require_meeting(meeting_id, caller)
    call_id = _call_id(meeting, meeting_id)
    updated = _write(
        UPDATE_CONTEXT,
        "updateCallMeetingContext",
        {"CallId": call_id, "MeetingContext": text, "UpdatedBy": caller.user_id},
    )
    return _context_output(call_id, updated)


# ── checklist ────────────────────────────────────────────────────────────────


def get_checklist(caller: Caller, meeting_id: Any) -> Dict[str, Any]:
    """get_meeting_checklist."""
    meeting = require_meeting(meeting_id, caller)
    return {"meetingId": _call_id(meeting, meeting_id), **checklist_output(meeting)}


def set_checklist(
    caller: Caller, meeting_id: Any, items: Any, reset_statuses: Any = False
) -> Dict[str, Any]:
    """set_meeting_checklist: replace the whole checklist, by LMA's replace rule.

    An item whose itemId was already there keeps its status, evidence and note,
    unless ``resetStatuses`` is true.
    """
    if items is None:
        raise ValueError("items is required; pass an empty list to clear the checklist")
    if reset_statuses is not None and not isinstance(reset_statuses, bool):
        raise ValueError("resetStatuses must be true or false")
    definitions = checklist_input(items)
    meeting = require_meeting(meeting_id, caller)
    call_id = _call_id(meeting, meeting_id)
    updated = _write(
        UPDATE_CHECKLIST,
        "updateCallChecklist",
        {
            "CallId": call_id,
            "Checklist": definitions,
            "ResetStatuses": reset_statuses is True,
            "StatusSource": rules.STATUS_SOURCE_USER,
            "UpdatedBy": caller.user_id,
        },
    )
    return {"meetingId": call_id, **checklist_output(updated)}


def update_item(
    caller: Caller, meeting_id: Any, item_id: Any, status: Any = None, note: Any = None
) -> Dict[str, Any]:
    """update_checklist_item: one item's status or note, set as the user's."""
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError("itemId is required")
    if status is None and note is None:
        raise ValueError("Give a status or a note to change")
    if status is not None and status not in rules.STATUSES:
        raise ValueError(f"status must be one of {', '.join(rules.STATUSES)}")
    if note is not None:
        if not isinstance(note, str):
            raise ValueError("note must be a string; pass an empty string to clear it")
        if len(note) > rules.MAX_NOTE_CHARS:
            raise ValueError(f"note is longer than {rules.MAX_NOTE_CHARS} characters")
    meeting = require_meeting(meeting_id, caller)
    call_id = _call_id(meeting, meeting_id)
    item_id = item_id.strip()
    if not any(
        isinstance(item, Mapping) and item.get("ItemId") == item_id
        for item in meeting.get("Checklist") or []
    ):
        raise ValueError(f"The checklist of meeting {call_id} has no item {item_id}")
    inputs: Dict[str, Any] = {
        "CallId": call_id,
        "ItemId": item_id,
        "StatusSource": rules.STATUS_SOURCE_USER,
        "UpdatedBy": caller.user_id,
    }
    if status is not None:
        inputs["Status"] = status
    if note is not None:
        inputs["Note"] = note
    updated = _write(UPDATE_ITEM, "updateCallChecklistItem", inputs)
    item = next(
        (
            entry
            for entry in updated.get("Checklist") or []
            if isinstance(entry, Mapping) and entry.get("ItemId") == item_id
        ),
        None,
    )
    if item is None:
        raise ValueError(f"The checklist of meeting {call_id} has no item {item_id}")
    return {"meetingId": call_id, **item_output(item)}


# ── labels and audience ──────────────────────────────────────────────────────


def _metadata_output(call_id: str, call: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "meetingId": call_id,
        "labels": labels_output(call.get("Labels")),
        "audience": call.get("Audience") or None,
        "updatedAt": call.get("MeetingMetadataUpdatedAt"),
    }


def update_metadata(
    caller: Caller, meeting_id: Any, labels: Any = None, audience: Any = None
) -> Dict[str, Any]:
    """update_meeting_metadata: replace the labels and/or set the audience.

    Two writes when both are given, labels first; if the audience write then
    fails, the labels have still changed, and the error says so.
    """
    if labels is None and audience is None:
        raise ValueError("Give labels, audience or both")
    stored_labels = labels_input(labels) if labels is not None else None
    stored_audience = audience_input(audience) if audience is not None else None
    meeting = require_meeting(meeting_id, caller)
    call_id = _call_id(meeting, meeting_id)
    current: Dict[str, Any] = dict(meeting)
    if stored_labels is not None:
        current = _write(
            UPDATE_LABELS, "updateCallLabels", {"CallId": call_id, "Labels": stored_labels}
        )
    if audience is not None:
        try:
            current = _write(
                UPDATE_AUDIENCE,
                "updateCallAudience",
                {"CallId": call_id, "Audience": stored_audience},
            )
        except ValueError as error:
            if stored_labels is None:
                raise
            raise ValueError(
                f"The labels were changed, but the audience could not be: {error}"
            ) from error
    return _metadata_output(call_id, current)
