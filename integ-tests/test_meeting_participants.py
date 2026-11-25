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
``test_a_person_added_to_an_existing_meeting_s_list_is_shared_with``
    updateCallParticipants, as the owner, adds the Participant to the list of
    the meeting that already exists (another User is refused), and Meeting
    Sharing shares the meeting with them and records the share as its own.
``test_a_virtual_participant_s_page_changes_its_own_list_then_its_meeting_s``
    updateCallParticipants named by VirtualParticipantId, as a never-launched
    Virtual Participant's owner: its own list while it has no meeting (answer
    null; another User is refused), and, once its row names the meeting's
    CallId, the meeting's list, leaving its own as it was.
``test_an_admin_creates_a_user_invited_to_the_meeting``
    createUser with inviteToMeeting adds the new address to the meeting's list
    (Cognito sends its invitation to a throwaway address); an unknown meeting
    is refused.
``test_a_scheduled_virtual_participant_s_list_is_read_before_its_meeting_exists``
    A Virtual Participant scheduled a day ahead with a participant list, read
    by VirtualParticipantId; it is cancelled and its row deleted at the end.

Teardown deletes the users (including the account the test created), the
meeting record and its list row, the owner's daily account count and the
Virtual Participant row. Needs pycognito and graphql-core (requirements.txt).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import pytest
import role_access_probe as probe
from live_meeting import read_meeting, wait_for

pytest.importorskip("pycognito")
pytest.importorskip("graphql")

MEETING_RECORD_TIMEOUT_S = 90.0
#: How long Meeting Sharing (the meetings table's stream) may take to share the meeting.
SHARE_TIMEOUT_S = 90.0

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
CREATE_USER = """
mutation CreateUser($input: CreateUserInput!) {
  createUser(input: $input) { username email role }
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

    Each run sends this address one Cognito invitation. LMA_TEST_PARTICIPANT_EMAIL
    names a mailbox of the operator's choosing (a "+tag" is added per run), for
    a stack whose AllowedSignUpEmailDomain refuses example.invalid. Otherwise it
    is in the stack's allowed domain when it has one, else in the reserved
    example.invalid domain, which delivers nowhere.
    """
    chosen = os.environ.get("LMA_TEST_PARTICIPANT_EMAIL", "").strip()
    if chosen:
        local, _, domain = chosen.partition("@")
        return f"{local}+integ-{stamp}@{domain}"
    domain = deployment.email_domain if deployment.email_domain != "example.com" else "example.invalid"
    return f"integ-participant-{stamp}@{domain}"


@pytest.fixture(scope="module")
def invited_address(deployment, stamp):
    """The address an Admin creates a user for with a meeting to invite them to.

    Chosen as new_address is, with its own tag, and sent one Cognito invitation
    per run; the account is deleted afterwards.
    """
    chosen = os.environ.get("LMA_TEST_PARTICIPANT_EMAIL", "").strip()
    if chosen:
        local, _, domain = chosen.partition("@")
        address = f"{local}+integ-invited-{stamp}@{domain}"
    else:
        domain = deployment.email_domain if deployment.email_domain != "example.com" else "example.invalid"
        address = f"integ-invited-{stamp}@{domain}"
    yield address
    cognito = deployment.session.client("cognito-idp")
    try:
        cognito.admin_delete_user(UserPoolId=deployment.pool_id, Username=address)
    except cognito.exceptions.UserNotFoundException:
        pass


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
        ExpressionAttributeValues={":pk": {"S": f"usr#{username}"}, ":sk": {"S": "participant-"}},
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


def _listed_emails(deployment, call_id: str) -> list[str]:
    record = read_meeting(
        deployment.session.client("dynamodb"), deployment.ai_resources["EventSourcingTable"], call_id
    )
    return [p["email"] for p in (record or {}).get("Participants") or []]


def _shared_with(deployment, call_id: str) -> tuple[list, list]:
    record = (
        read_meeting(
            deployment.session.client("dynamodb"), deployment.ai_resources["EventSourcingTable"], call_id
        )
        or {}
    )
    shared = record.get("SharedWith") or []
    if isinstance(shared, str):
        shared = [s.strip() for s in shared.strip("[]").split(",") if s.strip()]
    return list(shared), list(record.get("AutoSharedWith") or [])


def test_a_person_added_to_an_existing_meeting_s_list_is_shared_with(deployment, meeting, users) -> None:
    """The list of a meeting that already exists is changed through the API (as the meeting page
    and the Virtual Participant's page change it, before, during or after the meeting), and
    Meeting Sharing, on by default, shares the meeting with the person added."""
    call_id, _record = meeting
    owner = users["User"]
    added = users["Participant"].username
    # As test_the_owner_changes_the_list left it.
    current = [{"name": "Integ Admin", "email": users["Admin"].username}]
    refused = probe.call(
        deployment,
        users["Other"],
        UPDATE_PARTICIPANTS,
        {"input": {"CallId": call_id, "Participants": current + [{"name": "", "email": added}]}},
    )
    assert "Unauthorized" in _error_text(refused), refused
    changed = _data(
        probe.call(
            deployment,
            owner,
            UPDATE_PARTICIPANTS,
            {"input": {"CallId": call_id, "Participants": current + [{"name": "Integ Added", "email": added}]}},
        ),
        "updateCallParticipants",
    )
    assert changed["Participants"][-1] == {"name": "Integ Added", "email": added}
    shared = wait_for(
        lambda: added in _shared_with(deployment, call_id)[0] and _shared_with(deployment, call_id),
        timeout=SHARE_TIMEOUT_S,
        interval=4.0,
    )
    assert shared, (
        f"the meeting was not shared with the person added to its list within {SHARE_TIMEOUT_S:.0f}s; "
        "is Meeting Sharing › Share meetings with their listed participants on?"
    )
    assert added in shared[1], shared


def test_a_virtual_participant_s_page_changes_its_own_list_then_its_meeting_s(deployment, meeting, users) -> None:
    """updateCallParticipants named by VirtualParticipantId changes the Virtual Participant's list
    until its meeting exists (and answers null), and the meeting's list once it does (and
    answers the meeting), leaving the Virtual Participant's list as it was."""
    call_id, _record = meeting
    owner = users["User"]
    admin = {"name": "Integ Admin", "email": users["Admin"].username}
    participant = {"name": "Integ Participant", "email": users["Participant"].username}
    vp = _data(
        probe.call(
            deployment,
            owner,
            CREATE_VP,
            {
                "input": {
                    "meetingName": "integ-test (participant list from its page, never launched)",
                    "meetingPlatform": "ZOOM",
                    "meetingId": "0000000000",
                    "isScheduled": True,
                    "status": "SCHEDULED",
                    "meetingTime": int(time.time()) + 24 * 3600,
                    "participants": [admin],
                }
            },
        ),
        "createVirtualParticipant",
    )
    ddb = deployment.session.client("dynamodb")
    vp_table = deployment.ai_resources["VirtualParticipantTable"]
    key = {"id": {"S": vp["id"]}}

    def vp_emails() -> list[str]:
        item = ddb.get_item(TableName=vp_table, Key=key, ConsistentRead=True).get("Item") or {}
        return [e["M"]["email"]["S"] for e in (item.get("participants") or {}).get("L", [])]

    try:
        before_meeting = probe.call(
            deployment,
            owner,
            UPDATE_PARTICIPANTS,
            {"input": {"VirtualParticipantId": vp["id"], "Participants": [admin, participant]}},
        )
        assert _data(before_meeting, "updateCallParticipants") is None
        assert vp_emails() == [admin["email"], participant["email"]]
        refused = probe.call(
            deployment,
            users["Other"],
            UPDATE_PARTICIPANTS,
            {"input": {"VirtualParticipantId": vp["id"], "Participants": []}},
        )
        assert "Unauthorized" in _error_text(refused), refused

        # What the Virtual Participant does when it starts: link its meeting's CallId.
        ddb.update_item(
            TableName=vp_table,
            Key=key,
            UpdateExpression="SET CallId = :c",
            ExpressionAttributeValues={":c": {"S": call_id}},
        )
        on_meeting = _data(
            probe.call(
                deployment,
                owner,
                UPDATE_PARTICIPANTS,
                {"input": {"VirtualParticipantId": vp["id"], "Participants": [admin]}},
            ),
            "updateCallParticipants",
        )
        assert on_meeting["CallId"] == call_id
        assert [p["email"] for p in on_meeting["Participants"]] == [admin["email"]]
        # The record is read without a consistent read, so allow it a moment.
        assert wait_for(lambda: _listed_emails(deployment, call_id) == [admin["email"]], timeout=15.0, interval=1.0)
        assert vp_emails() == [admin["email"], participant["email"]]
    finally:
        # Unlinked first, so ending the never-launched Virtual Participant touches no meeting.
        ddb.update_item(TableName=vp_table, Key=key, UpdateExpression="REMOVE CallId")
        ended = probe.call(
            deployment,
            owner,
            END_VP,
            {"input": {"id": vp["id"], "endReason": "integ-test participant list: cancelled before launch"}},
        )
        ddb.delete_item(TableName=vp_table, Key=key)
        assert not ended.get("errors"), ended


def test_an_admin_creates_a_user_invited_to_the_meeting(deployment, meeting, users, invited_address) -> None:
    """createUser with inviteToMeeting: the account is made (Cognito sends its invitation, with
    the meeting's consent link, to a throwaway address) and the address is added to the list."""
    call_id, _record = meeting
    created = _data(
        probe.call(
            deployment,
            users["Admin"],
            CREATE_USER,
            {"input": {"email": invited_address, "role": "Participant", "inviteToMeeting": call_id}},
        ),
        "createUser",
    )
    assert created["email"].casefold() == invited_address.casefold()
    assert created["role"] == "Participant"
    assert wait_for(
        lambda: invited_address.casefold() in [e.casefold() for e in _listed_emails(deployment, call_id)],
        timeout=15.0,
        interval=1.0,
    ), _listed_emails(deployment, call_id)
    unknown = probe.call(
        deployment,
        users["Admin"],
        CREATE_USER,
        {"input": {"email": f"x-{invited_address}", "role": "User", "inviteToMeeting": "integ-no-such-meeting"}},
    )
    assert "no meeting or Virtual Participant" in _error_text(unknown), unknown


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
