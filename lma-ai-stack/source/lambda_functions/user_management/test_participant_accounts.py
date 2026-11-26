# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for getMeetingParticipantAccounts and createParticipantAccounts (no AWS).

These two operations are the only ones in this resolver a User may call, so what
is pinned here is what keeps them narrow:

* the caller must own the meeting (an Admin may name any), whether the meeting is
  named by its CallId or by the Virtual Participant that will create it, and a
  meeting that does not exist reads like one the caller does not own;
* only addresses on that meeting's participant list are looked up or created;
* only missing accounts are created, always in the Participant group, never in
  another, and an existing account is never changed;
* the answer about an account is "none" or its role;
* a User's daily limit and the per-request limit, and AllowedSignUpEmailDomain.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("USER_POOL_ID", "us-east-1_testpool")


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "user_management_index_participants", Path(__file__).resolve().parent / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


with mock.patch("boto3.client"):
    index = _load_index()

OWNER = "owner@example.com"
OTHER = "other@example.com"
CALL_ID = "call-1"
VP_ID = "vp-1"
LISTED = [
    {"name": "Ana Lima", "email": "ana@example.com"},
    {"name": "", "email": "Bo@Example.org"},
    {"name": "Cy", "email": "cy@example.net"},
    {"name": "Di", "email": "di@example.com"},
]


class _CognitoError(ClientError):
    def __init__(self, code: str = "InternalErrorException") -> None:
        super().__init__({"Error": {"Code": code, "Message": code}}, "CognitoOperation")


class UsernameExistsException(_CognitoError):
    pass


class UserNotFoundException(_CognitoError):
    pass


class FakeCognito:
    """A user pool whose usernames compare ignoring case, as LMA's does."""

    def __init__(self) -> None:
        # username -> {"email", "groups"}
        self.users: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.add_to_group_error: ClientError | None = None
        self.resend_error: ClientError | None = None
        # Addresses sent an invitation, in order.
        self.invited: list[str] = []
        self.exceptions = SimpleNamespace(
            UsernameExistsException=UsernameExistsException,
            UserNotFoundException=UserNotFoundException,
        )

    def add(self, username: str, *, email: str | None = None, groups=()) -> None:
        self.users[username] = {"email": email or username, "groups": list(groups)}

    def _key(self, username: str) -> str | None:
        return next((u for u in self.users if u.casefold() == username.casefold()), None)

    def names_called(self) -> list[str]:
        return [name for name, _ in self.calls]

    def admin_get_user(self, **kwargs):
        self.calls.append(("admin_get_user", kwargs))
        key = self._key(kwargs["Username"])
        if key is None:
            raise UserNotFoundException("UserNotFoundException")
        return {"Username": key}

    def list_users(self, **kwargs):
        self.calls.append(("list_users", kwargs))
        wanted = kwargs["Filter"].split('"')[1]
        return {"Users": [{"Username": u} for u, v in self.users.items() if v["email"] == wanted]}

    def admin_list_groups_for_user(self, **kwargs):
        self.calls.append(("admin_list_groups_for_user", kwargs))
        groups = self.users[self._key(kwargs["Username"])]["groups"]
        return {"Groups": [{"GroupName": g} for g in groups]}

    def admin_create_user(self, **kwargs):
        self.calls.append(("admin_create_user", kwargs))
        if kwargs.get("MessageAction") == "RESEND":
            if self.resend_error is not None:
                raise self.resend_error
            self.invited.append(kwargs["Username"])
            return {}
        if self._key(kwargs["Username"]) is not None:
            raise UsernameExistsException("UsernameExistsException")
        self.add(kwargs["Username"])
        if kwargs.get("MessageAction") != "SUPPRESS":
            self.invited.append(kwargs["Username"])
        return {}

    def admin_add_user_to_group(self, **kwargs):
        self.calls.append(("admin_add_user_to_group", kwargs))
        if self.add_to_group_error is not None:
            raise self.add_to_group_error
        self.users[self._key(kwargs["Username"])]["groups"].append(kwargs["GroupName"])
        return {}

    def admin_delete_user(self, **kwargs):
        self.calls.append(("admin_delete_user", kwargs))
        self.users.pop(self._key(kwargs["Username"]), None)
        return {}


def _participants_attr(people):
    return {"L": [{"M": {"name": {"S": p["name"]}, "email": {"S": p["email"]}}} for p in people]}


class FakeDynamo:
    def accounts_counted(self) -> int:
        return sum(
            n for (_pk, sk), n in self.counts.items() if sk.startswith("participant-accounts#")
        )

    def lookups_counted(self) -> int:
        return sum(
            n for (_pk, sk), n in self.counts.items() if sk.startswith("participant-lookups#")
        )

    def __init__(self) -> None:
        self.meetings: dict[str, dict] = {}
        self.vps: dict[str, dict] = {}
        self.counts: dict[tuple, int] = {}
        self.updates: list[dict] = []
        # The address hashes each day's lookup item holds.
        self.seen: dict[tuple, set] = {}

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        if kwargs["TableName"] == index.EVENT_SOURCING_TABLE and key["PK"]["S"].startswith("c#"):
            item = self.meetings.get(key["PK"]["S"][2:])
        elif kwargs["TableName"] == index.EVENT_SOURCING_TABLE:
            day = (key["PK"]["S"], key["SK"]["S"])
            count = self.counts.get(day)
            item = None if count is None else {"CreatedCount": {"N": str(count)}}
            if item is not None and day in self.seen:
                item["Seen"] = {"SS": sorted(self.seen[day])}
        else:
            item = self.vps.get(key["id"]["S"])
        return {"Item": item} if item else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        if "list_append" in kwargs["UpdateExpression"]:
            return self._append_participant(**kwargs)
        key = (kwargs["Key"]["PK"]["S"], kwargs["Key"]["SK"]["S"])
        values = kwargs["ExpressionAttributeValues"]
        current = self.counts.get(key)
        if ":room" in values:  # a block of lookups: fits when the count is at most room
            if current is not None and current > int(values[":room"]["N"]):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
            self.counts[key] = (current or 0) + int(values[":count"]["N"])
            self.seen.setdefault(key, set()).update(values[":fresh"]["SS"])
        elif "ConditionExpression" in kwargs:
            limit = int(values[":limit"]["N"])
            if current is not None and current >= limit:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
                )
            self.counts[key] = (current or 0) + 1
        else:
            self.counts[key] = (current or 0) + int(values[":minus"]["N"])
        return {}

    def appended(self) -> list:
        return [u for u in self.updates if "list_append" in u["UpdateExpression"]]

    def _append_participant(self, **kwargs):
        """createUser's append to a participant list, with its condition."""
        values = kwargs["ExpressionAttributeValues"]
        attribute = kwargs["ExpressionAttributeNames"]["#p"]
        if "PK" in kwargs["Key"]:
            item = self.meetings.get(kwargs["Key"]["PK"]["S"][2:])
        else:
            item = self.vps.get(kwargs["Key"]["id"]["S"])
        current = (item or {}).get(attribute, {"L": []})["L"]
        if item is None or len(current) >= int(values[":max"]["N"]):
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        item[attribute] = {"L": current + values[":entry"]["L"]}
        if ":by" in values:
            item["ParticipantsUpdatedBy"] = values[":by"]
        return {}


@pytest.fixture(name="pool")
def pool_fixture(monkeypatch):
    fake = FakeCognito()
    monkeypatch.setattr(index, "cognito", fake)
    return fake


@pytest.fixture(name="ddb")
def ddb_fixture(monkeypatch):
    fake = FakeDynamo()
    fake.meetings[CALL_ID] = {"Owner": {"S": OWNER}, "Participants": _participants_attr(LISTED)}
    fake.vps[VP_ID] = {"Owner": {"S": OWNER}, "participants": _participants_attr(LISTED[:2])}
    monkeypatch.setattr(index, "EVENT_SOURCING_TABLE", "events")
    monkeypatch.setattr(index, "VIRTUAL_PARTICIPANT_TABLE", "vps")
    monkeypatch.setattr(index.boto3, "client", lambda *_a, **_k: fake)
    monkeypatch.setattr(index, "ALLOWED_SIGNUP_EMAIL_DOMAINS", "")
    # Meeting Consent › Allow consent given in advance, as read from the configuration.
    monkeypatch.setattr(index, "_advance_consent_enabled", lambda: True)
    return fake


def event(field, arguments, *, username=OWNER, groups=("User",)):
    return {
        "info": {"fieldName": field},
        "identity": {"claims": {"cognito:username": username, "cognito:groups": list(groups)}},
        "arguments": arguments,
    }


def accounts(arguments, **caller):
    return index.handler(event("getMeetingParticipantAccounts", arguments, **caller), None)


def create(payload, **caller):
    return index.handler(event("createParticipantAccounts", {"input": payload}, **caller), None)


# ── who may ask ──────────────────────────────────────────────────────────────


def test_the_owner_reads_each_listed_persons_account(pool, ddb):
    pool.add("ana@example.com", groups=["Participant"])
    pool.add("legacy-name", email="bo@example.org")  # username is not the address
    pool.add("cy@example.net", groups=["Admin", "Participant"])
    answer = accounts({"CallId": CALL_ID})
    assert answer == [
        {"name": "Ana Lima", "email": "ana@example.com", "account": "Participant"},
        {"name": "", "email": "Bo@Example.org", "account": "User"},
        {"name": "Cy", "email": "cy@example.net", "account": "Admin"},
        {"name": "Di", "email": "di@example.com", "account": "none"},
    ]


def test_the_answer_carries_nothing_else_about_an_account(pool, ddb):
    pool.add("ana@example.com", groups=["Participant"])
    for entry in accounts({"CallId": CALL_ID}):
        assert set(entry) == {"name", "email", "account"}


def test_a_virtual_participant_s_list_is_read_before_it_has_a_meeting(pool, ddb):
    answer = accounts({"VirtualParticipantId": VP_ID})
    assert [entry["email"] for entry in answer] == ["ana@example.com", "Bo@Example.org"]


@pytest.mark.parametrize("arguments", [{"CallId": CALL_ID}, {"VirtualParticipantId": VP_ID}])
def test_a_user_who_does_not_own_the_meeting_is_refused(pool, ddb, arguments):
    with pytest.raises(Exception, match="Unauthorized"):
        accounts(arguments, username=OTHER)
    with pytest.raises(Exception, match="Unauthorized"):
        create(arguments, username=OTHER)
    assert pool.calls == []


def test_a_missing_meeting_reads_to_a_user_like_one_they_do_not_own(pool, ddb):
    with pytest.raises(Exception, match="Unauthorized"):
        accounts({"CallId": "no-such-meeting"})
    with pytest.raises(Exception, match="no such meeting"):
        accounts({"CallId": "no-such-meeting"}, username="admin@example.com", groups=["Admin"])


def test_an_admin_may_read_and_create_for_any_meeting(pool, ddb):
    admin = {"username": "admin@example.com", "groups": ["Admin"]}
    assert len(accounts({"CallId": CALL_ID}, **admin)) == 4
    result = create({"CallId": CALL_ID, "emails": ["di@example.com"]}, **admin)
    assert result["created"] == 1 and result["remainingToday"] is None
    assert ddb.updates == [], "an Admin is not counted against a daily limit"


def test_a_participant_is_refused_before_anything_is_read(pool, ddb):
    for field, arguments in (
        ("getMeetingParticipantAccounts", {"CallId": CALL_ID}),
        ("createParticipantAccounts", {"input": {"CallId": CALL_ID}}),
    ):
        with pytest.raises(Exception, match="Unauthorized"):
            index.handler(event(field, arguments, username=OWNER, groups=["Participant"]), None)
    assert pool.calls == []


@pytest.mark.parametrize(
    "arguments", [{}, {"CallId": CALL_ID, "VirtualParticipantId": VP_ID}, {"CallId": " "}]
)
def test_exactly_one_meeting_must_be_named(pool, ddb, arguments):
    with pytest.raises(Exception, match="exactly one"):
        accounts(arguments)


# ── what is created ──────────────────────────────────────────────────────────


def test_accounts_are_created_in_the_participant_group_only_for_those_without_one(pool, ddb):
    pool.add("ana@example.com", groups=["Admin"])
    pool.add("cy@example.net")
    result = create({"CallId": CALL_ID})
    outcomes = {r["email"]: r["outcome"] for r in result["results"]}
    assert outcomes == {
        "ana@example.com": "exists",
        "Bo@Example.org": "created",
        "cy@example.net": "exists",
        "di@example.com": "created",
    }
    assert result["created"] == 2
    groups = [kw["GroupName"] for name, kw in pool.calls if name == "admin_add_user_to_group"]
    assert groups == ["Participant", "Participant"]
    # Existing accounts keep exactly their groups.
    assert pool.users["ana@example.com"]["groups"] == ["Admin"]
    assert pool.users["cy@example.net"]["groups"] == []
    created = [kw for name, kw in pool.calls if name == "admin_create_user"]
    assert {"Name": "email", "Value": "Bo@Example.org"} in created[0]["UserAttributes"]
    assert pool.invited == ["Bo@Example.org", "di@example.com"]


def test_the_invitation_is_sent_only_once_the_account_is_in_the_participant_group(pool, ddb):
    create({"CallId": CALL_ID, "emails": ["di@example.com"]})
    steps = [
        (name, kw.get("MessageAction"))
        for name, kw in pool.calls
        if name in ("admin_create_user", "admin_add_user_to_group")
    ]
    assert steps == [
        ("admin_create_user", "SUPPRESS"),
        ("admin_add_user_to_group", None),
        ("admin_create_user", "RESEND"),
    ]
    resend = [kw for name, kw in pool.calls if kw.get("MessageAction") == "RESEND"][0]
    assert resend["DesiredDeliveryMediums"] == ["EMAIL"]


def test_the_invitation_names_the_meeting_and_its_consent_page(pool, ddb):
    """Read by the user pool's CustomMessage trigger, which builds the link."""
    ddb.meetings["Weekly sync / review"] = ddb.meetings[CALL_ID]
    create({"CallId": "Weekly sync / review", "emails": ["di@example.com"]})
    resend = [kw for name, kw in pool.calls if kw.get("MessageAction") == "RESEND"][0]
    assert resend["ClientMetadata"] == {
        "lmaRole": "Participant",
        "lmaAdvanceConsent": "on",
        "lmaInvitation": "meeting_participant",
        "meetingName": "Weekly sync / review",
        "consentQuery": "meeting=Weekly%20sync%20%2F%20review",
    }
    # Only the invitation carries it, not the suppressed create.
    suppressed = [kw for name, kw in pool.calls if kw.get("MessageAction") == "SUPPRESS"][0]
    assert "ClientMetadata" not in suppressed


def test_a_vp_invitation_links_the_vp_consent_page(pool, ddb):
    ddb.vps[VP_ID]["meetingName"] = {"S": "Planning"}
    create({"VirtualParticipantId": VP_ID, "emails": ["Bo@Example.org"]})
    resend = [kw for name, kw in pool.calls if kw.get("MessageAction") == "RESEND"][0]
    assert resend["ClientMetadata"]["meetingName"] == "Planning"
    assert resend["ClientMetadata"]["consentQuery"] == f"vp={VP_ID}"


def test_only_listed_addresses_may_be_named(pool, ddb):
    with pytest.raises(Exception, match="participant list"):
        create({"CallId": CALL_ID, "emails": ["ana@example.com", "stranger@example.com"]})
    assert "admin_create_user" not in pool.names_called()


def test_a_listed_address_is_matched_ignoring_case(pool, ddb):
    result = create({"CallId": CALL_ID, "emails": ["bo@example.ORG", "BO@example.org"]})
    assert [(r["email"], r["outcome"]) for r in result["results"]] == [
        ("Bo@Example.org", "created")
    ]


def test_an_account_made_between_the_lookup_and_the_create_is_reported_as_existing(
    pool, ddb, monkeypatch
):
    monkeypatch.setattr(index, "_accounts_of", lambda emails: {e: "none" for e in emails})
    pool.add("ana@example.com", groups=["Admin"])
    result = create({"CallId": CALL_ID, "emails": ["ana@example.com"]})
    assert result["results"][0]["outcome"] == "exists"
    assert pool.users["ana@example.com"]["groups"] == ["Admin"]
    assert ddb.accounts_counted() == 0, "the slot reserved for it is given back"


@pytest.mark.parametrize("failing", ["add_to_group_error", "resend_error"])
def test_a_failure_after_the_create_leaves_no_account_and_sends_no_invitation(pool, ddb, failing):
    setattr(pool, failing, _CognitoError())
    result = create({"CallId": CALL_ID, "emails": ["di@example.com"]})
    assert result["results"][0]["outcome"] == "failed"
    assert "di@example.com" not in pool.users
    assert pool.invited == []
    assert result["created"] == 0
    assert ddb.accounts_counted() == 0, "the daily slot is given back"


def test_addresses_outside_the_allowed_domains_are_not_created(pool, ddb, monkeypatch):
    monkeypatch.setattr(index, "ALLOWED_SIGNUP_EMAIL_DOMAINS", "example.com")
    result = create({"CallId": CALL_ID})
    outcomes = {r["email"]: r["outcome"] for r in result["results"]}
    assert outcomes["Bo@Example.org"] == "domain_not_allowed"
    assert outcomes["cy@example.net"] == "domain_not_allowed"
    assert outcomes["ana@example.com"] == "created"
    assert set(pool.users) == {"ana@example.com", "di@example.com"}


# ── limits ───────────────────────────────────────────────────────────────────


def test_a_user_s_daily_limit_stops_creation_and_is_reported(pool, ddb, monkeypatch):
    monkeypatch.setattr(index, "PARTICIPANT_ACCOUNTS_PER_DAY", 2)
    result = create({"CallId": CALL_ID})
    assert [r["outcome"] for r in result["results"]] == [
        "created",
        "created",
        "limit_reached",
        "limit_reached",
    ]
    assert result["remainingToday"] == 0
    assert len(pool.users) == 2
    # The count is the caller's, for today, and expires.
    update = next(u for u in ddb.updates if u["Key"]["SK"]["S"].startswith("participant-accounts#"))
    assert update["Key"]["PK"]["S"] == f"usr#{OWNER}"
    assert update["Key"]["SK"]["S"].startswith("participant-accounts#")
    assert ":ttl" in update["ExpressionAttributeValues"]


def test_one_request_creates_at_most_the_per_request_limit(pool, ddb, monkeypatch):
    monkeypatch.setattr(index, "PARTICIPANT_ACCOUNTS_PER_REQUEST", 1)
    result = create({"CallId": CALL_ID}, username="admin@example.com", groups=["Admin"])
    assert [r["outcome"] for r in result["results"]] == [
        "created",
        "limit_reached",
        "limit_reached",
        "limit_reached",
    ]


def test_the_remaining_daily_count_is_returned_to_a_user(pool, ddb):
    result = create({"CallId": CALL_ID, "emails": ["di@example.com"]})
    assert result["remainingToday"] == index.PARTICIPANT_ACCOUNTS_PER_DAY - 1


def test_a_user_s_lookups_count_each_address_once_a_day(pool, ddb):
    accounts({"CallId": CALL_ID})
    accounts({"CallId": CALL_ID})
    create({"CallId": CALL_ID, "emails": ["DI@example.com"]})
    accounts({"VirtualParticipantId": VP_ID})
    assert ddb.lookups_counted() == len(LISTED)
    (seen,) = ddb.seen.values()
    assert "ana@example.com" not in " ".join(seen), "only a hash of each address is kept"


def test_a_user_s_lookups_of_new_addresses_are_refused_past_the_daily_limit(pool, ddb, monkeypatch):
    monkeypatch.setattr(index, "PARTICIPANT_LOOKUPS_PER_DAY", 2)
    with pytest.raises(Exception, match="2 different addresses"):
        accounts({"CallId": CALL_ID})
    assert "admin_get_user" not in pool.names_called()
    assert accounts({"VirtualParticipantId": VP_ID})  # two addresses fit
    assert accounts({"VirtualParticipantId": VP_ID}), "the same two again cost nothing"
    with pytest.raises(Exception, match="2 different addresses"):
        create({"CallId": CALL_ID, "emails": ["cy@example.net"]})


def test_an_empty_list_answers_nothing_after_the_owner_check(pool, ddb):
    ddb.meetings[CALL_ID]["Participants"] = {"L": []}
    assert accounts({"CallId": CALL_ID}) == []
    with pytest.raises(Exception, match="Unauthorized"):
        accounts({"CallId": CALL_ID}, username=OTHER)
    assert pool.calls == [] and ddb.updates == []


def test_an_admin_s_lookups_are_not_counted(pool, ddb, monkeypatch):
    monkeypatch.setattr(index, "PARTICIPANT_LOOKUPS_PER_DAY", 1)
    accounts({"CallId": CALL_ID}, username="admin@example.com", groups=["Admin"])
    assert ddb.lookups_counted() == 0


def test_the_owner_is_compared_exactly(pool, ddb):
    with pytest.raises(Exception, match="Unauthorized"):
        accounts({"CallId": CALL_ID}, username=OWNER.upper())


# ── a Virtual Participant whose meeting exists ───────────────────────────────


def test_a_vp_whose_meeting_exists_is_read_as_that_meeting_s_list(pool, ddb):
    """Its page edits the meeting's list once the meeting exists, so that list is the one read."""
    ddb.vps[VP_ID]["CallId"] = {"S": CALL_ID}
    assert [a["email"] for a in accounts({"VirtualParticipantId": VP_ID})] == [
        p["email"] for p in LISTED
    ]
    # Still named and linked as the Virtual Participant.
    ddb.vps[VP_ID]["meetingName"] = {"S": "Planning"}
    create({"VirtualParticipantId": VP_ID, "emails": ["di@example.com"]})
    resend = [kw for name, kw in pool.calls if kw.get("MessageAction") == "RESEND"][0]
    assert resend["ClientMetadata"]["consentQuery"] == f"vp={VP_ID}"
    assert resend["ClientMetadata"]["meetingName"] == "Planning"


def test_a_vp_s_linked_meeting_with_another_owner_is_not_followed(pool, ddb):
    ddb.vps[VP_ID]["CallId"] = {"S": CALL_ID}
    ddb.meetings[CALL_ID]["Owner"] = {"S": OTHER}
    assert len(accounts({"VirtualParticipantId": VP_ID})) == 2


def test_the_invitation_says_nothing_of_consent_given_in_advance_when_it_is_off(
    pool, ddb, monkeypatch
):
    monkeypatch.setattr(index, "_advance_consent_enabled", lambda: False)
    create({"CallId": CALL_ID, "emails": ["di@example.com"]})
    resend = [kw for name, kw in pool.calls if kw.get("MessageAction") == "RESEND"][0]
    assert resend["ClientMetadata"]["lmaRole"] == "Participant"
    assert "lmaAdvanceConsent" not in resend["ClientMetadata"]


# ── createUser with a meeting to invite the person to ────────────────────────

ADMIN = {"username": "admin@example.com", "groups": ["Admin"]}


def create_user(payload, **caller):
    return index.handler(event("createUser", {"input": payload}, **{**ADMIN, **caller}), None)


def _sent_metadata(pool):
    sent = [kw for name, kw in pool.calls if name == "admin_create_user"]
    assert len(sent) == 1
    return sent[0]["ClientMetadata"]


def _emails(attribute):
    return [entry["M"]["email"]["S"] for entry in attribute["L"]]


@pytest.mark.parametrize("role", ["Admin", "User", "Participant"])
def test_without_a_meeting_the_invitation_carries_only_the_role(pool, ddb, role):
    create_user({"email": "new@example.com", "role": role})
    assert _sent_metadata(pool) == {"lmaRole": role, "lmaAdvanceConsent": "on"}
    assert ddb.appended() == []


@pytest.mark.parametrize("role", ["Admin", "User", "Participant"])
def test_a_user_invited_to_a_meeting_gets_its_consent_link_and_is_listed(pool, ddb, role):
    create_user({"email": "new@example.com", "role": role, "inviteToMeeting": CALL_ID})
    assert _sent_metadata(pool) == {
        "lmaRole": role,
        "lmaAdvanceConsent": "on",
        "lmaInvitation": "meeting_participant",
        "meetingName": CALL_ID,
        "consentQuery": f"meeting={CALL_ID}",
    }
    meeting = ddb.meetings[CALL_ID]
    assert _emails(meeting["Participants"])[-1] == "new@example.com"
    assert len(meeting["Participants"]["L"]) == len(LISTED) + 1
    assert meeting["ParticipantsUpdatedBy"] == {"S": "admin@example.com"}
    # The list is changed before the account is made.
    assert pool.names_called().index("admin_create_user") > 0


def test_a_vp_without_a_meeting_gets_the_address_on_its_own_list(pool, ddb):
    ddb.vps[VP_ID]["meetingName"] = {"S": "Planning"}
    create_user({"email": "new@example.com", "role": "Participant", "inviteToMeeting": VP_ID})
    metadata = _sent_metadata(pool)
    assert metadata["consentQuery"] == f"vp={VP_ID}" and metadata["meetingName"] == "Planning"
    assert _emails(ddb.vps[VP_ID]["participants"])[-1] == "new@example.com"
    assert len(ddb.meetings[CALL_ID]["Participants"]["L"]) == len(LISTED)


def test_a_vp_whose_meeting_exists_gets_the_address_on_the_meeting_s_list(pool, ddb):
    ddb.vps[VP_ID]["CallId"] = {"S": CALL_ID}
    create_user({"email": "new@example.com", "role": "User", "inviteToMeeting": VP_ID})
    assert _sent_metadata(pool)["consentQuery"] == f"vp={VP_ID}"
    assert _emails(ddb.meetings[CALL_ID]["Participants"])[-1] == "new@example.com"
    assert len(ddb.vps[VP_ID]["participants"]["L"]) == 2


def test_an_address_already_listed_is_not_listed_again(pool, ddb):
    create_user({"email": "DI@example.com", "role": "Participant", "inviteToMeeting": CALL_ID})
    assert ddb.appended() == []
    assert _sent_metadata(pool)["consentQuery"] == f"meeting={CALL_ID}"


def test_an_unknown_meeting_is_refused_and_no_account_is_made(pool, ddb):
    with pytest.raises(Exception, match="no meeting or Virtual Participant"):
        create_user({"email": "new@example.com", "role": "User", "inviteToMeeting": "nope"})
    assert "admin_create_user" not in pool.names_called() and ddb.appended() == []


def test_an_existing_account_is_refused_before_the_list_is_changed(pool, ddb):
    pool.add("new@example.com")
    with pytest.raises(Exception, match="already exists"):
        create_user({"email": "new@example.com", "role": "User", "inviteToMeeting": CALL_ID})
    assert ddb.appended() == []


def test_a_full_list_is_refused_and_no_account_is_made(pool, ddb):
    people = [{"name": "", "email": f"p{n}@example.com"} for n in range(index.MAX_PARTICIPANTS)]
    ddb.meetings[CALL_ID]["Participants"] = _participants_attr(people)
    with pytest.raises(Exception, match="already has 100 people"):
        create_user({"email": "new@example.com", "role": "User", "inviteToMeeting": CALL_ID})
    assert "admin_create_user" not in pool.names_called()


def test_a_blank_meeting_is_the_same_as_none(pool, ddb):
    create_user({"email": "new@example.com", "role": "User", "inviteToMeeting": "  "})
    assert "lmaInvitation" not in _sent_metadata(pool)


def test_only_an_admin_may_create_a_user_for_a_meeting(pool, ddb):
    with pytest.raises(Exception, match="Unauthorized"):
        create_user(
            {"email": "new@example.com", "role": "User", "inviteToMeeting": CALL_ID},
            username=OWNER,
            groups=["User"],
        )
    assert pool.calls == [] and ddb.updates == []


# ── a throttled lookup ───────────────────────────────────────────────────────


def _throttle(pool, monkeypatch, address, times):
    """Make the user pool throttle the lookups of one address, ``times`` times."""
    real = pool.admin_get_user
    left = {"n": times}

    def admin_get_user(**kwargs):
        if kwargs["Username"].casefold() == address and left["n"]:
            left["n"] -= 1
            raise _CognitoError("TooManyRequestsException")
        return real(**kwargs)

    monkeypatch.setattr(pool, "admin_get_user", admin_get_user)
    monkeypatch.setattr(index.time, "sleep", lambda _s: None)
    return left


def test_a_throttled_lookup_is_tried_again(pool, ddb, monkeypatch):
    pool.add("ana@example.com", groups=["Participant"])
    _throttle(pool, monkeypatch, "ana@example.com", index.LOOKUP_ATTEMPTS - 1)
    answer = {e["email"]: e["account"] for e in accounts({"CallId": CALL_ID})}
    assert answer["ana@example.com"] == "Participant"


def test_an_address_still_throttled_is_answered_unknown_and_the_rest_are_answered(
    pool, ddb, monkeypatch
):
    pool.add("cy@example.net", groups=["Participant"])
    _throttle(pool, monkeypatch, "ana@example.com", 99)
    answer = {e["email"]: e["account"] for e in accounts({"CallId": CALL_ID})}
    assert answer == {
        "ana@example.com": "unknown",
        "Bo@Example.org": "none",
        "cy@example.net": "Participant",
        "di@example.com": "none",
    }


def test_an_address_still_throttled_is_not_created_and_says_to_try_again(pool, ddb, monkeypatch):
    _throttle(pool, monkeypatch, "ana@example.com", 99)
    result = create({"CallId": CALL_ID, "emails": ["ana@example.com", "di@example.com"]})
    outcomes = {r["email"]: (r["outcome"], r["message"]) for r in result["results"]}
    assert outcomes["ana@example.com"][0] == "failed"
    assert "try again" in outcomes["ana@example.com"][1]
    assert outcomes["di@example.com"] == ("created", None)
    assert "ana@example.com" not in pool.users


def test_an_error_other_than_a_throttle_still_fails_the_answer(pool, ddb, monkeypatch):
    def broken(**_kwargs):
        raise _CognitoError("InternalErrorException")

    monkeypatch.setattr(pool, "admin_get_user", broken)
    with pytest.raises(ClientError):
        accounts({"CallId": CALL_ID})


def test_lookups_past_the_deadline_are_answered_unknown_at_once(pool, ddb, monkeypatch):
    pool.add("ana@example.com", groups=["Participant"])
    monkeypatch.setattr(index, "LOOKUP_DEADLINE_SECONDS", -1.0)
    answer = {e["email"]: e["account"] for e in accounts({"CallId": CALL_ID})}
    assert set(answer.values()) == {"unknown"}
    assert "admin_get_user" not in pool.names_called()


def test_a_throttled_lookup_does_not_pause_past_the_deadline(pool, ddb, monkeypatch):
    _throttle(pool, monkeypatch, "ana@example.com", 99)
    pauses = []
    monkeypatch.setattr(index.time, "sleep", pauses.append)
    monkeypatch.setattr(index, "LOOKUP_DEADLINE_SECONDS", 0.3)
    answer = {e["email"]: e["account"] for e in accounts({"CallId": CALL_ID})}
    assert answer["ana@example.com"] == "unknown"
    assert sum(pauses) < 0.3
