# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""reopenMeeting (index.py): who may continue a meeting, and the session it is given.

The event sourcing table is an in-memory fake with its conditions evaluated, so
the resolver runs end to end without AWS.
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("EVENT_SOURCING_TABLE", "events")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import index  # noqa: E402

CALL_ID = "Weekly sync - 2026-11-18 09:00"
OWNER = {
    "username": "owner-id",
    "sub": "sub-owner",
    "groups": [],
    "claims": {"email": "owner@example.com", "sub": "sub-owner"},
}
ADMIN = {"username": "admin-id", "sub": "sub-admin", "groups": ["Admin"], "claims": {"sub": "x"}}
SHARED = {"username": "bob-id", "sub": "sub-bob", "groups": [], "claims": {"sub": "sub-bob"}}


def ended_meeting(**overrides: Any) -> Dict[str, Any]:
    meeting = {
        "PK": f"c#{CALL_ID}",
        "SK": f"c#{CALL_ID}",
        "Owner": "owner-id",
        "SharedWith": "bob-id",
        "Status": "ENDED",
        "TotalConversationDurationMillis": Decimal("1800250"),
    }
    meeting.update(overrides)
    return meeting


class Table:
    """The meeting record and its transcript; the session condition evaluated."""

    def __init__(self, meeting: Dict[str, Any] | None, end_times: List[Any]) -> None:
        self.meeting = dict(meeting) if meeting is not None else None
        self.end_times = end_times
        self.updates: List[Dict[str, Any]] = []
        # Applied between the read and the write, to stand for another writer.
        self.before_write: Dict[str, Any] = {}

    def get_item(self, Key: Dict[str, str], **_: Any) -> Dict[str, Any]:
        assert Key == {"PK": f"c#{CALL_ID}", "SK": f"c#{CALL_ID}"}
        return {"Item": dict(self.meeting)} if self.meeting is not None else {}

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        assert kwargs["ExpressionAttributeValues"] == {":pk": f"trs#{CALL_ID}"}
        return {"Items": [{"EndTime": end} for end in self.end_times]}

    def update_item(self, **kwargs: Any) -> Dict[str, Any]:
        self.updates.append(kwargs)
        if self.meeting is not None:
            self.meeting.update(self.before_write)
        values = kwargs["ExpressionAttributeValues"]
        if (
            self.meeting is None
            or self.meeting.get("TranscriptDeletedAt")
            or int(self.meeting.get("SessionNumber", 0)) >= values[":session"]
        ):
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}},
                "UpdateItem",
            )
        self.meeting.update(
            SessionNumber=values[":session"],
            SessionOffsetSeconds=values[":offset"],
            ReopenedAt=values[":now"],
            ReopenedBy=values[":by"],
            ReopenedVia=values[":via"],
        )
        return {}


@pytest.fixture
def table(monkeypatch):
    def build(meeting: Any = "ended", end_times: List[Any] | None = None) -> Table:
        fake = Table(
            ended_meeting() if meeting == "ended" else meeting,
            [Decimal("1700.5"), Decimal("1805.2")] if end_times is None else end_times,
        )
        monkeypatch.setattr(index, "_TABLE", fake)
        return fake

    return build


def call(identity: Dict[str, Any], call_id: Any = CALL_ID) -> Dict[str, Any]:
    return index.handler(
        {
            "info": {"fieldName": "reopenMeeting"},
            "arguments": {"input": {"CallId": call_id}},
            "identity": identity,
        },
        None,
    )


def test_the_owner_reopens_an_ended_meeting_for_its_next_session(table) -> None:
    fake = table()
    answer = call(OWNER)
    assert answer["CallId"] == CALL_ID
    assert answer["SessionNumber"] == 2
    # After the latest end of anything in the transcript, plus the gap.
    assert answer["SessionOffsetSeconds"] == 1807
    assert fake.meeting["SessionNumber"] == 2
    assert fake.meeting["SessionOffsetSeconds"] == Decimal(1807)
    assert fake.meeting["ReopenedBy"] == "owner@example.com"
    # Nothing else on the meeting changes: it stays ENDED until the session starts.
    assert fake.meeting["Status"] == "ENDED"


def test_an_admin_may_reopen_another_users_meeting(table) -> None:
    table()
    assert call(ADMIN)["SessionNumber"] == 2


def test_a_user_the_meeting_is_shared_with_may_not_reopen_it(table) -> None:
    fake = table()
    assert call(SHARED)["errorType"] == "Unauthorized"
    assert fake.updates == []


def test_a_caller_without_a_user_pool_identity_may_not_reopen_a_meeting(table) -> None:
    fake = table()
    backend = {"userArn": "assumed-role/LMA-Role/session", "username": "AROAEXAMPLE:session"}
    assert call(backend)["errorType"] == "Unauthorized"
    assert fake.updates == []


def test_a_meeting_id_that_names_no_meeting_is_answered_as_not_found(table) -> None:
    fake = table(meeting=None)
    assert call(OWNER)["errorType"] == "MeetingNotFound"
    assert fake.updates == []


def test_a_meeting_whose_transcript_was_deleted_is_not_reopened(table) -> None:
    fake = table(ended_meeting(TranscriptDeletedAt="2026-11-18T11:00:00Z"))
    assert call(OWNER)["errorType"] == "TranscriptDeleted"
    assert fake.updates == []


@pytest.mark.parametrize("status", ["STARTED", "TRANSCRIBING"])
def test_a_meeting_still_being_captured_is_not_reopened(table, status) -> None:
    fake = table(ended_meeting(Status=status))
    assert call(OWNER)["errorType"] == "MeetingInProgress"
    assert fake.updates == []


def test_each_reopen_allows_a_later_session_than_any_before(table) -> None:
    fake = table(ended_meeting(SessionNumber=3, SessionStartedNumber=2))
    assert call(OWNER)["SessionNumber"] == 4
    assert fake.meeting["SessionNumber"] == 4


def test_a_meeting_with_nothing_on_its_timeline_starts_the_session_at_zero(table) -> None:
    table(ended_meeting(TotalConversationDurationMillis=0), end_times=[])
    assert call(OWNER)["SessionOffsetSeconds"] == 0


def test_a_transcript_deleted_between_the_read_and_the_write_refuses_the_reopen(table) -> None:
    fake = table()
    fake.before_write = {"TranscriptDeletedAt": "2026-11-18T11:00:00Z"}
    assert call(OWNER)["errorType"] == "MeetingChanged"


@pytest.mark.parametrize("call_id", [None, "", "   ", 42, "x" * 1025])
def test_a_missing_or_unusable_meeting_id_is_a_bad_request(table, call_id) -> None:
    fake = table()
    assert call(OWNER, call_id)["errorType"] == "BadRequest"
    assert fake.updates == []


def test_the_meeting_id_is_trimmed(table) -> None:
    table()
    assert call(OWNER, f"  {CALL_ID}\n")["CallId"] == CALL_ID


def test_only_the_reopen_field_is_answered(table) -> None:
    table()
    answer = index.handler({"info": {"fieldName": "getCall"}, "identity": OWNER}, None)
    assert answer["errorType"] == "BadRequest"


def _minutes_ago(minutes: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def test_a_session_allowed_moments_ago_that_has_not_started_is_not_overtaken(table) -> None:
    # A Virtual Participant rejoin, still joining: session 2 allowed, not started.
    fake = table(
        ended_meeting(
            SessionNumber=2,
            ReopenedAt=_minutes_ago(2),
            ReopenedBy="owner@example.com",
            ReopenedVia="rejoinVirtualParticipant",
        )
    )
    assert call(OWNER)["errorType"] == "MeetingInProgress"
    assert fake.updates == []


def test_a_session_allowed_long_ago_that_never_started_is_taken_as_abandoned(table) -> None:
    table(ended_meeting(SessionNumber=2, ReopenedAt=_minutes_ago(16)))
    assert call(OWNER)["SessionNumber"] == 3


def test_a_session_that_started_does_not_hold_the_next_reopen(table) -> None:
    table(ended_meeting(SessionNumber=2, SessionStartedNumber=2, ReopenedAt=_minutes_ago(1)))
    assert call(OWNER)["SessionNumber"] == 3


def test_the_callers_own_pending_reopen_is_answered_again_not_held_off(table) -> None:
    fake = table()
    first = call(OWNER)
    # Its START never got through (refused, connection failed, app closed): ask again.
    again = call(OWNER)
    assert again["SessionNumber"] == first["SessionNumber"] == 2
    assert again["SessionOffsetSeconds"] == first["SessionOffsetSeconds"]
    assert len(fake.updates) == 1, "the same session is answered, nothing new is allowed"


def test_another_users_pending_reopen_is_not_handed_out(table) -> None:
    table()
    assert call(OWNER)["SessionNumber"] == 2
    assert call(ADMIN)["errorType"] == "MeetingInProgress"


def test_a_reopen_time_slightly_ahead_of_this_clock_still_holds_the_meeting(table) -> None:
    fake = table(
        ended_meeting(
            SessionNumber=2, ReopenedAt=_minutes_ago(-1), ReopenedVia="rejoinVirtualParticipant"
        )
    )
    assert call(OWNER)["errorType"] == "MeetingInProgress"
    assert fake.updates == []
