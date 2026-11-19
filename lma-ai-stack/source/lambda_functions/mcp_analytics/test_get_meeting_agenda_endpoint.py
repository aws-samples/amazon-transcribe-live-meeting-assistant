# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""get_meeting_agenda through the API key endpoint.

A JSON-RPC ``tools/call`` arrives as an API Gateway event, goes through the real
``index.lambda_handler`` and the real ``tools`` package (the real tool and the real
consent gate), and reads a table stand-in. Nothing is mocked in between.
"""

import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["LOG_LEVEL"] = "WARNING"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("CALLS_TABLE", "test-calls-table")
os.environ.setdefault("LMA_WEB_APP_URL", "https://example.invalid")

HERE = Path(__file__).resolve().parent
# The configuration layer the deployed function carries, for the consent rules.
LAYER = HERE.parents[1] / "lambda_layers" / "configuration_layer"
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))


def _is_ours(name):
    return name in ("index", "tools") or name.startswith("tools.")


def _import_real_index():
    # test_jsonrpc puts stand-ins for index and tools into sys.modules, so the
    # real ones are imported on their own and the modules table is put back.
    saved = {name: module for name, module in sys.modules.items() if _is_ours(name)}
    for name in saved:
        del sys.modules[name]
    sys.path.insert(0, str(HERE))
    try:
        return importlib.import_module("index")
    finally:
        sys.path.remove(str(HERE))
        for name in [n for n in sys.modules if _is_ours(n)]:
            del sys.modules[name]
        sys.modules.update(saved)


index = _import_real_index()

ME = "me@example.com"
OTHER = "other@example.com"
FRIEND = "friend@example.com"
AGENDA = "1. Budget review\n- Hiring plan\n* Roadmap"


class FakeTable:
    name = "calls"

    def __init__(self, *rows):
        self.rows = {row["PK"]: row for row in rows}
        self.gets = []

    def get_item(self, Key, **kwargs):
        self.gets.append(kwargs)
        row = self.rows.get(Key["PK"])
        return {"Item": row} if row else {}


def row(call_id="m1", owner=ME, **extra):
    return {"PK": f"c#{call_id}", "SK": f"c#{call_id}", "Owner": owner, **extra}


def call(arguments, rows, user_id=ME, is_admin="false"):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_meeting_agenda", "arguments": arguments},
    }
    event = {
        "httpMethod": "POST",
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {"userId": user_id, "username": user_id, "isAdmin": is_admin}
        },
    }
    table = FakeTable(*rows)
    resource = type("Resource", (), {"Table": lambda self, name: table})()
    with patch("boto3.resource", return_value=resource):
        response = index.lambda_handler(event, None)
    assert response["statusCode"] == 200
    call.table = table
    call.body_bytes = len(response["body"].encode())
    return json.loads(response["body"])["result"]


def answer(result):
    assert "isError" not in result, result
    return json.loads(result["content"][0]["text"])


class TestGetMeetingAgendaThroughTheEndpoint(unittest.TestCase):
    def test_the_agenda_of_a_meeting_of_the_caller_is_returned(self):
        result = answer(call({"meetingId": "m1"}, [row(Agenda=AGENDA, AgendaSource="ui")]))

        self.assertEqual(
            result,
            {
                "meetingId": "m1",
                "hasAgenda": True,
                "items": [
                    {"number": 1, "text": "Budget review"},
                    {"number": 2, "text": "Hiring plan"},
                    {"number": 3, "text": "Roadmap"},
                ],
                "source": "ui",
                "sourceDescription": "set on the meeting page",
            },
        )

    def test_a_meeting_without_an_agenda_says_so(self):
        result = answer(call({"meetingId": "m1"}, [row()]))

        self.assertFalse(result["hasAgenda"])
        self.assertEqual(result["items"], [])
        self.assertIsNone(result["source"])

    def test_a_meeting_shared_with_the_caller_and_an_admin_read_it(self):
        rows = [
            row(owner=OTHER, SharedWith=f"[{OTHER}, {FRIEND}]", Agenda="One", AgendaSource="mcp")
        ]

        shared = answer(call({"meetingId": "m1"}, rows, user_id=FRIEND))
        admin = answer(call({"meetingId": "m1"}, rows, user_id="admin", is_admin="true"))

        self.assertEqual(shared["items"], admin["items"])
        self.assertEqual(shared["source"], "mcp")

    def test_a_meeting_of_somebody_else_and_a_missing_one_are_refused_alike(self):
        rows = [row(owner=OTHER, Agenda="One", AgendaSource="ui")]

        refused = call({"meetingId": "m1"}, rows)
        missing = call({"meetingId": "nope"}, rows)

        for result in (refused, missing):
            self.assertTrue(result["isError"])
            self.assertEqual(result["content"][0]["text"], "Error: Access denied to this meeting")

    def test_an_agenda_read_from_the_transcript_is_withheld_without_consent(self):
        consent = {"Mode": "opt_in", "Rule": "each_participant"}
        rows = [row(Agenda="One", AgendaSource="derived", Consent=consent)]

        result = call({"meetingId": "m1"}, rows)

        self.assertTrue(result["isError"])
        self.assertIn("Capture is not allowed for this meeting", result["content"][0]["text"])
        self.assertNotIn("One", result["content"][0]["text"])

    def test_an_agenda_a_person_typed_is_returned_without_consent(self):
        consent = {"Mode": "opt_in", "Rule": "each_participant"}
        rows = [row(Agenda="One", AgendaSource="ui", Consent=consent)]

        self.assertEqual(answer(call({"meetingId": "m1"}, rows))["items"][0]["text"], "One")

    def test_the_meeting_row_is_read_consistently(self):
        call({"meetingId": "m1"}, [row(Agenda="One", AgendaSource="ui")])

        self.assertEqual(len(call.table.gets), 1)
        self.assertIs(call.table.gets[0]["ConsistentRead"], True)

    def test_a_very_long_agenda_is_cut_and_the_answer_stays_under_one_hundred_kilobytes(self):
        for name, line in {"ascii": "x", "emoji": "😀", "quote": '"'}.items():
            with self.subTest(line=name):
                agenda = "\n".join(line * 150 for _ in range(10_000))
                result = answer(call({"meetingId": "m1"}, [row(Agenda=agenda, AgendaSource="ui")]))

                self.assertEqual(len(result["items"]), 100)
                self.assertIs(result["truncated"], True)
                self.assertIn("note", result)
                self.assertLess(call.body_bytes, 100_000)

    def test_ten_thousand_one_character_lines_are_cut(self):
        agenda = "\n".join("x" for _ in range(10_000))

        result = answer(call({"meetingId": "m1"}, [row(Agenda=agenda, AgendaSource="ui")]))

        self.assertEqual(len(result["items"]), 100)
        self.assertIs(result["truncated"], True)
        self.assertLess(call.body_bytes, 100_000)

    def test_a_meeting_id_that_is_not_text_is_a_tool_error(self):
        for value in (None, 7, ""):
            with self.subTest(value=value):
                arguments = {} if value is None else {"meetingId": value}
                result = call(arguments, [row(Agenda="One", AgendaSource="ui")])

                self.assertTrue(result["isError"])
                self.assertIn("meetingId must be a non-empty string", result["content"][0]["text"])

    def test_a_failed_read_is_a_tool_error_with_fixed_text(self):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_meeting_agenda", "arguments": {"meetingId": "m1"}},
        }
        event = {
            "httpMethod": "POST",
            "body": json.dumps(body),
            "requestContext": {"authorizer": {"userId": ME, "username": ME, "isAdmin": "false"}},
        }
        with patch("boto3.resource", side_effect=RuntimeError("arn:aws:dynamodb:::table/secret")):
            response = index.lambda_handler(event, None)

        result = json.loads(response["body"])["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "Error: Failed to read the meeting agenda")

    def test_the_tool_is_offered_by_tools_list_as_read_only(self):
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        event = {
            "httpMethod": "POST",
            "body": json.dumps(body),
            "requestContext": {"authorizer": {"userId": ME, "username": ME, "isAdmin": "false"}},
        }

        tools = json.loads(index.lambda_handler(event, None)["body"])["result"]["tools"]

        listed = {tool["name"]: tool for tool in tools}["get_meeting_agenda"]
        self.assertTrue(listed["annotations"]["readOnlyHint"])
        self.assertEqual(listed["inputSchema"]["required"], ["meetingId"])
        self.assertEqual(len(tools), 10)


if __name__ == "__main__":
    unittest.main()
