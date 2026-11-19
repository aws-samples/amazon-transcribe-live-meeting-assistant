# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""get_live_context: the setting, the arguments, the cursor, access, consent and the read.

The table is the stand-in of the simulation (test_get_live_context_simulation),
which evaluates the boto3 conditions and the projection the tool sends and
pages like a Query. The configuration layer is the real one, from the
repository, for the setting, the consent rules, the channels and the speaker
names.
"""

import base64
import json
import logging
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("CALLS_TABLE", "test-calls-table")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))
LAYER = HERE.parents[2] / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

from botocore.exceptions import ClientError  # noqa: E402
from lma_config import ConfigurationError  # noqa: E402

from tools import consent_gate, get_live_context  # noqa: E402
from tools.test_get_live_context_simulation import (  # noqa: E402
    T0,
    FakeTranscriptTable,
    decode,
    evaluate,
    iso,
    meeting_row,
)

G = get_live_context
ME = "me@example.com"
OTHER = "other@example.com"
FRIEND = "friend@example.com"
CALL = "live-1"
NOW = T0 + 3600.0
SECRET = "arn:aws:dynamodb:us-east-1:123456789012:table/secret-table"


def segment(segment_id, start, created, text=None, channel="REMOTE", speaker="spk_0", **extra):
    row = {
        "PK": f"trs#{CALL}",
        "SK": f"s#{segment_id}",
        "SegmentId": segment_id,
        "Channel": channel,
        "Speaker": speaker,
        "Transcript": text if text is not None else f"text of {segment_id}",
        "StartTime": Decimal(str(start)),
        "EndTime": Decimal(str(start + 2)),
        "IsPartial": False,
    }
    if created is not None:
        row["CreatedAt"] = created if isinstance(created, str) else iso(created)
    row.update(extra)
    return row


def table_with(*rows, meeting=None, page_rows=25):
    table = FakeTranscriptTable(meeting if meeting is not None else meeting_row(CALL), page_rows)
    for row in rows:
        table.put(row)
    return table


def call(table, cursor=None, max_lines=None, include_chat=None, user_id=ME, is_admin=False, **kw):
    return G.execute(
        meeting_id=kw.pop("meeting_id", CALL),
        cursor=cursor,
        max_lines=max_lines,
        include_chat=include_chat,
        user_id=user_id,
        is_admin=is_admin,
        now=kw.pop("now", lambda: NOW),
        timer=kw.pop("timer", lambda: 0.0),
        table=table,
    )


def cursor_of(**state):
    state.setdefault("v", 2)
    state.setdefault("m", CALL)
    text = json.dumps(state)
    return base64.urlsafe_b64encode(text.encode()).decode()


def ids(answer):
    return [line["segmentId"] for line in answer["lines"]]


class SettingOn(unittest.TestCase):
    """Every test here runs with the setting on, unless it says otherwise."""

    def setUp(self):
        patcher = patch.object(G, "live_context_state", return_value=G.ON)
        patcher.start()
        self.addCleanup(patcher.stop)


# ── the setting ─────────────────────────────────────────────────────────────


class FakeStore:
    def __init__(self, values=None, error=None):
        self.values = values or {}
        self.error = error
        self.reads = 0

    def get_section(self, name):
        self.reads += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(values=self.values.get(name, {}))


class TestTheSetting(unittest.TestCase):
    def setUp(self):
        self.saved = G._SETTING_STORE
        self.addCleanup(setattr, G, "_SETTING_STORE", self.saved)

    def _state_with(self, store):
        G._SETTING_STORE = G._WatchedStore(store)
        return G.live_context_state()

    def test_stored_true_is_on(self):
        self.assertEqual(
            self._state_with(FakeStore({"meeting_assist": {"mcpLiveContext": True}})), G.ON
        )

    def test_stored_false_absent_or_not_a_boolean_is_off(self):
        for values in ({"mcpLiveContext": False}, {}, {"mcpLiveContext": "yes"}):
            with self.subTest(values=values):
                self.assertEqual(self._state_with(FakeStore({"meeting_assist": values})), G.OFF)

    def test_a_table_that_cannot_be_read_is_unreadable_not_off(self):
        errors = (
            ClientError({"Error": {"Code": "AccessDeniedException", "Message": SECRET}}, "GetItem"),
            ConfigurationError("the stored body could not be decoded"),
            RuntimeError("something else"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self._state_with(FakeStore(error=error)), G.UNREADABLE)

    def test_a_failed_read_does_not_stick_once_the_table_answers(self):
        store = FakeStore(error=ConfigurationError("x"))
        self.assertEqual(self._state_with(store), G.UNREADABLE)
        store.error = None
        store.values = {"meeting_assist": {"mcpLiveContext": True}}
        self.assertEqual(G.live_context_state(), G.ON)

    def test_a_deployment_without_the_table_name_has_the_shipped_default_off(self):
        G._SETTING_STORE = None
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONFIGURATION_TABLE_NAME", None)
            self.assertEqual(G.live_context_state(), G.OFF)

    def test_a_missing_layer_is_unreadable(self):
        with patch.dict(sys.modules, {"lma_config": None}):
            self.assertEqual(G.live_context_state(), G.UNREADABLE)

    def test_one_store_serves_every_call_of_a_process(self):
        built = []

        class Built(FakeStore):
            def __init__(self):
                super().__init__({"meeting_assist": {"mcpLiveContext": True}})
                built.append(self)

        G._SETTING_STORE = None
        with patch("lma_config.ConfigurationStore", Built):
            for _ in range(3):
                self.assertEqual(G.live_context_state(), G.ON)
        self.assertEqual(len(built), 1)
        self.assertEqual(built[0].reads, 3)

    def test_the_read_names_the_setting_and_its_default(self):
        seen = {}

        def fake_read(section, setting, **kwargs):
            seen.update(section=section, setting=setting, **kwargs)
            return True

        G._SETTING_STORE = G._WatchedStore(FakeStore())
        with patch("lma_config.read_setting", fake_read):
            self.assertEqual(G.live_context_state(), G.ON)
        self.assertEqual((seen["section"], seen["setting"]), ("meeting_assist", "mcpLiveContext"))
        self.assertIs(seen["default"], False)
        self.assertIs(seen["store"], G._SETTING_STORE)

    def test_off_refuses_before_anything_is_read_and_says_where_to_turn_it_on(self):
        table = table_with(segment("a", 1, NOW))
        with patch.object(G, "live_context_state", return_value=G.OFF):
            with self.assertRaises(PermissionError) as raised:
                call(table, max_lines="many", meeting_id=None)
        self.assertEqual(str(raised.exception), G.SETTING_OFF_MESSAGE)
        self.assertIn("Admin", G.SETTING_OFF_MESSAGE)
        self.assertIn("Configuration", G.SETTING_OFF_MESSAGE)
        self.assertEqual((table.gets, table.queries), ([], []))

    def test_unreadable_refuses_with_try_again_and_never_says_turned_off(self):
        table = table_with(segment("a", 1, NOW))
        with patch.object(G, "live_context_state", return_value=G.UNREADABLE):
            with self.assertRaises(PermissionError) as raised:
                call(table)
        message = str(raised.exception)
        self.assertIn("could not be read", message)
        self.assertIn("try again", message)
        self.assertNotIn("off", message)
        self.assertEqual((table.gets, table.queries), ([], []))


# ── arguments ───────────────────────────────────────────────────────────────


class TestArguments(SettingOn):
    def test_a_missing_or_blank_meeting_id_is_refused_before_any_read(self):
        table = table_with()
        for value in (None, "", "   ", 7, ["x"], "x" * 513):
            with self.subTest(meeting_id=value):
                with self.assertRaises(ValueError) as raised:
                    call(table, meeting_id=value)
                self.assertEqual(str(raised.exception), G.MEETING_ID_ERROR)
        self.assertEqual((table.gets, table.queries), ([], []))

    def test_max_lines_defaults_to_200_and_is_moved_into_range(self):
        cases = {None: 200, 0: 1, -5: 1, 5000: 1000, "20": 20, 20.0: 20, " 7 ": 7, 1000: 1000}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(G.parse_max_lines(value), expected)

    def test_max_lines_that_is_not_a_whole_number_is_refused(self):
        for value in (
            True,
            False,
            "many",
            1.5,
            float("nan"),
            float("inf"),
            1e999,
            "1e999",
            [1],
            {},
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as raised:
                    G.parse_max_lines(value)
                self.assertEqual(str(raised.exception), G.MAX_LINES_ERROR)

    def test_include_chat_must_be_a_real_boolean(self):
        self.assertIs(G.parse_include_chat(None), True)
        self.assertIs(G.parse_include_chat(False), False)
        for value in ("false", 0, 1, "true", []):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    G.parse_include_chat(value)

    def test_bad_arguments_are_refused_before_the_meeting_is_read(self):
        table = table_with()
        for kwargs in ({"max_lines": "many"}, {"include_chat": "yes"}, {"cursor": "@@"}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    call(table, **kwargs)
        self.assertEqual((table.gets, table.queries), ([], []))


# ── the cursor: what it accepts ─────────────────────────────────────────────


class TestCursorParsing(unittest.TestCase):
    def parse(self, cursor, meeting_id=CALL, now=NOW):
        return G.parse_cursor(cursor, meeting_id, now)

    def assertRefused(self, cursor, message=G.CURSOR_INVALID):
        with self.assertRaises(ValueError) as raised:
            self.parse(cursor)
        self.assertEqual(str(raised.exception), message)

    def test_no_cursor_and_a_blank_one_mean_none(self):
        for value in (None, "", "   ", "\n"):
            with self.subTest(value=value):
                self.assertIsNone(self.parse(value))

    def test_a_cursor_the_tool_made_reads_back(self):
        self.assertEqual(
            self.parse(G.encode_cursor(CALL, NOW - 10)), {"w": NOW - 10, "n": None, "after": None}
        )
        walk = self.parse(G.encode_cursor(CALL, NOW - 10, (NOW, NOW - 3, "seg")))
        self.assertEqual(walk, {"w": NOW - 10, "n": NOW, "after": (NOW - 3, "seg")})

    def test_a_cursor_that_is_not_text_is_refused(self):
        for value in (12, 1.5, True, ["x"], {"v": 2}):
            with self.subTest(value=value):
                self.assertRefused(value)

    def test_text_that_is_not_base64_is_refused(self):
        for value in ("@@@", "abc", "not a cursor!", "é"):
            with self.subTest(value=value):
                self.assertRefused(value)

    def test_base64_that_is_not_json_is_refused(self):
        self.assertRefused(base64.urlsafe_b64encode(b"\xff\xfe not json").decode())
        self.assertRefused(base64.urlsafe_b64encode(b"{broken").decode())

    def test_json_that_is_not_an_object_is_refused(self):
        for body in ("[1, 2]", "2", '"text"', "null"):
            with self.subTest(body=body):
                self.assertRefused(base64.urlsafe_b64encode(body.encode()).decode())

    def test_a_cursor_with_no_version_or_version_1_is_from_an_older_version(self):
        old = base64.urlsafe_b64encode(json.dumps({"m": CALL, "t": 12.5}).encode()).decode()
        self.assertRefused(old, G.CURSOR_OLD)
        self.assertRefused(cursor_of(v=1, w=NOW), G.CURSOR_OLD)
        self.assertRefused(cursor_of(v=0, w=NOW), G.CURSOR_OLD)

    def test_an_unknown_version_is_refused(self):
        for version in (3, "2", True, None, 2.5):
            with self.subTest(version=version):
                self.assertRefused(cursor_of(v=version, w=NOW))

    def test_a_cursor_of_another_meeting_is_refused_without_naming_either(self):
        with self.assertRaises(ValueError) as raised:
            self.parse(cursor_of(m="someone-elses-meeting", w=NOW))
        self.assertEqual(str(raised.exception), G.CURSOR_OTHER_MEETING)
        self.assertNotIn("someone-elses-meeting", str(raised.exception))
        self.assertNotIn(CALL, str(raised.exception))

    def test_a_meeting_id_that_is_not_text_is_refused(self):
        for value in (None, 7, [CALL]):
            with self.subTest(value=value):
                self.assertRefused(cursor_of(m=value, w=NOW))

    def test_a_watermark_that_is_not_a_finite_time_is_refused(self):
        for value in (None, "1795000000", True, float("nan"), float("inf"), -1, [NOW]):
            with self.subTest(value=value):
                self.assertRefused(cursor_of(w=value))

    def test_a_watermark_beyond_what_a_clock_could_give_is_refused(self):
        self.assertRefused(cursor_of(w=NOW + G.FUTURE_LIMIT_SECONDS + 1))
        self.assertRefused(cursor_of(w=1e308))
        self.assertIsNotNone(self.parse(cursor_of(w=NOW + G.FUTURE_LIMIT_SECONDS)))

    def test_an_unknown_field_is_refused(self):
        self.assertRefused(cursor_of(w=NOW, extra=1))

    def test_a_cursor_longer_than_the_cap_is_refused(self):
        self.assertRefused(cursor_of(w=NOW, a=[NOW, "x" * 5000], n=NOW))
        self.assertRefused("A" * (G.MAX_CURSOR_CHARS + 4))

    def test_a_walk_needs_both_its_clock_and_its_last_line(self):
        self.assertRefused(cursor_of(w=NOW - 5, n=NOW))
        self.assertRefused(cursor_of(w=NOW - 5, a=[NOW - 1, "s"]))

    def test_a_walk_clock_before_the_watermark_or_in_the_future_is_refused(self):
        self.assertRefused(cursor_of(w=NOW - 5, n=NOW - 6, a=[NOW - 7, "s"]))
        self.assertRefused(cursor_of(w=NOW - 5, n=NOW + 400, a=[NOW, "s"]))

    def test_a_last_line_that_is_not_a_time_and_a_segment_is_refused(self):
        for after in ([NOW], [NOW, 5], ["x", "s"], [True, "s"], [NOW + 1, "s"], "s", [NOW, "s", 1]):
            with self.subTest(after=after):
                self.assertRefused(cursor_of(w=NOW - 5, n=NOW, a=after))

    def test_padding_removed_from_the_cursor_is_refused_not_guessed(self):
        cursor = G.encode_cursor(CALL, NOW - 10)
        if cursor.endswith("="):
            self.assertRefused(cursor.rstrip("="))

    def test_surrounding_spaces_are_ignored(self):
        self.assertEqual(self.parse("  " + G.encode_cursor(CALL, NOW) + " ")["w"], NOW)


# ── the cursor: what it returns ─────────────────────────────────────────────


class TestFollowing(SettingOn):
    def test_no_cursor_returns_the_newest_lines_in_meeting_order(self):
        rows = [segment(f"s{i:02d}", start=i * 4, created=NOW - 600 + i) for i in range(10)]
        # Written last, spoken first: newest by write time.
        rows.append(segment("late", start=1, created=NOW - 1))
        answer = call(table_with(*rows), max_lines=3)
        self.assertEqual(ids(answer), ["late", "s08", "s09"])
        self.assertFalse(answer["truncated"])
        self.assertEqual(decode(answer["nextCursor"]), {"v": 2, "m": CALL, "w": NOW})
        self.assertEqual(answer["note"], G.OLDER_NOTE)

    def test_no_cursor_on_a_short_meeting_has_no_note(self):
        answer = call(table_with(segment("a", 1, NOW - 5)))
        self.assertEqual(ids(answer), ["a"])
        self.assertNotIn("note", answer)

    def test_an_empty_transcript_answers_no_lines_and_a_cursor(self):
        answer = call(table_with())
        self.assertEqual(answer["lines"], [])
        self.assertEqual(decode(answer["nextCursor"])["w"], NOW)

    def test_a_cursor_returns_lines_written_since_its_watermark_minus_60_seconds(self):
        watermark = NOW - 30
        rows = [
            segment("old", 1, watermark - 61),
            segment("edge", 2, watermark - 60),
            segment("inside", 3, watermark - 59),
            segment("new", 4, NOW - 1),
        ]
        answer = call(table_with(*rows), cursor=G.encode_cursor(CALL, watermark))
        self.assertEqual(ids(answer), ["edge", "inside", "new"])
        self.assertEqual(decode(answer["nextCursor"])["w"], NOW)

    def test_a_line_written_late_with_an_early_meeting_time_is_returned(self):
        watermark = NOW - 10
        rows = [segment("first", 1, NOW - 3000), segment("answer", 2, NOW - 2)]
        answer = call(table_with(*rows), cursor=G.encode_cursor(CALL, watermark))
        self.assertEqual(ids(answer), ["answer"])

    def test_a_rewritten_line_comes_again_with_its_new_text(self):
        table = table_with(segment("a", 1, NOW - 20, text="first words"))
        first = call(table, cursor=G.encode_cursor(CALL, NOW - 30))
        table.put(segment("a", 1, NOW + 4, text="better words"))
        second = call(table, cursor=first["nextCursor"], now=lambda: NOW + 5)
        self.assertEqual([line["text"] for line in second["lines"]], ["better words"])

    def test_a_full_page_is_truncated_and_the_next_call_goes_on_after_its_last_line(self):
        rows = [segment(f"s{i:02d}", start=i, created=NOW - 50 + i) for i in range(25)]
        table = table_with(*rows)
        seen = []
        cursor = G.encode_cursor(CALL, NOW - 55)
        for _ in range(3):
            answer = call(table, cursor=cursor, max_lines=10)
            seen += ids(answer)
            cursor = answer["nextCursor"]
            if not answer["truncated"]:
                break
        self.assertEqual(seen, [f"s{i:02d}" for i in range(25)])
        self.assertFalse(answer["truncated"])
        self.assertEqual(decode(cursor)["w"], NOW)

    def test_the_continuation_names_the_exact_last_line_and_keeps_the_watermark(self):
        rows = [segment(f"s{i}", start=i, created=NOW - 20 + i) for i in range(5)]
        answer = call(table_with(*rows), cursor=G.encode_cursor(CALL, NOW - 25), max_lines=2)
        self.assertTrue(answer["truncated"])
        state = decode(answer["nextCursor"])
        self.assertEqual(state["w"], NOW - 25)
        self.assertEqual(state["n"], NOW)
        self.assertEqual(state["a"], [NOW - 19, "s1"])

    def test_a_page_that_takes_the_last_lines_exactly_is_not_truncated(self):
        rows = [segment(f"s{i}", start=i, created=NOW - 20 + i) for i in range(4)]
        answer = call(table_with(*rows), cursor=G.encode_cursor(CALL, NOW - 25), max_lines=4)
        self.assertFalse(answer["truncated"])
        self.assertNotIn("a", decode(answer["nextCursor"]))

    def test_the_size_budget_cuts_a_page_at_a_line_boundary(self):
        rows = [segment(f"s{i:02d}", i, NOW - 50 + i, text="x" * 1500) for i in range(30)]
        answer = call(table_with(*rows), cursor=G.encode_cursor(CALL, NOW - 55))
        self.assertTrue(answer["truncated"])
        # A line costs 60 + 1500 (text) + 3 (segment) + 5 (speaker) + 6 (channel)
        # + 5 (time) = 1579 units: 18 * 1579 <= 30,000 < 19 * 1579.
        self.assertEqual(len(answer["lines"]), 18)
        self.assertTrue(all(len(line["text"]) == 1500 for line in answer["lines"]))

    def test_a_line_over_2000_characters_is_cut_and_marked(self):
        answer = call(table_with(segment("long", 1, NOW - 1, text="y" * 5000)))
        text = answer["lines"][0]["text"]
        self.assertEqual(text, "y" * 2000 + " [...]")

    def test_lines_with_the_same_write_time_are_split_by_segment_without_loss(self):
        rows = [segment(f"t{i}", start=10 - i, created=NOW - 10) for i in range(6)]
        table = table_with(*rows)
        first = call(table, cursor=G.encode_cursor(CALL, NOW - 20), max_lines=4)
        second = call(table, cursor=first["nextCursor"], max_lines=4)
        self.assertEqual(sorted(ids(first) + ids(second)), [f"t{i}" for i in range(6)])
        self.assertFalse(set(ids(first)) & set(ids(second)))

    def test_a_line_written_during_a_walk_comes_at_its_end(self):
        rows = [segment(f"s{i}", start=i, created=NOW - 30 + i) for i in range(6)]
        table = table_with(*rows)
        first = call(table, cursor=G.encode_cursor(CALL, NOW - 40), max_lines=3)
        table.put(segment("during", start=0.5, created=NOW + 2))
        second = call(table, cursor=first["nextCursor"], max_lines=10, now=lambda: NOW + 3)
        self.assertEqual(ids(second)[-1], "s5")
        self.assertIn("during", ids(second))
        self.assertEqual(decode(second["nextCursor"])["w"], NOW)

    def test_a_write_time_ahead_of_the_clock_is_clamped_to_it(self):
        table = table_with(segment("ahead", 1, NOW + 120), segment("now", 2, NOW - 1))
        answer = call(table, cursor=G.encode_cursor(CALL, NOW - 10), max_lines=1)
        self.assertEqual(ids(answer), ["now"])
        state = decode(answer["nextCursor"])
        self.assertLessEqual(state["a"][0], state["n"])
        rest = call(table, cursor=answer["nextCursor"])
        self.assertEqual(ids(rest), ["ahead"])

    def test_a_line_ahead_of_the_clock_comes_again_until_the_clock_passes_it(self):
        table = table_with(segment("ahead", 1, NOW + 120))
        later = call(table, cursor=G.encode_cursor(CALL, NOW), now=lambda: NOW + 100)
        self.assertEqual(ids(later), ["ahead"])
        # Still inside the overlap of the next answer, then past it.
        again = call(table, cursor=later["nextCursor"], now=lambda: NOW + 181)
        self.assertEqual(ids(again), ["ahead"])
        after = call(table, cursor=again["nextCursor"], now=lambda: NOW + 190)
        self.assertEqual(ids(after), [])

    def test_write_times_take_the_nearest_earlier_row_when_unreadable_or_too_far_ahead(self):
        rows = [
            {"StartTime": 1, "SK": "s#a", "CreatedAt": iso(NOW - 100)},
            {"StartTime": 2, "SK": "s#b", "CreatedAt": "not a time"},
            {"StartTime": 3, "SK": "s#c"},
            {"StartTime": 4, "SK": "s#d", "CreatedAt": iso(NOW + 301)},
            {"StartTime": 5, "SK": "s#e", "CreatedAt": iso(NOW + 299)},
            {"StartTime": 6, "SK": "s#f", "CreatedAt": Decimal("1795000000")},
            {"StartTime": 7, "SK": "s#g", "CreatedAt": "2026-02-30T10:00:00Z"},
        ]
        self.assertEqual(
            G.write_times(rows, NOW), [NOW - 100, NOW - 100, NOW - 100, NOW - 100, NOW, NOW, NOW]
        )

    def test_a_first_row_with_no_readable_time_is_treated_as_new(self):
        rows = [
            {"StartTime": 1, "SK": "s#a"},
            {"StartTime": 2, "SK": "s#b", "CreatedAt": iso(NOW - 9)},
        ]
        self.assertEqual(G.write_times(rows, NOW), [NOW, NOW - 9])

    def test_meeting_order_decides_which_row_is_nearest_not_storage_order(self):
        rows = [
            {"StartTime": 9, "SK": "s#late", "CreatedAt": iso(NOW - 1)},
            {"StartTime": 5, "SK": "s#bad"},
            {"StartTime": 4, "SK": "s#before", "CreatedAt": iso(NOW - 50)},
        ]
        self.assertEqual(G.write_times(rows, NOW), [NOW - 1, NOW - 50, NOW - 50])

    def test_a_time_with_an_offset_and_one_with_no_zone_are_read(self):
        self.assertEqual(G.parse_write_time("2026-11-19T09:00:00+00:00"), T0)
        self.assertEqual(G.parse_write_time("2026-11-19T13:00:00+04:00"), T0)
        self.assertEqual(G.parse_write_time("2026-11-19T09:00:00"), T0)
        self.assertEqual(G.parse_write_time("2026-11-19T09:00:00.000Z"), T0)
        for value in (None, "", 5, "garbage", "9999-99-99"):
            self.assertIsNone(G.parse_write_time(value))

    def test_a_new_walk_never_goes_behind_the_cursor_watermark(self):
        table = table_with(segment("a", 1, NOW - 5))
        cursor = G.encode_cursor(CALL, NOW + 100)  # made by a clock 100 s ahead
        answer = call(table, cursor=cursor)
        self.assertEqual(decode(answer["nextCursor"])["w"], NOW + 100)
        self.assertEqual(ids(answer), [])

    def test_a_walk_keeps_its_own_clock_whatever_the_clock_says_now(self):
        rows = [segment(f"s{i}", start=i, created=NOW - 20 + i) for i in range(4)]
        table = table_with(*rows)
        first = call(table, cursor=G.encode_cursor(CALL, NOW - 25), max_lines=2)
        second = call(table, cursor=first["nextCursor"], now=lambda: NOW - 200)
        self.assertEqual(ids(second), ["s2", "s3"])
        self.assertEqual(decode(second["nextCursor"])["w"], NOW)

    def test_a_walk_clock_far_ahead_of_this_clock_is_refused(self):
        rows = [segment(f"s{i}", start=i, created=NOW - 20 + i) for i in range(4)]
        first = call(table_with(*rows), cursor=G.encode_cursor(CALL, NOW - 25), max_lines=2)
        with self.assertRaises(ValueError):
            call(table_with(*rows), cursor=first["nextCursor"], now=lambda: NOW - 301)

    def test_every_walk_ends_one_line_at_a_time(self):
        rows = [segment(f"s{i:02d}", start=i, created=NOW - 40) for i in range(30)]
        table = table_with(*rows)
        cursor, calls, seen = G.encode_cursor(CALL, NOW - 50), 0, []
        while True:
            calls += 1
            answer = call(table, cursor=cursor, max_lines=1)
            seen += ids(answer)
            cursor = answer["nextCursor"]
            if not answer["truncated"]:
                break
            self.assertLess(calls, 40)
        self.assertEqual(seen, [f"s{i:02d}" for i in range(30)])

    def test_a_continuation_with_made_up_times_reads_only_this_meeting_and_answers(self):
        table = table_with(segment("a", 1, NOW - 5))
        made_up = cursor_of(w=0, n=0, a=[0, "\uffff"])
        answer = call(table, cursor=made_up)
        self.assertEqual(answer["lines"], [])
        self.assertFalse(answer["truncated"])


# ── access, consent and what a line holds ───────────────────────────────────


class TestAccessAndConsent(SettingOn):
    def test_the_owner_an_older_agent_id_a_shared_user_and_an_admin_can_read(self):
        cases = [
            (meeting_row(CALL), ME, False),
            (meeting_row(CALL, owner=None, AgentId=ME), ME, False),
            (meeting_row(CALL, owner=OTHER, SharedWith=[FRIEND]), FRIEND, False),
            (meeting_row(CALL, owner=OTHER, SharedWith=f"[{OTHER}, {FRIEND}]"), FRIEND, False),
            (meeting_row(CALL, owner=OTHER), "admin", True),
        ]
        for meeting, user, admin in cases:
            with self.subTest(user=user, meeting=meeting):
                table = table_with(segment("a", 1, NOW - 1), meeting=meeting)
                self.assertEqual(ids(call(table, user_id=user, is_admin=admin)), ["a"])

    def test_another_user_no_identity_and_a_missing_meeting_get_the_same_refusal(self):
        cases = [
            (meeting_row(CALL, owner=OTHER), ME),
            (meeting_row(CALL, owner=OTHER, SharedWith=f"{FRIEND}x"), FRIEND),
            (meeting_row(CALL), None),
            (meeting_row("another-meeting"), ME),
        ]
        for meeting, user in cases:
            with self.subTest(user=user, meeting=meeting["PK"]):
                table = table_with(segment("a", 1, NOW - 1), meeting=meeting)
                with self.assertRaises(PermissionError) as raised:
                    call(table, user_id=user)
                self.assertEqual(str(raised.exception), G.ACCESS_DENIED_MESSAGE)
                self.assertEqual(table.queries, [])

    def test_a_cursor_cannot_read_a_meeting_the_caller_cannot_read(self):
        table = table_with(segment("a", 1, NOW - 1), meeting=meeting_row(CALL, owner=OTHER))
        with self.assertRaises(PermissionError):
            call(table, cursor=G.encode_cursor(CALL, NOW - 10))
        self.assertEqual(table.queries, [])

    def test_the_meeting_row_is_read_with_its_own_key_whatever_the_cursor_says(self):
        table = table_with(segment("a", 1, NOW - 1))
        with self.assertRaises(ValueError):
            call(table, cursor=cursor_of(m="other", w=NOW))
        self.assertEqual(table.gets, [])
        call(table)
        self.assertEqual(table.gets, [{"PK": f"c#{CALL}", "SK": f"c#{CALL}"}])
        self.assertTrue(
            all(evaluate(q["KeyConditionExpression"], {"PK": f"trs#{CALL}"}) for q in table.queries)
        )

    def test_consent_that_withholds_capture_refuses_and_reads_no_transcript(self):
        meeting = meeting_row(CALL, Consent={"Mode": "opt_in", "Rule": "owner_attests"})
        table = table_with(segment("a", 1, NOW - 1), meeting=meeting)
        with self.assertRaises(PermissionError) as raised:
            call(table)
        self.assertEqual(str(raised.exception), consent_gate.CAPTURE_NOT_ALLOWED_MESSAGE)
        self.assertEqual(table.queries, [])

    def test_consent_settings_that_cannot_be_read_refuse(self):
        table = table_with(segment("a", 1, NOW - 1))
        with patch.object(
            consent_gate,
            "deployment_consent_mode",
            side_effect=consent_gate.ConsentSettingsUnavailable,
        ):
            with self.assertRaises(consent_gate.ConsentSettingsUnavailable):
                call(table)
        self.assertEqual(table.queries, [])

    def test_access_is_checked_before_consent(self):
        meeting = meeting_row(
            CALL, owner=OTHER, Consent={"Mode": "opt_in", "Rule": "owner_attests"}
        )
        with self.assertRaises(PermissionError) as raised:
            call(table_with(meeting=meeting))
        self.assertEqual(str(raised.exception), G.ACCESS_DENIED_MESSAGE)

    def test_consent_that_allows_capture_returns_lines(self):
        meeting = meeting_row(CALL, Consent={"Mode": "notify", "Status": "NOTIFIED"})
        self.assertEqual(ids(call(table_with(segment("a", 1, NOW - 1), meeting=meeting))), ["a"])

    def test_an_ended_meeting_still_returns_lines_and_points_to_the_transcript(self):
        meeting = meeting_row(CALL, status="ENDED", UpdatedAt=iso(NOW - 30))
        table = table_with(segment("a", 1, NOW - 1), meeting=meeting)
        answer = call(table)
        self.assertTrue(answer["ended"])
        self.assertEqual(answer["status"], "ENDED")
        self.assertEqual(ids(answer), ["a"])
        self.assertEqual(answer["note"], G.ENDED_NOTE)
        for words in ("being prepared", "get_meeting_transcript", "try again", "summary"):
            self.assertIn(words, answer["note"])

    def ended_note(self, **fields):
        meeting = meeting_row(CALL, status="ENDED", **fields)
        return call(table_with(segment("a", 1, NOW - 1), meeting=meeting))["note"]

    def test_a_meeting_ended_within_two_hours_says_the_transcript_is_being_prepared(self):
        for age in (0, 60, 7199, 7200, -299):
            with self.subTest(age=age):
                self.assertEqual(self.ended_note(UpdatedAt=iso(NOW - age)), G.ENDED_NOTE)

    def test_a_meeting_whose_transcript_was_deleted_is_not_told_to_try_again(self):
        note = self.ended_note(UpdatedAt=iso(NOW - 30), TranscriptDeletedAt=iso(NOW - 10))

        self.assertEqual(note, G.ENDED_KEPT_NOTE)
        self.assertNotIn("being prepared", note)
        self.assertNotIn("try again", note)
        self.assertIn("get_meeting_transcript", note)

    def test_a_meeting_ended_long_ago_is_not_told_to_try_again(self):
        for updated in (iso(NOW - 7201), iso(NOW - 86_400 * 30), iso(NOW + 3600), "", "soon"):
            with self.subTest(updated=updated):
                self.assertEqual(self.ended_note(UpdatedAt=updated), G.ENDED_KEPT_NOTE)

    def test_a_meeting_with_no_update_time_is_not_told_to_try_again(self):
        self.assertEqual(self.ended_note(), G.ENDED_KEPT_NOTE)

    def test_the_meeting_read_names_the_update_time_and_the_deletion_mark(self):
        self.assertIn("UpdatedAt", G.MEETING_ATTRIBUTES)
        self.assertIn("TranscriptDeletedAt", G.MEETING_ATTRIBUTES)

    def test_a_running_meeting_is_not_ended(self):
        answer = call(table_with(segment("a", 1, NOW - 1)))
        self.assertEqual((answer["status"], answer["ended"]), ("STARTED", False))
        self.assertEqual(answer["meetingId"], CALL)


class TestLines(SettingOn):
    def test_a_line_holds_its_time_speaker_channel_segment_and_text(self):
        meeting = meeting_row(
            CALL, SpeakerIdentities={"identities": {"REMOTE#spk_0": {"name": "Bob"}}}
        )
        rows = [
            segment("a", 75, NOW - 3, speaker="spk_0"),
            segment("b", 3725, NOW - 2, channel="CALLER", speaker="spk_0"),
            segment(
                "c", 80, NOW - 1, channel="AGENT_ASSISTANT", speaker="whoever", text="An answer"
            ),
        ]
        answer = call(table_with(*rows, meeting=meeting))
        self.assertEqual(
            answer["lines"],
            [
                {
                    "t": "01:15",
                    "speaker": "Bob",
                    "channel": "REMOTE",
                    "segmentId": "a",
                    "text": "text of a",
                },
                {
                    "t": "01:20",
                    "speaker": "MeetingAssistant",
                    "channel": "AGENT_ASSISTANT",
                    "segmentId": "c",
                    "text": "An answer",
                },
                {
                    "t": "1:02:05",
                    "speaker": "Bob",
                    "channel": "REMOTE",
                    "segmentId": "b",
                    "text": "text of b",
                },
            ],
        )

    def test_the_meeting_chat_is_one_line_with_its_sender(self):
        rows = [
            segment(
                "m",
                5,
                NOW - 1,
                channel="MEETING_CHAT",
                speaker="Ana\nSilva",
                text="hi\r\n\nthere  all",
            ),
            segment("n", 6, NOW - 1, channel="MEETING_CHAT", speaker="", text="x"),
        ]
        lines = call(table_with(*rows))["lines"]
        self.assertEqual(lines[0]["text"], "hi / there all")
        self.assertEqual(lines[0]["speaker"], "Ana / Silva")
        self.assertEqual(lines[1]["speaker"], "Unknown sender")
        self.assertEqual(lines[0]["channel"], "MEETING_CHAT")

    def test_include_chat_false_reads_and_returns_no_chat(self):
        rows = [segment("m", 5, NOW - 1, channel="MEETING_CHAT"), segment("s", 6, NOW - 1)]
        table = table_with(*rows)
        self.assertEqual(ids(call(table, include_chat=False)), ["s"])
        self.assertFalse(evaluate(table.queries[-1]["FilterExpression"], rows[0]))

    def test_partial_rows_and_other_channels_are_not_lines(self):
        rows = [
            segment("partial", 1, NOW - 1, IsPartial=True),
            segment("category", 2, NOW - 1, channel="CATEGORY_MATCH"),
            segment("agent", 3, NOW - 1, channel="AGENT"),
            segment("local", 4, NOW - 1, channel="LOCAL"),
        ]
        self.assertEqual(ids(call(table_with(*rows))), ["agent", "local"])

    def test_speaker_names_fall_back_to_the_stored_speaker_when_they_fail(self):
        with patch("lma_speakers.speaker_namer", side_effect=RuntimeError("broken")):
            lines = call(table_with(segment("a", 1, NOW - 1, speaker="spk_3")))["lines"]
        self.assertEqual(lines[0]["speaker"], "spk_3")
        with patch("lma_speakers.speaker_namer", return_value=lambda row: 1 / 0):
            lines = call(table_with(segment("a", 1, NOW - 1, speaker="")))["lines"]
        self.assertEqual(lines[0]["speaker"], "Unknown")

    def test_the_stream_audio_name_stands_in_for_the_other_participant(self):
        meeting = meeting_row(CALL, CustomerPhoneNumber="Carla")
        rows = [segment("a", 1, NOW - 1, speaker="Other Participant")]
        self.assertEqual(call(table_with(*rows, meeting=meeting))["lines"][0]["speaker"], "Carla")

    def test_a_time_that_cannot_be_read_shows_dashes(self):
        row = segment("a", 1, NOW - 1)
        row["StartTime"] = "soon"
        row["EndTime"] = None
        self.assertEqual(call(table_with(row))["lines"][0]["t"], "--:--")


# ── the read ────────────────────────────────────────────────────────────────


class EndlessTable(FakeTranscriptTable):
    """A table that always has another page."""

    def query(self, **kwargs):
        self.queries.append(kwargs)
        number = len(self.queries)
        if number > 2 * G.MAX_PAGES:
            raise AssertionError("the read went on past the page cap")
        return {"Items": [], "LastEvaluatedKey": {"PK": f"trs#{CALL}", "SK": f"s#{number:05d}"}}


class TestTheRead(SettingOn):
    def test_every_page_is_read(self):
        rows = [segment(f"s{i:03d}", start=i, created=NOW - 500 + i) for i in range(120)]
        table = table_with(*rows, page_rows=25)
        answer = call(table, cursor=G.encode_cursor(CALL, NOW - 600), max_lines=1000)
        self.assertEqual(len(answer["lines"]), 120)
        self.assertEqual(len(table.queries), 5)

    def test_only_the_attributes_used_are_projected(self):
        table = table_with(segment("a", 1, NOW - 1, ExpiresAfter=1, Sentiment="POSITIVE"))
        call(table)
        query = table.queries[0]
        names = query["ExpressionAttributeNames"]
        projected = {names[token.strip()] for token in query["ProjectionExpression"].split(",")}
        self.assertEqual(projected, set(G.TRANSCRIPT_ATTRIBUTES))
        self.assertNotIn("IsPartial", projected)

    def test_the_page_cap_refuses_with_no_cursor(self):
        table = EndlessTable(meeting_row(CALL))
        with self.assertRaises(ValueError) as raised:
            call(table)
        self.assertEqual(str(raised.exception), G.TOO_LARGE_MESSAGE)
        self.assertIn("get_meeting_transcript", G.TOO_LARGE_MESSAGE)
        self.assertEqual(len(table.queries), G.MAX_PAGES)

    def test_the_time_budget_refuses_between_pages(self):
        ticks = iter([0.0, 5.0, 10.0, 21.0, 30.0])
        table = EndlessTable(meeting_row(CALL))
        with self.assertRaises(ValueError) as raised:
            call(table, timer=lambda: next(ticks))
        self.assertEqual(str(raised.exception), G.TOO_LARGE_MESSAGE)
        self.assertEqual(len(table.queries), 3)

    def test_a_slow_last_page_is_refused_too(self):
        ticks = iter([0.0, 20.5, 21.0])
        table = table_with(segment("a", 1, NOW - 1))
        with self.assertRaises(ValueError) as raised:
            call(table, timer=lambda: next(ticks))
        self.assertEqual(str(raised.exception), G.TOO_LARGE_MESSAGE)
        self.assertEqual(len(table.queries), 1)

    def test_two_pages_that_together_pass_the_budget_are_refused(self):
        rows = [segment(f"s{i:02d}", i, NOW - 50 + i) for i in range(30)]
        ticks = iter([0.0, 19.0, 38.0, 39.0])
        with self.assertRaises(ValueError) as raised:
            call(table_with(*rows, page_rows=25), timer=lambda: next(ticks))
        self.assertEqual(str(raised.exception), G.TOO_LARGE_MESSAGE)

    def test_the_work_after_the_read_counts_in_the_budget(self):
        ticks = iter([0.0, 19.0, 20.5])
        with self.assertRaises(ValueError) as raised:
            call(table_with(segment("a", 1, NOW - 1)), timer=lambda: next(ticks))
        self.assertEqual(str(raised.exception), G.TOO_LARGE_MESSAGE)

    def test_the_budget_starts_with_the_call_not_with_the_read(self):
        clock = {"now": 0.0}

        class Slow(FakeTranscriptTable):
            def get_item(self, **kwargs):
                clock["now"] += 15.0  # the meeting row took 15 seconds
                return super().get_item(**kwargs)

            def query(self, **kwargs):
                clock["now"] += 6.0
                return super().query(**kwargs)

        table = Slow(meeting_row(CALL))
        table.put(segment("a", 1, NOW - 1))
        with self.assertRaises(ValueError) as raised:
            call(table, timer=lambda: clock["now"])
        self.assertEqual(str(raised.exception), G.TOO_LARGE_MESSAGE)

    def test_an_answer_made_inside_the_budget_is_returned(self):
        ticks = iter([0.0, 19.0, 19.9])
        answer = call(table_with(segment("a", 1, NOW - 1)), timer=lambda: next(ticks))
        self.assertEqual(ids(answer), ["a"])

    def test_a_page_key_that_repeats_is_an_error_not_a_loop(self):
        class Stuck(FakeTranscriptTable):
            def query(self, **kwargs):
                self.queries.append(kwargs)
                return {"Items": [], "LastEvaluatedKey": {"PK": "p", "SK": "s"}}

        table = Stuck(meeting_row(CALL))
        with self.assertRaises(ValueError) as raised:
            call(table)
        self.assertEqual(str(raised.exception), G.TRANSCRIPT_UNREADABLE)
        self.assertEqual(len(table.queries), 2)

    def test_a_failed_query_has_a_fixed_message(self):
        class Failing(FakeTranscriptTable):
            def query(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "ResourceNotFoundException", "Message": SECRET}}, "Query"
                )

        with self.assertRaises(ValueError) as raised:
            call(Failing(meeting_row(CALL)))
        self.assertEqual(str(raised.exception), G.TRANSCRIPT_UNREADABLE)

    def test_a_failed_meeting_read_has_a_fixed_message(self):
        class Failing(FakeTranscriptTable):
            def get_item(self, **kwargs):
                raise RuntimeError(SECRET)

        with self.assertRaises(ValueError) as raised:
            call(Failing())
        self.assertEqual(str(raised.exception), G.MEETING_UNREADABLE)

    def test_the_table_is_the_calls_table_by_default(self):
        table = table_with(segment("a", 1, NOW - 1))
        resource = SimpleNamespace(Table=lambda name: table if name == "test-calls-table" else None)
        with patch.dict(os.environ, {"CALLS_TABLE": "test-calls-table"}):
            with patch("boto3.resource", return_value=resource):
                answer = G.execute(meeting_id=CALL, user_id=ME)
        self.assertEqual(ids(answer), ["a"])


# ── logs ────────────────────────────────────────────────────────────────────


class TestLogs(SettingOn):
    WORDS = "the confidential merger price is forty two"

    def _logs(self, table, **kwargs):
        with self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger().debug("start")
            try:
                call(table, **kwargs)
            except (ValueError, PermissionError):
                pass
        return "\n".join(captured.output)

    def test_logs_hold_a_digest_and_counts_never_text_names_or_the_id(self):
        meeting = meeting_row(
            CALL,
            MeetingTopic="Project Falcon",
            SpeakerIdentities={"identities": {"REMOTE#spk_0": {"name": "Beatriz Quintana"}}},
        )
        rows = [
            segment("a", 1, NOW - 1, text=self.WORDS),
            segment("b", 2, NOW - 1, channel="MEETING_CHAT", speaker="Ana Silva", text=self.WORDS),
        ]
        output = self._logs(table_with(*rows, meeting=meeting))
        for secret in (self.WORDS, "Beatriz", "Ana Silva", "Project Falcon", CALL):
            self.assertNotIn(secret, output)
        self.assertIn(G.meeting_digest(CALL), output)
        self.assertEqual(len(G.meeting_digest(CALL)), 12)

    def test_error_logs_hold_no_id_and_no_cause_text(self):
        class Failing(FakeTranscriptTable):
            def query(self, **kwargs):
                raise ClientError({"Error": {"Code": "X", "Message": SECRET}}, "Query")

        for table in (
            Failing(meeting_row(CALL)),
            table_with(meeting=meeting_row(CALL, owner=OTHER)),
        ):
            output = self._logs(table)
            self.assertNotIn(CALL, output)
            self.assertNotIn(SECRET, output)

    def test_speaker_name_failures_log_the_class_only(self):
        with patch("lma_speakers.speaker_namer", side_effect=RuntimeError(self.WORDS)):
            output = self._logs(table_with(segment("a", 1, NOW - 1)))
        self.assertIn("RuntimeError", output)
        self.assertNotIn(self.WORDS, output)


# ── cursors with any JSON in them ───────────────────────────────────────────

HUGE = int("1" + "0" * 400)
CURSOR_MESSAGES = {G.CURSOR_INVALID, G.CURSOR_OLD, G.CURSOR_OTHER_MEETING}


def random_json(rng, depth=0):
    kind = rng.randrange(9 if depth < 4 else 7)
    if kind == 0:
        return None
    if kind == 1:
        return rng.random() < 0.5
    if kind == 2:
        return rng.choice([0, -1, 2, NOW, NOW + 10_000, HUGE, -HUGE, 10**308 * 10, 2**63])
    if kind == 3:
        return rng.choice([0.5, -0.0, NOW - 1.5, 1e308, -1e308, float("nan"), float("inf")])
    if kind == 4:
        return rng.choice(["", CALL, "x" * 50, "\x00", "\ud83d", "2", "NaN"])
    if kind == 5:
        return rng.randrange(-5, 5) * 1e300
    if kind == 6:
        return int("9" * rng.randrange(1, 420))
    if kind == 7:
        return [random_json(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {
        rng.choice(["v", "m", "w", "n", "a", "x"]): random_json(rng, depth + 1)
        for _ in range(rng.randrange(4))
    }


class TestCursorsWithAnyContent(unittest.TestCase):
    def assertInvalid(self, cursor):
        with self.assertRaises(ValueError) as raised:
            G.parse_cursor(cursor, CALL, NOW)
        self.assertIs(type(raised.exception), ValueError)
        self.assertEqual(str(raised.exception), G.CURSOR_INVALID)

    def test_a_400_digit_watermark_is_a_bad_cursor_not_an_overflow(self):
        self.assertInvalid(cursor_of(w=HUGE))
        self.assertInvalid(cursor_of(w=-HUGE))

    def test_a_400_digit_walk_clock_is_a_bad_cursor_not_an_overflow(self):
        self.assertInvalid(cursor_of(w=NOW - 10, n=HUGE, a=[NOW - 20, "s"]))

    def test_a_400_digit_last_line_time_is_a_bad_cursor_not_an_overflow(self):
        self.assertInvalid(cursor_of(w=NOW - 10, n=NOW, a=[HUGE, "s"]))
        self.assertInvalid(cursor_of(w=NOW - 10, n=NOW, a=[-HUGE, "s"]))

    def test_a_huge_number_through_the_tool_gives_the_cursor_message(self):
        table = table_with(segment("a", 1, NOW - 1))
        with patch.object(G, "live_context_state", return_value=G.ON):
            for state in ({"w": HUGE}, {"w": NOW - 5, "n": HUGE, "a": [NOW - 6, "a"]}):
                with self.subTest(state=list(state)):
                    with self.assertRaises(ValueError) as raised:
                        call(table, cursor=cursor_of(**state))
                    self.assertEqual(str(raised.exception), G.CURSOR_INVALID)
        self.assertEqual(table.queries, [])

    def test_lists_nested_as_deep_as_the_length_cap_allows_are_a_bad_cursor(self):
        depth = 1500
        raw = '{"v":2,"m":"' + CALL + '","w":' + "[" * depth + "]" * depth + "}"
        self.assertInvalid(base64.urlsafe_b64encode(raw.encode()).decode())

    def test_random_json_in_every_field_only_ever_gives_the_fixed_cursor_errors(self):
        import random

        rng = random.Random(20261119)
        accepted = refused = 0
        for _ in range(4000):
            if rng.random() < 0.15:
                state = random_json(rng)
            else:
                state = {"v": 2, "m": CALL}
                for name in ("v", "m", "w", "n", "a", "x"):
                    if rng.random() < 0.5:
                        state[name] = random_json(rng)
                if rng.random() < 0.5:
                    state["a"] = [random_json(rng), random_json(rng)]
            try:
                text = json.dumps(state)
            except (TypeError, ValueError):
                continue
            cursor = base64.urlsafe_b64encode(text.encode()).decode()
            try:
                parsed = G.parse_cursor(cursor, CALL, NOW)
            except ValueError as error:
                self.assertIs(type(error), ValueError, text[:200])
                self.assertIn(str(error), CURSOR_MESSAGES, text[:200])
                refused += 1
            else:
                accepted += 1
                self.assertIsInstance(parsed, dict)
                self.assertTrue(0 <= parsed["w"] <= NOW + G.FUTURE_LIMIT_SECONDS)
        self.assertGreater(refused, 3000)
        self.assertGreater(accepted, 0)

    def test_random_bytes_only_ever_give_the_fixed_cursor_error(self):
        import random

        rng = random.Random(7)
        for _ in range(2000):
            raw = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 300)))
            cursor = rng.choice(
                [
                    base64.urlsafe_b64encode(raw).decode(),
                    raw.decode("latin-1"),
                    base64.b64encode(raw).decode(),
                ]
            )
            try:
                G.parse_cursor(cursor, CALL, NOW)
            except ValueError as error:
                self.assertIs(type(error), ValueError)
                self.assertIn(str(error), CURSOR_MESSAGES)


# ── the size of an answer ───────────────────────────────────────────────────


def http_body(answer):
    """The HTTP body index.lambda_handler makes of an answer."""
    text = json.dumps(answer, indent=2, default=str)
    message = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}
    return json.dumps(message)


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
    """The bytes a text adds to the HTTP body: written as JSON by index.py, then again."""
    return len(json.dumps(json.dumps(text))) - len(json.dumps(json.dumps("")))


class TestUnitsBoundTheBody(SettingOn):
    """No character takes more than 7/3 bytes of the body for each unit it is counted."""

    def test_any_text_is_bounded_by_its_units(self):
        import random

        chooser = random.Random(21)
        for case in range(1000):
            text = random_text(chooser, 400)
            with self.subTest(case=case):
                self.assertLessEqual(3 * escaped_size(text), 7 * G.units_of(text), repr(text[:40]))

    def test_each_character_that_json_escapes_is_bounded(self):
        chars = ['"', "\\", "\x7f", "\ud800", "\udfff", "\u2028", "\uffff", "\U0010ffff", "é"]
        chars += [chr(code) for code in range(0x20)]
        for char in chars:
            with self.subTest(char=repr(char)):
                self.assertLessEqual(3 * escaped_size(char * 10), 7 * G.units_of(char * 10))

    def test_del_counts_three_units(self):
        self.assertEqual(G.units_of("\x7f"), 3)
        self.assertEqual(G.units_of("a\x7fb"), 5)

    def test_lines_of_del_stay_under_100_kb(self):
        # 60 lines of 3,000 DEL characters with a speaker of 300 once gave 202,769 bytes.
        rows = [
            segment(f"d{i:03d}", i, NOW - 50 + i * 0.1, text="\x7f" * 3000, speaker="\x7f" * 300)
            for i in range(60)
        ]
        rows += [
            segment("\x7f" * 40 + f"{i:03d}", 100 + i, NOW - 40 + i * 0.01, text="\x7f" * 100)
            for i in range(300)
        ]
        table = table_with(*rows, page_rows=100)
        cursor, seen, calls = G.encode_cursor(CALL, NOW - 55), [], 0
        while True:
            calls += 1
            answer = call(table, cursor=cursor, max_lines=1000)
            self.assertLess(len(http_body(answer)), 100_000)
            seen += ids(answer)
            cursor = answer["nextCursor"]
            if not answer["truncated"]:
                break
            self.assertLess(calls, 100)
        self.assertEqual(len(seen), 360)

    def test_any_lines_stay_within_their_units_and_under_100_kb(self):
        import random

        chooser = random.Random(22)
        for case in range(1000):
            # Mostly small answers, one in ten large enough to be cut short.
            count, longest = (200, 2500) if case % 10 == 0 else (8, 150)
            rows = [
                segment(
                    f"{i:04d}" + random_text(chooser, 40),
                    i,
                    NOW - 50 + i * 0.01,
                    text=random_text(chooser, longest),
                    speaker=random_text(chooser, 60) or "spk_0",
                    channel=chooser.choice(["REMOTE", "LOCAL", "MEETING_CHAT", "AGENT_ASSISTANT"]),
                )
                for i in range(chooser.randint(1, count))
            ]
            with self.subTest(case=case):
                answer = call(
                    table_with(*rows, page_rows=500),
                    cursor=G.encode_cursor(CALL, NOW - 55),
                    max_lines=1000,
                )
                units = sum(
                    G.LINE_UNITS + sum(G.units_of(value) for value in line.values())
                    for line in answer["lines"]
                )
                if len(answer["lines"]) > 1:
                    self.assertLessEqual(units, G.MAX_PAGE_UNITS)
                empty = dict(answer, lines=[])
                lines_bytes = len(http_body(answer)) - len(http_body(empty))
                self.assertLessEqual(3 * lines_bytes, 7 * units)
                self.assertLess(len(http_body(answer)), 100_000)


class TestAnswerSize(SettingOn):
    UUID = "0123abcd-4567-89ef-0123-456789abcdef"

    def _first_page(self, text, count, max_lines=1000, speaker="spk_0", channel="REMOTE"):
        rows = [
            segment(
                f"{self.UUID[:-4]}{i:04d}",
                i,
                NOW - 50 + i * 0.01,
                text=text,
                speaker=speaker,
                channel=channel,
            )
            for i in range(count)
        ]
        return call(table_with(*rows), cursor=G.encode_cursor(CALL, NOW - 55), max_lines=max_lines)

    def test_units_count_what_the_encoder_writes(self):
        self.assertEqual(G.units_of("abc"), 3)
        self.assertEqual(G.units_of("é中"), 6)
        self.assertEqual(G.units_of("🙂"), 6)
        self.assertEqual(G.units_of('"\\\n\x01'), 12)

    def test_166_lines_of_120_characters_stay_under_100_kb_in_every_script(self):
        for name, char in (
            ("ASCII", "a"),
            ("accented", "é"),
            ("Cyrillic", "ж"),
            ("CJK", "中"),
            ("emoji", "🙂"),
            ("quotes", '"'),
            ("backslashes", "\\"),
            ("control", "\x01"),
            ("line breaks", "\n"),
        ):
            with self.subTest(script=name):
                answer = self._first_page(char * 120, 166)
                size = len(http_body(answer))
                self.assertLess(size, 100_000)
                self.assertTrue(answer["truncated"] or len(answer["lines"]) == 166)

    def test_1000_short_lines_stay_under_100_kb_and_the_rest_follows(self):
        answer = self._first_page("ok", 1000)
        self.assertLess(len(http_body(answer)), 100_000)
        self.assertTrue(answer["truncated"])
        self.assertLess(len(answer["lines"]), 1000)

    def test_the_largest_lines_of_any_content_stay_under_100_kb(self):
        for char in ("🙂", "\x01", '"', "中"):
            with self.subTest(char=repr(char)):
                answer = self._first_page(char * 5000, 30, speaker=char * 5000)
                self.assertLess(len(http_body(answer)), 100_000)
                chat = self._first_page(
                    char * 5000, 30, speaker=char * 5000, channel="MEETING_CHAT"
                )
                self.assertLess(len(http_body(chat)), 100_000)

    def test_a_single_line_of_the_largest_size_is_still_returned(self):
        answer = self._first_page("🙂" * 5000, 3, speaker="🙂" * 5000)
        self.assertGreaterEqual(len(answer["lines"]), 1)
        self.assertLess(len(http_body(answer)), 100_000)

    def test_a_long_speaker_name_is_cut_and_marked(self):
        answer = self._first_page("hi", 1, speaker="n" * 500, channel="MEETING_CHAT")
        self.assertEqual(answer["lines"][0]["speaker"], "n" * 200 + G.CUT_MARK)

    def test_a_walk_through_cjk_and_emoji_lines_has_no_gap_and_no_repeat(self):
        rows = [
            segment(f"s{i:03d}", i, NOW - 50 + i * 0.1, text=("中" if i % 2 else "🙂") * 300)
            for i in range(120)
        ]
        table = table_with(*rows, page_rows=40)
        cursor, seen, calls = G.encode_cursor(CALL, NOW - 55), [], 0
        while True:
            calls += 1
            answer = call(table, cursor=cursor, max_lines=200)
            self.assertLess(len(http_body(answer)), 100_000)
            seen += ids(answer)
            cursor = answer["nextCursor"]
            if not answer["truncated"]:
                break
            self.assertLess(calls, 50)
        self.assertEqual(seen, [f"s{i:03d}" for i in range(120)])
        self.assertGreater(calls, 3)


# ── rows with no readable write time ────────────────────────────────────────


class TestPlacingRowsWithNoWriteTime(SettingOn):
    START = T0  # the meeting row's CreatedAt in meeting_row()

    def test_a_row_with_no_time_is_placed_at_the_meeting_start_plus_its_end(self):
        rows = [
            {"StartTime": 1, "EndTime": 3, "SK": "s#a", "CreatedAt": iso(self.START + 4)},
            {"StartTime": 400, "EndTime": 402, "SK": "s#b"},
        ]
        self.assertEqual(G.write_times(rows, NOW, self.START), [self.START + 4, self.START + 402])

    def test_the_placement_is_never_before_the_nearest_earlier_row(self):
        rows = [
            {"StartTime": 1, "EndTime": 3, "SK": "s#a", "CreatedAt": iso(self.START + 50)},
            {"StartTime": 4, "EndTime": 5, "SK": "s#b"},
        ]
        self.assertEqual(G.write_times(rows, NOW, self.START), [self.START + 50, self.START + 50])

    def test_the_placement_is_clamped_to_the_clock(self):
        rows = [{"StartTime": 7200, "EndTime": 7300, "SK": "s#a", "CreatedAt": "garbage"}]
        self.assertEqual(G.write_times(rows, NOW, self.START), [NOW])

    def test_with_no_meeting_start_the_nearest_earlier_row_decides_as_before(self):
        rows = [
            {"StartTime": 1, "SK": "s#a", "CreatedAt": iso(NOW - 100)},
            {"StartTime": 400, "SK": "s#b"},
        ]
        self.assertEqual(G.write_times(rows, NOW, None), [NOW - 100, NOW - 100])

    def test_the_placement_is_the_same_on_every_call(self):
        rows = [{"StartTime": 10, "EndTime": 12, "SK": "s#a"}]
        first = G.write_times(rows, NOW, self.START)
        self.assertEqual(G.write_times(rows, NOW + 500, self.START), first)

    def test_a_row_with_no_time_stored_after_a_pause_is_delivered_once(self):
        # Spoken 3,000 s into the meeting, after a pause of 10 minutes; the row
        # before the pause was written long ago.
        table = table_with(segment("before", 2390, self.START + 2393))
        moment = self.START + 3010
        first = call(table, cursor=None, now=lambda: moment)
        self.assertEqual(ids(first), ["before"])
        table.put(segment("after", 3000, None))  # stored at about START + 3012
        second = call(table, cursor=first["nextCursor"], now=lambda: moment + 5)
        self.assertEqual(ids(second), ["after"])
        # Like any line, it comes again inside the next answer's 60 seconds, then not.
        third = call(table, cursor=second["nextCursor"], now=lambda: moment + 70)
        self.assertEqual(ids(third), ["after"])
        fourth = call(table, cursor=third["nextCursor"], now=lambda: moment + 75)
        self.assertEqual(ids(fourth), [])

    def test_the_meeting_start_is_read_with_the_meeting_row(self):
        table = table_with(segment("a", 1, NOW - 1))
        meeting = G.read_meeting(table, CALL, "digest")
        self.assertEqual(meeting["CreatedAt"], "2026-11-19T09:00:00.000Z")


class TestTheMeetingRead(SettingOn):
    def test_the_meeting_row_is_read_strongly_consistent(self):
        sent = []

        class Recording(FakeTranscriptTable):
            def get_item(self, **kwargs):
                sent.append(kwargs)
                return super().get_item(**kwargs)

        table = Recording(meeting_row(CALL))
        table.put(segment("a", 1, NOW - 1))
        call(table)
        self.assertEqual(len(sent), 1)
        self.assertIs(sent[0].get("ConsistentRead"), True)

    def test_an_objection_seen_only_by_a_consistent_read_withholds_the_transcript(self):
        allowed = meeting_row(CALL)
        objected = meeting_row(CALL, Consent={"Mode": "opt_in", "Rule": "owner_attests"})

        class Lagging(FakeTranscriptTable):
            """A replica that has not yet seen the objection."""

            def get_item(self, Key, ConsistentRead=False, **kwargs):
                self.gets.append(dict(Key))
                return {"Item": objected if ConsistentRead else allowed}

        table = Lagging()
        table.put(segment("a", 1, NOW - 1))
        with self.assertRaises(PermissionError) as raised:
            call(table)
        self.assertEqual(str(raised.exception), consent_gate.CAPTURE_NOT_ALLOWED_MESSAGE)
        self.assertEqual(table.queries, [])


if __name__ == "__main__":
    unittest.main()
