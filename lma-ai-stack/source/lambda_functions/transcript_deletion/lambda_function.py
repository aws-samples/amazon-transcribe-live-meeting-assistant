#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Deletes a meeting's transcript once it is summarized, when the Retention section says to.

The Retention section's "Delete the transcript after the summary" setting
(``lma_config.deletion_delay_minutes``) is off, or a delay: immediately, or 15,
30 or 60 minutes after the summary is written. This function is invoked with
the meeting's ``CallId`` and a ``Trigger``:

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
    Deletes.
``meeting-reopened``
    By the call event processor, with the ``SessionNumber``, when a rejoined
    session of the meeting has started and made it live again
    (docs/virtual-participant.md#rejoin). Cancels the pending deletion while the
    meeting is still live in that session and the setting is on, because that
    session's END schedules one afresh; see cancel_deletion.

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
* the transcript knowledge base's entries for those files.

The meeting record is then stamped with when its transcript was deleted, and
how many segments and files were removed, which the meeting page shows. The
summary, the meeting record, the recordings and the consent record are kept.

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
)
from lma_meeting_preparation.rules import strip_quotes  # configuration layer

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
TRIGGERS = (MEETING_ENDED, SUMMARY_WRITTEN, SCHEDULED, MEETING_REOPENED)

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


def current_delay() -> Optional[int]:
    """The setting as saved now, not as cached: a save applies to the next meeting to end.

    One read of the configuration table, twice a meeting. Without the table
    named, off (None).
    """
    global _STORE  # pylint: disable=global-statement -- one store per process
    if _STORE is None and os.environ.get(TABLE_NAME_ENV_VAR):
        _STORE = ConfigurationStore()
    if _STORE is not None:
        _STORE.invalidate()
    return deletion_delay_minutes(store=_STORE)


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


def schedule_name(call_id: str) -> str:
    """A schedule name for the meeting: at most 64 characters, from a fixed alphabet."""
    return "transcript-" + hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:48]


# ── the meeting record ──────────────────────────────────────────────────────


def read_meeting(call_id: str) -> Optional[Dict[str, Any]]:
    response = _table().get_item(
        Key=meeting_key(call_id),
        ProjectionExpression="PK, #status, TranscriptDeletionScheduledFor, TranscriptDeletedAt",
        ExpressionAttributeNames={"#status": "Status"},
        ConsistentRead=True,
    )
    return response.get("Item")


def record_schedule(call_id: str, moment: datetime) -> None:
    try:
        _table().update_item(
            Key=meeting_key(call_id),
            UpdateExpression="SET TranscriptDeletionScheduledFor = :at",
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
    record_deletion(call_id, trigger, segments, len(keys) + attachments)
    checklist_items = strip_checklist_quotes(call_id)
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
            }
        )
    )
    return {
        "action": "deleted",
        "segmentsDeleted": segments,
        "filesDeleted": len(keys) + attachments,
    }


# ── scheduling ──────────────────────────────────────────────────────────────


def _target_arn(context: Any) -> str:
    """This function's unqualified ARN, which the schedule invokes."""
    return ":".join(context.invoked_function_arn.split(":")[:7])


def schedule_deletion(call_id: str, moment: datetime, context: Any) -> Dict[str, Any]:
    """Schedule the meeting's deletion for ``moment``, unless one is already due sooner."""
    meeting = read_meeting(call_id) or {}
    current = _parse_iso(meeting.get("TranscriptDeletionScheduledFor"))
    # EventBridge Scheduler runs a one-time schedule on the minute; one in the
    # past would not run at all.
    moment = max(moment, _now() + timedelta(minutes=1))
    if current is not None and _now() < current <= moment:
        LOGGER.info("meeting %s is already due for deletion at %s", call_id, _iso(current))
        return {"action": "unchanged", "scheduledFor": _iso(current)}

    definition = {
        "Name": schedule_name(call_id),
        "GroupName": SCHEDULE_GROUP_NAME,
        "ScheduleExpression": f"at({moment.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%S})",
        "ScheduleExpressionTimezone": "UTC",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "ActionAfterCompletion": "DELETE",
        "Description": "Deletes one meeting's transcript (LMA Retention settings)",
        "Target": {
            "Arn": _target_arn(context),
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": json.dumps({"CallId": call_id, "Trigger": SCHEDULED}),
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
    record_schedule(call_id, moment)
    LOGGER.info(
        json.dumps(
            {"msg": "scheduled the transcript deletion", "callId": call_id, "at": _iso(moment)}
        )
    )
    return {"action": "scheduled", "scheduledFor": _iso(moment)}


def _session_of(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 1
    return max(number, 1)


def cancel_deletion(call_id: str, session: int) -> Dict[str, Any]:
    """Cancel the meeting's pending deletion while rejoined ``session`` is live.

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
    response = _table().get_item(
        Key=meeting_key(call_id),
        ProjectionExpression=(
            "PK, #status, SessionStartedNumber, TranscriptDeletionScheduledFor, TranscriptDeletedAt"
        ),
        ExpressionAttributeNames={"#status": "Status"},
        ConsistentRead=True,
    )
    meeting = response.get("Item")
    if meeting is None:
        return {"action": "none"}
    if meeting.get("TranscriptDeletedAt"):
        return {"action": "deleted", "transcriptDeletedAt": meeting["TranscriptDeletedAt"]}
    if (
        meeting.get("Status") != "STARTED"
        or _session_of(meeting.get("SessionStartedNumber")) != session
    ):
        LOGGER.info(
            "meeting %s is not live in session %s; deletion left as it is", call_id, session
        )
        return {"action": "unchanged"}
    scheduled_for = meeting.get("TranscriptDeletionScheduledFor")
    if not scheduled_for:
        return {"action": "none"}
    if current_delay() is None:
        LOGGER.info("deletion is off; meeting %s keeps its pending deletion", call_id)
        return {"action": "kept", "scheduledFor": scheduled_for}
    try:
        _table().update_item(
            Key=meeting_key(call_id),
            UpdateExpression="REMOVE TranscriptDeletionScheduledFor",
            ConditionExpression=(
                "attribute_exists(PK) AND attribute_not_exists(TranscriptDeletedAt) "
                "AND #status = :started AND SessionStartedNumber = :session "
                "AND TranscriptDeletionScheduledFor = :at"
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
            Name=schedule_name(call_id), GroupName=SCHEDULE_GROUP_NAME
        )
    except ClientError as error:
        if _error_code(error) != "ResourceNotFoundException":
            raise
    LOGGER.info(json.dumps({"msg": "cancelled the transcript deletion", "callId": call_id}))
    return {"action": "cancelled"}


def delete_now(call_id: str, trigger: str, context: Any) -> Dict[str, Any]:
    outcome = delete_transcript(call_id, trigger)
    outcome["sweep"] = schedule_deletion(
        call_id, _now() + timedelta(minutes=SWEEP_MINUTES), context
    )
    return outcome


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    call_id = event.get("CallId")
    trigger = event.get("Trigger")
    if not isinstance(call_id, str) or not call_id or trigger not in TRIGGERS:
        raise ValueError("expected a CallId and a Trigger of " + ", ".join(TRIGGERS))

    # A scheduled deletion runs whatever the setting says now: participants may
    # have been told the transcript would be deleted at that time.
    if trigger == SCHEDULED:
        return delete_transcript(call_id, trigger)
    if trigger == MEETING_REOPENED:
        return cancel_deletion(call_id, _session_of(event.get("SessionNumber")))

    delay = current_delay()
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
        return delete_now(call_id, trigger, context)
    return schedule_deletion(call_id, _now() + timedelta(minutes=wait), context)
