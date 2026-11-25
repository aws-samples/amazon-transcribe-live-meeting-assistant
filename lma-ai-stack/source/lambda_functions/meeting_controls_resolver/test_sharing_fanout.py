# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""A share's items are given its sharing list until every one has it (complete_sharing).

The share dialog's save (share_meetings) and Meeting Sharing (the stream
handler) both record a share with the SharingFanout marker, then give the
meeting's transcript items, key frames and chat attachments the list. These
tests cover a fan-out that fails or runs out of time after the record is
written, one that a newer share overtakes, the lease that keeps two from
working on a meeting at once, and the share dialog's save on condition of the
list it read.

No AWS: the same setup as test_delete_metering_records.py, with a fake table
that applies the updates the function sends, and a clock the test moves.
"""

from __future__ import annotations

import copy
import threading

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from test_delete_metering_records import index

OWNER = "owner@example.com"
ANA = "ana@example.com"
BEN = "ben@example.com"
CARL = "carl@example.com"
CREATED = "2026-11-24T17:05:00.000Z"
LIST_KEY = {"PK": "cls#2026-11-24#s#04", "SK": f"ts#{CREATED}#id#m1"}


def _failed(code="ConditionalCheckFailedException"):
    return ClientError({"Error": {"Code": code}}, "UpdateItem")


def _cancelled():
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException"},
            "CancellationReasons": [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}],
        },
        "TransactWriteItems",
    )


class Clock:
    """time.monotonic, time.time and time.sleep, moved by the test and by each item written."""

    def __init__(self):
        self.now = 1000.0
        self.per_item = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def time(self):
        return 1_800_000_000 + self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class MeetingsTable:
    """The meetings table: items by key, and the updates complete_sharing and record_shares send.

    Each update is applied as DynamoDB would apply it, condition included, for
    the expressions the function writes; any other expression fails the test.
    """

    def __init__(self, items=(), page=50, clock=None):
        self.items = {}
        for item in items:
            self.put(item)
        self.page = page
        self.clock = clock
        self.reads = 0
        self.gives = []  # (PK, SK, list or None), in order
        self.transactions = []
        self.lock = threading.Lock()
        self.on_give = None  # called after each item is given a list
        self.on_chunk_check = None  # called on each read of the marker's Token

    def put(self, item):
        self.items[(item["PK"], item["SK"])] = copy.deepcopy(item)

    def meeting(self, call_id="m1"):
        return self.items.get((f"c#{call_id}", f"c#{call_id}"))

    def get_item(self, Key, ConsistentRead=False, **kwargs):  # noqa: N803
        assert ConsistentRead
        if kwargs.get("ProjectionExpression") == "#fan.#tok" and self.on_chunk_check:
            self.on_chunk_check(self)
        self.reads += 1
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": copy.deepcopy(item)} if item else {}

    def query(self, **kwargs):
        pk = kwargs["ExpressionAttributeValues"][":pk"]
        after = (kwargs.get("ExclusiveStartKey") or {}).get("SK")
        rows = sorted(
            (item for (p, _), item in self.items.items() if p == pk), key=lambda i: i["SK"]
        )
        if after is not None:
            rows = [r for r in rows if r["SK"] > after]
        page = rows[: self.page]
        out = {"Items": [{"PK": r["PK"], "SK": r["SK"]} for r in page]}
        if len(rows) > self.page:
            out["LastEvaluatedKey"] = {"PK": pk, "SK": page[-1]["SK"]}
        return out

    def update_item(self, Key, UpdateExpression, ConditionExpression=None, **kwargs):  # noqa: N803
        values = kwargs.get("ExpressionAttributeValues") or {}
        row = self.items.get((Key["PK"], Key["SK"]))
        expression = UpdateExpression
        if expression in ("SET SharedWith = :val", "REMOVE SharedWith"):
            assert ConditionExpression == "attribute_exists(PK)"
            if row is None:
                raise _failed()
            with self.lock:
                if expression.startswith("SET"):
                    row["SharedWith"] = list(values[":val"])
                else:
                    row.pop("SharedWith", None)
                self.gives.append((Key["PK"], Key["SK"], row.get("SharedWith")))
                if self.clock is not None:
                    self.clock.now += self.clock.per_item
            if self.on_give:
                self.on_give(self, Key)
            return {}
        lease = (row or {}).get(index.LEASE_ATTRIBUTE)
        marker = (row or {}).get(index.FANOUT_ATTRIBUTE)
        if expression == "SET #lease = :lease":
            ok = (
                row is not None
                and marker is not None
                and (
                    lease is None
                    or lease["By"] == values[":me"]
                    or lease["Until"] <= values[":now"]
                )
            )
            if not ok:
                raise _failed()
            row[index.LEASE_ATTRIBUTE] = dict(values[":lease"])
        elif expression == "REMOVE #lease":
            if not (lease and lease["By"] == values[":me"]):
                raise _failed()
            row.pop(index.LEASE_ATTRIBUTE)
        elif expression == "SET #fan.#cur = :cur REMOVE #lease":
            ok = (
                marker
                and marker.get("Token") == values[":tok"]
                and lease
                and lease["By"] == values[":me"]
            )
            if not ok:
                raise _failed()
            marker["Cursor"] = dict(values[":cur"])
            row.pop(index.LEASE_ATTRIBUTE)
        elif expression == "REMOVE #fan, #lease":
            if not (marker and marker.get("Token") == values[":tok"]):
                raise _failed()
            row.pop(index.FANOUT_ATTRIBUTE)
            row.pop(index.LEASE_ATTRIBUTE, None)
        else:
            raise AssertionError(f"unexpected update {expression}")
        return {}

    def transact(self, writes):
        """index._transact: the sharing transaction, all or nothing.

        A meeting record's update (PK c#) is checked against its condition; a
        list row's is applied as given. Nothing is applied unless every
        condition holds.
        """
        meetings = [w["Update"] for w in writes if w["Update"]["Key"]["PK"].startswith("c#")]
        rows = [w["Update"] for w in writes if not w["Update"]["Key"]["PK"].startswith("c#")]
        for meeting in meetings:
            row = self.items.get((meeting["Key"]["PK"], meeting["Key"]["SK"]))
            values = meeting.get("ExpressionAttributeValues", {})
            condition = meeting["ConditionExpression"]
            ok = row is not None
            if ok and "#sw = :seen" in condition:
                ok = row.get("SharedWith") == values[":seen"]
            elif ok:
                ok = "SharedWith" not in row
            if ok and "#fan.#tok = :seentok" in condition:
                ok = (row.get(index.FANOUT_ATTRIBUTE) or {}).get("Token") == values[":seentok"]
            elif ok and "attribute_not_exists(#fan)" in condition:
                ok = index.FANOUT_ATTRIBUTE not in row
            if not ok:
                raise _cancelled()
        for meeting in meetings:
            row = self.items[(meeting["Key"]["PK"], meeting["Key"]["SK"])]
            values = meeting.get("ExpressionAttributeValues", {})
            names = meeting["ExpressionAttributeNames"]
            sets, _, removes = meeting["UpdateExpression"].partition("REMOVE")
            for assignment in sets.replace("SET", "").split(","):
                if "=" in assignment:
                    name, value = (part.strip() for part in assignment.split("="))
                    row[names[name]] = copy.deepcopy(values[value])
            for name in removes.split(","):
                if name.strip():
                    row.pop(names[name.strip()], None)
        for update in rows:
            key = (update["Key"]["PK"], update["Key"]["SK"])
            target = self.items.setdefault(key, dict(update["Key"]))
            if update["UpdateExpression"].startswith("SET"):
                target["SharedWith"] = list(update["ExpressionAttributeValues"][":sw"])
            else:
                target.pop("SharedWith", None)
        self.transactions.append(writes)


def meeting(call_id="m1", **attributes):
    pk = f"c#{call_id}"
    return {
        "PK": pk,
        "SK": pk,
        "CallId": call_id,
        "Owner": OWNER,
        "CreatedAt": CREATED,
        **attributes,
    }


def items_of(call_id="m1", segments=3, keyframes=1, attachments=1, shared_with=None):
    out = []
    for i in range(segments):
        out.append({"PK": f"trs#{call_id}", "SK": f"s#{i:05d}", "SharedWith": shared_with})
    for i in range(keyframes):
        out.append({"PK": f"kf#{call_id}", "SK": f"f#{i:05d}", "SharedWith": shared_with})
    for i in range(attachments):
        out.append({"PK": f"att#{call_id}", "SK": f"a#{i:05d}", "SharedWith": shared_with})
    for item in out:
        if item["SharedWith"] is None:
            item.pop("SharedWith")
    return out


def lists_of(table, call_id="m1"):
    """The sharing list of every item of the meeting."""
    return {
        (pk, sk): item.get("SharedWith")
        for (pk, sk), item in table.items.items()
        if pk.split("#", 1)[0] in index.FANOUT_STAGES and pk.endswith(f"#{call_id}")
    }


def record(item, sequence="1"):
    serializer = TypeSerializer()
    image = {name: serializer.serialize(value) for name, value in item.items()}
    return {
        "eventName": "MODIFY",
        "dynamodb": {
            "Keys": {"PK": image["PK"], "SK": image["SK"]},
            "NewImage": image,
            "SequenceNumber": sequence,
        },
    }


class Context:
    def __init__(self, seconds):
        self.seconds = seconds

    def get_remaining_time_in_millis(self):
        return int(self.seconds * 1000)


@pytest.fixture
def world(monkeypatch):
    """The fake table and clock, put in place, and who is told they were unshared."""
    clock = Clock()
    state = {"clock": clock, "unshared": []}

    def use(table):
        table.clock = clock
        monkeypatch.setattr(index, "ddbTable", table)
        monkeypatch.setattr(index, "_transact", table.transact)
        state["table"] = table
        return table

    monkeypatch.setattr(index, "time", clock)
    monkeypatch.setattr(index, "SEGMENT_CONCURRENCY", 1)
    monkeypatch.setattr(
        index,
        "_notify_unshared",
        lambda call_id, removed: state["unshared"].append((call_id, list(removed))),
    )
    monkeypatch.setattr(index, "_auto_share_enabled", lambda: True)
    monkeypatch.setattr(index, "account_username", lambda address: address)
    state["use"] = use
    return state


# ── a fan-out that stops after the record is written is completed ───────────


def test_a_fan_out_that_fails_after_the_record_is_completed_and_notifies_on_retry(world) -> None:
    # Ana was shared with by Meeting Sharing and is taken off the list.
    item = meeting(Participants=[], SharedWith=[ANA, CARL], AutoSharedWith=[ANA])
    table = world["use"](MeetingsTable([item, *items_of(segments=4, shared_with=[ANA, CARL])]))

    def fail_on_second(t, _key):
        if len(t.gives) == 2:
            t.on_give = None
            raise RuntimeError("DynamoDB unavailable")

    table.on_give = fail_on_second
    first = index.lambda_handler({"Records": [record(item, "5")]}, Context(300))
    assert first == {"batchItemFailures": [{"itemIdentifier": "5"}]}
    # The share is recorded, the marker says it is unfinished, nobody was told yet.
    stored = table.meeting()
    assert stored["SharedWith"] == [CARL] and "AutoSharedWith" not in stored
    assert stored[index.FANOUT_ATTRIBUTE]["Unshared"] == [ANA]
    assert index.LEASE_ATTRIBUTE not in stored
    assert world["unshared"] == []
    assert [CARL] in lists_of(table).values() and [ANA, CARL] in lists_of(table).values()

    # The stream retries the same record: the plan is already recorded, the marker is not done.
    again = index.lambda_handler({"Records": [record(item, "5")]}, Context(300))
    assert again == {"batchItemFailures": []}
    assert set(map(tuple, lists_of(table).values())) == {(CARL,)}
    assert world["unshared"] == [("m1", [ANA])]
    assert index.FANOUT_ATTRIBUTE not in table.meeting()


def test_a_fan_out_out_of_time_records_its_progress_and_the_next_record_finishes(world) -> None:
    item = meeting(Participants=[{"name": "Ana", "email": ANA}])
    table = world["use"](MeetingsTable([item, *items_of(segments=250)], page=40))
    world["clock"].per_item = 1.0  # each item written takes a second
    # 150 seconds for this invocation: Context(180) less the 30-second margin.
    first = index.lambda_handler({"Records": [record(item, "1")]}, Context(180))
    assert first == {"batchItemFailures": []}
    stored = table.meeting()
    cursor = stored[index.FANOUT_ATTRIBUTE]["Cursor"]
    assert cursor == {"Stage": "trs", "After": "s#00199"}  # two chunks of 100
    assert index.LEASE_ATTRIBUTE not in stored
    done_first = len(table.gives)
    assert done_first == 200

    # The progress is a write of the meeting record, so it reaches the stream.
    second = index.lambda_handler({"Records": [record(stored, "2")]}, Context(300))
    assert second == {"batchItemFailures": []}
    # Only what was left: 50 segments, the key frame and the attachment.
    assert len(table.gives) - done_first == 52
    assert set(map(tuple, lists_of(table).values())) == {(ANA,)}
    assert index.FANOUT_ATTRIBUTE not in table.meeting()


def test_progress_saved_in_one_stage_resumes_in_the_next(world) -> None:
    item = meeting(
        SharedWith=[BEN],
        **{index.FANOUT_ATTRIBUTE: {"Token": "t1", "Cursor": {"Stage": "kf", "After": "f#00000"}}},
    )
    table = world["use"](MeetingsTable([item, *items_of(segments=2, keyframes=2)]))
    assert index.complete_sharing("m1", world["clock"].now + 100) == "done"
    assert [(pk, sk) for pk, sk, _ in table.gives] == [("kf#m1", "f#00001"), ("att#m1", "a#00000")]


# ── idempotent ──────────────────────────────────────────────────────────────


def test_a_completed_share_is_not_fanned_out_again(world) -> None:
    item = meeting(Participants=[{"name": "Ana", "email": ANA}])
    table = world["use"](MeetingsTable([item, *items_of()]))
    index.lambda_handler({"Records": [record(item)]}, Context(300))
    gives = len(table.gives)
    # The records of the share's own writes come back through the stream: nothing to do.
    for later in (item, table.meeting()):
        assert index.lambda_handler({"Records": [record(later)]}, Context(300)) == {
            "batchItemFailures": []
        }
    assert len(table.gives) == gives and table.transactions[1:] == []
    assert index.complete_sharing("m1", world["clock"].now + 100) == "done"
    assert len(table.gives) == gives


def test_writing_an_item_again_gives_it_the_same_list(world) -> None:
    item = meeting(SharedWith=[ANA], **{index.FANOUT_ATTRIBUTE: {"Token": "t1"}})
    table = world["use"](MeetingsTable([item, *items_of(shared_with=[ANA])]))
    assert index.complete_sharing("m1", world["clock"].now + 100) == "done"
    assert set(map(tuple, lists_of(table).values())) == {(ANA,)}


def test_an_item_deleted_meanwhile_is_left_deleted(world) -> None:
    item = meeting(SharedWith=[ANA], **{index.FANOUT_ATTRIBUTE: {"Token": "t1"}})
    table = world["use"](MeetingsTable([item, *items_of(segments=3)]))

    def delete_next(t, key):
        t.items.pop(("trs#m1", "s#00001"), None)

    table.on_give = delete_next
    assert index.complete_sharing("m1", world["clock"].now + 100) == "done"
    assert ("trs#m1", "s#00001") not in table.items


def test_a_meeting_deleted_during_its_fan_out_ends_it(world) -> None:
    item = meeting(SharedWith=[ANA], **{index.FANOUT_ATTRIBUTE: {"Token": "t1"}})
    table = world["use"](MeetingsTable([item, *items_of(segments=1)]))
    table.on_give = lambda t, key: t.items.pop(("c#m1", "c#m1"), None)
    assert index.complete_sharing("m1", world["clock"].now + 100) == "done"
    assert table.meeting() is None


# ── a newer share overtakes a fan-out ───────────────────────────────────────


def test_a_share_made_during_a_fan_out_is_the_list_every_item_ends_with(world, monkeypatch) -> None:
    monkeypatch.setattr(index, "FANOUT_CHUNK", 2)
    item = meeting(SharedWith=[ANA], **{index.FANOUT_ATTRIBUTE: {"Token": "t1"}})
    table = world["use"](MeetingsTable([item, *items_of(segments=5)]))

    def owner_shares(t):
        t.on_chunk_check = None
        row = t.meeting()
        row["SharedWith"] = [ANA, BEN]
        row[index.FANOUT_ATTRIBUTE] = {"Token": "t2", "Unshared": [CARL]}

    table.on_chunk_check = owner_shares
    assert index.complete_sharing("m1", world["clock"].now + 100) == "done"
    assert set(map(tuple, lists_of(table).values())) == {(ANA, BEN)}
    # The first two items were written twice: first with the old list.
    assert table.gives[0][2] == [ANA] and table.gives[2][2] == [ANA, BEN]
    assert world["unshared"] == [("m1", [CARL])]


def test_someone_shared_with_again_before_the_fan_out_ends_is_not_told(world) -> None:
    item = meeting(
        SharedWith=[ANA, BEN], **{index.FANOUT_ATTRIBUTE: {"Token": "t1", "Unshared": [BEN]}}
    )
    world["use"](MeetingsTable([item]))
    index.complete_sharing("m1", world["clock"].now + 100)
    assert world["unshared"] == []


def test_a_new_share_keeps_the_unshared_an_unfinished_one_had_still_to_tell() -> None:
    earlier = meeting(**{index.FANOUT_ATTRIBUTE: {"Token": "t1", "Unshared": [CARL, BEN]}})
    fanout = index._new_fanout(earlier, [BEN.upper()], [ANA])
    assert fanout["Unshared"] == [CARL, ANA]
    assert fanout["Token"] != "t1"
    assert "Unshared" not in index._new_fanout(meeting(), [ANA], [])


# ── one fan-out at a time ───────────────────────────────────────────────────


def test_the_stream_waits_for_the_dialogs_lease_then_takes_over(world) -> None:
    clock = world["clock"]
    lease = {"By": "dialog-1", "Until": int(clock.time() + 10)}
    item = meeting(
        SharedWith=[ANA], **{index.FANOUT_ATTRIBUTE: {"Token": "t1"}, index.LEASE_ATTRIBUTE: lease}
    )
    table = world["use"](MeetingsTable([item, *items_of(segments=1)]))
    assert index.handle_meeting_stream({"Records": [record(item)]}, Context(300)) == {
        "batchItemFailures": []
    }
    assert sum(clock.sleeps) >= 10
    assert index.FANOUT_ATTRIBUTE not in table.meeting()


def test_a_lease_still_held_when_time_runs_out_reports_the_record_for_retry(world) -> None:
    clock = world["clock"]
    lease = {"By": "dialog-1", "Until": int(clock.time() + 10_000)}
    item = meeting(**{index.FANOUT_ATTRIBUTE: {"Token": "t1"}, index.LEASE_ATTRIBUTE: lease})
    table = world["use"](MeetingsTable([item, *items_of(segments=1)]))
    result = index.handle_meeting_stream({"Records": [record(item, "4")]}, Context(60))
    assert result == {"batchItemFailures": [{"itemIdentifier": "4"}]}
    assert table.gives == []


def test_the_dialog_does_not_wait_for_a_fan_out_that_holds_the_meeting(world) -> None:
    clock = world["clock"]
    lease = {"By": "stream", "Until": int(clock.time() + 300)}
    item = meeting(**{index.FANOUT_ATTRIBUTE: {"Token": "t1"}, index.LEASE_ATTRIBUTE: lease})
    world["use"](MeetingsTable([item]))
    assert index.complete_sharing("m1", clock.now + 20, runner="dialog-x") == "busy"
    assert clock.sleeps == []


def test_the_stream_takes_over_a_lease_an_earlier_invocation_left(world) -> None:
    clock = world["clock"]
    lease = {"By": index.STREAM_RUNNER, "Until": int(clock.time() + 300)}
    item = meeting(
        SharedWith=[ANA], **{index.FANOUT_ATTRIBUTE: {"Token": "t1"}, index.LEASE_ATTRIBUTE: lease}
    )
    table = world["use"](MeetingsTable([item, *items_of(segments=1)]))
    assert index.complete_sharing("m1", clock.now + 100, wait=True) == "done"
    assert clock.sleeps == [] and len(table.gives) == 3


def test_a_failed_fan_out_releases_its_lease(world) -> None:
    item = meeting(SharedWith=[ANA], **{index.FANOUT_ATTRIBUTE: {"Token": "t1"}})
    table = world["use"](MeetingsTable([item, *items_of(segments=1)]))

    def fail(_t, _key):
        raise RuntimeError("DynamoDB unavailable")

    table.on_give = fail
    with pytest.raises(RuntimeError):
        index.complete_sharing("m1", world["clock"].now + 100, runner="dialog-x")
    assert index.LEASE_ATTRIBUTE not in table.meeting()
    assert index.FANOUT_ATTRIBUTE in table.meeting()


# ── the share dialog ────────────────────────────────────────────────────────


def _dialog_call(call_id="m1", expected=None):
    call = {"CallId": call_id, "ListPK": LIST_KEY["PK"], "ListSK": f"ts#{CREATED}#id#{call_id}"}
    if expected is not None:
        call["ExpectedSharedWith"] = expected
    return call


def test_the_dialog_saves_on_the_list_it_read_and_completes_the_items(world) -> None:
    item = meeting(SharedWith=[ANA, CARL])
    table = world["use"](
        MeetingsTable([item, {**LIST_KEY, "SharedWith": [ANA, CARL]}, *items_of(shared_with=[ANA])])
    )
    result = index.share_meetings([_dialog_call(expected=[CARL, ANA])], OWNER, f"{ANA}, {BEN}")
    assert result["Result"] == "Meetings shared successfully"
    stored = table.meeting()
    assert stored["SharedWith"] == [ANA, BEN]
    assert table.items[(LIST_KEY["PK"], LIST_KEY["SK"])]["SharedWith"] == [ANA, BEN]
    assert set(map(tuple, lists_of(table).values())) == {(ANA, BEN)}
    assert world["unshared"] == [("m1", [CARL])]
    assert index.FANOUT_ATTRIBUTE not in stored and index.LEASE_ATTRIBUTE not in stored
    # On condition of the SharedWith it read, as Meeting Sharing writes.
    condition = table.transactions[0][0]["Update"]["ConditionExpression"]
    assert condition == "attribute_exists(PK) AND #sw = :seen AND attribute_not_exists(#fan)"


def test_a_dialog_save_after_the_list_changed_is_refused_and_changes_nothing(world) -> None:
    # The dialog read [Carl]; Meeting Sharing has since added Ana.
    item = meeting(SharedWith=[CARL, ANA], AutoSharedWith=[ANA])
    table = world["use"](MeetingsTable([item, *items_of(shared_with=[CARL, ANA])]))
    before = copy.deepcopy(table.items)
    result = index.share_meetings([_dialog_call(expected=[CARL])], OWNER, f"{CARL},{BEN}")
    assert result["ErrorType"] == "SharingListChanged"
    assert result["Result"] == "The sharing list changed; reopen to see the current list"
    assert result["Calls"] == []
    assert table.items == before and table.transactions == [] and table.gives == []
    assert world["unshared"] == []


def test_a_conflict_on_any_meeting_changes_none_of_them(world) -> None:
    first = meeting("m1", SharedWith=[CARL])
    second = meeting("m2", SharedWith=[ANA])
    table = world["use"](MeetingsTable([first, second]))
    calls = [_dialog_call("m1", expected=[CARL]), _dialog_call("m2", expected=[])]
    result = index.share_meetings(calls, OWNER, BEN)
    assert result["ErrorType"] == "SharingListChanged"
    assert result["Calls"] == []
    assert table.transactions == []


def test_meetings_shared_together_are_written_in_one_transaction(world, monkeypatch) -> None:
    """A change to the second meeting landing as the first is written refuses both."""
    first = meeting("m1", SharedWith=[CARL])
    second = meeting("m2", SharedWith=[CARL])
    table = world["use"](MeetingsTable([first, second]))
    original = table.transact

    def second_changes_first(writes):
        assert len(writes) == 4  # both meetings and both list rows, together
        table.meeting("m2")["SharedWith"] = [CARL, ANA]
        monkeypatch.setattr(index, "_transact", original)
        return original(writes)

    monkeypatch.setattr(index, "_transact", second_changes_first)
    calls = [_dialog_call("m1", expected=[CARL]), _dialog_call("m2", expected=[CARL])]
    result = index.share_meetings(calls, OWNER, f"{CARL},{BEN}")
    assert result["ErrorType"] == "SharingListChanged" and result["Calls"] == []
    assert table.meeting("m1")["SharedWith"] == [CARL]
    assert index.FANOUT_ATTRIBUTE not in table.meeting("m1")


def test_groups_saved_before_a_conflict_are_named(world, monkeypatch) -> None:
    monkeypatch.setattr(index, "SHARE_GROUP", 1)
    first = meeting("m1", SharedWith=[CARL])
    second = meeting("m2", SharedWith=[ANA])
    table = world["use"](MeetingsTable([first, second]))
    calls = [_dialog_call("m1", expected=[CARL]), _dialog_call("m2", expected=[CARL])]
    result = index.share_meetings(calls, OWNER, f"{CARL},{BEN}")
    assert result["ErrorType"] == "SharingListChanged"
    assert result["Calls"] == ["m1"]
    assert "1 of 2 meetings were shared before it" in result["Result"]
    assert table.meeting("m1")["SharedWith"] == [CARL, BEN]
    assert table.meeting("m2")["SharedWith"] == [ANA]


def test_a_share_landing_between_the_dialogs_read_and_write_is_refused(world, monkeypatch) -> None:
    item = meeting(SharedWith=[CARL])
    table = world["use"](MeetingsTable([item]))
    original = table.transact

    def auto_share_lands_first(writes):
        table.meeting()["SharedWith"] = [CARL, ANA]
        table.transact = original
        monkeypatch.setattr(index, "_transact", original)
        return original(writes)

    monkeypatch.setattr(index, "_transact", auto_share_lands_first)
    result = index.share_meetings([_dialog_call(expected=[CARL])], OWNER, f"{CARL},{BEN}")
    assert result["ErrorType"] == "SharingListChanged"
    assert table.meeting()["SharedWith"] == [CARL, ANA]


def test_a_caller_that_sends_no_expected_list_saves_as_before(world) -> None:
    item = meeting(SharedWith=[CARL, ANA])
    table = world["use"](MeetingsTable([item]))
    result = index.share_meetings([_dialog_call()], OWNER, BEN)
    assert result["Result"] == "Meetings shared successfully"
    assert table.meeting()["SharedWith"] == [BEN]
    assert world["unshared"] == [("m1", [CARL, ANA])]


def test_unsharing_everyone_removes_the_list_from_the_meeting_and_its_items(world) -> None:
    item = meeting(SharedWith=[ANA])
    table = world["use"](MeetingsTable([item, *items_of(shared_with=[ANA])]))
    index.share_meetings([_dialog_call(expected=[ANA])], OWNER, "")
    assert "SharedWith" not in table.meeting()
    assert set(lists_of(table).values()) == {None}
    assert world["unshared"] == [("m1", [ANA])]


def test_what_the_dialogs_time_does_not_cover_the_stream_completes(world, monkeypatch) -> None:
    monkeypatch.setattr(index, "DIALOG_FANOUT_SECONDS", 120.0)
    item = meeting()
    table = world["use"](MeetingsTable([item, *items_of(segments=250)]))
    world["clock"].per_item = 1.0
    result = index.share_meetings([_dialog_call(expected=[])], OWNER, ANA)
    assert result["Result"] == "Meetings shared successfully"
    stored = table.meeting()
    assert stored[index.FANOUT_ATTRIBUTE]["Cursor"] == {"Stage": "trs", "After": "s#00199"}
    assert index.LEASE_ATTRIBUTE not in stored and world["unshared"] == []
    # The progress reaches the stream, which finishes the share (Meeting Sharing off or on).
    monkeypatch.setattr(index, "_auto_share_enabled", lambda: False)
    index.handle_meeting_stream({"Records": [record(stored)]}, Context(300))
    assert set(map(tuple, lists_of(table).values())) == {(ANA,)}
    assert index.FANOUT_ATTRIBUTE not in table.meeting()


def test_a_dialog_fan_out_that_fails_is_left_to_the_stream_and_the_save_succeeds(world) -> None:
    item = meeting()
    table = world["use"](MeetingsTable([item, *items_of(segments=2)]))

    def fail(_t, _key):
        raise RuntimeError("DynamoDB unavailable")

    table.on_give = fail
    result = index.share_meetings([_dialog_call(expected=[])], OWNER, ANA)
    assert result["Result"] == "Meetings shared successfully"
    assert table.meeting()["SharedWith"] == [ANA]
    assert index.FANOUT_ATTRIBUTE in table.meeting()
    assert index.LEASE_ATTRIBUTE not in table.meeting()


def test_the_dialog_writes_only_the_list_row_of_the_meeting_it_shares(world) -> None:
    world["use"](MeetingsTable([meeting()]))
    other = {"CallId": "m1", "ListPK": LIST_KEY["PK"], "ListSK": f"ts#{CREATED}#id#m2"}
    assert index.share_meetings([other], OWNER, ANA) == {
        "Result": "Invalid meeting list key provided"
    }
    assert index.share_meetings([{**other, "ListPK": "c#m1"}], OWNER, ANA)["Result"].startswith(
        "Invalid"
    )


def test_the_handler_passes_the_expected_list_through(world, monkeypatch) -> None:
    table = world["use"](MeetingsTable([meeting(SharedWith=[ANA])]))
    monkeypatch.setattr(index, "verify_permissions", lambda event: True)
    event = {
        "identity": {"username": OWNER},
        "info": {"fieldName": "shareMeetings"},
        "arguments": {"input": {"Calls": [_dialog_call(expected=[])], "MeetingRecipients": BEN}},
    }
    assert index.lambda_handler(event, Context(30))["ErrorType"] == "SharingListChanged"
    assert table.meeting()["SharedWith"] == [ANA]
