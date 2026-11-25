# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Consent given in advance, on a deployed stack.

See docs/meeting-consent.md#consent-given-in-advance for the behaviour asserted.

Default run (no live meeting needed, a few minutes, self-cleaning). Three
throwaway users are created with a permanent password and no invitation
message, and signed in through SRP as the web UI signs in: a User who owns the
meeting, a Participant on its participant list, and a Participant who is not.
A meeting is created from the START a Virtual Participant started with a
participant list sends, and given a label. Then:

``test_the_listed_participant_reads_the_meetings_consent_page``
    getAdvanceConsentInvitation names the meeting, its organizer and the exact
    text the Participant would agree to, with its hash.
``test_the_listed_participant_consents_for_the_meeting``
    giveAdvanceConsent stores the consent under the Participant's own address:
    their account's sub, the scope, the times and the text with its hash, and
    no device details unless the deployment's setting keeps them.
``test_the_listed_participant_gives_and_withdraws_a_standing_consent``
    A consent for the organizer's meetings with the label, until a date within
    a year, is listed on My consents and then withdrawn; the record stays,
    marked withdrawn, and no longer covers the meeting.
``test_a_participant_not_on_the_list_learns_nothing_and_can_ask_to_be_added``
    The unlisted Participant is told only that they are not listed, cannot
    give consent, and asks to be added: the owner gets an in-app notification
    on the meeting, and nothing is stored about the Participant's consent.
``test_a_meeting_counts_a_consent_given_in_advance_and_a_withdrawal_as_an_objection``
    What a Virtual Participant does, without one: the runner's assumed role
    starts the meeting's consent (opt-in) and lists the attendee by a display
    name matching the list; the consent given in advance is recorded for them
    (source pre_meeting, basis display_name) and capture is allowed, an
    unlisted attendee withholds it again, and withdrawing the consent records
    an objection on the meeting within a minute. Skipped unless the runner's
    credentials are an assumed role, which the consent API treats as a backend
    role.

What the Virtual Participant posts in the chat, and its matching of a live
roster, need a live meeting and are not part of this run.

Teardown deletes the users, the consents and the request by their keys, and
the meeting's record, list row and notifications. Needs pycognito and
graphql-core (requirements.txt).
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import role_access_probe as probe
from live_meeting import read_meeting, wait_for

pytest.importorskip("pycognito")
pytest.importorskip("graphql")

MEETING_RECORD_TIMEOUT_S = 90.0
NOTIFICATION_TIMEOUT_S = 30.0

CONSENT_FIELDS = (
    "ConsentId Scope CallId Organizer LabelKey LabelValue GrantedAt ValidUntil "
    "WithdrawnAt WithdrawnReason NoticeText NoticeHash NoticeVersion Source Email Status "
    "DeviceDetailsKept"
)
INVITATION = """
query Invitation($CallId: ID, $Scope: String, $ValidUntil: String, $LabelKey: String) {
  getAdvanceConsentInvitation(CallId: $CallId, Scope: $Scope, ValidUntil: $ValidUntil, LabelKey: $LabelKey) {
    Listed Requested Enabled Email ListedName CallId MeetingName Organizer
    Labels { Key Value }
    Scope ValidUntil LabelKey LabelValue NoticeText NoticeHash MaxDays
    ActiveConsents { ConsentId Scope }
  }
}
"""
GIVE = """
mutation Give($input: GiveAdvanceConsentInput!) { giveAdvanceConsent(input: $input) { %s } }
""" % CONSENT_FIELDS
WITHDRAW = """
mutation Withdraw($input: WithdrawAdvanceConsentInput!) { withdrawAdvanceConsent(input: $input) { %s } }
""" % CONSENT_FIELDS
LIST_MINE = """
query Mine { listMyAdvanceConsents { Enabled MaxDays Email Consents { %s } } }
""" % CONSENT_FIELDS
ASK = """
mutation Ask($input: AskToBeAddedToMeetingInput!) { askToBeAddedToMeeting(input: $input) { Requested Listed } }
"""
NOTIFICATIONS = """
query Notifications($CallId: ID!) {
  listMeetingNotifications(CallId: $CallId, Category: "meeting") { Items { Type Title Body DedupKey } }
}
"""

LABEL = {"Key": "engagement", "Value": "integ-pre-meeting"}


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
        for label, role in (("owner", "User"), ("listed", "Participant"), ("unlisted", "Participant")):
            user = probe.RoleUser(
                role,
                f"integ-pmc-{label}-{stamp}@{deployment.email_domain}",
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


@pytest.fixture(scope="module")
def meeting(deployment, users, stamp):
    """A meeting owned by the User, listing the listed Participant, with a label."""
    session = deployment.session
    ddb = session.client("dynamodb")
    table = deployment.ai_resources["EventSourcingTable"]
    call_id = f"integ-test-pre-meeting-consent-{stamp}"
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    owner = users["owner"].username
    session.client("kinesis").put_record(
        StreamName=deployment.outputs["CallDataStreamName"],
        Data=json.dumps(
            {
                "EventType": "START",
                "CallId": call_id,
                "CustomerPhoneNumber": "Integ Test Pre-meeting Consent",
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
            }
        ).encode(),
        PartitionKey=call_id,
    )
    try:
        found = wait_for(
            lambda: read_meeting(ddb, table, call_id),
            timeout=MEETING_RECORD_TIMEOUT_S,
            interval=3.0,
        )
        assert found, (
            f"the START for {call_id} did not create a meeting within {MEETING_RECORD_TIMEOUT_S:.0f}s"
        )
        pk = {"S": f"c#{call_id}"}
        ddb.update_item(
            TableName=table,
            Key={"PK": pk, "SK": pk},
            UpdateExpression="SET Labels = :l",
            ExpressionAttributeValues={
                ":l": {"L": [{"M": {"Key": {"S": LABEL["Key"]}, "Value": {"S": LABEL["Value"]}}}]}
            },
        )
        yield call_id
    finally:
        _delete_everything(ddb, table, call_id, created_at, users)


def _delete_partition(ddb, table: str, pk: str) -> None:
    for item in ddb.query(
        TableName=table,
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": pk}},
        ProjectionExpression="PK, SK",
    ).get("Items", []):
        ddb.delete_item(TableName=table, Key={"PK": item["PK"], "SK": item["SK"]})


def _delete_everything(ddb, table: str, call_id: str, created_at: str, users) -> None:
    """Each item the test made, by its partition or key, never by a scan."""
    for user in users.values():
        _delete_partition(ddb, table, f"pmc#{user.username.casefold()}")
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ddb.delete_item(
            TableName=table,
            Key={"PK": {"S": f"usr#{user.username}"}, "SK": {"S": f"ask-to-be-added#{day}"}},
        )
    for pk in (f"c#{call_id}", f"pmr#c#{call_id}", f"nt#{call_id}"):
        _delete_partition(ddb, table, pk)
    shard = f"{int(created_at[11:13]) // 4:02d}"
    ddb.delete_item(
        TableName=table,
        Key={
            "PK": {"S": f"cls#{created_at[:10]}#s#{shard}"},
            "SK": {"S": f"ts#{created_at}#id#{call_id}"},
        },
    )


def _stored(deployment, email: str) -> list[dict]:
    from boto3.dynamodb.types import TypeDeserializer  # pylint: disable=import-outside-toplevel

    ddb = deployment.session.client("dynamodb")
    items = ddb.query(
        TableName=deployment.ai_resources["EventSourcingTable"],
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": f"pmc#{email.casefold()}"}},
        ConsistentRead=True,
    ).get("Items", [])
    des = TypeDeserializer()
    return [{k: des.deserialize(v) for k, v in item.items()} for item in items]


def test_the_listed_participant_reads_the_meetings_consent_page(deployment, users, meeting) -> None:
    listed = users["listed"]
    page = _data(
        probe.call(deployment, listed, INVITATION, {"CallId": meeting}),
        "getAdvanceConsentInvitation",
    )
    if not page["Enabled"]:
        pytest.skip("Meeting Consent › Allow consent given in advance is off on this stack")
    assert page["Listed"] is True
    assert page["Email"] == listed.username.casefold()
    assert page["ListedName"] == "Integ Listed"
    assert page["MeetingName"] == meeting and page["Organizer"] == users["owner"].username
    assert page["Labels"] == [LABEL]
    assert page["Scope"] == "meeting"
    assert users["owner"].username in page["NoticeText"] and meeting in page["NoticeText"]
    assert page["NoticeHash"] == hashlib.sha256(page["NoticeText"].encode()).hexdigest()


def test_the_listed_participant_consents_for_the_meeting(deployment, users, meeting) -> None:
    listed = users["listed"]
    page = _data(
        probe.call(deployment, listed, INVITATION, {"CallId": meeting}),
        "getAdvanceConsentInvitation",
    )
    if not page["Enabled"]:
        pytest.skip("Meeting Consent › Allow consent given in advance is off on this stack")
    # A hash for any other text is refused, and nothing is stored.
    refused = probe.call(
        deployment, listed, GIVE, {"input": {"CallId": meeting, "NoticeHash": "0" * 64}}
    )
    assert "changed" in _errors(refused), refused
    given = _data(
        probe.call(
            deployment, listed, GIVE, {"input": {"CallId": meeting, "NoticeHash": page["NoticeHash"]}}
        ),
        "giveAdvanceConsent",
    )
    assert given["Scope"] == "meeting" and given["CallId"] == meeting
    assert given["Source"] == "pre_meeting" and given["Status"] == "active"
    assert given["NoticeText"] == page["NoticeText"] and given["NoticeHash"] == page["NoticeHash"]
    [item] = [i for i in _stored(deployment, listed.username) if i["ConsentId"] == given["ConsentId"]]
    sub = probe.claims(listed.tokens["id"])["sub"]
    assert item["Subject"] == sub and item["Email"] == listed.username.casefold()
    assert item["NoticeText"] == page["NoticeText"]
    assert ("DeviceDetails" in item) is bool(given["DeviceDetailsKept"])
    granted = datetime.fromisoformat(item["GrantedAt"].replace("Z", "+00:00"))
    until = datetime.fromisoformat(item["ValidUntil"].replace("Z", "+00:00"))
    assert until - granted <= timedelta(days=365)
    assert int(item["ExpiresAfter"]) > until.timestamp()
    covering = _data(
        probe.call(deployment, listed, INVITATION, {"CallId": meeting}),
        "getAdvanceConsentInvitation",
    )["ActiveConsents"]
    assert [c["ConsentId"] for c in covering] == [given["ConsentId"]]


def test_the_listed_participant_gives_and_withdraws_a_standing_consent(
    deployment, users, meeting
) -> None:
    listed = users["listed"]
    first = _data(
        probe.call(deployment, listed, INVITATION, {"CallId": meeting}),
        "getAdvanceConsentInvitation",
    )
    if not first["Enabled"]:
        pytest.skip("Meeting Consent › Allow consent given in advance is off on this stack")
    too_late = (datetime.now(timezone.utc) + timedelta(days=first["MaxDays"] + 1)).strftime(
        "%Y-%m-%d"
    )
    late = probe.call(
        deployment,
        listed,
        INVITATION,
        {"CallId": meeting, "Scope": "organizer", "ValidUntil": too_late},
    )
    assert "at most" in _errors(late), late
    until = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    variables = {
        "CallId": meeting,
        "Scope": "organizer",
        "ValidUntil": until,
        "LabelKey": LABEL["Key"],
    }
    page = _data(
        probe.call(deployment, listed, INVITATION, variables), "getAdvanceConsentInvitation"
    )
    assert f"until the end of {until} (UTC)" in page["NoticeText"]
    assert LABEL["Value"] in page["NoticeText"]
    standing = _data(
        probe.call(
            deployment,
            listed,
            GIVE,
            {"input": {**variables, "NoticeHash": page["NoticeHash"]}},
        ),
        "giveAdvanceConsent",
    )
    assert standing["Scope"] == "organizer" and standing["Organizer"] == users["owner"].username
    assert (standing["LabelKey"], standing["LabelValue"]) == (LABEL["Key"], LABEL["Value"])
    assert standing["ValidUntil"].startswith(until)
    mine = _data(probe.call(deployment, listed, LIST_MINE, {}), "listMyAdvanceConsents")
    assert standing["ConsentId"] in {c["ConsentId"] for c in mine["Consents"]}
    withdrawn = _data(
        probe.call(deployment, listed, WITHDRAW, {"input": {"ConsentId": standing["ConsentId"]}}),
        "withdrawAdvanceConsent",
    )
    assert withdrawn["Status"] == "withdrawn" and withdrawn["WithdrawnAt"]
    # Nobody else can withdraw it, or see it.
    other = probe.call(
        deployment, users["unlisted"], WITHDRAW, {"input": {"ConsentId": standing["ConsentId"]}}
    )
    assert "not one of your consents" in _errors(other), other
    theirs = _data(probe.call(deployment, users["unlisted"], LIST_MINE, {}), "listMyAdvanceConsents")
    assert standing["ConsentId"] not in {c["ConsentId"] for c in theirs["Consents"]}
    # The record stays, marked withdrawn, and no longer covers the meeting.
    [item] = [
        i for i in _stored(deployment, listed.username) if i["ConsentId"] == standing["ConsentId"]
    ]
    assert item["WithdrawnAt"] and item["WithdrawnReason"] == "withdrawn"
    covering = _data(
        probe.call(deployment, listed, INVITATION, {"CallId": meeting}),
        "getAdvanceConsentInvitation",
    )["ActiveConsents"]
    assert standing["ConsentId"] not in {c["ConsentId"] for c in covering}


def test_a_participant_not_on_the_list_learns_nothing_and_can_ask_to_be_added(
    deployment, users, meeting
) -> None:
    unlisted = users["unlisted"]
    page = _data(
        probe.call(deployment, unlisted, INVITATION, {"CallId": meeting}),
        "getAdvanceConsentInvitation",
    )
    if not page["Enabled"]:
        pytest.skip("Meeting Consent › Allow consent given in advance is off on this stack")
    assert page["Listed"] is False and page["Requested"] is False
    assert page["MeetingName"] is None and page["Organizer"] is None and page["NoticeText"] is None
    refused = probe.call(deployment, unlisted, GIVE, {"input": {"CallId": meeting, "NoticeHash": "x"}})
    assert "not on this meeting's participant list" in _errors(refused), refused
    asked = _data(
        probe.call(
            deployment, unlisted, ASK, {"input": {"CallId": meeting, "Name": "Integ Unlisted"}}
        ),
        "askToBeAddedToMeeting",
    )
    assert asked == {"Requested": True, "Listed": False}

    def owner_notified():
        items = _data(
            probe.call(deployment, users["owner"], NOTIFICATIONS, {"CallId": meeting}),
            "listMeetingNotifications",
        )["Items"]
        return [i for i in items or [] if i["Type"] == "participant_list_request"]

    notified = wait_for(owner_notified, timeout=NOTIFICATION_TIMEOUT_S, interval=3.0)
    assert notified, "the owner was not notified that someone asked to be added"
    digest = hashlib.sha256(unlisted.username.casefold().encode()).hexdigest()[:16]
    assert notified[0]["DedupKey"] == f"participant_list_request:{digest}"
    # Asking again tells nobody again, and the page says it was asked.
    probe.call(deployment, unlisted, ASK, {"input": {"CallId": meeting}})
    time.sleep(3)
    assert len(owner_notified()) == 1
    again = _data(
        probe.call(deployment, unlisted, INVITATION, {"CallId": meeting}),
        "getAdvanceConsentInvitation",
    )
    assert again["Requested"] is True
    assert _stored(deployment, unlisted.username) == []


CONSENT_FIELDS_ON_RECORD = (
    "Mode Status CaptureAllowed Participants "
    "Events { ParticipantName Decision Source PreMeeting { ConsentId Scope MatchBasis Withdrawn } }"
)
START_CONSENT = (
    "mutation Start($input: StartMeetingConsentInput!) { startMeetingConsent(input: $input) "
    "{ Consent { %s } } }" % CONSENT_FIELDS_ON_RECORD
)
LIST_PRESENT = (
    "mutation Present($input: SetMeetingConsentParticipantsInput!) "
    "{ setMeetingConsentParticipants(input: $input) { Consent { %s } } }" % CONSENT_FIELDS_ON_RECORD
)
GET_CONSENT = "query Get($CallId: ID!) { getMeetingConsent(CallId: $CallId) { Consent { %s } } }" % (
    CONSENT_FIELDS_ON_RECORD
)
WITHDRAWAL_TIMEOUT_S = 60.0


def _backend(deployment, document: str, variables: dict) -> dict:
    """A call signed as LMA's backend roles sign theirs (an assumed role's credentials)."""
    from security_access_probe import sigv4_graphql  # pylint: disable=import-outside-toplevel

    _status, body = sigv4_graphql(deployment.session, deployment.graphql_url, document, variables)
    return body


def test_a_meeting_counts_a_consent_given_in_advance_and_a_withdrawal_as_an_objection(
    deployment, users, meeting
) -> None:
    """What a Virtual Participant does in the meeting, without one: the runner's
    assumed role lists the attendees as the Virtual Participant's role does."""
    arn = deployment.session.client("sts").get_caller_identity()["Arn"]
    if ":assumed-role/" not in arn:
        pytest.skip("the meeting consent API treats only an assumed role as a backend role")
    listed = users["listed"]
    page = _data(
        probe.call(deployment, listed, INVITATION, {"CallId": meeting}),
        "getAdvanceConsentInvitation",
    )
    if not page["Enabled"]:
        pytest.skip("Meeting Consent › Allow consent given in advance is off on this stack")
    given = _data(
        probe.call(
            deployment, listed, GIVE, {"input": {"CallId": meeting, "NoticeHash": page["NoticeHash"]}}
        ),
        "giveAdvanceConsent",
    )
    started = _backend(
        deployment,
        START_CONSENT,
        {"input": {"CallId": meeting, "Mode": "opt_in", "Rule": "each_participant"}},
    )
    assert not started.get("errors"), started
    present = _backend(
        deployment, LIST_PRESENT, {"input": {"CallId": meeting, "Participants": ["integ  LISTED"]}}
    )
    assert not present.get("errors"), present
    record = present["data"]["setMeetingConsentParticipants"]["Consent"]
    [event] = [e for e in record["Events"] if e["Source"] == "pre_meeting"]
    assert (event["ParticipantName"], event["Decision"]) == ("integ  LISTED", "consent")
    assert event["PreMeeting"]["ConsentId"] == given["ConsentId"]
    assert event["PreMeeting"]["MatchBasis"] == "display_name"
    assert record["CaptureAllowed"] is True, "everyone present is covered"
    # An attendee nobody listed is asked as usual.
    present = _backend(
        deployment,
        LIST_PRESENT,
        {"input": {"CallId": meeting, "Participants": ["integ  LISTED", "Somebody Else"]}},
    )
    assert present["data"]["setMeetingConsentParticipants"]["Consent"]["CaptureAllowed"] is False
    _backend(deployment, LIST_PRESENT, {"input": {"CallId": meeting, "Participants": ["integ  LISTED"]}})
    # Withdrawn on My consents: the meeting records the attendee's objection.
    _data(
        probe.call(deployment, listed, WITHDRAW, {"input": {"ConsentId": given["ConsentId"]}}),
        "withdrawAdvanceConsent",
    )

    def objected():
        body = _backend(deployment, GET_CONSENT, {"CallId": meeting})
        consent = ((body.get("data") or {}).get("getMeetingConsent") or {}).get("Consent") or {}
        return consent if consent.get("Status") == "objected" else None

    consent = wait_for(objected, timeout=WITHDRAWAL_TIMEOUT_S, interval=3.0)
    assert consent, "the withdrawal was not recorded on the meeting as an objection"
    assert consent["CaptureAllowed"] is False
    last = consent["Events"][-1]
    assert (last["Decision"], last["Source"], last["PreMeeting"]["Withdrawn"]) == (
        "objection",
        "pre_meeting",
        True,
    )
