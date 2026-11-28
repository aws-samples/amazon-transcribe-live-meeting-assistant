#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""A Virtual Participant's requests to be added, published to the meeting it creates.

Someone who opens a Virtual Participant's consent link before its meeting
exists, and is not on its participant list, can ask to be added; the request is
kept on the Virtual Participant's row (``participantListRequests``), which its
page shows. When the Virtual Participant's START creates the meeting, each
request from someone still not on the meeting's participant list is published
to the meeting as the ``participant_list_request`` notification a request made
on the meeting itself is, with the same key per address, so the meeting page
and the notifications show them and a later request from the same person
updates the one notification. See
docs/meeting-consent.md#asking-to-be-added-before-the-meeting-exists.

The row is read through getVirtualParticipant, whose resolver reads
consistently, after the meeting is created; the request side reads the row and
the meeting consistently after keeping a request. So a request kept while the
meeting is being created is seen by this read or by that one.

The meeting page could instead read the Virtual Participant's requests; a
notification is used because it reaches everyone who can see the meeting, in
the notifications drawer as well as on the page, and needs nothing new there.

Nothing here raises. A failure to read the row or to publish is logged; the
START is not failed for it, because its meeting is already created and a
redelivered START does not reach this again. The requests stay on the Virtual
Participant's page either way.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any, Dict, List, Mapping

from appsync_utils import execute_gql_query_with_retries  # pylint: disable=import-error
from gql.dsl import DSLQuery, DSLSchema, dsl_gql

#: The most requests published for one meeting (the row keeps at most 20).
MAX_PUBLISHED = 20
#: Seconds each publish may take.
PUBLISH_TIMEOUT_SECONDS = 5.0
#: Seconds the record waits for all of them, published at the same time.
PUBLISH_BUDGET_SECONDS = 8.0


def _query(schema: Any, vp_id: str) -> Any:
    dsl = DSLSchema(schema)
    vp = dsl.VirtualParticipant
    return dsl_gql(
        DSLQuery(
            dsl.Query.getVirtualParticipant.args(id=vp_id).select(
                vp.id,
                vp.Owner,
                vp.CallId,
                vp.participantListRequests.select(
                    dsl.ParticipantListRequest.name, dsl.ParticipantListRequest.email
                ),
            )
        )
    )


def _declares_requests(schema: Any) -> bool:
    # During a stack update the running API's schema can be older than this
    # code, and naming an undeclared field fails the read.
    vp_type = schema.get_type("VirtualParticipant")
    return vp_type is not None and "participantListRequests" in vp_type.fields


def open_requests(message: Mapping[str, Any], vp: Mapping[str, Any]) -> List[Dict[str, str]]:
    """The row's requests from people not on the meeting's list, once per address."""
    listed = {
        str(p.get("email") or "").strip().casefold()
        for p in message.get("Participants") or []
        if isinstance(p, Mapping)
    }
    out: Dict[str, Dict[str, str]] = {}
    for request in vp.get("participantListRequests") or []:
        if not isinstance(request, Mapping) or not isinstance(request.get("email"), str):
            continue
        email = request["email"].strip().casefold()
        if email and email not in listed:
            # The newest request from an address is the one kept (last wins).
            out.pop(email, None)
            out[email] = {"email": email, "name": str(request.get("name") or "")}
    return list(out.values())[-MAX_PUBLISHED:]


def _publish(call_id: str, request: Mapping[str, str]) -> None:
    # pylint: disable=import-error,import-outside-toplevel
    from lma_consent import advance
    from lma_notifications import publish_meeting_notification

    publish_meeting_notification(
        call_id,
        **advance.participant_list_request_notification(request["email"], request["name"]),
        timeout=PUBLISH_TIMEOUT_SECONDS,
    )


async def publish_vp_requests(message: Mapping[str, Any], appsync_session: Any, logger: Any) -> int:
    """Publish the requests kept on the START's Virtual Participant; how many were published.

    Only for a Virtual Participant linked to this meeting and owned by the
    meeting's owner, as the advance consent resolver reads a Virtual
    Participant as its meeting only then.
    """
    vp_id = message.get("VirtualParticipantId")
    call_id = message.get("CallId")
    schema = getattr(appsync_session.client, "schema", None)
    if not vp_id or not call_id or not schema or not _declares_requests(schema):
        return 0
    try:
        result = await execute_gql_query_with_retries(
            _query(schema, str(vp_id)), client_session=appsync_session, logger=logger
        )
    except Exception as error:  # pylint: disable=broad-except
        logger.warning("START: could not read the Virtual Participant's requests - %s", error)
        return 0
    vp = (result or {}).get("getVirtualParticipant") if isinstance(result, Mapping) else None
    if not isinstance(vp, Mapping) or vp.get("CallId") != call_id:
        return 0
    if (vp.get("Owner") or "") != (message.get("Owner") or ""):
        return 0
    requests = open_requests(message, vp)
    if not requests:
        return 0
    loop = asyncio.get_running_loop()
    # At the same time, in the default executor: each signed request is
    # synchronous, and this loop is also writing every other record in the batch.
    publishes = [
        loop.run_in_executor(None, functools.partial(_publish, call_id, request))
        for request in requests
    ]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*publishes, return_exceptions=True), timeout=PUBLISH_BUDGET_SECONDS
        )
    except asyncio.TimeoutError:
        logger.warning(
            "START: the requests to be added are slow to publish; some may not have been "
            "published, and the rest are not waited for"
        )
        return 0
    failed = [r for r in results if isinstance(r, BaseException)]
    for error in failed:
        logger.warning("START: a request to be added was not published - %s", error)
    published = len(results) - len(failed)
    if published:
        logger.info("START: published %d requests to be added to the meeting", published)
    return published
