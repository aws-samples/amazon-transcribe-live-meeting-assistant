# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Meeting lifecycle events on a live stack (docs/meeting-events.md).

One probe run (meeting_events_probe.py) puts a temporary rule and queue on the
stack's meeting event bus, drives one synthetic meeting and one synthetic
Virtual Participant record through every transition that publishes, and reads
what the rule delivered. Each test asserts one property of what it observed.

Skips when the stack has no MeetingEventBusName output (EnableMeetingEvents is
false). Self-cleaning: the rule, its target, the queue, the meeting's rows and
the Virtual Participant row are deleted afterwards.
"""

from __future__ import annotations

import json

import pytest

ALLOWED_FIELDS = {
    "version",
    "meetingId",
    "virtualParticipantId",
    "owner",
    "labels",
    "audience",
    "status",
    "summaryProfile",
    "sessionNumber",
    "vpStatus",
    "manualActionType",
    "occurredAt",
}


@pytest.fixture(scope="module")
def observed(client):
    from meeting_events_probe import bus_name, cleanup, run_probe

    if not bus_name(client.stack_name, region=client.region):
        pytest.skip(
            "no MeetingEventBusName output: EnableMeetingEvents is false on this stack"
        )
    result = run_probe(client.stack_name, region=client.region)
    yield result
    cleanup(client.stack_name, result, region=client.region)


def _of_type(observed, detail_type):
    return [
        event for event in observed["events"] if event["detail-type"] == detail_type
    ]


def _unique(events, *keys):
    """At least once: the same event may arrive twice, and counts once."""
    return {
        tuple(json.dumps(event["detail"].get(key)) for key in keys): event
        for event in events
    }


@pytest.mark.parametrize(
    "detail_type",
    [
        "Meeting Started",
        "Meeting Ended",
        "Summary Ready",
        "Virtual Participant Status Changed",
    ],
)
def test_a_rule_on_the_bus_receives_the_event(observed, detail_type) -> None:
    assert _of_type(observed, detail_type), (
        f"no {detail_type} reached the rule on {observed['bus']} for meeting "
        f"{observed['call_id']}; saw {[e['detail-type'] for e in observed['events']]}"
    )


def test_every_event_has_source_lma_and_version_1(observed) -> None:
    for event in observed["events"]:
        assert event["source"] == "lma"
        assert event["detail"]["version"] == 1


def test_no_event_carries_a_field_outside_the_contract(observed) -> None:
    for event in observed["events"]:
        assert set(event["detail"]) <= ALLOWED_FIELDS, event["detail"]


def test_no_event_carries_meeting_content(observed) -> None:
    from meeting_events_probe import SUMMARY_TEXT

    for event in observed["events"]:
        text = json.dumps(event["detail"])
        for fragment in (SUMMARY_TEXT, "Integration meeting name", "lobby", "puzzle"):
            assert fragment not in text, event["detail"]


def test_meeting_started_names_the_meeting_its_owner_labels_and_session(
    observed,
) -> None:
    (started,) = _unique(_of_type(observed, "Meeting Started"), "occurredAt").values()
    detail = started["detail"]
    assert detail["meetingId"] == observed["call_id"]
    assert detail["owner"] == "integ-test@lma.aws"
    assert detail["labels"] == {"integ-events": "probe"}
    assert detail["audience"] == "internal"
    assert detail["sessionNumber"] == 1


def test_meeting_ended_carries_the_ended_status(observed) -> None:
    (ended,) = _unique(_of_type(observed, "Meeting Ended"), "occurredAt").values()
    assert ended["detail"]["status"] == "ENDED"
    assert ended["detail"]["sessionNumber"] == 1


def test_summary_ready_is_published_once_for_the_stored_summary(observed) -> None:
    assert len(_unique(_of_type(observed, "Summary Ready"), "occurredAt")) == 1


def test_each_vp_status_change_is_one_event_and_other_writes_are_none(observed) -> None:
    vp_events = _unique(
        _of_type(observed, "Virtual Participant Status Changed"),
        "virtualParticipantId",
        "vpStatus",
        "occurredAt",
    ).values()
    statuses = sorted(event["detail"]["vpStatus"] for event in vp_events)
    assert statuses == ["INITIALIZING", "JOINING", "MANUAL_ACTION_REQUIRED"], statuses


def test_a_person_needed_names_the_kind_of_action(observed) -> None:
    (prompt,) = [
        event
        for event in _of_type(observed, "Virtual Participant Status Changed")
        if event["detail"]["vpStatus"] == "MANUAL_ACTION_REQUIRED"
    ][:1]
    detail = prompt["detail"]
    assert detail["manualActionType"] == "CAPTCHA"
    assert detail["virtualParticipantId"] == observed["vp_id"]
    assert detail["meetingId"] == observed["call_id"]
