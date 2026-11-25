# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A Participant's read-only access to the meetings shared with them, on a deployed stack.

See docs/user-based-access-control.md#participant for the behaviour asserted.

Default run (no live meeting needed, a few minutes, self-cleaning). Three
throwaway users are created with a permanent password and no invitation
message, and signed in through SRP as the web UI signs in: a User who owns the
meeting, a Participant on its participant list, and a Participant who is not.
A meeting is created from the START a Virtual Participant started with a
participant list sends, its transcript is seeded through the call data stream
(a spoken line and a meeting chat line), and an owner-only line and owner-only
preparation are written to the meeting directly. Then:

``test_the_meeting_is_shared_with_its_listed_participant``
    Meeting Sharing shares the meeting with the listed Participant from the
    meetings table's stream, and records that share as one it made; the owner
    is not added. Meeting Sharing is on by default; a stack with it off fails here.
``test_the_participant_reads_the_shared_meeting_and_only_the_participant_fields``
    getParticipantMeeting and getParticipantMeetingTranscript answer the listed
    Participant with the view's fields only: no meeting context, checklist,
    labels, sharing list or participant addresses, and no line on the meeting
    assistant's channels.
``test_the_participant_follows_the_live_transcript``
    onAddParticipantTranscriptSegment delivers a new line to the listed
    Participant, and refuses a selection of a field outside the view.
``test_a_participant_is_refused_a_meeting_not_shared_with_them``
    The unlisted Participant is refused every view operation for the meeting,
    and both Participants are refused the member operations at the field.
``test_a_share_gives_every_transcript_line_the_new_sharing_list``
    Forty more transcript lines are written with the meeting's sharing list,
    then the owner shares the meeting with one more address through
    shareMeetings, on condition of the list read (ExpectedSharedWith): every
    line of the transcript ends with the new list, and the meeting record's
    SharingFanout marker is removed once they do.
``test_a_share_saved_on_a_list_that_has_since_changed_is_refused``
    A shareMeetings save whose ExpectedSharedWith is not the meeting's list is
    refused with errorType SharingListChanged, and the sharing is unchanged.
``test_taking_someone_off_the_list_removes_only_the_share_meeting_sharing_made``
    A share the owner makes by hand stays when the list changes; the listed
    Participant's share goes when they are taken off it.

Teardown deletes the users, and the meeting's record, list row, transcript
items and daily counts, each by its own key. Needs pycognito, graphql-core and
websockets (requirements.txt).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
import role_access_probe as probe
from live_meeting import read_meeting, wait_for

pytest.importorskip("pycognito")
pytest.importorskip("graphql")

MEETING_RECORD_TIMEOUT_S = 90.0
SHARE_TIMEOUT_S = 120.0
SEGMENT_TIMEOUT_S = 90.0

#: The fields a ParticipantMeeting may hold (participant_view.MEETING_FIELDS).
MEETING_FIELDS = {
    "CallId",
    "CreatedAt",
    "UpdatedAt",
    "Status",
    "TotalConversationDurationMillis",
    "Owner",
    "MeetingPlatform",
    "Agenda",
    "CallSummaryText",
    "SummaryStatus",
    "ActionItems",
    "SpeakerIdentities",
    "OtherParticipantsName",
    "ParticipantNames",
    "Consent",
    "HasAudioRecording",
    "HasVideoRecording",
    "Sessions",
    "TranscriptDeletedAt",
    "RecordingsDeletedAt",
}

GET_MEETING = """
query GetParticipantMeeting($CallId: ID!) {
  getParticipantMeeting(CallId: $CallId) {
    %s
  }
}
""" % "\n    ".join(
    sorted(MEETING_FIELDS - {"ActionItems", "Consent", "Sessions"})
    + [
        "ActionItems { Text }",
        "Consent { Mode Status Replies { ParticipantName } }",
        "Sessions { SessionNumber }",
    ]
)
GET_TRANSCRIPT = """
query GetParticipantMeetingTranscript($CallId: ID!, $nextToken: String) {
  getParticipantMeetingTranscript(CallId: $CallId, nextToken: $nextToken) {
    Segments { SegmentId Channel Speaker Transcript IsPartial }
    nextToken
  }
}
"""
GET_RECORDINGS = """
query GetParticipantMeetingRecordings($CallId: ID!) {
  getParticipantMeetingRecordings(CallId: $CallId) { Kind Url }
}
"""
LIST_MEETINGS = """
query ListParticipantMeetings($nextToken: String) {
  listParticipantMeetings(limit: 100, nextToken: $nextToken) { Meetings { CallId Owner } nextToken }
}
"""
GET_CALL = "query GetCall($CallId: ID!) { getCall(CallId: $CallId) { CallId } }"
GET_SEGMENTS = (
    "query GetTranscriptSegments($callId: ID!) "
    "{ getTranscriptSegments(callId: $callId) { TranscriptSegments { SegmentId } } }"
)
UPDATE_PARTICIPANTS = """
mutation UpdateCallParticipants($input: UpdateCallParticipantsInput!) {
  updateCallParticipants(input: $input) { CallId Participants { email } }
}
"""
SHARE_MEETINGS = """
mutation ShareMeetings($input: ShareMeetingsInput!) {
  shareMeetings(input: $input) { Result SharedWith }
}
"""
SUBSCRIBE = """
subscription OnAddParticipantTranscriptSegment($CallId: ID!) {
  onAddParticipantTranscriptSegment(CallId: $CallId) { %s }
}
"""

OWNER_ONLY_CONTEXT = "integ-test owner-only preparation note"
OWNER_ONLY_ANSWER = "integ-test owner-only assistant answer"


def _data(response: dict, field: str) -> Any:
    assert not response.get("errors"), response.get("errors")
    return (response.get("data") or {})[field]


def _errors(response: dict) -> str:
    return " ".join(e.get("message", "") for e in response.get("errors") or [])


@pytest.fixture(scope="module")
def deployment(client) -> probe.Deployment:
    return probe.Deployment.find(client.stack_name, client.region)


@pytest.fixture(scope="module")
def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")


@pytest.fixture(scope="module")
def users(deployment, stamp):
    cognito = deployment.session.client("cognito-idp")
    made: list[probe.RoleUser] = []
    try:
        for label, role in (
            ("owner", "User"),
            ("listed", "Participant"),
            ("unlisted", "Participant"),
        ):
            user = probe.RoleUser(
                role,
                f"integ-participant-{label}-{stamp}@{deployment.email_domain}",
                probe._password(),
            )
            cognito.admin_create_user(
                UserPoolId=deployment.pool_id,
                Username=user.username,
                MessageAction="SUPPRESS",
                UserAttributes=[
                    {"Name": "email", "Value": user.username},
                    {"Name": "email_verified", "Value": "true"},
                ],
            )
            made.append(user)
            cognito.admin_set_user_password(
                UserPoolId=deployment.pool_id,
                Username=user.username,
                Password=user.password,
                Permanent=True,
            )
            if role == "Participant":
                cognito.admin_add_user_to_group(
                    UserPoolId=deployment.pool_id, Username=user.username, GroupName="Participant"
                )
        for user in made:
            probe.sign_in(deployment, user)
        yield dict(zip(("owner", "listed", "unlisted"), made))
    finally:
        left = probe.delete_role_users(deployment, made)
        assert not left, f"could not delete the temporary users {left}"


def _put(deployment, record: dict) -> None:
    deployment.session.client("kinesis").put_record(
        StreamName=deployment.outputs["CallDataStreamName"],
        Data=json.dumps(record).encode(),
        PartitionKey=record["CallId"],
    )


def _segment(
    call_id: str, segment_id: str, channel: str, speaker: str, text: str, start: float
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "EventType": "ADD_TRANSCRIPT_SEGMENT",
        "CallId": call_id,
        "Channel": channel,
        "SegmentId": segment_id,
        "StartTime": start,
        "EndTime": start + 1.0,
        "Transcript": text,
        "IsPartial": False,
        "CreatedAt": now,
        "UpdatedAt": now,
        "Sentiment": None,
        "TranscriptEvent": None,
        "Speaker": speaker,
        "AccessToken": "",
        "IdToken": "",
        "RefreshToken": "",
    }


@pytest.fixture(scope="module")
def meeting(deployment, users, stamp):
    """A meeting owned by the User, listing the listed Participant, with a seeded transcript."""
    session = deployment.session
    ddb = session.client("dynamodb")
    table = deployment.ai_resources["EventSourcingTable"]
    call_id = f"integ-test-participant-access-{stamp}"
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    owner = users["owner"].username
    _put(
        deployment,
        {
            "EventType": "START",
            "CallId": call_id,
            "CustomerPhoneNumber": "Integ Test Participant Access",
            "SystemPhoneNumber": "LMA Integ Test",
            "AgentId": owner,
            "CreatedAt": created_at,
            "AccessToken": "",
            "IdToken": "",
            "RefreshToken": "",
            "Participants": [
                {"name": "Integ Owner", "email": owner},
                {"name": "Integ Listed", "email": users["listed"].username},
            ],
            "ParticipantsUpdatedBy": owner,
            "ParticipantsUpdatedAt": created_at,
        },
    )
    seeded = {"speech": f"speech-{uuid.uuid4()}", "chat": f"chat-{uuid.uuid4()}"}
    try:
        found = wait_for(
            lambda: read_meeting(ddb, table, call_id),
            timeout=MEETING_RECORD_TIMEOUT_S,
            interval=3.0,
        )
        assert found, (
            f"the START for {call_id} did not create a meeting within {MEETING_RECORD_TIMEOUT_S:.0f}s"
        )
        # Owner-only material the view must never return.
        pk = {"S": f"c#{call_id}"}
        ddb.update_item(
            TableName=table,
            Key={"PK": pk, "SK": pk},
            UpdateExpression="SET MeetingContext = :c, Labels = :l",
            ExpressionAttributeValues={
                ":c": {"S": OWNER_ONLY_CONTEXT},
                ":l": {
                    "L": [{"M": {"Key": {"S": "engagement"}, "Value": {"S": "integ-owner-only"}}}]
                },
            },
        )
        _put(
            deployment,
            _segment(
                call_id, seeded["speech"], "REMOTE", "Integ Speaker", "integ spoken line", 1.0
            ),
        )
        _put(
            deployment,
            _segment(
                call_id, seeded["chat"], "MEETING_CHAT", "Integ Listed", "integ chat line", 2.0
            ),
        )
        # A meeting assistant answer, as the event processor stores one: owner-only.
        ddb.put_item(
            TableName=table,
            Item={
                "PK": {"S": f"trs#{call_id}"},
                "SK": {"S": f"ts#{created_at}#id#integ-answer"},
                "CallId": {"S": call_id},
                "SegmentId": {"S": "integ-answer"},
                "Channel": {"S": "CHAT_ASSISTANT"},
                "Transcript": {"S": OWNER_ONLY_ANSWER},
                "Speaker": {"S": "Meeting Assistant"},
                "IsPartial": {"BOOL": False},
                "StartTime": {"N": "3"},
                "EndTime": {"N": "4"},
                "Owner": {"S": owner},
                "SharedWith": {"S": f"[{users['listed'].username}]"},
                "ExpiresAfter": {"N": str(int(time.time()) + 3600)},
            },
        )
        yield call_id, created_at, seeded
    finally:
        _delete_meeting(ddb, table, call_id, created_at)


def _delete_meeting(ddb, table: str, call_id: str, created_at: str) -> None:
    """Each item the test made, by its key: the meeting's record, its list row and its transcript."""
    for pk in (f"c#{call_id}", f"trs#{call_id}"):
        for item in ddb.query(
            TableName=table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": pk}},
            ProjectionExpression="PK, SK",
        ).get("Items", []):
            ddb.delete_item(TableName=table, Key={"PK": item["PK"], "SK": item["SK"]})
    shard = f"{int(created_at[11:13]) // 4:02d}"
    ddb.delete_item(
        TableName=table,
        Key={
            "PK": {"S": f"cls#{created_at[:10]}#s#{shard}"},
            "SK": {"S": f"ts#{created_at}#id#{call_id}"},
        },
    )


def _shared_with(deployment, call_id: str) -> tuple[list, list]:
    record = (
        read_meeting(
            deployment.session.client("dynamodb"),
            deployment.ai_resources["EventSourcingTable"],
            call_id,
        )
        or {}
    )
    shared = record.get("SharedWith") or []
    if isinstance(shared, str):
        shared = [s.strip() for s in shared.strip("[]").split(",") if s.strip()]
    return list(shared), list(record.get("AutoSharedWith") or [])


@pytest.fixture(scope="module")
def shared(deployment, users, meeting):
    """The meeting's sharing once Meeting Sharing, on by default, has shared it with the listed Participant."""
    call_id, _created, _seeded = meeting
    listed = users["listed"].username
    result = wait_for(
        lambda: (
            listed in _shared_with(deployment, call_id)[0] and _shared_with(deployment, call_id)
        ),
        timeout=SHARE_TIMEOUT_S,
        interval=4.0,
    )
    assert result, (
        f"the meeting was not shared with its listed participant within {SHARE_TIMEOUT_S:.0f}s; "
        "is Meeting Sharing › Share meetings with their listed participants on?"
    )
    return result


def test_the_meeting_is_shared_with_its_listed_participant(shared, users) -> None:
    shared_with, auto_shared = shared
    assert users["listed"].username in shared_with
    assert users["listed"].username in auto_shared
    assert users["owner"].username not in shared_with
    assert users["unlisted"].username not in shared_with


def test_the_participant_reads_the_shared_meeting_and_only_the_participant_fields(
    deployment, users, meeting, shared
) -> None:
    call_id, _created, seeded = meeting
    listed = users["listed"]
    view = _data(
        probe.call(deployment, listed, GET_MEETING, {"CallId": call_id}), "getParticipantMeeting"
    )
    assert view["CallId"] == call_id
    assert view["Owner"] == users["owner"].username
    assert set(view) <= MEETING_FIELDS
    assert sorted(view["ParticipantNames"]) == ["Integ Listed", "Integ Owner"]
    text = json.dumps(view)
    for hidden in (
        OWNER_ONLY_CONTEXT,
        "integ-owner-only",
        users["listed"].username,
        users["unlisted"].username,
    ):
        assert hidden not in text, hidden

    # A field outside the view is not in its type, so the query is refused.
    refused = probe.call(
        deployment,
        listed,
        "query Q($CallId: ID!) { getParticipantMeeting(CallId: $CallId) { MeetingContext } }",
        {"CallId": call_id},
    )
    assert refused.get("errors"), refused

    def lines():
        page = _data(
            probe.call(deployment, listed, GET_TRANSCRIPT, {"CallId": call_id}),
            "getParticipantMeetingTranscript",
        )
        return page["Segments"]

    got = wait_for(
        lambda: {s["SegmentId"] for s in lines()} >= set(seeded.values()) and lines(),
        timeout=SEGMENT_TIMEOUT_S,
        interval=3.0,
    )
    assert got, "the seeded transcript lines did not reach the Participant view"
    assert {s["Channel"] for s in got} <= {"REMOTE", "LOCAL", "MEETING_CHAT"}
    assert OWNER_ONLY_ANSWER not in json.dumps(got)

    assert (
        _data(
            probe.call(deployment, listed, GET_RECORDINGS, {"CallId": call_id}),
            "getParticipantMeetingRecordings",
        )
        == []
    )

    listed_ids = []
    token = None
    for _ in range(20):
        page = _data(
            probe.call(deployment, listed, LIST_MEETINGS, {"nextToken": token}),
            "listParticipantMeetings",
        )
        listed_ids += [m["CallId"] for m in page["Meetings"]]
        token = page.get("nextToken")
        if call_id in listed_ids or not token:
            break
    assert call_id in listed_ids


def _realtime_url(deployment) -> str | None:
    url = deployment.graphql_url
    if ".appsync-api." not in url:
        return None
    return url.replace("https://", "wss://").replace(".appsync-api.", ".appsync-realtime-api.")


async def _subscribe(
    url: str, http_url: str, token: str, query: str, variables: dict, on_ready
) -> list:
    import websockets  # pylint: disable=import-outside-toplevel

    host = http_url.split("/")[2]
    auth = {"host": host, "Authorization": token}
    header = base64.b64encode(json.dumps(auth).encode()).decode()
    payload = base64.b64encode(b"{}").decode()
    received: list = []
    async with websockets.connect(
        f"{url}?header={header}&payload={payload}", subprotocols=["graphql-ws"]
    ) as ws:
        await ws.send(json.dumps({"type": "connection_init"}))
        await asyncio.wait_for(ws.recv(), timeout=20)
        await ws.send(
            json.dumps(
                {
                    "id": "1",
                    "type": "start",
                    "payload": {
                        "data": json.dumps({"query": query, "variables": variables}),
                        "extensions": {"authorization": auth},
                    },
                }
            )
        )
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if message.get("type") in ("start_ack", "error"):
                received.append(message)
                break
        if received[-1].get("type") == "error":
            return received
        on_ready()
        deadline = time.monotonic() + SEGMENT_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            except asyncio.TimeoutError:
                continue
            if message.get("type") == "data":
                received.append(message)
                break
    return received


def test_the_participant_follows_the_live_transcript(deployment, users, meeting, shared) -> None:
    pytest.importorskip("websockets")
    call_id, _created, _seeded = meeting
    url = _realtime_url(deployment)
    if not url:
        pytest.skip(
            "the GraphQL endpoint is not an AppSync domain; its real-time endpoint is not derived"
        )
    live_id = f"live-{uuid.uuid4()}"
    fields = "CallId SegmentId Transcript Channel Speaker IsPartial"
    received = asyncio.run(
        _subscribe(
            url,
            deployment.graphql_url,
            users["listed"].tokens["id"],
            SUBSCRIBE % fields,
            {"CallId": call_id},
            lambda: (
                time.sleep(2),
                _put(
                    deployment,
                    _segment(call_id, live_id, "REMOTE", "Integ Speaker", "integ live line", 5.0),
                ),
            ),
        )
    )
    assert received[0].get("type") == "start_ack", received
    data = [m for m in received if m.get("type") == "data"]
    assert data, "no live line reached the Participant"
    segment = data[0]["payload"]["data"]["onAddParticipantTranscriptSegment"]
    assert segment["SegmentId"] == live_id
    assert set(segment) == set(fields.split())

    # A selection beyond the view is refused when the subscription is made.
    refused = asyncio.run(
        _subscribe(
            url,
            deployment.graphql_url,
            users["listed"].tokens["id"],
            SUBSCRIBE % "SegmentId SharedWith",
            {"CallId": call_id},
            lambda: None,
        )
    )
    assert refused[-1].get("type") == "error", refused
    # And a Participant the meeting is not shared with is refused it.
    outsider = asyncio.run(
        _subscribe(
            url,
            deployment.graphql_url,
            users["unlisted"].tokens["id"],
            SUBSCRIBE % "SegmentId",
            {"CallId": call_id},
            lambda: None,
        )
    )
    assert outsider[-1].get("type") == "error", outsider


def test_a_participant_is_refused_a_meeting_not_shared_with_them(
    deployment, users, meeting, shared
) -> None:
    call_id, _created, _seeded = meeting
    unlisted = users["unlisted"]
    for document, field in (
        (GET_MEETING, "getParticipantMeeting"),
        (GET_TRANSCRIPT, "getParticipantMeetingTranscript"),
        (GET_RECORDINGS, "getParticipantMeetingRecordings"),
    ):
        response = probe.call(deployment, unlisted, document, {"CallId": call_id})
        assert "Unauthorized" in _errors(response), (field, response)
        assert not (response.get("data") or {}).get(field), (field, response)
    listed_meetings = _data(
        probe.call(deployment, unlisted, LIST_MEETINGS, {}), "listParticipantMeetings"
    )
    assert call_id not in [m["CallId"] for m in listed_meetings["Meetings"]]
    # The member operations refuse both Participants at the field, shared or not.
    for user in (users["listed"], unlisted):
        for document, variables, operation in (
            (GET_CALL, {"CallId": call_id}, "Query.getCall"),
            (GET_SEGMENTS, {"callId": call_id}, "Query.getTranscriptSegments"),
            (
                UPDATE_PARTICIPANTS,
                {"input": {"CallId": call_id, "Participants": []}},
                "Mutation.updateCallParticipants",
            ),
        ):
            assert probe.refused_at_field(
                probe.call(deployment, user, document, variables), operation
            ), (
                user.username,
                operation,
            )
    # And the owner is refused the Participant view at the field.
    owner_call = probe.call(deployment, users["owner"], GET_MEETING, {"CallId": call_id})
    assert probe.refused_at_field(owner_call, "Query.getParticipantMeeting"), owner_call


FANOUT_SEGMENTS = 40


def _entries(value: Any) -> list:
    if isinstance(value, str):
        return [s.strip() for s in value.strip("[]").split(",") if s.strip()]
    return [s for s in (value or []) if isinstance(s, str)]


def _share_call(call_id: str, created_at: str, expected: Any = None) -> dict:
    shard = f"{int(created_at[11:13]) // 4:02d}"
    call = {
        "CallId": call_id,
        "ListPK": f"cls#{created_at[:10]}#s#{shard}",
        "ListSK": f"ts#{created_at}#id#{call_id}",
    }
    if expected is not None:
        call["ExpectedSharedWith"] = expected
    return call


def _transcript_lists(deployment, call_id: str) -> dict:
    """The sharing list of every item under the meeting's transcript partition, by SK."""
    from boto3.dynamodb.types import TypeDeserializer

    ddb = deployment.session.client("dynamodb")
    deserializer = TypeDeserializer()
    out: dict = {}
    kwargs = {
        "TableName": deployment.ai_resources["EventSourcingTable"],
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": {"S": f"trs#{call_id}"}},
        "ProjectionExpression": "SK, SharedWith",
        "ConsistentRead": True,
    }
    while True:
        page = ddb.query(**kwargs)
        for item in page.get("Items", []):
            shared = item.get("SharedWith")
            out[item["SK"]["S"]] = _entries(deserializer.deserialize(shared) if shared else None)
        if not page.get("LastEvaluatedKey"):
            return out
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def test_a_share_gives_every_transcript_line_the_new_sharing_list(
    deployment, users, meeting, shared
) -> None:
    """The share dialog's save reaches every transcript line, and finishes (no SharingFanout left)."""
    call_id, created_at, _seeded = meeting
    ddb = deployment.session.client("dynamodb")
    table = deployment.ai_resources["EventSourcingTable"]
    current, _auto = _shared_with(deployment, call_id)
    # Lines written before the share, with the sharing list they were written with.
    for i in range(FANOUT_SEGMENTS):
        ddb.put_item(
            TableName=table,
            Item={
                "PK": {"S": f"trs#{call_id}"},
                "SK": {"S": f"ts#{created_at}#id#integ-fanout-{i:03d}"},
                "CallId": {"S": call_id},
                "SegmentId": {"S": f"integ-fanout-{i:03d}"},
                "Channel": {"S": "REMOTE"},
                "Transcript": {"S": f"integ fan-out line {i}"},
                "Speaker": {"S": "Integ Speaker"},
                "IsPartial": {"BOOL": False},
                "StartTime": {"N": str(10 + i)},
                "EndTime": {"N": str(11 + i)},
                "Owner": {"S": users["owner"].username},
                "SharedWith": {"L": [{"S": a} for a in current]},
                "ExpiresAfter": {"N": str(int(time.time()) + 3600)},
            },
        )
    added = f"integ-participant-fanout-{uuid.uuid4().hex[:8]}@example.invalid"
    wanted = current + [added]
    share = probe.call(
        deployment,
        users["owner"],
        SHARE_MEETINGS,
        {
            "input": {
                "Calls": [_share_call(call_id, created_at, expected=current)],
                "MeetingRecipients": ",".join(wanted),
            }
        },
    )
    assert _data(share, "shareMeetings")["Result"] == "Meetings shared successfully", share

    def finished():
        record = read_meeting(ddb, table, call_id) or {}
        lists = _transcript_lists(deployment, call_id)
        done = "SharingFanout" not in record and all(added in v for v in lists.values())
        return done and lists

    lists = wait_for(finished, timeout=SHARE_TIMEOUT_S, interval=3.0)
    assert lists, (
        f"not every transcript line had the new sharing list within {SHARE_TIMEOUT_S:.0f}s: "
        f"{_transcript_lists(deployment, call_id)}"
    )
    assert len(lists) >= FANOUT_SEGMENTS
    assert all(sorted(v) == sorted(wanted) for v in lists.values()), lists
    assert sorted(_shared_with(deployment, call_id)[0]) == sorted(wanted)


def test_a_share_saved_on_a_list_that_has_since_changed_is_refused(
    deployment, users, meeting, shared
) -> None:
    """A save with ExpectedSharedWith that no longer holds changes nothing, and says so."""
    call_id, created_at, _seeded = meeting
    before, auto_before = _shared_with(deployment, call_id)
    stale = [a for a in before if a != users["listed"].username]
    response = probe.call(
        deployment,
        users["owner"],
        SHARE_MEETINGS,
        {
            "input": {
                "Calls": [_share_call(call_id, created_at, expected=stale)],
                "MeetingRecipients": ",".join(stale),
            }
        },
    )
    errors = response.get("errors") or []
    assert [e.get("errorType") for e in errors] == ["SharingListChanged"], response
    assert "sharing list changed" in _errors(response), response
    assert _shared_with(deployment, call_id) == (before, auto_before)


def test_taking_someone_off_the_list_removes_only_the_share_meeting_sharing_made(
    deployment, users, meeting, shared
) -> None:
    call_id, created_at, _seeded = meeting
    owner = users["owner"]
    listed = users["listed"].username
    by_hand = f"integ-participant-byhand-{uuid.uuid4().hex[:8]}@example.invalid"
    shard = f"{int(created_at[11:13]) // 4:02d}"
    share = probe.call(
        deployment,
        owner,
        SHARE_MEETINGS,
        {
            "input": {
                "Calls": [
                    {
                        "CallId": call_id,
                        "ListPK": f"cls#{created_at[:10]}#s#{shard}",
                        "ListSK": f"ts#{created_at}#id#{call_id}",
                    }
                ],
                "MeetingRecipients": f"{listed},{by_hand}",
            }
        },
    )
    assert not share.get("errors"), share
    _data(
        probe.call(
            deployment,
            owner,
            UPDATE_PARTICIPANTS,
            {
                "input": {
                    "CallId": call_id,
                    "Participants": [{"name": "Integ Owner", "email": owner.username}],
                }
            },
        ),
        "updateCallParticipants",
    )
    after = wait_for(
        lambda: (
            listed not in _shared_with(deployment, call_id)[0] and _shared_with(deployment, call_id)
        ),
        timeout=SHARE_TIMEOUT_S,
        interval=4.0,
    )
    assert after, "the share of the Participant taken off the list was not removed"
    shared_with, auto_shared = after
    assert by_hand in shared_with
    assert listed not in auto_shared
    refused = probe.call(deployment, users["listed"], GET_MEETING, {"CallId": call_id})
    assert "Unauthorized" in _errors(refused), refused
