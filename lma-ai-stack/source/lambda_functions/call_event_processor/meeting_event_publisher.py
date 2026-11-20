#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""Meeting Started, Meeting Ended and Summary Ready, from the call event processor.

The processor is where every capture source's START and END lands and where the
end-of-meeting summary is stored, so these three events are published from its
dispatch, each only on the write that made the transition (see
docs/meeting-events.md). This module reads what an event carries from the
meeting record and hands it to ``lma_events``.

Nothing here raises. A failure to read the meeting or to publish is logged and
counted; the record that carried the transition is not failed for it, because
redelivering it would not publish the event again either -- the transition has
already been made.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Dict, Mapping, Optional

import lma_events  # pylint: disable=import-error
from appsync_utils import execute_gql_query_with_retries  # pylint: disable=import-error
from gql.dsl import DSLQuery, DSLSchema, dsl_gql

# The meeting fields an event is built from. Read rather than taken from the
# write's own answer, because no write the processor makes selects Labels,
# Audience or SummaryProfile.
_SCALAR_FIELDS = (
    "CallId",
    "Owner",
    "Status",
    "CreatedAt",
    "UpdatedAt",
    "Audience",
    "SummaryProfile",
    "SessionStartedNumber",
    # Only for when the summary was stored (Summary Ready's occurredAt).
    "CallSummaryState",
)


def _query(schema: Any, call_id: str) -> Any:
    dsl = DSLSchema(schema)
    # Only what the schema the API is running declares: during a stack update it
    # can be older than this code, and naming an undeclared field fails the read.
    declared = dsl.Call._type.fields  # pylint: disable=protected-access
    fields = [getattr(dsl.Call, name) for name in _SCALAR_FIELDS if name in declared]
    if "Labels" in declared:
        fields.append(dsl.Call.Labels.select(dsl.CallLabel.Key, dsl.CallLabel.Value))
    if "Sessions" in declared:
        fields.append(
            dsl.Call.Sessions.select(dsl.CallSession.SessionNumber, dsl.CallSession.StartedAt)
        )
    return dsl_gql(DSLQuery(dsl.Query.getCall.args(CallId=call_id).select(*fields)))


async def read_meeting(
    call_id: Optional[str], appsync_session: Any, logger: Any
) -> Optional[Dict[str, Any]]:
    """The meeting record's event fields, or None when it cannot be read."""
    schema = getattr(appsync_session.client, "schema", None)
    if not call_id or not schema:
        return None
    try:
        result = await execute_gql_query_with_retries(
            _query(schema, call_id), client_session=appsync_session, logger=logger
        )
    except Exception as error:  # pylint: disable=broad-except
        logger.error("meeting event: could not read meeting %s - %s", call_id, type(error).__name__)
        return None
    meeting = result.get("getCall") if isinstance(result, Mapping) else None
    return dict(meeting) if isinstance(meeting, Mapping) else None


def session_started_at(meeting: Mapping[str, Any], session_number: int) -> Optional[str]:
    """When the meeting's given capture session started, if the record says."""
    for session in meeting.get("Sessions") or []:
        if isinstance(session, Mapping) and session.get("SessionNumber") == session_number:
            return session.get("StartedAt")
    return None


async def _publish(detail_type: str, detail: Dict[str, Any]) -> bool:
    # In the default executor: boto3 is synchronous, and this loop is also
    # writing every other record in the batch.
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, functools.partial(lma_events.publish, detail_type, detail)
    )


#: How long the record waits for the summary-ready notification. The request
#: itself is given NOTIFICATION_TIMEOUT_SECONDS; this bounds the wait even if it
#: is slower, and the notification finishes, or not, on its own after that.
NOTIFICATION_WAIT_SECONDS = 4.0
NOTIFICATION_TIMEOUT_SECONDS = 3.0


async def _notify_summary_ready(call_id: Optional[str], logger: Any) -> None:
    """The in-app notification, never holding the record up for long or failing it."""
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                functools.partial(
                    lma_events.notify_summary_ready, call_id, timeout=NOTIFICATION_TIMEOUT_SECONDS
                ),
            ),
            timeout=NOTIFICATION_WAIT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("meeting event: the summary-ready notification is slow; not waiting for it")
    except Exception as error:  # pylint: disable=broad-except
        logger.warning(
            "meeting event: summary-ready notification not published - %s", type(error).__name__
        )


def _known(meeting: Mapping[str, Any], record: Mapping[str, Any]) -> Dict[str, Any]:
    """The meeting's event fields: what the record carries, overlaid with the read.

    The read of the meeting comes straight after the write that made the
    transition, and getCall is not a strongly consistent read, so it can miss
    that write or the meeting altogether. The record the processor is handling
    is the fallback for every field, and no event time is taken from the read
    where the record or the write's own answer has one.
    """
    known: Dict[str, Any] = {
        "CallId": record.get("CallId"),
        "Owner": record.get("Owner"),
        "Labels": record.get("Labels"),
        "Audience": record.get("Audience"),
        "SummaryProfile": record.get("SummaryProfile"),
    }
    known.update({key: value for key, value in meeting.items() if value not in (None, "", [])})
    return known


async def meeting_started(
    message: Mapping[str, Any], session_number: int, appsync_session: Any, logger: Any
) -> bool:
    """Publish Meeting Started for a created meeting or a reopened session.

    ``occurredAt`` is the meeting's CreatedAt, which no later write changes, or
    for a later session that session's StartedAt; where the read does not have
    them, the START record's own CreatedAt, which is the same on a redelivery.
    """
    if not lma_events.enabled():
        return False
    try:
        meeting = await read_meeting(message.get("CallId"), appsync_session, logger) or {}
        occurred_at = (
            meeting.get("CreatedAt")
            if session_number <= 1
            else session_started_at(meeting, session_number)
        ) or message.get("CreatedAt")
        detail = lma_events.meeting_detail(
            _known(meeting, message),
            session_number=session_number,
            occurred_at=occurred_at or None,
            virtual_participant_id=message.get("VirtualParticipantId"),
        )
        return await _publish(lma_events.MEETING_STARTED, detail)
    except Exception as error:  # pylint: disable=broad-except
        logger.error("meeting event: Meeting Started not published - %s", type(error).__name__)
        return False


def _written(result: Any, field: str) -> Dict[str, Any]:
    """The meeting as a mutation answered it, or {}."""
    if isinstance(result, Mapping) and isinstance(result.get(field), Mapping):
        return dict(result[field])
    return {}


async def meeting_ended(
    message: Mapping[str, Any],
    session_number: int,
    appsync_session: Any,
    logger: Any,
    written: Any = None,
) -> bool:
    """Publish Meeting Ended once the END's status write has been stored.

    ``written`` is updateCallStatus's answer: the meeting as that write left it,
    so ``occurredAt`` is that write's UpdatedAt rather than whatever the read
    after it returns.
    """
    if not lma_events.enabled():
        return False
    try:
        meeting = await read_meeting(message.get("CallId"), appsync_session, logger) or {}
        stored = _written(written, "updateCallStatus")
        detail = lma_events.meeting_detail(
            _known({**meeting, **{k: v for k, v in stored.items() if v is not None}}, message),
            status="ENDED",
            session_number=session_number,
            occurred_at=stored.get("UpdatedAt") or meeting.get("UpdatedAt"),
            virtual_participant_id=message.get("VirtualParticipantId"),
        )
        return await _publish(lma_events.MEETING_ENDED, detail)
    except Exception as error:  # pylint: disable=broad-except
        logger.error("meeting event: Meeting Ended not published - %s", type(error).__name__)
        return False


async def summary_ready(message: Mapping[str, Any], appsync_session: Any, logger: Any) -> bool:
    """Publish Summary Ready, and its in-app notification, for a stored summary.

    ``occurredAt`` is the record's SummaryCompletedAt, which the stored summary
    state was written with and which a redelivery repeats. Only a record from
    an orchestrator that does not stamp it falls back to the state as read.
    """
    call_id = message.get("CallId")
    published = False
    try:
        if lma_events.enabled():
            meeting = _known(await read_meeting(call_id, appsync_session, logger) or {}, message)
            completed_at = lma_events.iso_utc(message.get("SummaryCompletedAt"))
            detail = (
                lma_events.meeting_detail(meeting, occurred_at=completed_at)
                if completed_at
                else lma_events.summary_detail(meeting)
            )
            published = await _publish(lma_events.SUMMARY_READY, detail)
    except Exception as error:  # pylint: disable=broad-except
        logger.error("meeting event: Summary Ready not published - %s", type(error).__name__)
    await _notify_summary_ready(call_id, logger)
    return published
