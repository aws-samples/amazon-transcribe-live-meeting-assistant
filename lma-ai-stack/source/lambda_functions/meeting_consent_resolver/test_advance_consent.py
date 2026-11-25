#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Consent given in advance, as a meeting counts it.

When the Virtual Participant lists who is present, an attendee with no reply
whose display name matches one listed person with a consent given in advance
that covers the meeting is recorded as consented, source ``pre_meeting``, with
the basis of the match; in opt-in that lets capture start once everyone
present is covered. An objection wins over it, and a withdrawal during the
meeting is recorded as an objection. No AWS.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_index as base  # noqa: E402  pylint: disable=wrong-import-position

index = base.index
SER = TypeSerializer()
DES = TypeDeserializer()
NOW = datetime.now(timezone.utc)
CALL_ID = base.CALL_ID
OWNER = base.OWNER


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class AdvanceDynamo(base.FakeDynamo):
    """The meeting rows, and the consents given in advance under pmc#<address>."""

    def __init__(self) -> None:
        super().__init__()
        self.consents: Dict[str, List[Dict[str, Any]]] = {}
        self.applied: Dict[tuple, set] = {}
        self.queries: List[str] = []

    def give(self, email: str, **fields: Any) -> Dict[str, Any]:
        consent_id = fields.pop("ConsentId", f"20261125120000-{len(self.consents):012x}")
        item = {
            "PK": f"pmc#{email}",
            "SK": f"ac#{consent_id}",
            "ConsentId": consent_id,
            "Email": email,
            "Scope": "meeting",
            "CallId": CALL_ID,
            "Organizer": OWNER,
            "GrantedAt": iso(NOW - timedelta(days=1)),
            "ValidUntil": iso(NOW + timedelta(days=30)),
            "NoticeHash": "a" * 64,
            "NoticeVersion": "abcdefabcdef",
            "Source": "pre_meeting",
        }
        item.update(fields)
        self.consents.setdefault(item["PK"], []).append(item)
        return item

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues, **_kw):  # noqa: N803
        pk = ExpressionAttributeValues[":pk"]["S"]
        self.queries.append(pk)
        return {
            "Items": [
                {k: SER.serialize(v) for k, v in item.items()} for item in self.consents.get(pk, [])
            ]
        }

    def update_item(self, TableName, Key, UpdateExpression, ConditionExpression, **kwargs):  # noqa: N803
        if Key["PK"]["S"].startswith("pmc#"):
            key = (Key["PK"]["S"], Key["SK"]["S"])
            self.applied.setdefault(key, set()).update(
                kwargs["ExpressionAttributeValues"][":c"]["SS"]
            )
            return {}
        return super().update_item(TableName, Key, UpdateExpression, ConditionExpression, **kwargs)


@pytest.fixture
def dynamo(monkeypatch):
    fake = AdvanceDynamo()
    fake.put_meeting(
        Participants=[
            {"name": "Ana Silva", "email": "ana@example.com"},
            {"name": "Bo Chen", "email": "bo@example.com"},
            {"name": "Cy Diaz", "email": "cy@example.com"},
        ],
        Labels=[{"Key": "engagement", "Value": "acme"}],
        Status="TRANSCRIBING",
    )
    monkeypatch.setattr(index, "DYNAMODB", fake)
    monkeypatch.setattr(index, "S3", base.FakeS3())
    monkeypatch.setattr(index, "_STORE", base.FakeStore())
    return fake


def start(mode="opt_in", rule="each_participant"):
    base.call("startMeetingConsent", base.BACKEND, Mode=mode, Rule=rule)
    base.call("recordMeetingConsentNotice", base.BACKEND, Channel="chat")


def present(*names, vp_id=None):
    extra = {"VirtualParticipantId": vp_id} if vp_id else {}
    return base.call(
        "setMeetingConsentParticipants", base.BACKEND, Participants=list(names), **extra
    )["Consent"]


def test_a_covered_attendee_is_recorded_as_consented_with_the_basis(dynamo) -> None:
    given = dynamo.give("bo@example.com")
    start()
    record = present("Bo Chen")
    [event] = record["Events"]
    assert (event["ParticipantName"], event["Decision"], event["Source"]) == (
        "Bo Chen",
        "consent",
        "pre_meeting",
    )
    assert event["PreMeeting"]["MatchBasis"] == "display_name"
    assert event["PreMeeting"]["ConsentId"] == given["ConsentId"]
    assert event["PreMeeting"]["NoticeHash"] == "a" * 64
    assert event["PreMeeting"]["ListedName"] == "Bo Chen"
    assert "Email" not in str(event) and "@" not in str(event["PreMeeting"])
    # The consent notes the meeting, for a withdrawal to reach it.
    assert dynamo.applied[(given["PK"], given["SK"])] == {CALL_ID}


def test_opt_in_starts_once_everyone_present_is_covered(dynamo) -> None:
    dynamo.give("bo@example.com")
    dynamo.give(
        "cy@example.com", Scope="organizer", CallId=None, LabelKey="engagement", LabelValue="acme"
    )
    start()
    record = present("Bo Chen", "Cy Diaz")
    assert record["Status"] == "granted" and record["CaptureAllowed"] is True
    record = present("Bo Chen", "Cy Diaz", "Ana Silva")
    assert record["CaptureAllowed"] is False, "Ana has no consent given in advance and is asked"
    base.call(
        "recordMeetingConsentEvent",
        base.BACKEND,
        ParticipantName="Ana Silva",
        Decision="consent",
        Source="chat",
    )
    assert dynamo.consent()["CaptureAllowed"] is True


def test_an_objection_wins_over_a_consent_given_in_advance(dynamo) -> None:
    dynamo.give("bo@example.com")
    start()
    present("Bo Chen")
    base.call(
        "recordMeetingConsentEvent",
        base.BACKEND,
        ParticipantName="Bo Chen",
        Decision="objection",
        Source="chat",
    )
    record = present("Bo Chen", "Cy Diaz")
    assert record["Status"] == "objected" and record["CaptureAllowed"] is False
    # Listing them again records nothing more for Bo, who has replied.
    assert [e["Source"] for e in record["Events"]] == ["pre_meeting", "chat"]


def test_an_attendee_who_replied_in_the_meeting_is_not_looked_up(dynamo) -> None:
    dynamo.give("bo@example.com")
    start()
    base.call(
        "recordMeetingConsentEvent",
        base.BACKEND,
        ParticipantName="Bo Chen",
        Decision="objection",
        Source="chat",
    )
    record = present("Bo Chen")
    assert [e["Source"] for e in record["Events"]] == ["chat"]
    assert dynamo.queries == []


@pytest.mark.parametrize(
    "fields",
    [
        {"WithdrawnAt": iso(NOW - timedelta(hours=1))},
        {"ValidUntil": iso(NOW - timedelta(hours=1))},
        {"CallId": "another meeting"},
        {"Scope": "organizer", "CallId": None, "Organizer": "someone@example.com"},
        {"Scope": "organizer", "CallId": None, "LabelKey": "engagement", "LabelValue": "other"},
    ],
)
def test_a_consent_that_does_not_cover_the_meeting_is_not_counted(dynamo, fields) -> None:
    dynamo.give("bo@example.com", **fields)
    start()
    record = present("Bo Chen")
    assert record["Events"] == [] and record["CaptureAllowed"] is False


def test_a_consent_given_through_the_vp_link_counts_for_the_meeting_it_created(dynamo) -> None:
    dynamo.give("bo@example.com", CallId=None, VirtualParticipantId="vp-1")
    start()
    assert present("Bo Chen")["Events"] == []
    assert present("Bo Chen", "Cy Diaz", vp_id="vp-1")["Events"][0]["Source"] == "pre_meeting"


def test_an_attendee_is_matched_only_by_one_listed_name(dynamo) -> None:
    dynamo.give("bo@example.com")
    dynamo.put_meeting(
        Participants=[
            {"name": "Bo Chen", "email": "bo@example.com"},
            {"name": "bo  chen", "email": "other-bo@example.com"},
        ],
        Status="TRANSCRIBING",
    )
    start()
    assert present("Bo Chen")["Events"] == []
    assert dynamo.queries == []


def test_a_signed_in_user_listing_participants_counts_nothing_given_in_advance(dynamo) -> None:
    dynamo.give("bo@example.com")
    start()
    base.call("setMeetingConsentParticipants", base.user(OWNER), Participants=["Bo Chen"])
    assert dynamo.consent()["Events"] == []


def test_nothing_is_counted_when_the_deployment_turns_it_off(dynamo, monkeypatch) -> None:
    dynamo.give("bo@example.com")
    monkeypatch.setattr(index, "_STORE", base.FakeStore({"advanceConsentEnabled": False}))
    start()
    assert present("Bo Chen")["Events"] == []


def test_mode_off_counts_nothing(dynamo) -> None:
    dynamo.give("bo@example.com")
    base.call("startMeetingConsent", base.BACKEND, Mode="off")
    assert present("Bo Chen")["Events"] == []


def test_a_withdrawal_during_the_meeting_is_recorded_as_an_objection(dynamo) -> None:
    given = dynamo.give("bo@example.com")
    start(mode="notify")
    present("Bo Chen")
    answer = index.handler(
        {"advanceConsentWithdrawn": {"CallId": CALL_ID, "ConsentId": given["ConsentId"]}}, None
    )
    assert answer == {"CallId": CALL_ID, "Recorded": 1}
    record = dynamo.consent()
    assert record["Status"] == "objected" and record["CaptureAllowed"] is False
    last = record["Events"][-1]
    assert (last["ParticipantName"], last["Decision"], last["Source"]) == (
        "Bo Chen",
        "objection",
        "pre_meeting",
    )
    assert last["PreMeeting"]["Withdrawn"] is True


def test_a_withdrawal_leaves_an_attendee_who_replied_since_and_an_ended_meeting(dynamo) -> None:
    given = dynamo.give("bo@example.com")
    start()
    present("Bo Chen")
    base.call(
        "recordMeetingConsentEvent",
        base.BACKEND,
        ParticipantName="Bo Chen",
        Decision="consent",
        Source="chat",
    )
    event = {"advanceConsentWithdrawn": {"CallId": CALL_ID, "ConsentId": given["ConsentId"]}}
    assert index.handler(event, None)["Recorded"] == 0
    assert dynamo.consent()["CaptureAllowed"] is True
    item = dynamo.items[CALL_ID]
    item["Status"] = SER.serialize("ENDED")
    assert index.handler(event, None)["Recorded"] == 0


def test_the_withdrawal_entry_point_is_not_reached_through_appsync(dynamo) -> None:
    with pytest.raises(index.BadRequestError):
        index.handler(
            {
                "info": {"fieldName": "advanceConsentWithdrawn"},
                "arguments": {"input": {"CallId": CALL_ID}},
                "identity": base.user(OWNER),
                "advanceConsentWithdrawn": {"CallId": CALL_ID, "ConsentId": "x"},
            },
            None,
        )
