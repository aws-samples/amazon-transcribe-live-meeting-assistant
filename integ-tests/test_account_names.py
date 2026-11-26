# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""The names accounts go by in meetings, and the participant list's account search, on a deployed stack.

See docs/meeting-consent.md#the-match-is-by-display-name and
docs/virtual-participant.md#participant-list for the behaviour asserted.

Default run (no live meeting needed, a few minutes, self-cleaning). Five
throwaway users are created with a permanent password and no invitation
message, and signed in through SRP as the web UI signs in: an Admin, a User who
owns the meeting, and three Participants (Ana, Bo and Cy) on its participant
list by address only, with no names. Then:

``test_a_person_sets_their_own_names_and_an_admin_sets_another_users``
    Ana sets her display name and a short name on My consents
    (updateMyAccountNames) and reads them back; the Admin sets Bo's
    (setUserAccountNames); Cy sets a short name that Bo goes by too.
``test_the_account_search_finds_accounts_by_the_start_of_a_name_or_address``
    The owner finds Ana by the start of her display name, Bo by his short name
    and Cy by the start of his address, each with the display name and address
    only; the middle of a name and fewer than three characters find nothing;
    a Participant is refused.
``test_attendees_are_matched_by_their_accounts_names``
    What a Virtual Participant does, without one: the runner's assumed role
    starts the meeting's consent (opt-in) and lists the attendees. An attendee
    showing the short name both Bo and Cy go by is not matched; Ana, shown by
    her display name, and Bo, by his own short name, are recorded as consented
    under the consents they gave in advance, with ``MatchedNameSource``
    ``display_name`` and ``short_name``, and capture is allowed. Skipped unless
    the runner's credentials are an assumed role, which the consent API treats
    as a backend role.

Teardown clears each user's names, deletes the users, their consents, the
names and their search entries by their keys, and the meeting's record and
list row. Needs pycognito and graphql-core (requirements.txt).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import pytest
import role_access_probe as probe
from live_meeting import read_meeting, wait_for

pytest.importorskip("pycognito")
pytest.importorskip("graphql")

MEETING_RECORD_TIMEOUT_S = 90.0

NAMES = "DisplayName ShortNames AccountName"
UPDATE_MY_NAMES = (
    "mutation Names($input: UpdateMyAccountNamesInput!) { updateMyAccountNames(input: $input) { %s } }"
    % NAMES
)
LIST_MINE = "query Mine { listMyAdvanceConsents { Enabled Email Names { %s } } }" % NAMES
SET_USER_NAMES = (
    "mutation Set($input: SetUserAccountNamesInput!) "
    "{ setUserAccountNames(input: $input) { username email displayName shortNames } }"
)
SEARCH = "query Search($text: String!) { searchAccounts(text: $text) { name email } }"
INVITATION = (
    "query Invitation($CallId: ID) { getAdvanceConsentInvitation(CallId: $CallId) "
    "{ Listed Enabled NoticeHash } }"
)
GIVE = (
    "mutation Give($input: GiveAdvanceConsentInput!) "
    "{ giveAdvanceConsent(input: $input) { ConsentId Status } }"
)
CONSENT_FIELDS = (
    "Mode Status CaptureAllowed "
    "Events { ParticipantName Decision Source "
    "PreMeeting { ConsentId MatchBasis MatchedNameSource ListedName } }"
)
START_CONSENT = (
    "mutation Start($input: StartMeetingConsentInput!) { startMeetingConsent(input: $input) "
    "{ Consent { %s } } }" % CONSENT_FIELDS
)
LIST_PRESENT = (
    "mutation Present($input: SetMeetingConsentParticipantsInput!) "
    "{ setMeetingConsentParticipants(input: $input) { Consent { %s } } }" % CONSENT_FIELDS
)


def _data(response: dict, field: str) -> Any:
    assert not response.get("errors"), response.get("errors")
    return (response.get("data") or {})[field]


def _errors(response: dict) -> str:
    return " ".join(e.get("message", "") for e in response.get("errors") or [])


def _key(name: str) -> str:
    """A name as the search entries key it (lma_consent.rules.normalize_name, near enough here)."""
    return re.sub(r"\s+", " ", name.casefold()).strip()


@pytest.fixture(scope="module")
def deployment(client) -> probe.Deployment:
    return probe.Deployment.find(client.stack_name, client.region)


@pytest.fixture(scope="module")
def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")


@pytest.fixture(scope="module")
def names(stamp) -> dict:
    """The names set in this run, unique to it so the search and the match find only these."""
    return {
        "ana_display": f"Integ Ana {stamp}",
        "ana_short": f"Anita{stamp}",
        "bo_display": f"Integ Bo {stamp}",
        "bo_short": f"Bobby{stamp}",
        "shared": f"Integ Shared {stamp}",
    }


@pytest.fixture(scope="module")
def users(deployment, stamp, names):
    accounts = (
        ("admin", "Admin"),
        ("owner", "User"),
        ("ana", "Participant"),
        ("bo", "Participant"),
        ("cy", "Participant"),
    )
    made = probe.create_role_users(deployment, f"names-{stamp}", accounts)
    by_label = dict(zip((label for label, _ in accounts), made))
    try:
        for user in made:
            probe.sign_in(deployment, user)
        yield by_label
    finally:
        ddb = deployment.session.client("dynamodb")
        table = deployment.ai_resources["EventSourcingTable"]
        for user in made:
            try:
                probe.call(deployment, user, UPDATE_MY_NAMES, {"input": {"DisplayName": "", "ShortNames": []}})
            except Exception:  # noqa: BLE001 - the keys are deleted below either way
                pass
            _delete_partition(ddb, table, f"pmc#{user.username.casefold()}")
            for name in names.values():
                key = _key(name)
                ddb.delete_item(
                    TableName=table,
                    Key={"PK": {"S": f"acn#{key[:3]}"}, "SK": {"S": f"{key}\t{user.username.casefold()}"}},
                )
        left = probe.delete_role_users(deployment, made)
        assert not left, f"could not delete the temporary users {left}"


def _delete_partition(ddb, table: str, pk: str) -> None:
    for item in ddb.query(
        TableName=table,
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": {"S": pk}},
        ProjectionExpression="PK, SK",
    ).get("Items", []):
        ddb.delete_item(TableName=table, Key={"PK": item["PK"], "SK": item["SK"]})


@pytest.fixture(scope="module")
def meeting(deployment, users, stamp):
    """A meeting owned by the User, listing Ana, Bo and Cy by address only."""
    session = deployment.session
    ddb = session.client("dynamodb")
    table = deployment.ai_resources["EventSourcingTable"]
    call_id = f"integ-test-account-names-{stamp}"
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    owner = users["owner"].username
    session.client("kinesis").put_record(
        StreamName=deployment.outputs["CallDataStreamName"],
        Data=json.dumps(
            {
                "EventType": "START",
                "CallId": call_id,
                "CustomerPhoneNumber": "Integ Test Account Names",
                "SystemPhoneNumber": "LMA Integ Test",
                "AgentId": owner,
                "CreatedAt": created_at,
                "AccessToken": "",
                "IdToken": "",
                "RefreshToken": "",
                "Participants": [{"name": "", "email": users[label].username} for label in ("ana", "bo", "cy")],
                "ParticipantsUpdatedBy": owner,
                "ParticipantsUpdatedAt": created_at,
            }
        ).encode(),
        PartitionKey=call_id,
    )
    try:
        found = wait_for(lambda: read_meeting(ddb, table, call_id), timeout=MEETING_RECORD_TIMEOUT_S, interval=3.0)
        assert found, f"the START for {call_id} did not create a meeting within {MEETING_RECORD_TIMEOUT_S:.0f}s"
        yield call_id
    finally:
        for pk in (f"c#{call_id}", f"nt#{call_id}"):
            _delete_partition(ddb, table, pk)
        shard = f"{int(created_at[11:13]) // 4:02d}"
        ddb.delete_item(
            TableName=table,
            Key={
                "PK": {"S": f"cls#{created_at[:10]}#s#{shard}"},
                "SK": {"S": f"ts#{created_at}#id#{call_id}"},
            },
        )


def test_a_person_sets_their_own_names_and_an_admin_sets_another_users(deployment, users, names) -> None:
    ana = users["ana"]
    mine = _data(
        probe.call(
            deployment,
            ana,
            UPDATE_MY_NAMES,
            {"input": {"DisplayName": f"  {names['ana_display']} ", "ShortNames": [names["ana_short"]]}},
        ),
        "updateMyAccountNames",
    )
    assert (mine["DisplayName"], mine["ShortNames"]) == (names["ana_display"], [names["ana_short"]])
    read = _data(probe.call(deployment, ana, LIST_MINE, {}), "listMyAdvanceConsents")["Names"]
    assert read["DisplayName"] == names["ana_display"]
    # Names outside the limits are refused.
    refused = probe.call(
        deployment, ana, UPDATE_MY_NAMES, {"input": {"ShortNames": ["a1", "b2", "c3", "d4", "e5", "f6"]}}
    )
    assert "At most 5" in _errors(refused), refused
    bo = _data(
        probe.call(
            deployment,
            users["admin"],
            SET_USER_NAMES,
            {
                "input": {
                    "username": users["bo"].username,
                    "displayName": names["bo_display"],
                    "shortNames": [names["bo_short"], names["shared"]],
                }
            },
        ),
        "setUserAccountNames",
    )
    assert bo["displayName"] == names["bo_display"]
    assert bo["shortNames"] == [names["bo_short"], names["shared"]]
    # Bo sees what the Admin set for him.
    theirs = _data(probe.call(deployment, users["bo"], LIST_MINE, {}), "listMyAdvanceConsents")["Names"]
    assert theirs["DisplayName"] == names["bo_display"]
    _data(
        probe.call(deployment, users["cy"], UPDATE_MY_NAMES, {"input": {"ShortNames": [names["shared"]]}}),
        "updateMyAccountNames",
    )


def test_the_account_search_finds_accounts_by_the_start_of_a_name_or_address(deployment, users, names) -> None:
    owner = users["owner"]

    def search(text: str, who=owner) -> dict:
        return probe.call(deployment, who, SEARCH, {"text": text})

    found = _data(search(names["ana_display"][:-2]), "searchAccounts")
    assert found == [{"name": names["ana_display"], "email": users["ana"].username.casefold()}]
    found = _data(search(names["bo_short"].upper()), "searchAccounts")
    assert found == [{"name": names["bo_display"], "email": users["bo"].username.casefold()}]
    cy = users["cy"].username.casefold()
    assert {a["email"] for a in _data(search(cy.split("@")[0]), "searchAccounts")} == {cy}
    # The middle of a name is not its start.
    assert _data(search(names["ana_display"].split(" ", 1)[1]), "searchAccounts") == []
    assert "at least 3" in _errors(search("in")), "two characters must be refused"
    refused = search(names["ana_display"], who=users["ana"])
    assert probe.refused_at_field(refused, "Query.searchAccounts"), refused


def test_attendees_are_matched_by_their_accounts_names(deployment, users, names, meeting) -> None:
    arn = deployment.session.client("sts").get_caller_identity()["Arn"]
    if ":assumed-role/" not in arn:
        pytest.skip("the meeting consent API treats only an assumed role as a backend role")
    from security_access_probe import sigv4_graphql  # pylint: disable=import-outside-toplevel

    def backend(document: str, variables: dict) -> dict:
        _status, body = sigv4_graphql(deployment.session, deployment.graphql_url, document, variables)
        assert not body.get("errors"), body
        return body["data"]

    given = {}
    for label in ("ana", "bo", "cy"):
        page = _data(
            probe.call(deployment, users[label], INVITATION, {"CallId": meeting}), "getAdvanceConsentInvitation"
        )
        if not page["Enabled"]:
            pytest.skip("Meeting Consent › Allow consent given in advance is off on this stack")
        assert page["Listed"] is True, f"{label} is listed by address, with no name"
        given[label] = _data(
            probe.call(
                deployment,
                users[label],
                GIVE,
                {"input": {"CallId": meeting, "NoticeHash": page["NoticeHash"]}},
            ),
            "giveAdvanceConsent",
        )["ConsentId"]
    backend(START_CONSENT, {"input": {"CallId": meeting, "Mode": "opt_in", "Rule": "each_participant"}})

    # A name two listed people go by matches nobody.
    record = backend(LIST_PRESENT, {"input": {"CallId": meeting, "Participants": [names["shared"]]}})
    record = record["setMeetingConsentParticipants"]["Consent"]
    assert [e for e in record["Events"] if e["Source"] == "pre_meeting"] == []
    assert record["CaptureAllowed"] is False

    ana_shown, bo_shown = names["ana_display"].upper(), names["bo_short"]
    record = backend(LIST_PRESENT, {"input": {"CallId": meeting, "Participants": [ana_shown, bo_shown]}})
    record = record["setMeetingConsentParticipants"]["Consent"]
    events = {e["ParticipantName"]: e for e in record["Events"] if e["Source"] == "pre_meeting"}
    assert set(events) == {ana_shown, bo_shown}, events
    assert events[ana_shown]["PreMeeting"]["ConsentId"] == given["ana"]
    assert events[ana_shown]["PreMeeting"]["MatchBasis"] == "display_name"
    assert events[ana_shown]["PreMeeting"]["MatchedNameSource"] == "display_name"
    assert events[ana_shown]["PreMeeting"]["ListedName"] is None
    assert events[bo_shown]["PreMeeting"]["ConsentId"] == given["bo"]
    assert events[bo_shown]["PreMeeting"]["MatchedNameSource"] == "short_name"
    assert "@" not in json.dumps(record["Events"]), "no address is copied into the meeting record"
    assert record["CaptureAllowed"] is True, "everyone present is covered"
