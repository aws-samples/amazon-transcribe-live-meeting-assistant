#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Deletes a meeting's transcript, and its recordings, once it is summarized, when the
Retention section says to.

The Retention section's "Delete the transcript after the summary" setting
(``lma_config.deletion_delay_minutes``) is off, or a delay: immediately, or 15,
30 or 60 minutes after the summary is written. "Delete the recordings after the
summary" (``lma_config.recording_deletion_delay_minutes``) has the same choices
and is independent of it: each of the two is scheduled, recorded on the meeting
and run on its own, by the same rules (see "The recordings" below). This
function is invoked with the meeting's ``CallId`` and a ``Trigger``:

``meeting-ended``
    By the call event processor, on the meeting's first END. Schedules the
    deletion for the end of the meeting plus the delay, plus
    ``SUMMARY_WAIT_MINUTES`` while end-of-meeting summaries are on: the time
    by which the meeting is deleted even if its summary never arrives.
``summary-written``
    By the call event processor, once the summary is stored. Moves the
    deletion forward to the summary plus the delay, or deletes now. Not while
    the meeting is live (``STARTED``): an earlier session's summary that lands
    after a rejoined session has started leaves the deletion to that session's
    END and summary. ``meeting-ended`` has no such check, because the END
    invokes it before writing ``ENDED``.
``scheduled``
    By the one-time EventBridge Scheduler schedule either of the above made.
    Deletes: the transcript, or with ``"Kind": "recordings"`` the recordings.
``meeting-reopened``
    By the call event processor, with the ``SessionNumber``, when a rejoined
    session of the meeting has started and made it live again
    (docs/virtual-participant.md#rejoin). Cancels the pending deletion while the
    meeting is still live in that session and the setting is on, because that
    session's END schedules one afresh; see cancel_deletion.
``recording-added``
    By the call event processor, each time it stores a recording link on the
    meeting. Recordings only: once a meeting's recordings have been deleted,
    one stored after that -- a late upload, or a later session's -- is deleted
    at once, whatever the setting says now.

``meeting-ended``, ``summary-written`` and ``meeting-reopened`` act on both
settings, each independently of the other.

A schedule is only ever moved earlier, so a redelivered END cannot postpone a
deletion. After a deletion made on the spot, one more is scheduled
``SWEEP_MINUTES`` later, which removes anything written for the meeting after
it: a late assistant answer, or a segment an engine corrected after the end.

What is deleted, for the meeting:

* every item under its transcript partition (``trs#<CallId>``) -- each turn of
  the transcript, partial or final, and each meeting assistant answer;
* ``IssuesDetected`` on the meeting record, which quotes the transcript. Nothing
  writes it any more, but a meeting stored by an earlier version may carry it;
* every version of the transcript files in the recordings bucket: the text
  file the summary step writes for the transcript knowledge base, its metadata
  file, and an uploaded meeting's Amazon Transcribe output;
* the meeting's chat attachments, which belong to the chat lines of the
  transcript: their items (``att#<CallId>``) and every version of every file in
  the meeting's attachments folder (``lma-chat-attachments/<CallId>/``);
* the transcript knowledge base's entries for those files;
* the meeting's notifications of the categories that may quote the transcript
  (``guidance``; ``lma_notifications.TRANSCRIPT_BOUND_CATEGORIES``), with
  their dedup pointers, and every row under ``gd#<CallId>``: the live guidance
  analyzer's state (its summary of the meeting so far and what it published),
  and guidance rows a development build kept there. The meeting's other
  notifications are kept.

The meeting record is then stamped with when its transcript was deleted, and
how many segments and files were removed, which the meeting page shows. The
summary, the meeting record, the recordings and the consent record are kept.

The recordings, when their own setting says so (delete_recordings): every
version of every audio and video recording file the meeting record links to,
in every capture session, with the transcriber's raw audio beside each WAV;
and the Virtual
Participant's video segments (``lma-video-chunks/<CallId>/``). The record's
links are removed and it is stamped with when, and how many files went. Key
frames, chat attachments, consent evidence, the summary and the transcript are
kept.

Every step is idempotent, so a failure raises and the invocation is retried:
by Lambda for an asynchronous invoke, by EventBridge Scheduler for a schedule,
each with a dead-letter queue behind it.
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import unquote, urlparse

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config as BotoCoreConfig
from botocore.exceptions import BotoCoreError, ClientError

# pylint: disable=import-error
from lma_config import (  # configuration layer
    SUMMARY_WAIT_MINUTES,
    TABLE_NAME_ENV_VAR,
    ConfigurationStore,
    deletion_delay_minutes,
    recording_deletion_delay_minutes,
)
from lma_consent import without_platform_ids  # configuration layer
from lma_meeting_preparation.rules import strip_quotes  # configuration layer
from lma_notifications import TRANSCRIPT_BOUND_CATEGORIES  # configuration layer

# pylint: enable=import-error

# Stdlib logging rather than Powertools: this function carries only the
# configuration layer, which has no third-party dependencies.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})

EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
RECORDINGS_BUCKET = os.environ.get("RECORDINGS_BUCKET", "")
TRANSCRIPT_PREFIX = os.environ.get("TRANSCRIPT_PREFIX", "lma-transcripts/")
ATTACHMENTS_PREFIX = os.environ.get("ATTACHMENTS_PREFIX", "lma-chat-attachments/")
AUDIO_PREFIX = os.environ.get("AUDIO_PREFIX", "lma-audio-recordings/")
VIDEO_PREFIX = os.environ.get("VIDEO_PREFIX", "lma-video-recordings/")
VIDEO_CHUNKS_PREFIX = os.environ.get("VIDEO_CHUNKS_PREFIX", "lma-video-chunks/")
TRANSCRIPT_KB_ID = os.environ.get("TRANSCRIPT_KB_ID", "")
SCHEDULE_GROUP_NAME = os.environ.get("SCHEDULE_GROUP_NAME", "")
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
DEAD_LETTER_QUEUE_ARN = os.environ.get("DEAD_LETTER_QUEUE_ARN", "")
IS_TRANSCRIPT_SUMMARY_ENABLED = (
    os.environ.get("IS_TRANSCRIPT_SUMMARY_ENABLED", "true").lower() == "true"
)

#: Minutes after a deletion made on the spot at which the meeting is swept once more.
SWEEP_MINUTES = 10

MEETING_ENDED = "meeting-ended"
SUMMARY_WRITTEN = "summary-written"
SCHEDULED = "scheduled"
MEETING_REOPENED = "meeting-reopened"
RECORDING_ADDED = "recording-added"
TRIGGERS = (MEETING_ENDED, SUMMARY_WRITTEN, SCHEDULED, MEETING_REOPENED, RECORDING_ADDED)

#: What a deletion removes: the transcript, or the recordings. A scheduled
#: event names its ``Kind``; one that names none is the transcript's, as every
#: schedule made before the recordings could be deleted is.
TRANSCRIPT = "transcript"
RECORDINGS = "recordings"


class _Kind:
    """Where one kind of deletion keeps its state, and how its schedule is named."""

    def __init__(self, name: str, scheduled_field: str, deleted_field: str, what: str) -> None:
        self.name = name
        self.scheduled_field = scheduled_field
        self.deleted_field = deleted_field
        self.what = what


KINDS = {
    TRANSCRIPT: _Kind(
        TRANSCRIPT, "TranscriptDeletionScheduledFor", "TranscriptDeletedAt", "transcript"
    ),
    RECORDINGS: _Kind(
        RECORDINGS, "RecordingDeletionScheduledFor", "RecordingsDeletedAt", "recordings"
    ),
}

_clients: Dict[str, Any] = {}


def _client(name: str) -> Any:
    if name not in _clients:
        _clients[name] = boto3.client(name, config=CLIENT_CONFIG)
    return _clients[name]


def _table() -> Any:
    if "table" not in _clients:
        _clients["table"] = boto3.resource("dynamodb", config=CLIENT_CONFIG).Table(
            EVENT_SOURCING_TABLE
        )
    return _clients["table"]


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


_STORE: Optional[Any] = None


def _fresh_store() -> Optional[Any]:
    """The configuration store with its cache dropped, or None without the table named."""
    global _STORE  # pylint: disable=global-statement -- one store per process
    if _STORE is None and os.environ.get(TABLE_NAME_ENV_VAR):
        _STORE = ConfigurationStore()
    if _STORE is not None:
        _STORE.invalidate()
    return _STORE


def current_delay() -> Optional[int]:
    """The setting as saved now, not as cached: a save applies to the next meeting to end.

    One read of the configuration table, twice a meeting. Without the table
    named, off (None).
    """
    return deletion_delay_minutes(store=_fresh_store())


def current_recording_delay() -> Optional[int]:
    """The recordings' setting as saved now, read as current_delay reads the transcript's."""
    return recording_deletion_delay_minutes(store=_fresh_store())


def delay_of(kind: str) -> Optional[int]:
    return current_recording_delay() if kind == RECORDINGS else current_delay()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def posixify_filename(filename: str) -> str:
    """The file name the summary step gives a meeting's files (bedrock_summary_lambda)."""
    posix_filename = re.sub(r"[^a-zA-Z0-9_.]", "_", filename)
    posix_filename = re.sub(r"^_+", "", posix_filename)
    return re.sub(r"_+$", "", posix_filename)


def meeting_key(call_id: str) -> Dict[str, str]:
    return {"PK": f"c#{call_id}", "SK": f"c#{call_id}"}


def transcript_file_keys(call_id: str) -> List[str]:
    """Every object in the recordings bucket that holds the meeting's transcript."""
    name = posixify_filename(call_id)
    return [
        f"{TRANSCRIPT_PREFIX}{name}-TRANSCRIPT.txt",
        f"{TRANSCRIPT_PREFIX}{name}-TRANSCRIPT.txt.metadata.json",
        # Amazon Transcribe's output for an uploaded meeting, named by the
        # upload processor after the meeting id as it is.
        f"{TRANSCRIPT_PREFIX}{call_id}.transcribe.json",
    ]


def schedule_name(call_id: str, kind: str = TRANSCRIPT) -> str:
    """A schedule name for the meeting: at most 64 characters, from a fixed alphabet.

    One per kind, so the transcript's and the recordings' deletions are
    scheduled, moved and cancelled independently.
    """
    return f"{KINDS[kind].what}-" + hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:48]


# ── the meeting record ──────────────────────────────────────────────────────


def read_meeting(call_id: str) -> Optional[Dict[str, Any]]:
    response = _table().get_item(
        Key=meeting_key(call_id),
        ProjectionExpression=(
            "PK, #status, TranscriptDeletionScheduledFor, TranscriptDeletedAt, "
            "RecordingDeletionScheduledFor, RecordingsDeletedAt"
        ),
        ExpressionAttributeNames={"#status": "Status"},
        ConsistentRead=True,
    )
    return response.get("Item")


def record_schedule(call_id: str, moment: datetime, kind: str = TRANSCRIPT) -> None:
    try:
        _table().update_item(
            Key=meeting_key(call_id),
            UpdateExpression=f"SET {KINDS[kind].scheduled_field} = :at",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={":at": _iso(moment)},
        )
    except ClientError as error:
        if _error_code(error) != "ConditionalCheckFailedException":
            raise
        LOGGER.info("meeting %s has no record; not recording its deletion time", call_id)


def record_deletion(call_id: str, trigger: str, segments: int, files: int) -> None:
    """Stamp the meeting with its deletion. The first time and trigger are kept; counts add up."""
    try:
        _table().update_item(
            Key=meeting_key(call_id),
            UpdateExpression=(
                "SET TranscriptDeletedAt = if_not_exists(TranscriptDeletedAt, :now), "
                "TranscriptDeletionTrigger = if_not_exists(TranscriptDeletionTrigger, :trigger), "
                "TranscriptDeletionLastRunAt = :now "
                "ADD TranscriptSegmentsDeleted :segments, TranscriptFilesDeleted :files "
                "REMOVE IssuesDetected, TranscriptDeletionScheduledFor"
            ),
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues={
                ":now": _iso(_now()),
                ":trigger": trigger,
                ":segments": segments,
                ":files": files,
            },
        )
    except ClientError as error:
        if _error_code(error) != "ConditionalCheckFailedException":
            raise
        LOGGER.info("meeting %s has no record; its transcript was deleted all the same", call_id)


#: Attempts at removing the checklist's quotes while the checklist changes.
QUOTE_STRIP_ATTEMPTS = 4


class ChecklistQuotesNotRemoved(RuntimeError):
    """The checklist kept changing while its quotes were being removed."""


def strip_checklist_quotes(call_id: str) -> int:
    """Remove every evidence quote from the meeting's checklist; the number of items changed.

    A quote is transcript content, so it goes with the transcript. Each
    evidence entry keeps its segment id and its time. The write is conditional
    on the checklist's version as read (the meeting preparation resolver's
    guard), and bumps it, so a checklist write racing this one is read again
    rather than restoring a quote; and once TranscriptDeletedAt is set, which
    record_deletion does first, the resolver stores no new quote.
    """
    for _attempt in range(QUOTE_STRIP_ATTEMPTS):
        item = (
            _table()
            .get_item(
                Key=meeting_key(call_id),
                ProjectionExpression="Checklist, ChecklistVersion",
                ConsistentRead=True,
            )
            .get("Item")
        )
        checklist = (item or {}).get("Checklist")
        if not checklist:
            return 0
        stripped, changed = strip_quotes(checklist)
        if not changed:
            return 0
        version = int((item or {}).get("ChecklistVersion") or 0)
        if version:
            condition = "ChecklistVersion = :version"
            values: Dict[str, Any] = {":version": version}
        else:
            condition = "attribute_not_exists(ChecklistVersion)"
            values = {}
        values.update({":checklist": stripped, ":next": version + 1})
        try:
            _table().update_item(
                Key=meeting_key(call_id),
                UpdateExpression="SET Checklist = :checklist, ChecklistVersion = :next",
                ConditionExpression=f"attribute_exists(PK) AND {condition}",
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if _error_code(error) != "ConditionalCheckFailedException":
                raise
            LOGGER.info(
                "the checklist of %s changed while removing its quotes; reading it again", call_id
            )
            continue
        return sum(
            1
            for before, after in zip(checklist, stripped)
            if (before.get("Evidence") or []) != (after.get("Evidence") or [])
        )
    # Raised, so the invocation fails and is retried (the schedule's retry
    # policy, or the caller's): the deletion is safe to run again, and a
    # transcript recorded as deleted must not keep its quotes.
    raise ChecklistQuotesNotRemoved(
        f"the checklist of {call_id} kept changing while removing its quotes"
    )


class ConsentPlatformIdsNotRemoved(RuntimeError):
    """The consent record kept changing while its platform participant ids were removed."""


def strip_consent_platform_ids(call_id: str) -> int:
    """Remove the platform participant id from every consent reply's speaker link; the number removed.

    A chat reply linked to its transcript speaker by the platform's
    participant id (docs/meeting-consent.md) holds that id, which the
    transcript's segments also carried; it goes with them. The link keeps its
    basis and time, and names no speaker. The write is conditional on the
    record's version as read (the consent resolver's guard), and bumps it, so a
    consent write racing this one is read again rather than restoring an id;
    and once TranscriptDeletedAt is set, which record_deletion does first, the
    resolver stores no new one.
    """
    for _attempt in range(QUOTE_STRIP_ATTEMPTS):
        item = (
            _table()
            .get_item(
                Key=meeting_key(call_id),
                ProjectionExpression="#c",
                ExpressionAttributeNames={"#c": "Consent"},
                ConsistentRead=True,
            )
            .get("Item")
        )
        record = (item or {}).get("Consent")
        if not isinstance(record, dict):
            return 0
        stripped, changed = without_platform_ids(record)
        if not changed:
            return 0
        removed = sum(
            1
            for event in record.get("Events") or []
            if isinstance(event, dict)
            and isinstance(event.get("SpeakerLink"), dict)
            and "PlatformParticipantId" in event["SpeakerLink"]
        )
        version = int(record.get("Version") or 0)
        names = {"#c": "Consent", "#e": "Events", "#v": "Version"}
        values: Dict[str, Any] = {":events": stripped["Events"], ":next": version + 1}
        if version:
            condition = "#c.#v = :version"
            values[":version"] = version
        else:
            condition = "attribute_not_exists(#c.#v)"
        try:
            _table().update_item(
                Key=meeting_key(call_id),
                UpdateExpression="SET #c.#e = :events, #c.#v = :next",
                ConditionExpression=f"attribute_exists(PK) AND {condition}",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if _error_code(error) != "ConditionalCheckFailedException":
                raise
            LOGGER.info(
                "the consent record of %s changed while removing its platform ids; reading it again",
                call_id,
            )
            continue
        return removed
    # Raised, so the invocation fails and is retried, as for the checklist.
    raise ConsentPlatformIdsNotRemoved(
        f"the consent record of {call_id} kept changing while removing its platform ids"
    )


# ── deleting ────────────────────────────────────────────────────────────────


def _transcript_items(call_id: str) -> Iterable[Dict[str, Any]]:
    """Every item under the meeting's transcript partition, a page at a time."""
    query: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"trs#{call_id}"),
        "ProjectionExpression": "PK, SK",
        "ConsistentRead": True,
    }
    while True:
        page = _table().query(**query)
        yield from page.get("Items", [])
        if not page.get("LastEvaluatedKey"):
            return
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def delete_segments(call_id: str) -> int:
    deleted = 0
    # The batch writer resends any item DynamoDB leaves unprocessed.
    with _table().batch_writer() as batch:
        for item in _transcript_items(call_id):
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            deleted += 1
    return deleted


def _versions_of(key: str) -> List[Dict[str, str]]:
    """Every version and delete marker of exactly ``key`` (an unversioned object has one)."""
    found: List[Dict[str, str]] = []
    paginator = _client("s3").get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=RECORDINGS_BUCKET, Prefix=key):
        for entry in page.get("Versions", []) + page.get("DeleteMarkers", []):
            if entry.get("Key") == key:
                found.append({"Key": key, "VersionId": entry["VersionId"]})
    return found


def delete_files(call_id: str) -> List[str]:
    """Delete every version of the meeting's transcript files; the keys that held any."""
    if not RECORDINGS_BUCKET:
        return []
    deleted: List[str] = []
    for key in transcript_file_keys(call_id):
        versions = _versions_of(key)
        # DeleteObjects takes up to 1000 entries.
        for start in range(0, len(versions), 1000):
            response = _client("s3").delete_objects(
                Bucket=RECORDINGS_BUCKET,
                Delete={"Objects": versions[start : start + 1000], "Quiet": True},
            )
            errors = response.get("Errors") or []
            if errors:
                raise RuntimeError(
                    f"could not delete {len(errors)} version(s) of {key}: "
                    + "; ".join(f"{e.get('Code')}: {e.get('Message')}" for e in errors[:3])
                )
        if versions:
            deleted.append(key)
    return deleted


def _attachment_items(call_id: str) -> Iterable[Dict[str, Any]]:
    """Every chat attachment item of the meeting, a page at a time."""
    query: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"att#{call_id}"),
        "ProjectionExpression": "PK, SK",
        "ConsistentRead": True,
    }
    while True:
        page = _table().query(**query)
        yield from page.get("Items", [])
        if not page.get("LastEvaluatedKey"):
            return
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _partition_items(
    pk: str, *, names: Optional[Dict[str, str]] = None
) -> Iterable[Dict[str, Any]]:
    """Every item under one partition key, a page at a time, with PK, SK and ``names``."""
    query: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(pk),
        "ProjectionExpression": ", ".join(["PK", "SK", *(names or {})]),
        "ConsistentRead": True,
    }
    if names:
        query["ExpressionAttributeNames"] = names
    while True:
        page = _table().query(**query)
        yield from page.get("Items", [])
        if not page.get("LastEvaluatedKey"):
            return
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def delete_transcript_bound_notifications(call_id: str) -> int:
    """Delete the meeting's notifications that may quote the transcript; returns how many rows.

    The notifications of TRANSCRIPT_BOUND_CATEGORIES and their dedup pointers,
    both of which carry the category, under ``nt#<CallId>``; the partition's
    other rows (other categories, publish counters, users' views) are kept.
    Also every row under ``gd#<CallId>``: the live guidance analyzer's state, which
    holds a summary drawn from the transcript, and guidance a development build kept.
    """
    deleted = 0
    with _table().batch_writer() as batch:
        for item in _partition_items(f"nt#{call_id}", names={"#category": "Category"}):
            if item.get("Category") in TRANSCRIPT_BOUND_CATEGORIES:
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                deleted += 1
        for item in _partition_items(f"gd#{call_id}"):
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            deleted += 1
    return deleted


def delete_whiteboards(call_id: str) -> int:
    """Delete every row under ``wb#<CallId>``; returns how many.

    A meeting's whiteboards are drawn from its transcript, so they are deleted
    with it, whoever wrote them: each whiteboard, every version, the count, the
    write counters and the assistant's state with its notes on the meeting so far.
    Once the transcript is deleted the resolver refuses any further write.
    """
    deleted = 0
    with _table().batch_writer() as batch:
        for item in _partition_items(f"wb#{call_id}"):
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            deleted += 1
    return deleted


def delete_attachments(call_id: str) -> int:
    """Delete the meeting's chat attachments: their items, and every version of their files.

    The files are found by the meeting's folder, with a trailing slash so one
    meeting's folder never matches another whose id merely begins the same way.
    Returns the number of files that had any version.
    """
    with _table().batch_writer() as batch:
        for item in _attachment_items(call_id):
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    if not RECORDINGS_BUCKET:
        return 0
    folder = f"{ATTACHMENTS_PREFIX}{posixify_filename(call_id)}/"
    entries: List[Dict[str, str]] = []
    keys = set()
    paginator = _client("s3").get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=RECORDINGS_BUCKET, Prefix=folder):
        for entry in page.get("Versions", []) + page.get("DeleteMarkers", []):
            if entry.get("Key", "").startswith(folder):
                entries.append({"Key": entry["Key"], "VersionId": entry["VersionId"]})
                keys.add(entry["Key"])
    for start in range(0, len(entries), 1000):
        response = _client("s3").delete_objects(
            Bucket=RECORDINGS_BUCKET,
            Delete={"Objects": entries[start : start + 1000], "Quiet": True},
        )
        errors = response.get("Errors") or []
        if errors:
            raise RuntimeError(
                f"could not delete {len(errors)} version(s) of the chat attachments: "
                + "; ".join(f"{e.get('Code')}: {e.get('Message')}" for e in errors[:3])
            )
    return len(keys)


def _s3_data_sources() -> List[str]:
    """The ids of the transcript knowledge base's S3 data sources."""
    agent = _client("bedrock-agent")
    ids: List[str] = []
    token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"knowledgeBaseId": TRANSCRIPT_KB_ID, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = agent.list_data_sources(**kwargs)
        for summary in page.get("dataSourceSummaries", []):
            source = agent.get_data_source(
                knowledgeBaseId=TRANSCRIPT_KB_ID, dataSourceId=summary["dataSourceId"]
            )
            configuration = source.get("dataSource", {}).get("dataSourceConfiguration", {})
            if configuration.get("type") == "S3":
                ids.append(summary["dataSourceId"])
        token = page.get("nextToken")
        if not token:
            return ids


def remove_from_knowledge_base(keys: List[str]) -> None:
    """Remove the files' entries from the transcript knowledge base.

    Not a reason to retry: the files are already gone, so the knowledge base's
    scheduled sync removes the entries on its next run if this could not.
    """
    if not TRANSCRIPT_KB_ID or not keys:
        return
    documents = [
        {"dataSourceType": "S3", "s3": {"uri": f"s3://{RECORDINGS_BUCKET}/{key}"}}
        for key in keys
        if not key.endswith(".metadata.json")
    ]
    if not documents:
        return
    try:
        for data_source_id in _s3_data_sources():
            try:
                _client("bedrock-agent").delete_knowledge_base_documents(
                    knowledgeBaseId=TRANSCRIPT_KB_ID,
                    dataSourceId=data_source_id,
                    documentIdentifiers=documents,
                )
            except ClientError as error:
                LOGGER.warning(
                    "the knowledge base did not take the deletion from data source %s (%s); "
                    "its next sync removes the entries",
                    data_source_id,
                    _error_code(error),
                )
    except (ClientError, BotoCoreError):
        LOGGER.warning(
            "could not reach the transcript knowledge base; its next sync removes the entries",
            exc_info=True,
        )


def delete_transcript(call_id: str, trigger: str) -> Dict[str, Any]:
    segments = delete_segments(call_id)
    keys = delete_files(call_id)
    remove_from_knowledge_base(keys)
    attachments = delete_attachments(call_id)
    notifications = delete_transcript_bound_notifications(call_id)
    record_deletion(call_id, trigger, segments, len(keys) + attachments)
    # After the record: the whiteboard resolver refuses every write once the
    # meeting carries TranscriptDeletedAt, so nothing written to them can
    # outlast this sweep, as for the checklist quotes below.
    whiteboards = delete_whiteboards(call_id)
    checklist_items = strip_checklist_quotes(call_id)
    consent_ids = strip_consent_platform_ids(call_id)
    LOGGER.info(
        json.dumps(
            {
                "msg": "deleted the meeting's transcript",
                "callId": call_id,
                "trigger": trigger,
                "segmentsDeleted": segments,
                "filesDeleted": len(keys),
                "attachmentFilesDeleted": attachments,
                "checklistItemsWithQuotesRemoved": checklist_items,
                "consentPlatformIdsRemoved": consent_ids,
                "notificationRowsDeleted": notifications,
                "whiteboardRowsDeleted": whiteboards,
            }
        )
    )
    return {
        "action": "deleted",
        "segmentsDeleted": segments,
        "filesDeleted": len(keys) + attachments,
    }


# ── the recordings ──────────────────────────────────────────────────────────

#: The meeting record's recording links: the current one of each kind, and the
#: list of every capture session's (Call.RecordingUrls, Call.VideoRecordingUrls),
#: with the prefix a key each names must be directly under.
AUDIO_LINKS = ("RecordingUrl", "RecordingUrls")
VIDEO_LINKS = ("VideoRecordingUrl", "VideoRecordingUrls")
#: Kept in step with the link lists by the updateCall resolver, and removed with them.
LINK_SESSION_FIELDS = ("RecordingUrlSessions", "VideoRecordingUrlSessions")

#: Attempts at removing the links while a recording link is being added.
LINK_REMOVE_ATTEMPTS = 4


class RecordingLinksKeptChanging(RuntimeError):
    """Recording links kept being added while the recordings were being deleted."""


class UnresolvedRecordingLink(RuntimeError):
    """The meeting links to a recording this function cannot name a key for."""


def recording_key_from_url(url: Any, prefix: str) -> Optional[str]:
    """The recordings-bucket key a stored recording link names, or None.

    The streaming producers store ``https://<bucket>.s3.<region>.<suffix>/<key>``,
    the key URL-encoded by the Virtual Participant and as it is by the
    transcriber; the upload finalizer stores ``s3://<bucket>/<key>``, the key as
    it is. Only a key in this deployment's bucket, directly under ``prefix``, is
    returned, so a deletion stays inside that kind of recording.
    """
    if not isinstance(url, str) or not url or not RECORDINGS_BUCKET:
        return None
    if url.startswith("s3://"):
        bucket, _, key = url[len("s3://") :].partition("/")
        if bucket != RECORDINGS_BUCKET:
            return None
    else:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").startswith(
            f"{RECORDINGS_BUCKET}.s3."
        ):
            return None
        key = unquote(parsed.path.lstrip("/"))
    name = key[len(prefix) :]
    if not key.startswith(prefix) or not name or "/" in name or name in (".", ".."):
        return None
    return key


def _links(meeting: Dict[str, Any], fields: Iterable[str]) -> List[str]:
    """Every link the record holds in ``fields`` (a string or a list of them), each once."""
    urls: List[str] = []
    for field in fields:
        value = meeting.get(field)
        for url in value if isinstance(value, list) else [value]:
            if isinstance(url, str) and url and url not in urls:
                urls.append(url)
    return urls


def linked_recording_keys(meeting: Dict[str, Any]) -> List[str]:
    """The keys of the recording files the meeting record links to, each once.

    A WAV's raw audio, which the transcriber stores beside it under the same
    name, goes with it. A link no key can be named for raises
    UnresolvedRecordingLink before anything is deleted: removing the links
    would then leave a recording stored with nothing pointing at it, while the
    meeting says its recordings were deleted. The run fails instead, and is
    retried and then kept on the dead-letter queue, with the links in place.
    """
    keys: List[str] = []
    for fields, prefix in ((AUDIO_LINKS, AUDIO_PREFIX), (VIDEO_LINKS, VIDEO_PREFIX)):
        for url in _links(meeting, fields):
            key = recording_key_from_url(url, prefix)
            if not key:
                raise UnresolvedRecordingLink(
                    f"a {fields[0]} link of the meeting names no recording under {prefix} "
                    "in this deployment's bucket; its links are left in place"
                )
            siblings = [key, key[: -len(".wav")] + ".raw"] if key.endswith(".wav") else [key]
            keys.extend(k for k in siblings if k not in keys)
    return keys


def _delete_versions(entries: List[Dict[str, str]], what: str) -> None:
    """Delete the listed versions, a thousand at a time; raise when S3 keeps any."""
    for start in range(0, len(entries), 1000):
        response = _client("s3").delete_objects(
            Bucket=RECORDINGS_BUCKET,
            Delete={"Objects": entries[start : start + 1000], "Quiet": True},
        )
        errors = response.get("Errors") or []
        if errors:
            raise RuntimeError(
                f"could not delete {len(errors)} version(s) of {what}: "
                + "; ".join(f"{e.get('Code')}: {e.get('Message')}" for e in errors[:3])
            )


def _listed_versions(prefix: str, keep) -> List[Dict[str, str]]:
    """Every version and delete marker under ``prefix`` whose key ``keep`` accepts."""
    found: List[Dict[str, str]] = []
    paginator = _client("s3").get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=RECORDINGS_BUCKET, Prefix=prefix):
        for entry in page.get("Versions", []) + page.get("DeleteMarkers", []):
            if keep(entry.get("Key", "")):
                found.append({"Key": entry["Key"], "VersionId": entry["VersionId"]})
    return found


def delete_recording_files(call_id: str, meeting: Dict[str, Any]) -> int:
    """Delete every version of the meeting's recording files; the number of files that had any.

    The files the record links to (linked_recording_keys), and the Virtual
    Participant's video segments in ``<chunks prefix><CallId>/`` -- the meeting
    id as it is, with a trailing slash so that one meeting's folder never
    matches another whose id merely begins the same way. No recording is found
    by its file name: the producers turn the meeting id into a file name by
    replacing characters, so two meetings can share one, and the transcriber
    stores a second meeting's recording of a taken name under that name with
    a time added. A name is no proof of whose recording it is.
    """
    if not RECORDINGS_BUCKET:
        return 0
    entries: List[Dict[str, str]] = []
    for key in linked_recording_keys(meeting):
        entries.extend(_listed_versions(key, lambda k, key=key: k == key))
    folder = f"{VIDEO_CHUNKS_PREFIX}{call_id}/"
    entries.extend(_listed_versions(folder, lambda k: k.startswith(folder)))
    _delete_versions(entries, f"the recordings of {call_id}")
    return len({e["Key"] for e in entries})


def _read_links(call_id: str) -> Optional[Dict[str, Any]]:
    names = {f"#l{n}": field for n, field in enumerate(AUDIO_LINKS + VIDEO_LINKS)}
    response = _table().get_item(
        Key=meeting_key(call_id),
        ProjectionExpression=", ".join(["PK", *names]),
        ExpressionAttributeNames=names,
        ConsistentRead=True,
    )
    return response.get("Item")


def record_recordings_deletion(
    call_id: str, trigger: str, meeting: Dict[str, Any], files: int
) -> bool:
    """Remove the meeting's recording links and stamp the deletion; False if a link was added.

    The write is conditional on the links being as ``meeting`` read them: the
    current link of each kind the same, and each list of them as long, since
    the updateCall resolver only ever appends to it. A link added between the
    read and this write fails the condition, and the caller reads the meeting
    again and deletes that recording too, rather than removing a link to a
    file it never deleted. A link added after it is the processor's
    ``recording-added``, which finds the meeting's recordings deleted.
    The first time and trigger are kept; the count adds up.
    """
    names: Dict[str, str] = {}
    values: Dict[str, Any] = {":now": _iso(_now()), ":trigger": trigger, ":files": files}
    conditions = ["attribute_exists(PK)"]
    for n, field in enumerate(AUDIO_LINKS + VIDEO_LINKS):
        names[f"#l{n}"] = field
        value = meeting.get(field)
        if value is None:
            conditions.append(f"attribute_not_exists(#l{n})")
        elif isinstance(value, list):
            conditions.append(f"size(#l{n}) = :l{n}")
            values[f":l{n}"] = len(value)
        else:
            conditions.append(f"#l{n} = :l{n}")
            values[f":l{n}"] = value
    for n, field in enumerate(LINK_SESSION_FIELDS):
        names[f"#s{n}"] = field
    removed = ", ".join(["RecordingDeletionScheduledFor", *names])
    try:
        _table().update_item(
            Key=meeting_key(call_id),
            UpdateExpression=(
                "SET RecordingsDeletedAt = if_not_exists(RecordingsDeletedAt, :now), "
                "RecordingDeletionTrigger = if_not_exists(RecordingDeletionTrigger, :trigger), "
                "RecordingDeletionLastRunAt = :now "
                "ADD RecordingFilesDeleted :files "
                f"REMOVE {removed}"
            ),
            ConditionExpression=" AND ".join(conditions),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as error:
        if _error_code(error) != "ConditionalCheckFailedException":
            raise
        return False
    return True


def delete_recordings(call_id: str, trigger: str) -> Dict[str, Any]:
    """Delete the meeting's recordings, remove its links to them, and stamp the deletion."""
    files = 0
    for _attempt in range(LINK_REMOVE_ATTEMPTS):
        meeting = _read_links(call_id)
        files += delete_recording_files(call_id, meeting or {})
        if meeting is None:
            LOGGER.info(
                "meeting %s has no record; its recordings were deleted all the same", call_id
            )
            break
        if record_recordings_deletion(call_id, trigger, meeting, files):
            break
        LOGGER.info(
            "a recording link was added to %s while its recordings were deleted; reading it again",
            call_id,
        )
    else:
        # Raised, so the invocation fails and is retried: every step is safe
        # to run again, and a link must not be removed while its file is kept.
        raise RecordingLinksKeptChanging(
            f"recording links kept being added to {call_id} while its recordings were deleted"
        )
    LOGGER.info(
        json.dumps(
            {
                "msg": "deleted the meeting's recordings",
                "callId": call_id,
                "trigger": trigger,
                "filesDeleted": files,
            }
        )
    )
    return {"action": "deleted", "filesDeleted": files}


def recording_added(call_id: str) -> Dict[str, Any]:
    """A recording link was stored: delete the recordings again if they have been deleted.

    Once a meeting's recordings are deleted they stay deleted. A recording
    uploaded after that -- the transcriber stores a session's recording after
    its END, and a video can take longer than the summary -- or one of a later
    session, is deleted when its link is stored, whatever the setting says now.
    Until then, a recording waits for the meeting's deletion, if it has one.
    """
    meeting = read_meeting(call_id) or {}
    if not meeting.get("RecordingsDeletedAt"):
        return {"action": "none"}
    return delete_recordings(call_id, RECORDING_ADDED)


def delete_kind(call_id: str, trigger: str, kind: str) -> Dict[str, Any]:
    if kind == RECORDINGS:
        return delete_recordings(call_id, trigger)
    return delete_transcript(call_id, trigger)


# ── scheduling ──────────────────────────────────────────────────────────────


def _target_arn(context: Any) -> str:
    """This function's unqualified ARN, which the schedule invokes."""
    return ":".join(context.invoked_function_arn.split(":")[:7])


def schedule_deletion(
    call_id: str, moment: datetime, context: Any, kind: str = TRANSCRIPT
) -> Dict[str, Any]:
    """Schedule the meeting's deletion of ``kind`` for ``moment``, unless one is due sooner."""
    spec = KINDS[kind]
    meeting = read_meeting(call_id) or {}
    current = _parse_iso(meeting.get(spec.scheduled_field))
    # EventBridge Scheduler runs a one-time schedule on the minute; one in the
    # past would not run at all.
    moment = max(moment, _now() + timedelta(minutes=1))
    if current is not None and _now() < current <= moment:
        LOGGER.info(
            "meeting %s is already due for %s deletion at %s", call_id, spec.what, _iso(current)
        )
        return {"action": "unchanged", "scheduledFor": _iso(current)}

    definition = {
        "Name": schedule_name(call_id, kind),
        "GroupName": SCHEDULE_GROUP_NAME,
        "ScheduleExpression": f"at({moment.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%S})",
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "ActionAfterCompletion": "DELETE",
        "Description": f"Deletes one meeting's {spec.what} (LMA Retention settings)",
        "Target": {
            "Arn": _target_arn(context),
            "RoleArn": SCHEDULER_ROLE_ARN,
            # A transcript schedule names no Kind, as every one made before
            # the recordings could be deleted does not.
            "Input": json.dumps(
                {
                    "CallId": call_id,
                    "Trigger": SCHEDULED,
                    **({"Kind": kind} if kind != TRANSCRIPT else {}),
                }
            ),
            "RetryPolicy": {"MaximumEventAgeInSeconds": 86400, "MaximumRetryAttempts": 185},
            **(
                {"DeadLetterConfig": {"Arn": DEAD_LETTER_QUEUE_ARN}}
                if DEAD_LETTER_QUEUE_ARN
                else {}
            ),
        },
    }
    scheduler = _client("scheduler")
    try:
        scheduler.create_schedule(**definition)
    except ClientError as error:
        if _error_code(error) != "ConflictException":
            raise
        scheduler.update_schedule(**definition)
    record_schedule(call_id, moment, kind)
    LOGGER.info(
        json.dumps(
            {"msg": f"scheduled the {spec.what} deletion", "callId": call_id, "at": _iso(moment)}
        )
    )
    return {"action": "scheduled", "scheduledFor": _iso(moment)}


def _session_of(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return max(number, 1)


def cancel_deletion(call_id: str, session: int, kind: str = TRANSCRIPT) -> Dict[str, Any]:
    """Cancel the meeting's pending deletion of ``kind`` while rejoined ``session`` is live.

    Only while the meeting is STARTED in that session: this is invoked
    asynchronously when the session starts, so it can arrive after the
    session's own END has dealt with the deletion, and must then leave it
    alone. Only when a deletion will follow, too: with the setting off, the
    session's END schedules none, and the pending one is kept, as a scheduled
    deletion is kept when the setting is turned off.

    The recorded time is removed first, conditional on the meeting being as it
    was read -- still live in the session, the deletion still pending at the
    same time, the transcript still there -- and the schedule is deleted only
    when that write succeeds.
    """
    spec = KINDS[kind]
    response = _table().get_item(
        Key=meeting_key(call_id),
        ProjectionExpression=(
            f"PK, #status, SessionStartedNumber, {spec.scheduled_field}, {spec.deleted_field}"
        ),
        ExpressionAttributeNames={"#status": "Status"},
        ConsistentRead=True,
    )
    meeting = response.get("Item")
    if meeting is None:
        return {"action": "none"}
    if meeting.get(spec.deleted_field):
        deleted_key = spec.deleted_field[0].lower() + spec.deleted_field[1:]
        return {"action": "deleted", deleted_key: meeting[spec.deleted_field]}
    scheduled_for = meeting.get(spec.scheduled_field)
    if not scheduled_for:
        return {"action": "none"}
    if (
        meeting.get("Status") != "STARTED"
        or _session_of(meeting.get("SessionStartedNumber")) != session
    ):
        LOGGER.info(
            "meeting %s is not live in session %s; deletion left as it is", call_id, session
        )
        return {"action": "unchanged"}
    if delay_of(kind) is None:
        LOGGER.info("%s deletion is off; meeting %s keeps its pending one", spec.what, call_id)
        return {"action": "kept", "scheduledFor": scheduled_for}
    try:
        _table().update_item(
            Key=meeting_key(call_id),
            UpdateExpression=f"REMOVE {spec.scheduled_field}",
            ConditionExpression=(
                f"attribute_exists(PK) AND attribute_not_exists({spec.deleted_field}) "
                "AND #status = :started AND SessionStartedNumber = :session "
                f"AND {spec.scheduled_field} = :at"
            ),
            ExpressionAttributeNames={"#status": "Status"},
            ExpressionAttributeValues={
                ":started": "STARTED",
                ":session": session,
                ":at": scheduled_for,
            },
        )
    except ClientError as error:
        if _error_code(error) != "ConditionalCheckFailedException":
            raise
        LOGGER.info("meeting %s changed while its deletion was being cancelled", call_id)
        return {"action": "unchanged"}
    try:
        _client("scheduler").delete_schedule(
            Name=schedule_name(call_id, kind), GroupName=SCHEDULE_GROUP_NAME
        )
    except ClientError as error:
        if _error_code(error) != "ResourceNotFoundException":
            raise
    LOGGER.info(json.dumps({"msg": f"cancelled the {spec.what} deletion", "callId": call_id}))
    return {"action": "cancelled"}


def delete_now(call_id: str, trigger: str, context: Any, kind: str = TRANSCRIPT) -> Dict[str, Any]:
    outcome = delete_kind(call_id, trigger, kind)
    outcome["sweep"] = schedule_deletion(
        call_id, _now() + timedelta(minutes=SWEEP_MINUTES), context, kind
    )
    return outcome


def on_meeting_event(call_id: str, trigger: str, context: Any, kind: str) -> Dict[str, Any]:
    """A meeting ended or was summarized: delete ``kind`` now, or schedule it, by its setting."""
    delay = delay_of(kind)
    if delay is None:
        return {"action": "none"}

    if trigger == MEETING_ENDED:
        wait = delay + (SUMMARY_WAIT_MINUTES if IS_TRANSCRIPT_SUMMARY_ENABLED else 0)
    else:
        # A summary is written after its meeting has ended. One that is written
        # while the meeting is live again -- an earlier session's summary
        # arriving after a rejoined session has started -- neither deletes nor
        # schedules: that session's own END and summary do.
        meeting = read_meeting(call_id) or {}
        if meeting.get("Status") == "STARTED":
            LOGGER.info("meeting %s is live; its summary leaves the deletion as it is", call_id)
            return {"action": "live"}
        wait = delay
    if wait == 0:
        return delete_now(call_id, trigger, context, kind)
    return schedule_deletion(call_id, _now() + timedelta(minutes=wait), context, kind)


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    call_id = event.get("CallId")
    trigger = event.get("Trigger")
    if not isinstance(call_id, str) or not call_id or trigger not in TRIGGERS:
        raise ValueError("expected a CallId and a Trigger of " + ", ".join(TRIGGERS))

    # A scheduled deletion runs whatever the setting says now: participants may
    # have been told the transcript, or the recordings, would be deleted then.
    if trigger == SCHEDULED:
        # No Kind is the transcript's, as in every schedule made before the
        # recordings could be deleted. Any other value is refused, so that a
        # deletion run again by hand with a mistyped Kind deletes nothing.
        kind = event.get("Kind", TRANSCRIPT)
        if kind not in KINDS:
            raise ValueError("expected a Kind of " + ", ".join(KINDS))
        return delete_kind(call_id, trigger, kind)
    if trigger == RECORDING_ADDED:
        return recording_added(call_id)

    # The two settings are independent: each kind is acted on whatever the
    # other did, and a failure of either fails the invocation, after both have
    # run, so that it is retried. Every step is safe to repeat.
    outcomes: Dict[str, Dict[str, Any]] = {}
    failures: List[Exception] = []
    for kind in (TRANSCRIPT, RECORDINGS):
        try:
            if trigger == MEETING_REOPENED:
                outcomes[kind] = cancel_deletion(
                    call_id, _session_of(event.get("SessionNumber")), kind
                )
            else:
                outcomes[kind] = on_meeting_event(call_id, trigger, context, kind)
        except Exception as error:  # pylint: disable=broad-except -- raised below
            LOGGER.exception(
                "the %s deletion of %s failed on %s", KINDS[kind].what, call_id, trigger
            )
            failures.append(error)
    if failures:
        raise failures[0]
    # The transcript's outcome, as before the recordings could be deleted, and
    # the recordings' beside it when their setting did anything.
    outcome = dict(outcomes[TRANSCRIPT])
    if outcomes[RECORDINGS] != {"action": "none"}:
        outcome["recordings"] = outcomes[RECORDINGS]
    return outcome
