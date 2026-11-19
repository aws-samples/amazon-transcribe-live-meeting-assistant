# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""get_live_context through the API key endpoint.

A JSON-RPC message arrives as an API Gateway event and goes through the real
``index.lambda_handler``, the real ``tools`` package and the real configuration
layer (the setting is read with ``read_setting`` from a store stand-in, the
consent rules and the speaker names are the layer's). The table stand-in
evaluates the key condition, the filter and the projection the tool sends.
"""

import base64
import importlib
import json
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from boto3.dynamodb.conditions import AttributeBase

os.environ["LOG_LEVEL"] = "WARNING"
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("CALLS_TABLE", "test-calls-table")
os.environ.setdefault("LMA_WEB_APP_URL", "https://example.invalid")

HERE = Path(__file__).resolve().parent
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
live = index.get_live_context

ME = "me@example.com"
CALL = "live-meeting"


def _matches(condition, item):
    expression = condition.get_expression()
    operator, values = expression["operator"], expression["values"]
    if operator == "AND":
        return _matches(values[0], item) and _matches(values[1], item)
    assert isinstance(values[0], AttributeBase)
    name = values[0].name
    if operator == "=":
        return name in item and item[name] == values[1]
    if operator == "IN":
        return name in item and item[name] in list(values[1])
    raise AssertionError(f"unexpected operator {operator}")


def _project(item, projection, names):
    wanted = [names.get(part.strip(), part.strip()) for part in projection.split(",")]
    return {name: item[name] for name in wanted if name in item}


class Table:
    def __init__(self, rows):
        self.rows = rows
        self.queries = 0

    def get_item(self, Key, ProjectionExpression, ExpressionAttributeNames, ConsistentRead):
        assert ConsistentRead is True, "the consent decision needs the latest meeting row"
        for row in self.rows:
            if row["PK"] == Key["PK"] and row["SK"] == Key["SK"]:
                return {"Item": _project(row, ProjectionExpression, ExpressionAttributeNames)}
        return {}

    def query(self, **kwargs):
        self.queries += 1
        names = kwargs["ExpressionAttributeNames"]
        items = [
            _project(row, kwargs["ProjectionExpression"], names)
            for row in sorted(self.rows, key=lambda row: row["SK"])
            if _matches(kwargs["KeyConditionExpression"], row)
            and _matches(kwargs["FilterExpression"], row)
        ]
        return {"Items": items}


class Store:
    def __init__(self, values=None, error=None):
        self.values = values
        self.error = error

    def get_section(self, name):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(values=self.values if name == "meeting_assist" else {})


def stamp(seconds_ago):
    from datetime import datetime, timedelta, timezone

    moment = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def rows():
    meeting = {
        "PK": f"c#{CALL}",
        "SK": f"c#{CALL}",
        "Owner": ME,
        "Status": "STARTED",
        "SpeakerIdentities": json.dumps({"identities": {"REMOTE#spk_1": {"name": "Bob"}}}),
    }

    def line(segment, start, seconds_ago, text, channel="REMOTE", speaker="spk_1", partial=False):
        return {
            "PK": f"trs#{CALL}",
            "SK": f"s#{segment}",
            "SegmentId": segment,
            "Channel": channel,
            "Speaker": speaker,
            "Transcript": text,
            "StartTime": Decimal(start),
            "EndTime": Decimal(start) + 2,
            "IsPartial": partial,
            "CreatedAt": stamp(seconds_ago),
        }

    return [
        meeting,
        line("b", 30, 20, "Second thing"),
        line("a", 10, 40, "First thing"),
        line("p", 40, 1, "half a wor", partial=True),
        line("c", 35, 10, "Ana typed\nthis", channel="MEETING_CHAT", speaker="Ana"),
    ]


def rpc(method, params=None, user_id=ME, table=None, store=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    event = {
        "httpMethod": "POST",
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {"userId": user_id, "username": user_id, "isAdmin": "false"}
        },
    }
    resource = SimpleNamespace(Table=lambda name: table)
    saved = live._SETTING_STORE
    live._SETTING_STORE = live._WatchedStore(store)
    try:
        with patch("boto3.resource", return_value=resource):
            response = index.lambda_handler(event, None)
    finally:
        live._SETTING_STORE = saved
    assert response["statusCode"] == 200
    return json.loads(response["body"])["result"]


ON = Store({"mcpLiveContext": True})
OFF = Store({"mcpLiveContext": False})


def get_live(arguments, store=ON, table=None, user_id=ME):
    table = table if table is not None else Table(rows())
    return rpc(
        "tools/call", {"name": "get_live_context", "arguments": arguments}, user_id, table, store
    )


class TestGetLiveContextThroughTheEndpoint(unittest.TestCase):
    def test_with_the_setting_on_the_running_meeting_is_read_line_by_line(self):
        result = get_live({"meetingId": CALL})

        self.assertNotIn("isError", result)
        answer = json.loads(result["content"][0]["text"])
        self.assertEqual(answer["meetingId"], CALL)
        self.assertFalse(answer["ended"])
        self.assertFalse(answer["truncated"])
        self.assertEqual(
            [(line["t"], line["speaker"], line["text"]) for line in answer["lines"]],
            [
                ("00:10", "Bob", "First thing"),
                ("00:30", "Bob", "Second thing"),
                ("00:35", "Ana", "Ana typed / this"),
            ],
        )
        state = json.loads(base64.urlsafe_b64decode(answer["nextCursor"]))
        self.assertEqual((state["v"], state["m"]), (2, CALL))

    def test_the_next_cursor_returns_only_the_last_minute_and_what_came_after(self):
        table = Table(rows())
        first = json.loads(get_live({"meetingId": CALL}, table=table)["content"][0]["text"])
        table.rows.append(
            {
                "PK": f"trs#{CALL}",
                "SK": "s#d",
                "SegmentId": "d",
                "Channel": "LOCAL",
                "Speaker": "Me",
                "Transcript": "Third thing",
                "StartTime": Decimal(50),
                "EndTime": Decimal(52),
                "IsPartial": False,
                "CreatedAt": stamp(0),
            }
        )
        table.rows = [row for row in table.rows if row["SK"] != "s#a"] + [
            {**row, "CreatedAt": stamp(300)} for row in table.rows if row["SK"] == "s#a"
        ]
        second = get_live(
            {"meetingId": CALL, "cursor": first["nextCursor"], "includeChat": False}, table=table
        )

        answer = json.loads(second["content"][0]["text"])
        self.assertEqual([line["segmentId"] for line in answer["lines"]], ["b", "d"])

    def test_with_the_setting_off_the_tool_is_not_listed_and_a_call_is_refused_unread(self):
        table = Table(rows())
        tools = rpc("tools/list", store=OFF, table=table)["tools"]
        self.assertNotIn("get_live_context", [tool["name"] for tool in tools])
        self.assertEqual(len(tools), len(index.MCP_TOOLS) - 1)

        result = get_live({"meetingId": CALL}, store=OFF, table=table)

        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], f"Error: {live.SETTING_OFF_MESSAGE}")
        self.assertEqual(table.queries, 0)

    def test_with_the_setting_on_the_tool_is_listed(self):
        tools = rpc("tools/list", store=ON, table=Table([]))["tools"]
        self.assertIn("get_live_context", [tool["name"] for tool in tools])
        self.assertEqual(len(tools), len(index.MCP_TOOLS))

    def test_a_setting_that_cannot_be_read_hides_the_tool_and_says_try_again(self):
        broken = Store(error=RuntimeError("table gone"))
        tools = rpc("tools/list", store=broken, table=Table([]))["tools"]
        self.assertNotIn("get_live_context", [tool["name"] for tool in tools])

        result = get_live({"meetingId": CALL}, store=broken)

        self.assertTrue(result["isError"])
        self.assertIn("try again", result["content"][0]["text"])

    def test_another_users_meeting_is_refused(self):
        result = get_live({"meetingId": CALL}, user_id="someone@example.com")

        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "Error: Access denied to this meeting")

    def test_a_cursor_that_is_not_valid_is_a_tool_error(self):
        result = get_live({"meetingId": CALL, "cursor": "not-a-cursor"})

        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], f"Error: {live.CURSOR_INVALID}")


if __name__ == "__main__":
    unittest.main()
