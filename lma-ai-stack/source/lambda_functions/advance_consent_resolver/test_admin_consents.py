#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Administering everyone's consents given in advance, and each account's own consents.

What an Admin and a person depend on: only an Admin lists, withdraws for
someone or erases; the list is filtered and paged on the server; a withdrawal
on someone's behalf has the effect of their own (the meetings that counted it
are told) and says it was an administrator's; an erasure withdraws what is in
force first, removes the consent items and keeps an audit entry with no
consent content. And a person reads and withdraws only the consents their own
account gave, not those of an earlier account that had their address. No AWS.
"""

from __future__ import annotations

import base64
import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_index as base  # noqa: E402  pylint: disable=wrong-import-position

index = base.index
aws = base.aws
ANA, BO, OWNER = base.ANA, base.BO, base.OWNER
SER, DES = TypeSerializer(), TypeDeserializer()
NOW = datetime.now(timezone.utc)
ADMIN = "admin@example.com"


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class LowClient:
    """The low-level client over the same items the resolver's Table reads and writes."""

    class exceptions:  # noqa: N801 - mirrors botocore's client.exceptions
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self, table: base.FakeTable) -> None:
        self.table = table
        self.puts: List[Dict[str, Any]] = []
        self.index_reads = 0

    @staticmethod
    def _plain(raw):
        return {k: DES.deserialize(v) for k, v in raw.items()}

    def query(self, TableName, KeyConditionExpression, ExpressionAttributeValues, **kw):  # noqa: N803
        if kw.get("IndexName") is None:
            # One person's partition, as an erasure reads it.
            pk = ExpressionAttributeValues[":pk"]["S"]
            rows = [i for (p, s), i in self.table.items.items() if p == pk and s.startswith("ac#")]
            return {"Items": [{k: SER.serialize(v) for k, v in r.items()} for r in rows]}
        assert kw.get("IndexName") == "TypeDateIndex"
        self.index_reads += 1
        rows = sorted(
            (
                item
                for item in self.table.items.values()
                if item.get("ItemType") == "advance_consent"
            ),
            key=lambda i: i["SK"],
            reverse=True,
        )
        start = kw.get("ExclusiveStartKey")
        if start:
            rows = [r for r in rows if r["SK"] < start["SK"]["S"]]
        page, rest = rows[: kw["Limit"]], rows[kw["Limit"] :]
        prefix = (ExpressionAttributeValues.get(":p") or {}).get("S")
        shown = [r for r in page if not prefix or r["PK"].startswith(prefix)]
        out: Dict[str, Any] = {
            "Items": [
                {
                    "PK": SER.serialize(r["PK"]),
                    "SK": SER.serialize(r["SK"]),
                    "ItemType": SER.serialize("advance_consent"),
                }
                for r in shown
            ]
        }
        if rest:
            out["LastEvaluatedKey"] = {
                "ItemType": {"S": "advance_consent"},
                "SK": {"S": page[-1]["SK"]},
                "PK": {"S": page[-1]["PK"]},
            }
        return out

    def batch_get_item(self, RequestItems):  # noqa: N803
        [(table, request)] = RequestItems.items()
        found = []
        for key in request["Keys"]:
            item = self.table.items.get((key["PK"]["S"], key["SK"]["S"]))
            if item:
                found.append({k: SER.serialize(v) for k, v in item.items()})
        return {"Responses": {table: found}}

    def get_item(self, TableName, Key, **_kw):  # noqa: N803
        return {}

    def update_item(self, TableName, Key, ExpressionAttributeValues, **_kw):  # noqa: N803
        current = self.table.items.get((Key["PK"]["S"], Key["SK"]["S"]))
        values = self._plain(ExpressionAttributeValues)
        if not current or current.get("WithdrawnAt") or not current["ValidUntil"] > values[":w"]:
            raise self.exceptions.ConditionalCheckFailedException()
        current.update(
            WithdrawnAt=values[":w"], WithdrawnReason=values[":r"], ExpiresAfter=values[":e"]
        )
        if ":b" in values:
            current["WithdrawnBy"] = values[":b"]
        return {"Attributes": {k: SER.serialize(v) for k, v in current.items()}}

    def delete_item(self, TableName, Key):  # noqa: N803
        self.table.items.pop((Key["PK"]["S"], Key["SK"]["S"]), None)

    def put_item(self, TableName, Item):  # noqa: N803
        self.puts.append(self._plain(Item))


@pytest.fixture
def admin(aws, monkeypatch):
    low = LowClient(aws["dynamo"].events)
    monkeypatch.setattr(index, "DYNAMODB_CLIENT", low)
    monkeypatch.setattr(index, "MEETING_CONSENT_FUNCTION", "meeting-consent")
    invoked: List[Dict[str, Any]] = []

    class Lambda:
        def invoke(self, **kwargs):
            invoked.append(json.loads(kwargs["Payload"]))
            return {}

    monkeypatch.setattr(index, "LAMBDA", Lambda())
    aws["cognito"].add(ADMIN)
    aws.update(low=low, invoked=invoked)
    return aws


def store(aws, email, n, **fields):
    consent_id = f"2026112{n:01d}120000-{n:012x}"
    item = {
        "PK": f"pmc#{email}",
        "SK": f"ac#{consent_id}",
        "ItemType": "advance_consent",
        "ConsentId": consent_id,
        "Email": email,
        "Subject": f"sub-{email}",
        "Scope": "meeting",
        "CallId": "Weekly",
        "Organizer": OWNER,
        "GrantedAt": iso(NOW - timedelta(days=1)),
        "ValidUntil": iso(NOW + timedelta(days=30)),
        "NoticeText": "I agree.",
        "NoticeHash": "a" * 64,
        "NoticeVersion": "abcdefabcdef",
        "Source": "pre_meeting",
    }
    item.update(fields)
    aws["dynamo"].events.items[(item["PK"], item["SK"])] = copy.deepcopy(item)
    return item


def as_admin(field, arguments=None, username=ADMIN):
    event = base.event(field, username, arguments)
    event["identity"]["claims"]["cognito:groups"] = ["Admin"]
    return index.handler(event, None)


# ── listing ─────────────────────────────────────────────────────────────────


def test_an_admin_lists_everyones_consents_newest_first(admin) -> None:
    store(admin, ANA, 1)
    store(admin, BO, 2, AppliedAt={"Weekly": iso(NOW)}, Supersedes=["x"])
    page = as_admin("listAllAdvanceConsents")
    assert [c["Email"] for c in page["Consents"]] == [BO, ANA]
    assert page["Consents"][0]["CountedIn"] == [{"CallId": "Weekly", "At": iso(NOW)}]
    assert page["Consents"][0]["Supersedes"] == ["x"]
    assert page["NextToken"] is None


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ({"Person": "ANA@"}, [ANA]),
        ({"Scope": "deployment"}, [BO]),
        ({"State": "withdrawn"}, ["cy@example.com"]),
        ({"State": "expired"}, ["di@example.com"]),
        ({"ExpiringBefore": (NOW + timedelta(days=10)).strftime("%Y-%m-%d")}, ["di@example.com"]),
    ],
)
def test_the_list_is_filtered_on_the_server(admin, arguments, expected) -> None:
    store(admin, ANA, 1)
    store(admin, BO, 2, Scope="deployment", CallId=None)
    store(admin, "cy@example.com", 3, WithdrawnAt=iso(NOW), WithdrawnReason="withdrawn")
    store(admin, "di@example.com", 4, ValidUntil=iso(NOW - timedelta(days=1)))
    assert [
        c["Email"] for c in as_admin("listAllAdvanceConsents", arguments)["Consents"]
    ] == expected


def test_the_list_is_paged_with_a_token_the_server_checks(admin) -> None:
    for n in range(5):
        store(admin, f"p{n}@example.com", n)
    first = as_admin("listAllAdvanceConsents", {"Limit": 2})
    second = as_admin("listAllAdvanceConsents", {"Limit": 2, "NextToken": first["NextToken"]})
    third = as_admin("listAllAdvanceConsents", {"Limit": 2, "NextToken": second["NextToken"]})
    seen = [c["Email"] for page in (first, second, third) for c in page["Consents"]]
    assert seen == [f"p{n}@example.com" for n in (4, 3, 2, 1, 0)]
    forged = base64.urlsafe_b64encode(json.dumps({"PK": {"S": "c#x"}}).encode()).decode()
    with pytest.raises(index.BadRequest):
        as_admin("listAllAdvanceConsents", {"NextToken": forged})


@pytest.mark.parametrize("groups", [[], ["Participant"]])
@pytest.mark.parametrize(
    "field,arguments",
    [
        ("listAllAdvanceConsents", {}),
        (
            "withdrawAdvanceConsentAsAdmin",
            {"input": {"Email": ANA, "ConsentId": "20261121120000-000000000001"}},
        ),
        ("deleteAdvanceConsentRecords", {"input": {"Email": ANA, "AllForPerson": True}}),
    ],
)
def test_only_an_admin_administers_everyones_consents(admin, groups, field, arguments) -> None:
    store(admin, ANA, 1)
    event = base.event(field, BO, arguments)
    event["identity"]["claims"]["cognito:groups"] = groups
    with pytest.raises(index.Refused):
        index.handler(event, None)
    assert (
        admin["dynamo"]
        .events.items[(f"pmc#{ANA}", "ac#20261121120000-000000000001")]
        .get("WithdrawnAt")
        is None
    )


# ── withdrawing on someone's behalf ─────────────────────────────────────────


def test_an_admin_withdrawal_reaches_the_meetings_that_counted_it_and_names_an_admin(admin) -> None:
    given = store(admin, ANA, 1, AppliedAt={"Weekly": iso(NOW)})
    answer = as_admin(
        "withdrawAdvanceConsentAsAdmin",
        {"input": {"Email": ANA, "ConsentId": given["ConsentId"], "Note": "Asked by phone"}},
    )
    assert answer["Status"] == "withdrawn" and answer["WithdrawnBy"] == ADMIN
    assert [i["advanceConsentWithdrawn"]["CallId"] for i in admin["invoked"]] == ["Weekly"]
    mine = base.call("listMyAdvanceConsents", ANA)["Consents"][0]
    assert (
        mine["WithdrawnReason"] == "withdrawn_by_admin"
        and mine["WithdrawnNote"] == "Asked by phone"
    )
    assert "WithdrawnBy" not in mine, "the person is told an administrator withdrew it, not which"


# ── erasing ─────────────────────────────────────────────────────────────────


def test_an_erasure_withdraws_first_removes_the_items_and_audits_without_content(admin) -> None:
    store(admin, ANA, 1, AppliedAt={"Weekly": iso(NOW)})
    store(admin, ANA, 2, WithdrawnAt=iso(NOW), WithdrawnReason="withdrawn")
    store(admin, BO, 3)
    answer = as_admin(
        "deleteAdvanceConsentRecords", {"input": {"Email": ANA, "AllForPerson": True}}
    )
    assert (answer["Deleted"], answer["Withdrawn"]) == (2, 1)
    assert [i["advanceConsentWithdrawn"]["CallId"] for i in admin["invoked"]] == ["Weekly"]
    left = {k for k in admin["dynamo"].events.items if k[0].startswith("pmc#")}
    assert left == {(f"pmc#{BO}", "ac#20261123120000-000000000003")}
    pending, audit = admin["low"].puts
    assert pending["Status"] == "pending" and audit["Status"] == "complete"
    assert pending["AuditId"] == audit["AuditId"]
    assert audit["PK"].startswith("cnsaudit#") and audit["ErasedBy"] == ADMIN
    assert audit["Email"] == ANA and audit["Count"] == 2 and audit["Withdrawn"] == 1
    assert sorted(audit["ConsentIds"]) == sorted(answer["ConsentIds"])
    assert "I agree." not in json.dumps(audit, default=str) and "NoticeHash" not in audit


def test_an_erasure_that_stops_part_way_is_on_record(admin, monkeypatch) -> None:
    store(admin, ANA, 1)
    store(admin, ANA, 2)
    deleted = []

    def delete_once(TableName, Key):  # noqa: N803
        if deleted:
            raise base.ClientError({"Error": {"Code": "ThrottlingException"}}, "DeleteItem")
        deleted.append(Key)
        admin["dynamo"].events.items.pop((Key["PK"]["S"], Key["SK"]["S"]), None)

    monkeypatch.setattr(admin["low"], "delete_item", delete_once)
    with pytest.raises(base.ClientError):
        as_admin("deleteAdvanceConsentRecords", {"input": {"Email": ANA, "AllForPerson": True}})
    [audit] = admin["low"].puts
    assert audit["Status"] == "pending" and len(audit["ConsentIds"]) == 2


def test_an_erasure_of_named_consents_leaves_the_others(admin) -> None:
    first = store(admin, ANA, 1)
    store(admin, ANA, 2)
    answer = as_admin(
        "deleteAdvanceConsentRecords", {"input": {"Email": ANA, "ConsentIds": [first["ConsentId"]]}}
    )
    assert answer["ConsentIds"] == [first["ConsentId"]]
    assert (f"pmc#{ANA}", "ac#20261122120000-000000000002") in admin["dynamo"].events.items


def test_an_erasure_names_its_consents_one_way_only(admin) -> None:
    with pytest.raises(index.BadRequest):
        as_admin("deleteAdvanceConsentRecords", {"input": {"Email": ANA}})
    with pytest.raises(index.BadRequest):
        as_admin(
            "deleteAdvanceConsentRecords",
            {
                "input": {
                    "Email": ANA,
                    "AllForPerson": True,
                    "ConsentIds": ["20261121120000-000000000001"],
                }
            },
        )


# ── a consent belongs to the account that gave it ───────────────────────────


def test_a_new_account_with_the_address_neither_sees_nor_withdraws_an_earlier_accounts_consents(
    admin,
) -> None:
    earlier = store(admin, ANA, 1, Subject="sub-of-a-deleted-account")
    assert base.call("listMyAdvanceConsents", ANA)["Consents"] == []
    with pytest.raises(index.BadRequest):
        base.call("withdrawAdvanceConsent", ANA, {"input": {"ConsentId": earlier["ConsentId"]}})
    assert admin["dynamo"].events.items[(earlier["PK"], earlier["SK"])].get("WithdrawnAt") is None


def test_a_consent_given_is_kept_in_the_index_admins_list_from(admin) -> None:
    given = base.give(ANA)
    item = admin["dynamo"].events.items[(f"pmc#{ANA}", f"ac#{given['ConsentId']}")]
    assert item["ItemType"] == "advance_consent"
    assert [c["ConsentId"] for c in as_admin("listAllAdvanceConsents")["Consents"]] == [
        given["ConsentId"]
    ]


def test_a_page_filled_by_the_last_consent_has_no_next_token(admin) -> None:
    store(admin, ANA, 1)
    store(admin, BO, 2)
    assert as_admin("listAllAdvanceConsents", {"Limit": 2})["NextToken"] is None


def test_the_last_page_has_no_next_token_when_a_filter_reads_past_a_page(admin) -> None:
    for n in range(3):
        store(admin, f"p{n}@example.com", n)
    store(admin, ANA, 9)
    page = as_admin("listAllAdvanceConsents", {"Person": "ana@", "Limit": 2})
    assert [c["Email"] for c in page["Consents"]] == [ANA] and page["NextToken"] is None


def test_an_erasure_past_the_cap_answers_that_more_remain(admin, monkeypatch) -> None:
    monkeypatch.setattr(index.administration, "MAX_ERASED", 2)
    for n in range(1, 4):
        store(admin, ANA, n)
    first = as_admin("deleteAdvanceConsentRecords", {"input": {"Email": ANA, "AllForPerson": True}})
    assert (first["Deleted"], first["MoreRemain"]) == (2, True)
    assert admin["low"].puts[-1]["Status"] == "partial"
    second = as_admin(
        "deleteAdvanceConsentRecords", {"input": {"Email": ANA, "AllForPerson": True}}
    )
    assert (second["Deleted"], second["MoreRemain"]) == (1, False)
    assert admin["low"].puts[-1]["Status"] == "complete"


def test_names_that_cannot_be_read_ask_the_admin_to_try_again(admin, monkeypatch) -> None:
    store(admin, ANA, 1)

    def changing(*_a, **_k):
        raise index.account_names.NamesChanged("the names could not all be read; try again")

    monkeypatch.setattr(index.account_names, "read_profiles", changing)
    with pytest.raises(index.BadRequest, match="try again"):
        as_admin("listAllAdvanceConsents")


def test_named_consents_that_cannot_be_read_ask_the_admin_to_try_again(admin, monkeypatch) -> None:
    first = store(admin, ANA, 1)

    def unreadable(*_a, **_k):
        raise index.administration.ConsentsUnreadable("some consents could not be read; try again")

    monkeypatch.setattr(index.administration, "read_items", unreadable)
    with pytest.raises(index.BadRequest, match="try again"):
        as_admin(
            "deleteAdvanceConsentRecords",
            {"input": {"Email": ANA, "ConsentIds": [first["ConsentId"]]}},
        )
    assert (first["PK"], first["SK"]) in admin["dynamo"].events.items
