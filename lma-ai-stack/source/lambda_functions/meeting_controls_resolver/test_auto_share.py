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


def test_an_address_the_share_dialog_would_refuse_is_never_shared_with() -> None:
    assert listed_addresses([{"email": "a,b@example.com"}, {"email": "[x@example.com"}]) == []
    assert listed_addresses([{"email": "not-an-address"}, {"email": "a b@example.com"}]) == []
    assert listed_addresses([{"email": "x@y@example.com"}, {"email": "x@example"}]) == []
    # Every address the list keeps is one share_meetings accepts.
    assert all(
        index.RECIPIENT_PATTERN.fullmatch(a) for a in listed_addresses(_people(ANA, BEN, CARL))
    )
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
    """The meetings table: consistent reads, and the transaction the handler writes."""

    def __init__(self, items):
        self.items = {item["PK"]: dict(item) for item in items}
        self.reads = 0

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        assert ConsistentRead
        self.reads += 1
        item = self.items.get(Key["PK"])
        return {"Item": dict(item)} if item else {}


def _cancelled():
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}],
        },
        "TransactWriteItems",
    )


@pytest.fixture
def stream(monkeypatch):
    """The handler's writes: the transactions, the items given each list, and who is told."""
    seen = {"transactions": [], "items": [], "unshared": [], "fail": set(), "cancel": set()}
    table_ref = {}

    def fake_transact(writes):
        meeting = writes[0]["Update"]
        call_id = meeting["Key"]["PK"][2:]
        if call_id in seen["fail"]:
            raise RuntimeError("DynamoDB unavailable")
        if call_id in seen["cancel"]:
            raise _cancelled()
        seen["transactions"].append(writes)
        # Apply it to the fake table, so the read after the items sees it.
        table = table_ref.get("table")
        if table is not None:
            row = table.items[meeting["Key"]["PK"]]
            values = meeting.get("ExpressionAttributeValues", {})
            if ":sw" in values:
                row["SharedWith"] = values[":sw"]
            elif "#sw" in meeting["UpdateExpression"].split("REMOVE")[-1]:
                row.pop("SharedWith", None)

    def fake_share_items(_session, _schema, call_id, recipients):
        seen["items"].append((call_id, list(recipients)))

    class Session:
        client = type("Client", (), {"schema": object()})()

    class Client:
        def __enter__(self):
            return Session()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(index, "_transact", fake_transact)
    monkeypatch.setattr(index, "_share_items", fake_share_items)
    monkeypatch.setattr(
        index,
        "_tell_unshared",
        lambda _s, _sc, call_id, removed: seen["unshared"].append((call_id, removed)),
    )
    monkeypatch.setattr(index, "appsync_client", Client())
    monkeypatch.setattr(index, "DSLSchema", lambda schema: schema)
    monkeypatch.setattr(index, "_auto_share_enabled", lambda: True)
    monkeypatch.setattr(index, "account_username", lambda address: address)

    def use(table):
        table_ref["table"] = table
        monkeypatch.setattr(index, "ddbTable", table)
        return table

    seen["use"] = use
    return seen


def test_the_handler_shares_a_meeting_from_its_record_in_one_conditional_write(stream) -> None:
    item = _meeting(Participants=_people(ANA, BEN), SharedWith=[CARL])
    stream["use"](StreamTable([item]))
    result = index.lambda_handler({"Records": [_record(item)]}, None)
    assert result == {"batchItemFailures": []}
    (writes,) = stream["transactions"]
    meeting, list_row = (w["Update"] for w in writes)
    assert meeting["Key"] == {"PK": "c#m1", "SK": "c#m1"}
    assert meeting["ExpressionAttributeValues"][":sw"] == [CARL, ANA, BEN]
    assert meeting["ExpressionAttributeValues"][":auto"] == [ANA, BEN]
    # Only if the meeting's SharedWith is still the one the plan was made from.
    assert meeting["ConditionExpression"] == "attribute_exists(PK) AND #sw = :seen"
    assert meeting["ExpressionAttributeValues"][":seen"] == [CARL]
    assert list_row["Key"] == {
        "PK": "cls#2026-11-24#s#04",
        "SK": "ts#2026-11-24T17:05:00.000Z#id#m1",
    }
    assert list_row["ExpressionAttributeValues"] == {":sw": [CARL, ANA, BEN]}
    # The transcript, key frames and chat attachments are given the list, once.
    assert stream["items"] == [("m1", [CARL, ANA, BEN])]
    assert stream["unshared"] == []


def test_a_meeting_shared_with_nobody_is_written_on_condition_that_it_still_is(stream) -> None:
    item = _meeting(Participants=_people(ANA))
    stream["use"](StreamTable([item]))
    index.lambda_handler({"Records": [_record(item)]}, None)
    meeting = stream["transactions"][0][0]["Update"]
    assert meeting["ConditionExpression"] == "attribute_exists(PK) AND attribute_not_exists(#sw)"


def test_a_share_made_meanwhile_is_not_overwritten_and_the_record_is_retried(stream) -> None:
    item = _meeting(Participants=_people(ANA))
    stream["use"](StreamTable([item]))
    stream["cancel"].add("m1")
    result = index.lambda_handler({"Records": [_record(item, "5")]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "5"}]}
    assert stream["items"] == []


def test_a_meeting_deleted_before_its_shares_are_recorded_is_not_retried(stream) -> None:
    item = _meeting(Participants=_people(ANA))
    table = stream["use"](StreamTable([item]))
    stream["cancel"].add("m1")
    original = table.get_item
    reads = []

    def gone_on_second_read(Key, ConsistentRead=False):  # noqa: N803
        reads.append(Key)
        return original(Key, ConsistentRead) if len(reads) == 1 else {}

    table.get_item = gone_on_second_read
    assert index.lambda_handler({"Records": [_record(item)]}, None) == {"batchItemFailures": []}


def test_the_items_follow_a_share_that_lands_while_they_are_written(stream) -> None:
    item = _meeting(Participants=_people(ANA))
    table = stream["use"](StreamTable([item]))
    original = index._share_items

    def share_then_owner_shares(session, schema, call_id, recipients):
        original(session, schema, call_id, recipients)
        if len(stream["items"]) == 1:
            table.items["c#m1"]["SharedWith"] = [ANA, CARL]

    index._share_items = share_then_owner_shares
    try:
        index.lambda_handler({"Records": [_record(item)]}, None)
    finally:
        index._share_items = original
    assert stream["items"] == [("m1", [ANA]), ("m1", [ANA, CARL])]


def test_a_record_with_nothing_to_change_is_not_read_again(stream, monkeypatch) -> None:
    item = _meeting(Participants=_people(ANA), SharedWith=[ANA], AutoSharedWith=[ANA])
    table = stream["use"](StreamTable([item]))
    enabled_reads = []
    monkeypatch.setattr(index, "_auto_share_enabled", lambda: enabled_reads.append(1) or True)
    index.lambda_handler({"Records": [_record(item)]}, None)
    assert table.reads == 0 and stream["transactions"] == [] and enabled_reads == []


def test_the_current_record_decides_not_the_one_in_the_stream(stream) -> None:
    # The stream still shows Ana unshared; the table shows auto-share already did it.
    behind = _meeting(Participants=_people(ANA))
    now = _meeting(Participants=_people(ANA), SharedWith=[ANA], AutoSharedWith=[ANA])
    table = stream["use"](StreamTable([now]))
    index.lambda_handler({"Records": [_record(behind)]}, None)
    assert table.reads == 1 and stream["transactions"] == []


def test_each_meeting_is_handled_once_per_batch_and_other_items_are_skipped(stream) -> None:
    first = _meeting(Participants=_people(ANA))
    second = _meeting(Participants=_people(ANA, BEN))
    segment = {"PK": "c#m1", "SK": "ts#2026", "Participants": _people(CARL)}
    stream["use"](StreamTable([second]))
    records = [_record(first, "1"), _record(second, "2"), _record(segment, "3")]
    records.append(_record(_meeting(Participants=_people(CARL)), "4", event_name="REMOVE"))
    index.lambda_handler({"Records": records}, None)
    assert stream["items"] == [("m1", [ANA, BEN])]


def test_auto_share_turned_off_changes_nothing(stream, monkeypatch) -> None:
    item = _meeting(Participants=_people(ANA))
    table = stream["use"](StreamTable([item]))
    monkeypatch.setattr(index, "_auto_share_enabled", lambda: False)
    index.lambda_handler({"Records": [_record(item)]}, None)
    assert stream["transactions"] == [] and table.reads == 0


def test_a_meeting_whose_share_fails_is_reported_for_retry_and_the_rest_go_on(stream) -> None:
    bad = _meeting("bad", Participants=_people(ANA))
    good = _meeting("good", Participants=_people(BEN))
    stream["use"](StreamTable([bad, good]))
    stream["fail"].add("bad")
    result = index.lambda_handler({"Records": [_record(bad, "7"), _record(good, "8")]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "7"}]}
    assert stream["items"] == [("good", [BEN])]


def test_taking_someone_off_the_list_unshares_them_and_tells_them(stream) -> None:
    item = _meeting(Participants=[], SharedWith=[ANA, CARL], AutoSharedWith=[ANA])
    stream["use"](StreamTable([item]))
    index.lambda_handler({"Records": [_record(item)]}, None)
    meeting = stream["transactions"][0][0]["Update"]
    assert meeting["ExpressionAttributeValues"][":sw"] == [CARL]
    assert "REMOVE #auto" in meeting["UpdateExpression"]
    assert stream["unshared"] == [("m1", [ANA])]


def test_only_the_record_of_auto_shares_is_written_when_sharing_does_not_change(stream) -> None:
    # Ana was auto-shared and is off the list, and the owner had already unshared her.
    item = _meeting(Participants=[], SharedWith=[CARL], AutoSharedWith=[ANA])
    stream["use"](StreamTable([item]))
    index.lambda_handler({"Records": [_record(item)]}, None)
    (writes,) = stream["transactions"]
    assert len(writes) == 1
    assert writes[0]["Update"]["UpdateExpression"] == "REMOVE #auto"
    assert stream["items"] == [] and stream["unshared"] == []


def test_the_transaction_is_sent_in_the_low_level_form(monkeypatch) -> None:
    sent = []

    class Client:
        def transact_write_items(self, TransactItems):  # noqa: N803
            sent.append(TransactItems)

    class Meta:
        client = Client()

    monkeypatch.setattr(index.ddb, "meta", Meta(), raising=False)
    plan = plan_auto_share(OWNER, [], _people(ANA), None)
    item = _meeting(Participants=_people(ANA))
    writes = index._sharing_write("m1", item, plan, [ANA], {"PK": "cls#x", "SK": "ts#y"})
    index._transact(writes)
    meeting = sent[0][0]["Update"]
    assert meeting["Key"] == {"PK": {"S": "c#m1"}, "SK": {"S": "c#m1"}}
    assert meeting["ExpressionAttributeValues"][":sw"] == {"L": [{"S": ANA}]}


# ── the account a listed address belongs to ─────────────────────────────────


def test_a_listed_address_is_shared_under_its_accounts_own_username(stream, monkeypatch) -> None:
    item = _meeting(Participants=[{"name": "Jane", "email": "Jane.Doe@Example.com"}])
    stream["use"](StreamTable([item]))
    monkeypatch.setattr(
        index, "account_username", lambda address: address.lower() if "Jane" in address else address
    )
    index.lambda_handler({"Records": [_record(item)]}, None)
    meeting = stream["transactions"][0][0]["Update"]
    assert meeting["ExpressionAttributeValues"][":sw"] == ["jane.doe@example.com"]
    assert meeting["ExpressionAttributeValues"][":auto"] == ["jane.doe@example.com"]
    # The next record of the same meeting finds nothing to do.
    again = _meeting(
        Participants=[{"name": "Jane", "email": "Jane.Doe@Example.com"}],
        SharedWith=["jane.doe@example.com"],
        AutoSharedWith=["jane.doe@example.com"],
    )
    assert not index._auto_share_plan(again).changes_anything


def test_the_account_lookup_answers_the_pools_username_or_the_address(monkeypatch) -> None:
    class NotFound(Exception):
        pass

    class Cognito:
        exceptions = type("E", (), {"UserNotFoundException": NotFound})

        def admin_get_user(self, UserPoolId, Username):  # noqa: N803
            assert UserPoolId == "pool-1"
            if Username.lower() == "jane.doe@example.com":
                return {"Username": "jane.doe@example.com"}
            raise NotFound()

    monkeypatch.setattr(index, "USER_POOL_ID", "pool-1")
    monkeypatch.setattr(index, "_cognito", Cognito())
    assert index.account_username("Jane.Doe@Example.com") == "jane.doe@example.com"
    assert index.account_username("Nobody@Example.com") == "Nobody@Example.com"


# ── the setting is read, not assumed ────────────────────────────────────────


def _with_section(monkeypatch, values=None, error=None):
    import sys
    import types
    from pathlib import Path

    layer = Path(__file__).resolve().parents[2] / "lambda_layers" / "configuration_layer"
    if str(layer) not in sys.path:
        sys.path.insert(0, str(layer))
    import lma_config  # noqa: F401 -- the configuration layer

    class Store:
        def get_section(self, name):
            assert name == "meeting_sharing"
            if error:
                raise error
            return types.SimpleNamespace(values=values)

    module = sys.modules["lma_config"]
    monkeypatch.setattr(module, "ConfigurationStore", Store)
    monkeypatch.setattr(index, "_CONFIGURATION_STORE", None)


def test_the_setting_saved_off_turns_sharing_off_and_unset_is_on(monkeypatch) -> None:
    _with_section(monkeypatch, {"shareWithListedParticipants": False})
    assert index._auto_share_enabled() is False
    _with_section(monkeypatch, {})
    assert index._auto_share_enabled() is True


def test_a_setting_that_cannot_be_read_fails_the_record_rather_than_sharing(monkeypatch) -> None:
    from botocore.exceptions import ClientError

    _with_section(monkeypatch, error=ClientError({"Error": {"Code": "Throttling"}}, "GetItem"))
    with pytest.raises(ClientError):
        index._auto_share_enabled()
    item = _meeting(Participants=_people(ANA))
    monkeypatch.setattr(index, "ddbTable", StreamTable([item]))
    result = index.lambda_handler({"Records": [_record(item, "9")]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "9"}]}
