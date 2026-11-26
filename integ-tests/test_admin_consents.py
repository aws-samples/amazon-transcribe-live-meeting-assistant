# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Administering consents given in advance, on a deployed stack.

See docs/meeting-consent.md#administering-consents-given-in-advance for the
behaviour asserted.

Default run (no live meeting needed, a few minutes, self-cleaning). Throwaway
users are created with a permanent password and no invitation message and
signed in through SRP: an Admin, a User who owns a meeting that lists Ana,
and Participants Ana, Bo and Cy, each of whom gives a standing consent for
every meeting on the deployment; Ana also sets a display name. Then:

``test_an_admin_lists_and_filters_everyones_consents``
    listAllAdvanceConsents finds each by the start of the address, with Ana's
    display name, and filters by scope and state; a small page has a
    NextToken that reads the next one.
``test_the_admin_operations_are_refused_to_a_user_and_a_participant``
    listAllAdvanceConsents, withdrawAdvanceConsentAsAdmin and
    deleteAdvanceConsentRecords are refused at the field.
``test_an_admin_withdrawal_reaches_a_meeting_that_counted_the_consent``
    With the runner's credentials an assumed role (the consent API's backend
    role, as the Virtual Participant's): the meeting counts Ana's consent, the
    Admin withdraws it with a note, Ana's My consents shows it withdrawn by an
    administrator with the note, and the meeting records her objection within
    a minute. Skipped otherwise.
``test_an_admin_erasure_removes_the_items_and_keeps_an_audit_entry``
    Ana's consent records are deleted: no consent item is left under her
    address and the audit entry names the consents without their text.
``test_deleting_a_user_withdraws_or_erases_their_consents``
    Bo is deleted without the option: his consent is kept, withdrawn
    (``account_deleted``). Cy is deleted with it: his consent items are gone
    and an audit entry is kept. An account created again with Bo's address
    sees none of the earlier account's consents.

Teardown deletes the users, every item under their addresses, the audit
entries this run wrote, and the meeting's record and list row. Skipped where
Meeting Consent › Allow consent given in advance is off. Needs pycognito and
graphql-core (requirements.txt).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import role_access_probe as probe
from live_meeting import read_meeting, wait_for

pytest.importorskip("pycognito")
pytest.importorskip("graphql")

MEETING_RECORD_TIMEOUT_S = 90.0
WITHDRAWAL_TIMEOUT_S = 60.0

ADMIN_FIELDS = "ConsentId Scope Email Status DisplayName WithdrawnBy WithdrawnReason WithdrawnNote CountedIn { CallId At }"
LIST_ALL = (
    "query All($Person: String, $Scope: String, $State: String, $Limit: Int, $NextToken: String) "
    "{ listAllAdvanceConsents(Person: $Person, Scope: $Scope, State: $State, Limit: $Limit, NextToken: $NextToken) "
    "{ Consents { %s } NextToken } }" % ADMIN_FIELDS
)
WITHDRAW_AS_ADMIN = (
    "mutation W($input: WithdrawAdvanceConsentAsAdminInput!) "
    "{ withdrawAdvanceConsentAsAdmin(input: $input) { %s } }" % ADMIN_FIELDS
)
ERASE = (
    "mutation E($input: DeleteAdvanceConsentRecordsInput!) "
    "{ deleteAdvanceConsentRecords(input: $input) { Deleted Withdrawn ConsentIds AuditId } }"
)
DELETE_USER = (
    "mutation D($input: DeleteUserInput!) { deleteUser(input: $input) { username success consentRecordsDeleted } }"
)
LIST_MINE = (
    "query Mine { listMyAdvanceConsents { Enabled Email "
    "Consents { ConsentId Status WithdrawnReason WithdrawnNote } } }"
)
STANDING = (
    "query S($Scope: String, $ValidUntil: String) "
    "{ getAdvanceConsentInvitation(Scope: $Scope, ValidUntil: $ValidUntil) { Enabled NoticeHash } }"
)
INVITATION = "query I($CallId: ID) { getAdvanceConsentInvitation(CallId: $CallId) { Listed Enabled NoticeHash } }"
GIVE = "mutation G($input: GiveAdvanceConsentInput!) { giveAdvanceConsent(input: $input) { ConsentId } }"
UPDATE_MY_NAMES = (
    "mutation N($input: UpdateMyAccountNamesInput!) { updateMyAccountNames(input: $input) { DisplayName } }"
)
CONSENT_FIELDS = "Status CaptureAllowed Events { ParticipantName Decision Source PreMeeting { ConsentId Withdrawn } }"
START_CONSENT = (
    "mutation Start($input: StartMeetingConsentInput!) { startMeetingConsent(input: $input) "
    "{ Consent { %s } } }" % CONSENT_FIELDS
)
LIST_PRESENT = (
    "mutation P($input: SetMeetingConsentParticipantsInput!) "
    "{ setMeetingConsentParticipants(input: $input) { Consent { %s } } }" % CONSENT_FIELDS
)
GET_CONSENT = "query G($CallId: ID!) { getMeetingConsent(CallId: $CallId) { Consent { %s } } }" % CONSENT_FIELDS


def _data(response: dict, field: str) -> Any:
    assert not response.get("errors"), response.get("errors")
    return (response.get("data") or {})[field]


def _stored(ddb, table: str, email: str) -> list[dict]:
    from boto3.dynamodb.types import TypeDeserializer  # pylint: disable=import-outside-toplevel

    des = TypeDeserializer()
    items = ddb.query(
        TableName=table,
        KeyConditionExpression="PK = :pk AND begins_with(SK, :ac)",
        ExpressionAttributeValues={":pk": {"S": f"pmc#{email.casefold()}"}, ":ac": {"S": "ac#"}},
        ConsistentRead=True,
    ).get("Items", [])
    return [{k: des.deserialize(v) for k, v in item.items()} for item in items]


def _audit(ddb, table: str, audit_id: str) -> dict | None:
    from boto3.dynamodb.types import TypeDeserializer  # pylint: disable=import-outside-toplevel

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    for item in ddb.query(
        TableName=table,
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": f"cnsaudit#{month}"}},
        ConsistentRead=True,
    ).get("Items", []):
        if item.get("AuditId", {}).get("S") == audit_id:
            return {k: TypeDeserializer().deserialize(v) for k, v in item.items()}
    return None


def _delete_partition(ddb, table: str, pk: str) -> None:
    for item in ddb.query(
        TableName=table,
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": pk}},
        ProjectionExpression="PK, SK",
    ).get("Items", []):
        ddb.delete_item(TableName=table, Key={"PK": item["PK"], "SK": item["SK"]})


@pytest.fixture(scope="module")
def deployment(client) -> probe.Deployment:
    return probe.Deployment.find(client.stack_name, client.region)


@pytest.fixture(scope="module")
def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")


@pytest.fixture(scope="module")
def run(deployment, stamp):
    """The users, the meeting listing Ana, and what the run writes, removed afterwards."""
    accounts = (
        ("admin", "Admin"),
        ("owner", "User"),
        ("ana", "Participant"),
        ("bo", "Participant"),
        ("cy", "Participant"),
    )
    made = probe.create_role_users(deployment, f"adm-{stamp}", accounts)
    users = dict(zip((label for label, _ in accounts), made))
    state: dict[str, Any] = {"users": users, "audits": [], "extra": []}
    session = deployment.session
    ddb = session.client("dynamodb")
    table = deployment.ai_resources["EventSourcingTable"]
    call_id = f"integ-test-admin-consents-{stamp}"
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    try:
        for user in made:
            probe.sign_in(deployment, user)
        page = _data(
            probe.call(
                deployment,
                users["ana"],
                STANDING,
                {"Scope": "deployment", "ValidUntil": (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")},
            ),
            "getAdvanceConsentInvitation",
        )
        if not page["Enabled"]:
            pytest.skip("Meeting Consent › Allow consent given in advance is off on this stack")
        until = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        for label in ("ana", "bo", "cy"):
            offer = _data(
                probe.call(deployment, users[label], STANDING, {"Scope": "deployment", "ValidUntil": until}),
                "getAdvanceConsentInvitation",
            )
            given = _data(
                probe.call(
                    deployment,
                    users[label],
                    GIVE,
                    {"input": {"Scope": "deployment", "ValidUntil": until, "NoticeHash": offer["NoticeHash"]}},
                ),
                "giveAdvanceConsent",
            )
            state[f"{label}_standing"] = given["ConsentId"]
        _data(
            probe.call(deployment, users["ana"], UPDATE_MY_NAMES, {"input": {"DisplayName": f"Integ Ana {stamp}"}}),
            "updateMyAccountNames",
        )
        owner = users["owner"].username
        session.client("kinesis").put_record(
            StreamName=deployment.outputs["CallDataStreamName"],
            Data=json.dumps(
                {
                    "EventType": "START",
                    "CallId": call_id,
                    "CustomerPhoneNumber": "Integ Test Admin Consents",
                    "SystemPhoneNumber": "LMA Integ Test",
                    "AgentId": owner,
                    "CreatedAt": created_at,
                    "AccessToken": "",
                    "IdToken": "",
                    "RefreshToken": "",
                    "Participants": [{"name": "Integ Ana", "email": users["ana"].username}],
                    "ParticipantsUpdatedBy": owner,
                    "ParticipantsUpdatedAt": created_at,
                }
            ).encode(),
            PartitionKey=call_id,
        )
        found = wait_for(lambda: read_meeting(ddb, table, call_id), timeout=MEETING_RECORD_TIMEOUT_S, interval=3.0)
        assert found, f"the START for {call_id} did not create a meeting"
        state["call_id"] = call_id
        yield state
    finally:
        everyone = made + state["extra"]
        # Ana's display name has a search entry (lma_consent.account_names).
        name_key = f"integ ana {stamp}"
        ddb.delete_item(
            TableName=table,
            Key={
                "PK": {"S": f"acn#{name_key[:3]}"},
                "SK": {"S": f"{name_key}\t{users['ana'].username.casefold()}"},
            },
        )
        for user in everyone:
            _delete_partition(ddb, table, f"pmc#{user.username.casefold()}")
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        for audit_id in state["audits"]:
            for item in ddb.query(
                TableName=table,
                KeyConditionExpression="PK = :pk",
                ExpressionAttributeValues={":pk": {"S": f"cnsaudit#{month}"}},
            ).get("Items", []):
                if item.get("AuditId", {}).get("S") == audit_id:
                    ddb.delete_item(TableName=table, Key={"PK": item["PK"], "SK": item["SK"]})
        for pk in (f"c#{call_id}", f"nt#{call_id}"):
            _delete_partition(ddb, table, pk)
        shard = f"{int(created_at[11:13]) // 4:02d}"
        ddb.delete_item(
            TableName=table,
            Key={"PK": {"S": f"cls#{created_at[:10]}#s#{shard}"}, "SK": {"S": f"ts#{created_at}#id#{call_id}"}},
        )
        probe.delete_role_users(deployment, everyone)
        cognito = deployment.session.client("cognito-idp")
        still = []
        for user in everyone:
            try:
                cognito.admin_get_user(UserPoolId=deployment.pool_id, Username=user.username)
                still.append(user.username)
            except cognito.exceptions.UserNotFoundException:
                pass
        assert not still, f"could not delete the temporary users {still}"


def test_an_admin_lists_and_filters_everyones_consents(deployment, run, stamp) -> None:
    admin, ana = run["users"]["admin"], run["users"]["ana"]
    prefix = ana.username.split("@", 1)[0]
    found = _data(probe.call(deployment, admin, LIST_ALL, {"Person": prefix}), "listAllAdvanceConsents")
    [mine] = [c for c in found["Consents"] if c["ConsentId"] == run["ana_standing"]]
    assert mine["Email"] == ana.username.casefold() and mine["Scope"] == "deployment"
    assert mine["DisplayName"] == f"Integ Ana {stamp}"
    assert all(c["Email"].startswith(prefix) for c in found["Consents"])
    meeting_only = _data(
        probe.call(deployment, admin, LIST_ALL, {"Person": prefix, "Scope": "meeting"}), "listAllAdvanceConsents"
    )
    assert run["ana_standing"] not in {c["ConsentId"] for c in meeting_only["Consents"]}
    withdrawn = _data(
        probe.call(deployment, admin, LIST_ALL, {"Person": prefix, "State": "withdrawn"}), "listAllAdvanceConsents"
    )
    assert run["ana_standing"] not in {c["ConsentId"] for c in withdrawn["Consents"]}
    run_prefix = "integ-rbac-"
    first = _data(probe.call(deployment, admin, LIST_ALL, {"Person": run_prefix, "Limit": 1}), "listAllAdvanceConsents")
    assert len(first["Consents"]) == 1 and first["NextToken"], first
    second = _data(
        probe.call(deployment, admin, LIST_ALL, {"Person": run_prefix, "Limit": 1, "NextToken": first["NextToken"]}),
        "listAllAdvanceConsents",
    )
    assert second["Consents"] and second["Consents"][0]["ConsentId"] != first["Consents"][0]["ConsentId"]


def test_the_admin_operations_are_refused_to_a_user_and_a_participant(deployment, run) -> None:
    ana = run["users"]["ana"].username
    calls = (
        ("Query.listAllAdvanceConsents", LIST_ALL, {}),
        (
            "Mutation.withdrawAdvanceConsentAsAdmin",
            WITHDRAW_AS_ADMIN,
            {"input": {"Email": ana, "ConsentId": run["ana_standing"]}},
        ),
        ("Mutation.deleteAdvanceConsentRecords", ERASE, {"input": {"Email": ana, "AllForPerson": True}}),
    )
    for role in ("owner", "ana"):
        for operation, document, variables in calls:
            response = probe.call(deployment, run["users"][role], document, variables)
            assert probe.refused_at_field(response, operation), (role, operation, response)


def test_an_admin_withdrawal_reaches_a_meeting_that_counted_the_consent(deployment, run) -> None:
    arn = deployment.session.client("sts").get_caller_identity()["Arn"]
    if ":assumed-role/" not in arn:
        pytest.skip("the meeting consent API treats only an assumed role as a backend role")
    from security_access_probe import sigv4_graphql  # pylint: disable=import-outside-toplevel

    def backend(document, variables):
        _status, body = sigv4_graphql(deployment.session, deployment.graphql_url, document, variables)
        assert not body.get("errors"), body
        return body["data"]

    ana, admin, call_id = run["users"]["ana"], run["users"]["admin"], run["call_id"]
    page = _data(probe.call(deployment, ana, INVITATION, {"CallId": call_id}), "getAdvanceConsentInvitation")
    given = _data(
        probe.call(deployment, ana, GIVE, {"input": {"CallId": call_id, "NoticeHash": page["NoticeHash"]}}),
        "giveAdvanceConsent",
    )["ConsentId"]
    backend(START_CONSENT, {"input": {"CallId": call_id, "Mode": "opt_in", "Rule": "each_participant"}})
    record = backend(LIST_PRESENT, {"input": {"CallId": call_id, "Participants": ["Integ Ana"]}})
    counted = record["setMeetingConsentParticipants"]["Consent"]
    assert counted["CaptureAllowed"] is True, counted
    answer = _data(
        probe.call(
            deployment, admin, WITHDRAW_AS_ADMIN, {"input": {"Email": ana.username, "ConsentId": given, "Note": "Integ note"}}
        ),
        "withdrawAdvanceConsentAsAdmin",
    )
    assert answer["Status"] == "withdrawn" and answer["WithdrawnBy"] == admin.username
    mine = _data(probe.call(deployment, ana, LIST_MINE, {}), "listMyAdvanceConsents")["Consents"]
    [seen] = [c for c in mine if c["ConsentId"] == given]
    assert (seen["WithdrawnReason"], seen["WithdrawnNote"]) == ("withdrawn_by_admin", "Integ note")

    def objected():
        consent = (backend(GET_CONSENT, {"CallId": call_id}).get("getMeetingConsent") or {}).get("Consent") or {}
        return consent if consent.get("Status") == "objected" else None

    consent = wait_for(objected, timeout=WITHDRAWAL_TIMEOUT_S, interval=3.0)
    assert consent and consent["CaptureAllowed"] is False, "the meeting did not record the objection"
    assert consent["Events"][-1]["PreMeeting"]["Withdrawn"] is True


def test_an_admin_erasure_removes_the_items_and_keeps_an_audit_entry(deployment, run) -> None:
    ddb = deployment.session.client("dynamodb")
    table = deployment.ai_resources["EventSourcingTable"]
    ana, admin = run["users"]["ana"], run["users"]["admin"]
    before = {i["ConsentId"] for i in _stored(ddb, table, ana.username)}
    assert run["ana_standing"] in before
    answer = _data(
        probe.call(deployment, admin, ERASE, {"input": {"Email": ana.username, "AllForPerson": True}}),
        "deleteAdvanceConsentRecords",
    )
    run["audits"].append(answer["AuditId"])
    assert answer["Deleted"] == len(before) and set(answer["ConsentIds"]) == before
    assert answer["Withdrawn"] >= 1, "the standing consent was in force and is withdrawn first"
    assert _stored(ddb, table, ana.username) == []
    audit = _audit(ddb, table, answer["AuditId"])
    assert audit and audit["ErasedBy"] == admin.username and int(audit["Count"]) == len(before)
    assert audit["Email"] == ana.username.casefold() and set(audit["ConsentIds"]) == before
    assert not {"NoticeText", "NoticeHash", "Scope"} & set(audit), "the audit entry keeps no consent content"


def test_deleting_a_user_withdraws_or_erases_their_consents(deployment, run, stamp) -> None:
    ddb = deployment.session.client("dynamodb")
    table = deployment.ai_resources["EventSourcingTable"]
    admin, bo, cy = run["users"]["admin"], run["users"]["bo"], run["users"]["cy"]
    kept = _data(probe.call(deployment, admin, DELETE_USER, {"input": {"username": bo.username}}), "deleteUser")
    assert kept["success"] is True and kept["consentRecordsDeleted"] is None
    [item] = [i for i in _stored(ddb, table, bo.username) if i["ConsentId"] == run["bo_standing"]]
    assert item["WithdrawnReason"] == "account_deleted"
    erased = _data(
        probe.call(deployment, admin, DELETE_USER, {"input": {"username": cy.username, "deleteConsentRecords": True}}),
        "deleteUser",
    )
    assert erased["consentRecordsDeleted"] == 1
    assert _stored(ddb, table, cy.username) == []
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    audits = [
        i
        for i in ddb.query(
            TableName=table,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": f"cnsaudit#{month}"}},
            ConsistentRead=True,
        ).get("Items", [])
        if i.get("Email", {}).get("S") == cy.username.casefold()
    ]
    assert audits and audits[-1]["Reason"]["S"] == "account_deleted"
    run["audits"] += [a["AuditId"]["S"] for a in audits]
    # A new account with Bo's address does not see the earlier account's consents.
    [again] = probe.create_role_users(deployment, f"adm-{stamp}", (("bo", "Participant"),))
    run["extra"].append(again)
    assert again.username == bo.username
    probe.sign_in(deployment, again)
    mine = _data(probe.call(deployment, again, LIST_MINE, {}), "listMyAdvanceConsents")
    assert run["bo_standing"] not in {c["ConsentId"] for c in mine["Consents"]}
