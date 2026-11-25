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
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

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

    def _key(self, key):
        return (key[self.hash_key], key.get(self.range_key) if self.range_key else None)

    def get_item(self, Key, **_kwargs):  # noqa: N803
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
            current.update(
                WithdrawnAt=values[":w"], WithdrawnReason=values[":r"], ExpiresAfter=values[":e"]
            )
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


@pytest.fixture
def aws(monkeypatch):
    dynamo, cognito = FakeDynamo(), FakeCognito()
    for name in (OWNER, ANA, BO):
        cognito.add(name)
    monkeypatch.setattr(index, "DYNAMODB", dynamo)
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
    return {"dynamo": dynamo, "cognito": cognito, "settings": settings, "published": published}


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
    assert notification["actions"][0]["payload"] == "Bo *Chen* <bo@example.com>"
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
