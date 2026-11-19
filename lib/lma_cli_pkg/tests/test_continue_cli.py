# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Unit tests for `lma vp rejoin` and `lma meetings reopen`."""

import json
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from lma_cli.cli import main
from lma_sdk.exceptions import LMAAppSyncError
from lma_sdk.models.meeting import MeetingSession
from lma_sdk.models.virtual_participant import VpRow

CALL_ID = "Weekly sync - 2026-11-19-10:00:00.000"


@pytest.fixture
def client():
    return MagicMock(name="LMAClient")


def _invoke(client, args):
    return CliRunner().invoke(main, args, obj={"client_factory": lambda **kw: client})


class TestMeetingsReopen:
    def test_prints_the_session_number_and_offset(self, client):
        client.meetings.reopen.return_value = MeetingSession(
            call_id=CALL_ID, session_number=2, session_offset_seconds=1807
        )
        result = _invoke(client, ["meetings", "reopen", "--id", CALL_ID])
        assert result.exit_code == 0, result.output
        client.meetings.reopen.assert_called_once_with(CALL_ID)
        assert "Session number: 2" in result.output
        assert "Session offset: 1807 s" in result.output
        assert '"sessionNumber": 2' in result.output

    def test_prints_json(self, client):
        client.meetings.reopen.return_value = MeetingSession(
            call_id=CALL_ID, session_number=3, session_offset_seconds=12.5
        )
        result = _invoke(client, ["meetings", "reopen", "--id", CALL_ID, "--json"])
        assert result.exit_code == 0, result.output
        answer = json.loads(result.output)
        assert (answer["session_number"], answer["session_offset_seconds"]) == (3, 12.5)

    def test_prints_the_servers_refusal_and_fails(self, client):
        errors = [{"errorType": "MeetingInProgress", "message": "This meeting is still being captured."}]
        client.meetings.reopen.side_effect = LMAAppSyncError(
            "This meeting is still being captured.", details={"errors": errors}
        )
        result = _invoke(client, ["meetings", "reopen", "--id", CALL_ID])
        assert result.exit_code == 1
        assert "This meeting is still being captured." in result.output
        assert "MeetingInProgress" in result.output

    def test_needs_an_id(self, client):
        result = _invoke(client, ["meetings", "reopen"])
        assert result.exit_code != 0
        client.meetings.reopen.assert_not_called()


class TestVpRejoin:
    def test_prints_the_rejoined_session(self, client):
        client.vp.rejoin.return_value = VpRow(
            id="vp-1", status="INITIALIZING", CallId=CALL_ID, sessionNumber=2, sessionTimeOffset=1802
        )
        result = _invoke(client, ["vp", "rejoin", "--id", "vp-1"])
        assert result.exit_code == 0, result.output
        client.vp.rejoin.assert_called_once_with("vp-1")
        assert "vp-1 → INITIALIZING" in result.output
        assert "Session:        2" in result.output
        assert "1802 s" in result.output

    def test_prints_the_servers_refusal_and_fails(self, client):
        client.vp.rejoin.side_effect = LMAAppSyncError(
            "AppSync GraphQL error: This Virtual Participant has not left its meeting."
        )
        result = _invoke(client, ["vp", "rejoin", "--id", "vp-1"])
        assert result.exit_code == 1
        assert "has not left its meeting" in result.output
