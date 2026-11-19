# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""list_meetings decides owner, participant and status before it counts toward
the limit, accepts IN_PROGRESS, and reads meeting rows in batches.

The table is a stand-in that pages like the TypeDateIndex (newest first,
``Limit``, ``LastEvaluatedKey``) and counts every request made to it.
"""

import inspect
import json
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("CALLS_TABLE", "test-calls-table")
os.environ.setdefault("LMA_WEB_APP_URL", "https://example.invalid")

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from tools import list_meetings  # noqa: E402

ME = "me@example.com"
OTHER = "other@example.com"


def meeting(call_id, owner=ME, status="ENDED", **extra):
    row = {
        "PK": f"c#{call_id}",
        "SK": f"c#{call_id}",
        "CallId": call_id,
        "Owner": owner,
        "Status": status,
        "MeetingTopic": f"topic {call_id}",
        "CreatedAt": "2026-11-18T10:00:00Z",
    }
    row.update(extra)
    return row


def project(row, request):
    """The attributes of ``row`` a ProjectionExpression names, as DynamoDB returns them."""
    expression = request.get("ProjectionExpression")
    if expression is None:
        return row
    names = request.get("ExpressionAttributeNames", {})
    projected = {}
    for path in expression.split(","):
        parts = [names.get(part, part) for part in path.strip().split(".")]
        source, target = row, projected
        for depth, part in enumerate(parts):
            if not isinstance(source, dict) or part not in source:
                break
            if depth == len(parts) - 1:
                target[part] = source[part]
            else:
                source = source[part]
                target = target.setdefault(part, {})
    return projected


def size_of(row):
    return len(json.dumps(row, default=str))


class FakeClient:
    """The low-level client of the table: BatchGetItem only."""

    def __init__(self, table):
        self.table = table

    def batch_get_item(self, RequestItems):
        table = self.table
        request = RequestItems[table.name]
        keys = request["Keys"]
        table.batch_calls.append([key["PK"] for key in keys])
        table.batch_projections.append(request.get("ProjectionExpression"))
        if len(table.batch_calls) <= table.failing_batches:
            raise RuntimeError("batch read failed")
        held = 0
        if len(table.batch_calls) <= table.unprocessed_calls:
            held = min(table.unprocessed_count, len(keys))
        served, left = (keys[: len(keys) - held], keys[len(keys) - held :])
        items = [project(table.rows[k["PK"]], request) for k in served if k["PK"] in table.rows]
        table.bytes_read += sum(size_of(item) for item in items)
        response = {"Responses": {table.name: items}}
        if left:
            response["UnprocessedKeys"] = {table.name: {"Keys": left}}
        if request.get("ProjectionExpression") and table.after_decision:
            table.after_decision(table)
        return response


class FakeTable:
    """Index rows newest first, plus the meeting rows they point at."""

    name = "calls"

    def __init__(self, meetings, extra_index_rows=()):
        self.rows = {m["PK"]: m for m in meetings}
        self.index = [{"CallId": m["CallId"], "SK": f"ts#{i:06d}"} for i, m in enumerate(meetings)]
        self.index.extend(extra_index_rows)
        self.query_calls = []
        self.batch_calls = []
        self.batch_projections = []
        self.get_item_calls = []
        self.get_item_projections = []
        self.bytes_read = 0
        self.failing_batches = 0
        self.unprocessed_calls = 0
        self.unprocessed_count = 0
        self.unreadable = set()
        self.after_decision = None
        # DynamoDB also returns a LastEvaluatedKey after a full page that ends
        # exactly at the end of the range; ``dynamodb_keys`` does the same.
        self.dynamodb_keys = False
        self.failing_probe = False
        self.meta = type("Meta", (), {"client": FakeClient(self)})()

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        if self.failing_probe and kwargs["Limit"] == 1:
            raise RuntimeError("throttled")
        start = 0
        if "ExclusiveStartKey" in kwargs:
            start = int(kwargs["ExclusiveStartKey"]["SK"][3:]) + 1
        page = self.index[start : start + kwargs["Limit"]]
        response = {"Items": page}
        full_page = self.dynamodb_keys and page and len(page) == kwargs["Limit"]
        if start + len(page) < len(self.index) or full_page:
            response["LastEvaluatedKey"] = {"SK": page[-1]["SK"]}
        return response

    def get_item(self, Key, **request):
        self.get_item_calls.append(Key["PK"])
        self.get_item_projections.append(request.get("ProjectionExpression"))
        call_id = Key["PK"][2:]
        if call_id in self.unreadable:
            raise RuntimeError("read failed")
        row = self.rows.get(Key["PK"])
        if not row:
            return {}
        item = project(row, request)
        self.bytes_read += size_of(item)
        return {"Item": item}

    @property
    def requests(self):
        return len(self.query_calls) + len(self.batch_calls) + len(self.get_item_calls)

    def decision_batches(self):
        """The batches that read only the attributes the filters need."""
        return [b for b, p in zip(self.batch_calls, self.batch_projections) if p is not None]

    def full_batches(self):
        """The batches that read whole meeting rows."""
        return [b for b, p in zip(self.batch_calls, self.batch_projections) if p is None]


def run(table, **kwargs):
    """list_meetings.execute against the stand-in table."""
    resource = type("Resource", (), {"Table": lambda self, name: table})()
    arguments = {"user_id": ME, "is_admin": False, "limit": 20}
    arguments.update(kwargs)
    with patch.object(list_meetings.boto3, "resource", return_value=resource):
        return list_meetings.execute(**arguments)


def ids(result):
    return [m["meetingId"] for m in result["meetings"]]


class TestFiltersBeforeTheLimit(unittest.TestCase):
    def test_other_users_newer_meetings_do_not_use_up_the_limit(self):
        newer = [meeting(f"o{i}", owner=OTHER) for i in range(30)]
        table = FakeTable(newer + [meeting("mine")])

        result = run(table, limit=1)

        self.assertEqual(ids(result), ["mine"])
        self.assertEqual(result["count"], 1)

    def test_status_is_decided_before_the_limit(self):
        rows = [meeting(f"e{i}", status="ENDED") for i in range(30)]
        table = FakeTable(rows + [meeting("live", status="STARTED")])

        self.assertEqual(ids(run(table, status="STARTED", limit=1)), ["live"])

    def test_participant_is_decided_before_the_limit(self):
        rows = [meeting(f"n{i}", Participants=["Cy"]) for i in range(30)]
        table = FakeTable(rows + [meeting("with-ann", Participants=["Ann Lee"])])

        self.assertEqual(ids(run(table, participant="ann", limit=1)), ["with-ann"])

    def test_all_three_filters_together(self):
        rows = [
            meeting("a", owner=OTHER, status="STARTED", Participants=["Ann"]),
            meeting("b", status="ENDED", Participants=["Ann"]),
            meeting("c", status="STARTED", Participants=["Cy"]),
            meeting("d", status="STARTED", Participants=["Ann"]),
        ]

        result = run(FakeTable(rows), status="started", participant="ann", limit=1)

        self.assertEqual(ids(result), ["d"])

    def test_a_caller_never_sees_another_users_meeting(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(5)])

        self.assertEqual(run(table)["meetings"], [])

    def test_agent_id_counts_as_owner(self):
        older = meeting("old-row", owner=None, AgentId=ME)
        del older["Owner"]
        table = FakeTable([meeting("x", owner=OTHER), older])

        self.assertEqual(ids(run(table, limit=1)), ["old-row"])

    def test_a_caller_without_an_identity_sees_nothing(self):
        # An empty or missing id must not match a row whose owner is empty or missing.
        ownerless = meeting("none", owner=None)
        del ownerless["Owner"]
        rows = [
            ownerless,
            meeting("empty", owner=""),
            meeting("null", owner=None),
            meeting("agent-empty", owner=OTHER, AgentId=""),
            meeting("mine"),
        ]

        for user_id in (None, ""):
            with self.subTest(user_id=user_id):
                result = run(FakeTable(rows), user_id=user_id)

                self.assertEqual(result["meetings"], [])
                self.assertEqual(result["count"], 0)

    def test_a_caller_without_an_identity_sees_nothing_with_every_filter_set(self):
        rows = [meeting("empty", owner="", Participants=["Ann"], status="STARTED")]

        for user_id in (None, ""):
            result = run(
                FakeTable(rows), user_id=user_id, status="STARTED", participant="ann", limit=5
            )

            self.assertEqual(result["meetings"], [])

    def test_an_admin_without_an_identity_still_sees_every_meeting(self):
        table = FakeTable([meeting("a", owner=OTHER), meeting("b", owner="")])

        result = run(table, user_id=None, is_admin=True)

        self.assertEqual(ids(result), ["a", "b"])

    def test_admin_sees_every_meeting_and_the_limit_still_applies(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(10)])

        result = run(table, user_id="admin", is_admin=True, limit=3)

        self.assertEqual(ids(result), ["o0", "o1", "o2"])

    def test_admin_with_a_status_still_gets_a_full_list(self):
        rows = [meeting(f"e{i}", owner=OTHER) for i in range(30)]
        table = FakeTable(rows + [meeting("live", owner=OTHER, status="TRANSCRIBING")])

        result = run(table, user_id="admin", is_admin=True, status="IN_PROGRESS", limit=1)

        self.assertEqual(ids(result), ["live"])

    def test_the_walk_stops_as_soon_as_the_limit_is_reached(self):
        table = FakeTable([meeting(f"m{i}") for i in range(500)])

        result = run(table, limit=3)

        self.assertEqual(ids(result), ["m0", "m1", "m2"])
        self.assertEqual(len(table.query_calls), 1)
        # One batch decides on the page, one more reads the 3 that are listed.
        self.assertEqual([len(b) for b in table.decision_batches()], [100])
        self.assertEqual(table.full_batches(), [["c#m0", "c#m1", "c#m2"]])

    def test_results_keep_the_newest_first_order(self):
        rows = [meeting(f"m{i}", owner=OTHER if i % 2 else ME) for i in range(10)]

        self.assertEqual(ids(run(FakeTable(rows), limit=10)), ["m0", "m2", "m4", "m6", "m8"])

    def test_the_answer_echoes_the_filters_as_given(self):
        result = run(FakeTable([]), status="in_progress", participant="ann")

        self.assertEqual(result["filters"]["status"], "in_progress")
        self.assertEqual(result["filters"]["participant"], "ann")


class TestStatusValues(unittest.TestCase):
    def setUp(self):
        self.rows = [
            meeting(f"{s}-1", status=s)
            for s in list_meetings.STATUS_FILTERS
            if s not in ("ALL", "IN_PROGRESS")
        ]

    def listed(self, status):
        return sorted(ids(run(FakeTable(self.rows), status=status)))

    def test_no_status_and_all_list_every_status(self):
        everything = sorted(m["CallId"] for m in self.rows)
        for status in (None, "ALL", "all", " All "):
            with self.subTest(status=status):
                self.assertEqual(self.listed(status), everything)

    def test_in_progress_is_started_and_transcribing(self):
        for status in ("IN_PROGRESS", "in_progress", " In_Progress\n"):
            with self.subTest(status=status):
                self.assertEqual(self.listed(status), ["STARTED-1", "TRANSCRIBING-1"])

    def test_each_stored_status_matches_itself_only(self):
        for stored in ("STARTED", "TRANSCRIBING", "ERRORED", "ENDED"):
            for form in (stored, stored.lower(), f"  {stored.title()} "):
                with self.subTest(form=form):
                    self.assertEqual(self.listed(form), [f"{stored}-1"])

    def test_statuses_for_returns_the_stored_statuses(self):
        self.assertIsNone(list_meetings.statuses_for(None))
        self.assertIsNone(list_meetings.statuses_for("ALL"))
        self.assertEqual(list_meetings.statuses_for("in_progress"), {"STARTED", "TRANSCRIBING"})
        self.assertEqual(list_meetings.statuses_for("ended"), {"ENDED"})

    def test_a_status_that_is_not_accepted_is_refused_naming_the_accepted_ones(self):
        for bad in ("RUNNING", "ENDED,STARTED", "IN PROGRESS", "_", 7, ["ENDED"], True):
            with self.subTest(status=bad):
                with self.assertRaises(ValueError) as caught:
                    list_meetings.statuses_for(bad)
                message = str(caught.exception)
                for accepted in (
                    "ALL",
                    "IN_PROGRESS",
                    "STARTED",
                    "TRANSCRIBING",
                    "ERRORED",
                    "ENDED",
                ):
                    self.assertIn(accepted, message)

    def test_a_blank_status_is_no_filter(self):
        everything = sorted(m["CallId"] for m in self.rows)
        for status in ("", "  ", "\t\n"):
            with self.subTest(status=status):
                self.assertIsNone(list_meetings.statuses_for(status))
                self.assertEqual(self.listed(status), everything)

    def test_a_blank_status_is_echoed_as_given(self):
        self.assertEqual(run(FakeTable(self.rows), status="")["filters"]["status"], "")

    def test_execute_refuses_a_bad_status_before_reading_anything(self):
        table = FakeTable([meeting("m")])

        with self.assertRaises(ValueError) as caught:
            run(table, status="RUNNING")

        self.assertNotIn("Failed to list meetings", str(caught.exception))
        self.assertEqual(table.requests, 0)


class TestPageCap(unittest.TestCase):
    def test_a_filter_that_matches_nothing_reads_at_most_ten_pages_of_100(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(2500)])

        result = run(table, limit=5)

        self.assertEqual(result["meetings"], [])
        # Ten pages of 100, then one row asked for to learn whether rows are left.
        self.assertEqual([call["Limit"] for call in table.query_calls], [100] * 10 + [1])

    def test_a_match_beyond_the_cap_is_not_found(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(1000)] + [meeting("far")])

        self.assertEqual(run(table, limit=1)["meetings"], [])

    def test_a_match_on_the_last_allowed_page_is_found(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(999)] + [meeting("last")])

        self.assertEqual(ids(run(table, limit=1)), ["last"])

    def test_a_walk_that_does_not_filter_keeps_its_old_page_size(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(300)])

        run(table, user_id="admin", is_admin=True, limit=7)
        run(table, user_id="admin", is_admin=True, limit=250)

        self.assertEqual([call["Limit"] for call in table.query_calls][:1], [7])
        self.assertEqual(table.query_calls[1]["Limit"], 100)

    def test_a_filtered_walk_reads_pages_of_100_whatever_the_limit(self):
        table = FakeTable([meeting(f"m{i}") for i in range(300)])

        run(table, limit=2)

        self.assertEqual(table.query_calls[0]["Limit"], list_meetings.FILTERED_PAGE_SIZE)
        self.assertEqual(list_meetings.FILTERED_PAGE_SIZE, 100)

    def test_the_index_is_read_newest_first(self):
        table = FakeTable([meeting("m")])

        run(table)

        self.assertIs(table.query_calls[0]["ScanIndexForward"], False)

    def test_a_failed_index_query_ends_the_walk_with_what_was_found(self):
        table = FakeTable([meeting(f"m{i}") for i in range(250)])
        original = table.query

        def failing(**kwargs):
            if "ExclusiveStartKey" in kwargs:
                raise RuntimeError("throttled")
            return original(**kwargs)

        table.query = failing

        self.assertEqual(len(run(table, limit=150)["meetings"]), 100)


class TestBatchReads(unittest.TestCase):
    def test_a_table_of_1000_rows_takes_at_most_50_requests(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(1000)])

        run(table, limit=1)

        self.assertEqual(len(table.get_item_calls), 0)
        self.assertLessEqual(table.requests, 50)

    def test_a_filtered_walk_decides_on_a_whole_page_per_request(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(1000)])

        run(table, limit=1)
        small = [len(batch) for batch in table.batch_calls]
        table.batch_calls.clear()
        run(table, limit=500)
        large = [len(batch) for batch in table.batch_calls]

        self.assertEqual(small, [100] * 10)
        self.assertEqual(large, [100] * 10)

    def test_whole_rows_wait_until_the_walk_has_enough_matches(self):
        rows = []
        for page in range(3):
            rows += [meeting(f"m{page}-{i}") for i in range(10)]
            rows += [meeting(f"o{page}-{i}", owner=OTHER) for i in range(90)]
        table = FakeTable(rows)

        run(table, limit=40)

        # 10 matches a page are not enough for 40; the walk ends after the third
        # page, and the 30 matches are then read whole in one request.
        self.assertEqual(len(table.decision_batches()), 3)
        self.assertEqual(
            table.full_batches(), [[f"c#m{p}-{i}" for p in range(3) for i in range(10)]]
        )

    def test_whole_rows_are_read_no_more_than_needed_and_at_most_100_at_a_time(self):
        table = FakeTable([meeting(f"m{i}") for i in range(300)])

        run(table, limit=150)

        self.assertEqual([len(batch) for batch in table.full_batches()], [100, 50])

    def test_a_walk_without_a_filter_reads_batches_of_what_is_still_needed(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(300)])

        run(table, user_id="admin", is_admin=True, limit=10)
        run(table, user_id="admin", is_admin=True, limit=250)

        sizes = [len(batch) for batch in table.batch_calls]
        self.assertEqual(sizes, [10, 100, 100, 50])
        self.assertEqual(set(table.batch_projections), {None})

    def test_rows_are_read_in_index_order_newest_first(self):
        table = FakeTable([meeting(f"m{i}") for i in range(70)])

        run(table, limit=60)

        asked = [key for batch in table.batch_calls for key in batch]
        self.assertEqual(asked[:60], [f"c#m{i}" for i in range(60)])

    def test_a_row_listed_twice_in_the_index_is_read_once(self):
        rows = [meeting("a"), meeting("b")]
        table = FakeTable(rows, extra_index_rows=[{"CallId": "a", "SK": "ts#000002"}])

        self.assertEqual(ids(run(table)), ["a", "b"])
        self.assertEqual(sum(batch.count("c#a") for batch in table.decision_batches()), 1)
        self.assertEqual(sum(batch.count("c#a") for batch in table.full_batches()), 1)

    def test_an_index_row_without_a_call_id_is_skipped(self):
        table = FakeTable([meeting("a")], extra_index_rows=[{"SK": "ts#000001"}])

        self.assertEqual(ids(run(table)), ["a"])

    def test_get_meetings_by_ids_returns_the_rows_by_id(self):
        table = FakeTable([meeting("a"), meeting("b")])

        found = list_meetings.get_meetings_by_ids(table, ["a", "b", "missing"])

        self.assertEqual(sorted(found), ["a", "b"])
        self.assertEqual(found["a"]["MeetingTopic"], "topic a")

    def test_get_meetings_by_ids_takes_at_most_100_ids(self):
        table = FakeTable([])

        with self.assertRaises(ValueError):
            list_meetings.get_meetings_by_ids(table, [f"id{i}" for i in range(101)])
        self.assertEqual(table.requests, 0)

    def test_get_meetings_by_ids_makes_no_request_for_no_ids(self):
        table = FakeTable([])

        self.assertEqual(list_meetings.get_meetings_by_ids(table, []), {})
        self.assertEqual(table.requests, 0)

    def test_get_meetings_by_ids_accepts_exactly_100_ids(self):
        table = FakeTable([meeting(f"m{i}") for i in range(100)])

        found = list_meetings.get_meetings_by_ids(table, [f"m{i}" for i in range(100)])

        self.assertEqual(len(found), 100)


class TestUnprocessedKeys(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(list_meetings.time, "sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_unprocessed_keys_are_asked_for_again(self):
        table = FakeTable([meeting(f"m{i}") for i in range(10)])
        table.unprocessed_calls, table.unprocessed_count = 1, 4

        found = list_meetings.get_meetings_by_ids(table, [f"m{i}" for i in range(10)])

        self.assertEqual(len(found), 10)
        self.assertEqual([len(batch) for batch in table.batch_calls], [10, 4])

    def test_each_retry_waits_twice_as_long(self):
        table = FakeTable([meeting(f"m{i}") for i in range(10)])
        table.unprocessed_calls, table.unprocessed_count = 3, 2

        list_meetings.get_meetings_by_ids(table, [f"m{i}" for i in range(10)])

        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [0.05, 0.1, 0.2])

    def test_a_clean_read_does_not_wait(self):
        table = FakeTable([meeting("a")])

        list_meetings.get_meetings_by_ids(table, ["a"])

        self.sleep.assert_not_called()

    def test_keys_still_unprocessed_after_4_requests_are_read_one_by_one(self):
        table = FakeTable([meeting(f"m{i}") for i in range(10)])
        table.unprocessed_calls, table.unprocessed_count = 99, 3

        rows, unreadable = list_meetings.read_meetings(table, [f"m{i}" for i in range(10)])

        self.assertEqual(len(table.batch_calls), list_meetings.BATCH_GET_ATTEMPTS)
        self.assertEqual(list_meetings.BATCH_GET_ATTEMPTS, 4)
        self.assertEqual(sorted(table.get_item_calls), ["c#m7", "c#m8", "c#m9"])
        self.assertEqual((len(rows), unreadable), (10, 0))

    def test_a_list_is_complete_when_every_batch_leaves_keys_unprocessed(self):
        table = FakeTable([meeting(f"m{i}") for i in range(30)])
        table.unprocessed_calls, table.unprocessed_count = 99, 5

        self.assertEqual(len(run(table, limit=30)["meetings"]), 30)


class TestFallbackToSingleReads(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(list_meetings.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_failed_batch_falls_back_to_one_read_per_meeting(self):
        table = FakeTable([meeting(f"m{i}") for i in range(5)])
        table.failing_batches = 99

        result = run(table, limit=5)

        self.assertEqual(ids(result), [f"m{i}" for i in range(5)])
        # Five reads of the attributes the filters need, then five of whole rows.
        self.assertEqual(len(table.get_item_calls), 10)
        self.assertEqual(table.get_item_projections.count(None), 5)
        self.assertNotIn("note", result)

    def test_only_the_ids_the_batch_did_not_return_are_read_again(self):
        table = FakeTable([meeting("a"), meeting("b")])
        table.unprocessed_calls, table.unprocessed_count = 99, 1

        list_meetings.read_meetings(table, ["a", "b"])

        self.assertEqual(table.get_item_calls, ["c#b"])

    def test_a_row_that_cannot_be_read_is_counted_and_noted(self):
        table = FakeTable([meeting("a"), meeting("b"), meeting("c")])
        table.failing_batches = 99
        table.unreadable = {"b"}

        result = run(table)

        self.assertEqual(ids(result), ["a", "c"])
        self.assertEqual(result["note"], list_meetings.UNREADABLE_NOTE)
        self.assertEqual(
            list_meetings.UNREADABLE_NOTE, "Some meetings could not be read; try again."
        )

    def test_several_unreadable_rows_give_one_note(self):
        table = FakeTable([meeting(f"m{i}") for i in range(4)])
        table.failing_batches = 99
        table.unreadable = {"m0", "m2"}

        result = run(table)

        self.assertEqual(ids(result), ["m1", "m3"])
        self.assertEqual(result["note"], list_meetings.UNREADABLE_NOTE)

    def test_a_row_that_no_longer_exists_is_skipped_without_a_note(self):
        table = FakeTable(
            [meeting("a"), meeting("c")], extra_index_rows=[{"CallId": "gone", "SK": "ts#000002"}]
        )

        result = run(table)

        self.assertEqual(ids(result), ["a", "c"])
        self.assertNotIn("note", result)

    def test_read_meetings_counts_unreadable_and_not_gone_rows(self):
        table = FakeTable([meeting("a")])
        table.unreadable = {"broken"}

        rows, unreadable = list_meetings.read_meetings(table, ["a", "broken", "gone"])

        self.assertEqual((sorted(rows), unreadable), (["a"], 1))

    def test_an_unreadable_row_is_not_counted_when_the_single_read_works(self):
        table = FakeTable([meeting("a")])
        table.failing_batches = 1

        rows, unreadable = list_meetings.read_meetings(table, ["a"])

        self.assertEqual((sorted(rows), unreadable), (["a"], 0))

    def test_an_answer_without_unreadable_rows_has_no_note_key(self):
        result = run(FakeTable([meeting("a")]))

        self.assertEqual(set(result), {"meetings", "count", "filters"})


class TestCallersWithoutAFilter(unittest.TestCase):
    """query_by_date_range behaves as before; walk_date_range takes the row filter."""

    def test_every_row_counts_toward_the_limit(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(10)])

        rows = list_meetings.query_by_date_range(table, "2026-11-01", "2026-11-18", 4)

        self.assertEqual([row["CallId"] for row in rows], ["o0", "o1", "o2", "o3"])
        self.assertEqual(table.query_calls[0]["Limit"], 4)

    def test_it_returns_a_list_of_rows(self):
        table = FakeTable([meeting("a")])

        rows = list_meetings.query_by_date_range(table, "2026-11-01", "2026-11-18", 10)

        self.assertIsInstance(rows, list)
        self.assertEqual(rows[0]["MeetingTopic"], "topic a")

    def test_the_wrapper_has_its_original_signature(self):
        parameters = inspect.signature(list_meetings.query_by_date_range).parameters

        self.assertEqual(list(parameters), ["table", "start_date", "end_date", "limit"])

    def test_an_include_function_decides_which_rows_count(self):
        table = FakeTable(
            [meeting(f"m{i}", Status="ENDED" if i % 3 else "STARTED") for i in range(30)]
        )

        rows = list_meetings.walk_date_range(
            table, "2026-11-01", "2026-11-18", 2, lambda m: m["Status"] == "STARTED"
        )[0]

        self.assertEqual([row["CallId"] for row in rows], ["m0", "m3"])

    def test_a_walk_returns_at_most_the_limit_with_or_without_a_filter(self):
        rows = [meeting(f"m{i}") for i in range(150)]

        for include in (None, lambda m: True):
            for limit in (1, 7, 100, 120):
                with self.subTest(filtered=include is not None, limit=limit):
                    found = list_meetings.walk_date_range(
                        FakeTable(rows), "2026-11-01", "2026-11-18", limit, include
                    )[0]

                    self.assertEqual(len(found), limit)

    def test_the_answer_of_execute_holds_at_most_the_limit(self):
        table = FakeTable([meeting(f"m{i}") for i in range(150)])

        for limit in (1, 7, 100):
            result = run(table, limit=limit)

            self.assertEqual((len(result["meetings"]), result["count"]), (limit, limit))

    def test_the_sort_key_bounds_are_unchanged(self):
        table = FakeTable([])

        list_meetings.query_by_date_range(table, "2026-11-01", "2026-11-18", 5)

        condition = table.query_calls[0]["KeyConditionExpression"]
        lo, hi = condition._values[1]._values[1:]
        self.assertEqual((lo, hi), ("ts#2026-11-01", "ts#2026-11-18~"))


class TestAnswerShape(unittest.TestCase):
    def test_a_meeting_is_formatted_as_before(self):
        table = FakeTable([meeting("a", Summary="x", TotalConversationDurationMillis=90000)])

        entry = run(table)["meetings"][0]

        self.assertEqual(entry["meetingId"], "a")
        self.assertEqual(entry["meetingName"], "topic a")
        self.assertEqual(entry["duration"], 90.0)
        self.assertTrue(entry["hasSummary"])
        self.assertEqual(entry["owner"], ME)

    def test_given_dates_are_used_for_the_walk(self):
        table = FakeTable([meeting("a")])

        run(table, start_date="2026-11-01", end_date="2026-11-18")

        condition = table.query_calls[0]["KeyConditionExpression"]
        self.assertEqual(condition._values[1]._values[1:], ("ts#2026-11-01", "ts#2026-11-18~"))

    def test_a_failure_while_listing_is_reported_as_a_list_failure(self):
        table = FakeTable([meeting("a", Participants=["Ann", {"Name": "x"}])])

        with self.assertRaises(ValueError) as caught:
            run(table, participant="zed")

        self.assertIn("Failed to list meetings", str(caught.exception))


class TestTwoStepReads(unittest.TestCase):
    """A filtered walk asks for a few attributes first and for whole rows of matches only."""

    def big(self, call_id, owner=OTHER, **extra):
        return meeting(call_id, owner=owner, Summary="x" * 4000, **extra)

    def test_rows_that_do_not_match_are_never_read_whole(self):
        table = FakeTable([self.big(f"o{i}") for i in range(1000)])

        result = run(table)

        self.assertEqual(result["meetings"], [])
        self.assertEqual(table.full_batches(), [])
        self.assertEqual(len(table.decision_batches()), 10)
        # The response of 1,000 whole rows would be over 4 MB; the attributes the
        # filters need are well under 100 bytes a row. (DynamoDB still reads, and
        # bills, the whole items; only the size of the response shrinks.)
        self.assertLess(table.bytes_read, 100 * 1000)

    def test_the_first_read_names_only_what_the_filters_and_the_key_need(self):
        table = FakeTable([meeting("a")])

        run(table)

        request = list_meetings.DECISION_PROJECTION
        named = {
            request["ExpressionAttributeNames"][part.strip()]
            for part in request["ProjectionExpression"].split(",")
        }
        self.assertEqual(named, {"PK", "CallId", "Owner", "AgentId", "Status", "Participants"})
        self.assertEqual(table.batch_projections[0], request["ProjectionExpression"])

    def test_only_the_matches_still_needed_are_read_whole(self):
        rows = [self.big(f"o{i}") if i % 3 else self.big(f"m{i}", owner=ME) for i in range(90)]
        table = FakeTable(rows)

        result = run(table, limit=5)

        self.assertEqual(ids(result), ["m0", "m3", "m6", "m9", "m12"])
        self.assertEqual(table.full_batches(), [["c#m0", "c#m3", "c#m6", "c#m9", "c#m12"]])

    def test_the_listed_meetings_are_whole_rows(self):
        table = FakeTable([meeting("a", Summary="text", TranscriptUri="s3://x")])

        entry = run(table)["meetings"][0]

        self.assertEqual(entry["meetingName"], "topic a")
        self.assertTrue(entry["hasSummary"])
        self.assertTrue(entry["hasTranscript"])

    def test_answers_are_those_of_reading_every_row_whole(self):
        import random

        chooser = random.Random(5)
        rows = []
        for i in range(400):
            row = meeting(
                f"m{i}",
                owner=chooser.choice([ME, OTHER, "third@example.com"]),
                status=chooser.choice(["STARTED", "TRANSCRIBING", "ENDED", "ERRORED"]),
                Participants=chooser.choice([["Ann"], ["Cy"], [{"Name": "Ann Lee"}], []]),
            )
            if chooser.random() < 0.1:
                del row["Owner"]
                row["AgentId"] = chooser.choice([ME, OTHER])
            rows.append(row)
        for user, admin in ((ME, False), (OTHER, False), ("admin", True)):
            for status in (None, "IN_PROGRESS", "ENDED"):
                for participant in (None, "ann"):
                    for limit in (1, 7, 60):
                        statuses = list_meetings.statuses_for(status)
                        expected = [
                            r["CallId"]
                            for r in rows
                            if (admin or user in (r.get("Owner"), r.get("AgentId")))
                            and (
                                not participant
                                or list_meetings.participant_in_meeting(r, participant)
                            )
                            and (statuses is None or r["Status"] in statuses)
                        ][:limit]
                        with self.subTest(user=user, status=status, p=participant, limit=limit):
                            result = run(
                                FakeTable(rows),
                                user_id=user,
                                is_admin=admin,
                                status=status,
                                participant=participant,
                                limit=limit,
                            )
                            self.assertEqual(ids(result), expected)
                            self.assertEqual(result["count"], len(expected))

    def test_a_row_that_changed_after_the_first_read_is_decided_again(self):
        table = FakeTable([meeting("a", status="STARTED"), meeting("b", status="STARTED")])

        def a_ends(table):
            table.rows["c#a"] = dict(table.rows["c#a"], Status="ENDED")

        table.after_decision = a_ends

        self.assertEqual(ids(run(table, status="IN_PROGRESS")), ["b"])

    def test_a_row_gone_after_the_first_read_is_skipped_and_the_next_match_used(self):
        table = FakeTable([meeting(f"m{i}") for i in range(5)])

        def m0_goes(table):
            table.rows.pop("c#m0", None)

        table.after_decision = m0_goes

        result = run(table, limit=2)

        self.assertEqual(ids(result), ["m1", "m2"])
        self.assertNotIn("note", result)

    def test_a_whole_row_that_cannot_be_read_is_counted_and_noted(self):
        table = FakeTable([meeting("a"), meeting("b")])

        def b_breaks(table):
            table.failing_batches = 99
            table.unreadable = {"b"}

        table.after_decision = b_breaks
        result = run(table)

        self.assertEqual(ids(result), ["a"])
        self.assertEqual(result["note"], list_meetings.UNREADABLE_NOTE)

    def test_an_unreadable_row_newer_than_the_last_listed_meeting_is_noted(self):
        table = FakeTable([meeting("m0"), meeting("m1"), meeting("m2"), meeting("m3")])
        table.failing_batches = 99
        table.unreadable = {"m1"}

        result = run(table, limit=2)

        self.assertEqual(ids(result), ["m0", "m2"])
        self.assertEqual(result["note"], list_meetings.UNREADABLE_NOTE)

    def test_an_unreadable_row_older_than_a_full_list_is_not_noted(self):
        # It could not have been listed: the list was full before it.
        table = FakeTable([meeting("m0"), meeting("m1"), meeting("m2"), meeting("m3")])
        table.failing_batches = 99
        table.unreadable = {"m3"}

        result = run(table, limit=2)

        self.assertEqual(ids(result), ["m0", "m1"])
        self.assertNotIn("note", result)

    def test_the_single_read_fallback_keeps_the_projection(self):
        table = FakeTable([meeting("a")])
        table.failing_batches = 1

        run(table)

        projection = list_meetings.DECISION_PROJECTION["ProjectionExpression"]
        self.assertEqual(table.get_item_projections[0], projection)

    def test_an_admin_without_filters_reads_whole_rows_at_once(self):
        table = FakeTable([self.big(f"o{i}") for i in range(300)])

        run(table, user_id="admin", is_admin=True, limit=20)

        self.assertEqual(table.requests, 2)
        self.assertEqual(table.batch_projections, [None])


class TestTruncated(unittest.TestCase):
    def test_the_page_cap_with_rows_left_is_said(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(1500)])

        result = run(table, limit=5)

        self.assertIs(result["truncated"], True)
        self.assertEqual(result["note"], list_meetings.TRUNCATED_NOTE)
        probe = table.query_calls[-1]
        self.assertEqual(probe["Limit"], 1)
        self.assertEqual(probe["ExclusiveStartKey"], {"SK": "ts#000999"})

    def test_exactly_1000_rows_with_a_key_after_the_last_page_is_not_truncated(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(1000)])
        table.dynamodb_keys = True

        result = run(table, limit=5)

        self.assertEqual(set(result), {"meetings", "count", "filters"})

    def test_1001_rows_are_truncated(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(1001)])
        table.dynamodb_keys = True

        self.assertIs(run(table, limit=5)["truncated"], True)

    def test_a_failed_check_keeps_the_flag(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(1000)])
        table.dynamodb_keys = True
        table.failing_probe = True

        self.assertIs(run(table, limit=5)["truncated"], True)

    def test_a_limit_reached_on_the_last_page_is_not_truncated(self):
        rows = [meeting(f"o{i}", owner=OTHER) for i in range(999)] + [meeting("last")]
        table = FakeTable(rows + [meeting(f"x{i}", owner=OTHER) for i in range(50)])

        result = run(table, limit=1)

        self.assertEqual(ids(result), ["last"])
        self.assertNotIn("truncated", result)
        self.assertEqual(len(table.query_calls), 10)

    def test_both_notes_are_joined(self):
        table = FakeTable([meeting(f"o{i}", owner=OTHER) for i in range(1100)])
        table.failing_batches = 99
        table.unreadable = {"o3"}

        result = run(table, limit=5)

        self.assertIs(result["truncated"], True)
        self.assertEqual(
            result["note"], list_meetings.TRUNCATED_NOTE + " " + list_meetings.UNREADABLE_NOTE
        )

    def test_the_note_is_one_sentence(self):
        self.assertEqual(list_meetings.TRUNCATED_NOTE.count("."), 1)
        self.assertTrue(list_meetings.TRUNCATED_NOTE.endswith("."))

    def test_a_short_walk_has_no_flag_and_makes_no_extra_query(self):
        table = FakeTable([meeting(f"m{i}") for i in range(150)])

        result = run(table, limit=500)

        self.assertNotIn("truncated", result)
        self.assertEqual(len(table.query_calls), 2)


class TestFailureLogs(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(list_meetings.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_failed_read_logs_the_class_and_not_the_message(self):
        table = FakeTable([meeting("a")])
        detail = "arn:aws:dynamodb:us-east-1:123456789012:table/calls"

        def failing(RequestItems):
            raise LookupError(detail)

        table.meta.client.batch_get_item = failing
        table.unreadable = {"a"}
        original = table.get_item

        def get_item(Key, **request):
            try:
                return original(Key, **request)
            except RuntimeError:
                raise KeyError(detail) from None

        table.get_item = get_item
        with self.assertLogs(level=logging.WARNING) as logs:
            list_meetings.read_meetings(table, ["a"])

        text = "\n".join(logs.output)
        self.assertNotIn("123456789012", text)
        self.assertIn("LookupError", text)
        self.assertIn("KeyError", text)

    def test_an_aws_error_logs_its_code(self):
        error = RuntimeError("table calls in 123456789012")
        error.response = {"Error": {"Code": "ProvisionedThroughputExceededException"}}

        described = list_meetings.failure_class(error)

        self.assertEqual(described, "RuntimeError: ProvisionedThroughputExceededException")

    def test_the_single_meeting_reader_is_gone(self):
        self.assertFalse(hasattr(list_meetings, "get_meeting_by_id"))


class TestFailedIndexPage(unittest.TestCase):
    """An index page that cannot be read ends the walk, and the answer says so."""

    def failing_after(self, table, pages):
        original = table.query

        def query(**kwargs):
            if len(table.query_calls) >= pages:
                table.query_calls.append(kwargs)
                raise RuntimeError("arn:aws:dynamodb:us-east-1:123456789012:table/calls")
            return original(**kwargs)

        table.query = query

    def test_a_failed_page_of_a_filtered_walk_gives_the_note(self):
        table = FakeTable([meeting(f"m{i}") for i in range(250)])
        self.failing_after(table, 1)

        result = run(table, limit=150)

        self.assertEqual(len(result["meetings"]), 100)
        self.assertEqual(result["note"], list_meetings.SEARCH_FAILED_NOTE)
        self.assertNotIn("truncated", result)

    def test_a_failed_page_of_an_unfiltered_walk_gives_the_note(self):
        rows = [meeting(f"o{i}", owner=OTHER) for i in range(30)]
        table = FakeTable(rows)
        # Ten index rows whose meetings are gone leave the first page short.
        table.index = [{"CallId": f"gone{i}", "SK": f"ts#{i:06d}"} for i in range(10)] + [
            {"CallId": row["CallId"], "SK": f"ts#{i + 10:06d}"} for i, row in enumerate(rows)
        ]
        self.failing_after(table, 1)

        result = run(table, user_id="admin", is_admin=True, limit=25)

        self.assertEqual(ids(result), [f"o{i}" for i in range(15)])
        self.assertEqual(result["note"], list_meetings.SEARCH_FAILED_NOTE)

    def test_a_failed_first_page_gives_an_empty_list_and_the_note(self):
        table = FakeTable([meeting("a")])
        self.failing_after(table, 0)

        result = run(table)

        self.assertEqual(result["meetings"], [])
        self.assertEqual(result["note"], list_meetings.SEARCH_FAILED_NOTE)

    def test_the_note_is_joined_with_the_unreadable_note(self):
        table = FakeTable([meeting(f"m{i}") for i in range(150)])
        table.failing_batches = 99
        table.unreadable = {"m1"}
        self.failing_after(table, 1)

        result = run(table, limit=150)

        self.assertEqual(
            result["note"],
            list_meetings.UNREADABLE_NOTE + " " + list_meetings.SEARCH_FAILED_NOTE,
        )

    def test_the_note_is_one_sentence(self):
        note = list_meetings.SEARCH_FAILED_NOTE
        self.assertEqual(note, "The search could not be completed; try again.")

    def test_a_walk_whose_pages_were_all_read_has_no_note(self):
        table = FakeTable([meeting(f"m{i}") for i in range(250)])

        self.assertNotIn("note", run(table, limit=150))

    def test_the_log_names_the_failure_class_only(self):
        table = FakeTable([meeting("a")])
        self.failing_after(table, 0)

        with self.assertLogs(level=logging.WARNING) as logs:
            run(table)

        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("123456789012", text)


class TestUnfilteredUnreadableNote(unittest.TestCase):
    """A walk without a filter notes an unreadable row only if it could have been listed."""

    def setUp(self):
        patcher = patch.object(list_meetings.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def table(self, unreadable):
        # An index row whose meeting is gone comes first, so the second page's
        # batch holds rows beyond the third meeting.
        rows = [meeting(f"c{i:03d}", owner=OTHER) for i in range(10)]
        table = FakeTable(rows)
        table.index = [{"CallId": "gone", "SK": "ts#000000"}] + [
            {"CallId": row["CallId"], "SK": f"ts#{i + 1:06d}"} for i, row in enumerate(rows)
        ]
        table.failing_batches = 99
        table.unreadable = set(unreadable)
        return table

    def test_an_unreadable_row_beyond_the_answer_is_not_noted(self):
        result = run(self.table({"c003"}), user_id="admin", is_admin=True, limit=3)

        self.assertEqual(ids(result), ["c000", "c001", "c002"])
        self.assertNotIn("note", result)

    def test_an_unreadable_row_within_the_answer_is_noted(self):
        result = run(self.table({"c001"}), user_id="admin", is_admin=True, limit=3)

        self.assertEqual(ids(result), ["c000", "c002", "c003"])
        self.assertEqual(result["note"], list_meetings.UNREADABLE_NOTE)

    def test_an_unreadable_row_of_a_short_list_is_noted(self):
        result = run(self.table({"c009"}), user_id="admin", is_admin=True, limit=50)

        self.assertEqual(len(result["meetings"]), 9)
        self.assertEqual(result["note"], list_meetings.UNREADABLE_NOTE)


class TestStatusThatIsNotAString(unittest.TestCase):
    """A row whose Status is a list or a map never matches and never fails the call."""

    def rows(self):
        return [
            meeting("listed", status="ENDED"),
            meeting("as-list", status=["ENDED"]),
            meeting("as-map", status={"S": "ENDED"}),
            meeting("as-number", status=5),
        ]

    def test_such_a_row_does_not_match_a_status_filter(self):
        for status in ("ENDED", "IN_PROGRESS", "STARTED"):
            with self.subTest(status=status):
                result = run(FakeTable(self.rows()), status=status)
                expected = ["listed"] if status == "ENDED" else []
                self.assertEqual(ids(result), expected)

    def test_an_admin_status_filter_skips_it_too(self):
        result = run(FakeTable(self.rows()), user_id="admin", is_admin=True, status="ended")

        self.assertEqual(ids(result), ["listed"])

    def test_without_a_status_filter_it_is_listed_as_before(self):
        result = run(FakeTable(self.rows()))

        self.assertEqual(ids(result), ["listed", "as-list", "as-map", "as-number"])


class TestTruncatedNoteWording(unittest.TestCase):
    def test_the_note_names_no_number_of_meetings(self):
        # An unfiltered walk with a small limit checks fewer than 1,000 rows.
        self.assertNotRegex(list_meetings.TRUNCATED_NOTE, r"\d")

    def test_an_unfiltered_walk_with_a_small_limit_gives_the_same_note(self):
        rows = [meeting(f"o{i}", owner=OTHER) for i in range(80)]
        table = FakeTable(rows)
        table.index = [{"CallId": f"gone{i}", "SK": f"ts#{i:06d}"} for i in range(50)] + [
            {"CallId": row["CallId"], "SK": f"ts#{i + 50:06d}"} for i, row in enumerate(rows)
        ]

        result = run(table, user_id="admin", is_admin=True, limit=5)

        self.assertIs(result["truncated"], True)
        self.assertEqual(result["note"], list_meetings.TRUNCATED_NOTE)
        self.assertEqual(sum(call["Limit"] for call in table.query_calls[:-1]), 50)


class TestLimitArgument(unittest.TestCase):
    """The tool schema says ``number``, so a client may send 20.0 or "20"."""

    def table(self, count=30):
        return FakeTable([meeting(f"m{i}") for i in range(count)])

    def test_a_whole_float_is_a_limit(self):
        result = run(self.table(), limit=5.0)

        self.assertEqual((len(result["meetings"]), result["count"]), (5, 5))

    def test_the_float_of_the_default_is_a_limit(self):
        self.assertEqual(run(self.table(), limit=20.0)["count"], 20)

    def test_a_numeric_string_is_a_limit(self):
        for text in ("5", " 5 ", "5.0"):
            with self.subTest(text=text):
                self.assertEqual(run(self.table(), limit=text)["count"], 5)

    def test_no_limit_means_the_default(self):
        self.assertEqual(run(self.table(), limit=None)["count"], 20)

    def test_a_missing_limit_means_the_default(self):
        table = self.table()
        resource = type("Resource", (), {"Table": lambda self, name: table})()

        with patch.object(list_meetings.boto3, "resource", return_value=resource):
            result = list_meetings.execute(user_id=ME, is_admin=False)

        self.assertEqual(result["count"], 20)

    def test_anything_that_is_not_a_whole_number_is_refused(self):
        for value in (True, False, 2.5, float("nan"), float("inf"), "abc", "", "2.5", [], {}, (3,)):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    run(self.table(), limit=value)

                self.assertEqual(str(caught.exception), "limit must be an integer")

    def test_a_refusal_does_not_read_the_table(self):
        table = self.table()

        with self.assertRaises(ValueError):
            run(table, limit=True)

        self.assertEqual(table.requests, 0)

    def test_a_limit_below_one_is_read_as_one(self):
        for value in (0, -3, 0.0, "0"):
            with self.subTest(value=value):
                self.assertEqual(run(self.table(), limit=value)["count"], 1)

    def test_a_limit_above_the_most_a_walk_can_read_is_read_as_that_most(self):
        seen = []
        real = list_meetings.walk_date_range

        def spy(table, start, end, limit, include=None):
            seen.append(limit)
            return real(table, start, end, limit, include)

        with patch.object(list_meetings, "walk_date_range", spy):
            for value in (1000, 1001, 10**9, 5e6, "99999"):
                run(self.table(3), limit=value)

        self.assertEqual(seen, [1000] * 5)
        self.assertEqual(list_meetings.MAX_LIMIT, list_meetings.MAX_PAGES * 100)

    def test_the_walk_gets_a_whole_number(self):
        seen = []
        real = list_meetings.walk_date_range

        def spy(table, start, end, limit, include=None):
            seen.append(limit)
            return real(table, start, end, limit, include)

        with patch.object(list_meetings, "walk_date_range", spy):
            run(self.table(), limit=7.0)
            run(self.table(), limit="7")

        self.assertEqual(seen, [7, 7])
        self.assertTrue(all(type(value) is int for value in seen))

    def test_an_unfiltered_walk_with_a_float_limit_reads_the_same_rows(self):
        result = run(self.table(), user_id="admin", is_admin=True, limit=4.0)

        self.assertEqual(ids(result), ["m0", "m1", "m2", "m3"])


class TestWalkResult(unittest.TestCase):
    def test_the_result_has_names(self):
        found = list_meetings.walk_date_range(
            FakeTable([meeting("a")]), "2026-11-01", "2026-11-18", 5, lambda m: True
        )

        self.assertEqual(
            found._fields, ("rows", "unreadable", "truncated", "failed"), "named fields"
        )
        self.assertEqual([row["CallId"] for row in found.rows], ["a"])
        self.assertEqual((found.unreadable, found.truncated, found.failed), (0, False, False))

    def test_it_still_unpacks_as_four_values(self):
        rows, unreadable, truncated, failed = list_meetings.walk_date_range(
            FakeTable([meeting("a")]), "2026-11-01", "2026-11-18", 5
        )

        self.assertEqual((len(rows), unreadable, truncated, failed), (1, 0, False, False))

    def test_a_failed_page_is_named_failed(self):
        table = FakeTable([meeting("a")])
        table.query = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("down"))

        found = list_meetings.walk_date_range(table, "2026-11-01", "2026-11-18", 5, lambda m: True)

        self.assertIs(found.failed, True)
        self.assertEqual(found.rows, [])

    def test_the_wrapper_still_returns_a_list(self):
        rows = list_meetings.query_by_date_range(
            FakeTable([meeting("a")]), "2026-11-01", "2026-11-18", 5
        )

        self.assertIs(type(rows), list)


if __name__ == "__main__":
    unittest.main()
