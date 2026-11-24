# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Unit tests for client.meetings: finding meetings by their labels, and deleting them;
and continuing a meeting (client.meetings.reopen, client.vp.rejoin).

The AWS clients are stand-ins holding a small meetings table: list rows on the
TypeDateIndex and meeting records keyed c#<CallId>. What is asserted is which
meetings a user's selection holds (their own, or every one for an Admin), that a
dry run deletes nothing, that a meeting in progress is skipped, and that each
deletion is one request to the meeting controls function's deleteMeetings -- the
per-meeting delete path the meetings list uses, which removes every part of the
meeting.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any, Dict, List

from unittest.mock import MagicMock

import pytest
from boto3.dynamodb.types import TypeSerializer

from lma_sdk.exceptions import (
    LMAAppSyncError,
    LMAValidationError,
    LMAVirtualParticipantError,
)
from lma_sdk.models.meeting import MeetingSession
from lma_sdk.operations import meetings as meeting_ops
from lma_sdk.operations.meetings import meeting_id_from

SERIALIZER = TypeSerializer()


def _ddb(item: Dict[str, Any]) -> Dict[str, Any]:
    return {key: SERIALIZER.serialize(value) for key, value in item.items()}


def _meeting(call_id, owner, labels, status="ENDED", created="2026-11-01T10:00:00Z"):
    return {
        "list": {
            "PK": f"cls#{created[:10]}#s#02",
            "SK": f"ts#{created}#id#{call_id}",
            "ItemType": "call",
            "CallId": call_id,
            "Owner": owner,
            "CreatedAt": created,
        },
        "record": {
            "PK": f"c#{call_id}",
            "SK": f"c#{call_id}",
            "CallId": call_id,
            "Owner": owner,
            "Status": status,
            "Labels": [{"Key": k, "Value": v} for k, v in labels.items()],
        },
    }


MEETINGS = [
    _meeting("acme-1", "alice", {"engagement": "acme"}),
    _meeting("acme-2", "bob", {"engagement": "acme", "phase": "two"}),
    _meeting("acme-live", "alice", {"engagement": "acme"}, status="TRANSCRIBING"),
    _meeting("globex-1", "alice", {"engagement": "globex"}),
    _meeting("unlabelled", "alice", {}),
]


class FakeDynamo:
    """Query pages of two rows, so a selection has to follow LastEvaluatedKey."""

    def __init__(self, meetings):
        self.rows = [m["list"] for m in meetings]
        self.records = {m["record"]["CallId"]: m["record"] for m in meetings}
        self.queries: List[Dict[str, Any]] = []

    def query(self, **kwargs):
        self.queries.append(kwargs)
        start = int(kwargs.get("ExclusiveStartKey", {}).get("offset", {"N": "0"})["N"])
        page = self.rows[start : start + 2]
        response: Dict[str, Any] = {"Items": [_ddb(row) for row in page]}
        if start + 2 < len(self.rows):
            response["LastEvaluatedKey"] = {"offset": {"N": str(start + 2)}}
        return response

    def batch_get_item(self, RequestItems):
        ((table, request),) = RequestItems.items()
        found = []
        for key in request["Keys"]:
            call_id = key["PK"]["S"].removeprefix("c#")
            if call_id in self.records:
                found.append(_ddb(self.records[call_id]))
        return {"Responses": {table: found}}


class FakeLambda:
    def __init__(self, answers=None):
        self.invocations: List[Dict[str, Any]] = []
        self.answers = answers or {}

    def invoke(self, **kwargs):
        event = json.loads(kwargs["Payload"])
        self.invocations.append({"FunctionName": kwargs["FunctionName"], "event": event})
        call_id = event["arguments"]["input"]["Calls"][0]["CallId"]
        answer = self.answers.get(call_id, {"Result": meeting_ops.DELETED})
        if isinstance(answer, Exception):
            return {"FunctionError": "Unhandled", "Payload": io.BytesIO(b'{"errorMessage": "boom"}')}
        return {"Payload": io.BytesIO(json.dumps(answer).encode())}


class FakeCognito:
    def __init__(self, groups):
        self.groups = groups

    def admin_list_groups_for_user(self, **kwargs):
        return {"Groups": [{"GroupName": g} for g in self.groups.get(kwargs["Username"], [])]}


class FakeCfn:
    def describe_stack_resource(self, StackName, LogicalResourceId):
        ids = {"AISTACK": "test-lma-stack-AISTACK-nested",
               "EventSourcingTable": "test-lma-stack-EventSourcingTable"}
        return {"StackResourceDetail": {"PhysicalResourceId": ids[LogicalResourceId]}}


@pytest.fixture
def aws(client):
    fakes = SimpleNamespace(
        dynamodb=FakeDynamo(MEETINGS),
        lambda_=FakeLambda(),
        cognito=FakeCognito({"admin": ["Admin"], "alice": ["Readers"]}),
        cloudformation=FakeCfn(),
    )
    services = {
        "dynamodb": fakes.dynamodb,
        "lambda": fakes.lambda_,
        "cognito-idp": fakes.cognito,
        "cloudformation": fakes.cloudformation,
        "sts": SimpleNamespace(get_caller_identity=lambda: {"Account": "111122223333"}),
    }
    client.session.client.side_effect = lambda name, **_kw: services[name]
    vp = SimpleNamespace(
        resolve_user=lambda ident: SimpleNamespace(username=ident, sub=f"sub-{ident}"),
        user_pool_id="us-east-1_pool",
    )
    client._vp_ops = vp
    return fakes


# ── labels ─────────────────────────────────────────────────────────────────


def test_labels_are_parsed_as_key_value_pairs():
    assert meeting_ops.parse_labels(["engagement=acme", "phase = two"]) == {
        "engagement": "acme",
        "phase": "two",
    }
    assert meeting_ops.parse_labels(["note=a=b"]) == {"note": "a=b"}


@pytest.mark.parametrize("values", [[], ["engagement"], ["=acme"], ["a=1", "a=2"]])
def test_a_malformed_label_is_refused(values):
    with pytest.raises(LMAValidationError):
        meeting_ops.parse_labels(values)


# ── who the selection is for ───────────────────────────────────────────────


def test_a_user_finds_only_their_own_meetings(client, aws):
    found = client.meetings.find_by_labels({"engagement": "acme"}, as_user="alice")
    assert [m.call_id for m in found] == ["acme-1", "acme-live"]
    assert all(m.owner == "alice" for m in found)


def test_an_admin_finds_every_users_meetings(client, aws):
    found = client.meetings.find_by_labels({"engagement": "acme"}, as_user="admin")
    assert [m.call_id for m in found] == ["acme-1", "acme-2", "acme-live"]


def test_every_label_given_has_to_match(client, aws):
    found = client.meetings.find_by_labels({"engagement": "acme", "phase": "two"}, as_user="admin")
    assert [m.call_id for m in found] == ["acme-2"]


def test_a_user_in_the_admin_group_is_told_apart_by_cognito(client, aws):
    assert client.meetings.user_scope("admin") == ("admin", True)
    assert client.meetings.user_scope("alice") == ("alice", False)


def test_the_whole_index_is_read(client, aws):
    client.meetings.find_by_labels({"engagement": "globex"}, as_user="admin")
    assert len(aws.dynamodb.queries) == 3  # five rows, two a page
    assert aws.dynamodb.queries[0]["IndexName"] == "TypeDateIndex"
    assert aws.dynamodb.queries[0]["TableName"] == "test-lma-stack-EventSourcingTable"


# ── the dry run ────────────────────────────────────────────────────────────


def test_a_dry_run_lists_the_meetings_and_deletes_nothing(client, aws):
    result = client.meetings.delete_by_labels({"engagement": "acme"}, as_user="admin")
    assert result.dry_run is True
    assert [m.call_id for m in result.matched] == ["acme-1", "acme-2"]
    assert [m.call_id for m in result.skipped_in_progress] == ["acme-live"]
    assert result.deleted == [] and result.failed == []
    assert aws.lambda_.invocations == []


# ── deleting ───────────────────────────────────────────────────────────────


def test_each_meeting_is_deleted_through_the_per_meeting_delete_path(client, aws):
    result = client.meetings.delete_by_labels({"engagement": "acme"}, as_user="alice", confirm=True)
    assert result.dry_run is False
    assert result.deleted == ["acme-1"]
    assert [m.call_id for m in result.skipped_in_progress] == ["acme-live"]
    (invocation,) = aws.lambda_.invocations
    assert invocation["FunctionName"] == "test-lma-stack-MeetingControlsLambda"
    assert invocation["event"] == {
        "identity": {"username": "alice", "groups": []},
        "arguments": {
            "input": {
                "Calls": [
                    {
                        "CallId": "acme-1",
                        "ListPK": "cls#2026-11-01#s#02",
                        "ListSK": "ts#2026-11-01T10:00:00Z#id#acme-1",
                    }
                ]
            }
        },
        "info": {"fieldName": "deleteMeetings"},
    }


def test_an_admin_deletes_as_an_admin(client, aws):
    result = client.meetings.delete_by_labels({"engagement": "acme"}, as_user="admin", confirm=True)
    assert result.scope == "all"
    assert result.deleted == ["acme-1", "acme-2"]
    assert {tuple(i["event"]["identity"]["groups"]) for i in aws.lambda_.invocations} == {("Admin",)}


def test_a_meeting_in_progress_is_never_deleted(client, aws):
    client.meetings.delete_by_labels({"engagement": "acme"}, as_user="admin", confirm=True)
    deleted = {i["event"]["arguments"]["input"]["Calls"][0]["CallId"] for i in aws.lambda_.invocations}
    assert "acme-live" not in deleted


def test_a_failed_meeting_is_reported_and_the_rest_are_still_deleted(client, aws):
    aws.lambda_.answers = {
        "acme-1": {"Result": "You don't have permission to share or delete one or more"},
        "acme-2": RuntimeError("raised"),
    }
    aws.dynamodb.rows.append(_meeting("acme-3", "bob", {"engagement": "acme"})["list"])
    aws.dynamodb.records["acme-3"] = _meeting("acme-3", "bob", {"engagement": "acme"})["record"]
    result = client.meetings.delete_by_labels({"engagement": "acme"}, as_user="admin", confirm=True)
    assert result.deleted == ["acme-3"]
    assert [(f.call_id, f.error) for f in result.failed] == [
        ("acme-1", "You don't have permission to share or delete one or more"),
        ("acme-2", "boom"),
    ]


def test_nothing_matching_deletes_nothing(client, aws):
    result = client.meetings.delete_by_labels({"engagement": "none"}, as_user="admin", confirm=True)
    assert result.matched == [] and aws.lambda_.invocations == []


def test_the_result_names_the_stack_region_and_account(client, aws):
    result = client.meetings.delete_by_labels({"engagement": "acme"}, as_user="admin")
    assert (result.stack, result.region, result.account) == (
        "test-lma-stack",
        "us-east-1",
        "111122223333",
    )


def test_keys_left_unprocessed_are_retried_with_backoff(client, aws, monkeypatch):
    slept = []
    monkeypatch.setattr(meeting_ops.time, "sleep", slept.append)
    real = aws.dynamodb.batch_get_item
    calls = {"n": 0}

    def throttled_once(RequestItems):
        calls["n"] += 1
        if calls["n"] == 1:
            ((table, _request),) = RequestItems.items()
            return {"Responses": {table: []}, "UnprocessedKeys": RequestItems}
        return real(RequestItems)

    aws.dynamodb.batch_get_item = throttled_once
    found = client.meetings.find_by_labels({"engagement": "acme"}, as_user="admin")
    assert [m.call_id for m in found] == ["acme-1", "acme-2", "acme-live"]
    assert slept and slept[0] > 0


def test_a_table_that_keeps_refusing_reads_fails_rather_than_spinning(client, aws, monkeypatch):
    monkeypatch.setattr(meeting_ops.time, "sleep", lambda _s: None)
    aws.dynamodb.batch_get_item = lambda RequestItems: {
        "Responses": {},
        "UnprocessedKeys": RequestItems,
    }
    with pytest.raises(meeting_ops.LMAConfigurationError, match="kept refusing"):
        client.meetings.find_by_labels({"engagement": "acme"}, as_user="admin")


# ── continuing a meeting ───────────────────────────────────────────────────

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
