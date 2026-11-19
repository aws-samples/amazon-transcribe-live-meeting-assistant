# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""get_meeting_agenda: the numbered items, where they came from, who may read them.

The consent gate runs for real (its rule is the configuration layer's). A meeting's
own Consent record decides wherever a test needs the gate to say yes or no; where a
test needs the deployment's mode, ``deployment_consent_mode`` is replaced.
"""

import json
import os
import sys
import unittest
from pathlib import Path
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

from tools import consent_gate, get_meeting_agenda  # noqa: E402
from tools.text_budget import units_of  # noqa: E402

OWNER = "owner@example.com"
FRIEND = "friend@example.com"
OTHER = "other@example.com"
SECRET_ARN = "arn:aws:dynamodb:us-east-1:123456789012:table/secret-calls-table"

AGENDA = "1. Budget review\n- Hiring plan\n* Roadmap\n• Risks\na) Any other business"
DENIED = {"Mode": "opt_in", "Rule": "each_participant"}  # nobody has consented
ALLOWED = {"Mode": "off"}


def meeting(call_id="call-1", **fields):
    row = {"PK": f"c#{call_id}", "SK": f"c#{call_id}", "Owner": OWNER}
    row.update(fields)
    return row


class FakeTable:
    name = "test-calls-table"

    def __init__(self, *rows, error=None):
        self.rows = {row["PK"]: row for row in rows}
        self.error = error
        self.reads = []

    def get_item(self, **kwargs):
        self.reads.append(kwargs)
        if self.error:
            raise self.error
        row = self.rows.get(kwargs["Key"]["PK"])
        return {"Item": row} if row else {}


def run(table, meeting_id="call-1", user_id=OWNER, is_admin=False):
    resource = MagicMock()
    resource.Table.return_value = table
    with patch("boto3.resource", return_value=resource) as factory:
        result = get_meeting_agenda.execute(meeting_id, user_id=user_id, is_admin=is_admin)
    factory.assert_called_once_with("dynamodb")
    resource.Table.assert_called_once_with("test-calls-table")
    return result


class AgendaItemsTest(unittest.TestCase):
    def test_items_are_numbered_from_one_with_list_marks_removed(self):
        result = run(FakeTable(meeting(Agenda=AGENDA, AgendaSource="ui")))

        self.assertTrue(result["hasAgenda"])
        self.assertEqual(
            result["items"],
            [
                {"number": 1, "text": "Budget review"},
                {"number": 2, "text": "Hiring plan"},
                {"number": 3, "text": "Roadmap"},
                {"number": 4, "text": "Risks"},
                {"number": 5, "text": "Any other business"},
            ],
        )
        self.assertEqual(result["meetingId"], "call-1")

    def test_blank_lines_and_crlf_line_ends_are_not_items(self):
        agenda = "First\r\n\r\n   \r\nSecond\r\n"

        result = run(FakeTable(meeting(Agenda=agenda, AgendaSource="mcp")))

        self.assertEqual([i["text"] for i in result["items"]], ["First", "Second"])

    def test_only_a_line_break_splits_items_not_every_unicode_separator(self):
        # The meeting page splits on \r?\n only, so both number an agenda alike.
        agenda = "One\u2028still one\x85still one\u2029still one\nTwo"

        result = run(FakeTable(meeting(Agenda=agenda, AgendaSource="ui")))

        self.assertEqual(
            [i["text"] for i in result["items"]],
            ["One\u2028still one\x85still one\u2029still one", "Two"],
        )

    def test_a_control_character_inside_an_item_is_shown_as_a_space(self):
        # Written out it would cost six times what one character stands for.
        agenda = "One\x0btwo\x0cthree\x01four\nFive\tsix"

        result = run(FakeTable(meeting(Agenda=agenda, AgendaSource="ui")))

        self.assertEqual([i["text"] for i in result["items"]], ["One two three four", "Five\tsix"])

    def test_a_mark_needs_a_space_after_it_to_be_removed(self):
        agenda = "-5 degrees\n2024 plan\n3.5 percent\n12) twelve\n1234. four digits"

        result = run(FakeTable(meeting(Agenda=agenda, AgendaSource="ui")))

        self.assertEqual(
            [i["text"] for i in result["items"]],
            ["-5 degrees", "2024 plan", "3.5 percent", "twelve", "1234. four digits"],
        )

    def test_a_long_agenda_is_numbered_to_its_last_item(self):
        agenda = "\n".join(f"Item {n}" for n in range(1, 101))

        result = run(FakeTable(meeting(Agenda=agenda, AgendaSource="ui")))

        self.assertEqual(len(result["items"]), 100)
        self.assertEqual(result["items"][-1], {"number": 100, "text": "Item 100"})
        self.assertNotIn("truncated", result)

    def test_the_answer_serializes_without_help(self):
        result = run(FakeTable(meeting(Agenda=AGENDA, AgendaSource="scheduled")))

        self.assertEqual(json.loads(json.dumps(result)), result)


class ConsistentReadTest(unittest.TestCase):
    def test_the_meeting_row_is_read_consistently_so_an_objection_is_seen_at_once(self):
        table = FakeTable(meeting(Agenda="One", AgendaSource="derived", Consent=ALLOWED))

        run(table)

        self.assertEqual(len(table.reads), 1)
        self.assertIs(table.reads[0].get("ConsistentRead"), True)

    def test_a_meeting_that_is_missing_is_read_consistently_too(self):
        table = FakeTable()

        with self.assertRaises(PermissionError):
            run(table)

        self.assertIs(table.reads[0].get("ConsistentRead"), True)


def on_the_wire(result):
    """The size of the answer as the endpoint sends it: written by index.py, then wrapped."""
    text = json.dumps(result, indent=2, default=str)
    body = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}
    return len(json.dumps(body).encode())


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


class EscapedSizeTest(unittest.TestCase):
    def test_any_agenda_costs_at_most_four_bytes_a_unit_on_the_wire(self):
        import random

        chooser = random.Random(12)
        for case in range(1000):
            # Mostly short agendas, one in ten long enough to be cut.
            longest, count = (1000, 60) if case % 10 == 0 else (200, 20)
            lines = [random_text(chooser, longest) for _ in range(chooser.randint(1, count))]
            agenda = "\n".join(lines)
            with self.subTest(case=case):
                result = run(FakeTable(meeting(Agenda=agenda, AgendaSource="ui")))
                if not result["hasAgenda"]:
                    continue
                used = sum(units_of(item["text"]) for item in result["items"])
                blank = json.loads(json.dumps(result))
                for item in blank["items"]:
                    item["text"] = ""
                self.assertLessEqual(used, get_meeting_agenda.TEXT_BUDGET)
                self.assertLessEqual(on_the_wire(result) - on_the_wire(blank), 4 * used)
                self.assertLess(on_the_wire(result), 100_000)


class BoundedAnswerTest(unittest.TestCase):
    ALPHABETS = {
        "ascii": "a",
        "quote": '"',
        "backslash": "\\",
        "accented": "é",
        "cyrillic": "ж",
        "cjk": "中",
        "emoji": "😀",
        "tab": "\t",
        "control": "\x01",
    }

    def read(self, agenda, **fields):
        fields.setdefault("AgendaSource", "ui")
        return run(FakeTable(meeting(Agenda=agenda, **fields)))

    def test_an_agenda_that_fits_has_no_truncated_flag_and_no_note(self):
        result = self.read(AGENDA)

        self.assertEqual(
            set(result), {"meetingId", "hasAgenda", "items", "source", "sourceDescription"}
        )
        for item in result["items"]:
            self.assertEqual(set(item), {"number", "text"})

    def test_an_agenda_of_exactly_the_most_items_is_whole(self):
        result = self.read("\n".join(f"Item {n}" for n in range(1, 101)))

        self.assertEqual(len(result["items"]), get_meeting_agenda.MAX_ITEMS)
        self.assertNotIn("truncated", result)
        self.assertNotIn("note", result)

    def test_one_item_more_is_left_out_and_the_answer_says_so(self):
        result = self.read("\n".join(f"Item {n}" for n in range(1, 102)))

        self.assertEqual(len(result["items"]), 100)
        self.assertEqual(result["items"][-1], {"number": 100, "text": "Item 100"})
        self.assertIs(result["truncated"], True)
        self.assertIn("101 items", result["note"])
        self.assertIn("first 100", result["note"])
        self.assertNotIn("shortened", result["note"])

    def test_ten_thousand_one_character_lines_are_cut_to_the_most_items(self):
        result = self.read("\n".join("x" for _ in range(10_000)))

        self.assertEqual(len(result["items"]), 100)
        self.assertEqual([i["number"] for i in result["items"]], list(range(1, 101)))
        self.assertIs(result["truncated"], True)
        self.assertIn("10000 items", result["note"])
        self.assertLess(on_the_wire(result), 100_000)

    def test_the_old_size_of_that_agenda_is_gone(self):
        result = self.read("\n".join("x" for _ in range(5_000)))

        self.assertLess(on_the_wire(result), 20_000)

    def test_an_item_longer_than_the_text_budget_is_cut_and_marked(self):
        result = self.read("short\n" + "a" * 30_000 + "\nlast")

        short, long, last = result["items"]
        self.assertEqual(short, {"number": 1, "text": "short"})
        self.assertEqual(last, {"number": 3, "text": "last"})
        self.assertIs(long["textTruncated"], True)
        self.assertEqual(len(long["text"]), 20_000 - len("short") - len("last"))
        self.assertIs(result["truncated"], True)
        self.assertIn("textTruncated=true", result["note"])
        self.assertNotIn("left out", result["note"])

    def test_the_texts_of_all_items_share_the_budget_by_the_cost_of_a_character(self):
        result = self.read("\n".join("中" * 300 for _ in range(100)))

        units = sum(units_of(i["text"]) for i in result["items"])
        self.assertLessEqual(units, 20_000)
        self.assertTrue(all(i["textTruncated"] for i in result["items"]))

    def test_the_worst_agenda_of_any_alphabet_stays_under_one_hundred_kilobytes(self):
        for name, char in self.ALPHABETS.items():
            for lines, width in ((100, 200), (100, 2_000), (10_000, 1), (1, 200_000), (5, 40_000)):
                with self.subTest(alphabet=name, lines=lines, width=width):
                    result = self.read(
                        "\n".join(("z" + char * width)[:width] for _ in range(lines))
                    )

                    self.assertLess(on_the_wire(result), 100_000)

    def test_an_agenda_that_is_withheld_is_still_withheld_when_it_is_long(self):
        row = meeting(
            Agenda="\n".join("x" for _ in range(500)), AgendaSource="derived", Consent=DENIED
        )

        with self.assertRaises(PermissionError) as caught:
            run(FakeTable(row))

        self.assertEqual(str(caught.exception), consent_gate.CAPTURE_NOT_ALLOWED_MESSAGE)


class SourceTest(unittest.TestCase):
    def test_each_known_source_is_returned_with_its_description(self):
        expected = {
            "scheduled": "given when the Virtual Participant was started or scheduled",
            "mcp": "passed by an MCP client when it started or scheduled the Virtual Participant",
            "ui": "set on the meeting page",
            "derived": "read from the meeting transcript",
        }
        for source, description in expected.items():
            with self.subTest(source=source):
                row = meeting(Agenda="One", AgendaSource=source, Consent=ALLOWED)

                result = run(FakeTable(row))

                self.assertEqual(result["source"], source)
                self.assertEqual(result["sourceDescription"], description)

    def test_a_source_that_is_not_recorded_is_null_and_not_echoed(self):
        for stored in (None, "", "elsewhere", "UI", ["ui"], 7, {"S": "ui"}):
            with self.subTest(stored=stored):
                fields = {"Agenda": "One", "Consent": ALLOWED}
                if stored is not None:
                    fields["AgendaSource"] = stored
                result = run(FakeTable(meeting(**fields)))

                self.assertTrue(result["hasAgenda"])
                self.assertIsNone(result["source"])
                self.assertEqual(
                    result["sourceDescription"], "the source of this agenda is not recorded"
                )
                self.assertNotIn("elsewhere", json.dumps(result))


class NoAgendaTest(unittest.TestCase):
    def test_a_meeting_without_an_agenda_says_so(self):
        for stored in (None, "", "  \n \r\n", "- \n* ", 42, ["a"], {"x": 1}):
            with self.subTest(stored=stored):
                fields = {} if stored is None else {"Agenda": stored}
                result = run(FakeTable(meeting(**fields, AgendaSource="ui")))

                self.assertEqual(
                    result,
                    {
                        "meetingId": "call-1",
                        "hasAgenda": False,
                        "items": [],
                        "source": None,
                        "sourceDescription": get_meeting_agenda.NO_AGENDA,
                    },
                )
        self.assertIn("do not make one up", get_meeting_agenda.NO_AGENDA)

    def test_a_meeting_without_an_agenda_is_not_gated_because_nothing_is_withheld(self):
        row = meeting(Consent=DENIED)

        with patch.object(consent_gate, "require_capture_allowed") as gate:
            result = run(FakeTable(row))

        gate.assert_not_called()
        self.assertFalse(result["hasAgenda"])


class ConsentTest(unittest.TestCase):
    def test_an_agenda_a_person_supplied_is_not_gated(self):
        for source in ("scheduled", "mcp", "ui"):
            with self.subTest(source=source):
                row = meeting(Agenda=AGENDA, AgendaSource=source, Consent=DENIED)

                with patch.object(consent_gate, "require_capture_allowed") as gate:
                    result = run(FakeTable(row))

                gate.assert_not_called()
                self.assertEqual(len(result["items"]), 5)

    def test_a_person_supplied_agenda_is_returned_when_capture_is_not_allowed(self):
        # No mock: the real gate would refuse this meeting, and is not asked.
        self.assertFalse(consent_gate.capture_allowed(meeting(Consent=DENIED)))
        row = meeting(Agenda="One", AgendaSource="ui", Consent=DENIED)

        self.assertTrue(run(FakeTable(row))["hasAgenda"])

    def test_a_derived_agenda_is_withheld_when_capture_is_not_allowed(self):
        row = meeting(Agenda=AGENDA, AgendaSource="derived", Consent=DENIED)

        with self.assertRaises(PermissionError) as caught:
            run(FakeTable(row))

        self.assertEqual(str(caught.exception), consent_gate.CAPTURE_NOT_ALLOWED_MESSAGE)

    def test_a_derived_agenda_is_returned_when_capture_is_allowed(self):
        row = meeting(Agenda=AGENDA, AgendaSource="derived", Consent=ALLOWED)

        self.assertEqual(run(FakeTable(row))["source"], "derived")

    def test_a_source_that_is_missing_or_unknown_is_gated_too(self):
        for stored in (None, "", "elsewhere", "UI", "Scheduled", ["ui"], 7):
            with self.subTest(stored=stored):
                fields = {"Agenda": "One", "Consent": DENIED}
                if stored is not None:
                    fields["AgendaSource"] = stored

                with self.assertRaises(PermissionError) as caught:
                    run(FakeTable(meeting(**fields)))

                self.assertEqual(str(caught.exception), consent_gate.CAPTURE_NOT_ALLOWED_MESSAGE)

    def test_a_gated_agenda_follows_the_deployment_mode_when_the_record_has_no_mode(self):
        row = meeting(Agenda="One", AgendaSource="derived")
        for mode, allowed in (("off", True), ("notify", True), ("opt_in", False)):
            with (
                self.subTest(mode=mode),
                patch.object(consent_gate, "deployment_consent_mode", return_value=mode),
            ):
                if allowed:
                    self.assertTrue(run(FakeTable(row))["hasAgenda"])
                else:
                    with self.assertRaises(PermissionError):
                        run(FakeTable(row))

    def test_settings_that_cannot_be_read_fail_closed_with_the_gates_own_message(self):
        row = meeting(Agenda="One", AgendaSource="derived")
        failure = consent_gate.ConsentSettingsUnavailable()

        with patch.object(consent_gate, "deployment_consent_mode", side_effect=failure):
            with self.assertRaises(consent_gate.ConsentSettingsUnavailable) as caught:
                run(FakeTable(row))

        self.assertEqual(str(caught.exception), consent_gate.SETTINGS_UNAVAILABLE_MESSAGE)

    def test_a_consent_value_that_is_not_a_record_is_refused_not_allowed(self):
        row = meeting(Agenda="One", AgendaSource="derived", Consent="granted")

        with self.assertRaises(consent_gate.ConsentSettingsUnavailable):
            run(FakeTable(row))

    def test_access_is_decided_before_consent(self):
        row = meeting(Agenda="One", AgendaSource="derived", Consent=DENIED)

        with patch.object(consent_gate, "require_capture_allowed") as gate:
            with self.assertRaises(PermissionError) as caught:
                run(FakeTable(row), user_id=OTHER)

        gate.assert_not_called()
        self.assertEqual(str(caught.exception), get_meeting_agenda.ACCESS_DENIED_MESSAGE)


class AccessTest(unittest.TestCase):
    def read(self, row, **kwargs):
        return run(FakeTable(row), **kwargs)["hasAgenda"]

    def test_the_owner_reads(self):
        self.assertTrue(self.read(meeting(Agenda="One", AgendaSource="ui")))

    def test_an_older_row_names_its_owner_as_agent_id(self):
        row = {"PK": "c#call-1", "SK": "c#call-1", "AgentId": OWNER, "Agenda": "One"}
        row["AgendaSource"] = "ui"

        self.assertTrue(self.read(row))

    def test_a_person_the_meeting_is_shared_with_reads(self):
        shapes = {
            "list": [FRIEND],
            "string": f"{OTHER}, {FRIEND}",
            "brackets": f"[{FRIEND}]",
            "set": {FRIEND},
        }
        for name, shared in shapes.items():
            with self.subTest(shape=name):
                row = meeting(Agenda="One", AgendaSource="ui", SharedWith=shared)

                self.assertTrue(self.read(row, user_id=FRIEND))

    def test_someone_else_is_refused(self):
        row = meeting(Agenda="One", AgendaSource="ui", SharedWith=[FRIEND])

        with self.assertRaises(PermissionError) as caught:
            self.read(row, user_id=OTHER)

        self.assertEqual(str(caught.exception), "Access denied to this meeting")

    def test_a_part_of_a_shared_entry_is_not_an_entry(self):
        row = meeting(Agenda="One", AgendaSource="ui", SharedWith=[f"x{FRIEND}"])

        with self.assertRaises(PermissionError):
            self.read(row, user_id=FRIEND)

    def test_an_admin_reads_any_meeting(self):
        row = meeting(Agenda="One", AgendaSource="ui")

        self.assertTrue(self.read(row, user_id="admin@example.com", is_admin=True))

    def test_no_identity_reads_nothing(self):
        row = meeting(Agenda="One", AgendaSource="ui", SharedWith=[""])
        for user_id in (None, ""):
            with self.subTest(user_id=user_id), self.assertRaises(PermissionError):
                self.read(row, user_id=user_id)

    def test_a_missing_meeting_and_a_forbidden_one_are_refused_with_the_same_text(self):
        forbidden = meeting("exists", Agenda="One", AgendaSource="ui")
        messages = []
        for call_id in ("exists", "no-such-meeting"):
            with self.assertRaises(PermissionError) as caught:
                run(FakeTable(forbidden), call_id, user_id=OTHER)
            messages.append(str(caught.exception))

        self.assertEqual(messages[0], messages[1])
        self.assertEqual(messages[0], "Access denied to this meeting")
        self.assertNotIn("exists", messages[0])

    def test_a_missing_meeting_is_refused_even_for_an_admin(self):
        with self.assertRaises(PermissionError) as caught:
            run(FakeTable(), "no-such-meeting", user_id="admin@example.com", is_admin=True)

        self.assertEqual(str(caught.exception), "Access denied to this meeting")

    def test_an_empty_row_is_not_a_meeting(self):
        table = FakeTable()
        table.get_item = lambda **kwargs: {"Item": {}}

        with self.assertRaises(PermissionError):
            run(table, user_id="admin@example.com", is_admin=True)


class ArgumentTest(unittest.TestCase):
    def test_a_meeting_id_that_cannot_be_one_is_refused_before_anything_is_read(self):
        table = FakeTable(meeting(Agenda="One", AgendaSource="ui"))
        for value in (None, "", "   ", "\n", 5, 1.5, True, ["call-1"], {"id": "call-1"}, "x" * 257):
            with self.subTest(value=repr(value)[:20]):
                with self.assertRaises(ValueError) as caught:
                    run(table, value)

                self.assertEqual(
                    str(caught.exception),
                    "meetingId must be a non-empty string of at most 256 characters",
                )
        self.assertEqual(table.reads, [])

    def test_the_longest_meeting_id_is_accepted(self):
        call_id = "x" * 256

        result = run(FakeTable(meeting(call_id, Agenda="One", AgendaSource="ui")), call_id)

        self.assertEqual(result["meetingId"], call_id)

    def test_a_meeting_id_with_spaces_and_hyphens_is_read_as_given(self):
        call_id = "Weekly Sync-2026-11-19-10-00-00"
        table = FakeTable(meeting(call_id, Agenda="One", AgendaSource="ui"))

        run(table, call_id)

        self.assertEqual(table.reads[0]["Key"], {"PK": f"c#{call_id}", "SK": f"c#{call_id}"})


class ReadTest(unittest.TestCase):
    def test_only_the_attributes_the_tool_uses_are_requested(self):
        table = FakeTable(meeting(Agenda="One", AgendaSource="ui"))

        run(table)

        request = table.reads[0]
        self.assertEqual(len(table.reads), 1)
        projected = {
            request["ExpressionAttributeNames"].get(name.strip(), name.strip())
            for name in request["ProjectionExpression"].split(",")
        }
        self.assertEqual(
            projected,
            {"Owner", "AgentId", "SharedWith", "Agenda", "AgendaSource", "Consent"},
        )
        # Every placeholder is used, and every one used is defined.
        for placeholder in request["ExpressionAttributeNames"]:
            self.assertIn(placeholder, request["ProjectionExpression"])

    def test_a_failed_read_is_a_fixed_message_without_the_cause(self):
        table = FakeTable(error=RuntimeError(SECRET_ARN))

        with self.assertLogs(level="ERROR") as logged:
            with self.assertRaises(ValueError) as caught:
                run(table)

        self.assertEqual(str(caught.exception), "Failed to read the meeting agenda")
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn("arn:", repr(caught.exception))
        self.assertNotIn("arn:", "\n".join(logged.output))
        self.assertIn("RuntimeError", "\n".join(logged.output))

    def test_a_table_that_cannot_be_opened_is_the_same_fixed_message(self):
        with patch("boto3.resource", side_effect=RuntimeError(SECRET_ARN)):
            with self.assertRaises(ValueError) as caught:
                get_meeting_agenda.execute("call-1", user_id=OWNER)

        self.assertEqual(str(caught.exception), "Failed to read the meeting agenda")


class LoggingTest(unittest.TestCase):
    def test_the_log_names_the_meeting_by_repr_and_never_holds_the_agenda(self):
        secret = "Acquisition of Contoso"
        row = meeting("call\nsecond line", Agenda=f"1. {secret}", AgendaSource="ui")

        with self.assertLogs(level="INFO") as logged:
            run(FakeTable(row), "call\nsecond line")

        text = "\n".join(logged.output)
        self.assertIn("'call\\nsecond line'", text)
        self.assertNotIn("call\nsecond line", text)
        self.assertNotIn(secret, text)
        self.assertNotIn(OWNER, text)

    def test_a_refusal_is_logged_without_the_agenda(self):
        secret = "Acquisition of Contoso"
        row = meeting(Agenda=f"1. {secret}", AgendaSource="ui")

        with self.assertLogs(level="WARNING") as logged:
            with self.assertRaises(PermissionError):
                run(FakeTable(row), user_id=OTHER)

        self.assertNotIn(secret, "\n".join(logged.output))


if __name__ == "__main__":
    unittest.main()
