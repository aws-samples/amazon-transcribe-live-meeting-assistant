# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""
Tool: get_live_context
Read the transcript of a running meeting as it grows, a few seconds behind the meeting.

The tool is offered only while the Configuration page setting Meeting Assistant >
MCP server: live meeting context (``meeting_assist.mcpLiveContext``) is on.

How a client follows a meeting
------------------------------
The transcript rows (``PK = trs#<CallId>``, ``SK = s#<SegmentId>``) are not
stored in time order, and a final row can be written again in place with new
text, so no key range holds "what is new". Every call reads the meeting's whole
transcript and follows the time each row was stored (``CreatedAt``), its
*write time*:

* The cursor holds a watermark: the Lambda's clock when the previous answer was
  made. A call returns every line written at or after (watermark - 60 s). The
  60 second overlap brings a line again when it was rewritten or stored late
  (an assistant answer, a revision of what was heard), so the client keeps one
  line per ``segmentId`` and skips a repeat with the same text.
* A line is missed only if LMA stores it more than about 60 seconds after the
  write time it stamps on the row when it starts processing it; a difference
  between the writer's clock and the Lambda's counts against those 60 seconds.
* An answer holds at most ``maxLines`` lines and MAX_PAGE_UNITS units (see
  ``units_of``), so that whatever the text, the serialized answer stays under
  100 KB. When more are left it says ``truncated`` and its cursor names its last
  line, so the next call goes on strictly after that line with the same
  watermark and clock: a walk through a backlog has no gap and no overlap, and
  it ends.
* Write times are clamped to the Lambda's clock at the start of the walk (kept
  in the cursor), so a writer whose clock is ahead cannot put a line after the
  watermark of the answer that should have carried it. A row with no write time
  that can be read, or one more than 5 minutes ahead, is placed at the meeting's
  start plus the row's end time in the meeting (the time it was processed, less
  the processing delay), and never before the nearest earlier row in meeting
  order. The placement does not change from call to call, so the row comes once
  rather than in every answer.

Lines are ordered by (write time, segment id) to page and are shown in meeting
time order within one answer.

Logs carry the meeting id only as a 12 character SHA-256 digest, counts, and
the class names of exceptions: never transcript text, speaker names or meeting
names.
"""

import base64
import binascii
import hashlib
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import boto3

from tools.access_helper import can_read_meeting
from tools.consent_gate import require_capture_allowed
from tools.get_transcript import transcript_may_be_coming
from tools.list_active_meetings import NUMBER_TEXT

logger = logging.getLogger()

# ── the setting ─────────────────────────────────────────────────────────────

ON = "on"
OFF = "off"
UNREADABLE = "unreadable"

SETTING_OFF_MESSAGE = (
    "Live meeting context is turned off for this deployment. An Admin can turn it on in "
    "the LMA web app on the Configuration page: Meeting Assistant > MCP server: live "
    "meeting context."
)
SETTING_UNREADABLE_MESSAGE = (
    "The live meeting context setting could not be read, so the tool is not available "
    "right now; try again."
)

# ── arguments ───────────────────────────────────────────────────────────────

DEFAULT_MAX_LINES = 200
MAX_MAX_LINES = 1000
MAX_LINES_ERROR = "maxLines must be an integer"
INCLUDE_CHAT_ERROR = "includeChat must be true or false"
MEETING_ID_ERROR = "meetingId is required"
MAX_MEETING_ID_CHARS = 512

CURSOR_VERSION = 2
MAX_CURSOR_CHARS = 4096
CURSOR_INVALID = "The cursor is not valid; call again without a cursor to start over."
CURSOR_OLD = (
    "The cursor was made by an older version of this tool; call again without a cursor "
    "to start over."
)
CURSOR_OTHER_MEETING = (
    "The cursor belongs to another meeting; pass the cursor of this meeting's last answer, or none."
)

# ── reading and paging ──────────────────────────────────────────────────────

#: Each call returns the lines written this long before the watermark again.
OVERLAP_SECONDS = 60.0
#: A write time further ahead of the Lambda's clock than this is not believed.
FUTURE_LIMIT_SECONDS = 300.0
#: What one answer holds at most, in units (see units_of): each line costs
#: LINE_UNITS for the JSON around it plus the units of its fields. A unit is at
#: most 7/3 bytes of the HTTP body (a character the shared encoder escapes is
#: escaped again in the body) and LINE_UNITS covers the 137 bytes of a line's
#: names and punctuation, so the lines of an answer take at most about 70 KB.
MAX_PAGE_UNITS = 30_000
LINE_UNITS = 60
#: Where one line's text and one speaker name are cut.
MAX_LINE_CHARS = 2_000
MAX_SPEAKER_CHARS = 200
CUT_MARK = " [...]"
#: Bounds on one call: past either the call is refused. The time covers the
#: read and the work after it, so an answer is made well inside API Gateway's
#: 29 seconds.
MAX_PAGES = 100
TIME_BUDGET_SECONDS = 20.0

ASSISTANT_CHANNEL = "AGENT_ASSISTANT"
ASSISTANT_SPEAKER = "MeetingAssistant"
UNKNOWN_SPEAKER = "Unknown"
UNKNOWN_SENDER = "Unknown sender"
ENDED_STATUS = "ENDED"

ACCESS_DENIED_MESSAGE = "Access denied to this meeting"
MEETING_UNREADABLE = "The meeting could not be read; try again."
TRANSCRIPT_UNREADABLE = "The meeting's transcript could not be read; try again."
TOO_LARGE_MESSAGE = (
    "This meeting's transcript is too large to follow live in the time a call has; "
    "use get_meeting_transcript after the meeting."
)
ENDED_NOTE = (
    "The meeting has ended. Its full transcript is being prepared: get_meeting_transcript "
    "returns it once it is ready, usually within a minute, so try again then; "
    "get_meeting_summary returns the summary later."
)
# For an ended meeting whose transcript will not be written now: it was
# deleted, or the meeting ended too long ago (see get_transcript).
ENDED_KEPT_NOTE = (
    "The meeting has ended; get_meeting_transcript returns its full transcript if one "
    "is kept, and get_meeting_summary its summary."
)
OLDER_NOTE = (
    "Only the newest lines are shown; get_meeting_transcript returns the full transcript "
    "after the meeting."
)

# The attributes read from each transcript row: only those the answer and the
# speaker names use. Placeholders, because some of the names are reserved words.
TRANSCRIPT_ATTRIBUTES = (
    "SK",
    "SegmentId",
    "Channel",
    "Speaker",
    "Transcript",
    "StartTime",
    "EndTime",
    "CreatedAt",
)
MEETING_ATTRIBUTES = (
    "Owner",
    "AgentId",
    "SharedWith",
    "Status",
    "Consent",
    "SpeakerIdentities",
    "CustomerPhoneNumber",
    "CreatedAt",
    "UpdatedAt",
    "TranscriptDeletedAt",
)

_LINE_BREAKS = re.compile(r"[\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]+")


# ── the setting ─────────────────────────────────────────────────────────────


class _WatchedStore:
    """The process's ConfigurationStore, noting whether its last read raised.

    ``read_setting`` answers a failed read with the setting's default, which for
    this setting is off. The tool tells the two apart so the caller hears "try
    again" rather than "turned off" when the table could not be read.
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self.failed = False

    def get_section(self, name: str) -> Any:
        try:
            return self._store.get_section(name)
        except Exception:
            self.failed = True
            raise


# One store per process, so the section's cache tier (five minutes) spans calls.
_SETTING_STORE: Optional[_WatchedStore] = None


def live_context_state() -> str:
    """ON when the deployment has switched live meeting context on, OFF when not.

    UNREADABLE when the setting could not be read (the layer is missing, the
    table could not be read, its content could not be understood). A
    deployment with no configuration table has the shipped default, off.
    """
    global _SETTING_STORE  # pylint: disable=global-statement
    try:
        # pylint: disable-next=import-error,import-outside-toplevel
        from lma_config import (
            MEETING_ASSIST,
            ConfigurationStore,
            ConfigurationTableNotConfigured,
            read_setting,
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.error("the configuration layer could not be loaded: %s", type(error).__name__)
        return UNREADABLE
    if _SETTING_STORE is None:
        try:
            _SETTING_STORE = _WatchedStore(ConfigurationStore())
        except ConfigurationTableNotConfigured:
            return OFF
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.error("the configuration store could not be built: %s", type(error).__name__)
            return UNREADABLE
    store = _SETTING_STORE
    store.failed = False
    try:
        value = read_setting(MEETING_ASSIST.name, "mcpLiveContext", default=False, store=store)
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.error("the live meeting context setting could not be read: %s", type(error).__name__)
        return UNREADABLE
    if store.failed:
        logger.warning("the live meeting context setting could not be read")
        return UNREADABLE
    return ON if value is True else OFF


def require_live_context() -> None:
    """Refuse, before anything is read, unless the setting is on."""
    state = live_context_state()
    if state == ON:
        return
    if state == UNREADABLE:
        raise PermissionError(SETTING_UNREADABLE_MESSAGE)
    raise PermissionError(SETTING_OFF_MESSAGE)


# ── arguments ───────────────────────────────────────────────────────────────


def parse_meeting_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_MEETING_ID_CHARS:
        raise ValueError(MEETING_ID_ERROR)
    return value


def parse_max_lines(value: Any) -> int:
    """200 when absent, otherwise a whole number moved into 1 to 1,000.

    Read as list_active_meetings reads ``limit``: a numeric string or a float with
    no fraction is that number; a boolean, a fraction, anything that is not a
    number and a value that is not finite are refused.
    """
    if value is None:
        return DEFAULT_MAX_LINES
    if isinstance(value, bool):
        raise ValueError(MAX_LINES_ERROR)
    number: Any = value
    if isinstance(value, str):
        text = value.strip()
        if not NUMBER_TEXT.fullmatch(text):
            raise ValueError(MAX_LINES_ERROR)
        try:
            number = int(text)
        except ValueError:
            number = float(text)
    if isinstance(number, float):
        if not math.isfinite(number) or number != math.floor(number):
            raise ValueError(MAX_LINES_ERROR)
        number = int(number)
    if not isinstance(number, int):
        raise ValueError(MAX_LINES_ERROR)
    return max(1, min(MAX_MAX_LINES, number))


def parse_include_chat(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, bool):
        raise ValueError(INCLUDE_CHAT_ERROR)
    return value


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        # An integer too large for a float is not a time any clock gives.
        return None
    return number if math.isfinite(number) else None


def encode_cursor(meeting_id: str, watermark: float, walk: Optional[Tuple] = None) -> str:
    """A cursor: the meeting, the watermark and, inside a walk, its clock and last line."""
    state: Dict[str, Any] = {"v": CURSOR_VERSION, "m": meeting_id, "w": watermark}
    if walk is not None:
        clock, last_time, last_segment = walk
        state["n"] = clock
        state["a"] = [last_time, last_segment]
    text = json.dumps(state, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def parse_cursor(cursor: Any, meeting_id: str, now: float) -> Optional[Dict[str, Any]]:
    """The cursor's state, or None when there is none.

    Refuses a cursor that is not one this tool made, one made by an older
    version, one for another meeting, and one whose times are beyond what a
    Lambda clock could have given.
    """
    if cursor is None:
        return None
    if not isinstance(cursor, str):
        raise ValueError(CURSOR_INVALID)
    text = cursor.strip()
    if not text:
        return None
    if len(text) > MAX_CURSOR_CHARS:
        raise ValueError(CURSOR_INVALID)
    try:
        raw = base64.b64decode(text.encode("ascii"), altchars=b"-_", validate=True)
        state = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeError, ValueError):
        raise ValueError(CURSOR_INVALID) from None
    if not isinstance(state, dict):
        raise ValueError(CURSOR_INVALID)
    version = state.get("v")
    if "v" not in state or (
        isinstance(version, int) and not isinstance(version, bool) and version < 2
    ):
        raise ValueError(CURSOR_OLD)
    if version != CURSOR_VERSION or isinstance(version, bool):
        raise ValueError(CURSOR_INVALID)
    if set(state) - {"v", "m", "w", "n", "a"} or not isinstance(state.get("m"), str):
        raise ValueError(CURSOR_INVALID)
    if state["m"] != meeting_id:
        raise ValueError(CURSOR_OTHER_MEETING)
    watermark = _finite(state.get("w"))
    if watermark is None or watermark < 0 or watermark > now + FUTURE_LIMIT_SECONDS:
        raise ValueError(CURSOR_INVALID)
    parsed: Dict[str, Any] = {"w": watermark, "n": None, "after": None}
    if "n" in state or "a" in state:
        clock = _finite(state.get("n"))
        after = state.get("a")
        if (
            clock is None
            or clock < watermark
            or clock > now + FUTURE_LIMIT_SECONDS
            or not isinstance(after, list)
            or len(after) != 2
        ):
            raise ValueError(CURSOR_INVALID)
        last_time = _finite(after[0])
        last_segment = after[1]
        if (
            last_time is None
            or last_time > clock
            or not isinstance(last_segment, str)
            or len(last_segment) > MAX_CURSOR_CHARS
        ):
            raise ValueError(CURSOR_INVALID)
        parsed["n"] = clock
        parsed["after"] = (last_time, last_segment)
    return parsed


# ── reading ─────────────────────────────────────────────────────────────────


def meeting_digest(meeting_id: str) -> str:
    """How a meeting is named in the logs."""
    return hashlib.sha256(meeting_id.encode("utf-8")).hexdigest()[:12]


def _names(attributes: Tuple[str, ...], prefix: str) -> Tuple[str, Dict[str, str]]:
    names = {f"#{prefix}{i}": name for i, name in enumerate(attributes)}
    return ", ".join(names), names


def read_meeting(table, meeting_id: str, digest: str) -> Optional[Dict[str, Any]]:
    projection, names = _names(MEETING_ATTRIBUTES, "m")
    try:
        # Strongly consistent, as the consent resolver reads it: an objection
        # recorded a moment ago must already withhold the transcript.
        response = table.get_item(
            Key={"PK": f"c#{meeting_id}", "SK": f"c#{meeting_id}"},
            ProjectionExpression=projection,
            ExpressionAttributeNames=names,
            ConsistentRead=True,
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.error("meeting %s could not be read: %s", digest, type(error).__name__)
        raise ValueError(MEETING_UNREADABLE) from None
    item = response.get("Item")
    return item if isinstance(item, dict) else None


def check_time(timer: Callable[[], float], started: float, digest: str) -> None:
    """Refuse the call once TIME_BUDGET_SECONDS have passed since ``started``."""
    if timer() - started > TIME_BUDGET_SECONDS:
        logger.warning(
            "live context for meeting %s not made within %d seconds", digest, TIME_BUDGET_SECONDS
        )
        raise ValueError(TOO_LARGE_MESSAGE)


def read_transcript(
    table,
    meeting_id: str,
    include_chat: bool,
    timer: Callable[[], float],
    digest: str,
    started: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Every final row of the meeting's transcript, across all result pages.

    The filter is the meeting assistant's (query_transcript_items): the speech
    channels under both spellings, the assistant's answers and, when asked for,
    the meeting chat; partial rows left out. A Query applies the filter after
    reading, so even a moderate meeting spans pages. The read stops with an
    error after MAX_PAGES pages, or when TIME_BUDGET_SECONDS have passed since
    ``started`` (checked after every page, the last one too): an answer built
    from part of the transcript could drop lines without saying so.
    """
    # pylint: disable-next=import-outside-toplevel
    from boto3.dynamodb.conditions import Attr, Key

    # pylint: disable-next=import-error,import-outside-toplevel
    from lma_channels import MEETING_CHAT, STORED_SPEECH_CHANNELS

    channels = [*STORED_SPEECH_CHANNELS, ASSISTANT_CHANNEL]
    if include_chat:
        channels.append(MEETING_CHAT)
    projection, names = _names(TRANSCRIPT_ATTRIBUTES, "t")
    query: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"trs#{meeting_id}"),
        "FilterExpression": Attr("Channel").is_in(channels) & Attr("IsPartial").eq(False),
        "ProjectionExpression": projection,
        "ExpressionAttributeNames": names,
    }
    if started is None:
        started = timer()
    items: List[Dict[str, Any]] = []
    pages = 0
    start_key = None
    while True:
        if start_key is not None:
            query["ExclusiveStartKey"] = start_key
        try:
            response = table.query(**query)
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.error(
                "transcript of meeting %s could not be read: %s", digest, type(error).__name__
            )
            raise ValueError(TRANSCRIPT_UNREADABLE) from None
        pages += 1
        items.extend(item for item in response.get("Items", []) if isinstance(item, dict))
        check_time(timer, started, digest)
        next_key = response.get("LastEvaluatedKey")
        if not next_key:
            return items
        if next_key == start_key:
            logger.error("transcript of meeting %s returned the same page key twice", digest)
            raise ValueError(TRANSCRIPT_UNREADABLE)
        if pages >= MAX_PAGES:
            logger.warning("transcript of meeting %s not read within %d pages", digest, MAX_PAGES)
            raise ValueError(TOO_LARGE_MESSAGE)
        start_key = next_key


# ── lines ───────────────────────────────────────────────────────────────────


def _is_chat(row: Dict[str, Any]) -> bool:
    # pylint: disable-next=import-error,import-outside-toplevel
    from lma_channels import is_chat_channel

    return is_chat_channel(row.get("Channel"))


def one_line(text: Any) -> str:
    """Text on one line: line breaks become " / ", runs of spaces one space."""
    if not isinstance(text, str):
        return ""
    parts = [" ".join(part.split()) for part in _LINE_BREAKS.split(text)]
    return " / ".join(part for part in parts if part)


def line_text(row: Dict[str, Any]) -> str:
    """The text of a row as a line shows it, before the line cap."""
    if _is_chat(row):
        return one_line(row.get("Transcript"))
    text = row.get("Transcript")
    return text if isinstance(text, str) else ""


def cut_text(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """A line's text (or a speaker name), cut at ``limit`` characters and then marked."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + CUT_MARK


def units_of(text: str) -> int:
    """What a text costs in an answer's budget.

    As get_meeting_frames counts it: an ASCII character 1, any other character
    in the first plane 3, one beyond it 6, which is what the shared encoder
    writes for each (a \\uXXXX escape, two for a character beyond the first
    plane). A quote, a backslash, a control character and DEL (U+007F) count 3
    as well, because the encoder escapes them too and the HTTP body escapes the
    escape: no character takes more than 7/3 bytes of the body for each unit.
    """
    total = 0
    for char in text:
        code = ord(char)
        if code < 0x80:
            total += 3 if code < 0x20 or code == 0x7F or char in '"\\' else 1
        else:
            total += 3 if code <= 0xFFFF else 6
    return total


def meeting_seconds(row: Dict[str, Any]) -> float:
    """When a row began, in seconds from the start of the meeting (its end if no start)."""
    for name in ("StartTime", "EndTime"):
        value = _number(row.get(name))
        if value is not None:
            return value
    return 0.0


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def format_elapsed(value: Any) -> str:
    """A time from the start of the meeting as MM:SS, or H:MM:SS from one hour on.

    As the meeting assistant's current_meeting_transcript shows it, without the
    brackets.
    """
    seconds = _number(value)
    if seconds is None:
        return "--:--"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_write_time(value: Any) -> Optional[float]:
    """A CreatedAt as seconds since the epoch, or None when it cannot be read."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def segment_of(row: Dict[str, Any]) -> str:
    key = row.get("SK")
    if isinstance(key, str) and key.startswith("s#"):
        return key[2:]
    segment = row.get("SegmentId")
    return segment if isinstance(segment, str) else ""


def placed_time(row: Dict[str, Any], meeting_start: Optional[float]) -> Optional[float]:
    """When a row was about to be processed: the meeting's start plus the row's end.

    A row's StartTime and EndTime count from the start of the meeting, and LMA
    processes a row once it ends, so this is the row's write time less the
    processing delay. None without a meeting start or a time in the row.
    """
    if meeting_start is None:
        return None
    for name in ("EndTime", "StartTime"):
        value = _number(row.get(name))
        if value is not None:
            return meeting_start + value
    return None


def write_times(
    rows: List[Dict[str, Any]], clock: float, meeting_start: Optional[float] = None
) -> List[float]:
    """The write time of each row, clamped to ``clock``.

    A row whose CreatedAt cannot be read, or is more than FUTURE_LIMIT_SECONDS
    ahead of ``clock``, is placed at ``meeting_start`` plus its end time
    (placed_time), but never before the write time of the nearest earlier row
    in meeting order. Placing it by its own time keeps a row stored after a
    pause from taking the old time of the row before the pause, which every
    later answer would leave out. With neither, the row takes ``clock`` (it is
    treated as new).
    """
    order = sorted(range(len(rows)), key=lambda i: (meeting_seconds(rows[i]), segment_of(rows[i])))
    times = [clock] * len(rows)
    previous: Optional[float] = None
    for i in order:
        written = parse_write_time(rows[i].get("CreatedAt"))
        if written is not None and written <= clock + FUTURE_LIMIT_SECONDS:
            times[i] = min(written, clock)
        else:
            known = [t for t in (previous, placed_time(rows[i], meeting_start)) if t is not None]
            times[i] = min(max(known), clock) if known else clock
        previous = times[i]
    return times


def speaker_names(rows: List[Dict[str, Any]], meeting: Dict[str, Any]) -> Callable:
    """A function naming each row's speaker as the meeting page does.

    Voices are named with lma_speakers from all the speech of the meeting and the
    meeting row's SpeakerIdentities, as FetchTranscript and the meeting
    assistant do; a chat line names its sender, an assistant answer the
    assistant. When the names cannot be worked out, a row keeps its stored
    speaker.
    """
    namer = None
    try:
        # pylint: disable-next=import-error,import-outside-toplevel
        from lma_speakers import speaker_namer

        namer = speaker_namer(
            [row for row in rows if not _is_chat(row)],
            meeting.get("SpeakerIdentities"),
            meeting.get("CustomerPhoneNumber"),
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("speaker names not applied: %s", type(error).__name__)

    def stored(row: Dict[str, Any]) -> str:
        speaker = row.get("Speaker")
        return speaker.strip() if isinstance(speaker, str) and speaker.strip() else UNKNOWN_SPEAKER

    def name(row: Dict[str, Any]) -> str:
        if _is_chat(row):
            return one_line(row.get("Speaker")) or UNKNOWN_SENDER
        if row.get("Channel") == ASSISTANT_CHANNEL:
            return ASSISTANT_SPEAKER
        if namer is not None:
            try:
                named = namer(row)
                return named if isinstance(named, str) and named else UNKNOWN_SPEAKER
            except Exception as error:  # pylint: disable=broad-exception-caught
                logger.warning("a speaker name not applied: %s", type(error).__name__)
        return stored(row)

    def capped(row: Dict[str, Any]) -> str:
        # A name is one line of a few words; the cap keeps one line's size bounded.
        return cut_text(name(row), MAX_SPEAKER_CHARS)

    return capped


def _channel(row: Dict[str, Any]) -> str:
    # pylint: disable-next=import-error,import-outside-toplevel
    from lma_channels import canonical_channel

    channel = row.get("Channel")
    return canonical_channel(channel) if isinstance(channel, str) else ""


def take(entries: List[Tuple], max_lines: int, cost: Callable[[Tuple], int]) -> int:
    """How many entries, from the start, fit in one answer (at least one)."""
    used = 0
    count = 0
    for entry in entries:
        if count >= max_lines:
            break
        size = cost(entry)
        if count and used + size > MAX_PAGE_UNITS:
            break
        used += size
        count += 1
    return count


def execute(
    meeting_id: Any = None,
    cursor: Any = None,
    max_lines: Any = None,
    include_chat: Any = None,
    user_id: Optional[str] = None,
    is_admin: bool = False,
    now: Optional[Callable[[], float]] = None,
    timer: Optional[Callable[[], float]] = None,
    table: Any = None,
) -> Dict[str, Any]:
    """
    The lines of a meeting's transcript written since the cursor's watermark.

    Args:
        meeting_id: Meeting ID (CallId)
        cursor: The nextCursor of the previous answer; none for the newest lines
        max_lines: Most lines in one answer (default 200, 1 to 1,000)
        include_chat: Whether the meeting chat is among the lines (default true)
        user_id: User ID for access control
        is_admin: Whether the caller is an Admin
        now: The wall clock, in seconds since the epoch (tests only)
        timer: The clock the time budget is measured with (tests only)
        table: The EventSourcing table (tests only)

    Checks, in order, each refusing the call: the setting; the arguments; the
    meeting exists and the caller may read it; its consent settings allow its
    content out.
    """
    timer = timer or time.monotonic
    started = timer()
    require_live_context()
    meeting_id = parse_meeting_id(meeting_id)
    wanted = parse_max_lines(max_lines)
    chat = parse_include_chat(include_chat)
    clock_now = (now or time.time)()
    position = parse_cursor(cursor, meeting_id, clock_now)
    digest = meeting_digest(meeting_id)

    if table is None:
        table = boto3.resource("dynamodb").Table(os.environ.get("CALLS_TABLE"))
    meeting = read_meeting(table, meeting_id, digest)
    if meeting is None or not can_read_meeting(meeting, user_id, is_admin):
        logger.info("live context refused for meeting %s", digest)
        raise PermissionError(ACCESS_DENIED_MESSAGE)
    require_capture_allowed(meeting)

    rows = read_transcript(table, meeting_id, chat, timer, digest, started)

    if position is None:
        clock = clock_now
    elif position["after"] is None:
        # A new walk: never behind the watermark, so no cursor goes backward.
        clock = max(clock_now, position["w"])
    else:
        clock = position["n"]
    times = write_times(rows, clock, parse_write_time(meeting.get("CreatedAt")))
    name_of = speaker_names(rows, meeting)
    entries = sorted(
        (
            (written, segment_of(row), row, cut_text(line_text(row)))
            for written, row in zip(times, rows)
        ),
        key=lambda entry: (entry[0], entry[1]),
    )

    lines: Dict[int, Dict[str, Any]] = {}

    def line_of(entry: Tuple) -> Dict[str, Any]:
        _, segment, row, text = entry
        key = id(row)
        if key not in lines:
            lines[key] = {
                "t": format_elapsed(row.get("StartTime", row.get("EndTime"))),
                "speaker": name_of(row),
                "channel": _channel(row),
                "segmentId": segment,
                "text": text,
            }
        return lines[key]

    def cost(entry: Tuple) -> int:
        return LINE_UNITS + sum(units_of(value) for value in line_of(entry).values())

    notes = []
    truncated = False
    if position is None:
        # The newest lines, counted back from the newest.
        count = take(list(reversed(entries)), wanted, cost)
        page = entries[len(entries) - count :]
        if count < len(entries):
            notes.append(OLDER_NOTE)
        next_cursor = encode_cursor(meeting_id, clock)
    else:
        floor = position["w"] - OVERLAP_SECONDS
        after = position["after"]
        chosen = [
            entry
            for entry in entries
            if entry[0] >= floor and (after is None or (entry[0], entry[1]) > after)
        ]
        count = take(chosen, wanted, cost)
        page = chosen[:count]
        if count < len(chosen):
            truncated = True
            last = page[-1]
            next_cursor = encode_cursor(meeting_id, position["w"], (clock, last[0], last[1]))
        else:
            next_cursor = encode_cursor(meeting_id, clock)

    page = sorted(page, key=lambda entry: (meeting_seconds(entry[2]), entry[0], entry[1]))
    status = meeting.get("Status") if isinstance(meeting.get("Status"), str) else "UNKNOWN"
    ended = status == ENDED_STATUS
    if ended:
        coming = transcript_may_be_coming(meeting, clock_now)
        notes.insert(0, ENDED_NOTE if coming else ENDED_KEPT_NOTE)
    result: Dict[str, Any] = {
        "meetingId": meeting_id,
        "status": status,
        "ended": ended,
        "lines": [line_of(entry) for entry in page],
        "nextCursor": next_cursor,
        "truncated": truncated,
    }
    if notes:
        result["note"] = " ".join(notes)
    # The naming and ordering above count in the budget too.
    check_time(timer, started, digest)
    logger.info(
        "live context for meeting %s: %d rows read, %d lines returned, truncated %s",
        digest,
        len(rows),
        len(page),
        truncated,
    )
    return result
