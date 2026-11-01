#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The meeting agenda as the meeting assistant reads it.

The agenda is stored on the meeting record (``Agenda``, plain text with one item
per line, and ``AgendaSource`` / ``AgendaUpdatedBy`` / ``AgendaUpdatedAt`` beside
it); see docs/meeting-agenda.md. This module turns that record into the text the
``get_meeting_agenda`` tool returns, and formats the transcript times the agent
cites when it reports progress against the agenda. It has no AWS dependency of
its own, so it is tested without one.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional

# "-", "*", "•", "1.", "1)", "a." and "a)" at the start of a line, with the
# whitespace after them -- the same marks the meeting page hides when it lists
# the agenda (ui/src/components/common/agenda.js), so the agent numbers the
# items exactly as the page does.
_LIST_MARK = re.compile(r"^\s*(?:[-*•]|\d{1,3}[.)]|[a-zA-Z][.)])\s+")

_SOURCE_DESCRIPTIONS = {
    "scheduled": "given when the Virtual Participant was started or scheduled",
    "mcp": "passed by an MCP client when it started or scheduled the Virtual Participant",
    "ui": "set on the meeting page",
    "derived": "read from the meeting transcript",
}

NO_AGENDA = (
    "This meeting has no agenda. If you were asked about the agenda or progress "
    "against it, say plainly that no agenda has been set for this meeting -- do not "
    "make one up -- and offer to list the topics discussed so far instead."
)


def parse_agenda_items(text: Optional[str]) -> List[str]:
    """The agenda's items, in order, with blank lines and list marks removed."""
    items = []
    # Split on line breaks only, as the page does (/\r?\n/), not on every
    # separator str.splitlines() knows, so both number an agenda the same way.
    for line in re.split(r"\r?\n", text or ""):
        item = _LIST_MARK.sub("", line).strip()
        if item:
            items.append(item)
    return items


def format_elapsed(seconds: Any) -> str:
    """A transcript time, seconds from the start of the meeting, as [MM:SS].

    Hours are added only when the meeting has run that long, so the common case
    reads the way the meeting page shows it.
    """
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "[--:--]"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"[{hours}:{minutes:02d}:{secs:02d}]"
    return f"[{minutes:02d}:{secs:02d}]"


def describe_agenda_for_agent(meeting: Optional[Mapping[str, Any]]) -> str:
    """The text the get_meeting_agenda tool returns for a meeting record.

    Who last changed the agenda is not included. Answers reach everyone in the
    meeting -- the voice assistant speaks them and the meeting chat posts them --
    and the editor is recorded by username, which is commonly an email address;
    nothing about the agenda's items needs it. The meeting page shows it to the
    people who can open the meeting.
    """
    items = parse_agenda_items((meeting or {}).get("Agenda"))
    if not items:
        return NO_AGENDA
    source = _SOURCE_DESCRIPTIONS.get(str(meeting.get("AgendaSource") or ""))
    provenance: List[str] = []
    if source:
        provenance.append(source)
    header = f"Meeting agenda ({len(items)} item{'s' if len(items) != 1 else ''}"
    header += f"; {'; '.join(provenance)})" if provenance else ")"
    lines = [header + ":"]
    lines.extend(f"{number}. {item}" for number, item in enumerate(items, start=1))
    return "\n".join(lines)


def read_meeting_record(table: Any, call_id: str) -> Optional[Dict[str, Any]]:
    """The agenda and its source from one meeting record, or None if there is no such meeting.

    ``table`` is a boto3 DynamoDB Table resource for the event-sourcing table,
    where the meeting record is stored under ``c#<CallId>``.
    """
    response = table.get_item(
        Key={"PK": f"c#{call_id}", "SK": f"c#{call_id}"},
        ProjectionExpression="#agenda, AgendaSource",
        ExpressionAttributeNames={"#agenda": "Agenda"},
    )
    return response.get("Item")
