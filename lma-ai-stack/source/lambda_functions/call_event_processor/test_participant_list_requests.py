#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""A Virtual Participant's requests to be added, published when its START creates the meeting.

What the owner depends on: each request kept on the Virtual Participant's row
from someone still not on the meeting's participant list becomes the meeting's
``participant_list_request`` notification, keyed per address as a request made
on the meeting is; only for the Virtual Participant linked to this meeting and
owned by its owner; only when the START created the meeting; and nothing here
fails the START. These drive the real dispatch with the harness from
``test_graphql_response_handling`` (the real schema, only the GraphQL transport
replaced).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
import test_graphql_response_handling as harness  # noqa: E402
from graphql import build_ast_schema, parse

cep = harness.cep
QueuedSession = harness.QueuedSession
TransportQueryError = harness.TransportQueryError
process_event = harness.process_event
requests_module = cep.participant_list_requests

OWNER = "owner@example.com"
CALL_ID = "call-1"
VP_ID = "vp-1"


@pytest.fixture(scope="module")
def schema():
    return build_ast_schema(
        parse(harness.APPSYNC_PRELUDE + harness.SCHEMA_FILE.read_text(encoding="utf-8")),
        assume_valid_sdl=True,
    )


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(cep, "METRICS", harness.RecordingEmitter(), raising=True)
    monkeypatch.setattr(cep, "TRANSCRIPT_DELETION_FUNCTION_ARN", "", raising=False)

    async def no_event(*_args, **_kwargs):
        return None

    monkeypatch.setattr(cep.meeting_event_publisher, "meeting_started", no_event)


@pytest.fixture
def published(monkeypatch) -> List[tuple]:
    sent: List[tuple] = []
    monkeypatch.setattr(
        requests_module, "_publish", lambda call_id, request: sent.append((call_id, request))
    )
    return sent


def start(**extra: Any) -> Dict[str, Any]:
    return {
        "EventType": "START",
        "CallId": CALL_ID,
        "AgentId": OWNER,
        "VirtualParticipantId": VP_ID,
        "Participants": [{"name": "Ana", "email": "ana@example.com"}],
        **extra,
    }


def vp_row(**fields: Any) -> Dict[str, Any]:
    return {
        "getVirtualParticipant": {
            "id": VP_ID,
            "Owner": OWNER,
            "CallId": CALL_ID,
            "participantListRequests": [
                {"name": "Bo", "email": "Bo@Example.com"},
                {"name": "Ana", "email": "ANA@example.com"},  # on the meeting's list since
                {"name": None, "email": "cy@example.com"},
            ],
            **fields,
        }
    }


def created() -> Dict[str, Any]:
    return {"createCall": {"CallId": CALL_ID, "Owner": OWNER}}


def test_the_start_that_creates_the_meeting_publishes_each_open_request(
    schema, published, monkeypatch
):
    session = QueuedSession(schema, [created(), vp_row()])
    process_event(session, start(), monkeypatch)
    # Published at the same time, so in no particular order.
    assert sorted(published, key=lambda p: p[1]["email"]) == [
        (CALL_ID, {"email": "bo@example.com", "name": "Bo"}),
        (CALL_ID, {"email": "cy@example.com", "name": ""}),
    ]


def test_a_start_with_an_empty_participant_list_publishes_every_request(
    schema, published, monkeypatch
):
    session = QueuedSession(schema, [created(), vp_row()])
    process_event(session, start(Participants=[]), monkeypatch)
    assert sorted(request["email"] for _, request in published) == [
        "ana@example.com",
        "bo@example.com",
        "cy@example.com",
    ]


def test_a_redelivered_start_publishes_nothing(schema, published, monkeypatch):
    already = TransportQueryError(
        "Item already exists",
        errors=[{"message": "Item already exists", "errorType": "DynamoDB"}],
    )
    session = QueuedSession(schema, [already, vp_row()])
    process_event(session, start(), monkeypatch)
    assert published == []


@pytest.mark.parametrize(
    "fields",
    [{"CallId": "another meeting"}, {"CallId": None}, {"Owner": "someone-else@example.com"}],
)
def test_a_vp_not_linked_to_this_meeting_by_its_owner_is_not_read_as_it(
    schema, published, monkeypatch, fields
):
    session = QueuedSession(schema, [created(), vp_row(**fields)])
    process_event(session, start(), monkeypatch)
    assert published == []


def test_a_start_without_a_vp_reads_no_row(schema, published, monkeypatch):
    session = QueuedSession(schema, [created()])
    message = start()
    del message["VirtualParticipantId"]
    process_event(session, message, monkeypatch)
    assert len(session.queries) == 1 and published == []


def test_an_unreadable_row_or_a_failed_publish_does_not_fail_the_start(schema, monkeypatch):
    def failing(*_args):
        raise RuntimeError("throttled")

    monkeypatch.setattr(requests_module, "_publish", failing)
    session = QueuedSession(schema, [created(), vp_row()])
    process_event(session, start(), monkeypatch)  # does not raise
    unreadable = RuntimeError("timeout")
    session = QueuedSession(schema, [created(), unreadable])
    process_event(session, start(), monkeypatch)  # does not raise


def test_the_published_notification_is_the_one_a_request_on_the_meeting_makes(monkeypatch):
    import lma_notifications  # pylint: disable=import-outside-toplevel
    from lma_consent import advance  # pylint: disable=import-outside-toplevel

    sent: List[tuple] = []
    monkeypatch.setattr(
        lma_notifications,
        "publish_meeting_notification",
        lambda call_id, **kwargs: sent.append((call_id, kwargs)) or {},
    )
    requests_module._publish(CALL_ID, {"email": "bo@example.com", "name": "Bo"})
    [(call_id, kwargs)] = sent
    assert call_id == CALL_ID
    assert kwargs.pop("timeout") == requests_module.PUBLISH_TIMEOUT_SECONDS
    assert kwargs == advance.participant_list_request_notification("bo@example.com", "Bo")
    assert kwargs["dedup_key"] == advance.request_dedup_key("Bo@Example.com")
    # A notification the publish helper accepts.
    lma_notifications.build_input(call_id, **kwargs)


def test_open_requests_keep_one_per_address_and_at_most_the_cap():
    vp = {
        "participantListRequests": [
            {"name": "Old", "email": "p@example.com"},
            *({"name": f"N{i}", "email": f"n{i}@example.com"} for i in range(25)),
            {"name": "New", "email": "P@example.com"},
            "not a request",
            {"email": None},
        ]
    }
    out = requests_module.open_requests({"Participants": None}, vp)
    assert len(out) == requests_module.MAX_PUBLISHED
    assert out[-1] == {"email": "p@example.com", "name": "New"}
    assert sum(1 for r in out if r["email"] == "p@example.com") == 1


def test_slow_publishes_do_not_hold_the_start_past_the_budget(schema, monkeypatch):
    import threading  # pylint: disable=import-outside-toplevel

    release = threading.Event()
    monkeypatch.setattr(requests_module, "_publish", lambda *_a: release.wait(5))
    monkeypatch.setattr(requests_module, "PUBLISH_BUDGET_SECONDS", 0.2)
    session = QueuedSession(schema, [created(), vp_row()])
    try:
        process_event(session, start(), monkeypatch)  # returns once the budget is spent
    finally:
        release.set()
    assert any("slow to publish" in m for m in cep.LOGGER.at_level("WARNING"))
