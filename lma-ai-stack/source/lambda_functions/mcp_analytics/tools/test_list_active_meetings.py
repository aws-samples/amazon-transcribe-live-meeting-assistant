# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""list_active_meetings: running meetings of the last 24 hours that the caller may read.

The table is a stand-in that behaves like the TypeDateIndex: it honours the sort
key bounds, returns rows newest first, pages with ``Limit``, and (as DynamoDB
does) returns a ``LastEvaluatedKey`` after a page that is full even when no row
follows it. Every request made to it is recorded.
"""

import json
import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("CALLS_TABLE", "test-calls-table")
os.environ["LMA_WEB_APP_URL"] = "https://lma.example.invalid"

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from tools import list_active_meetings, list_meetings  # noqa: E402

NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)
ME = "me@example.com"
OTHER = "other@example.com"
FRIEND = "friend@example.com"


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def meeting(call_id, owner=ME, status="STARTED", age_minutes=10, **extra):
    created = stamp(NOW - timedelta(minutes=age_minutes))
    row = {
        "PK": f"c#{call_id}",
        "SK": f"c#{call_id}",
        "CallId": call_id,
        "Status": status,
        "MeetingTopic": f"topic {call_id}",
        "CreatedAt": created,
    }
    if owner is not None:
        row["Owner"] = owner
    row.update(extra)
    return row


def project(row, request):
    """The attributes of ``row`` a ProjectionExpression names, as DynamoDB returns them.

    A nested path (``#consent.#mode``) keeps only that field of the map; a path
    into a value that is not a map, or into a missing one, returns nothing.
    """
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
                if not isinstance(source, dict):
                    break
                target = target.setdefault(part, {})
    return projected


def size_of(row):
    return len(json.dumps(row, default=str))


class FakeClient:
    def __init__(self, table):
        self.table = table

    def batch_get_item(self, RequestItems):
        table = self.table
        keys = RequestItems[table.name]["Keys"]
        table.batch_calls.append([key["PK"] for key in keys])
        failure = table.batch_failure(keys)
        if failure:
            raise failure
        held = 0
        if len(table.batch_calls) <= table.unprocessed_calls:
            held = min(table.unprocessed_count, len(keys))
        served, left = keys[: len(keys) - held], keys[len(keys) - held :]
        request = RequestItems[table.name]
        table.batch_projections.append(request.get("ProjectionExpression"))
        found = [project(table.rows[k["PK"]], request) for k in served if k["PK"] in table.rows]
        table.bytes_read += sum(size_of(item) for item in found)
        response = {"Responses": {table.name: found}}
        if left:
            response["UnprocessedKeys"] = {table.name: {"Keys": left}}
        return response


class FakeTable:
    name = "calls"

    def __init__(self, meetings, uploads=(), index_overrides=None):
        self.rows = {m["PK"]: m for m in meetings}
        for call_id in uploads:
            self.rows[f"uj#{call_id}"] = {
                "PK": f"uj#{call_id}",
                "SK": f"uj#{call_id}",
                "RecordType": "UploadJob",
            }
        self.index = []
        for m in meetings:
            entry = {
                "ItemType": "call",
                "SK": f"ts#{m['CreatedAt']}#id#{m['CallId']}",
                "CallId": m["CallId"],
                "CreatedAt": m["CreatedAt"],
            }
            for field in ("Owner", "SharedWith", "UpdatedAt"):
                if field in m:
                    entry[field] = m[field]
            entry.update((index_overrides or {}).get(m["CallId"], {}))
            self.index.append(entry)
        self.index.sort(key=lambda e: e["SK"], reverse=True)
        self.query_calls = []
        self.batch_calls = []
        self.batch_projections = []
        self.bytes_read = 0
        self.get_item_calls = []
        self.batch_failure = lambda keys: None
        self.unprocessed_calls = 0
        self.unprocessed_count = 0
        self.query_failure = None
        self.probe_failure = None
        self.get_item_failures = set()
        self.meta = type("Meta", (), {"client": FakeClient(self)})()

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        if self.query_failure and "ExclusiveStartKey" not in kwargs:
            raise self.query_failure
        if self.probe_failure and kwargs["Limit"] == 1:
            raise self.probe_failure
        lo, hi = kwargs["KeyConditionExpression"]._values[1]._values[1:]
        rows = [e for e in self.index if lo <= e["SK"] <= hi]
        if "ExclusiveStartKey" in kwargs:
            after = kwargs["ExclusiveStartKey"]["SK"]
            rows = [e for e in rows if e["SK"] < after]
        page = rows[: kwargs["Limit"]]
        response = {"Items": page}
        if len(page) == kwargs["Limit"]:
            response["LastEvaluatedKey"] = {"SK": page[-1]["SK"]}
        return response

    def get_item(self, Key, **kwargs):
        self.get_item_calls.append(Key["PK"])
        if Key["PK"] in self.get_item_failures:
            raise RuntimeError("read failed")
        row = self.rows.get(Key["PK"])
        return {"Item": project(row, kwargs)} if row else {}

    def meeting_reads(self):
        """The meeting rows asked for, by batch or one by one."""
        asked = [key for batch in self.batch_calls for key in batch if key.startswith("c#")]
        return asked + [key for key in self.get_item_calls if key.startswith("c#")]

    def upload_lookups(self):
        asked = [key for batch in self.batch_calls for key in batch if key.startswith("uj#")]
        return asked + [key for key in self.get_item_calls if key.startswith("uj#")]


def run(table, user_id=ME, is_admin=False, limit=None):
    resource = type("Resource", (), {"Table": lambda self, name: table})()
    with patch.object(list_active_meetings.boto3, "resource", return_value=resource):
        return list_active_meetings.execute(
            limit=limit, user_id=user_id, is_admin=is_admin, now=NOW
        )


def ids(result):
    return [m["meetingId"] for m in result["meetings"]]


class SleepFree(unittest.TestCase):
    def setUp(self):
        for module in (list_active_meetings, list_meetings):
            patcher = patch.object(module.time, "sleep")
            patcher.start()
            self.addCleanup(patcher.stop)


class TestWhichMeetingsAreListed(SleepFree):
    def test_started_and_transcribing_meetings_are_listed_newest_first(self):
        table = FakeTable(
            [
                meeting("older", status="TRANSCRIBING", age_minutes=30),
                meeting("newer", status="STARTED", age_minutes=5),
            ]
        )

        self.assertEqual(ids(run(table)), ["newer", "older"])

    def test_meetings_that_are_not_running_are_not_listed(self):
        table = FakeTable(
            [
                meeting("ended", status="ENDED", age_minutes=1),
                meeting("errored", status="ERRORED", age_minutes=2),
                meeting("unknown", status="SOMETHING", age_minutes=3),
                meeting("no-status", status=None, age_minutes=4),
                meeting("live", status="STARTED", age_minutes=5),
            ]
        )

        self.assertEqual(ids(run(table)), ["live"])

    def test_a_status_that_is_not_a_string_is_not_running_and_does_not_fail_the_call(self):
        table = FakeTable(
            [
                meeting("as-list", status=["STARTED"], age_minutes=1),
                meeting("as-map", status={"S": "STARTED"}, age_minutes=2),
                meeting("as-number", status=5, age_minutes=3),
                meeting("live", status="STARTED", age_minutes=4),
            ]
        )
        for user, admin in ((ME, False), ("admin", True)):
            with self.subTest(admin=admin):
                result = run(table, user_id=user, is_admin=admin)
                self.assertEqual(ids(result), ["live"])
                self.assertNotIn("note", result)

    def test_a_meeting_started_more_than_24_hours_ago_is_not_listed(self):
        table = FakeTable(
            [
                meeting("inside", age_minutes=23 * 60 + 59),
                meeting("outside", age_minutes=24 * 60 + 5),
            ]
        )

        self.assertEqual(ids(run(table)), ["inside"])

    def test_an_empty_table_gives_an_empty_list(self):
        self.assertEqual(run(FakeTable([])), {"meetings": [], "count": 0})

    def test_count_is_the_number_of_meetings(self):
        table = FakeTable([meeting(f"m{i}", age_minutes=i + 1) for i in range(4)])

        result = run(table)

        self.assertEqual(result["count"], 4)
        self.assertEqual(len(result["meetings"]), 4)

    def test_a_meeting_the_index_lists_twice_is_listed_once(self):
        table = FakeTable([meeting("a")])
        table.index.append(dict(table.index[0], SK=table.index[0]["SK"] + "2"))

        self.assertEqual(ids(run(table)), ["a"])

    def test_the_answer_has_no_truncated_flag_or_note_when_nothing_is_missing(self):
        result = run(FakeTable([meeting("a")]))

        self.assertEqual(set(result), {"meetings", "count"})


class TestUploadedRecordings(SleepFree):
    def test_an_uploaded_recording_being_processed_is_not_listed(self):
        table = FakeTable(
            [
                meeting("upload", status="TRANSCRIBING", age_minutes=1),
                meeting("live", status="TRANSCRIBING", age_minutes=2),
            ],
            uploads=["upload"],
        )

        self.assertEqual(ids(run(table)), ["live"])

    def test_only_running_meetings_are_looked_up(self):
        table = FakeTable(
            [
                meeting("ended", status="ENDED", age_minutes=1),
                meeting("theirs", owner=OTHER, age_minutes=2),
                meeting("live", age_minutes=3),
            ]
        )

        run(table)

        self.assertEqual(table.upload_lookups(), ["uj#live"])

    def test_the_lookups_of_a_page_go_out_in_one_batch(self):
        table = FakeTable([meeting(f"m{i}", age_minutes=i + 1) for i in range(30)])

        run(table, limit=30)

        batches = [b for b in table.batch_calls if b[0].startswith("uj#")]
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 30)
        self.assertEqual(table.get_item_calls, [])

    def test_an_upload_lookup_that_fails_keeps_the_meeting_listed(self):
        table = FakeTable([meeting("live")], uploads=["live"])
        table.batch_failure = lambda keys: (
            RuntimeError("down") if keys[0]["PK"][:2] == "uj" else None
        )
        table.get_item_failures = {"uj#live"}

        self.assertEqual(ids(run(table)), ["live"])

    def test_a_failed_upload_batch_is_answered_one_by_one(self):
        table = FakeTable(
            [meeting("upload", status="TRANSCRIBING"), meeting("live", age_minutes=20)],
            uploads=["upload"],
        )
        table.batch_failure = lambda keys: (
            RuntimeError("down") if keys[0]["PK"][:2] == "uj" else None
        )

        self.assertEqual(ids(run(table)), ["live"])
        self.assertEqual(sorted(table.get_item_calls), ["uj#live", "uj#upload"])

    def test_upload_keys_left_unprocessed_are_asked_for_again(self):
        table = FakeTable(
            [meeting("upload", status="TRANSCRIBING"), meeting("live", age_minutes=20)],
            uploads=["upload"],
        )
        original = table.meta.client.batch_get_item
        state = {"first": True}

        def holding_back(RequestItems):
            keys = RequestItems[table.name]["Keys"]
            if keys[0]["PK"].startswith("uj#") and state.pop("first", False):
                table.batch_calls.append([k["PK"] for k in keys])
                return {
                    "Responses": {table.name: []},
                    "UnprocessedKeys": {table.name: {"Keys": keys}},
                }
            return original(RequestItems)

        table.meta.client.batch_get_item = holding_back

        self.assertEqual(ids(run(table)), ["live"])

    def test_upload_keys_that_stay_unprocessed_are_read_one_by_one(self):
        table = FakeTable([meeting("upload", status="TRANSCRIBING")], uploads=["upload"])
        original = table.meta.client.batch_get_item

        def never_processed(RequestItems):
            keys = RequestItems[table.name]["Keys"]
            if keys[0]["PK"].startswith("uj#"):
                table.batch_calls.append([k["PK"] for k in keys])
                return {
                    "Responses": {table.name: []},
                    "UnprocessedKeys": {table.name: {"Keys": keys}},
                }
            return original(RequestItems)

        table.meta.client.batch_get_item = never_processed

        self.assertEqual(ids(run(table)), [])
        self.assertEqual(table.get_item_calls, ["uj#upload"])
        self.assertEqual(
            len([b for b in table.batch_calls if b[0].startswith("uj#")]),
            list_active_meetings.LOOKUP_ATTEMPTS,
        )


class TestAccess(SleepFree):
    def test_the_owner_sees_their_meeting_and_nobody_elses(self):
        table = FakeTable([meeting("mine"), meeting("theirs", owner=OTHER)])

        self.assertEqual(ids(run(table)), ["mine"])

    def test_a_meeting_shared_with_the_caller_is_listed_in_each_shape(self):
        shapes = {
            "list": [FRIEND, OTHER],
            "string": f"{OTHER},{FRIEND}",
            "bracket": f"[{OTHER}, {FRIEND}]",
        }
        for name, shared in shapes.items():
            with self.subTest(shape=name):
                table = FakeTable([meeting("shared", owner=OTHER, SharedWith=shared)])

                self.assertEqual(ids(run(table, user_id=FRIEND)), ["shared"])

    def test_a_meeting_not_shared_with_the_caller_is_not_listed(self):
        table = FakeTable(
            [
                meeting("a", owner=OTHER, SharedWith=[OTHER]),
                meeting("b", owner=OTHER, SharedWith=f"x{FRIEND}"),
                meeting("c", owner=OTHER, SharedWith=[f"{OTHER},{FRIEND}"]),
            ]
        )

        self.assertEqual(ids(run(table, user_id=FRIEND)), [])

    def test_an_older_row_with_an_agent_id_and_no_owner_is_listed_for_that_agent(self):
        table = FakeTable([meeting("old", owner=None, AgentId=ME)])

        self.assertEqual(ids(run(table)), ["old"])
        self.assertEqual(run(table)["meetings"][0]["owner"], ME)

    def test_a_row_without_any_owner_is_not_listed_for_a_user(self):
        table = FakeTable([meeting("orphan", owner=None)])

        self.assertEqual(ids(run(table)), [])

    def test_a_row_shared_with_the_caller_but_with_no_owner_is_decided_on_the_meeting_row(self):
        table = FakeTable(
            [
                meeting("shared", owner=None, SharedWith=[FRIEND], age_minutes=1),
                meeting("closed", owner=None, SharedWith=[OTHER], age_minutes=2),
            ]
        )

        self.assertEqual(ids(run(table, user_id=FRIEND)), ["shared"])
        self.assertIn("c#closed", table.meeting_reads())

    def test_the_meeting_row_decides_when_the_index_row_is_out_of_date(self):
        table = FakeTable(
            [meeting("a", owner=ME, SharedWith=[ME])],
            index_overrides={"a": {"Owner": ME}},
        )
        table.rows["c#a"]["Owner"] = OTHER
        table.rows["c#a"]["SharedWith"] = [OTHER]

        self.assertEqual(ids(run(table)), [])

    def test_an_admin_sees_every_running_meeting(self):
        table = FakeTable(
            [
                meeting("theirs", owner=OTHER, age_minutes=1),
                meeting("orphan", owner=None, age_minutes=2),
                meeting("mine", age_minutes=3),
                meeting("ended", owner=OTHER, status="ENDED", age_minutes=4),
            ]
        )

        self.assertEqual(
            ids(run(table, user_id="admin", is_admin=True)), ["theirs", "orphan", "mine"]
        )

    def test_an_admin_without_a_user_id_sees_every_running_meeting(self):
        table = FakeTable([meeting("theirs", owner=OTHER)])

        self.assertEqual(ids(run(table, user_id=None, is_admin=True)), ["theirs"])

    def test_a_caller_without_an_identity_gets_nothing_and_nothing_is_read(self):
        table = FakeTable([meeting("mine"), meeting("orphan", owner=None)])

        for user_id in (None, ""):
            with self.subTest(user_id=user_id):
                self.assertEqual(run(table, user_id=user_id), {"meetings": [], "count": 0})
        self.assertEqual(table.query_calls, [])

    def test_the_meetings_of_other_people_do_not_use_up_the_limit(self):
        newer = [meeting(f"o{i}", owner=OTHER, age_minutes=i + 1) for i in range(30)]
        table = FakeTable(newer + [meeting("mine", age_minutes=60)])

        self.assertEqual(ids(run(table, limit=1)), ["mine"])

    def test_meetings_that_are_over_do_not_use_up_the_limit(self):
        newer = [meeting(f"e{i}", status="ENDED", age_minutes=i + 1) for i in range(60)]
        table = FakeTable(newer + [meeting("live", age_minutes=120)])

        self.assertEqual(ids(run(table, limit=1)), ["live"])

    def test_the_meeting_of_somebody_else_is_read_only_in_part(self):
        table = FakeTable(
            [meeting("theirs", owner=OTHER, Summary="x" * 4000), meeting("mine", age_minutes=20)]
        )

        self.assertEqual(ids(run(table)), ["mine"])
        meeting_batches = [
            projection
            for batch, projection in zip(table.batch_calls, table.batch_projections)
            if batch[0].startswith("c#")
        ]
        self.assertEqual(
            meeting_batches, [list_active_meetings.MEETING_PROJECTION["ProjectionExpression"]]
        )
        self.assertLess(table.bytes_read, 1000)

    def test_a_row_owned_by_someone_else_with_the_callers_agent_id_is_listed(self):
        # list_meetings lists this row for the caller, so this tool does too.
        table = FakeTable([meeting("both", owner=OTHER, AgentId=ME)])

        self.assertEqual(ids(run(table)), ["both"])
        self.assertEqual(ids(run(table, user_id=OTHER)), ["both"])
        self.assertEqual(ids(run(table, user_id=FRIEND)), [])

    def test_a_meeting_the_index_lists_for_someone_else_but_that_is_shared_is_read(self):
        table = FakeTable([meeting("shared", owner=OTHER, SharedWith=FRIEND)])

        self.assertEqual(ids(run(table, user_id=FRIEND)), ["shared"])


class TestLimit(SleepFree):
    def setUp(self):
        super().setUp()
        self.table = FakeTable([meeting(f"m{i}", age_minutes=i + 1) for i in range(80)])

    def count(self, limit):
        return run(self.table, limit=limit)["count"]

    def test_the_default_is_10(self):
        self.assertEqual(self.count(None), 10)
        self.assertEqual(list_active_meetings.DEFAULT_LIMIT, 10)

    def test_a_limit_is_applied(self):
        self.assertEqual(self.count(3), 3)
        self.assertEqual(self.count(50), 50)

    def test_a_limit_outside_1_to_50_is_moved_to_the_nearest_end(self):
        for value, expected in (
            (0, 1),
            (-5, 1),
            (51, 50),
            (1000, 50),
            (10**400, 50),
            (-(10**400), 1),
        ):
            with self.subTest(limit=value):
                self.assertEqual(self.count(value), expected)

    def test_numbers_in_other_forms_are_accepted(self):
        for value, expected in (
            ("7", 7),
            (" 7 ", 7),
            ("+7", 7),
            (7.0, 7),
            ("7.0", 7),
            ("1e1", 10),
            (2.0e1, 20),
        ):
            with self.subTest(limit=value):
                self.assertEqual(self.count(value), expected)

    def test_values_that_are_not_an_integer_are_refused(self):
        bad = [
            True,
            False,
            "abc",
            "",
            "  ",
            "1_0",
            "0x10",
            "٣",
            "ten",
            "7 8",
            [],
            [3],
            {},
            {"limit": 3},
            2.5,
            "2.5",
            math.nan,
            math.inf,
            -math.inf,
            1e999,
            "1e999",
            "nan",
            "inf",
            "-inf",
            "9" * 400 + ".5",
            object(),
        ]
        for value in bad:
            with self.subTest(limit=value):
                with self.assertRaises(ValueError) as caught:
                    run(self.table, limit=value)
                self.assertEqual(str(caught.exception), "limit must be an integer")

    def test_a_refused_limit_reads_nothing(self):
        with self.assertRaises(ValueError):
            run(self.table, limit="abc")

        self.assertEqual(self.table.query_calls, [])

    def test_a_refused_limit_is_reported_before_the_identity_is_considered(self):
        with self.assertRaises(ValueError):
            run(self.table, user_id=None, limit=True)

    def test_a_digit_string_too_long_to_be_a_number_is_refused_not_a_crash(self):
        with self.assertRaises(ValueError) as caught:
            run(self.table, limit="9" * 5000)

        self.assertEqual(str(caught.exception), "limit must be an integer")

    def test_a_long_whole_number_is_moved_to_the_top_of_the_range(self):
        self.assertEqual(self.count("9" * 300), 50)

    def test_parse_limit_directly(self):
        parse = list_active_meetings.parse_limit
        self.assertEqual(parse(None), 10)
        self.assertEqual(parse(5), 5)
        self.assertEqual(parse(0), 1)
        self.assertEqual(parse(99), 50)


class TestPagingAndTheCap(SleepFree):
    def rows(self, count, **kwargs):
        return [meeting(f"m{i:04d}", age_minutes=1 + i // 10, **kwargs) for i in range(count)]

    def test_the_walk_stops_at_the_limit(self):
        table = FakeTable(self.rows(500))

        self.assertEqual(run(table, limit=5)["count"], 5)
        self.assertEqual(len(table.query_calls), 1)

    def test_the_index_is_read_in_pages_of_100_newest_first(self):
        table = FakeTable(self.rows(250, status="ENDED"))

        run(table)

        self.assertEqual({call["Limit"] for call in table.query_calls[:3]}, {100})
        self.assertIs(table.query_calls[0]["ScanIndexForward"], False)
        self.assertEqual(table.query_calls[0]["IndexName"], "TypeDateIndex")

    def test_a_running_meeting_on_a_later_page_is_found(self):
        rows = self.rows(250, status="ENDED") + [meeting("deep", age_minutes=200)]
        table = FakeTable(rows)

        result = run(table)

        self.assertEqual(ids(result), ["deep"])
        self.assertNotIn("truncated", result)

    def test_at_most_ten_pages_are_read(self):
        table = FakeTable(self.rows(1500, status="ENDED"))

        run(table)

        self.assertEqual(len([c for c in table.query_calls if c["Limit"] == 100]), 10)

    def test_a_cap_that_ends_the_walk_with_rows_left_sets_truncated_and_a_note(self):
        rows = self.rows(1001, status="ENDED")
        table = FakeTable(rows)

        result = run(table)

        self.assertIs(result["truncated"], True)
        self.assertIn("last 24 hours", result["note"])
        self.assertEqual(result["meetings"], [])

    def test_a_cap_that_falls_exactly_at_the_end_of_the_rows_is_not_truncated(self):
        # DynamoDB returns a LastEvaluatedKey after a full last page, though nothing follows.
        table = FakeTable(self.rows(1000, status="ENDED"))

        result = run(table)

        self.assertTrue(table.query_calls[9].get("ExclusiveStartKey"))
        self.assertEqual(result, {"meetings": [], "count": 0})
        probe = table.query_calls[-1]
        self.assertEqual(probe["Limit"], 1)
        self.assertIn("ExclusiveStartKey", probe)

    def test_the_check_for_rows_left_reads_one_row_after_the_last_key(self):
        table = FakeTable(self.rows(1200, status="ENDED"))

        run(table)

        self.assertEqual(len(table.query_calls), 11)
        last_page, probe = table.query_calls[9], table.query_calls[10]
        self.assertEqual(probe["Limit"], 1)
        self.assertEqual(probe["KeyConditionExpression"], last_page["KeyConditionExpression"])
        self.assertEqual(probe["IndexName"], "TypeDateIndex")

    def test_a_check_that_fails_keeps_the_flag_on(self):
        table = FakeTable(self.rows(1000, status="ENDED"))
        table.probe_failure = RuntimeError("throttled")

        result = run(table)

        self.assertIs(result["truncated"], True)
        self.assertIn("note", result)

    def test_a_walk_that_ends_before_the_cap_is_not_truncated(self):
        table = FakeTable(self.rows(450, status="ENDED"))

        self.assertNotIn("truncated", run(table))
        self.assertEqual(len(table.query_calls), 5)

    def test_a_list_that_reaches_its_limit_is_not_truncated(self):
        rows = self.rows(1200, status="ENDED")
        rows[900:905] = [meeting(f"live{i}", age_minutes=91) for i in range(5)]
        table = FakeTable(rows)

        result = run(table, limit=2)

        self.assertEqual(result["count"], 2)
        self.assertNotIn("truncated", result)

    def test_a_cap_with_some_meetings_found_is_still_truncated(self):
        rows = self.rows(1200, status="ENDED")
        rows[5] = meeting("early", age_minutes=1)
        table = FakeTable(rows)

        result = run(table)

        self.assertEqual(ids(result), ["early"])
        self.assertIs(result["truncated"], True)


class TestTheWindow(SleepFree):
    def bounds(self, table):
        return table.query_calls[0]["KeyConditionExpression"]._values[1]._values[1:]

    def test_the_upper_bound_ends_in_z_and_a_tilde(self):
        table = FakeTable([])

        run(table)

        lo, hi = self.bounds(table)
        self.assertTrue(hi.endswith("Z~"), hi)
        self.assertEqual(hi, "ts#2026-11-19T12:00:00.000Z~")

    def test_the_bounds_span_24_hours(self):
        table = FakeTable([])

        run(table)

        lo, hi = self.bounds(table)
        self.assertEqual(lo, "ts#2026-11-18T12:00:00.000Z")
        start = datetime.strptime(lo[3:], "%Y-%m-%dT%H:%M:%S.%fZ")
        end = datetime.strptime(hi[3:-1], "%Y-%m-%dT%H:%M:%S.%fZ")
        self.assertEqual(end - start, timedelta(hours=24))

    def test_a_real_sort_key_falls_inside_the_bounds(self):
        table = FakeTable([])
        run(table)
        lo, hi = self.bounds(table)

        sk = f"ts#{stamp(NOW)}#id#abc-123"

        self.assertLessEqual(lo, sk)
        self.assertLessEqual(sk, hi)

    def test_the_bounds_come_from_the_given_time_in_utc(self):
        table = FakeTable([])
        local = NOW.astimezone(timezone(timedelta(hours=4)))

        resource = type("Resource", (), {"Table": lambda self, name: table})()
        with patch.object(list_active_meetings.boto3, "resource", return_value=resource):
            list_active_meetings.execute(user_id=ME, now=local)

        self.assertEqual(self.bounds(table)[1], "ts#2026-11-19T12:00:00.000Z~")

    def test_without_a_given_time_the_clock_is_used(self):
        table = FakeTable([])
        resource = type("Resource", (), {"Table": lambda self, name: table})()
        with patch.object(list_active_meetings.boto3, "resource", return_value=resource):
            list_active_meetings.execute(user_id=ME)

        hi = datetime.strptime(self.bounds(table)[1][3:-1], "%Y-%m-%dT%H:%M:%S.%fZ")
        self.assertLess(
            abs(datetime.now(timezone.utc).replace(tzinfo=None) - hi), timedelta(minutes=1)
        )


class TestUnreadableMeetings(SleepFree):
    def test_a_meeting_that_cannot_be_read_gives_a_note(self):
        table = FakeTable([meeting("a", age_minutes=1), meeting("b", age_minutes=2)])
        table.batch_failure = lambda keys: (
            RuntimeError("down") if keys[0]["PK"][:2] == "c#" else None
        )
        table.get_item_failures = {"c#a"}

        result = run(table)

        self.assertEqual(ids(result), ["b"])
        self.assertEqual(result["note"], list_meetings.UNREADABLE_NOTE)
        self.assertNotIn("truncated", result)

    def test_a_failed_batch_is_read_one_by_one_without_a_note(self):
        table = FakeTable([meeting("a", age_minutes=1), meeting("b", age_minutes=2)])
        table.batch_failure = lambda keys: (
            RuntimeError("down") if keys[0]["PK"][:2] == "c#" else None
        )

        result = run(table)

        self.assertEqual(ids(result), ["a", "b"])
        self.assertNotIn("note", result)

    def test_a_meeting_that_is_gone_is_skipped_without_a_note(self):
        table = FakeTable([meeting("a", age_minutes=1), meeting("b", age_minutes=2)])
        del table.rows["c#a"]

        result = run(table)

        self.assertEqual(ids(result), ["b"])
        self.assertNotIn("note", result)

    def test_both_notes_are_given_when_both_apply(self):
        rows = [meeting(f"e{i}", status="ENDED", age_minutes=1 + i // 10) for i in range(1100)]
        rows.append(meeting("a", age_minutes=0))
        table = FakeTable(rows)
        table.batch_failure = lambda keys: (
            RuntimeError("down") if keys[0]["PK"][:2] == "c#" else None
        )
        table.get_item_failures = {"c#e3"}

        result = run(table)

        self.assertIs(result["truncated"], True)
        self.assertIn(list_meetings.UNREADABLE_NOTE, result["note"])
        self.assertIn("last 24 hours", result["note"])

    def test_the_unreadable_rows_come_from_the_shared_reader(self):
        table = FakeTable([meeting("a")])
        reader = patch.object(list_active_meetings, "read_meeting_rows", return_value=({}, ["a"]))
        with reader as read:
            result = run(table)

        read.assert_called()
        self.assertEqual(result["note"], list_meetings.UNREADABLE_NOTE)


class TestFailures(SleepFree):
    def test_a_failed_query_is_reported_with_a_fixed_message(self):
        table = FakeTable([meeting("a")])
        secret = "arn:aws:dynamodb:us-east-1:123456789012:table/secret-name"
        table.query_failure = RuntimeError(f"not authorized to perform Query on {secret}")

        with self.assertLogs(level="ERROR") as logged:
            with self.assertRaises(ValueError) as caught:
                run(table)

        self.assertEqual(str(caught.exception), "Failed to list active meetings")
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        # The log names the class of the failure, not its text, which can name
        # the table and the account.
        self.assertIn("RuntimeError", "\n".join(logged.output))
        self.assertNotIn("123456789012", "\n".join(logged.output))
        self.assertNotIn("secret-name", "\n".join(logged.output))

    def test_a_failure_while_reading_a_later_page_is_reported_the_same_way(self):
        table = FakeTable([meeting(f"e{i}", status="ENDED") for i in range(150)])
        original = table.query

        def failing(**kwargs):
            if "ExclusiveStartKey" in kwargs:
                raise RuntimeError("arn:aws:dynamodb:us-east-1:123456789012:table/x")
            return original(**kwargs)

        table.query = failing

        with self.assertRaises(ValueError) as caught:
            run(table)

        self.assertEqual(str(caught.exception), "Failed to list active meetings")

    def test_a_failure_to_open_the_table_is_reported_the_same_way(self):
        with patch.object(list_active_meetings.boto3, "resource", side_effect=RuntimeError("x")):
            with self.assertRaises(ValueError) as caught:
                list_active_meetings.execute(user_id=ME, now=NOW)

        self.assertEqual(str(caught.exception), "Failed to list active meetings")


class TestTheFieldsOfAMeeting(SleepFree):
    def fields(self, **extra):
        table = FakeTable([meeting("call-1", UpdatedAt="2026-11-19T11:59:00.000Z", **extra)])
        return run(table)["meetings"][0]

    def test_the_fields_are_exactly_these(self):
        entry = self.fields(
            Consent={"Mode": "notify", "Status": "NOTIFIED", "CaptureAllowed": True}
        )

        self.assertEqual(
            set(entry),
            {
                "meetingId",
                "meetingName",
                "status",
                "startedAt",
                "lastActivityAt",
                "owner",
                "consent",
                "meetingUrl",
                "virtualParticipantUrl",
            },
        )

    def test_the_values_come_from_the_meeting_row(self):
        entry = self.fields()

        self.assertEqual(entry["meetingId"], "call-1")
        self.assertEqual(entry["meetingName"], "topic call-1")
        self.assertEqual(entry["status"], "STARTED")
        self.assertEqual(entry["startedAt"], stamp(NOW - timedelta(minutes=10)))
        self.assertEqual(entry["lastActivityAt"], "2026-11-19T11:59:00.000Z")
        self.assertEqual(entry["owner"], ME)

    def test_the_last_activity_is_the_start_when_the_row_was_never_updated(self):
        table = FakeTable([meeting("call-1")])

        entry = run(table)["meetings"][0]

        self.assertEqual(entry["lastActivityAt"], entry["startedAt"])

    def test_the_consent_record_is_reported_as_mode_status_and_capture_allowed(self):
        entry = self.fields(
            Consent={"Mode": "opt_in", "Status": "PENDING", "CaptureAllowed": False, "Extra": "x"}
        )

        self.assertEqual(
            entry["consent"], {"mode": "opt_in", "status": "PENDING", "captureAllowed": False}
        )

    def test_a_meeting_without_a_consent_record_has_consent_null(self):
        for consent in ({}, None, "text", 7):
            with self.subTest(consent=consent):
                extra = {} if consent is None else {"Consent": consent}
                self.assertIsNone(self.fields(**extra)["consent"])

    def test_a_consent_record_with_none_of_the_three_fields_gives_consent_null(self):
        # The projected read returns no Consent for such a record.
        entry = self.fields(Consent={"Evidence": ["x"], "Version": 2})

        self.assertIsNone(entry["consent"])

    def test_the_links_point_into_the_web_app(self):
        entry = self.fields()

        self.assertEqual(entry["meetingUrl"], "https://lma.example.invalid/#/calls/call-1")
        self.assertEqual(
            entry["virtualParticipantUrl"],
            "https://lma.example.invalid/#/virtual-participant/call-1",
        )

    def test_no_transcript_or_summary_text_is_returned(self):
        entry = self.fields(Summary="the summary", Transcript="what was said", Participants=["Ann"])

        text = repr(entry)
        self.assertNotIn("the summary", text)
        self.assertNotIn("what was said", text)
        self.assertNotIn("Ann", text)


class TestProjectedReads(SleepFree):
    """Meeting rows are read once, projected to the fields the tool needs."""

    def test_the_projection_names_only_what_access_status_and_the_answer_need(self):
        request = list_active_meetings.MEETING_PROJECTION
        names = request["ExpressionAttributeNames"]
        paths = {
            ".".join(names[part] for part in path.strip().split("."))
            for path in request["ProjectionExpression"].split(",")
        }
        self.assertEqual(
            paths,
            {
                "PK",
                "CallId",
                "Owner",
                "AgentId",
                "SharedWith",
                "Status",
                "MeetingTopic",
                "CreatedAt",
                "UpdatedAt",
                "Consent.Mode",
                "Consent.Status",
                "Consent.CaptureAllowed",
            },
        )

    def test_the_consent_evidence_is_never_read(self):
        consent = {
            "Mode": "opt_in",
            "Status": "CONSENTED",
            "CaptureAllowed": True,
            "Evidence": [{"Text": "y" * 5000}],
        }
        table = FakeTable([meeting("a", Consent=consent, Summary="z" * 5000)])

        result = run(table)

        self.assertEqual(
            result["meetings"][0]["consent"],
            {"mode": "opt_in", "status": "CONSENTED", "captureAllowed": True},
        )
        self.assertLess(table.bytes_read, 1000)

    def test_every_meeting_read_is_projected_and_none_is_read_whole(self):
        rows = [
            meeting(f"m{i}", owner=OTHER if i % 2 else ME, age_minutes=i + 1) for i in range(60)
        ]
        table = FakeTable(rows)

        run(table, limit=50)
        run(table, user_id="admin", is_admin=True, limit=50)

        projection = list_active_meetings.MEETING_PROJECTION["ProjectionExpression"]
        meeting_batches = [
            p for b, p in zip(table.batch_calls, table.batch_projections) if b[0].startswith("c#")
        ]
        self.assertEqual(set(meeting_batches), {projection})

    def test_one_request_decides_a_page(self):
        rows = [meeting(f"e{i}", status="ENDED", age_minutes=i + 1) for i in range(250)]
        table = FakeTable(rows)

        run(table, user_id="admin", is_admin=True)

        sizes = [len(b) for b in table.batch_calls if b[0].startswith("c#")]
        self.assertEqual(sizes, [100, 100, 50])

    def test_an_admin_walk_over_1000_rows_takes_a_few_requests(self):
        rows = [
            meeting(f"o{i}", owner=OTHER, status="ENDED", age_minutes=i + 1) for i in range(999)
        ]
        table = FakeTable(rows + [meeting("live", owner=OTHER, age_minutes=1400)])

        result = run(table, user_id="admin", is_admin=True, limit=20)

        self.assertEqual(ids(result), ["live"])
        meeting_batches = [b for b in table.batch_calls if b[0].startswith("c#")]
        self.assertEqual(len(meeting_batches), 10)
        self.assertEqual(len(table.get_item_calls), 0)

    def test_an_unreadable_row_newer_than_the_last_listed_meeting_is_noted(self):
        table = FakeTable([meeting(f"m{i}", age_minutes=i + 1) for i in range(4)])
        table.batch_failure = lambda keys: (
            RuntimeError("down") if keys[0]["PK"][:2] == "c#" else None
        )
        table.get_item_failures = {"c#m1"}

        result = run(table, limit=2)

        self.assertEqual(ids(result), ["m0", "m2"])
        self.assertEqual(result["note"], list_meetings.UNREADABLE_NOTE)

    def test_an_unreadable_row_older_than_a_full_list_is_not_noted(self):
        table = FakeTable([meeting(f"m{i}", age_minutes=i + 1) for i in range(4)])
        table.batch_failure = lambda keys: (
            RuntimeError("down") if keys[0]["PK"][:2] == "c#" else None
        )
        table.get_item_failures = {"c#m3"}

        result = run(table, limit=2)

        self.assertEqual(ids(result), ["m0", "m1"])
        self.assertNotIn("note", result)

    def test_a_failed_upload_lookup_logs_the_class_and_not_the_text(self):
        table = FakeTable([meeting("live")])
        detail = "arn:aws:dynamodb:us-east-1:123456789012:table/calls"
        table.batch_failure = lambda keys: (
            LookupError(detail) if keys[0]["PK"][:2] == "uj" else None
        )
        table.get_item_failures = {"uj#live"}

        with self.assertLogs(level="WARNING") as logged:
            result = run(table)

        self.assertEqual(ids(result), ["live"])
        self.assertNotIn("note", result)
        text = "\n".join(logged.output)
        self.assertIn("LookupError", text)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("123456789012", text)


if __name__ == "__main__":
    unittest.main()
