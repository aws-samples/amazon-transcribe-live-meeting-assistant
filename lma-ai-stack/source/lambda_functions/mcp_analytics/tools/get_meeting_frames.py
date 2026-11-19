# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""
Tool: get_meeting_frames
The key frames of a meeting's shared screen: when each was on screen, its title
and the text read from it.

A key frame is a picture of the shared screen kept each time it changed and held
still (docs/meeting-video.md). Frames are items under ``kf#<CallId>`` in the
event sourcing table, one per frame, in time order by sort key. They are read in
two steps so that a meeting with hundreds of frames does not cost hundreds of
texts: step 1 reads only the attributes that choosing frames needs (id, time,
what the frame shows), and step 2 reads the title and text of the frames chosen.

Choosing follows the meeting assistant's own tool
(``lma-meetingassist-setup-stack/src/meeting_frames.py``): each frame is on
screen from its own time until the next frame replaces it, frames of the
meeting's camera video are left out after those spans are worked out, and a time
range keeps the frames that were on screen at any point in it.

Only text is returned. The pictures, and the links that would open them, are never
read.
"""

import logging
import math
import os
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import boto3

from tools import consent_gate
from tools.access_helper import can_read_meeting
from tools.text_budget import TEXT_BUDGET, cut_to_units, fit_texts, plain_text, units_of

logger = logging.getLogger()

DEFAULT_LAST = 3
MAX_LAST = 20
# The most frames one answer holds, with or without a time range.
MAX_FRAMES = 20

# Step 1 reads the frames of a meeting in pages of PAGE_SIZE, at most MAX_PAGES
# pages: 2,000 frames, twice what a meeting keeps.
PAGE_SIZE = 100
MAX_PAGES = 20

# Step 2 asks for up to 20 frames' text at once. Keys DynamoDB leaves unprocessed
# are asked for again, BATCH_ATTEMPTS requests in all, pausing 0.05 * 2**attempt
# seconds between them.
BATCH_ATTEMPTS = 4

# The titles and the texts of one answer share TEXT_BUDGET units (see
# tools/text_budget.py: an ASCII character counts 1, any other character in the
# first plane 3, a character beyond it 6), however many frames the answer has.
# A title is one line, far shorter than these limits in practice; it is cut to
# TITLE_LIMIT characters and to TITLE_UNITS units, and what the titles use is
# taken off the budget the texts share.
TITLE_LIMIT = 200
TITLE_UNITS = 200
# A frame id is cut to this many characters (the key frame processor refuses
# longer ones), and what the ids use is taken off the budget too.
FRAME_ID_LIMIT = 64

# A frame of the meeting's camera video rather than shared content, as the key
# frame text reader stores it.
CAMERA_CONTENT = "CAMERA"

ACCESS_DENIED_MESSAGE = "Access denied to this meeting"
MEETING_ID_MESSAGE = "meetingId must be a non-empty string of at most 256 characters"
MAX_MEETING_ID_LENGTH = 256
LAST_MESSAGE = f"last must be a whole number from 1 to {MAX_LAST}"
FAILURE_MESSAGE = "Failed to read the meeting frames"
UNREAD_MESSAGE = "Some frames could not be read; try again"
NO_FRAMES_NOTE = (
    "This meeting has no key frames: nothing was screen-shared while its video was "
    "being recorded, or its video is not recorded."
)


def check_meeting_id(meeting_id: Any) -> str:
    """The meeting id, when it can be one. Raises ValueError otherwise."""
    if (
        not isinstance(meeting_id, str)
        or not meeting_id.strip()
        or len(meeting_id) > MAX_MEETING_ID_LENGTH
    ):
        raise ValueError(MEETING_ID_MESSAGE)
    return meeting_id


def parse_last(value: Any) -> int:
    """How many of the latest frames to return: 3 when absent, otherwise 1 to 20.

    A whole number, or a float with no fraction, is taken as it is; a number
    outside 1 to 20 is moved to the nearest end. A boolean, anything that is not
    a number (a string included), a fraction and a value that is not finite are
    refused.
    """
    if value is None:
        return DEFAULT_LAST
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(LAST_MESSAGE)
    if isinstance(value, float):
        if not math.isfinite(value) or value != math.floor(value):
            raise ValueError(LAST_MESSAGE)
        value = int(value)
    return max(1, min(MAX_LAST, value))


def parse_seconds(value: Any, name: str) -> Optional[float]:
    """A time in seconds from the start of the meeting, or None when not given.

    Refuses a boolean, anything that is not a number (a string included), a value
    that is not finite or is negative, and an integer too large for a float.
    """
    if value is None:
        return None
    message = f"{name} must be a number of seconds, 0 or more, from the start of the meeting"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(message) from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(message)
    return number


def format_time(seconds: Any) -> str:
    """A meeting time as the transcript shows it: MM:SS, or H:MM:SS from an hour."""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError, OverflowError):
        return "--:--"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def shows_shared_content(frame: Mapping[str, Any]) -> bool:
    """Whether a frame shows shared content: all but CAMERA ones (none, before it was read)."""
    return frame.get("ContentKind") != CAMERA_CONTENT


def with_spans(
    frames: Sequence[Mapping[str, Any]],
) -> List[Tuple[Mapping[str, Any], Optional[float]]]:
    """Each frame in time order with the time the next one replaced it (None for the last)."""
    ordered = sorted(
        (f for f in frames if isinstance(f.get("Timestamp"), float)),
        key=lambda f: f["Timestamp"],
    )
    return [
        (frame, ordered[i + 1]["Timestamp"] if i + 1 < len(ordered) else None)
        for i, frame in enumerate(ordered)
    ]


def shared_spans(
    frames: Sequence[Mapping[str, Any]],
) -> List[Tuple[Mapping[str, Any], Optional[float]]]:
    """The spans of the frames that show shared content.

    The spans are worked out with the camera frames in, and those frames are left
    out after, so a slide is on screen until the camera replaced it.
    """
    return [span for span in with_spans(frames) if shows_shared_content(span[0])]


def on_screen_in(
    spans: Sequence[Tuple[Mapping[str, Any], Optional[float]]],
    start: Optional[float],
    end: Optional[float],
) -> List[Tuple[Mapping[str, Any], Optional[float]]]:
    """The spans of the frames that were on screen at any point of the time range."""
    lo = start if start is not None else 0.0
    hi = end if end is not None else float("inf")
    return [s for s in spans if s[0]["Timestamp"] <= hi and (s[1] is None or s[1] > lo)]


def select_frames(
    frames: Sequence[Mapping[str, Any]],
    *,
    last: int = 1,
    start: Optional[float] = None,
    end: Optional[float] = None,
) -> List[Tuple[Mapping[str, Any], Optional[float]]]:
    """The frames a question is about, each with when it left the screen.

    ``start``/``end``: those on screen at any point in the range. Otherwise the
    last ``last`` (at least one). At most MAX_FRAMES, keeping the latest.
    """
    spans = shared_spans(frames)
    if not spans:
        return []
    if start is not None or end is not None:
        return on_screen_in(spans, start, end)[-MAX_FRAMES:]
    count = max(1, min(MAX_FRAMES, int(last or 1)))
    return spans[-count:]


def number_of(value: Any) -> Optional[float]:
    """A stored number as a float (DynamoDB returns Decimal), or None if it is not one."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def read_meeting(table, meeting_id: str) -> Optional[Dict[str, Any]]:
    """The meeting row with only what access and consent need, or None if there is none.

    Read consistently, as the consent service reads the row it decides on: an
    objection that has just been recorded must be seen by the next call.
    """
    response = table.get_item(
        Key={"PK": f"c#{meeting_id}", "SK": f"c#{meeting_id}"},
        ConsistentRead=True,
        ProjectionExpression="#owner, AgentId, SharedWith, #consent",
        ExpressionAttributeNames={"#owner": "Owner", "#consent": "Consent"},
    )
    return response.get("Item")


def step_one_query(meeting_id: str) -> Dict[str, Any]:
    """The query that lists a meeting's frames with the attributes choosing them needs."""
    from boto3.dynamodb.conditions import Key

    return {
        "KeyConditionExpression": Key("PK").eq(f"kf#{meeting_id}"),
        "ProjectionExpression": "SK, FrameId, #t, ContentKind",
        "ExpressionAttributeNames": {"#t": "Timestamp"},
        "Limit": PAGE_SIZE,
    }


def read_frames(table, meeting_id: str) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    """Step 1: the frames of the meeting, oldest first, without their text.

    Returns the frames (``SK``, ``FrameId``, ``Timestamp`` as a float and
    ``ContentKind``) and, when the page cap ended the read with frames really left,
    the time of the last frame read (otherwise None).
    """
    query = step_one_query(meeting_id)
    frames: List[Dict[str, Any]] = []
    last_key = None
    pages = 0
    while pages < MAX_PAGES:
        kwargs = dict(query)
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        response = table.query(**kwargs)
        pages += 1
        for item in response.get("Items", []):
            if not isinstance(item.get("SK"), str):
                continue
            frames.append(
                {
                    "SK": item["SK"],
                    "FrameId": item.get("FrameId"),
                    "Timestamp": number_of(item.get("Timestamp")),
                    "ContentKind": item.get("ContentKind"),
                }
            )
        next_key = response.get("LastEvaluatedKey")
        if not next_key or next_key == last_key:
            return frames, None
        last_key = next_key

    # The cap ended the read. DynamoDB returns a key after a page that ends exactly
    # at the end of the data, so the key alone does not say whether frames are left.
    if not rows_left(table, query, last_key):
        return frames, None
    stamps = [f["Timestamp"] for f in frames if f["Timestamp"] is not None]
    return frames, (stamps[-1] if stamps else 0.0)


def rows_left(table, query: Dict[str, Any], last_key: Dict[str, Any]) -> bool:
    """Whether a frame follows ``last_key``. When the check itself fails the answer is yes."""
    try:
        probe = dict(query, Limit=1, ExclusiveStartKey=last_key)
        return bool(table.query(**probe).get("Items"))
    except Exception as e:
        logger.warning("Could not check for more key frames: %s", type(e).__name__)
        return True


def read_texts(table, meeting_id: str, sort_keys: Sequence[str]) -> Dict[str, Mapping[str, Any]]:
    """Step 2: the title and text of the frames with these sort keys, by sort key.

    Keys DynamoDB leaves unprocessed are asked for again; any still unread after
    BATCH_ATTEMPTS requests, and a request that fails, raise ValueError. A frame
    that no longer exists is simply absent from the result.
    """
    found: Dict[str, Mapping[str, Any]] = {}
    keys = [{"PK": f"kf#{meeting_id}", "SK": sort_key} for sort_key in sort_keys]
    for attempt in range(BATCH_ATTEMPTS):
        try:
            response = table.meta.client.batch_get_item(
                RequestItems={
                    table.name: {
                        "Keys": keys,
                        "ProjectionExpression": "SK, #title, SlideText",
                        "ExpressionAttributeNames": {"#title": "Title"},
                    }
                }
            )
        except Exception as e:
            logger.error(
                "Batch read of %d frames of meeting %r failed: %s",
                len(keys),
                meeting_id,
                type(e).__name__,
            )
            raise ValueError(FAILURE_MESSAGE) from None
        for item in response.get("Responses", {}).get(table.name, []):
            if isinstance(item.get("SK"), str):
                found[item["SK"]] = item
        keys = (response.get("UnprocessedKeys") or {}).get(table.name, {}).get("Keys") or []
        if not keys:
            return found
        if attempt + 1 < BATCH_ATTEMPTS:
            time.sleep(0.05 * 2**attempt)
    logger.error("%d frames of meeting %r were not read", len(keys), meeting_id)
    raise ValueError(UNREAD_MESSAGE)


def seconds_of(value: Optional[float]) -> Optional[float]:
    """A time in seconds to a thousandth, or None."""
    return None if value is None else round(value, 3)


def check_arguments(
    meeting_id: Any, last: Any, from_seconds: Any, to_seconds: Any
) -> Tuple[str, int, Optional[float], Optional[float]]:
    """The arguments, checked. Raises ValueError for any that cannot be used.

    Runs before anything is read, so a refusal says nothing about which meetings exist.
    """
    meeting_id = check_meeting_id(meeting_id)
    wanted_last = parse_last(last)
    start = parse_seconds(from_seconds, "fromSeconds")
    end = parse_seconds(to_seconds, "toSeconds")
    if start is not None and end is not None and start > end:
        raise ValueError("fromSeconds must not be after toSeconds")
    return meeting_id, wanted_last, start, end


def open_meeting(meeting_id: str, user_id: Optional[str], is_admin: bool):
    """The table, once the caller is known to be allowed the meeting's frames.

    Raises PermissionError when the meeting does not exist or the caller may not
    read it (the same answer for both), or when its consent settings do not allow
    capture.
    """
    try:
        table = boto3.resource("dynamodb").Table(os.environ.get("CALLS_TABLE"))
        meeting = read_meeting(table, meeting_id)
    except Exception as e:
        # The cause (which can name resources) goes to the log, not to the caller.
        logger.error("Could not read meeting %r: %s", meeting_id, type(e).__name__)
        raise ValueError(FAILURE_MESSAGE) from None

    # A meeting that does not exist and one the caller may not read are answered
    # alike, and the id is not repeated.
    if not meeting or not can_read_meeting(meeting, user_id, is_admin):
        logger.warning("Frames of meeting %r refused", meeting_id)
        raise PermissionError(ACCESS_DENIED_MESSAGE)
    # Frames are captured content: the meeting's consent settings always decide.
    consent_gate.require_capture_allowed(meeting)
    return table


def selection_notes(
    spans: Sequence[Tuple[Mapping[str, Any], Optional[float]]],
    selected: Sequence[Tuple[Mapping[str, Any], Optional[float]]],
    start: Optional[float],
    end: Optional[float],
) -> List[str]:
    """What the answer says about how the frames were chosen, when there is something to say."""
    notes: List[str] = []
    in_range = start is not None or end is not None
    if in_range:
        on_screen = len(on_screen_in(spans, start, end))
        if on_screen > MAX_FRAMES:
            notes.append(
                f"{on_screen} frames were on screen in that range; the latest {MAX_FRAMES} "
                "are shown. Ask for a shorter range to see the others."
            )
    if not spans:
        notes.append(NO_FRAMES_NOTE)
    elif not selected and in_range:
        first = min(frame["Timestamp"] for frame, _ in spans)
        notes.append(
            f"None of the meeting's {len(spans)} key frames was on screen in that range; "
            f"the first went up at {format_time(first)}."
        )
    return notes


def title_of(stored: Mapping[str, Any]) -> Optional[str]:
    """The frame's title as the answer shows it, or None when it has none.

    Cut to TITLE_LIMIT characters and TITLE_UNITS units, so that the titles of
    twenty frames take a bounded part of the answer's budget.
    """
    title = stored.get("Title")
    if not isinstance(title, str) or not title.strip():
        return None
    return cut_to_units(plain_text(title.strip())[:TITLE_LIMIT], TITLE_UNITS)


def frame_id_of(frame: Mapping[str, Any]) -> str:
    """The frame's id as the answer shows it: as stored, cut to FRAME_ID_LIMIT characters."""
    frame_id = frame.get("FrameId") or str(frame["SK"])[2:]
    return str(frame_id)[:FRAME_ID_LIMIT]


def answer_frame(
    frame: Mapping[str, Any],
    until: Optional[float],
    frame_id: str,
    title: Optional[str],
    text: Optional[str],
    cut: bool,
) -> Dict[str, Any]:
    """One frame as the answer shows it, with its id, title and text already cut."""
    went_up = frame["Timestamp"]
    return {
        "frameId": frame_id,
        "fromSeconds": seconds_of(went_up),
        "toSeconds": seconds_of(until),
        "from": format_time(went_up),
        "to": format_time(until) if until is not None else None,
        "title": title,
        "text": text,
        "textTruncated": cut,
    }


def execute(
    meeting_id: Any = None,
    last: Any = None,
    from_seconds: Any = None,
    to_seconds: Any = None,
    user_id: Optional[str] = None,
    is_admin: bool = False,
) -> Dict[str, Any]:
    """
    Get the key frames of a meeting's shared screen, as text.

    Args:
        meeting_id: Meeting ID (CallId)
        last: How many of the latest frames (default 3, 1 to 20). Ignored when a
            time range is given.
        from_seconds: Start of a time range, seconds from the start of the meeting
        to_seconds: End of a time range, seconds from the start of the meeting
        user_id: User ID for access control
        is_admin: Whether the caller is an Admin

    Returns:
        Dict with meetingId, count, totalFrames, frames, textRead (how many of the
        frames' texts had been read from the picture) and, when there is
        something to say, a note.
    """
    meeting_id, wanted_last, start, end = check_arguments(
        meeting_id, last, from_seconds, to_seconds
    )
    table = open_meeting(meeting_id, user_id, is_admin)

    try:
        frames, unread_after = read_frames(table, meeting_id)
    except Exception as e:
        logger.error("Could not read the frames of meeting %r: %s", meeting_id, type(e).__name__)
        raise ValueError(FAILURE_MESSAGE) from None

    spans = shared_spans(frames)
    selected = select_frames(frames, last=wanted_last, start=start, end=end)
    notes = selection_notes(spans, selected, start, end)
    if unread_after is not None:
        notes.append(
            f"The meeting has more than {MAX_PAGES * PAGE_SIZE:,} key frames: frames after "
            f"{format_time(unread_after)} were not read."
        )

    texts = (
        read_texts(table, meeting_id, [frame["SK"] for frame, _ in selected]) if selected else {}
    )
    stored = [texts.get(frame["SK"], {}) for frame, _ in selected]
    titles = [title_of(item) for item in stored]
    frame_ids = [frame_id_of(frame) for frame, _ in selected]
    fixed_units = sum(units_of(title) for title in titles if title)
    fixed_units += sum(units_of(frame_id) for frame_id in frame_ids)
    fitted = fit_texts(
        [s["SlideText"] if isinstance(s.get("SlideText"), str) else None for s in stored],
        TEXT_BUDGET - fixed_units,
    )
    if any(cut for _, cut in fitted):
        notes.append(
            "Some frames' text was shortened to keep the answer small; those frames have "
            "textTruncated=true."
        )

    answer_frames = [
        answer_frame(frame, until, frame_id, title, text, cut)
        for (frame, until), frame_id, title, (text, cut) in zip(selected, frame_ids, titles, fitted)
    ]
    result: Dict[str, Any] = {
        "meetingId": meeting_id,
        "count": len(answer_frames),
        "totalFrames": len(spans),
        "frames": answer_frames,
        "textRead": sum(1 for frame in answer_frames if frame["text"] is not None),
    }
    if notes:
        result["note"] = " ".join(notes)
    logger.info("Read %d of %d frames of meeting %r", len(answer_frames), len(spans), meeting_id)
    return result
