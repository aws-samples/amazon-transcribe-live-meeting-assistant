# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Sharing a meeting with the people on its participant list (auto_share.py and the stream handler).

No AWS: the AppSync client and the GraphQL DSL are replaced before import (as
in test_delete_metering_records.py), and the table, the sharing and the setting
are fakes.
"""

from __future__ import annotations

import pytest
from auto_share import list_row_key, listed_addresses, plan_auto_share, shared_with_after
from boto3.dynamodb.types import TypeSerializer
from test_delete_metering_records import index

OWNER = "owner@example.com"
ANA = "ana@example.com"
BEN = "ben@example.com"
CARL = "carl@example.com"


def _people(*emails):
    return [{"name": e.split("@")[0].title(), "email": e} for e in emails]


# ── the plan ────────────────────────────────────────────────────────────────


def test_a_new_meeting_is_shared_with_everyone_listed_but_the_owner() -> None:
    plan = plan_auto_share(OWNER, [], _people(ANA, OWNER.upper(), BEN), None)
    assert plan.add == [ANA, BEN]
    assert plan.remove == []
    assert plan.auto_shared == [ANA, BEN]


def test_nothing_changes_once_the_list_is_shared() -> None:
    plan = plan_auto_share(OWNER, [ANA, BEN], _people(ANA, BEN), [ANA, BEN])
    assert not plan.changes_anything


def test_someone_added_to_the_list_is_shared_with() -> None:
    plan = plan_auto_share(OWNER, [ANA], _people(ANA, BEN), [ANA])
    assert plan.add == [BEN]
    assert plan.auto_shared == [ANA, BEN]


def test_someone_taken_off_the_list_loses_the_share_auto_share_made() -> None:
    plan = plan_auto_share(OWNER, [ANA, BEN], _people(ANA), [ANA, BEN])
    assert plan.add == []
    assert plan.remove == [BEN]
    assert plan.auto_shared == [ANA]


def test_a_share_the_owner_made_by_hand_is_never_removed() -> None:
    # Carl was shared with by hand and was never auto-shared: off the list he stays.
    plan = plan_auto_share(OWNER, [ANA, CARL], _people(ANA), [ANA])
    assert plan.remove == []
    assert not plan.changes_anything


def test_a_listed_person_already_shared_with_by_hand_stays_a_manual_share() -> None:
    plan = plan_auto_share(OWNER, [CARL], _people(CARL), None)
    assert plan.add == []
    assert plan.auto_shared == []
    # Taken off the list later, the share stays.
    later = plan_auto_share(OWNER, [CARL], [], plan.auto_shared)
    assert later.remove == []


def test_someone_the_owner_unshared_by_hand_is_not_shared_with_again() -> None:
    # Ana was auto-shared, then removed in the share dialog, and is still listed.
    plan = plan_auto_share(OWNER, [BEN], _people(ANA, BEN), [ANA, BEN])
    assert plan.add == []
    assert plan.remove == []
    assert not plan.changes_anything


def test_taken_off_and_put_back_on_the_list_is_shared_with_again() -> None:
    off = plan_auto_share(OWNER, [BEN], _people(BEN), [ANA, BEN])
    assert off.remove == [] and off.auto_shared == [BEN]
    back = plan_auto_share(OWNER, [BEN], _people(ANA, BEN), off.auto_shared)
    assert back.add == [ANA]


def test_addresses_compare_ignoring_case_and_are_shared_as_listed() -> None:
    plan = plan_auto_share(OWNER, [ANA.upper()], _people(ANA, "Ben@Example.com"), None)
    assert plan.add == ["Ben@Example.com"]
    removed = plan_auto_share(OWNER, [ANA.upper()], [], [ANA])
    assert removed.remove == [ANA.upper()]


def test_a_cleared_list_removes_every_auto_share() -> None:
    plan = plan_auto_share(OWNER, [ANA, BEN, CARL], [], [ANA, BEN])
    assert sorted(plan.remove) == [ANA, BEN]
    assert plan.auto_shared == []
    assert shared_with_after([ANA, BEN, CARL], plan) == [CARL]


def test_an_address_holding_a_separator_is_never_shared_with() -> None:
    assert listed_addresses([{"email": "a,b@example.com"}, {"email": "[x@example.com"}]) == []
    assert listed_addresses("not a list") == []
    assert listed_addresses([{"email": ANA}, {"email": ANA.upper()}, {"name": "no email"}]) == [ANA]


def test_the_sharing_list_keeps_its_entries_and_adds_the_new_ones() -> None:
    plan = plan_auto_share(OWNER, [CARL], _people(ANA), None)
    assert shared_with_after([CARL], plan) == [CARL, ANA]


def test_the_list_row_key_is_the_one_create_call_writes() -> None:
    assert list_row_key("m1", "2026-11-24T17:05:00.000Z") == {
        "PK": "cls#2026-11-24#s#04",
        "SK": "ts#2026-11-24T17:05:00.000Z#id#m1",
    }
    assert list_row_key("m1", "2026-11-24T03:00:00Z")["PK"].endswith("#s#00")
    assert list_row_key("m1", None) is None


# ── the stream handler ──────────────────────────────────────────────────────


def _record(item, sequence="1", event_name="MODIFY"):
    serializer = TypeSerializer()
    image = {name: serializer.serialize(value) for name, value in item.items()}
    return {
        "eventName": event_name,
        "dynamodb": {
            "Keys": {"PK": image["PK"], "SK": image["SK"]},
            "NewImage": image,
            "SequenceNumber": sequence,
        },
    }


def _meeting(call_id="m1", **attributes):
    pk = f"c#{call_id}"
    return {
        "PK": pk,
        "SK": pk,
        "CallId": call_id,
        "Owner": OWNER,
        "CreatedAt": "2026-11-24T17:05:00.000Z",
        **attributes,
    }


class StreamTable:
    def __init__(self, items):
        self.items = {item["PK"]: dict(item) for item in items}
        self.reads = 0
        self.updates = []

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        assert ConsistentRead
        self.reads += 1
        item = self.items.get(Key["PK"])
        return {"Item": dict(item)} if item else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


@pytest.fixture
def stream(monkeypatch):
    shared = []

    def fake_share_meeting(_session, _schema, callid, list_pk, list_sk, recipients, owner):
        shared.append((callid, list_pk, list_sk, recipients, owner))

    class Session:
        client = type("Client", (), {"schema": object()})()

    class Client:
        def __enter__(self):
            return Session()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(index, "share_meeting", fake_share_meeting)
    monkeypatch.setattr(index, "appsync_client", Client())
    monkeypatch.setattr(index, "DSLSchema", lambda schema: schema)
    monkeypatch.setattr(index, "_auto_share_enabled", lambda: True)
    return shared


def test_the_handler_shares_a_meeting_from_its_record_and_records_what_it_added(
    stream, monkeypatch
) -> None:
    item = _meeting(Participants=_people(ANA, BEN), SharedWith=[CARL])
    table = StreamTable([item])
    monkeypatch.setattr(index, "ddbTable", table)
    result = index.lambda_handler({"Records": [_record(item)]}, None)
    assert result == {"batchItemFailures": []}
    assert stream == [
        (
            "m1",
            "cls#2026-11-24#s#04",
            "ts#2026-11-24T17:05:00.000Z#id#m1",
            f"{CARL},{ANA},{BEN}",
            OWNER,
        )
    ]
    (update,) = table.updates
    assert update["ExpressionAttributeValues"] == {":auto": [ANA, BEN]}
    assert update["ExpressionAttributeNames"] == {"#auto": "AutoSharedWith"}


def test_a_record_with_nothing_to_change_is_not_read_again(stream, monkeypatch) -> None:
    item = _meeting(Participants=_people(ANA), SharedWith=[ANA], AutoSharedWith=[ANA])
    table = StreamTable([item])
    monkeypatch.setattr(index, "ddbTable", table)
    enabled_reads = []
    monkeypatch.setattr(index, "_auto_share_enabled", lambda: enabled_reads.append(1) or True)
    index.lambda_handler({"Records": [_record(item)]}, None)
    assert table.reads == 0 and stream == [] and enabled_reads == []


def test_the_current_record_decides_not_the_one_in_the_stream(stream, monkeypatch) -> None:
    # The stream still shows Ana unshared; the table shows auto-share already did it.
    behind = _meeting(Participants=_people(ANA))
    now = _meeting(Participants=_people(ANA), SharedWith=[ANA], AutoSharedWith=[ANA])
    table = StreamTable([now])
    monkeypatch.setattr(index, "ddbTable", table)
    index.lambda_handler({"Records": [_record(behind)]}, None)
    assert table.reads == 1 and stream == [] and table.updates == []


def test_each_meeting_is_handled_once_per_batch_and_other_items_are_skipped(
    stream, monkeypatch
) -> None:
    first = _meeting(Participants=_people(ANA))
    second = _meeting(Participants=_people(ANA, BEN))
    segment = {"PK": "c#m1", "SK": "ts#2026", "Participants": _people(CARL)}
    table = StreamTable([second])
    monkeypatch.setattr(index, "ddbTable", table)
    records = [_record(first, "1"), _record(second, "2"), _record(segment, "3")]
    records.append(_record(_meeting(Participants=_people(CARL)), "4", event_name="REMOVE"))
    index.lambda_handler({"Records": records}, None)
    assert [entry[3] for entry in stream] == [f"{ANA},{BEN}"]


def test_auto_share_turned_off_changes_nothing(stream, monkeypatch) -> None:
    item = _meeting(Participants=_people(ANA))
    table = StreamTable([item])
    monkeypatch.setattr(index, "ddbTable", table)
    monkeypatch.setattr(index, "_auto_share_enabled", lambda: False)
    index.lambda_handler({"Records": [_record(item)]}, None)
    assert stream == [] and table.updates == [] and table.reads == 0


def test_a_meeting_whose_share_fails_is_reported_for_retry_and_the_rest_go_on(
    stream, monkeypatch
) -> None:
    bad = _meeting("bad", Participants=_people(ANA))
    good = _meeting("good", Participants=_people(BEN))
    table = StreamTable([bad, good])
    monkeypatch.setattr(index, "ddbTable", table)

    def share(_session, _schema, callid, *_rest):
        if callid == "bad":
            raise RuntimeError("AppSync unavailable")
        stream.append(callid)

    monkeypatch.setattr(index, "share_meeting", share)
    result = index.lambda_handler({"Records": [_record(bad, "7"), _record(good, "8")]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "7"}]}
    assert stream == ["good"]
    # AutoSharedWith is written only after a share succeeds.
    assert [u["Key"]["PK"] for u in table.updates] == ["c#good"]


def test_dropping_the_last_auto_share_removes_the_attribute(stream, monkeypatch) -> None:
    item = _meeting(Participants=[], SharedWith=[ANA, CARL], AutoSharedWith=[ANA])
    table = StreamTable([item])
    monkeypatch.setattr(index, "ddbTable", table)
    index.lambda_handler({"Records": [_record(item)]}, None)
    assert stream[0][3] == CARL
    assert table.updates[0]["UpdateExpression"] == "REMOVE #auto"


def test_a_meeting_deleted_before_its_shares_are_recorded_is_not_retried(
    stream, monkeypatch
) -> None:
    from botocore.exceptions import ClientError

    item = _meeting(Participants=_people(ANA))

    class DeletedMeanwhile(StreamTable):
        def update_item(self, **kwargs):
            assert kwargs["ConditionExpression"] == "attribute_exists(PK)"
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

    monkeypatch.setattr(index, "ddbTable", DeletedMeanwhile([item]))
    assert index.lambda_handler({"Records": [_record(item)]}, None) == {"batchItemFailures": []}
