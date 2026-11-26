# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Each role is admitted to what the role matrix lists, and refused the rest, on a deployed stack.

A throwaway Admin, User and Participant are created for the run (no invitation
email, a permanent password), signed in through SRP as the web UI signs in, and
deleted afterwards. See role_access_probe.py for what is called and how.

What this run establishes about an upgrade: an existing account in no group
signs in as a User and keeps every operation a User had, and an Admin keeps
every operation an Admin had, because the "allowed" calls fail on any field
refusal; and a Participant is refused every operation and every other entry
point but transcript translation through the identity pool. Mutations are
only called as roles that are refused them.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import role_access_probe as probe
import security_report

pytest.importorskip("pycognito")
pytest.importorskip("graphql")


@pytest.fixture(scope="module")
def deployment(client) -> probe.Deployment:
    found = probe.Deployment.find(client.stack_name, client.region)
    security_report.REPORT.stack = client.stack_name
    security_report.REPORT.region = found.session.region_name or ""
    return found


@pytest.fixture(scope="module")
def users(deployment: probe.Deployment):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S")
    made = probe.create_role_users(deployment, stamp)
    try:
        for user in made:
            probe.sign_in(deployment, user)
        yield {user.role: user for user in made}
    finally:
        left = probe.delete_role_users(deployment, made)
        assert not left, f"could not delete the temporary users {left}"


def test_every_token_names_the_callers_role(users) -> None:
    """The pre token generation trigger: one role group in both tokens, and lma_role in the ID token."""
    for role, user in users.items():
        id_claims = probe.claims(user.tokens["id"])
        access_claims = probe.claims(user.tokens["access"])
        assert id_claims.get("lma_role") == role
        for token_claims in (id_claims, access_claims):
            groups = token_claims.get("cognito:groups") or []
            assert [g for g in groups if g in ("Admin", "User", "Participant")] == [role], (role, groups)


def test_each_role_is_refused_or_admitted_at_every_query_and_mutation_as_the_matrix_says(
    deployment, users
) -> None:
    matrix = probe.load_matrix()
    schema = probe.load_schema()
    wrong: list[str] = []
    for operation, role, expect_refused in probe.plan_calls(matrix, schema).calls:
        document, variables = probe.build_operation(schema, operation)
        response = probe.call(deployment, users[role], document, variables)
        refused = probe.refused_at_field(response, operation)
        errors = [e.get("message", "")[:120] for e in response.get("errors") or []]
        security_report.REPORT.record(
            "AppSync role matrix",
            operation,
            role,
            "refused" if expect_refused else "not refused",
            "refused" if refused else "not refused",
            "; ".join(errors) if refused != expect_refused else "",
        )
        if refused != expect_refused:
            wrong.append(f"{operation} as {role}: {'refused' if refused else 'not refused'} {errors}")
    if wrong:
        # pytest.fail, not assert: assertion rewriting would print the list too.
        pytest.fail(security_report.describe_failures(security_report.REPORT.failures("AppSync role matrix")), pytrace=False)


def test_the_transcriber_refuses_a_participant_and_admits_a_user(deployment, users) -> None:
    assert probe.transcriber_status(deployment, users["Participant"]) == 403
    assert probe.transcriber_status(deployment, users["User"]) == 101
    assert probe.transcriber_status(deployment, users["Admin"]) == 101


def test_the_identity_pool_gives_each_role_its_iam_role(deployment, users) -> None:
    for role in ("Admin", "User"):
        arn, session = probe.identity_pool_role(deployment, users[role])
        assert "CognitoAuthorizedRole" in arn, (role, arn)
        assert probe.settings_readable(deployment, session), role
    arn, session = probe.identity_pool_role(deployment, users["Participant"])
    assert "CognitoParticipantRole" in arn, arn
    assert not probe.settings_readable(deployment, session)


def test_the_participant_identity_pool_role_translates_and_is_refused_everything_else(deployment, users) -> None:
    """The Translate switch on a shared meeting's page works; nothing else the web UI's role allows does."""
    assert deployment.recordings_bucket, "the Cognito stack names no S3BucketName"
    _, session = probe.identity_pool_role(deployment, users["Participant"])
    assert probe.translated(session, "Good morning, everyone.").strip()
    refusals = probe.participant_refusals(deployment, session)
    assert all(refusals.values()), {action: r for action, r in refusals.items() if not r}


def test_the_mcp_server_refuses_a_key_whose_owner_is_a_participant(deployment, users, request) -> None:
    status = probe.mcp_status_with_key(deployment, users["Participant"])
    if status is None:
        # A stack deployed with EnableMCP=false has nothing to check, so
        # --no-skips leaves this skip alone.
        request.node.add_marker(pytest.mark.optional_feature)
        pytest.skip("the stack has no MCP API key endpoint (EnableMCP=false)")
    assert status == 403
    assert probe.mcp_status_with_key(deployment, users["User"]) == 200


#: The participant list operations, which a User may call for their own meetings only,
#: each naming a placeholder meeting that nobody owns.
PARTICIPANT_LIST_CALLS = (
    (
        "Query.getMeetingParticipantAccounts",
        "query Probe($CallId: ID) { getMeetingParticipantAccounts(CallId: $CallId) { email account } }",
        {"CallId": probe.PLACEHOLDER_ID},
    ),
    (
        "Mutation.createParticipantAccounts",
        (
            "mutation Probe($input: CreateParticipantAccountsInput!) "
            "{ createParticipantAccounts(input: $input) { created } }"
        ),
        {"input": {"CallId": probe.PLACEHOLDER_ID}},
    ),
    (
        "Mutation.updateCallParticipants",
        (
            "mutation Probe($input: UpdateCallParticipantsInput!) "
            "{ updateCallParticipants(input: $input) { CallId } }"
        ),
        {"input": {"CallId": probe.PLACEHOLDER_ID, "Participants": []}},
    ),
    (
        # The same list named by a Virtual Participant's id, as its page names it.
        "Mutation.updateCallParticipants",
        (
            "mutation Probe($input: UpdateCallParticipantsInput!) "
            "{ updateCallParticipants(input: $input) { CallId } }"
        ),
        {"input": {"VirtualParticipantId": probe.PLACEHOLDER_ID, "Participants": []}},
    ),
)

CREATE_USER_FOR_A_MEETING = (
    "mutation Probe($input: CreateUserInput!) { createUser(input: $input) { username } }"
)


def test_creating_a_user_for_a_meeting_is_refused_to_a_user_and_a_participant(deployment, users) -> None:
    """createUser with inviteToMeeting stays with User Management: refused at the field to
    everyone but an Admin. test_meeting_participants.py creates one as an Admin."""
    variables = {
        "input": {
            "email": f"integ-never-created-{probe.PLACEHOLDER_ID}@example.invalid",
            "role": "Participant",
            "inviteToMeeting": probe.PLACEHOLDER_ID,
        }
    }
    for role in ("User", "Participant"):
        response = probe.call(deployment, users[role], CREATE_USER_FOR_A_MEETING, variables)
        assert probe.refused_at_field(response, "Mutation.createUser"), (role, response)


def test_the_participant_list_operations_refuse_a_participant_and_a_user_who_does_not_own_the_meeting(
    deployment, users
) -> None:
    """A Participant is refused at the field. A User reaches the resolver, which refuses a
    meeting they do not own; test_meeting_participants.py covers the owner's own meeting."""
    for operation, document, variables in PARTICIPANT_LIST_CALLS:
        participant = probe.call(deployment, users["Participant"], document, variables)
        assert probe.refused_at_field(participant, operation), (operation, participant)
        user = probe.call(deployment, users["User"], document, variables)
        messages = " ".join(e.get("message", "") for e in user.get("errors") or [])
        assert not probe.refused_at_field(user, operation), (operation, user)
        assert "Unauthorized" in messages, (operation, user)
        assert not (user.get("data") or {}).get(operation.split(".", 1)[1]), (operation, user)


#: A Participant's read-only view, each naming a placeholder meeting shared with nobody.
PARTICIPANT_VIEW_CALLS = (
    (
        "Query.getParticipantMeeting",
        "query Probe($CallId: ID!) { getParticipantMeeting(CallId: $CallId) { CallId } }",
    ),
    (
        "Query.getParticipantMeetingTranscript",
        "query Probe($CallId: ID!) { getParticipantMeetingTranscript(CallId: $CallId) { nextToken } }",
    ),
    (
        "Query.getParticipantMeetingRecordings",
        "query Probe($CallId: ID!) { getParticipantMeetingRecordings(CallId: $CallId) { Kind } }",
    ),
)


def test_the_participant_view_answers_only_meetings_shared_with_the_caller(deployment, users) -> None:
    """A Participant reaches the view's resolvers, which refuse a meeting not shared with them;
    an Admin and a User are refused the view at the field. test_participant_access.py reads a
    meeting that is shared with the Participant, and checks the fields it is given."""
    for operation, document in PARTICIPANT_VIEW_CALLS:
        variables = {"CallId": probe.PLACEHOLDER_ID}
        participant = probe.call(deployment, users["Participant"], document, variables)
        assert not probe.refused_at_field(participant, operation), (operation, participant)
        messages = " ".join(e.get("message", "") for e in participant.get("errors") or [])
        assert "Unauthorized" in messages, (operation, participant)
        assert not (participant.get("data") or {}).get(operation.split(".", 1)[1]), (operation, participant)
        for role in ("Admin", "User"):
            refused = probe.call(deployment, users[role], document, variables)
            assert probe.refused_at_field(refused, operation), (operation, role, refused)


LIST_MY_CONSENTS = "query Probe { listMyAdvanceConsents { Email Consents { ConsentId Email } } }"
WITHDRAW_CONSENT = (
    "mutation Probe($input: WithdrawAdvanceConsentInput!) "
    "{ withdrawAdvanceConsent(input: $input) { ConsentId } }"
)
INVITATION = (
    "query Probe($CallId: ID) { getAdvanceConsentInvitation(CallId: $CallId) "
    "{ Listed MeetingName Organizer NoticeText } }"
)


def test_every_role_reaches_its_own_consents_given_in_advance_only(deployment, users) -> None:
    """Each role lists the consents kept under its own address, withdraws none of anybody
    else's, and is told only "not listed" for a meeting it is not on.
    test_pre_meeting_consent.py gives, lists and withdraws consents for a real meeting."""
    for role, user in users.items():
        answer = probe.call(deployment, user, LIST_MY_CONSENTS, {})
        assert not answer.get("errors"), (role, answer)
        mine = answer["data"]["listMyAdvanceConsents"]
        assert mine["Email"] == user.username.casefold(), (role, mine)
        assert all(c["Email"] == mine["Email"] for c in mine["Consents"] or []), (role, mine)
        withdrawn = probe.call(
            deployment, user, WITHDRAW_CONSENT, {"input": {"ConsentId": "20260101000000-000000000000"}}
        )
        assert not probe.refused_at_field(withdrawn, "Mutation.withdrawAdvanceConsent"), (role, withdrawn)
        assert "not one of your consents" in " ".join(
            e.get("message", "") for e in withdrawn.get("errors") or []
        ), (role, withdrawn)
        page = probe.call(deployment, user, INVITATION, {"CallId": probe.PLACEHOLDER_ID})
        assert not page.get("errors"), (role, page)
        invitation = page["data"]["getAdvanceConsentInvitation"]
        assert invitation["Listed"] is False, (role, invitation)
        assert invitation["MeetingName"] is None and invitation["NoticeText"] is None, (role, invitation)


SEARCH_ACCOUNTS = "query Probe($text: String!) { searchAccounts(text: $text) { name email } }"
UPDATE_MY_NAMES = (
    "mutation Probe($input: UpdateMyAccountNamesInput!) "
    "{ updateMyAccountNames(input: $input) { DisplayName ShortNames } }"
)
SET_USER_NAMES = (
    "mutation Probe($input: SetUserAccountNamesInput!) { setUserAccountNames(input: $input) { username } }"
)


def test_the_account_search_answers_admins_and_users_and_refuses_participants(deployment, users) -> None:
    """The participant list's account search: an Admin and a User find the temporary Admin by
    the start of its address, with its name and address only; a Participant is refused at the
    field, and fewer than three characters are refused to everyone.
    test_account_names.py searches by display name and short name."""
    prefix = users["Admin"].username.split("@", 1)[0]
    for role in ("Admin", "User"):
        answer = probe.call(deployment, users[role], SEARCH_ACCOUNTS, {"text": prefix})
        assert not answer.get("errors"), (role, answer)
        found = answer["data"]["searchAccounts"]
        assert users["Admin"].username.casefold() in {a["email"] for a in found}, (role, found)
        assert len(found) <= 10, (role, found)
        short = probe.call(deployment, users[role], SEARCH_ACCOUNTS, {"text": "ab"})
        assert "at least 3" in " ".join(e.get("message", "") for e in short.get("errors") or []), (role, short)
    refused = probe.call(deployment, users["Participant"], SEARCH_ACCOUNTS, {"text": prefix})
    assert probe.refused_at_field(refused, "Query.searchAccounts"), refused


def test_every_role_sets_its_own_names_and_only_an_admin_sets_another_users(deployment, users) -> None:
    """updateMyAccountNames acts on the caller's own names, whatever their role (cleared again
    here); setUserAccountNames is refused at the field to a User and a Participant."""
    for role, user in users.items():
        named = probe.call(deployment, user, UPDATE_MY_NAMES, {"input": {"DisplayName": f"Integ {role}"}})
        assert not named.get("errors"), (role, named)
        assert named["data"]["updateMyAccountNames"]["DisplayName"] == f"Integ {role}", (role, named)
        cleared = probe.call(deployment, user, UPDATE_MY_NAMES, {"input": {"DisplayName": "", "ShortNames": []}})
        assert not cleared.get("errors"), (role, cleared)
    target = {"input": {"username": users["Admin"].username, "displayName": "Not Theirs"}}
    for role in ("User", "Participant"):
        response = probe.call(deployment, users[role], SET_USER_NAMES, target)
        assert probe.refused_at_field(response, "Mutation.setUserAccountNames"), (role, response)
