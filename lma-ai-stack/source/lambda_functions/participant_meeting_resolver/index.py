#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""The read-only view a Participant has of the meetings shared with them.

Four AppSync queries, each admitted to the Participant role only:

- ``listParticipantMeetings``: the meetings shared with the caller, newest first;
- ``getParticipantMeeting``: one of them, as a ``ParticipantMeeting``;
- ``getParticipantMeetingTranscript``: its transcript, a page at a time;
- ``getParticipantMeetingRecordings``: signed links to its recordings.

The answers are built from an allowlist (``participant_view.py``): a field the
meeting record gains later reaches a Participant only when it is added there
and to the ``ParticipantMeeting`` type. A meeting is answered only when its
SharedWith names the caller, compared as whole entries as getCall compares
them; being its owner is not enough, since a Participant sees shared meetings
only.

Recordings are played through links this function signs, not through the
identity pool: the pool's Participant role has no permissions, because S3 has
no grant that follows each meeting's sharing list.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import unquote, urlparse

import boto3
import participant_view
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config as BotoCoreConfig

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})
DYNAMODB = boto3.resource("dynamodb", config=CLIENT_CONFIG)
S3_CLIENT = boto3.client(
    "s3",
    config=BotoCoreConfig(
        retries={"mode": "adaptive", "max_attempts": 3}, signature_version="s3v4"
    ),
)

EVENT_SOURCING_TABLE_NAME = os.environ.get("EVENT_SOURCING_TABLE_NAME", "")
RECORDINGS_BUCKET_NAME = os.environ.get("RECORDINGS_BUCKET_NAME", "")
AUDIO_RECORDINGS_PREFIX = os.environ.get("AUDIO_RECORDINGS_PREFIX", "lma-audio-recordings/")
VIDEO_RECORDINGS_PREFIX = os.environ.get("VIDEO_RECORDINGS_PREFIX", "lma-video-recordings/")

TYPE_DATE_INDEX = "TypeDateIndex"
ITEM_TYPE_CALL = "call"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
#: GSI pages read per request at most, so a caller with few shares cannot make
#: one request read the whole index.
MAX_LIST_PAGES = 10
#: How long a recording link stays valid.
RECORDING_LINK_SECONDS = 3600

PARTICIPANT = "Participant"


class Refused(Exception):
    """The caller may not read what they asked for."""


# BEGIN shared_with_entries -- one text in every Lambda that reads SharedWith;
# lma-ai-stack/test/test_shared_with_access.py holds the copies to it.
# The entries of a SharedWith value, read as the AppSync resolvers read them
# (the "SharedWith entries" VTL block and sharedWithEntries in the JS
# resolvers): a list, tuple or set holds one entry per string element, and a
# string -- comma-separated or "[a, b]" -- is split at every "[", "]" and ",".
# Those three characters only ever separate entries, so a list element that
# contains one is not an entry. Entries are trimmed and empty ones dropped;
# membership is then a comparison of whole entries.
def shared_with_entries(value: object) -> list:
    """The trimmed, non-empty entries of a SharedWith value."""
    if isinstance(value, str):
        parts = value.replace("[", ",").replace("]", ",").split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        parts = [
            part
            for part in value
            if isinstance(part, str) and not any(separator in part for separator in "[],")
        ]
    else:
        return []
    return [part.strip() for part in parts if part.strip()]


# END shared_with_entries


def _plain(value: Any) -> Any:
    """DynamoDB's Decimals as int or float, recursively, for the GraphQL answer."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _encode_token(key: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not key:
        return None
    return base64.urlsafe_b64encode(json.dumps(_plain(dict(key))).encode()).decode()


def _decode_token(token: Optional[str], expected: Mapping[str, str]) -> Optional[dict]:
    """A token this function issued, or None. Its key must name the partition asked for."""
    if not token:
        return None
    try:
        key = json.loads(base64.urlsafe_b64decode(token.encode()).decode())
    except (ValueError, TypeError):
        raise Refused("invalid nextToken") from None
    if not isinstance(key, dict) or any(key.get(name) != value for name, value in expected.items()):
        raise Refused("invalid nextToken")
    return key


def caller_of(event: Mapping[str, Any]) -> str:
    """The signed-in Participant's username; refused for anyone else.

    AppSync admits only the Participant role to these fields; this holds the
    function to the same rule should a resolver ever point another field at it.
    """
    identity = event.get("identity") or {}
    groups = identity.get("groups") or []
    username = identity.get("username") or ""
    if not isinstance(username, str) or not username or PARTICIPANT not in groups:
        raise Refused("only a Participant may read this view")
    if "Admin" in groups:
        raise Refused("only a Participant may read this view")
    return username


def shared_with_caller(meeting: Optional[Mapping[str, Any]], username: str) -> bool:
    return bool(meeting) and username in shared_with_entries(meeting.get("SharedWith"))


def _table():
    return DYNAMODB.Table(EVENT_SOURCING_TABLE_NAME)


def read_meeting(call_id: Any) -> Optional[dict]:
    if not isinstance(call_id, str) or not call_id:
        return None
    key = f"c#{call_id}"
    return _table().get_item(Key={"PK": key, "SK": key}, ConsistentRead=True).get("Item")


def shared_meeting(event: Mapping[str, Any]) -> dict:
    """The meeting the request names, when it is shared with the caller."""
    username = caller_of(event)
    call_id = (event.get("arguments") or {}).get("CallId")
    meeting = read_meeting(call_id)
    if not shared_with_caller(meeting, username):
        # The same answer for a meeting that does not exist, so the refusal
        # says nothing about which meetings there are.
        raise Refused(f"{username} is not authorized to read this meeting")
    return meeting


# ── the operations ─────────────────────────────────────────────────────────


def list_participant_meetings(event: Mapping[str, Any]) -> dict:
    username = caller_of(event)
    args = event.get("arguments") or {}
    limit = max(1, min(int(args.get("limit") or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    condition = Key("ItemType").eq(ITEM_TYPE_CALL)
    start, end = args.get("startDateTime"), args.get("endDateTime")
    if start and end:
        condition = condition & Key("SK").between(f"ts#{start}", f"ts#{end}#~")
    elif start:
        condition = condition & Key("SK").gte(f"ts#{start}")
    elif end:
        condition = condition & Key("SK").lte(f"ts#{end}#~")
    last_key = _decode_token(args.get("nextToken"), {"ItemType": ITEM_TYPE_CALL})
    table = _table()
    found: List[dict] = []
    pages = 0
    while len(found) < limit and pages < MAX_LIST_PAGES:
        query = {
            "IndexName": TYPE_DATE_INDEX,
            "KeyConditionExpression": condition,
            # The list row's SharedWith, which shareCall writes with the
            # meeting's; a list holding the caller, or an older string form,
            # which the entry check below reads whole.
            "FilterExpression": Attr("SharedWith").contains(username),
            "ScanIndexForward": False,
        }
        if last_key:
            query["ExclusiveStartKey"] = last_key
        response = table.query(**query)
        pages += 1
        last_key = response.get("LastEvaluatedKey")
        rows = response.get("Items", [])
        for position, row in enumerate(rows):
            if username not in shared_with_entries(row.get("SharedWith")) or not row.get("CallId"):
                continue
            found.append(row)
            if len(found) >= limit:
                # Resume after this row rather than after the page, which would
                # skip the page's later rows; the index is keyed on (ItemType,
                # SK) and the table on (PK, SK), so the key needs all three.
                if position < len(rows) - 1:
                    last_key = {name: row[name] for name in ("ItemType", "PK", "SK") if name in row}
                break
        if not last_key:
            break
    meetings = []
    for row in found:
        meeting = read_meeting(row["CallId"])
        if shared_with_caller(meeting, username):
            meetings.append(participant_view.meeting_summary(meeting))
    return {"Meetings": meetings, "nextToken": _encode_token(last_key)}


def get_participant_meeting(event: Mapping[str, Any]) -> dict:
    return participant_view.participant_meeting(shared_meeting(event))


def get_participant_meeting_transcript(event: Mapping[str, Any]) -> dict:
    meeting = shared_meeting(event)
    call_id = meeting["CallId"]
    args = event.get("arguments") or {}
    pk = f"trs#{call_id}"
    query = {
        "KeyConditionExpression": Key("PK").eq(pk),
        "FilterExpression": Attr("IsPartial").eq(False)
        & Attr("Channel").is_in(list(participant_view.TRANSCRIPT_CHANNELS)),
    }
    start = _decode_token(args.get("nextToken"), {"PK": pk})
    if start:
        query["ExclusiveStartKey"] = start
    response = _table().query(**query)
    return {
        "Segments": [
            participant_view.transcript_segment(item) for item in response.get("Items", [])
        ],
        "nextToken": _encode_token(response.get("LastEvaluatedKey")),
    }


def _recording_key(url: Any, prefix: str) -> Optional[str]:
    """The key a stored recording link names in the recordings bucket, directly under ``prefix``."""
    if not isinstance(url, str) or not url or not RECORDINGS_BUCKET_NAME:
        return None
    parsed = urlparse(url)
    if not (parsed.hostname or "").startswith(f"{RECORDINGS_BUCKET_NAME}.s3."):
        return None
    key = unquote(parsed.path.lstrip("/"))
    name = key[len(prefix) :]
    if not key.startswith(prefix) or not name or "/" in name:
        return None
    return key


def get_participant_meeting_recordings(event: Mapping[str, Any]) -> List[dict]:
    meeting = shared_meeting(event)
    if meeting.get("RecordingsDeletedAt"):
        return []
    recordings = []
    prefixes = {"AUDIO": AUDIO_RECORDINGS_PREFIX, "VIDEO": VIDEO_RECORDINGS_PREFIX}
    for link in participant_view.recording_links(meeting):
        kind = link["kind"]
        key = _recording_key(link["url"], prefixes[kind])
        if key is None:
            continue
        url = S3_CLIENT.generate_presigned_url(
            "get_object",
            Params={"Bucket": RECORDINGS_BUCKET_NAME, "Key": key},
            ExpiresIn=RECORDING_LINK_SECONDS,
        )
        recordings.append(
            {
                "Kind": kind,
                "SessionNumber": link["session"],
                "Url": url,
                "ExpiresInSeconds": RECORDING_LINK_SECONDS,
            }
        )
    return recordings


OPERATIONS = {
    "listParticipantMeetings": list_participant_meetings,
    "getParticipantMeeting": get_participant_meeting,
    "getParticipantMeetingTranscript": get_participant_meeting_transcript,
    "getParticipantMeetingRecordings": get_participant_meeting_recordings,
}


def handler(event: Dict[str, Any], context: Any) -> Any:  # pylint: disable=unused-argument
    """AppSync entry point."""
    field = (event.get("info") or {}).get("fieldName", "")
    operation = OPERATIONS.get(field)
    if operation is None:
        raise ValueError(f"Unknown field: {field}")
    try:
        return _plain(operation(event))
    except Refused as refusal:
        LOGGER.info("Refused %s: %s", field, refusal)
        # AppSync's own word for a refusal, which the UI and the role probe read.
        raise PermissionError(f"Unauthorized: {refusal}") from None
