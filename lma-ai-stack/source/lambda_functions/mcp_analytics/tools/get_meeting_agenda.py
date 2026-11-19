# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""
Tool: get_meeting_agenda
The agenda stored on a meeting, as numbered items and where it came from.

The agenda is plain text on the meeting row (``Agenda``, one item per line, with
``AgendaSource`` beside it; see docs/meeting-agenda.md). The numbering, the
wording of the sources and the case of a meeting with no agenda follow the
meeting assistant's own tool (``lma-meetingassist-setup-stack/src/meeting_agenda.py``),
so a client numbers the items the way the meeting page and the assistant do.
No model is called: the tool returns what is stored.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import boto3

from tools import consent_gate
from tools.access_helper import can_read_meeting
from tools.text_budget import TEXT_BUDGET, fit_texts

logger = logging.getLogger()

# Meeting ids are CallIds. Nothing longer than this is one, and a key longer than
# DynamoDB accepts would only fail later with a message that echoes it.
MAX_MEETING_ID_LENGTH = 256

# The answer is bounded whatever the agenda holds (an agenda may be 10,000
# characters, which can be 10,000 items): at most MAX_ITEMS items, and the text of
# all items together within TEXT_BUDGET units (tools/text_budget.py), shared so
# that a short item keeps its room and a long one is cut. A real agenda is a few
# lines and is returned whole. When anything is cut the answer says so.
MAX_ITEMS = 100

# A person typed these agendas, or passed them in: when the Virtual Participant was
# started or scheduled, from an MCP client, or on the meeting page. They are not
# captured content, so the consent gate does not apply to them. Any other source
# (read from the transcript, missing, or one this code does not know) is gated.
PERSON_SUPPLIED_SOURCES = frozenset({"scheduled", "mcp", "ui"})

SOURCE_DESCRIPTIONS = {
    "scheduled": "given when the Virtual Participant was started or scheduled",
    "mcp": "passed by an MCP client when it started or scheduled the Virtual Participant",
    "ui": "set on the meeting page",
    "derived": "read from the meeting transcript",
}
UNKNOWN_SOURCE_DESCRIPTION = "the source of this agenda is not recorded"
NO_AGENDA = (
    "This meeting has no agenda. Say plainly that no agenda has been set for this "
    "meeting; do not make one up."
)

ACCESS_DENIED_MESSAGE = "Access denied to this meeting"
MEETING_ID_MESSAGE = "meetingId must be a non-empty string of at most 256 characters"
FAILURE_MESSAGE = "Failed to read the meeting agenda"

# "-", "*", "•", "1.", "1)", "a." and "a)" at the start of a line, with the
# whitespace after them: the marks the meeting page hides when it lists the agenda
# (ui/src/components/common/agenda.js), so items are numbered as the page numbers them.
_LIST_MARK = re.compile(r"^\s*(?:[-*•]|\d{1,3}[.)]|[a-zA-Z][.)])\s+")


def parse_agenda_items(text: Any) -> List[str]:
    """The agenda's items, in order, with blank lines and list marks removed."""
    if not isinstance(text, str):
        return []
    items = []
    # Split on line breaks only, as the meeting page does (/\r?\n/), not on every
    # separator str.splitlines() knows, so both number an agenda the same way.
    for line in re.split(r"\r?\n", text):
        item = _LIST_MARK.sub("", line).strip()
        if item:
            items.append(item)
    return items


def check_meeting_id(meeting_id: Any) -> str:
    """The meeting id, when it can be one. Raises ValueError otherwise.

    Runs before anything is read, so the refusal says nothing about which
    meetings exist.
    """
    if (
        not isinstance(meeting_id, str)
        or not meeting_id.strip()
        or len(meeting_id) > MAX_MEETING_ID_LENGTH
    ):
        raise ValueError(MEETING_ID_MESSAGE)
    return meeting_id


def read_meeting(meeting_id: str) -> Optional[Dict[str, Any]]:
    """The meeting row with only the attributes this tool uses, or None if there is none.

    Read consistently, as the consent service reads the row it decides on: an
    objection that has just been recorded must be seen by the next call.
    """
    table = boto3.resource("dynamodb").Table(os.environ.get("CALLS_TABLE"))
    response = table.get_item(
        Key={"PK": f"c#{meeting_id}", "SK": f"c#{meeting_id}"},
        ConsistentRead=True,
        ProjectionExpression="#owner, AgentId, SharedWith, #agenda, AgendaSource, #consent",
        ExpressionAttributeNames={
            "#owner": "Owner",
            "#agenda": "Agenda",
            "#consent": "Consent",
        },
    )
    return response.get("Item")


def bounded_items(items: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The answer's items (number and text) and what was cut to bound them.

    The second value holds one sentence for each cut: items left out, texts
    shortened. An item whose text was shortened has ``textTruncated`` true; the
    others carry no such key, so an agenda that fits is returned as it always was.
    """
    notes: List[str] = []
    shown = items[:MAX_ITEMS]
    if len(items) > len(shown):
        notes.append(
            f"The agenda has {len(items)} items; the first {len(shown)} are shown "
            "and the others are left out."
        )
    fitted = fit_texts(shown, TEXT_BUDGET)
    answer = []
    for number, (text, cut) in enumerate(fitted, start=1):
        item: Dict[str, Any] = {"number": number, "text": text}
        if cut:
            item["textTruncated"] = True
        answer.append(item)
    if any(cut for _, cut in fitted):
        notes.append(
            "Some items were shortened to keep the answer small; those items have "
            "textTruncated=true."
        )
    return answer, notes


def execute(
    meeting_id: Any = None, user_id: Optional[str] = None, is_admin: bool = False
) -> Dict[str, Any]:
    """
    Get the agenda of a meeting the caller may read.

    Args:
        meeting_id: Meeting ID (CallId)
        user_id: User ID for access control
        is_admin: Whether the caller is an Admin

    Returns:
        Dict with meetingId, hasAgenda, items (number and text), source and
        sourceDescription. ``source`` is null when none is recorded or it is not
        one this server knows. An agenda of more than MAX_ITEMS items, or whose
        text is longer than the answer's budget, is cut: ``truncated`` is true
        and ``note`` says what was left out.
    """
    meeting_id = check_meeting_id(meeting_id)

    try:
        meeting = read_meeting(meeting_id)
    except Exception as e:
        # The cause (which can name resources) goes to the log, not to the caller.
        logger.error("Could not read the agenda of meeting %r: %s", meeting_id, type(e).__name__)
        raise ValueError(FAILURE_MESSAGE) from None

    # A meeting that does not exist and one the caller may not read are answered
    # alike, and the id is not repeated.
    if not meeting or not can_read_meeting(meeting, user_id, is_admin):
        logger.warning("Agenda of meeting %r refused", meeting_id)
        raise PermissionError(ACCESS_DENIED_MESSAGE)

    items = parse_agenda_items(meeting.get("Agenda"))
    stored_source = meeting.get("AgendaSource")
    if not isinstance(stored_source, str):
        stored_source = None
    known_source = stored_source if stored_source in SOURCE_DESCRIPTIONS else None

    if not items:
        logger.info("Meeting %r has no agenda", meeting_id)
        return {
            "meetingId": meeting_id,
            "hasAgenda": False,
            "items": [],
            "source": None,
            "sourceDescription": NO_AGENDA,
        }

    if stored_source not in PERSON_SUPPLIED_SOURCES:
        # Read from the transcript, or from nowhere this code can name: captured
        # content, so the meeting's consent settings decide. Fails closed.
        consent_gate.require_capture_allowed(meeting)

    logger.info("Read %d agenda items of meeting %r", len(items), meeting_id)
    answer_items, notes = bounded_items(items)
    result: Dict[str, Any] = {
        "meetingId": meeting_id,
        "hasAgenda": True,
        "items": answer_items,
        "source": known_source,
        "sourceDescription": (
            SOURCE_DESCRIPTIONS[known_source] if known_source else UNKNOWN_SOURCE_DESCRIPTION
        ),
    }
    if notes:
        result["truncated"] = True
        result["note"] = " ".join(notes)
    return result
