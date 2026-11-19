# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""list_meetings through the API key endpoint.

A JSON-RPC ``tools/call`` arrives as an API Gateway event, goes through the real
``index.lambda_handler`` and the real ``tools`` package, and reads a table
stand-in that pages like the TypeDateIndex. Nothing is mocked in between.
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


class FakeClient:
    def __init__(self, table):
        self.table = table

    def batch_get_item(self, RequestItems):
        keys = RequestItems[self.table.name]["Keys"]
        found = [self.table.rows[k["PK"]] for k in keys if k["PK"] in self.table.rows]
        return {"Responses": {self.table.name: found}}


class FakeTable:
    name = "calls"

    def __init__(self, rows):
        self.rows = {row["PK"]: row for row in rows}
        self.index = [{"CallId": row["CallId"], "SK": f"ts#{i:06d}"} for i, row in enumerate(rows)]
        self.meta = type("Meta", (), {"client": FakeClient(self)})()

    def query(self, **kwargs):
        start = (
            int(kwargs["ExclusiveStartKey"]["SK"][3:]) + 1 if "ExclusiveStartKey" in kwargs else 0
        )
        page = self.index[start : start + kwargs["Limit"]]
        response = {"Items": page}
        if start + len(page) < len(self.index):
            response["LastEvaluatedKey"] = {"SK": page[-1]["SK"]}
        return response

    def get_item(self, Key):
        row = self.rows.get(Key["PK"])
        return {"Item": row} if row else {}


def row(call_id, owner=ME, status="ENDED"):
    return {
        "PK": f"c#{call_id}",
        "SK": f"c#{call_id}",
        "CallId": call_id,
        "Owner": owner,
        "Status": status,
        "MeetingTopic": f"topic {call_id}",
    }


def call(arguments, rows, user_id=ME, is_admin="false", tool="list_meetings"):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    event = {
        "httpMethod": "POST",
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {"userId": user_id, "username": user_id, "isAdmin": is_admin}
        },
    }
    table = FakeTable(rows)
    resource = type("Resource", (), {"Table": lambda self, name: table})()
    with patch("boto3.resource", return_value=resource):
        response = index.lambda_handler(event, None)
    assert response["statusCode"] == 200
    return json.loads(response["body"])["result"]


class TestListMeetingsThroughTheEndpoint(unittest.TestCase):
    def test_in_progress_in_lower_case_with_a_limit_of_one(self):
        others = [row(f"o{i}", owner=OTHER, status="STARTED") for i in range(30)]
        rows = others + [row("ended-1"), row("transcribing", status="TRANSCRIBING"), row("old")]

        result = call({"status": "in_progress", "limit": 1}, rows)

        self.assertNotIn("isError", result)
        answer = json.loads(result["content"][0]["text"])
        self.assertEqual([m["meetingId"] for m in answer["meetings"]], ["transcribing"])
        self.assertEqual(answer["count"], 1)
        self.assertEqual(answer["filters"]["status"], "in_progress")
        self.assertNotIn("note", answer)

    def test_a_status_that_is_not_accepted_is_an_error_naming_the_accepted_ones(self):
        result = call({"status": "RUNNING"}, [row("a")])

        self.assertTrue(result["isError"])
        text = result["content"][0]["text"]
        for accepted in ("ALL", "IN_PROGRESS", "STARTED", "TRANSCRIBING", "ERRORED", "ENDED"):
            self.assertIn(accepted, text)

    def test_a_blank_status_lists_every_status(self):
        rows = [row("live", status="STARTED"), row("done")]

        result = call({"status": ""}, rows)

        self.assertNotIn("isError", result)
        answer = json.loads(result["content"][0]["text"])
        self.assertEqual([m["meetingId"] for m in answer["meetings"]], ["live", "done"])

    def test_the_listing_without_arguments_is_the_callers_own_meetings(self):
        rows = [row("theirs", owner=OTHER), row("mine")]

        answer = json.loads(call({}, rows)["content"][0]["text"])

        self.assertEqual([m["meetingId"] for m in answer["meetings"]], ["mine"])

    def test_an_admin_sees_the_meetings_of_others(self):
        rows = [row("theirs", owner=OTHER, status="STARTED")]

        answer = json.loads(
            call({"status": "IN_PROGRESS"}, rows, "admin", "true")["content"][0]["text"]
        )

        self.assertEqual([m["meetingId"] for m in answer["meetings"]], ["theirs"])

    def test_the_status_is_described_with_its_accepted_values_in_tools_list(self):
        listed = {tool["name"]: tool for tool in index.MCP_TOOLS}["list_meetings"]

        description = listed["inputSchema"]["properties"]["status"]["description"]

        for accepted in ("ALL", "IN_PROGRESS", "STARTED", "TRANSCRIBING", "ERRORED", "ENDED"):
            self.assertIn(accepted, description)


if __name__ == "__main__":
    unittest.main()
