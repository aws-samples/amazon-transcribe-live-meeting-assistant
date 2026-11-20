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


async def _notify_summary_ready(call_id: Optional[str]) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, functools.partial(lma_events.notify_summary_ready, call_id)
    )


def _fallback(message: Mapping[str, Any]) -> Dict[str, Any]:
    """What the record itself says, for when the meeting could not be read."""
    return {"CallId": message.get("CallId"), "Owner": message.get("Owner")}


async def meeting_started(
    message: Mapping[str, Any], session_number: int, appsync_session: Any, logger: Any
) -> bool:
    """Publish Meeting Started for a created meeting or a reopened session."""
    if not lma_events.enabled():
        return False
    try:
        meeting = await read_meeting(message.get("CallId"), appsync_session, logger)
        meeting = meeting or _fallback(message)
        occurred_at = (
            meeting.get("CreatedAt")
            if session_number <= 1
            else session_started_at(meeting, session_number) or meeting.get("UpdatedAt")
        )
        detail = lma_events.meeting_detail(
            meeting,
            session_number=session_number,
            occurred_at=occurred_at,
            virtual_participant_id=message.get("VirtualParticipantId"),
        )
        return await _publish(lma_events.MEETING_STARTED, detail)
    except Exception as error:  # pylint: disable=broad-except
        logger.error("meeting event: Meeting Started not published - %s", type(error).__name__)
        return False


async def meeting_ended(
    message: Mapping[str, Any], session_number: int, appsync_session: Any, logger: Any
) -> bool:
    """Publish Meeting Ended once the END's status write has been stored."""
    if not lma_events.enabled():
        return False
    try:
        meeting = await read_meeting(message.get("CallId"), appsync_session, logger)
        meeting = meeting or _fallback(message)
        detail = lma_events.meeting_detail(
            meeting,
            status="ENDED",
            session_number=session_number,
            virtual_participant_id=message.get("VirtualParticipantId"),
        )
        return await _publish(lma_events.MEETING_ENDED, detail)
    except Exception as error:  # pylint: disable=broad-except
        logger.error("meeting event: Meeting Ended not published - %s", type(error).__name__)
        return False


async def summary_ready(message: Mapping[str, Any], appsync_session: Any, logger: Any) -> bool:
    """Publish Summary Ready, and its in-app notification, for a stored summary."""
    call_id = message.get("CallId")
    published = False
    try:
        if lma_events.enabled():
            meeting = await read_meeting(call_id, appsync_session, logger)
            meeting = meeting or _fallback(message)
            published = await _publish(lma_events.SUMMARY_READY, lma_events.summary_detail(meeting))
        await _notify_summary_ready(call_id)
    except Exception as error:  # pylint: disable=broad-except
        logger.error("meeting event: Summary Ready not published - %s", type(error).__name__)
    return published
