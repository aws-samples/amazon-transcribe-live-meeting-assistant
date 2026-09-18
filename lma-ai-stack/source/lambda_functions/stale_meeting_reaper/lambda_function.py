#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Close out meetings that have stopped producing activity.

A meeting row moves to ``ENDED`` when an ``END`` event reaches the
``call_event_processor`` through ``CallDataStream``. Every client that streams
audio emits that event as part of an orderly shutdown, and two paths already
have a backstop for a client that never gets there: the Virtual Participant has
the VP stack's task reaper, and an uploaded recording has the
``upload_meeting_finalizer`` EventBridge rule. A meeting streamed from the
browser tab, the desktop capture app or the browser extension has neither, so if
the websocket connection's task goes away without running its close handler the
meeting row keeps the status it had — ``STARTED`` or ``TRANSCRIBING`` — and the
UI shows it as in progress for as long as the row is retained.

This function is the backstop for those meetings. On a schedule it looks for
meeting rows whose last activity is older than
``MEETING_INACTIVITY_TIMEOUT_IN_MINUTES`` and emits the same ``END`` event the
client would have sent, so the meeting ends, its summary is generated, and it
leaves the in-progress list.

How "last activity" is determined
---------------------------------
The meeting row at ``PK = SK = c#<CallId>`` carries ``UpdatedAt``, and the
processor issues an ``updateCallAggregation`` mutation for every non-partial
transcript segment, which runs ``updateCall.request.vtl`` against that row and
stamps ``UpdatedAt``. A meeting that is genuinely being transcribed therefore
refreshes ``UpdatedAt`` at conversational frequency, and one whose task died
stops refreshing it. Rows written before that mutation existed may have no
``UpdatedAt``, so ``CreatedAt`` is the fallback.

The consequence to keep in mind when choosing the timeout: a meeting that is
still connected but has produced no finalized speech for longer than the
timeout is indistinguishable from an abandoned one and will be ended. The
default is deliberately several hours for that reason.

Candidates are found through the ``TypeDateIndex`` GSI, which is range-sorted by
the meeting-list row's ``SK`` (``ts#<ISO8601>#id#<CallId>``) and therefore
chronological on meeting start time. The query covers meetings started between
``REAPER_LOOKBACK_IN_DAYS`` ago and the inactivity cutoff: anything newer than
the cutoff cannot be stale yet, and the lower bound keeps a single invocation's
work bounded. A meeting that started before the lookback window and is still
open is not reaped, so the lookback wants to comfortably exceed the longest
meeting a deployment expects.

Meetings still being processed by the upload pipeline are skipped. A batch
Amazon Transcribe job on a long recording can easily run past the inactivity
timeout without touching the meeting row, and ``upload_meeting_finalizer``
closes those meetings itself when the job finishes.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from boto3.dynamodb.conditions import Key
from botocore.config import Config as BotoCoreConfig
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_kinesis.client import KinesisClient
else:
    KinesisClient = object

LOGGER = Logger(location="%(filename)s:%(lineno)d - %(funcName)s()")

EVENT_SOURCING_TABLE = os.environ["EVENT_SOURCING_TABLE"]
CALL_DATA_STREAM_NAME = os.environ["CALL_DATA_STREAM_NAME"]
# 0 disables the reaper without removing its resources.
INACTIVITY_TIMEOUT_MINUTES = int(os.environ.get("MEETING_INACTIVITY_TIMEOUT_IN_MINUTES", "0"))
LOOKBACK_DAYS = float(os.environ.get("REAPER_LOOKBACK_IN_DAYS", "7"))
# Bounds the work a single invocation does; the next scheduled run continues.
MAX_MEETINGS_PER_RUN = int(os.environ.get("REAPER_MAX_MEETINGS_PER_RUN", "100"))

CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})
DYNAMODB_RESOURCE = boto3.resource("dynamodb", config=CLIENT_CONFIG)
KINESIS_CLIENT: KinesisClient = boto3.client("kinesis", config=CLIENT_CONFIG)

TYPE_DATE_INDEX = "TypeDateIndex"
ITEM_TYPE_CALL = "call"
# Statuses a meeting can be left in when its client never sends END.
OPEN_STATUSES = frozenset({"STARTED", "TRANSCRIBING"})
# Upload-pipeline job statuses that mean the pipeline still owns the meeting.
ACTIVE_UPLOAD_JOB_STATUSES = frozenset({"CREATED", "PENDING", "UPLOADED", "TRANSCRIBING"})
# DynamoDB BatchGetItem hard limit.
BATCH_GET_LIMIT = 100
# Pages of GSI results a single invocation reads.
MAX_QUERY_PAGES = 20
DEFAULT_OWNER = "system@lma.aws"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp written by the resolvers, or return None.

    ``$util.time.nowISO8601()`` renders ``Z`` rather than ``+00:00``, and rows
    written by Python code use ``+00:00``; both need to compare against an
    aware datetime.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _call_id_from_list_row(row: Dict[str, Any]) -> Optional[str]:
    """Read the meeting id from a list-tracking row.

    ``CallId`` is in the GSI projection; the ``SK`` (``ts#<ISO>#id#<CallId>``)
    is the fallback for rows written before that attribute was added.
    """
    call_id = row.get("CallId")
    if isinstance(call_id, str) and call_id:
        return call_id
    sort_key = row.get("SK", "")
    marker = "#id#"
    if isinstance(sort_key, str) and marker in sort_key:
        return sort_key.split(marker, 1)[1] or None
    return None


def _query_candidate_call_ids(window_start: datetime, cutoff: datetime) -> List[str]:
    """List meeting ids whose meeting-list row falls in the candidate window."""
    table = DYNAMODB_RESOURCE.Table(EVENT_SOURCING_TABLE)
    key_condition = Key("ItemType").eq(ITEM_TYPE_CALL) & Key("SK").between(
        f"ts#{window_start.isoformat()}",
        # "~" sorts after "#", so the upper bound includes every id at the cutoff.
        f"ts#{cutoff.isoformat()}#~",
    )
    call_ids: List[str] = []
    seen = set()
    last_key = None
    for _page in range(MAX_QUERY_PAGES):
        query_kwargs: Dict[str, Any] = {
            "IndexName": TYPE_DATE_INDEX,
            "KeyConditionExpression": key_condition,
        }
        if last_key:
            query_kwargs["ExclusiveStartKey"] = last_key
        response = table.query(**query_kwargs)
        for row in response.get("Items", []):
            call_id = _call_id_from_list_row(row)
            if call_id and call_id not in seen:
                seen.add(call_id)
                call_ids.append(call_id)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
    else:
        LOGGER.warning(
            "candidate query page limit reached; remaining meetings are left for the next run",
            extra={"page_limit": MAX_QUERY_PAGES, "candidates": len(call_ids)},
        )
    return call_ids


def _batch_get_rows(call_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Read the ``c#`` meeting rows and ``uj#`` upload-job rows for the ids.

    Returned keyed by the item's ``PK`` so both row types stay distinguishable.
    """
    if not call_ids:
        return {}
    keys = []
    for call_id in call_ids:
        keys.append({"PK": f"c#{call_id}", "SK": f"c#{call_id}"})
        keys.append({"PK": f"uj#{call_id}", "SK": f"uj#{call_id}"})

    client = DYNAMODB_RESOURCE.meta.client
    rows: Dict[str, Dict[str, Any]] = {}
    for start in range(0, len(keys), BATCH_GET_LIMIT):
        request = {
            EVENT_SOURCING_TABLE: {
                "Keys": keys[start : start + BATCH_GET_LIMIT],
                # The meeting row may have been stamped moments ago by a
                # transcript segment; read it consistently so an active meeting
                # is not judged on a stale replica.
                "ConsistentRead": True,
            }
        }
        for _attempt in range(5):
            response = client.batch_get_item(RequestItems=request)
            for item in response.get("Responses", {}).get(EVENT_SOURCING_TABLE, []):
                primary_key = item.get("PK")
                if primary_key:
                    rows[primary_key] = item
            unprocessed = response.get("UnprocessedKeys", {}).get(EVENT_SOURCING_TABLE)
            if not unprocessed or not unprocessed.get("Keys"):
                break
            request = {EVENT_SOURCING_TABLE: unprocessed}
    return rows


def _last_activity(call_row: Dict[str, Any]) -> Optional[datetime]:
    return _parse_iso(call_row.get("UpdatedAt")) or _parse_iso(call_row.get("CreatedAt"))


def _is_stale(call_row: Dict[str, Any], upload_job_row: Dict[str, Any], cutoff: datetime) -> bool:
    if call_row.get("Status") not in OPEN_STATUSES:
        return False
    if upload_job_row and upload_job_row.get("Status") in ACTIVE_UPLOAD_JOB_STATUSES:
        return False
    last_activity = _last_activity(call_row)
    if last_activity is None:
        # No usable timestamp: the candidate window already established that
        # the meeting-list row is older than the cutoff, so treat it as stale.
        return True
    return last_activity < cutoff


def _emit_end_event(call_row: Dict[str, Any]) -> None:
    """Emit the END event on Kinesis for one meeting.

    ``UpdatedAt`` is intentionally omitted, matching
    ``upload_meeting_finalizer._emit_end_event``: ``updateCall.request.vtl``
    only applies a mutation whose ``UpdatedAt`` is newer than the row's, and
    letting the resolver fall back to ``$util.time.nowISO8601()`` guarantees
    that without this function having to reason about clock skew.
    """
    call_id = call_row["PK"][len("c#") :]
    payload = {
        "EventType": "END",
        "CallId": call_id,
        "CustomerPhoneNumber": call_row.get("CustomerPhoneNumber") or "Customer",
        "SystemPhoneNumber": call_row.get("SystemPhoneNumber") or "System",
        "AgentId": call_row.get("Owner") or call_row.get("AgentId") or DEFAULT_OWNER,
        "CreatedAt": _now().isoformat(),
    }
    KINESIS_CLIENT.put_record(
        StreamName=CALL_DATA_STREAM_NAME,
        PartitionKey=call_id,
        Data=json.dumps(payload, default=str).encode("utf-8"),
    )


@LOGGER.inject_lambda_context
def handler(event: Dict[str, Any], context: LambdaContext) -> Dict[str, Any]:
    """Scheduled entry point. Returns a summary of the run for the logs."""
    if INACTIVITY_TIMEOUT_MINUTES <= 0:
        LOGGER.info("meeting inactivity timeout is 0; nothing to do")
        return {"enabled": False, "candidates": 0, "ended": 0}

    now = _now()
    cutoff = now - timedelta(minutes=INACTIVITY_TIMEOUT_MINUTES)
    window_start = now - timedelta(days=LOOKBACK_DAYS)

    candidate_ids = _query_candidate_call_ids(window_start, cutoff)
    rows = _batch_get_rows(candidate_ids)

    stale_call_rows = []
    for call_id in candidate_ids:
        call_row = rows.get(f"c#{call_id}")
        if not call_row:
            continue
        if _is_stale(call_row, rows.get(f"uj#{call_id}", {}), cutoff):
            stale_call_rows.append(call_row)

    ended = 0
    failed = 0
    for call_row in stale_call_rows[:MAX_MEETINGS_PER_RUN]:
        call_id = call_row["PK"][len("c#") :]
        try:
            _emit_end_event(call_row)
        except ClientError:
            failed += 1
            LOGGER.exception("could not emit END event", extra={"call_id": call_id})
            continue
        ended += 1
        LOGGER.info(
            "ended inactive meeting",
            extra={
                "call_id": call_id,
                "status": call_row.get("Status"),
                "last_activity": call_row.get("UpdatedAt") or call_row.get("CreatedAt"),
                "inactivity_timeout_minutes": INACTIVITY_TIMEOUT_MINUTES,
            },
        )

    summary = {
        "enabled": True,
        "candidates": len(candidate_ids),
        "stale": len(stale_call_rows),
        "ended": ended,
        "failed": failed,
        "cutoff": cutoff.isoformat(),
    }
    LOGGER.info("reaper run complete", extra=summary)
    return summary
