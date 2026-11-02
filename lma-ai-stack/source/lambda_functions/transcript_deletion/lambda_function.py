#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
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
    deletion forward to the summary plus the delay, or deletes now.
``scheduled``
    By the one-time EventBridge Scheduler schedule either of the above made.
    Deletes.

A schedule is only ever moved earlier, so a redelivered END cannot postpone a
deletion. After a deletion made on the spot, one more is scheduled
``SWEEP_MINUTES`` later, which removes anything written for the meeting after
it: a late assistant answer, or a segment an engine corrected after the end.

What is deleted, for the meeting:

* every item under its transcript partition (``trs#<CallId>``) -- each turn of
  the transcript, partial or final, and each meeting assistant answer -- apart
  from category markers, which hold only a category's name;
* ``IssuesDetected`` on the meeting record, which quotes the transcript;
* every version of the transcript files in the recordings bucket: the text
  file the summary step writes for the transcript knowledge base, its metadata
  file, and an uploaded meeting's Amazon Transcribe output;
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

# pylint: enable=import-error

# Stdlib logging rather than Powertools: this function carries only the
# configuration layer, which has no third-party dependencies.
LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})

EVENT_SOURCING_TABLE = os.environ.get("EVENT_SOURCING_TABLE", "")
RECORDINGS_BUCKET = os.environ.get("RECORDINGS_BUCKET", "")
TRANSCRIPT_PREFIX = os.environ.get("TRANSCRIPT_PREFIX", "lma-transcripts/")
TRANSCRIPT_KB_ID = os.environ.get("TRANSCRIPT_KB_ID", "")
SCHEDULE_GROUP_NAME = os.environ.get("SCHEDULE_GROUP_NAME", "")
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
DEAD_LETTER_QUEUE_ARN = os.environ.get("DEAD_LETTER_QUEUE_ARN", "")
IS_TRANSCRIPT_SUMMARY_ENABLED = (
    os.environ.get("IS_TRANSCRIPT_SUMMARY_ENABLED", "true").lower() == "true"
)

#: Minutes after a deletion made on the spot at which the meeting is swept once more.
SWEEP_MINUTES = 10

#: Items under the transcript partition that are kept: they hold no transcript text.
KEPT_CHANNELS = frozenset({"CATEGORY_MATCH"})

MEETING_ENDED = "meeting-ended"
SUMMARY_WRITTEN = "summary-written"
SCHEDULED = "scheduled"
TRIGGERS = (MEETING_ENDED, SUMMARY_WRITTEN, SCHEDULED)

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
    named, the stack parameter's value.
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
        ProjectionExpression="PK, TranscriptDeletionScheduledFor, TranscriptDeletedAt",
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


# ── deleting ────────────────────────────────────────────────────────────────


def _transcript_items(call_id: str) -> Iterable[Dict[str, Any]]:
    """Every item under the meeting's transcript partition, a page at a time."""
    query: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"trs#{call_id}"),
        "ProjectionExpression": "PK, SK, #channel",
        "ExpressionAttributeNames": {"#channel": "Channel"},
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
            if item.get("Channel") in KEPT_CHANNELS:
                continue
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


def _s3_data_sources() -> List[str]:
    agent = _client("bedrock-agent")
    ids: List[str] = []
    token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"knowledgeBaseId": TRANSCRIPT_KB_ID, "maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = agent.list_data_sources(**kwargs)
        ids.extend(summary["dataSourceId"] for summary in page.get("dataSourceSummaries", []))
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
    record_deletion(call_id, trigger, segments, len(keys))
    LOGGER.info(
        json.dumps(
            {
                "msg": "deleted the meeting's transcript",
                "callId": call_id,
                "trigger": trigger,
                "segmentsDeleted": segments,
                "filesDeleted": len(keys),
            }
        )
    )
    return {"action": "deleted", "segmentsDeleted": segments, "filesDeleted": len(keys)}


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

    delay = current_delay()
    if delay is None:
        return {"action": "none"}

    if trigger == MEETING_ENDED:
        wait = delay + (SUMMARY_WAIT_MINUTES if IS_TRANSCRIPT_SUMMARY_ENABLED else 0)
    else:
        wait = delay
    if wait == 0:
        return delete_now(call_id, trigger, context)
    return schedule_deletion(call_id, _now() + timedelta(minutes=wait), context)
