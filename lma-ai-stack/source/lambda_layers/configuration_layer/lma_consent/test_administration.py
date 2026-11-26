#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Tests for erasing consents given in advance in bounded steps (no AWS).

What matters: one erasure removes at most MAX_ERASED consents; when more are
kept under the address its answer and its audit entry say so (``MoreRemain``,
status ``partial``) and erasing again removes the next ones, while an erasure
that removed every one is ``complete``; and named consents are read by their
keys, so a named consent older than the first MAX_ERASED is still found.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from lma_consent import administration

TABLE = "events"
EMAIL = "ana@example.com"
NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
SER, DES = TypeSerializer(), TypeDeserializer()


class PagedClient:
    """One person's consents, read a page of ``page`` items at a time, newest first."""

    class exceptions:  # noqa: N801 - mirrors botocore's client.exceptions
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self, count: int, page: int) -> None:
        self.page = page
        self.items: Dict[str, Dict[str, Any]] = {}
        for n in range(count):
            sk = f"ac#20261101{n:06d}-{n:012x}"
            self.items[sk] = {
                "PK": f"pmc#{EMAIL}",
                "SK": sk,
                "ConsentId": sk[3:],
                "Email": EMAIL,
                "ValidUntil": "2020-01-01T00:00:00Z",  # expired: nothing to withdraw
            }
        self.puts: List[Dict[str, Any]] = []

    def query(self, **kwargs):
        rows = sorted(self.items.values(), key=lambda i: i["SK"], reverse=True)
        start = kwargs.get("ExclusiveStartKey")
        if start:
            rows = [r for r in rows if r["SK"] < start["SK"]["S"]]
        size = min(self.page, kwargs.get("Limit") or self.page)
        page = rows[:size]
        out: Dict[str, Any] = {"Items": [{k: SER.serialize(v) for k, v in r.items()} for r in page]}
        # DynamoDB can answer a key with the page that ends exactly at the last item.
        if page:
            out["LastEvaluatedKey"] = {"PK": {"S": page[-1]["PK"]}, "SK": {"S": page[-1]["SK"]}}
        return out

    def batch_get_item(self, RequestItems):  # noqa: N803
        [(table, request)] = RequestItems.items()
        found = [
            {k: SER.serialize(v) for k, v in self.items[key["SK"]["S"]].items()}
            for key in request["Keys"]
            if key["SK"]["S"] in self.items
        ]
        return {"Responses": {table: found}}

    def put_item(self, TableName, Item):  # noqa: N803
        self.puts.append({k: DES.deserialize(v) for k, v in Item.items()})

    def delete_item(self, TableName, Key):  # noqa: N803
        self.items.pop(Key["SK"]["S"], None)


def erase(client, **kwargs):
    return administration.erase(
        client,
        TABLE,
        email=EMAIL,
        erased_by="admin@example.com",
        now=NOW,
        expires_after=int((NOW + timedelta(days=90)).timestamp()),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def small_cap(monkeypatch):
    monkeypatch.setattr(administration, "MAX_ERASED", 4)


def test_an_erasure_past_the_cap_says_more_remain_and_the_next_one_finishes():
    client = PagedClient(count=6, page=3)
    first = erase(client)
    assert (first["Deleted"], first["MoreRemain"]) == (4, True)
    audit = client.puts[-1]
    assert audit["Status"] == "partial" and audit["Count"] == 4 and audit["MoreRemain"] is True
    second = erase(client)
    assert (second["Deleted"], second["MoreRemain"]) == (2, False)
    assert client.puts[-1]["Status"] == "complete" and "MoreRemain" not in client.puts[-1]
    assert client.items == {}


def test_exactly_the_cap_with_an_empty_last_page_is_complete():
    client = PagedClient(count=4, page=2)
    answer = erase(client)
    assert (answer["Deleted"], answer["MoreRemain"]) == (4, False)
    assert client.puts[-1]["Status"] == "complete"


def test_one_past_the_cap_on_a_page_boundary_is_found():
    client = PagedClient(count=5, page=2)
    answer = erase(client)
    assert (answer["Deleted"], answer["MoreRemain"]) == (4, True)


def test_a_named_consent_older_than_the_cap_is_still_erased():
    client = PagedClient(count=6, page=3)
    oldest = min(client.items)[3:]
    answer = erase(client, consent_ids=[oldest, oldest])
    assert answer["ConsentIds"] == [oldest] and answer["MoreRemain"] is False
    assert len(client.items) == 5


def test_an_unreadable_named_consent_stops_the_erasure_before_anything_is_removed(monkeypatch):
    client = PagedClient(count=2, page=3)
    monkeypatch.setattr(
        client,
        "batch_get_item",
        lambda RequestItems: {"Responses": {}, "UnprocessedKeys": RequestItems},  # noqa: N803
    )
    monkeypatch.setattr(administration.time, "sleep", lambda _s: None)
    with pytest.raises(administration.ConsentsUnreadable):
        erase(client, consent_ids=[min(client.items)[3:]])
    assert len(client.items) == 2 and client.puts == []
