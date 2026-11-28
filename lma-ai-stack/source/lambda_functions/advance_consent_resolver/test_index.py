#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Tests for the advance consent resolver.

What a person depends on: a consent is given only by someone on the meeting's
participant list, with exactly the text the page showed; it is stored with
who gave it, when, for what and until when, and without their device unless
the deployment keeps it; each person reads and withdraws only their own; and
someone not on the list learns nothing about the meeting and can only ask to be
added, which tells the owner in LMA and records nothing about consent.

No AWS: the module's clients are built at import time and replaced here.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("EVENT_SOURCING_TABLE", "lma-call-events")
os.environ.setdefault("VIRTUAL_PARTICIPANT_TABLE", "lma-vps")
os.environ.setdefault("USER_POOL_ID", "us-east-1_pool")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "lambda_layers" / "configuration_layer"))


def _load():
    spec = importlib.util.spec_from_file_location(
        "advance_consent_resolver_index", HERE / "index.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load()
from lma_consent import advance, rules  # noqa: E402  pylint: disable=wrong-import-position

OWNER = "owner@example.com"
ANA = "ana@example.com"
BO = "bo@example.com"
CALL_ID = "Weekly sync - 2026-11-30"
VP_ID = "vp-123"


def _failed():
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "Op")


def _key_values(condition) -> Dict[str, Any]:
    """The PK and SK prefix of a boto3 Key condition, as the resolver builds it."""
    out: Dict[str, Any] = {}

    def walk(node):
        expression = node.get_expression()
        operator = expression["operator"]
        if operator == "AND":
            for part in expression["values"]:
                walk(part)
        elif operator == "=":
            out[expression["values"][0].name] = ("eq", expression["values"][1])
        elif operator == "begins_with":
            out[expression["values"][0].name] = ("begins", expression["values"][1])

    walk(condition)
    return out


class FakeTable:
    """One table: items by (hash, range) key, with the conditions the resolver writes on."""

    def __init__(self, hash_key: str, range_key: str | None = None) -> None:
        self.hash_key, self.range_key = hash_key, range_key
        self.items: Dict[tuple, Dict[str, Any]] = {}
        #: Whether each get_item asked for a consistent read, in order.
        self.consistent_reads: List[bool] = []

    def _key(self, key):
        return (key[self.hash_key], key.get(self.range_key) if self.range_key else None)

    def get_item(self, Key, **kwargs):  # noqa: N803
        self.consistent_reads.append(bool(kwargs.get("ConsistentRead")))
        item = self.items.get(self._key(Key))
        return {"Item": copy.deepcopy(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):  # noqa: N803
        key = self._key(Item)
        current = self.items.get(key)
        if ConditionExpression == "attribute_not_exists(PK)" and current:
            raise _failed()
        if ConditionExpression and "ExpiresAfter < :now" in ConditionExpression and current:
            if not current.get("ExpiresAfter", 0) < ExpressionAttributeValues[":now"]:
                raise _failed()
        self.items[key] = copy.deepcopy(Item)

    def delete_item(self, Key):  # noqa: N803
        self.items.pop(self._key(Key), None)

    #: Items per page, as DynamoDB pages a long partition.
    page_size = 2

    def query(self, KeyConditionExpression, ScanIndexForward=True, Limit=None, **kwargs):  # noqa: N803
        wanted = _key_values(KeyConditionExpression)
        out = []
        for (hash_value, range_value), item in self.items.items():
            if hash_value != wanted[self.hash_key][1]:
                continue
            kind, prefix = wanted.get(self.range_key, ("begins", ""))
            if kind == "begins" and not str(range_value).startswith(prefix):
                continue
            out.append(copy.deepcopy(item))
        out.sort(key=lambda i: i[self.range_key], reverse=not ScanIndexForward)
        if Limit:
            return {"Items": out[:Limit]}
        start = int((kwargs.get("ExclusiveStartKey") or {}).get("n", 0))
        page = out[start : start + self.page_size]
        if kwargs.get("FilterExpression") is not None:
            # The one filter the resolver uses: not withdrawn.
            page = [item for item in page if "WithdrawnAt" not in item]
        more = start + self.page_size < len(out)
        return {
            "Items": page,
            **({"LastEvaluatedKey": {"n": start + self.page_size}} if more else {}),
        }

    def update_item(self, Key, UpdateExpression, ConditionExpression="", **kwargs):  # noqa: N803
        values = kwargs.get("ExpressionAttributeValues") or {}
        key = self._key(Key)
        current = self.items.get(key)
        if UpdateExpression.startswith("SET WithdrawnAt"):
            if not current or current.get("WithdrawnAt"):
                raise _failed()
            if (
                "ValidUntil > :w" in ConditionExpression
                and not current["ValidUntil"] > values[":w"]
            ):
                raise _failed()
            if "Subject = :s" in ConditionExpression and current.get("Subject") != values[":s"]:
                raise _failed()
            current.update(
                WithdrawnAt=values[":w"], WithdrawnReason=values[":r"], ExpiresAfter=values[":e"]
            )
            if ":b" in values:
                current["WithdrawnBy"] = values[":b"]
            if ":n" in values:
                current["WithdrawnNote"] = values[":n"]
            return {"Attributes": copy.deepcopy(current)}
        if UpdateExpression.startswith("ADD #n"):
            current = current or dict(Key)
            if current.get("Count", 0) >= values[":max"]:
                raise _failed()
            current["Count"] = current.get("Count", 0) + 1
            self.items[key] = current
            return {}
        if "participantListRequests" in UpdateExpression:
            if not current:
                raise _failed()
            if "attribute_not_exists(participantListRequests)" in ConditionExpression:
                if "participantListRequests" in current:
                    raise _failed()
            elif current.get("participantListRequests") != values[":old"]:
                raise _failed()
            current["participantListRequests"] = copy.deepcopy(values[":r"])
            return {}
        raise AssertionError(f"unexpected update {UpdateExpression}")


class FakeDynamo:
    def __init__(self) -> None:
        self.events = FakeTable("PK", "SK")
        self.vps = FakeTable("id")

    def Table(self, name):  # noqa: N802
        return self.events if name == index.EVENT_SOURCING_TABLE else self.vps


class FakeCognito:
    class exceptions:  # noqa: N801
        class UserNotFoundException(Exception):
            pass

    def __init__(self) -> None:
        self.users: Dict[str, Dict[str, str]] = {}

    def add(self, username, email=None, verified=True):
        self.users[username] = {
            "sub": f"sub-{username}",
            "email": email or username,
            "email_verified": "true" if verified else "false",
        }

    def admin_get_user(self, UserPoolId, Username):  # noqa: N803
        if Username not in self.users:
            raise self.exceptions.UserNotFoundException()
        return {
            "UserAttributes": [{"Name": k, "Value": v} for k, v in self.users[Username].items()]
        }


class FakeNamesClient:
    """The low-level client the account names are kept with (lma_consent.account_names)."""

    class exceptions:  # noqa: N801 - mirrors botocore's client.exceptions
        class TransactionCanceledException(Exception):
            pass

    def __init__(self) -> None:
        self.items: Dict[tuple, Dict[str, Any]] = {}

    @staticmethod
    def _key(key):
        return (key["PK"]["S"], key["SK"]["S"])

    def get_item(self, TableName, Key, **_kwargs):  # noqa: N803
        item = self.items.get(self._key(Key))
        return {"Item": copy.deepcopy(item)} if item else {}

    def transact_write_items(self, TransactItems):  # noqa: N803
        for entry in TransactItems:
            if "Put" in entry:
                self.items[self._key(entry["Put"]["Item"])] = copy.deepcopy(entry["Put"]["Item"])
            else:
                self.items.pop(self._key(entry["Delete"]["Key"]), None)


@pytest.fixture
def aws(monkeypatch):
    dynamo, cognito = FakeDynamo(), FakeCognito()
    for name in (OWNER, ANA, BO):
        cognito.add(name)
    names = FakeNamesClient()
    monkeypatch.setattr(index, "DYNAMODB", dynamo)
    monkeypatch.setattr(index, "DYNAMODB_CLIENT", names)
    monkeypatch.setattr(index, "COGNITO", cognito)
    settings: Dict[str, Any] = {}
    monkeypatch.setattr(index, "consent_settings", lambda: rules.effective_settings(settings))
    published = []

    def publish(call_id, **kwargs):
        published.append((call_id, kwargs))
        return {}

    import lma_notifications  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(lma_notifications, "publish_meeting_notification", publish)
    dynamo.events.items[(f"c#{CALL_ID}", f"c#{CALL_ID}")] = {
        "PK": f"c#{CALL_ID}",
        "SK": f"c#{CALL_ID}",
        "CallId": CALL_ID,
        "Owner": OWNER,
        "Participants": [{"name": "Ana Silva", "email": "Ana@Example.com"}],
        "Labels": [{"Key": "engagement", "Value": "acme"}],
    }
    return {
        "dynamo": dynamo,
        "cognito": cognito,
        "settings": settings,
        "published": published,
        "names": names,
    }


def event(field, username, arguments=None, *, ip="203.0.113.7", agent="Browser/1.0"):
    return {
        "info": {"fieldName": field},
        "arguments": arguments or {},
        "identity": {
            "username": username,
            "claims": {"sub": f"sub-{username}"},
            "sourceIp": [ip],
        },
        "request": {"userAgent": agent},
    }


def call(field, username, arguments=None, **kwargs):
    return index.handler(event(field, username, arguments, **kwargs), None)


def invitation(username, **arguments):
    return call("getAdvanceConsentInvitation", username, {"CallId": CALL_ID, **arguments})


def give(username, **data):
    offer = invitation(username, **{k: v for k, v in data.items() if k != "NoticeHash"})
    data.setdefault("NoticeHash", offer["NoticeHash"])
    return call("giveAdvanceConsent", username, {"input": {"CallId": CALL_ID, **data}})


def stored(aws, email):
    return [item for (pk, _sk), item in aws["dynamo"].events.items.items() if pk == f"pmc#{email}"]


# ── who is asking ───────────────────────────────────────────────────────────


def test_a_backend_role_is_refused():
    with pytest.raises(index.Refused):
        index.handler(
            {
                "info": {"fieldName": "listMyAdvanceConsents"},
                "identity": {"userArn": "arn:aws:sts::123456789012:assumed-role/R/s"},
            },
            None,
        )


def test_the_address_is_the_user_pools_verified_one(aws):
    aws["cognito"].add("bo", email="BO@example.com", verified=False)
    with pytest.raises(index.Refused, match="verified"):
        call("listMyAdvanceConsents", "bo")
    aws["cognito"].users["bo"]["sub"] = "someone-else"
    with pytest.raises(index.Refused):
        call("listMyAdvanceConsents", "bo")


# ── the consent page ────────────────────────────────────────────────────────


def test_someone_not_on_the_list_learns_nothing_about_the_meeting(aws):
    answer = invitation(BO)
    assert answer == {"Listed": False, "Requested": False, "Enabled": True, "Email": BO}


def test_a_link_to_no_meeting_reads_as_not_listed(aws):
    answer = call("getAdvanceConsentInvitation", ANA, {"CallId": "no such meeting"})
    assert answer["Listed"] is False and "MeetingName" not in answer


def test_a_listed_person_sees_the_meeting_and_the_exact_text(aws):
    answer = invitation(ANA)
    assert answer["Listed"] is True
    assert answer["MeetingName"] == CALL_ID and answer["Organizer"] == OWNER
    assert answer["ListedName"] == "Ana Silva"
    assert answer["Scope"] == "meeting"
    assert f'the meeting "{CALL_ID}"' in answer["NoticeText"]
    assert OWNER in answer["NoticeText"]
    assert answer["NoticeHash"] == advance.text_hash(answer["NoticeText"])
    assert answer["Labels"] == [{"Key": "engagement", "Value": "acme"}]


def test_the_standing_text_names_the_date_and_the_label(aws):
    until = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    answer = invitation(ANA, Scope="organizer", ValidUntil=until, LabelKey="engagement")
    assert "every meeting they organize that is labelled engagement: acme" in answer["NoticeText"]
    assert f"until the end of {until} (UTC)" in answer["NoticeText"]


def test_a_vp_link_reads_the_meeting_the_vp_created(aws):
    aws["dynamo"].vps.items[(VP_ID, None)] = {
        "id": VP_ID,
        "Owner": OWNER,
        "meetingName": "Weekly sync",
        "participants": [{"name": "", "email": BO}],
        "CallId": CALL_ID,
    }
    # The meeting's own list (Ana), not the Virtual Participant's copy (Bo).
    answer = call("getAdvanceConsentInvitation", ANA, {"VirtualParticipantId": VP_ID})
    assert answer["Listed"] is True and answer["CallId"] == CALL_ID
    assert answer["MeetingName"] == "Weekly sync"
    assert (
        call("getAdvanceConsentInvitation", BO, {"VirtualParticipantId": VP_ID})["Listed"] is False
    )


def test_exactly_one_meeting_id_is_named(aws):
    with pytest.raises(index.BadRequest):
        call("getAdvanceConsentInvitation", ANA, {})
    with pytest.raises(index.BadRequest):
        call("getAdvanceConsentInvitation", ANA, {"CallId": CALL_ID, "VirtualParticipantId": VP_ID})


# ── giving consent ──────────────────────────────────────────────────────────


def test_a_consent_records_who_when_what_and_the_text_without_the_device(aws):
    answer = give(ANA)
    [item] = stored(aws, ANA)
    assert item["Email"] == ANA and item["Subject"] == f"sub-{ANA}"
    assert item["Source"] == "pre_meeting" and item["Scope"] == "meeting"
    assert item["CallId"] == CALL_ID and item["Organizer"] == OWNER
    assert item["NoticeHash"] == advance.text_hash(item["NoticeText"])
    assert item["NoticeVersion"] == advance.text_version(None)
    assert "DeviceDetails" not in item
    granted = advance.parse_time(item["GrantedAt"])
    until = advance.parse_time(item["ValidUntil"])
    assert timedelta(days=364) < until - granted <= timedelta(days=365)
    assert item["ExpiresAfter"] > until.timestamp()
    assert answer["Status"] == "active" and answer["DeviceDetailsKept"] is False


def test_device_details_are_kept_only_when_the_setting_is_on(aws):
    aws["settings"]["keepAdvanceConsentDeviceDetails"] = True
    answer = give(ANA)
    [item] = stored(aws, ANA)
    assert item["DeviceDetails"] == {"SourceIp": "203.0.113.7", "UserAgent": "Browser/1.0"}
    assert answer["DeviceDetailsKept"] is True
    assert "DeviceDetails" not in answer and "SourceIp" not in str(answer)


def test_only_a_listed_person_can_give_consent(aws):
    with pytest.raises(index.Refused):
        call(
            "giveAdvanceConsent",
            BO,
            {"input": {"CallId": CALL_ID, "NoticeHash": invitation(ANA)["NoticeHash"]}},
        )
    assert stored(aws, BO) == []


def test_consent_is_given_only_for_the_text_the_page_showed(aws):
    with pytest.raises(index.BadRequest, match="changed"):
        give(ANA, NoticeHash="0" * 64)
    shown = invitation(ANA)["NoticeHash"]
    aws["settings"]["advanceConsentText"] = "{organizer} may record {scope}."
    with pytest.raises(index.BadRequest, match="changed"):
        give(ANA, NoticeHash=shown)
    assert stored(aws, ANA) == []


def test_a_standing_consent_runs_to_a_date_at_most_a_year_ahead(aws):
    today = datetime.now(timezone.utc)
    with pytest.raises(index.BadRequest):
        give(ANA, Scope="organizer")
    with pytest.raises(index.BadRequest):
        give(ANA, Scope="organizer", ValidUntil=(today + timedelta(days=366)).strftime("%Y-%m-%d"))
    with pytest.raises(index.BadRequest):
        give(ANA, Scope="organizer", ValidUntil=(today - timedelta(days=1)).strftime("%Y-%m-%d"))
    until = (today + timedelta(days=365)).strftime("%Y-%m-%d")
    answer = give(ANA, Scope="organizer", ValidUntil=until)
    assert answer["ValidUntil"] == f"{until}T23:59:59Z" and answer["Organizer"] == OWNER
    assert answer["CallId"] is None


def test_the_setting_shortens_the_longest_period(aws):
    aws["settings"]["advanceConsentMaxDays"] = 30
    late = (datetime.now(timezone.utc) + timedelta(days=31)).strftime("%Y-%m-%d")
    with pytest.raises(index.BadRequest, match="30 days"):
        give(ANA, Scope="organizer", ValidUntil=late)


def test_a_label_must_be_one_of_the_meetings(aws):
    until = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%d")
    with pytest.raises(index.BadRequest, match="label"):
        give(ANA, Scope="organizer", ValidUntil=until, LabelKey="other")
    answer = give(ANA, Scope="organizer", ValidUntil=until, LabelKey="engagement")
    assert (answer["LabelKey"], answer["LabelValue"]) == ("engagement", "acme")


def test_a_new_consent_for_the_same_thing_replaces_the_earlier_one(aws):
    first = give(ANA)
    second = give(ANA)
    consents = {c["ConsentId"]: c for c in call("listMyAdvanceConsents", ANA)["Consents"]}
    assert consents[first["ConsentId"]]["Status"] == "withdrawn"
    assert consents[first["ConsentId"]]["WithdrawnReason"] == "replaced"
    assert consents[second["ConsentId"]]["Status"] == "active"


def test_nothing_new_is_taken_when_the_deployment_turns_it_off(aws):
    give(ANA)
    aws["settings"]["advanceConsentEnabled"] = False
    with pytest.raises(index.Refused):
        call("giveAdvanceConsent", ANA, {"input": {"CallId": CALL_ID, "NoticeHash": "x"}})
    answer = call("listMyAdvanceConsents", ANA)
    assert answer["Enabled"] is False and len(answer["Consents"]) == 1
    assert invitation(ANA)["NoticeText"] is None


# ── reading and withdrawing your own ────────────────────────────────────────


def test_each_person_reads_only_their_own_consents(aws):
    give(ANA)
    aws["dynamo"].events.items[(f"c#{CALL_ID}", f"c#{CALL_ID}")]["Participants"].append(
        {"name": "Bo", "email": BO}
    )
    give(BO)
    assert {c["Email"] for c in call("listMyAdvanceConsents", ANA)["Consents"]} == {ANA}
    assert {c["Email"] for c in call("listMyAdvanceConsents", BO)["Consents"]} == {BO}


def test_withdrawing_stops_it_counting_and_keeps_the_record(aws):
    given = give(ANA)
    answer = call("withdrawAdvanceConsent", ANA, {"input": {"ConsentId": given["ConsentId"]}})
    assert answer["Status"] == "withdrawn" and answer["WithdrawnAt"]
    [item] = stored(aws, ANA)
    meeting = {"CallId": CALL_ID, "Owner": OWNER, "Labels": []}
    assert advance.covers(item, meeting, datetime.now(timezone.utc)) is False
    assert invitation(ANA)["ActiveConsents"] == []
    # Withdrawing again answers the record as it is.
    again = call("withdrawAdvanceConsent", ANA, {"input": {"ConsentId": given["ConsentId"]}})
    assert again["WithdrawnAt"] == answer["WithdrawnAt"]


def test_nobody_can_withdraw_another_persons_consent(aws):
    given = give(ANA)
    with pytest.raises(index.BadRequest, match="not one of your consents"):
        call("withdrawAdvanceConsent", BO, {"input": {"ConsentId": given["ConsentId"]}})
    assert stored(aws, ANA)[0].get("WithdrawnAt") is None
    with pytest.raises(index.BadRequest):
        call("withdrawAdvanceConsent", ANA, {"input": {"ConsentId": "../pmc#bo@example.com"}})


def test_a_replacing_consent_takes_over_the_meetings_that_counted_the_one_it_replaced(
    aws, monkeypatch
):
    first = give(ANA)
    key = (f"pmc#{ANA}", f"ac#{first['ConsentId']}")
    aws["dynamo"].events.items[key]["AppliedAt"] = {CALL_ID: "2026-11-25T10:00:00Z"}
    second = give(ANA)
    invoked = []

    class FakeLambda:
        def invoke(self, **kwargs):
            invoked.append(json.loads(kwargs["Payload"])["advanceConsentWithdrawn"])
            return {}

    monkeypatch.setattr(index, "LAMBDA", FakeLambda())
    monkeypatch.setattr(index, "MEETING_CONSENT_FUNCTION", "consent-function")
    call("withdrawAdvanceConsent", ANA, {"input": {"ConsentId": second["ConsentId"]}})
    [told] = invoked
    assert told["CallId"] == CALL_ID
    assert told["ConsentIds"] == [second["ConsentId"], first["ConsentId"]]


def test_an_old_consent_in_force_is_found_past_newer_withdrawn_ones(aws):
    first = give(ANA)
    for _ in range(4):
        given = give(
            ANA,
            Scope="organizer",
            ValidUntil=(datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d"),
        )
        call("withdrawAdvanceConsent", ANA, {"input": {"ConsentId": given["ConsentId"]}})
    assert [c["ConsentId"] for c in invitation(ANA)["ActiveConsents"]] == [first["ConsentId"]]
    # A new consent for the same meeting still replaces it.
    second = give(ANA)
    statuses = {c["ConsentId"]: c["Status"] for c in call("listMyAdvanceConsents", ANA)["Consents"]}
    assert (statuses[first["ConsentId"]], statuses[second["ConsentId"]]) == ("withdrawn", "active")


def test_the_consent_page_lists_the_consents_that_cover_the_meeting(aws):
    given = give(ANA)
    assert [c["ConsentId"] for c in invitation(ANA)["ActiveConsents"]] == [given["ConsentId"]]


# ── asking to be added ──────────────────────────────────────────────────────


def test_asking_to_be_added_notifies_the_owner_once_and_records_no_consent(aws):
    answer = call("askToBeAddedToMeeting", BO, {"input": {"CallId": CALL_ID, "Name": "Bo *Chen*"}})
    assert answer == {"Requested": True, "Listed": False}
    [(call_id, notification)] = aws["published"]
    assert call_id == CALL_ID
    assert notification["category"] == "meeting"
    assert notification["type"] == "participant_list_request"
    assert "Bo \\*Chen\\* \\<bo@example\\.com\\>" in notification["body"]
    assert notification["actions"][0]["payload"] == {"text": "Bo *Chen* <bo@example.com>"}
    assert notification["timeout"] == 10
    # The notification is one the publish helper accepts.
    import lma_notifications  # pylint: disable=import-outside-toplevel

    built = lma_notifications.build_input(
        call_id, **{k: v for k, v in notification.items() if k != "timeout"}
    )
    assert json.loads(built["Actions"][0]["Payload"]) == {"text": "Bo *Chen* <bo@example.com>"}
    assert stored(aws, BO) == []
    # Asking again, and the page, say it was asked; nobody is notified again.
    call("askToBeAddedToMeeting", BO, {"input": {"CallId": CALL_ID}})
    assert len(aws["published"]) == 1
    assert invitation(BO)["Requested"] is True


def test_a_listed_person_is_not_asked_for(aws):
    answer = call("askToBeAddedToMeeting", ANA, {"input": {"CallId": CALL_ID}})
    assert answer == {"Requested": False, "Listed": True}
    assert aws["published"] == []


def test_asking_for_a_scheduled_vp_is_kept_on_its_row_for_the_owner(aws):
    aws["dynamo"].vps.items[(VP_ID, None)] = {
        "id": VP_ID,
        "Owner": OWNER,
        "meetingName": "Planning",
        "participants": [{"name": "Ana", "email": ANA}],
    }
    call("askToBeAddedToMeeting", BO, {"input": {"VirtualParticipantId": VP_ID, "Name": "Bo"}})
    [request] = aws["dynamo"].vps.items[(VP_ID, None)]["participantListRequests"]
    assert request["email"] == BO and request["name"] == "Bo" and request["requestedAt"]
    assert aws["published"] == []


def test_a_full_vp_row_drops_answered_and_oldest_requests_rather_than_refusing(aws, monkeypatch):
    monkeypatch.setattr(index, "MAX_REQUESTS_PER_VP", 3)
    aws["dynamo"].vps.items[(VP_ID, None)] = {
        "id": VP_ID,
        "Owner": OWNER,
        "participants": [
            {"name": "Ana", "email": ANA},
            {"name": "Old", "email": "old@example.com"},
        ],
        "participantListRequests": [
            {"name": "Old", "email": "old@example.com", "requestedAt": "2026-11-01T00:00:00Z"},
            {"name": "P1", "email": "p1@example.com", "requestedAt": "2026-11-02T00:00:00Z"},
            {"name": "P2", "email": "p2@example.com", "requestedAt": "2026-11-03T00:00:00Z"},
        ],
    }
    for person in ("p3@example.com", BO):
        aws["cognito"].add(person)
        call("askToBeAddedToMeeting", person, {"input": {"VirtualParticipantId": VP_ID}})
    kept = [r["email"] for r in aws["dynamo"].vps.items[(VP_ID, None)]["participantListRequests"]]
    # Old was added to the list since; P1 is the oldest still waiting.
    assert kept == ["p2@example.com", "p3@example.com", BO]


def test_a_request_that_reached_nobody_can_be_made_again(aws, monkeypatch):
    import lma_notifications  # pylint: disable=import-outside-toplevel

    def failing(*_args, **_kwargs):
        raise lma_notifications.NotificationRateLimited("too many")

    monkeypatch.setattr(lma_notifications, "publish_meeting_notification", failing)
    with pytest.raises(index.BadRequest, match="try again"):
        call("askToBeAddedToMeeting", BO, {"input": {"CallId": CALL_ID}})
    assert invitation(BO)["Requested"] is False
    monkeypatch.setattr(lma_notifications, "publish_meeting_notification", lambda *a, **k: {})
    assert call("askToBeAddedToMeeting", BO, {"input": {"CallId": CALL_ID}})["Requested"] is True


def test_a_link_to_no_meeting_is_answered_as_one_the_caller_is_not_listed_on(aws, monkeypatch):
    monkeypatch.setattr(index, "ASKS_PER_DAY", 1)
    missing = {"CallId": "no such meeting"}
    assert call("getAdvanceConsentInvitation", BO, missing)["Requested"] is False
    assert call("askToBeAddedToMeeting", BO, {"input": missing}) == {
        "Requested": True,
        "Listed": False,
    }
    assert call("getAdvanceConsentInvitation", BO, missing)["Requested"] is True
    assert aws["published"] == []
    # Counted the same, so the next request is refused either way.
    with pytest.raises(index.BadRequest, match="at most 1"):
        call("askToBeAddedToMeeting", BO, {"input": {"CallId": CALL_ID}})


def test_a_consent_from_the_vp_link_covers_the_meeting_it_created(aws):
    aws["dynamo"].vps.items[(VP_ID, None)] = {
        "id": VP_ID,
        "Owner": OWNER,
        "meetingName": "Weekly sync",
        "participants": [{"name": "Ana Silva", "email": ANA}],
    }
    page = call("getAdvanceConsentInvitation", ANA, {"VirtualParticipantId": VP_ID})
    given = call(
        "giveAdvanceConsent",
        ANA,
        {"input": {"VirtualParticipantId": VP_ID, "NoticeHash": page["NoticeHash"]}},
    )
    assert given["CallId"] is None and given["VirtualParticipantId"] == VP_ID
    # The VP creates its meeting; the meeting's own link is used from then on.
    aws["dynamo"].vps.items[(VP_ID, None)]["CallId"] = CALL_ID
    assert [c["ConsentId"] for c in invitation(ANA)["ActiveConsents"]] == [given["ConsentId"]]
    again = give(ANA)
    statuses = {
        c["ConsentId"]: (c["Status"], c["WithdrawnReason"])
        for c in call("listMyAdvanceConsents", ANA)["Consents"]
    }
    assert statuses[given["ConsentId"]] == ("withdrawn", "replaced")
    assert statuses[again["ConsentId"]] == ("active", None)


def test_an_expired_consent_is_not_withdrawn_and_keeps_its_expiry(aws):
    given = give(ANA)
    [key] = [k for k in aws["dynamo"].events.items if k[0] == f"pmc#{ANA}"]
    item = aws["dynamo"].events.items[key]
    item["ValidUntil"] = "2026-01-01T00:00:00Z"
    item["ExpiresAfter"] = 1
    answer = call("withdrawAdvanceConsent", ANA, {"input": {"ConsentId": given["ConsentId"]}})
    assert answer["Status"] == "expired" and answer["WithdrawnAt"] is None
    assert item["ExpiresAfter"] == 1


def test_asking_is_limited_per_day(aws, monkeypatch):
    monkeypatch.setattr(index, "ASKS_PER_DAY", 1)
    call("askToBeAddedToMeeting", BO, {"input": {"CallId": CALL_ID}})
    aws["dynamo"].events.items[("c#other", "c#other")] = {
        "PK": "c#other",
        "SK": "c#other",
        "Owner": OWNER,
        "Participants": [],
    }
    with pytest.raises(index.BadRequest, match="at most 1"):
        call("askToBeAddedToMeeting", BO, {"input": {"CallId": "other"}})
    # The refused request is not kept, so it can be asked again tomorrow.
    assert ("pmr#c#other", BO) not in aws["dynamo"].events.items


def test_a_withdrawal_reaches_each_meeting_that_counted_the_consent(aws, monkeypatch):
    given = give(ANA)
    [key] = [k for k in aws["dynamo"].events.items if k[0] == f"pmc#{ANA}"]
    aws["dynamo"].events.items[key]["AppliedAt"] = {
        CALL_ID: "2026-11-25T10:00:00Z",
        "Earlier meeting": "2026-11-20T10:00:00Z",
    }
    invoked = []

    class FakeLambda:
        def invoke(self, **kwargs):
            invoked.append(kwargs)
            return {}

    monkeypatch.setattr(index, "LAMBDA", FakeLambda())
    monkeypatch.setattr(
        index, "MEETING_CONSENT_FUNCTION", "arn:aws:lambda:us-east-1:123456789012:function:consent"
    )
    call("withdrawAdvanceConsent", ANA, {"input": {"ConsentId": given["ConsentId"]}})
    assert {json.loads(i["Payload"])["advanceConsentWithdrawn"]["CallId"] for i in invoked} == {
        CALL_ID,
        "Earlier meeting",
    }
    assert all(i["InvocationType"] == "Event" for i in invoked)
    assert all(
        json.loads(i["Payload"])["advanceConsentWithdrawn"]["ConsentId"] == given["ConsentId"]
        for i in invoked
    )
    assert [json.loads(i["Payload"])["advanceConsentWithdrawn"]["CallId"] for i in invoked] == [
        CALL_ID,
        "Earlier meeting",
    ], "the most recently noted first"
    # Withdrawing again passes nothing on.
    invoked.clear()
    call("withdrawAdvanceConsent", ANA, {"input": {"ConsentId": given["ConsentId"]}})
    assert invoked == []


# ── standing consents given with no meeting named ───────────────────────────

PRESETS = HERE.parents[1] / "lambda_layers" / "configuration_layer" / "lma_config" / "presets"


def _day(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")


def standing_offer(username, **arguments):
    return call("getAdvanceConsentInvitation", username, arguments)


def give_standing(username, **data):
    offer = standing_offer(username, **{k: v for k, v in data.items() if k != "NoticeHash"})
    data.setdefault("NoticeHash", offer["NoticeHash"])
    return call("giveAdvanceConsent", username, {"input": data})


def test_a_consent_for_every_meeting_needs_no_link_and_names_no_meeting(aws):
    # Bo is on no meeting's participant list.
    until = _day(30)
    offer = standing_offer(BO, Scope="deployment", ValidUntil=until)
    assert offer["Listed"] is False and "MeetingName" not in offer
    assert offer["Scope"] == "deployment" and offer["Organizer"] is None
    assert "every meeting on this Live Meeting Assistant deployment" in offer["NoticeText"]
    assert f"until the end of {until} (UTC)" in offer["NoticeText"]
    assert offer["NoticeText"].startswith("I agree that the organizer of each meeting may")
    assert offer["NoticeHash"] == advance.text_hash(offer["NoticeText"])
    answer = give_standing(BO, Scope="deployment", ValidUntil=until)
    [item] = stored(aws, BO)
    assert item["Scope"] == "deployment" and item["Email"] == BO
    assert item["NoticeText"] == offer["NoticeText"] and item["NoticeHash"] == offer["NoticeHash"]
    assert item["NoticeVersion"] == advance.text_version(None)
    for absent in ("CallId", "VirtualParticipantId", "MeetingName", "Organizer", "LabelKey"):
        assert absent not in item
    assert answer["ValidUntil"] == f"{until}T23:59:59Z" and answer["Status"] == "active"
    meeting = {"CallId": CALL_ID, "Owner": OWNER, "Labels": []}
    assert advance.covers(item, meeting, datetime.now(timezone.utc))


def test_a_consent_for_an_organizer_names_the_address_typed(aws):
    until = _day(10)
    answer = give_standing(
        BO,
        Scope="organizer",
        Organizer=" Owner@Example.com ",
        ValidUntil=until,
        LabelKey="engagement",
        LabelValue="acme",
    )
    assert answer["Organizer"] == OWNER and answer["MeetingName"] is None
    assert (answer["LabelKey"], answer["LabelValue"]) == ("engagement", "acme")
    assert (
        f"I agree that {OWNER} may record and transcribe every meeting they organize"
        in answer["NoticeText"]
    )
    assert "labelled engagement: acme" in answer["NoticeText"]
    [item] = stored(aws, BO)
    meeting = {
        "CallId": CALL_ID,
        "Owner": OWNER,
        "Labels": [{"Key": "engagement", "Value": "acme"}],
    }
    assert advance.covers(item, meeting, datetime.now(timezone.utc))


def test_the_organizer_typed_is_not_looked_up(aws):
    looked_up = []
    real = aws["cognito"].admin_get_user

    def admin_get_user(UserPoolId, Username):  # noqa: N803
        looked_up.append(Username)
        return real(UserPoolId=UserPoolId, Username=Username)

    aws["cognito"].admin_get_user = admin_get_user
    until = _day(10)
    known = standing_offer(BO, Scope="organizer", Organizer=OWNER, ValidUntil=until)
    unknown = standing_offer(
        BO, Scope="organizer", Organizer="nobody@example.com", ValidUntil=until
    )
    assert looked_up == [BO, BO]
    assert set(known) == set(unknown)
    assert known["NoticeText"].replace(OWNER, "nobody@example.com") == unknown["NoticeText"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"Scope": "organizer", "ValidUntil": "2099-01-01", "Organizer": "not an address"},
        {"Scope": "organizer", "Organizer": OWNER},
        {"Scope": "deployment"},
        {"Scope": "organizer", "Organizer": OWNER, "LabelKey": "engagement"},
    ],
)
def test_a_standing_consent_needs_a_valid_organizer_date_and_label(aws, arguments):
    with pytest.raises(index.BadRequest):
        standing_offer(BO, **arguments)
    with pytest.raises(index.BadRequest):
        call("giveAdvanceConsent", BO, {"input": {**arguments, "NoticeHash": "0" * 64}})
    assert stored(aws, BO) == []


def test_a_consent_for_one_meeting_or_the_deployment_from_a_link_is_refused(aws):
    with pytest.raises(index.BadRequest):
        call("giveAdvanceConsent", BO, {"input": {"Scope": "meeting", "NoticeHash": "0" * 64}})
    with pytest.raises(index.BadRequest):
        invitation(ANA, Scope="deployment", ValidUntil=_day(10))
    assert stored(aws, BO) == [] and stored(aws, ANA) == []


def test_a_standing_consent_is_given_only_for_the_text_the_page_showed(aws):
    shown = standing_offer(BO, Scope="deployment", ValidUntil=_day(10))["NoticeHash"]
    with pytest.raises(index.BadRequest, match="changed"):
        give_standing(BO, Scope="deployment", ValidUntil=_day(11), NoticeHash=shown)
    aws["settings"]["advanceConsentText"] = "{organizer} may record {scope}."
    with pytest.raises(index.BadRequest, match="changed"):
        give_standing(BO, Scope="deployment", ValidUntil=_day(10), NoticeHash=shown)
    assert stored(aws, BO) == []
    # The deployment's configured text is the one shown and kept.
    answer = give_standing(BO, Scope="deployment", ValidUntil=_day(10))
    assert answer["NoticeText"].startswith("the organizer of each meeting may record every meeting")
    assert answer["NoticeVersion"] == advance.text_version("{organizer} may record {scope}.")


@pytest.mark.parametrize(
    "preset", ["standard", "regulated-notify", "regulated-consent-required", "team-hosted-external"]
)
@pytest.mark.parametrize("scope", ["deployment", "organizer"])
def test_a_standing_consent_is_capped_by_the_presets_longest_period(aws, preset, scope):
    values = json.loads((PRESETS / f"{preset}.json").read_text())["sections"]["meeting_consent"][
        "values"
    ]
    days = values["advanceConsentMaxDays"]
    aws["settings"]["advanceConsentMaxDays"] = days
    extra = {"Organizer": OWNER} if scope == "organizer" else {}
    with pytest.raises(index.BadRequest, match=f"{days} days"):
        standing_offer(BO, Scope=scope, ValidUntil=_day(days + 1), **extra)
    answer = give_standing(BO, Scope=scope, ValidUntil=_day(days), **extra)
    assert answer["Status"] == "active"
    assert standing_offer(BO, Scope=scope, ValidUntil=_day(1), **extra)["MaxDays"] == days


def test_the_regulated_and_team_hosted_presets_hold_standing_consents_to_180_days():
    for preset in ("regulated-notify", "regulated-consent-required", "team-hosted-external"):
        values = json.loads((PRESETS / f"{preset}.json").read_text())["sections"][
            "meeting_consent"
        ]["values"]
        assert values["advanceConsentMaxDays"] == 180


def test_a_new_consent_for_every_meeting_replaces_the_earlier_one_only(aws):
    first = give_standing(BO, Scope="deployment", ValidUntil=_day(10))
    organizer = give_standing(BO, Scope="organizer", Organizer=OWNER, ValidUntil=_day(10))
    offer = standing_offer(BO, Scope="deployment", ValidUntil=_day(20))
    assert [c["ConsentId"] for c in offer["ActiveConsents"]] == [first["ConsentId"]]
    second = give_standing(BO, Scope="deployment", ValidUntil=_day(20))
    consents = {c["ConsentId"]: c for c in call("listMyAdvanceConsents", BO)["Consents"]}
    assert consents[first["ConsentId"]]["WithdrawnReason"] == "replaced"
    assert consents[second["ConsentId"]]["Status"] == "active"
    assert consents[organizer["ConsentId"]]["Status"] == "active"


def test_standing_consents_are_listed_and_withdrawn_by_their_owner_only(aws):
    given = give_standing(BO, Scope="deployment", ValidUntil=_day(10))
    assert [c["ConsentId"] for c in call("listMyAdvanceConsents", ANA)["Consents"]] == []
    with pytest.raises(index.BadRequest):
        call("withdrawAdvanceConsent", ANA, {"input": {"ConsentId": given["ConsentId"]}})
    [listed] = call("listMyAdvanceConsents", BO)["Consents"]
    assert listed["Scope"] == "deployment" and listed["Status"] == "active"
    answer = call("withdrawAdvanceConsent", BO, {"input": {"ConsentId": given["ConsentId"]}})
    assert answer["Status"] == "withdrawn"


def test_no_standing_consent_is_taken_when_the_deployment_turns_it_off(aws):
    aws["settings"]["advanceConsentEnabled"] = False
    offer = standing_offer(BO, Scope="deployment", ValidUntil=_day(10))
    assert offer["Enabled"] is False and "NoticeText" not in offer
    with pytest.raises(index.Refused):
        call(
            "giveAdvanceConsent",
            BO,
            {"input": {"Scope": "deployment", "ValidUntil": _day(10), "NoticeHash": "x"}},
        )
    assert stored(aws, BO) == []


def test_a_request_kept_on_a_vp_whose_meeting_has_just_been_created_reaches_the_meeting(
    aws, monkeypatch
):
    """The VP links its CallId before its meeting exists. A request read while the
    meeting did not exist is kept on the row; if the meeting exists by the time it
    is kept, it is published to the meeting as well, as the START that created the
    meeting may already have read the row."""
    aws["dynamo"].vps.items[(VP_ID, None)] = {
        "id": VP_ID,
        "Owner": OWNER,
        "CallId": CALL_ID,
        "participants": [{"name": "Ana", "email": ANA}],
    }
    meeting = aws["dynamo"].events.items.pop((f"c#{CALL_ID}", f"c#{CALL_ID}"))
    real_keep = index._keep_request_on_vp

    def keep_then_meeting_created(*args):
        kept = real_keep(*args)
        aws["dynamo"].events.items[(f"c#{CALL_ID}", f"c#{CALL_ID}")] = meeting
        return kept

    monkeypatch.setattr(index, "_keep_request_on_vp", keep_then_meeting_created)
    answer = call("askToBeAddedToMeeting", BO, {"input": {"VirtualParticipantId": VP_ID}})
    assert answer == {"Requested": True, "Listed": False}
    [request] = aws["dynamo"].vps.items[(VP_ID, None)]["participantListRequests"]
    assert request["email"] == BO
    [(call_id, notification)] = aws["published"]
    assert call_id == CALL_ID and notification["type"] == "participant_list_request"
    assert notification["dedup_key"] == advance.request_dedup_key(BO)


def test_a_kept_request_whose_late_publish_fails_is_still_answered_as_made(aws, monkeypatch):
    aws["dynamo"].vps.items[(VP_ID, None)] = {"id": VP_ID, "Owner": OWNER, "CallId": CALL_ID}
    meeting = aws["dynamo"].events.items.pop((f"c#{CALL_ID}", f"c#{CALL_ID}"))
    real_keep = index._keep_request_on_vp

    def keep_then_meeting_created(*args):
        kept = real_keep(*args)
        aws["dynamo"].events.items[(f"c#{CALL_ID}", f"c#{CALL_ID}")] = meeting
        return kept

    import lma_notifications  # pylint: disable=import-outside-toplevel

    def failing(*_args, **_kwargs):
        raise lma_notifications.NotificationRateLimited("too many")

    monkeypatch.setattr(index, "_keep_request_on_vp", keep_then_meeting_created)
    monkeypatch.setattr(lma_notifications, "publish_meeting_notification", failing)
    answer = call("askToBeAddedToMeeting", BO, {"input": {"VirtualParticipantId": VP_ID}})
    assert answer == {"Requested": True, "Listed": False}
    assert aws["dynamo"].vps.items[(VP_ID, None)]["participantListRequests"][0]["email"] == BO


def test_a_request_on_a_meeting_and_one_on_its_vp_share_one_notification_key(aws):
    call("askToBeAddedToMeeting", BO, {"input": {"CallId": CALL_ID}})
    [(_, notification)] = aws["published"]
    assert notification == {
        **advance.participant_list_request_notification(BO, ""),
        "timeout": 10,
    }


def test_a_failed_second_look_for_the_meeting_still_answers_the_request_as_made(aws, monkeypatch):
    aws["dynamo"].vps.items[(VP_ID, None)] = {"id": VP_ID, "Owner": OWNER, "CallId": "not yet"}
    real_meeting_of = index.meeting_of

    def failing_second_look(arguments, consistent=False):
        if consistent:
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "Get")
        return real_meeting_of(arguments)

    monkeypatch.setattr(index, "meeting_of", failing_second_look)
    answer = call("askToBeAddedToMeeting", BO, {"input": {"VirtualParticipantId": VP_ID}})
    assert answer == {"Requested": True, "Listed": False}
    assert aws["dynamo"].vps.items[(VP_ID, None)]["participantListRequests"][0]["email"] == BO


def test_every_read_after_a_request_is_kept_on_a_vp_row_is_consistent(aws, monkeypatch):
    """The row is read consistently to keep the request, and again with its meeting
    afterwards, so a meeting created just before the request was kept is found.
    The call event processor's read of the row after creating the meeting is the
    getVirtualParticipant resolver's, which is consistent as well
    (lma-ai-stack/test/test_vp_read_is_consistent.py)."""
    aws["dynamo"].vps.items[(VP_ID, None)] = {"id": VP_ID, "Owner": OWNER, "CallId": CALL_ID}
    # The VP has linked its CallId; its meeting is created while the request is kept.
    meeting = aws["dynamo"].events.items.pop((f"c#{CALL_ID}", f"c#{CALL_ID}"))
    real_keep = index._keep_request_on_vp
    reads_before_keep = {}

    def keep(*args):
        reads_before_keep["vps"] = len(aws["dynamo"].vps.consistent_reads)
        reads_before_keep["events"] = len(aws["dynamo"].events.consistent_reads)
        kept = real_keep(*args)
        aws["dynamo"].events.items[(f"c#{CALL_ID}", f"c#{CALL_ID}")] = meeting
        return kept

    monkeypatch.setattr(index, "_keep_request_on_vp", keep)
    call("askToBeAddedToMeeting", BO, {"input": {"VirtualParticipantId": VP_ID}})
    vp_reads = aws["dynamo"].vps.consistent_reads[reads_before_keep["vps"] :]
    meeting_reads = aws["dynamo"].events.consistent_reads[reads_before_keep["events"] :]
    # The read that keeps the request, then the second look at the row.
    assert vp_reads == [True, True], vp_reads
    assert meeting_reads and all(meeting_reads), meeting_reads
    [(call_id, _)] = aws["published"]
    assert call_id == CALL_ID
