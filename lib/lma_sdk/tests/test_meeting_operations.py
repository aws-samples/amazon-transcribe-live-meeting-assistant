# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Unit tests for client.meetings.reopen and client.vp.rejoin."""

from unittest.mock import MagicMock

import pytest

from lma_sdk.exceptions import (
    LMAAppSyncError,
    LMAValidationError,
    LMAVirtualParticipantError,
)
from lma_sdk.models.meeting import MeetingSession
from lma_sdk.operations.meetings import meeting_id_from

CALL_ID = "Weekly sync - 2026-11-19-10:00:00.000"


@pytest.fixture
def appsync(client):
    fake = MagicMock(name="appsync")
    client._appsync_ops = fake
    return fake


def _sent(appsync):
    call = appsync.graphql.call_args.kwargs
    return call["query"], call["variables"]


def _refused(error_type, message):
    errors = [{"errorType": error_type, "message": message}]
    return LMAAppSyncError(f"AppSync GraphQL error: {message}", details={"errors": errors})


class TestMeetingIdFrom:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (CALL_ID, CALL_ID),
            (f"  {CALL_ID}\n", CALL_ID),
            ("https://d1.cloudfront.net/#/calls/Weekly%20sync%20-%202026", "Weekly sync - 2026"),
            ("https://d1.cloudfront.net/#/calls/m%2F1/?tab=summary", "m/1"),
            ("https://example.com/other", None),
            ("https://d1.cloudfront.net/#/calls/", None),
            ("", None),
            (None, None),
        ],
    )
    def test_reads_an_id_or_a_meeting_page_link(self, value, expected):
        assert meeting_id_from(value) == expected


class TestReopen:
    def test_answers_the_session_the_server_allowed(self, client, appsync):
        appsync.graphql.return_value = {
            "reopenMeeting": {
                "CallId": CALL_ID,
                "SessionNumber": 2,
                "SessionOffsetSeconds": 1807,
                "ReopenedAt": "2026-11-19T11:00:00Z",
            }
        }
        session = client.meetings.reopen(CALL_ID)
        assert session == MeetingSession(
            call_id=CALL_ID,
            session_number=2,
            session_offset_seconds=1807.0,
            reopened_at="2026-11-19T11:00:00Z",
        )
        query, variables = _sent(appsync)
        assert "reopenMeeting(input: $input)" in query
        assert variables == {"input": {"CallId": CALL_ID}}

    def test_takes_the_meeting_id_from_a_meeting_page_link(self, client, appsync):
        appsync.graphql.return_value = {
            "reopenMeeting": {"CallId": "m 1", "SessionNumber": 3, "SessionOffsetSeconds": 0}
        }
        client.meetings.reopen("https://d1.cloudfront.net/#/calls/m%201")
        assert _sent(appsync)[1] == {"input": {"CallId": "m 1"}}

    @pytest.mark.parametrize("value", ["", "   ", "https://example.com/not-a-meeting"])
    def test_refuses_nothing_usable_without_calling_the_server(self, client, appsync, value):
        with pytest.raises(LMAValidationError):
            client.meetings.reopen(value)
        appsync.graphql.assert_not_called()

    @pytest.mark.parametrize(
        ("error_type", "message"),
        [
            ("MeetingNotFound", "There is no meeting with this ID."),
            ("MeetingInProgress", "This meeting is still being captured."),
            ("TranscriptDeleted", "This meeting's transcript has been deleted."),
            ("Unauthorized", "Only the meeting's owner or an Admin can continue this meeting."),
        ],
    )
    def test_raises_the_servers_refusal_with_its_type_and_message(
        self, client, appsync, error_type, message
    ):
        appsync.graphql.side_effect = _refused(error_type, message)
        with pytest.raises(LMAAppSyncError) as caught:
            client.meetings.reopen(CALL_ID)
        assert caught.value.error_type == error_type
        assert str(caught.value) == message

    def test_raises_an_error_without_a_type_as_it_is(self, client, appsync):
        original = LMAAppSyncError("AppSync HTTP 403: denied", details={"status": 403})
        appsync.graphql.side_effect = original
        with pytest.raises(LMAAppSyncError) as caught:
            client.meetings.reopen(CALL_ID)
        assert caught.value is original
        assert caught.value.error_type is None

    def test_an_empty_answer_is_an_error(self, client, appsync):
        appsync.graphql.return_value = {"reopenMeeting": None}
        with pytest.raises(LMAAppSyncError):
            client.meetings.reopen(CALL_ID)


class TestRejoin:
    def test_rejoins_and_answers_the_row_with_its_session(self, client, appsync):
        appsync.graphql.return_value = {
            "rejoinVirtualParticipant": {
                "id": "vp-1",
                "status": "INITIALIZING",
                "CallId": CALL_ID,
                "sessionNumber": 2,
                "sessionTimeOffset": 1802,
            }
        }
        row = client.vp.rejoin("vp-1")
        assert (row.status, row.CallId, row.sessionNumber, row.sessionTimeOffset) == (
            "INITIALIZING",
            CALL_ID,
            2,
            1802.0,
        )
        query, variables = _sent(appsync)
        assert "rejoinVirtualParticipant(input: $input)" in query
        assert variables == {"input": {"id": "vp-1"}}

    def test_raises_the_servers_refusal(self, client, appsync):
        appsync.graphql.side_effect = LMAAppSyncError(
            "AppSync GraphQL error: This Virtual Participant has not left its meeting."
        )
        with pytest.raises(LMAAppSyncError, match="has not left"):
            client.vp.rejoin("vp-1")

    def test_an_empty_answer_is_an_error(self, client, appsync):
        appsync.graphql.return_value = {}
        with pytest.raises(LMAVirtualParticipantError):
            client.vp.rejoin("vp-1")
