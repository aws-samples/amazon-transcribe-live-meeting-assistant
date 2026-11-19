# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A meeting's capture sessions: the next session, its offset, and who may reopen."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import pytest
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import lma_sessions as sessions  # noqa: E402

CALL_ID = "Weekly sync - 2026-11-18 09:00"


class Table:
    """The event sourcing table: one meeting record, conditions evaluated."""

    def __init__(self, meeting: Dict[str, Any] | None, end_times: List[Any] = ()) -> None:
        self.meeting = dict(meeting) if meeting is not None else None
        self.end_times = list(end_times)
        self.updates: List[Dict[str, Any]] = []

    def get_item(self, Key: Dict[str, str], ConsistentRead: bool) -> Dict[str, Any]:
        assert Key == sessions.meeting_key(CALL_ID) and ConsistentRead
        return {"Item": dict(self.meeting)} if self.meeting is not None else {}

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        assert kwargs["ExpressionAttributeValues"] == {":pk": f"trs#{CALL_ID}"}
        half = len(self.end_times) // 2
        if "ExclusiveStartKey" not in kwargs:
            return {
                "Items": [{"EndTime": end} for end in self.end_times[:half]],
                "LastEvaluatedKey": {"PK": "next"},
            }
        return {"Items": [{"EndTime": end} for end in self.end_times[half:]]}

    def update_item(self, **kwargs: Any) -> Dict[str, Any]:
        self.updates.append(kwargs)
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
            ReopenedBy=values[":by"],
        )
        return {}


@pytest.mark.parametrize(
    "value,expected",
    [(None, 1), ("", 1), (0, 1), (1, 1), (Decimal("3"), 3), ("4", 4), (True, 1), ("x", 1)],
)
def test_a_stored_session_number_below_two_is_the_first_session(value, expected) -> None:
    assert sessions.session_number(value) == expected


def test_the_next_session_follows_every_session_already_allowed_or_started() -> None:
    assert sessions.next_session_number() == 2
    assert sessions.next_session_number(None, None) == 2
    assert sessions.next_session_number(Decimal("2"), Decimal("3"), None) == 4


@pytest.mark.parametrize(
    "end_times,meeting,expected",
    [
        ([Decimal("400"), Decimal("512.2")], {"TotalConversationDurationMillis": 400000}, 514),
        ([], {"TotalConversationDurationMillis": Decimal("90000")}, 91),
        ([], {"SessionOffsetSeconds": Decimal("300")}, 301),
        ([None, "x", float("nan")], {}, 0),
        ([], {}, 0),
    ],
)
def test_a_new_session_starts_after_everything_already_on_the_timeline(
    end_times, meeting, expected
) -> None:
    assert sessions.next_session_offset(end_times, meeting) == expected


def test_every_page_of_the_transcript_is_read() -> None:
    table = Table({"PK": "x"}, [1, 2, 3, 4])
    assert list(sessions.transcript_end_times(table, CALL_ID)) == [1, 2, 3, 4]


def test_a_session_is_allowed_once_on_a_meeting_whose_transcript_is_kept() -> None:
    table = Table({"PK": "x", "SessionNumber": 2})
    sessions.allow_session(table, CALL_ID, 3, 120, "owner@example.com")
    assert table.meeting["SessionNumber"] == 3
    assert table.meeting["SessionOffsetSeconds"] == Decimal(120)
    with pytest.raises(sessions.SessionNotAllowed):
        sessions.allow_session(table, CALL_ID, 3, 120, "owner@example.com")


@pytest.mark.parametrize(
    "meeting", [None, {"PK": "x", "TranscriptDeletedAt": "2026-11-18T10:00:00Z"}]
)
def test_no_session_is_allowed_on_a_missing_meeting_or_a_deleted_transcript(meeting) -> None:
    with pytest.raises(sessions.SessionNotAllowed):
        sessions.allow_session(Table(meeting), CALL_ID, 2, 0, "owner@example.com")


def test_an_other_write_failure_is_raised_as_it_is() -> None:
    class Failing(Table):
        def update_item(self, **kwargs: Any) -> Dict[str, Any]:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "U")

    with pytest.raises(ClientError):
        sessions.allow_session(Failing({"PK": "x"}), CALL_ID, 2, 0, "owner@example.com")


@pytest.mark.parametrize(
    "identity,allowed",
    [
        ({"username": "owner@example.com", "groups": []}, True),
        ({"username": "admin@example.com", "groups": ["Admin"]}, True),
        ({"username": "admin@example.com", "claims": {"cognito:groups": "Admin"}}, True),
        ({"username": "bob@example.com", "groups": []}, False),
        ({"groups": []}, False),
        (None, False),
        # A backend role (the lma CLI and SDK) may reopen any meeting.
        ({"userArn": "arn:aws:sts::123456789012:assumed-role/Operator/session"}, True),
        # An IAM user is not a backend role.
        ({"userArn": "arn:aws:iam::123456789012:user/someone", "username": "AIDAEXAMPLE"}, False),
    ],
)
def test_only_the_meetings_owner_or_an_admin_may_reopen_it(identity, allowed) -> None:
    meeting = {"Owner": "owner@example.com", "SharedWith": "bob@example.com"}
    assert sessions.caller_owns_meeting(identity, meeting) is allowed


def test_the_reopener_is_recorded_by_email_then_username() -> None:
    assert sessions.who({"username": "u", "claims": {"email": "e@example.com"}}) == "e@example.com"
    assert sessions.who({"username": "u"}) == "u"
    assert sessions.who(None) == "unknown"
    role = "arn:aws:sts::123456789012:assumed-role/Operator/session"
    assert sessions.who({"userArn": role, "username": "AROAEXAMPLE:session"}) == role
