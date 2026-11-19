# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""
Tool: list_active_meetings
List the meetings that are running now, so a client knows which meetingId to ask about.
"""

import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import boto3

from tools.access_helper import can_read_meeting
from tools.list_meetings import BATCH_GET_MAX, UNREADABLE_NOTE, failure_class, read_meeting_rows
from tools.url_helper import get_meeting_url, get_virtual_participant_url

logger = logging.getLogger()

DEFAULT_LIMIT = 10
MAX_LIMIT = 50
# Meetings created in this long before the call are looked at, newest first.
LOOKBACK = timedelta(hours=24)
# A meeting is running while it has one of these statuses.
OPEN_STATUSES = frozenset({"STARTED", "TRANSCRIBING"})
# The index is read in pages of PAGE_SIZE rows, at most MAX_PAGES pages.
PAGE_SIZE = 100
MAX_PAGES = 10
# Upload jobs are looked up this many at a time (the most one BatchGetItem takes).
LOOKUP_BATCH = 100
LOOKUP_ATTEMPTS = 4

TRUNCATED_NOTE = (
    "Only the most recent meetings of the last 24 hours were checked, so a running "
    "meeting that started earlier in that window may not be listed."
)
LIMIT_ERROR = "limit must be an integer"
# What a numeric string may look like: ASCII digits, a sign, a fraction, an exponent.
NUMBER_TEXT = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", re.ASCII)
FAILURE_MESSAGE = "Failed to list active meetings"

# The attributes of a meeting row that access, status and the answer need, and
# the key to match each row to its meeting. Only three fields of the consent
# record are read: the record can also hold evidence of up to 150 KB.
MEETING_PROJECTION: Dict[str, Any] = {
    "ProjectionExpression": (
        "#pk, #id, #owner, #agent, #shared, #status, #topic, #created, #updated, "
        "#consent.#mode, #consent.#status, #consent.#capture"
    ),
    "ExpressionAttributeNames": {
        "#pk": "PK",
        "#id": "CallId",
        "#owner": "Owner",
        "#agent": "AgentId",
        "#shared": "SharedWith",
        "#status": "Status",
        "#topic": "MeetingTopic",
        "#created": "CreatedAt",
        "#updated": "UpdatedAt",
        "#consent": "Consent",
        "#mode": "Mode",
        "#capture": "CaptureAllowed",
    },
}


def parse_limit(value: Any) -> int:
    """The number of meetings to return: 10 when absent, otherwise 1 to 50.

    A whole number is taken as it is, a numeric string or a float with no
    fraction is read as that number, and a number outside 1 to 50 is moved to the
    nearest end. A boolean, anything that is not a number, a fraction, and a
    value that is not finite (``1e999``) are refused.
    """
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool):
        raise ValueError(LIMIT_ERROR)
    number: Any = value
    if isinstance(value, str):
        text = value.strip()
        if not NUMBER_TEXT.fullmatch(text):
            raise ValueError(LIMIT_ERROR)
        try:
            number = int(text)
        except ValueError:
            number = float(text)
    if isinstance(number, float):
        if not math.isfinite(number) or number != math.floor(number):
            raise ValueError(LIMIT_ERROR)
        number = int(number)
    if not isinstance(number, int):
        raise ValueError(LIMIT_ERROR)
    return max(1, min(MAX_LIMIT, number))


def execute(
    limit: Any = None,
    user_id: Optional[str] = None,
    is_admin: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    List the meetings that are running now and that the caller may read.

    Args:
        limit: Maximum number of meetings to return (default 10, 1 to 50)
        user_id: User ID for access control
        is_admin: Whether the caller is an Admin
        now: The time to count the last 24 hours back from (tests only)

    Returns:
        Dict with the meetings, their count, and a note when the list may be
        incomplete.
    """
    wanted = parse_limit(limit)
    if not user_id and not is_admin:
        return {"meetings": [], "count": 0}

    try:
        table = boto3.resource("dynamodb").Table(os.environ.get("CALLS_TABLE"))
        meetings, unreadable, truncated = find_active_meetings(
            table, wanted, user_id, is_admin, now or datetime.now(timezone.utc)
        )
    except Exception as e:
        # Only the class of the cause is logged: its text can name resources.
        logger.error("Error listing active meetings (%s)", failure_class(e))
        raise ValueError(FAILURE_MESSAGE) from None

    result: Dict[str, Any] = {"meetings": [format_meeting(m) for m in meetings]}
    result["count"] = len(meetings)
    notes = []
    if truncated:
        result["truncated"] = True
        notes.append(TRUNCATED_NOTE)
    if unreadable:
        notes.append(UNREADABLE_NOTE)
    if notes:
        result["note"] = " ".join(notes)

    logger.info("Listed %d active meetings for user %s", len(meetings), user_id)
    return result


def sort_key_bounds(now: datetime) -> Tuple[str, str]:
    """The sort keys of the index that hold the last 24 hours.

    A sort key is ``ts#<ISO 8601 time>#id#<CallId>``. The upper bound ends in
    "~" (0x7E), which sorts after every character that can follow the time.
    """
    return f"ts#{iso_time(now - LOOKBACK)}", f"ts#{iso_time(now)}~"


def iso_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def find_active_meetings(
    table, limit: int, user_id: Optional[str], is_admin: bool, now: datetime
) -> Tuple[List[Dict[str, Any]], int, bool]:
    """Walk the TypeDateIndex newest first and collect up to ``limit`` running meetings.

    Returns the meeting rows, how many meetings could not be read, and whether
    the page cap ended the walk with index rows still left.
    """
    from boto3.dynamodb.conditions import Key

    sk_lo, sk_hi = sort_key_bounds(now)
    query_kwargs: Dict[str, Any] = {
        "IndexName": "TypeDateIndex",
        "KeyConditionExpression": Key("ItemType").eq("call") & Key("SK").between(sk_lo, sk_hi),
        "ScanIndexForward": False,  # newest first
        "Limit": PAGE_SIZE,
    }

    found: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    # Rows that could not be read, as positions in the walk, and the position
    # of the last meeting listed.
    unread: List[int] = []
    last_listed = -1
    pages = 0
    last_key = None
    while len(found) < limit and pages < MAX_PAGES:
        if last_key:
            query_kwargs["ExclusiveStartKey"] = last_key
        response = table.query(**query_kwargs)
        pages += 1
        last_key = response.get("LastEvaluatedKey")

        candidates = []
        for entry in response.get("Items", []):
            call_id = entry.get("CallId")
            if not call_id or call_id in seen:
                continue
            seen[call_id] = len(seen)
            candidates.append(call_id)

        # The index has no AgentId and no Status, so every candidate's meeting
        # row is read, projected to the fields above: one request per page.
        for position in range(0, len(candidates), BATCH_GET_MAX):
            if len(found) >= limit:
                break
            batch = candidates[position : position + BATCH_GET_MAX]
            rows, failed = read_meeting_rows(table, batch, MEETING_PROJECTION)
            unread.extend(seen[call_id] for call_id in failed)
            running = [
                (call_id, rows[call_id])
                for call_id in batch
                if call_id in rows
                and is_open(rows[call_id].get("Status"))
                and can_read_meeting(rows[call_id], user_id, is_admin)
            ]
            uploads = upload_jobs(table, [call_id for call_id, _ in running])
            for call_id, row in running:
                if call_id in uploads:
                    continue
                found.append({**row, "CallId": call_id})
                last_listed = seen[call_id]
                if len(found) >= limit:
                    break
        if not last_key:
            break

    truncated = bool(last_key) and len(found) < limit and rows_left(table, query_kwargs, last_key)
    # A row that could not be read counts when it could have been listed:
    # always while the list is short, otherwise only when it is newer than the
    # last meeting listed.
    full = len(found) >= limit
    unreadable = sum(1 for at in unread if not full or at < last_listed)
    return found, unreadable, truncated


def is_open(status: Any) -> bool:
    """Whether a stored Status is one of a running meeting.

    A Status that is not a string (a corrupt row) is not running and must not
    fail the whole call.
    """
    return isinstance(status, str) and status in OPEN_STATUSES


def rows_left(table, query_kwargs: Dict[str, Any], last_key: Dict[str, Any]) -> bool:
    """Whether the index has a row after ``last_key``.

    DynamoDB returns a key after a page that ends exactly at the end of the
    range, so the key alone does not say. When the check itself fails the answer
    is yes: the list may be incomplete.
    """
    try:
        probe = dict(query_kwargs, Limit=1, ExclusiveStartKey=last_key)
        return bool(table.query(**probe).get("Items"))
    except Exception as e:
        logger.warning("Could not check for more index rows (%s)", failure_class(e))
        return True


def upload_jobs(table, call_ids: List[str]) -> Set[str]:
    """The ids among ``call_ids`` that have an upload-job row.

    An uploaded recording has the status TRANSCRIBING while it is processed, but
    it is not a meeting that is being held. A lookup that cannot be answered
    counts as no upload job, so the meeting stays listed: the list may then hold
    an upload, but it never misses a meeting that is running.
    """
    uploads: Set[str] = set()
    for start in range(0, len(call_ids), LOOKUP_BATCH):
        chunk = call_ids[start : start + LOOKUP_BATCH]
        left = read_upload_batch(table, chunk, uploads)
        for call_id in left:
            try:
                if table.get_item(Key=upload_key(call_id), ProjectionExpression="PK").get("Item"):
                    uploads.add(call_id)
            except Exception as e:
                logger.warning(
                    "Upload lookup for meeting %r failed (%s)", call_id, failure_class(e)
                )
    return uploads


def upload_key(call_id: str) -> Dict[str, str]:
    return {"PK": f"uj#{call_id}", "SK": f"uj#{call_id}"}


def read_upload_batch(table, call_ids: List[str], uploads: Set[str]) -> List[str]:
    """Look the upload-job rows of up to 100 ids up in one batch, retrying keys left
    unprocessed. Adds the ids that have a row to ``uploads`` and returns the ids
    that were not answered."""
    keys = [upload_key(call_id) for call_id in call_ids]
    for attempt in range(LOOKUP_ATTEMPTS):
        try:
            response = table.meta.client.batch_get_item(
                RequestItems={table.name: {"Keys": keys, "ProjectionExpression": "PK"}}
            )
        except Exception as e:
            logger.warning(
                "Batch upload lookup of %d meetings failed (%s)", len(keys), failure_class(e)
            )
            return [key["PK"][3:] for key in keys]
        for item in response.get("Responses", {}).get(table.name, []):
            pk = item.get("PK")
            if isinstance(pk, str) and pk.startswith("uj#"):
                uploads.add(pk[3:])
        keys = (response.get("UnprocessedKeys") or {}).get(table.name, {}).get("Keys") or []
        if not keys:
            return []
        if attempt + 1 < LOOKUP_ATTEMPTS:
            time.sleep(0.05 * 2**attempt)
    return [key["PK"][3:] for key in keys]


def format_meeting(meeting: Dict[str, Any]) -> Dict[str, Any]:
    """The fields of one running meeting. No transcript text."""
    meeting_id = meeting.get("CallId", "")
    consent = meeting.get("Consent")
    return {
        "meetingId": meeting_id,
        "meetingName": meeting.get("MeetingTopic", ""),
        "status": meeting.get("Status", "UNKNOWN"),
        "startedAt": meeting.get("CreatedAt", ""),
        "lastActivityAt": meeting.get("UpdatedAt") or meeting.get("CreatedAt", ""),
        "owner": meeting.get("Owner", meeting.get("AgentId", "")),
        "consent": (
            {
                "mode": consent.get("Mode"),
                "status": consent.get("Status"),
                "captureAllowed": consent.get("CaptureAllowed"),
            }
            if isinstance(consent, dict) and consent
            else None
        ),
        "meetingUrl": get_meeting_url(meeting_id),
        "virtualParticipantUrl": get_virtual_participant_url(meeting_id),
    }
