# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A meeting's participant list, its attendees' LMA accounts, and Participant accounts for them.

See docs/virtual-participant.md#participant-list and
docs/user-management.md#participant-accounts-for-a-meeting for the behaviour
asserted.

Default run (no meeting needed, about a minute, self-cleaning). Four throwaway
users are created with a permanent password and no invitation message, and
signed in through SRP as the web UI signs in: an Admin, a User who owns the
meeting, a second User, and a Participant. Then:

``test_the_meeting_carries_the_participant_list_it_was_started_with``
    Puts on the call data stream the START a Virtual Participant started with a
    participant list sends, owned by the first User, and reads the list back
    from the meeting record the Call Event Processor creates.
``test_the_owner_reads_whether_each_attendee_has_an_account``
    getMeetingParticipantAccounts, as the owner, answers Admin, User,
    Participant and none for the four listed addresses, and nothing else.
``test_another_user_and_a_participant_are_refused``
    The second User is refused all three operations by the resolvers (not the
    meeting's owner); the Participant is refused them by AppSync at the field.
``test_the_owner_creates_a_participant_account_for_a_listed_attendee``
    createParticipantAccounts, as the owner, for the one listed address with no
    account, creates it in the Participant group (Cognito sends its invitation,
    to a throwaway address), answers "exists" when asked again, and refuses an
    address not on the list.
``test_the_owner_changes_the_list``
    updateCallParticipants, as the owner, replaces the list, normalized.
``test_a_scheduled_virtual_participant_s_list_is_read_before_its_meeting_exists``
    A Virtual Participant scheduled a day ahead with a participant list, read
    by VirtualParticipantId; it is cancelled and its row deleted at the end.

Teardown deletes the users (including the account the test created), the
meeting record and its list row, the owner's daily account count and the
Virtual Participant row. Needs pycognito and graphql-core (requirements.txt).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import pytest
import role_access_probe as probe
from live_meeting import read_meeting, wait_for

pytest.importorskip("pycognito")
pytest.importorskip("graphql")

MEETING_RECORD_TIMEOUT_S = 90.0

GET_ACCOUNTS = """
query GetMeetingParticipantAccounts($CallId: ID, $VirtualParticipantId: ID) {
  getMeetingParticipantAccounts(CallId: $CallId, VirtualParticipantId: $VirtualParticipantId) {
    name email account
  }
}
"""
CREATE_ACCOUNTS = """
mutation CreateParticipantAccounts($input: CreateParticipantAccountsInput!) {
  createParticipantAccounts(input: $input) { results { email outcome message } created remainingToday }
}
"""
UPDATE_PARTICIPANTS = """
mutation UpdateCallParticipants($input: UpdateCallParticipantsInput!) {
  updateCallParticipants(input: $input) { CallId Participants { name email } ParticipantsUpdatedBy }
}
"""
CREATE_VP = """
mutation CreateVirtualParticipant($input: CreateVirtualParticipantInput!) {
  createVirtualParticipant(input: $input) { id participants { name email } }
}
"""
END_VP = """
mutation EndVirtualParticipant($input: EndVirtualParticipantInput!) {
  endVirtualParticipant(input: $input) { id status }
}
"""


def _data(response: dict, field: str) -> Any:
    assert not response.get("errors"), response.get("errors")
    return (response.get("data") or {})[field]


def _error_text(response: dict) -> str:
    return " ".join(e.get("message", "") for e in response.get("errors") or [])


@pytest.fixture(scope="module")
def deployment(client) -> probe.Deployment:
    return probe.Deployment.find(client.stack_name, client.region)


@pytest.fixture(scope="module")
def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")


@pytest.fixture(scope="module")
def new_address(deployment, stamp) -> str:
    """The listed address with no account, for which the test creates one.

    In the stack's allowed domain when it has one (AllowedSignUpEmailDomain),
    else in the reserved example.invalid domain, which delivers nowhere.
    """
    domain = deployment.email_domain if deployment.email_domain != "example.com" else "example.invalid"
    return f"integ-participant-{stamp}@{domain}"


@pytest.fixture(scope="module")
def users(deployment, stamp, new_address):
    made = probe.create_role_users(deployment, stamp)
    other = probe.RoleUser("User", f"integ-rbac-other-{stamp}@{deployment.email_domain}", probe._password())
    cognito = deployment.session.client("cognito-idp")
    try:
        cognito.admin_create_user(
            UserPoolId=deployment.pool_id,
            Username=other.username,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": other.username},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
        made.append(other)
        cognito.admin_set_user_password(
            UserPoolId=deployment.pool_id, Username=other.username, Password=other.password, Permanent=True
        )
        for user in made:
            probe.sign_in(deployment, user)
        by_role = {user.role: user for user in made if user is not other}
        yield {**by_role, "Other": other}
    finally:
        left = probe.delete_role_users(deployment, made)
        try:
            cognito.admin_delete_user(UserPoolId=deployment.pool_id, Username=new_address)
        except cognito.exceptions.UserNotFoundException:
            pass
        assert not left, f"could not delete the temporary users {left}"


def _listed(users, new_address) -> list[dict[str, str]]:
    return [
        {"name": "Integ Admin", "email": users["Admin"].username},
        {"name": "Integ Owner", "email": users["User"].username.upper()},
        {"name": "Integ Participant", "email": users["Participant"].username},
        {"name": "Integ New Attendee", "email": new_address},
    ]


@pytest.fixture(scope="module")
def meeting(deployment, users, new_address, stamp):
    """A meeting owned by the first User, created from a Virtual Participant's START."""
    session = deployment.session
    ddb = session.client("dynamodb")
    table = deployment.ai_resources["EventSourcingTable"]
    call_id = f"integ-test-participants-{stamp}"
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    owner = users["User"].username
    record = {
        "EventType": "START",
        "CallId": call_id,
        "CustomerPhoneNumber": "Integ Test Participant",
        "SystemPhoneNumber": "LMA Integ Test",
        "AgentId": owner,
        "CreatedAt": created_at,
        "AccessToken": "",
        "IdToken": "",
        "RefreshToken": "",
        # What preparationStartFields (the VP's meeting-preparation.ts) adds.
        "Participants": _listed(users, new_address),
        "ParticipantsUpdatedBy": owner,
        "ParticipantsUpdatedAt": created_at,
    }
    session.client("kinesis").put_record(
        StreamName=deployment.outputs["CallDataStreamName"],
        Data=json.dumps(record).encode(),
        PartitionKey=call_id,
    )
    found = wait_for(lambda: read_meeting(ddb, table, call_id), timeout=MEETING_RECORD_TIMEOUT_S, interval=3.0)
    try:
        assert found, f"the START for {call_id} did not create a meeting within {MEETING_RECORD_TIMEOUT_S:.0f}s"
        yield call_id, found
    finally:
        _delete_meeting(ddb, table, call_id, created_at)
        _delete_daily_counts(ddb, table, owner)


def _delete_meeting(ddb, table: str, call_id: str, created_at: str) -> None:
    """The meeting's c# items and the list row createCall writes beside it."""
    pk = {"S": f"c#{call_id}"}
    for item in ddb.query(
        TableName=table, KeyConditionExpression="PK = :pk", ExpressionAttributeValues={":pk": pk}
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


def _delete_daily_counts(ddb, table: str, username: str) -> None:
    for item in ddb.query(
        TableName=table,
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={":pk": {"S": f"usr#{username}"}, ":sk": {"S": "participant-accounts#"}},
    ).get("Items", []):
        ddb.delete_item(TableName=table, Key={"PK": item["PK"], "SK": item["SK"]})


def test_the_meeting_carries_the_participant_list_it_was_started_with(meeting, users, new_address) -> None:
    _call_id, record = meeting
    assert [dict(p) for p in record["Participants"]] == _listed(users, new_address)
    assert record["ParticipantsUpdatedBy"] == users["User"].username


def test_the_owner_reads_whether_each_attendee_has_an_account(deployment, meeting, users, new_address) -> None:
    call_id, _record = meeting
    answer = _data(
        probe.call(deployment, users["User"], GET_ACCOUNTS, {"CallId": call_id}), "getMeetingParticipantAccounts"
    )
    assert [(entry["email"].lower(), entry["account"]) for entry in answer] == [
        (users["Admin"].username.lower(), "Admin"),
        (users["User"].username.lower(), "User"),
        (users["Participant"].username.lower(), "Participant"),
        (new_address.lower(), "none"),
    ]
    assert all(set(entry) == {"name", "email", "account"} for entry in answer)


def test_another_user_and_a_participant_are_refused(deployment, meeting, users, new_address) -> None:
    call_id, _record = meeting
    calls = (
        ("Query.getMeetingParticipantAccounts", GET_ACCOUNTS, {"CallId": call_id}),
        ("Mutation.createParticipantAccounts", CREATE_ACCOUNTS, {"input": {"CallId": call_id}}),
        (
            "Mutation.updateCallParticipants",
            UPDATE_PARTICIPANTS,
            {"input": {"CallId": call_id, "Participants": []}},
        ),
    )
    for operation, document, variables in calls:
        refused = probe.call(deployment, users["Other"], document, variables)
        assert refused.get("errors") and "Unauthorized" in _error_text(refused), (operation, refused)
        assert not probe.refused_at_field(refused, operation), "a User reaches the resolver, which refuses"
        participant = probe.call(deployment, users["Participant"], document, variables)
        assert probe.refused_at_field(participant, operation), (operation, participant)
    # Nothing changed.
    still = _data(
        probe.call(deployment, users["User"], GET_ACCOUNTS, {"CallId": call_id}), "getMeetingParticipantAccounts"
    )
    assert [entry["email"] for entry in still] == [p["email"] for p in _listed(users, new_address)]


def test_the_owner_creates_a_participant_account_for_a_listed_attendee(
    deployment, meeting, users, new_address
) -> None:
    call_id, _record = meeting
    owner = users["User"]
    result = _data(
        probe.call(
            deployment, owner, CREATE_ACCOUNTS, {"input": {"CallId": call_id, "emails": [new_address.upper()]}}
        ),
        "createParticipantAccounts",
    )
    assert result["created"] == 1, result
    assert [(r["email"], r["outcome"]) for r in result["results"]] == [(new_address, "created")]
    assert isinstance(result["remainingToday"], int)

    cognito = deployment.session.client("cognito-idp")
    groups = cognito.admin_list_groups_for_user(UserPoolId=deployment.pool_id, Username=new_address)["Groups"]
    assert [g["GroupName"] for g in groups] == ["Participant"]

    again = _data(
        probe.call(deployment, owner, CREATE_ACCOUNTS, {"input": {"CallId": call_id}}), "createParticipantAccounts"
    )
    assert again["created"] == 0
    assert {r["outcome"] for r in again["results"]} == {"exists"}
    # Existing accounts are left as they were.
    admin_groups = cognito.admin_list_groups_for_user(
        UserPoolId=deployment.pool_id, Username=users["Admin"].username
    )["Groups"]
    assert "Participant" not in [g["GroupName"] for g in admin_groups]

    unlisted = probe.call(
        deployment,
        owner,
        CREATE_ACCOUNTS,
        {"input": {"CallId": call_id, "emails": [f"not-listed-{new_address}"]}},
    )
    assert "participant list" in _error_text(unlisted), unlisted

    statuses = _data(
        probe.call(deployment, owner, GET_ACCOUNTS, {"CallId": call_id}), "getMeetingParticipantAccounts"
    )
    assert statuses[-1]["account"] == "Participant"


def test_the_owner_changes_the_list(deployment, meeting, users) -> None:
    call_id, _record = meeting
    owner = users["User"]
    changed = _data(
        probe.call(
            deployment,
            owner,
            UPDATE_PARTICIPANTS,
            {
                "input": {
                    "CallId": call_id,
                    "Participants": [
                        {"name": "  Integ   Admin ", "email": users["Admin"].username},
                        {"name": "", "email": users["Admin"].username.upper()},
                    ],
                }
            },
        ),
        "updateCallParticipants",
    )
    assert changed["Participants"] == [{"name": "Integ Admin", "email": users["Admin"].username}]
    assert changed["ParticipantsUpdatedBy"] == owner.username
    refused = probe.call(
        deployment, owner, UPDATE_PARTICIPANTS, {"input": {"CallId": call_id, "Participants": [{"email": "x"}]}}
    )
    assert "not an email address" in _error_text(refused), refused


def test_a_scheduled_virtual_participant_s_list_is_read_before_its_meeting_exists(
    deployment, users, new_address
) -> None:
    owner = users["User"]
    created = probe.call(
        deployment,
        owner,
        CREATE_VP,
        {
            "input": {
                "meetingName": "integ-test (participant list, never launched)",
                "meetingPlatform": "ZOOM",
                "meetingId": "0000000000",
                "isScheduled": True,
                "status": "SCHEDULED",
                "meetingTime": int(time.time()) + 24 * 3600,
                "participants": [{"name": "Integ Admin", "email": users["Admin"].username}],
            }
        },
    )
    vp = _data(created, "createVirtualParticipant")
    vp_table = deployment.ai_resources["VirtualParticipantTable"]
    try:
        assert vp["participants"] == [{"name": "Integ Admin", "email": users["Admin"].username}]
        answer = _data(
            probe.call(deployment, owner, GET_ACCOUNTS, {"VirtualParticipantId": vp["id"]}),
            "getMeetingParticipantAccounts",
        )
        assert [(e["email"], e["account"]) for e in answer] == [(users["Admin"].username, "Admin")]
        refused = probe.call(deployment, users["Other"], GET_ACCOUNTS, {"VirtualParticipantId": vp["id"]})
        assert "Unauthorized" in _error_text(refused), refused
    finally:
        ended = probe.call(
            deployment,
            owner,
            END_VP,
            {"input": {"id": vp["id"], "endReason": "integ-test participant list: cancelled before launch"}},
        )
        deployment.session.client("dynamodb").delete_item(TableName=vp_table, Key={"id": {"S": vp["id"]}})
        assert not ended.get("errors"), ended
