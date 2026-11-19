# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""get_meeting_frames: which frames are chosen, what is read, and how big the answer gets.

The table stand-in behaves like DynamoDB where it matters here: a query returns the
rows of one partition in sort key order, in pages, with a continuation key after any
full page (even the last); a projection returns only the attributes it names, and
placeholders must all be used and defined; BatchGetItem can leave keys unprocessed.
It records every read, so a test can say which texts were never fetched.
"""

import json
import math
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ["LOG_LEVEL"] = "WARNING"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["CALLS_TABLE"] = "test-calls-table"

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))
# The configuration layer the deployed function carries, for lma_consent.
LAYER = HERE.parents[2] / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

from tools import consent_gate, get_meeting_frames  # noqa: E402

OWNER = "owner@example.com"
FRIEND = "friend@example.com"
OTHER = "other@example.com"
SECRET_ARN = "arn:aws:dynamodb:us-east-1:123456789012:table/secret-calls-table"
CALL = "call-1"
DENIED = {"Mode": "opt_in", "Rule": "each_participant"}
ALLOWED = {"Mode": "off"}


def meeting(call_id=CALL, **fields):
    row = {"PK": f"c#{call_id}", "SK": f"c#{call_id}", "Owner": OWNER, "Consent": ALLOWED}
    row.update(fields)
    return row


def frame(seconds, call_id=CALL, kind="SCREEN", title=None, text=None, **extra):
    """A key frame row as the call event processor and the text reader store it."""
    millis = int(round(seconds * 1000))
    row = {
        "PK": f"kf#{call_id}",
        "SK": f"f#{millis:012d}-a",
        "CallId": call_id,
        "FrameId": f"{millis:012d}-a",
        "Timestamp": Decimal(str(seconds)),
        "ImageUrl": f"https://bucket.s3.amazonaws.com/keyframes/{millis}.jpg",
        "ThumbnailUrl": f"https://bucket.s3.amazonaws.com/keyframes/{millis}-t.jpg",
        "Width": 1280,
        "Height": 720,
        "Owner": OWNER,
        "CreatedAt": "2026-11-19T10:00:00Z",
        "ChangeScore": Decimal("0.4"),
    }
    if kind is not None:
        row["ContentKind"] = kind
    if title is not None:
        row["Title"] = title
    row["SlideText"] = f"text of {millis}" if text is None else text
    row.update(extra)
    return row


def frame_id(seconds):
    return f"{int(round(seconds * 1000)):012d}-a"


def project(item, expression, names):
    """The item cut to a projection, as DynamoDB does, and as strictly."""
    parts = [part.strip() for part in expression.split(",")]
    used = {part for part in parts if part.startswith("#")}
    if used != set(names or {}):
        raise AssertionError(f"placeholders used {used} but defined {set(names or {})}")
    wanted = [(names or {}).get(part, part) for part in parts]
    return {name: item[name] for name in wanted if name in item}


class FakeClient:
    def __init__(self, table):
        self.table = table

    def batch_get_item(self, RequestItems):
        table = self.table
        table.batches.append(RequestItems)
        if table.batch_error:
            raise table.batch_error
        assert list(RequestItems) == [table.name]
        request = RequestItems[table.name]
        keys = request["Keys"]
        assert 1 <= len(keys) <= 100
        leave = table.unprocessed_plan.pop(0) if table.unprocessed_plan else 0
        served, left = keys[: len(keys) - leave], keys[len(keys) - leave :]
        found = []
        for key in served:
            row = table.rows.get((key["PK"], key["SK"]))
            if row:
                table.text_reads.append(key["SK"])
                found.append(
                    project(
                        row, request["ProjectionExpression"], request["ExpressionAttributeNames"]
                    )
                )
        response = {"Responses": {table.name: found}}
        if left:
            response["UnprocessedKeys"] = {table.name: {**request, "Keys": left}}
        return response


class FakeTable:
    name = "test-calls-table"

    def __init__(self, *rows, unprocessed_plan=(), batch_error=None, query_error=None):
        self.rows = {(row["PK"], row["SK"]): row for row in rows}
        self.unprocessed_plan = list(unprocessed_plan)
        self.batch_error = batch_error
        self.query_error = query_error
        self.queries = []
        self.gets = []
        self.batches = []
        self.text_reads = []
        self.fail_probe = False
        self.meta = SimpleNamespace(client=FakeClient(self))

    def get_item(self, **kwargs):
        self.gets.append(kwargs)
        row = self.rows.get((kwargs["Key"]["PK"], kwargs["Key"]["SK"]))
        if not row:
            return {}
        return {
            "Item": project(row, kwargs["ProjectionExpression"], kwargs["ExpressionAttributeNames"])
        }

    def query(self, **kwargs):
        self.queries.append(kwargs)
        if self.query_error:
            raise self.query_error
        if self.fail_probe and kwargs["Limit"] == 1:
            raise RuntimeError(SECRET_ARN)
        partition = kwargs["KeyConditionExpression"]._values[1]
        rows = sorted(
            (row for (pk, _), row in self.rows.items() if pk == partition),
            key=lambda row: row["SK"],
        )
        start = kwargs.get("ExclusiveStartKey")
        if start:
            assert start["PK"] == partition
            rows = [row for row in rows if row["SK"] > start["SK"]]
        page = rows[: kwargs["Limit"]]
        response = {
            "Items": [
                project(r, kwargs["ProjectionExpression"], kwargs["ExpressionAttributeNames"])
                for r in page
            ]
        }
        if len(page) == kwargs["Limit"]:
            # DynamoDB returns a key after any page that is full, the last one included.
            response["LastEvaluatedKey"] = {"PK": partition, "SK": page[-1]["SK"]}
        return response


def run(table, call_id=CALL, user_id=OWNER, is_admin=False, **arguments):
    """execute() on a table stand-in; the arguments are the tool's own names."""
    names = {"fromSeconds": "from_seconds", "toSeconds": "to_seconds"}
    arguments = {names.get(key, key): value for key, value in arguments.items()}
    resource = MagicMock()
    resource.Table.return_value = table
    with (
        patch("boto3.resource", return_value=resource),
        patch("tools.get_meeting_frames.time.sleep"),
    ):
        return get_meeting_frames.execute(call_id, user_id=user_id, is_admin=is_admin, **arguments)


def table_of(*frames, **kwargs):
    return FakeTable(meeting(), *frames, **kwargs)


def ids(result):
    return [f["frameId"] for f in result["frames"]]


def slides(*seconds, **kwargs):
    return [frame(s, **kwargs) for s in seconds]


class SelectionTest(unittest.TestCase):
    def test_the_last_three_frames_are_returned_oldest_first_by_default(self):
        table = table_of(*slides(10, 20, 30, 40, 50))

        result = run(table)

        self.assertEqual(ids(result), [frame_id(30), frame_id(40), frame_id(50)])
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["totalFrames"], 5)
        self.assertNotIn("note", result)

    def test_each_frame_is_on_screen_until_the_next_one_replaces_it(self):
        table = table_of(*slides(65, 125.5, 3725))

        result = run(table, last=3)

        first, second, third = result["frames"]
        self.assertEqual((first["fromSeconds"], first["toSeconds"]), (65.0, 125.5))
        self.assertEqual((first["from"], first["to"]), ("01:05", "02:05"))
        self.assertEqual((second["from"], second["to"]), ("02:05", "1:02:05"))
        self.assertEqual((third["from"], third["toSeconds"], third["to"]), ("1:02:05", None, None))

    def test_last_picks_how_many(self):
        table = table_of(*slides(*range(10, 310, 10)))

        self.assertEqual(run(table, last=1)["count"], 1)
        self.assertEqual(run(table, last=7)["count"], 7)
        self.assertEqual(run(table, last=20)["count"], 20)
        self.assertEqual(run(table, last=3.0)["count"], 3)

    def test_last_outside_one_to_twenty_is_moved_to_the_nearest_end(self):
        table = table_of(*slides(*range(10, 310, 10)))

        for value, expected in ((0, 1), (-5, 1), (21, 20), (500, 20), (10**400, 20), (1e300, 20)):
            with self.subTest(last=value):
                self.assertEqual(run(table, last=value)["count"], expected)

    def test_a_last_that_is_not_a_whole_number_is_refused(self):
        table = table_of(*slides(10, 20))
        for value in (True, False, "3", "", 2.5, math.nan, math.inf, -math.inf, [3], {"n": 3}):
            with self.subTest(last=value):
                with self.assertRaises(ValueError) as caught:
                    run(table, last=value)

                self.assertEqual(str(caught.exception), "last must be a whole number from 1 to 20")
        self.assertEqual(table.gets, [])

    def test_a_meeting_with_fewer_frames_than_asked_returns_them_all(self):
        result = run(table_of(*slides(10, 20)), last=20)

        self.assertEqual(result["count"], 2)

    def test_camera_frames_are_left_out_after_the_spans_are_worked_out(self):
        table = table_of(
            frame(10), frame(20, kind="CAMERA"), frame(30), frame(40, kind="MIXED"), frame(50)
        )

        result = run(table, last=10)

        self.assertEqual(ids(result), [frame_id(10), frame_id(30), frame_id(40), frame_id(50)])
        # The slide at 10 stayed up until the camera replaced it, not until 30.
        self.assertEqual(result["frames"][0]["toSeconds"], 20.0)
        self.assertEqual(result["totalFrames"], 4)

    def test_a_frame_whose_text_has_not_been_read_still_counts_as_shared_content(self):
        table = table_of(frame(10, kind=None))

        self.assertEqual(run(table)["count"], 1)

    def test_a_meeting_with_only_camera_frames_has_none_to_show(self):
        result = run(table_of(frame(10, kind="CAMERA"), frame(20, kind="CAMERA")))

        self.assertEqual((result["count"], result["totalFrames"], result["frames"]), (0, 0, []))
        self.assertIn("no key frames", result["note"])

    def test_a_meeting_without_frames_says_so(self):
        table = table_of()

        result = run(table)

        self.assertEqual(
            (result["count"], result["totalFrames"], result["frames"], result["textRead"]),
            (0, 0, [], 0),
        )
        self.assertIn("no key frames", result["note"])
        self.assertEqual(table.batches, [])

    def test_rows_without_a_usable_time_are_skipped(self):
        no_time = frame(2)
        del no_time["Timestamp"]
        broken = [
            frame(1, Timestamp="soon"),
            no_time,
            frame(3, Timestamp=True),
            frame(4, Timestamp=[4]),
        ]
        table = table_of(*broken, frame(30), frame(40))

        result = run(table, last=10)

        self.assertEqual(ids(result), [frame_id(30), frame_id(40)])
        self.assertEqual(result["totalFrames"], 2)

    def test_rows_without_a_sort_key_are_skipped(self):
        odd = frame(5)
        table = table_of(frame(30))
        original_query = table.query

        def query(**kwargs):
            response = original_query(**kwargs)
            response["Items"].append({k: v for k, v in odd.items() if k != "SK"})
            return response

        table.query = query

        self.assertEqual(ids(run(table)), [frame_id(30)])

    def test_a_frame_id_missing_from_the_row_is_taken_from_the_sort_key(self):
        row = frame(30)
        del row["FrameId"]

        result = run(table_of(row))

        self.assertEqual(result["frames"][0]["frameId"], frame_id(30))


class RangeTest(unittest.TestCase):
    def table(self):
        return table_of(*slides(10, 60, 120, 300, 600))

    def test_frames_on_screen_at_any_point_of_the_range_are_returned(self):
        result = run(self.table(), fromSeconds=100, toSeconds=310)

        # 60-120 is still up at 100; 300-600 is up at 310.
        self.assertEqual(ids(result), [frame_id(60), frame_id(120), frame_id(300)])

    def test_a_frame_that_left_the_screen_exactly_at_the_start_is_not_in_the_range(self):
        result = run(self.table(), fromSeconds=120, toSeconds=130)

        self.assertEqual(ids(result), [frame_id(120)])

    def test_a_frame_that_went_up_exactly_at_the_end_is_in_the_range(self):
        result = run(self.table(), fromSeconds=0, toSeconds=60)

        self.assertEqual(ids(result), [frame_id(10), frame_id(60)])

    def test_a_start_alone_means_until_the_end(self):
        result = run(self.table(), fromSeconds=250)

        self.assertEqual(ids(result), [frame_id(120), frame_id(300), frame_id(600)])

    def test_an_end_alone_means_from_the_start(self):
        result = run(self.table(), toSeconds=70)

        self.assertEqual(ids(result), [frame_id(10), frame_id(60)])

    def test_a_range_of_one_moment(self):
        result = run(self.table(), fromSeconds=150, toSeconds=150)

        self.assertEqual(ids(result), [frame_id(120)])

    def test_last_is_ignored_when_there_is_a_range(self):
        result = run(self.table(), last=1, fromSeconds=0, toSeconds=1000)

        self.assertEqual(result["count"], 5)

    def test_a_bad_last_is_still_refused_when_there_is_a_range(self):
        with self.assertRaises(ValueError):
            run(self.table(), last="many", fromSeconds=0)

    def test_a_range_keeps_the_latest_twenty_and_says_that_frames_were_cut(self):
        table = table_of(*slides(*range(10, 510, 10)))  # 50 frames

        result = run(table, fromSeconds=0, toSeconds=10_000)

        self.assertEqual(result["count"], 20)
        self.assertEqual(result["frames"][0]["frameId"], frame_id(310))
        self.assertEqual(result["frames"][-1]["frameId"], frame_id(500))
        self.assertIn("50 frames were on screen in that range; the latest 20", result["note"])

    def test_exactly_twenty_frames_in_a_range_are_not_a_cut(self):
        table = table_of(*slides(*range(10, 210, 10)))  # 20 frames

        result = run(table, fromSeconds=0, toSeconds=10_000)

        self.assertEqual(result["count"], 20)
        self.assertNotIn("note", result)

    def test_a_range_with_no_frame_in_it_names_the_time_the_first_one_went_up(self):
        table = table_of(*slides(500, 600))

        result = run(table, fromSeconds=10, toSeconds=100)

        self.assertEqual(result["count"], 0)
        self.assertEqual(result["totalFrames"], 2)
        self.assertIn(
            "None of the meeting's 2 key frames was on screen in that range", result["note"]
        )
        self.assertIn("the first went up at 08:20", result["note"])

    def test_a_range_before_the_first_frame_but_inside_no_slide_returns_none(self):
        table = table_of(*slides(500))

        self.assertEqual(run(table, toSeconds=100)["count"], 0)

    def test_the_seconds_are_validated(self):
        table = self.table()
        bad = (
            True,
            False,
            "5",
            "",
            math.nan,
            math.inf,
            -math.inf,
            -1,
            -0.001,
            10**400,
            -(10**400),
            [5],
            {"s": 5},
        )
        for name in ("fromSeconds", "toSeconds"):
            for value in bad:
                with self.subTest(name=name, value=repr(value)[:24]):
                    with self.assertRaises(ValueError) as caught:
                        run(table, **{name: value})

                    self.assertEqual(
                        str(caught.exception),
                        f"{name} must be a number of seconds, 0 or more, from the start of the meeting",
                    )
        self.assertEqual(table.gets, [])

    def test_a_400_digit_integer_is_the_tools_own_message_not_an_overflow(self):
        with self.assertRaises(ValueError) as caught:
            run(self.table(), toSeconds=int("9" * 400))

        self.assertNotIsInstance(caught.exception, OverflowError)
        self.assertIn("toSeconds must be a number of seconds", str(caught.exception))

    def test_large_but_finite_seconds_and_zero_are_accepted(self):
        for value in (0, 0.0, -0.0, 10**300, 1.7e308, 1e-9):
            with self.subTest(value=value):
                run(self.table(), fromSeconds=0, toSeconds=value)

    def test_a_start_after_the_end_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            run(self.table(), fromSeconds=100, toSeconds=99.9)

        self.assertEqual(str(caught.exception), "fromSeconds must not be after toSeconds")

    def test_a_null_range_is_no_range(self):
        result = run(self.table(), fromSeconds=None, toSeconds=None)

        self.assertEqual(result["count"], 3)


class DecimalTest(unittest.TestCase):
    def test_numbers_read_from_dynamodb_arrive_as_floats(self):
        table = table_of(frame(12.5, text="a"), frame(70.25, text="b"))

        result = run(table, last=2)

        first = result["frames"][0]
        self.assertIsInstance(first["fromSeconds"], float)
        self.assertEqual((first["fromSeconds"], first["toSeconds"]), (12.5, 70.25))
        # The shared serializer has no way to write a Decimal.
        json.dumps(result)

    def test_times_are_rounded_to_a_thousandth(self):
        table = table_of(frame(1, Timestamp=Decimal("1.23456789")), frame(2))

        self.assertEqual(run(table, last=2)["frames"][0]["fromSeconds"], 1.235)

    def test_an_integer_valued_time_is_a_float(self):
        table = table_of(frame(10, Timestamp=Decimal("10")))

        self.assertIsInstance(run(table)["frames"][0]["fromSeconds"], float)

    def test_a_time_that_is_not_finite_is_skipped(self):
        table = table_of(
            frame(10, Timestamp=Decimal("NaN")), frame(20, Timestamp=Decimal("Infinity"))
        )

        self.assertEqual(run(table)["count"], 0)

    def test_a_huge_stored_time_is_handled(self):
        table = table_of(frame(10, Timestamp=Decimal("1E+300")), frame(20))

        result = run(table, last=2)

        self.assertEqual(result["count"], 2)
        json.dumps(result)


class TwoStepReadTest(unittest.TestCase):
    def test_step_one_reads_only_what_choosing_frames_needs(self):
        table = table_of(*slides(10, 20, 30))

        run(table)

        query = table.queries[0]
        names = query["ExpressionAttributeNames"]
        projected = {
            names.get(p.strip(), p.strip()) for p in query["ProjectionExpression"].split(",")
        }
        self.assertEqual(projected, {"SK", "FrameId", "Timestamp", "ContentKind"})
        self.assertEqual(query["KeyConditionExpression"]._values[1], f"kf#{CALL}")
        self.assertEqual(query["Limit"], 100)
        self.assertNotIn("ConsistentRead", query)

    def test_step_two_reads_the_text_of_the_chosen_frames_only(self):
        table = table_of(*slides(*range(10, 510, 10)))  # 50 frames

        result = run(table, last=3)

        chosen = [f"f#{frame_id(s)}" for s in (480, 490, 500)]
        self.assertEqual(table.text_reads, chosen)
        self.assertEqual(len(table.batches), 1)
        keys = table.batches[0][table.name]["Keys"]
        self.assertEqual(keys, [{"PK": f"kf#{CALL}", "SK": sk} for sk in chosen])
        self.assertEqual(result["count"], 3)

    def test_the_text_of_frames_outside_the_range_is_never_read(self):
        table = table_of(*slides(*range(10, 510, 10)))

        run(table, fromSeconds=100, toSeconds=125)

        self.assertEqual(
            table.text_reads,
            [f"f#{frame_id(s)}" for s in (100, 110, 120)],
        )

    def test_camera_frames_text_is_never_read(self):
        table = table_of(frame(10), frame(20, kind="CAMERA"), frame(30))

        run(table, last=5)

        self.assertEqual(table.text_reads, [f"f#{frame_id(10)}", f"f#{frame_id(30)}"])

    def test_no_text_is_read_when_no_frame_is_chosen(self):
        table = table_of(*slides(500, 600))

        run(table, fromSeconds=1, toSeconds=2)

        self.assertEqual(table.batches, [])

    def test_step_two_asks_for_the_title_and_text_and_never_the_pictures(self):
        table = table_of(*slides(10, 20))

        run(table)

        request = table.batches[0][table.name]
        names = request["ExpressionAttributeNames"]
        projected = {
            names.get(p.strip(), p.strip()) for p in request["ProjectionExpression"].split(",")
        }
        self.assertEqual(projected, {"SK", "Title", "SlideText"})

    def test_no_picture_link_or_unrelated_attribute_is_in_the_answer(self):
        table = table_of(frame(10, title="Budget", text="Q3"), frame(20, title="Plan", text="Q4"))

        result = run(table, last=2)

        written = json.dumps(result)
        for forbidden in ("ImageUrl", "ThumbnailUrl", "bucket", ".jpg", "https://", "ChangeScore"):
            self.assertNotIn(forbidden, written)
        self.assertEqual(
            set(result["frames"][0]),
            {
                "frameId",
                "fromSeconds",
                "toSeconds",
                "from",
                "to",
                "title",
                "text",
                "textTruncated",
            },
        )
        self.assertEqual(set(result), {"meetingId", "count", "totalFrames", "frames", "textRead"})

    def test_title_and_text_come_back_with_their_frame(self):
        table = table_of(
            frame(10, title="Budget", text="Q3 numbers"), frame(20, title="Plan", text="Q4")
        )

        result = run(table, last=2)

        self.assertEqual(
            [(f["title"], f["text"], f["textTruncated"]) for f in result["frames"]],
            [("Budget", "Q3 numbers", False), ("Plan", "Q4", False)],
        )
        self.assertEqual(result["textRead"], 2)

    def test_a_frame_whose_text_has_not_been_read_has_null_text(self):
        unread = frame(10)
        del unread["SlideText"]
        table = table_of(unread, frame(20, text=""), frame(30, text="words"))

        result = run(table, last=3)

        self.assertEqual([f["text"] for f in result["frames"]], [None, "", "words"])
        self.assertEqual(result["textRead"], 2)
        self.assertEqual(result["count"], 3)

    def test_a_text_that_is_not_a_string_is_not_text(self):
        table = table_of(frame(10, SlideText=Decimal("7")), frame(20, SlideText=["a"]))

        result = run(table, last=2)

        self.assertEqual([f["text"] for f in result["frames"]], [None, None])
        self.assertEqual(result["textRead"], 0)

    def test_a_title_that_is_missing_blank_or_not_text_is_null(self):
        table = table_of(
            frame(10),
            frame(20, title="   "),
            frame(30, title=Decimal("5")),
            frame(40, title=" Plan "),
        )

        result = run(table, last=4)

        self.assertEqual([f["title"] for f in result["frames"]], [None, None, None, "Plan"])

    def test_a_long_title_is_cut(self):
        result = run(table_of(frame(10, title="T" * 500)))

        self.assertEqual(len(result["frames"][0]["title"]), 200)

    def test_a_frame_deleted_between_the_steps_has_null_text(self):
        table = table_of(*slides(10, 20, 30))
        original = table.meta.client.batch_get_item

        def batch(RequestItems):
            table.rows.pop((f"kf#{CALL}", f"f#{frame_id(20)}"), None)
            return original(RequestItems)

        table.meta.client.batch_get_item = batch

        result = run(table)

        self.assertEqual([f["text"] is None for f in result["frames"]], [False, True, False])
        self.assertEqual(result["textRead"], 2)

    def test_keys_left_unprocessed_are_asked_for_again(self):
        table = table_of(*slides(10, 20, 30, 40), unprocessed_plan=[3, 1, 0])

        with patch("tools.get_meeting_frames.time.sleep") as sleep:
            with patch("boto3.resource") as factory:
                factory.return_value.Table.return_value = table
                result = get_meeting_frames.execute(CALL, last=4, user_id=OWNER)

        self.assertEqual(len(table.batches), 3)
        self.assertEqual([len(b[table.name]["Keys"]) for b in table.batches], [4, 3, 1])
        self.assertEqual(result["textRead"], 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.05, 0.1])
        # Each retry keeps the projection, so nothing else is read.
        for batch in table.batches:
            self.assertIn("ProjectionExpression", batch[table.name])

    def test_keys_still_unprocessed_after_four_requests_are_an_error(self):
        table = table_of(*slides(10, 20, 30), unprocessed_plan=[2, 2, 2, 2, 2])

        with patch("tools.get_meeting_frames.time.sleep") as sleep:
            with patch("boto3.resource") as factory:
                factory.return_value.Table.return_value = table
                with self.assertRaises(ValueError) as caught:
                    get_meeting_frames.execute(CALL, user_id=OWNER)

        self.assertEqual(str(caught.exception), "Some frames could not be read; try again")
        self.assertEqual(len(table.batches), 4)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.05, 0.1, 0.2])

    def test_a_failed_batch_read_is_a_fixed_message_without_the_cause(self):
        table = table_of(*slides(10, 20), batch_error=RuntimeError(SECRET_ARN))

        with self.assertLogs(level="ERROR") as logged:
            with self.assertRaises(ValueError) as caught:
                run(table)

        self.assertEqual(str(caught.exception), "Failed to read the meeting frames")
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn("arn:", "\n".join(logged.output) + repr(caught.exception))

    def test_a_failed_query_is_a_fixed_message_without_the_cause(self):
        table = table_of(*slides(10, 20), query_error=RuntimeError(SECRET_ARN))

        with self.assertLogs(level="ERROR") as logged:
            with self.assertRaises(ValueError) as caught:
                run(table)

        self.assertEqual(str(caught.exception), "Failed to read the meeting frames")
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("arn:", "\n".join(logged.output) + repr(caught.exception))
        self.assertEqual(table.batches, [])

    def test_a_failed_meeting_read_is_a_fixed_message_without_the_cause(self):
        table = table_of(*slides(10))
        table.get_item = MagicMock(side_effect=RuntimeError(SECRET_ARN))

        with self.assertRaises(ValueError) as caught:
            run(table)

        self.assertEqual(str(caught.exception), "Failed to read the meeting frames")
        self.assertNotIn("arn:", repr(caught.exception))
        self.assertEqual(table.queries, [])

    def test_a_table_that_cannot_be_opened_is_the_same_fixed_message(self):
        with patch("boto3.resource", side_effect=RuntimeError(SECRET_ARN)):
            with self.assertRaises(ValueError) as caught:
                get_meeting_frames.execute(CALL, user_id=OWNER)

        self.assertEqual(str(caught.exception), "Failed to read the meeting frames")

    def test_a_stuck_continuation_key_does_not_loop(self):
        table = table_of(*slides(10, 20))
        original = table.query

        def query(**kwargs):
            response = original(**kwargs)
            response["LastEvaluatedKey"] = {"PK": f"kf#{CALL}", "SK": "f#same"}
            return response

        table.query = query

        run(table)

        self.assertLessEqual(len(table.queries), 2)


class PageCapTest(unittest.TestCase):
    def many(self, count):
        return [frame(second, text="x") for second in range(1, count + 1)]

    def test_a_meeting_below_the_cap_is_read_to_its_end_with_no_note(self):
        table = table_of(*self.many(250))

        result = run(table, last=1)

        self.assertEqual(len(table.queries), 3)
        self.assertEqual(result["totalFrames"], 250)
        self.assertNotIn("note", result)

    def test_at_most_two_thousand_frames_are_read(self):
        table = table_of(*self.many(2500))

        result = run(table, last=1)

        self.assertEqual(len([q for q in table.queries if q["Limit"] == 100]), 20)
        self.assertEqual(result["totalFrames"], 2000)
        self.assertEqual(result["frames"][0]["frameId"], frame_id(2000))
        self.assertIn("The meeting has more than 2,000 key frames", result["note"])

    def test_the_note_names_the_time_after_which_frames_were_not_read(self):
        table = table_of(*self.many(2500))

        result = run(table, last=1)

        self.assertIn("frames after 33:20 were not read", result["note"])

    def test_a_probe_confirms_that_a_frame_is_really_left(self):
        table = table_of(*self.many(2500))

        run(table, last=1)

        probe = table.queries[-1]
        self.assertEqual(probe["Limit"], 1)
        self.assertEqual(probe["ExclusiveStartKey"]["SK"], f"f#{frame_id(2000)}")
        self.assertEqual(len(table.queries), 21)

    def test_exactly_two_thousand_frames_are_not_a_truncation(self):
        # DynamoDB returns a continuation key after the last full page: the probe says none is left.
        table = table_of(*self.many(2000))

        result = run(table, last=1)

        self.assertEqual(len(table.queries), 21)
        self.assertEqual(table.queries[-1]["Limit"], 1)
        self.assertEqual(result["totalFrames"], 2000)
        self.assertNotIn("note", result)

    def test_two_thousand_and_one_frames_are_a_truncation(self):
        result = run(table_of(*self.many(2001)), last=1)

        self.assertIn("frames after 33:20 were not read", result["note"])

    def test_a_probe_that_fails_keeps_the_note(self):
        table = table_of(*self.many(2000))
        table.fail_probe = True

        result = run(table, last=1)

        self.assertIn("The meeting has more than 2,000 key frames", result["note"])
        self.assertNotIn("arn:", result["note"])

    def test_a_range_past_the_cap_is_answered_from_what_was_read(self):
        table = table_of(*self.many(2500))

        result = run(table, fromSeconds=2400, toSeconds=2450)

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["frames"][0]["frameId"], frame_id(2000))
        self.assertIn("were not read", result["note"])

    def test_the_note_when_nothing_could_be_timed_still_names_a_time(self):
        rows = [frame(s, Timestamp="none") for s in range(1, 2100)]
        result = run(table_of(*rows))

        self.assertIn("frames after 00:00 were not read", result["note"])


def random_text(chooser, longest):
    """A text drawn from every class of character JSON writes differently."""
    classes = (
        lambda: chr(chooser.randint(0x20, 0x7E)),
        lambda: chooser.choice('"\\'),
        lambda: chr(chooser.randint(0, 0x1F)),
        lambda: "\x7f",
        lambda: chr(chooser.randint(0x80, 0x7FF)),
        lambda: chr(chooser.randint(0x800, 0xFFFF)),
        lambda: chr(chooser.randint(0xD800, 0xDFFF)),
        lambda: chr(chooser.randint(0x10000, 0x10FFFF)),
    )
    weights = [chooser.random() for _ in classes]
    picks = chooser.choices(classes, weights=weights, k=chooser.randint(0, longest))
    return "".join(pick() for pick in picks)


def escaped_size(text):
    """The bytes a text adds to the body: written as JSON by index.py, then again."""
    return len(json.dumps(json.dumps(text)).encode()) - len(json.dumps(json.dumps("")))


def ascii_text(n):
    return "a" * n


class BudgetUnitsTest(unittest.TestCase):
    def test_units_count_ascii_one_other_characters_three_and_beyond_the_first_plane_six(self):
        units = get_meeting_frames.units_of
        self.assertEqual(units("abc 123\n"), 8)
        self.assertEqual(units("é"), 3)
        self.assertEqual(units("Привет"), 18)
        self.assertEqual(units("中文"), 6)
        self.assertEqual(units("\uffff"), 3)
        self.assertEqual(units("\U00010000"), 6)
        self.assertEqual(units("😀"), 6)
        self.assertEqual(units(""), 0)

    def test_a_cut_never_exceeds_the_allowance_and_never_splits_a_character(self):
        cut = get_meeting_frames.cut_to_units
        self.assertEqual(cut("aé", 4), "aé")
        self.assertEqual(cut("aé", 3), "a")
        self.assertEqual(cut("a😀b", 6), "a")
        self.assertEqual(cut("a😀b", 7), "a😀")
        self.assertEqual(cut("abc", 0), "")
        self.assertEqual(cut("abc", 3), "abc")
        self.assertEqual(cut("", 5), "")

    def test_control_characters_other_than_line_breaks_and_tabs_become_spaces(self):
        plain = get_meeting_frames.plain_text
        self.assertEqual(plain("a\x00b\x1fc\x7fd"), "a b c d")
        self.assertEqual(plain("a\nb\tc"), "a\nb\tc")
        self.assertEqual(plain("é中😀"), "é中😀")


class EscapedSizeTest(unittest.TestCase):
    """No text costs more than 4 bytes in the body for each unit it is counted."""

    def test_units_bound_the_written_size_of_any_text(self):
        import random

        chooser = random.Random(7)
        units = get_meeting_frames.units_of
        for case in range(1000):
            text = random_text(chooser, 300)
            with self.subTest(case=case):
                self.assertLessEqual(escaped_size(text), 4 * units(text), repr(text[:40]))

    def test_each_kind_of_character_is_bounded(self):
        units = get_meeting_frames.units_of
        for char in ("a", '"', "\\", "\n", "\t", "\r", "\x00", "\x01", "\x1f", "\x7f"):
            with self.subTest(char=repr(char)):
                self.assertLessEqual(escaped_size(char * 50), 4 * units(char * 50))
        for char in ("\x80", "é", "\u2028", "中", "\ud800", "\udfff", "\uffff", "😀"):
            with self.subTest(char=repr(char)):
                self.assertLessEqual(escaped_size(char * 50), 4 * units(char * 50))

    def test_control_characters_and_del_cost_two_units(self):
        units = get_meeting_frames.units_of
        self.assertEqual(units("\x00\x01\x1f\x7f"), 8)
        self.assertEqual(units("\n\t"), 2)

    def test_a_cut_of_any_text_stays_within_its_allowance(self):
        import random

        chooser = random.Random(8)
        for case in range(1000):
            text = random_text(chooser, 300)
            allowed = chooser.randint(0, 600)
            with self.subTest(case=case):
                kept = get_meeting_frames.cut_to_units(text, allowed)
                self.assertTrue(text.startswith(kept))
                self.assertLessEqual(get_meeting_frames.units_of(kept), allowed)
                if len(kept) < len(text):
                    longer = text[: len(kept) + 1]
                    self.assertGreater(get_meeting_frames.units_of(longer), allowed)

    def test_fitted_texts_of_any_kind_stay_within_four_bytes_a_unit_of_the_budget(self):
        import random

        chooser = random.Random(9)
        for case in range(1000):
            longest = 2000 if case % 10 == 0 else 300
            texts = [random_text(chooser, longest) for _ in range(chooser.randint(1, 20))]
            budget = chooser.randint(0, 20_000 if case % 10 == 0 else 3000)
            with self.subTest(case=case):
                fitted = get_meeting_frames.fit_texts(texts, budget)
                kept = [text for text, _ in fitted]
                self.assertLessEqual(sum(map(get_meeting_frames.units_of, kept)), budget)
                self.assertLessEqual(sum(map(escaped_size, kept)), 4 * budget)


class FitTextsTest(unittest.TestCase):
    def fit(self, *texts):
        return get_meeting_frames.fit_texts(list(texts))

    def total(self, fitted):
        return sum(get_meeting_frames.units_of(t) for t, _ in fitted if t is not None)

    def test_texts_that_fit_are_not_touched(self):
        fitted = self.fit("a" * 100, "é" * 100, None, "")

        self.assertEqual(
            fitted, [("a" * 100, False), ("é" * 100, False), (None, False), ("", False)]
        )

    def test_a_text_of_exactly_the_budget_is_whole(self):
        self.assertEqual(self.fit("a" * 20_000), [("a" * 20_000, False)])

    def test_one_unit_over_the_budget_is_cut_and_flagged(self):
        fitted = self.fit("a" * 20_001)

        self.assertEqual(fitted, [("a" * 20_000, True)])

    def test_equal_long_texts_share_the_budget_equally(self):
        fitted = self.fit(*["a" * 5000] * 20)

        self.assertEqual([len(t) for t, _ in fitted], [1000] * 20)
        self.assertTrue(all(cut for _, cut in fitted))

    def test_short_texts_leave_their_room_to_long_ones(self):
        fitted = self.fit(*["a" * 100] * 19, "b" * 30_000)

        self.assertEqual([len(t) for t, _ in fitted[:19]], [100] * 19)
        self.assertFalse(any(cut for _, cut in fitted[:19]))
        self.assertEqual(len(fitted[19][0]), 20_000 - 1900)
        self.assertTrue(fitted[19][1])

    def test_the_order_of_the_texts_does_not_change_their_shares(self):
        texts = ["a" * 100, "b" * 30_000, "c" * 8_000, "d" * 50]
        forward = [len(t) for t, _ in self.fit(*texts)]
        backward = [len(t) for t, _ in reversed(self.fit(*reversed(texts)))]

        self.assertEqual(forward, backward)
        self.assertEqual(forward[0], 100)
        self.assertEqual(forward[3], 50)
        # What the two short texts leave is split between the two long ones.
        self.assertEqual(forward[2], 8000)
        self.assertEqual(forward[1], 20_000 - 100 - 50 - 8000)

    def test_three_unequal_long_texts_split_what_the_short_one_leaves(self):
        fitted = self.fit("a" * 2000, "b" * 30_000, "c" * 30_000)

        self.assertEqual(len(fitted[0][0]), 2000)
        self.assertEqual(len(fitted[1][0]), 9000)
        self.assertEqual(len(fitted[2][0]), 9000)

    def test_the_total_never_exceeds_the_budget(self):
        for sample in ("a", "é", "Я", "中", "😀"):
            for count in (1, 2, 3, 7, 20):
                with self.subTest(sample=sample, count=count):
                    fitted = self.fit(*[sample * 30_000] * count)

                    self.assertLessEqual(self.total(fitted), 20_000)
                    # And the room is used: no text is cut well short of its share.
                    self.assertGreater(self.total(fitted), 20_000 - 6 * count)

    def test_a_cut_text_is_the_start_of_the_original(self):
        original = "Slide: " + "中文é😀 " * 5000

        ((kept, cut),) = self.fit(original)

        self.assertTrue(cut)
        self.assertTrue(original.startswith(kept))

    def test_a_text_that_is_none_takes_no_room(self):
        fitted = self.fit(None, "a" * 30_000, None)

        self.assertEqual([t is None for t, _ in fitted], [True, False, True])
        self.assertEqual(len(fitted[1][0]), 20_000)

    def test_a_text_with_control_characters_costs_no_more_than_it_is_counted(self):
        fitted = self.fit("\x01" * 30_000)

        self.assertEqual(fitted[0][0], " " * 20_000)


class AnswerSizeTest(unittest.TestCase):
    SAMPLES = {
        "ascii": "Quarterly revenue grew while costs stayed flat. ",
        "accented": "Résumé: déjà vu, señor, über, naïve façade. ",
        "cyrillic": "Квартальная выручка выросла, расходы не изменились. ",
        "cjk": "本季度收入增长，成本保持不变。第三季度计划。",
        "emoji": "📈🚀✅🎯 ",
    }

    def answer_for(self, sample, frames=20, chars=6000):
        text = (sample * (chars // len(sample) + 1))[:chars]
        table = table_of(
            *[frame(10 * (n + 1), title=f"Slide {n}", text=text) for n in range(frames)]
        )
        return run(table, last=20)

    def written(self, result):
        # As index.execute_tool_call writes it.
        return json.dumps(result, indent=2, default=str)

    def test_twenty_frames_of_six_thousand_characters_stay_well_under_one_hundred_kilobytes(self):
        for name, sample in self.SAMPLES.items():
            with self.subTest(script=name):
                result = self.answer_for(sample)

                size = len(self.written(result).encode())
                self.assertLess(size, 100_000, f"{name}: {size} bytes")
                units = sum(get_meeting_frames.units_of(f["text"]) for f in result["frames"])
                self.assertLessEqual(units, 20_000)
                self.assertEqual(result["count"], 20)

    def test_the_chinese_case_that_once_gave_845_kilobytes_is_a_fraction_of_that(self):
        result = self.answer_for(self.SAMPLES["cjk"])

        self.assertLess(len(self.written(result).encode()), 60_000)

    def test_a_cut_frame_says_so_and_the_answer_says_so_once(self):
        result = self.answer_for(self.SAMPLES["ascii"])

        self.assertTrue(all(f["textTruncated"] for f in result["frames"]))
        self.assertEqual(result["note"].count("textTruncated=true"), 1)
        self.assertEqual(result["textRead"], 20)

    def test_every_frame_keeps_a_share_of_the_budget(self):
        result = self.answer_for(self.SAMPLES["ascii"])

        # The titles ("Slide 0" to "Slide 19", 150 units) and the ids (20 of 14
        # characters, 280 units) come off the 20,000 first.
        lengths = [len(f["text"]) for f in result["frames"]]
        self.assertEqual(sum(lengths) + 150 + 280, 20_000)
        self.assertLessEqual(max(lengths) - min(lengths), 1)

    def test_a_short_text_keeps_all_of_its_room_next_to_a_long_one(self):
        table = table_of(
            frame(10, text="short slide"), frame(20, text="long " * 10_000), frame(30, text="tiny")
        )

        result = run(table, last=3)

        short, long, tiny = result["frames"]
        self.assertEqual((short["text"], short["textTruncated"]), ("short slide", False))
        self.assertEqual((tiny["text"], tiny["textTruncated"]), ("tiny", False))
        ids_units = 3 * len(frame_id(10))
        self.assertEqual(len(long["text"]), 20_000 - ids_units - len("short slide") - len("tiny"))
        self.assertTrue(long["textTruncated"])

    def test_nothing_is_flagged_and_there_is_no_note_when_everything_fits(self):
        result = run(table_of(frame(10, text="a"), frame(20, text="b")), last=2)

        self.assertFalse(any(f["textTruncated"] for f in result["frames"]))
        self.assertNotIn("note", result)

    def test_only_the_selected_frames_share_the_budget(self):
        table = table_of(*[frame(10 * (n + 1), text="z" * 9000) for n in range(40)])

        result = run(table, last=2)

        self.assertEqual([len(f["text"]) for f in result["frames"]], [9000, 9000])
        self.assertNotIn("note", result)

    def test_a_title_cannot_push_the_answer_past_the_limit(self):
        table = table_of(
            *[frame(10 * (n + 1), title="😀" * 1000, text="中" * 9000) for n in range(20)]
        )

        result = run(table, last=20)

        self.assertLess(len(self.written(result).encode()), 100_000)

    def on_the_wire(self, result):
        """The size of the answer as the endpoint sends it: written by index.py, then wrapped."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": self.written(result)}]},
        }
        return len(json.dumps(body).encode())

    def test_titles_and_texts_together_are_held_to_the_budget(self):
        table = table_of(
            *[frame(10 * (n + 1), title="T" * 200, text="é" * 6000) for n in range(20)]
        )

        result = run(table, last=20)

        units = get_meeting_frames.units_of
        used = sum(
            units(f["frameId"]) + units(f["title"]) + units(f["text"]) for f in result["frames"]
        )
        self.assertLessEqual(used, get_meeting_frames.TEXT_BUDGET)
        self.assertGreater(used, get_meeting_frames.TEXT_BUDGET - 60)
        self.assertTrue(all(f["textTruncated"] for f in result["frames"]))

    def test_a_title_is_cut_by_what_it_costs_as_well_as_by_its_length(self):
        table = table_of(
            frame(10, title="a" * 500),
            frame(20, title="中" * 500),
            frame(30, title="😀" * 500),
            frame(40, title="Q3 plan"),
        )

        result = run(table, last=4)

        self.assertEqual(
            [f["title"] for f in result["frames"]],
            ["a" * 200, "中" * 66, "😀" * 33, "Q3 plan"],
        )

    def test_a_title_with_control_characters_has_them_turned_into_spaces(self):
        result = run(table_of(frame(10, title="a\x01b\x7fc")))

        self.assertEqual(result["frames"][0]["title"], "a b c")

    def test_the_worst_case_of_any_alphabet_stays_under_one_hundred_kilobytes_on_the_wire(self):
        alphabets = {
            "ascii": "a",
            "quote": '"',
            "backslash": "\\",
            "newline": "\n",
            "tab": "\t",
            "accented": "é",
            "cyrillic": "ж",
            "cjk": "中",
            "emoji": "😀",
            "control": "\x01",
        }
        for name, char in alphabets.items():
            with self.subTest(alphabet=name):
                table = table_of(
                    *[frame(10 * (n + 1), title=char * 300, text=char * 6000) for n in range(20)]
                )

                result = run(table, last=20)

                self.assertEqual(result["count"], 20)
                self.assertLess(self.on_the_wire(result), 100_000)

    def test_emoji_titles_with_emoji_texts_stay_under_the_limit(self):
        # 200-emoji titles and 6,000-emoji texts on 20 frames came to 107,890 bytes on
        # the wire while titles stood outside the budget.
        table = table_of(
            *[frame(10 * (n + 1), title="😀" * 200, text="😀" * 6000) for n in range(20)]
        )

        result = run(table, last=20)

        self.assertLess(self.on_the_wire(result), 100_000)

    def test_a_long_frame_id_is_cut_to_64_characters(self):
        row = frame(10, FrameId="x" * 1000)

        result = run(table_of(row))

        self.assertEqual(result["frames"][0]["frameId"], "x" * 64)
        self.assertEqual(get_meeting_frames.FRAME_ID_LIMIT, 64)

    def test_a_frame_id_of_64_characters_is_whole(self):
        result = run(table_of(frame(10, FrameId="y" * 64)))

        self.assertEqual(result["frames"][0]["frameId"], "y" * 64)

    def test_frame_ids_are_counted_in_the_budget(self):
        table = table_of(
            *[
                frame(10 * (n + 1), FrameId=f"{n:02d}" + "中" * 62, text="a" * 6000)
                for n in range(20)
            ]
        )

        result = run(table, last=20)

        units = get_meeting_frames.units_of
        used = sum(units(f["frameId"]) + units(f["text"]) for f in result["frames"])
        self.assertLessEqual(used, get_meeting_frames.TEXT_BUDGET)
        self.assertEqual(sum(units(f["frameId"]) for f in result["frames"]), 20 * (2 + 62 * 3))

    def test_long_ids_of_quotes_keep_the_answer_under_one_hundred_kilobytes(self):
        # Twenty 1,000-character ids of quotes once gave 164,944 bytes on the wire.
        for char in ('"', "\\", "\x01", "\x7f", "😀"):
            with self.subTest(char=repr(char)):
                table = table_of(
                    *[
                        frame(10 * (n + 1), FrameId=char * 1000, title='"' * 300, text='"' * 6000)
                        for n in range(20)
                    ]
                )

                result = run(table, last=20)

                self.assertEqual(result["count"], 20)
                self.assertLess(self.on_the_wire(result), 100_000)

    def test_a_frame_id_that_is_not_text_is_shown_as_text(self):
        result = run(table_of(frame(10, FrameId=Decimal("12345"))))

        self.assertEqual(result["frames"][0]["frameId"], "12345")

    def test_any_ids_titles_and_texts_cost_at_most_four_bytes_a_unit_on_the_wire(self):
        import random

        chooser = random.Random(11)
        units = get_meeting_frames.units_of
        for case in range(1000):
            # Mostly small answers, one in ten large enough for the texts to be cut.
            count = chooser.randint(1, 20)
            longest = 3000 if case % 10 == 0 else 200
            rows = [
                frame(
                    10 * (n + 1),
                    FrameId=random_text(chooser, 100) or "id",
                    title=random_text(chooser, 300),
                    text=random_text(chooser, longest),
                )
                for n in range(count)
            ]
            with self.subTest(case=case):
                result = run(table_of(*rows), last=20)
                used = sum(
                    units(f["frameId"]) + units(f["title"] or "") + units(f["text"] or "")
                    for f in result["frames"]
                )
                blank = json.loads(json.dumps(result, default=str))
                for entry in blank["frames"]:
                    entry["frameId"] = ""
                    entry["title"] = "" if entry["title"] is not None else None
                    entry["text"] = "" if entry["text"] is not None else None
                self.assertLessEqual(used, get_meeting_frames.TEXT_BUDGET)
                self.assertLessEqual(self.on_the_wire(result) - self.on_the_wire(blank), 4 * used)
                self.assertLess(self.on_the_wire(result), 100_000)

    def test_the_meeting_row_is_read_consistently_so_an_objection_is_seen_at_once(self):
        table = table_of(*slides(10, 20))

        run(table)

        self.assertEqual(len(table.gets), 1)
        self.assertIs(table.gets[0].get("ConsistentRead"), True)

    def test_the_frames_themselves_are_not_read_consistently(self):
        # Frames are written once and read long after; only the meeting row decides.
        table = table_of(*slides(10, 20))

        run(table)

        self.assertTrue(table.queries)
        self.assertTrue(all("ConsistentRead" not in query for query in table.queries))


class AccessTest(unittest.TestCase):
    def read(self, row, *frames, **kwargs):
        return run(FakeTable(row, *frames), **kwargs)

    def test_the_owner_reads(self):
        self.assertEqual(self.read(meeting(), frame(10))["count"], 1)

    def test_an_older_row_names_its_owner_as_agent_id(self):
        row = {"PK": f"c#{CALL}", "SK": f"c#{CALL}", "AgentId": OWNER, "Consent": ALLOWED}

        self.assertEqual(self.read(row, frame(10))["count"], 1)

    def test_a_person_the_meeting_is_shared_with_reads(self):
        for shared in ([FRIEND], f"{OTHER}, {FRIEND}", f"[{FRIEND}]", {FRIEND}):
            with self.subTest(shared=shared):
                row = meeting(SharedWith=shared)

                self.assertEqual(self.read(row, frame(10), user_id=FRIEND)["count"], 1)

    def test_someone_else_is_refused_and_no_frame_is_read(self):
        table = FakeTable(meeting(SharedWith=[FRIEND]), frame(10))

        with self.assertRaises(PermissionError) as caught:
            run(table, user_id=OTHER)

        self.assertEqual(str(caught.exception), "Access denied to this meeting")
        self.assertEqual((table.queries, table.batches), ([], []))

    def test_an_admin_reads_any_meeting(self):
        result = self.read(meeting(), frame(10), user_id="admin@example.com", is_admin=True)

        self.assertEqual(result["count"], 1)

    def test_no_identity_reads_nothing(self):
        for user_id in (None, ""):
            with self.subTest(user_id=user_id), self.assertRaises(PermissionError):
                self.read(meeting(), frame(10), user_id=user_id)

    def test_a_missing_meeting_and_a_forbidden_one_are_refused_with_the_same_text(self):
        messages = []
        for call_id, user in (("exists", OTHER), ("no-such-meeting", OWNER)):
            table = FakeTable(meeting("exists"), frame(10, call_id="exists"))
            with self.assertRaises(PermissionError) as caught:
                run(table, call_id, user_id=user)
            messages.append(str(caught.exception))

        self.assertEqual(messages, ["Access denied to this meeting"] * 2)

    def test_frames_of_a_meeting_without_a_row_are_not_read(self):
        table = FakeTable(frame(10))

        with self.assertRaises(PermissionError):
            run(table, user_id="admin@example.com", is_admin=True)

        self.assertEqual(table.queries, [])

    def test_only_the_meeting_row_is_read_before_access_is_decided(self):
        table = FakeTable(meeting(), frame(10))

        run(table)

        request = table.gets[0]
        projected = {
            request["ExpressionAttributeNames"].get(p.strip(), p.strip())
            for p in request["ProjectionExpression"].split(",")
        }
        self.assertEqual(projected, {"Owner", "AgentId", "SharedWith", "Consent"})

    def test_the_meeting_id_decides_which_frames_are_read(self):
        table = FakeTable(
            meeting("a"),
            meeting("b", Owner=OTHER),
            frame(10, call_id="a", text="mine"),
            frame(10, call_id="b", text="theirs"),
        )

        result = run(table, "a")

        self.assertEqual([f["text"] for f in result["frames"]], ["mine"])

    def test_arguments_are_checked_before_any_read(self):
        table = FakeTable(meeting(), frame(10))
        bad = (
            {"call_id": None},
            {"call_id": ""},
            {"call_id": 12},
            {"call_id": "x" * 257},
            {"last": "2"},
            {"fromSeconds": "1"},
            {"toSeconds": -1},
            {"fromSeconds": 5, "toSeconds": 1},
        )
        for arguments in bad:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                run(table, user_id=OTHER, **arguments)
        self.assertEqual((table.gets, table.queries, table.batches), ([], [], []))

    def test_a_meeting_id_that_cannot_be_one_has_its_own_message(self):
        with self.assertRaises(ValueError) as caught:
            run(FakeTable(), call_id=["x"])

        self.assertEqual(
            str(caught.exception), "meetingId must be a non-empty string of at most 256 characters"
        )


class ConsentTest(unittest.TestCase):
    def test_a_meeting_whose_consent_does_not_allow_capture_returns_no_frames(self):
        table = FakeTable(meeting(Consent=DENIED), frame(10, text="secret slide"))

        with self.assertRaises(PermissionError) as caught:
            run(table)

        self.assertEqual(str(caught.exception), consent_gate.CAPTURE_NOT_ALLOWED_MESSAGE)
        self.assertNotIn("secret slide", str(caught.exception))
        self.assertEqual((table.queries, table.batches), ([], []))

    def test_a_consent_record_decides_for_every_mode(self):
        cases = {
            "off": ({"Mode": "off"}, True),
            "notify": ({"Mode": "notify"}, True),
            "notify objected": (
                {"Mode": "notify", "Events": [{"ParticipantName": "Ana", "Decision": "objection"}]},
                False,
            ),
            "opt_in": (DENIED, False),
            "opt_in attested": (
                {"Mode": "opt_in", "Rule": "owner_attests", "Attestation": {"By": OWNER}},
                True,
            ),
        }
        for name, (record, allowed) in cases.items():
            with self.subTest(case=name):
                table = FakeTable(meeting(Consent=record), frame(10))

                if allowed:
                    self.assertEqual(run(table)["count"], 1)
                else:
                    with self.assertRaises(PermissionError):
                        run(table)

    def test_a_meeting_without_a_record_follows_the_deployment_mode(self):
        row = meeting()
        del row["Consent"]
        for mode, allowed in (("off", True), ("notify", True), ("opt_in", False)):
            with (
                self.subTest(mode=mode),
                patch.object(consent_gate, "deployment_consent_mode", return_value=mode),
            ):
                table = FakeTable(row, frame(10))
                if allowed:
                    self.assertEqual(run(table)["count"], 1)
                else:
                    with self.assertRaises(PermissionError):
                        run(table)

    def test_the_gate_applies_to_an_admin_too(self):
        table = FakeTable(meeting(Consent=DENIED), frame(10))

        with self.assertRaises(PermissionError):
            run(table, user_id="admin@example.com", is_admin=True)

    def test_settings_that_cannot_be_read_fail_closed(self):
        row = meeting()
        del row["Consent"]
        failure = consent_gate.ConsentSettingsUnavailable()
        table = FakeTable(row, frame(10))

        with patch.object(consent_gate, "deployment_consent_mode", side_effect=failure):
            with self.assertRaises(consent_gate.ConsentSettingsUnavailable):
                run(table)

        self.assertEqual(table.queries, [])

    def test_access_is_decided_before_consent(self):
        table = FakeTable(meeting(Consent=DENIED), frame(10))

        with self.assertRaises(PermissionError) as caught:
            run(table, user_id=OTHER)

        self.assertEqual(str(caught.exception), "Access denied to this meeting")


class LoggingTest(unittest.TestCase):
    def test_the_log_holds_the_meeting_by_repr_and_counts_but_never_frame_content(self):
        call_id = "call\nsecond line"
        table = FakeTable(
            meeting(call_id),
            frame(10, call_id=call_id, title="Acquisition of Contoso", text="Offer: $9 per share"),
        )

        with self.assertLogs(level="INFO") as logged:
            run(table, call_id)

        text = "\n".join(logged.output)
        self.assertIn("'call\\nsecond line'", text)
        self.assertNotIn("call\nsecond line", text)
        for content in ("Contoso", "$9", OWNER):
            self.assertNotIn(content, text)

    def test_a_refusal_is_logged_without_frame_content(self):
        table = FakeTable(meeting(), frame(10, text="Offer: $9 per share"))

        with self.assertLogs(level="WARNING") as logged:
            with self.assertRaises(PermissionError):
                run(table, user_id=OTHER)

        self.assertNotIn("$9", "\n".join(logged.output))


if __name__ == "__main__":
    unittest.main()
