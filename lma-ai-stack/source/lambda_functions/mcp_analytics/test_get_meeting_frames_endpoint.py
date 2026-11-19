# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""get_meeting_frames through the API key endpoint.

A JSON-RPC ``tools/call`` arrives as an API Gateway event, goes through the real
``index.lambda_handler`` and the real ``tools`` package (the real tool and the real
consent gate), and reads a table stand-in that pages like DynamoDB and answers
BatchGetItem. Nothing is mocked in between.
"""

import importlib
import json
import os
import sys
import unittest
from decimal import Decimal
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


class FakeClient:
    def __init__(self, table):
        self.table = table

    def batch_get_item(self, RequestItems):
        request = RequestItems[self.table.name]
        found = [
            {name: row[name] for name in ("SK", "Title", "SlideText") if name in row}
            for row in (self.table.rows.get((k["PK"], k["SK"])) for k in request["Keys"])
            if row
        ]
        return {"Responses": {self.table.name: found}}


class FakeTable:
    name = "calls"

    def __init__(self, *rows):
        self.rows = {(row["PK"], row["SK"]): row for row in rows}
        self.meta = type("Meta", (), {"client": FakeClient(self)})()
        self.gets = []

    def get_item(self, Key, **kwargs):
        self.gets.append(kwargs)
        row = self.rows.get((Key["PK"], Key["SK"]))
        return {"Item": row} if row else {}

    def query(self, **kwargs):
        partition = kwargs["KeyConditionExpression"]._values[1]
        rows = sorted(
            (r for (pk, _), r in self.rows.items() if pk == partition), key=lambda r: r["SK"]
        )
        if "ExclusiveStartKey" in kwargs:
            rows = [r for r in rows if r["SK"] > kwargs["ExclusiveStartKey"]["SK"]]
        page = rows[: kwargs["Limit"]]
        projected = [
            {name: r[name] for name in ("SK", "FrameId", "Timestamp", "ContentKind") if name in r}
            for r in page
        ]
        response = {"Items": projected}
        if len(page) == kwargs["Limit"]:
            response["LastEvaluatedKey"] = {"PK": partition, "SK": page[-1]["SK"]}
        return response


def meeting(call_id="m1", owner=ME, **extra):
    return {"PK": f"c#{call_id}", "SK": f"c#{call_id}", "Owner": owner, **extra}


def frame(seconds, call_id="m1", **extra):
    millis = int(seconds * 1000)
    return {
        "PK": f"kf#{call_id}",
        "SK": f"f#{millis:012d}-a",
        "FrameId": f"{millis:012d}-a",
        "Timestamp": Decimal(str(seconds)),
        "ImageUrl": f"https://bucket.s3.amazonaws.com/keyframes/{millis}.jpg",
        "ContentKind": "SCREEN",
        "Title": f"Slide at {seconds}",
        "SlideText": f"Text at {seconds}",
        **extra,
    }


def call(arguments, rows, user_id=ME, is_admin="false"):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_meeting_frames", "arguments": arguments},
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


def frames_of(*seconds, call_id="m1"):
    return [frame(s, call_id=call_id) for s in seconds]


class TestGetMeetingFramesThroughTheEndpoint(unittest.TestCase):
    def test_the_last_three_frames_of_a_meeting_of_the_caller_are_returned(self):
        rows = [meeting(), *frames_of(10, 70, 130, 190, 250)]

        result = answer(call({"meetingId": "m1"}, rows))

        self.assertEqual(result["meetingId"], "m1")
        self.assertEqual((result["count"], result["totalFrames"], result["textRead"]), (3, 5, 3))
        self.assertEqual(
            [(f["from"], f["to"], f["title"], f["text"]) for f in result["frames"]],
            [
                ("02:10", "03:10", "Slide at 130", "Text at 130"),
                ("03:10", "04:10", "Slide at 190", "Text at 190"),
                ("04:10", None, "Slide at 250", "Text at 250"),
            ],
        )
        self.assertEqual(result["frames"][0]["fromSeconds"], 130.0)
        self.assertNotIn("note", result)

    def test_a_time_range_and_last_are_read_from_the_arguments(self):
        rows = [meeting(), *frames_of(10, 70, 130, 190, 250)]

        in_range = answer(call({"meetingId": "m1", "fromSeconds": 75, "toSeconds": 140}, rows))
        two = answer(call({"meetingId": "m1", "last": 2}, rows))

        self.assertEqual([f["fromSeconds"] for f in in_range["frames"]], [70.0, 130.0])
        self.assertEqual([f["fromSeconds"] for f in two["frames"]], [190.0, 250.0])

    def test_no_picture_link_reaches_the_client(self):
        rows = [meeting(), *frames_of(10, 70)]

        text = call({"meetingId": "m1"}, rows)["content"][0]["text"]

        for forbidden in ("ImageUrl", "https://", ".jpg", "bucket"):
            self.assertNotIn(forbidden, text)

    def test_frames_of_another_meeting_are_not_mixed_in(self):
        rows = [meeting(), meeting("m2", owner=OTHER), *frames_of(10), *frames_of(20, call_id="m2")]

        result = answer(call({"meetingId": "m1"}, rows))

        self.assertEqual([f["fromSeconds"] for f in result["frames"]], [10.0])

    def test_a_meeting_shared_with_the_caller_and_an_admin_read_it(self):
        rows = [meeting(owner=OTHER, SharedWith=[FRIEND]), *frames_of(10)]

        shared = answer(call({"meetingId": "m1"}, rows, user_id=FRIEND))
        admin = answer(call({"meetingId": "m1"}, rows, user_id="admin", is_admin="true"))

        self.assertEqual(shared["frames"], admin["frames"])
        self.assertEqual(shared["count"], 1)

    def test_a_meeting_of_somebody_else_and_a_missing_one_are_refused_alike(self):
        rows = [meeting(owner=OTHER), *frames_of(10)]

        refused = call({"meetingId": "m1"}, rows)
        missing = call({"meetingId": "nope"}, rows)

        for result in (refused, missing):
            self.assertTrue(result["isError"])
            self.assertEqual(result["content"][0]["text"], "Error: Access denied to this meeting")

    def test_a_meeting_whose_consent_does_not_allow_capture_returns_no_frames(self):
        consent = {"Mode": "opt_in", "Rule": "each_participant"}
        rows = [meeting(Consent=consent), *frames_of(10)]

        result = call({"meetingId": "m1"}, rows)

        self.assertTrue(result["isError"])
        self.assertIn("Capture is not allowed for this meeting", result["content"][0]["text"])
        self.assertNotIn("Text at", result["content"][0]["text"])

    def test_a_meeting_whose_consent_allows_capture_returns_frames(self):
        rows = [meeting(Consent={"Mode": "notify"}), *frames_of(10)]

        self.assertEqual(answer(call({"meetingId": "m1"}, rows))["count"], 1)

    def test_arguments_that_cannot_be_used_are_tool_errors(self):
        rows = [meeting(), *frames_of(10)]
        cases = {
            "last must be a whole number from 1 to 20": {"last": True},
            "fromSeconds must be a number of seconds": {"fromSeconds": "5"},
            "toSeconds must be a number of seconds": {"toSeconds": 10**400},
            "fromSeconds must not be after toSeconds": {"fromSeconds": 9, "toSeconds": 1},
            "meetingId must be a non-empty string": {"meetingId": ""},
        }
        for message, arguments in cases.items():
            with self.subTest(message=message):
                result = call({"meetingId": "m1", **arguments}, rows)

                self.assertTrue(result["isError"])
                self.assertIn(message, result["content"][0]["text"])

    def test_a_long_text_is_cut_and_the_answer_stays_small(self):
        rows = [meeting(), *[frame(n * 10, SlideText="中文" * 3000) for n in range(1, 21)]]

        response = call({"meetingId": "m1", "last": 20}, rows)

        result = answer(response)
        self.assertLess(len(response["content"][0]["text"].encode()), 100_000)
        self.assertTrue(all(f["textTruncated"] for f in result["frames"]))
        self.assertIn("textTruncated=true", result["note"])

    def test_the_worst_answer_is_under_one_hundred_kilobytes_as_sent(self):
        for name, char in {"emoji": "😀", "quote": '"', "backslash": "\\", "cjk": "中"}.items():
            with self.subTest(alphabet=name):
                rows = [
                    meeting(),
                    *[frame(n * 10, Title=char * 300, SlideText=char * 6000) for n in range(1, 21)],
                ]

                result = answer(call({"meetingId": "m1", "last": 20}, rows))

                self.assertEqual(result["count"], 20)
                self.assertLess(call.body_bytes, 100_000)

    def test_the_meeting_row_is_read_consistently(self):
        call({"meetingId": "m1"}, [meeting(), *frames_of(10)])

        self.assertEqual(len(call.table.gets), 1)
        self.assertIs(call.table.gets[0]["ConsistentRead"], True)

    def test_a_failed_read_is_a_tool_error_with_fixed_text(self):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_meeting_frames", "arguments": {"meetingId": "m1"}},
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
        self.assertEqual(result["content"][0]["text"], "Error: Failed to read the meeting frames")

    def test_the_tool_is_offered_by_tools_list_as_read_only(self):
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        event = {
            "httpMethod": "POST",
            "body": json.dumps(body),
            "requestContext": {"authorizer": {"userId": ME, "username": ME, "isAdmin": "false"}},
        }

        tools = json.loads(index.lambda_handler(event, None)["body"])["result"]["tools"]

        listed = {tool["name"]: tool for tool in tools}["get_meeting_frames"]
        self.assertTrue(listed["annotations"]["readOnlyHint"])
        self.assertEqual(listed["inputSchema"]["required"], ["meetingId"])
        self.assertEqual(
            set(listed["inputSchema"]["properties"]),
            {"meetingId", "last", "fromSeconds", "toSeconds"},
        )
        self.assertEqual(len(tools), 10)

    def test_the_gateway_path_does_not_reach_the_tool(self):
        # The gateway path picks a tool from the shape of its input; no shape is this tool's.
        table = FakeTable(meeting(), *frames_of(10))
        resource = type("Resource", (), {"Table": lambda self, name: table})()
        shapes = (
            {"meetingId": "m1"},
            {"meetingId": "m1", "last": 2},
            {"meetingId": "m1", "fromSeconds": 1, "toSeconds": 5},
        )
        with patch("boto3.resource", return_value=resource):
            with patch.object(index.get_meeting_frames, "execute") as frames:
                with patch.object(index.get_meeting_agenda, "execute") as agenda:
                    for shape in shapes:
                        event = {**shape, "requestContext": {"authorizer": {"claims": {"sub": ME}}}}
                        index.lambda_handler(event, None)

        frames.assert_not_called()
        agenda.assert_not_called()


if __name__ == "__main__":
    unittest.main()
