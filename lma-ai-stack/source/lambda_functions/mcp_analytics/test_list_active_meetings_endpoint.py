# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""list_active_meetings through the API key endpoint.

A JSON-RPC ``tools/call`` arrives as an API Gateway event, goes through the real
``index.lambda_handler`` and the real ``tools`` package, and reads a table
stand-in that pages like the TypeDateIndex. Nothing is mocked in between.
"""

import importlib
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

os.environ["LOG_LEVEL"] = "WARNING"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("CALLS_TABLE", "test-calls-table")
os.environ.setdefault("LMA_WEB_APP_URL", "https://example.invalid")

HERE = Path(__file__).resolve().parent


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


def stamp(minutes_ago):
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class FakeClient:
    def __init__(self, table):
        self.table = table

    def batch_get_item(self, RequestItems):
        keys = RequestItems[self.table.name]["Keys"]
        found = [self.table.rows[k["PK"]] for k in keys if k["PK"] in self.table.rows]
        return {"Responses": {self.table.name: found}}


class FakeTable:
    name = "calls"

    def __init__(self, meetings, uploads=()):
        self.rows = {m["PK"]: m for m in meetings}
        for call_id in uploads:
            self.rows[f"uj#{call_id}"] = {"PK": f"uj#{call_id}", "SK": f"uj#{call_id}"}
        self.index = sorted(
            (
                {
                    "ItemType": "call",
                    "SK": f"ts#{m['CreatedAt']}#id#{m['CallId']}",
                    "CallId": m["CallId"],
                    "Owner": m["Owner"],
                    **({"SharedWith": m["SharedWith"]} if "SharedWith" in m else {}),
                }
                for m in meetings
            ),
            key=lambda entry: entry["SK"],
            reverse=True,
        )
        self.meta = type("Meta", (), {"client": FakeClient(self)})()

    def query(self, **kwargs):
        lo, hi = kwargs["KeyConditionExpression"]._values[1]._values[1:]
        rows = [e for e in self.index if lo <= e["SK"] <= hi]
        if "ExclusiveStartKey" in kwargs:
            rows = [e for e in rows if e["SK"] < kwargs["ExclusiveStartKey"]["SK"]]
        page = rows[: kwargs["Limit"]]
        response = {"Items": page}
        if len(page) == kwargs["Limit"]:
            response["LastEvaluatedKey"] = {"SK": page[-1]["SK"]}
        return response

    def get_item(self, Key, **kwargs):
        row = self.rows.get(Key["PK"])
        return {"Item": row} if row else {}


def row(call_id, owner=ME, status="STARTED", minutes_ago=10, **extra):
    return {
        "PK": f"c#{call_id}",
        "SK": f"c#{call_id}",
        "CallId": call_id,
        "Owner": owner,
        "Status": status,
        "MeetingTopic": f"topic {call_id}",
        "CreatedAt": stamp(minutes_ago),
        **extra,
    }


def call(arguments, rows, uploads=(), user_id=ME, is_admin="false", name="list_active_meetings"):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    event = {
        "httpMethod": "POST",
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {"userId": user_id, "username": user_id, "isAdmin": is_admin}
        },
    }
    table = FakeTable(rows, uploads)
    resource = type("Resource", (), {"Table": lambda self, name: table})()
    with patch("boto3.resource", return_value=resource):
        response = index.lambda_handler(event, None)
    assert response["statusCode"] == 200
    return json.loads(response["body"])["result"]


def answer(result):
    assert "isError" not in result, result
    return json.loads(result["content"][0]["text"])


class TestListActiveMeetingsThroughTheEndpoint(unittest.TestCase):
    def test_a_running_meeting_of_the_caller_is_listed_with_its_fields(self):
        rows = [
            row(
                "live",
                minutes_ago=5,
                UpdatedAt=stamp(1),
                Consent={"Mode": "notify", "Status": "NOTIFIED", "CaptureAllowed": True},
            ),
            row("done", status="ENDED", minutes_ago=3),
            row("theirs", owner=OTHER, minutes_ago=2),
        ]

        result = answer(call({}, rows))

        self.assertEqual(result["count"], 1)
        meeting = result["meetings"][0]
        self.assertEqual(meeting["meetingId"], "live")
        self.assertEqual(meeting["meetingName"], "topic live")
        self.assertEqual(meeting["status"], "STARTED")
        self.assertEqual(meeting["owner"], ME)
        self.assertEqual(
            meeting["consent"], {"mode": "notify", "status": "NOTIFIED", "captureAllowed": True}
        )
        self.assertLess(meeting["startedAt"], meeting["lastActivityAt"])
        self.assertTrue(meeting["meetingUrl"].endswith("/#/calls/live"))
        self.assertNotIn("truncated", result)

    def test_an_upload_being_processed_and_a_meeting_of_another_user_are_not_listed(self):
        rows = [
            row("upload", status="TRANSCRIBING", minutes_ago=1),
            row("theirs", owner=OTHER, minutes_ago=2),
            row("shared", owner=OTHER, minutes_ago=3, SharedWith=[FRIEND]),
            row("mine", status="TRANSCRIBING", minutes_ago=4),
        ]

        result = answer(call({"limit": 5}, rows, uploads=["upload"]))

        self.assertEqual([m["meetingId"] for m in result["meetings"]], ["mine"])

    def test_a_meeting_shared_with_the_caller_is_listed(self):
        rows = [row("shared", owner=OTHER, SharedWith=f"[{OTHER}, {FRIEND}]")]

        result = answer(call({}, rows, user_id=FRIEND))

        self.assertEqual([m["meetingId"] for m in result["meetings"]], ["shared"])

    def test_an_admin_sees_the_meetings_of_everyone(self):
        rows = [row("a", owner=OTHER, minutes_ago=1), row("b", owner=FRIEND, minutes_ago=2)]

        result = answer(call({}, rows, user_id="admin", is_admin="true"))

        self.assertEqual([m["meetingId"] for m in result["meetings"]], ["a", "b"])

    def test_the_limit_is_moved_into_range_and_a_string_limit_is_read(self):
        rows = [row(f"m{i}", minutes_ago=i + 1) for i in range(60)]

        self.assertEqual(answer(call({"limit": 500}, rows))["count"], 50)
        self.assertEqual(answer(call({"limit": "3"}, rows))["count"], 3)
        self.assertEqual(answer(call({}, rows))["count"], 10)

    def test_a_limit_that_is_not_an_integer_is_a_tool_error(self):
        for value in (True, "many", 1.5, 1e999, [1]):
            with self.subTest(limit=value):
                result = call({"limit": value}, [row("a")])

                self.assertTrue(result["isError"])
                self.assertEqual(result["content"][0]["text"], "Error: limit must be an integer")

    def test_a_failed_read_is_a_tool_error_with_fixed_text(self):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_active_meetings", "arguments": {}},
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
        self.assertEqual(result["content"][0]["text"], "Error: Failed to list active meetings")

    def test_the_tool_is_offered_by_tools_list_as_read_only(self):
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        event = {
            "httpMethod": "POST",
            "body": json.dumps(body),
            "requestContext": {"authorizer": {"userId": ME, "username": ME, "isAdmin": "false"}},
        }

        tools = json.loads(index.lambda_handler(event, None)["body"])["result"]["tools"]

        listed = {tool["name"]: tool for tool in tools}["list_active_meetings"]
        self.assertTrue(listed["annotations"]["readOnlyHint"])
        self.assertIn("lastActivityAt", listed["description"])
        self.assertIn("truncated", listed["description"])
        self.assertEqual(len(tools), 10)

    def test_the_gateway_path_does_not_reach_the_tool(self):
        event = {
            "limit": 5,
            "requestContext": {"authorizer": {"claims": {"sub": ME}}},
        }
        table = FakeTable([row("live")])
        resource = type("Resource", (), {"Table": lambda self, name: table})()
        with patch("boto3.resource", return_value=resource):
            response = index.lambda_handler(event, None)

        body = json.loads(response["body"])
        self.assertIn("filters", json.loads(body["content"][0]["text"]))


if __name__ == "__main__":
    unittest.main()
