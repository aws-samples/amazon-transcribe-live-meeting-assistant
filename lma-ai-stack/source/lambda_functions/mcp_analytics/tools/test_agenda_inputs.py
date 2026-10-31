# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The optional agenda an MCP client passes to start_meeting_now / schedule_meeting.

What matters is that the agenda lands on the Virtual Participant row the
createVirtualParticipant mutation writes, because that row is where the VP reads
it from when the meeting starts; and that a call without one is sent exactly as
before.
"""

import ast
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("APPSYNC_GRAPHQL_URL", "https://example.invalid/graphql")
os.environ.setdefault("LMA_SETTINGS_PARAMETER", "lma-settings")
os.environ.setdefault("LMA_WEB_APP_URL", "https://example.invalid")

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

from tools import schedule_meeting, start_meeting_now  # noqa: E402
from tools.agenda_helper import MAX_AGENDA_CHARS, normalize_agenda  # noqa: E402


def _mcp_tools():
    """MCP_TOOLS from index.py, read without importing the handler module."""
    tree = ast.parse((HERE.parent / "index.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "MCP_TOOLS" for t in node.targets
        ):
            return {tool["name"]: tool for tool in ast.literal_eval(node.value)}
    raise AssertionError("MCP_TOOLS not found in index.py")


class TestNormalizeAgenda(unittest.TestCase):
    def test_absent_or_blank_is_no_agenda(self):
        self.assertIsNone(normalize_agenda(None))
        self.assertIsNone(normalize_agenda(""))
        self.assertIsNone(normalize_agenda("  \n "))
        self.assertIsNone(normalize_agenda([]))

    def test_text_is_trimmed_and_kept_line_for_line(self):
        self.assertEqual(normalize_agenda("\n Budget\nHiring \n"), "Budget\nHiring")

    def test_a_list_of_items_becomes_one_item_per_line(self):
        self.assertEqual(normalize_agenda(["Budget ", "", " Hiring"]), "Budget\nHiring")

    def test_other_shapes_are_refused(self):
        with self.assertRaises(ValueError):
            normalize_agenda({"items": ["Budget"]})
        with self.assertRaises(ValueError):
            normalize_agenda(["Budget", 3])

    def test_an_agenda_longer_than_the_record_accepts_is_refused(self):
        with self.assertRaises(ValueError):
            normalize_agenda("x" * (MAX_AGENDA_CHARS + 1))
        self.assertEqual(len(normalize_agenda("x" * MAX_AGENDA_CHARS)), MAX_AGENDA_CHARS)


def _appsync_response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class _ToolHarness(unittest.TestCase):
    """Runs a tool with AWS and HTTP replaced, keeping the mutation it sent."""

    def run_tool(self, module, **kwargs):
        sent = []

        def post(_url, headers=None, data=None, timeout=None):  # noqa: ARG001
            sent.append(json.loads(data))
            return _appsync_response(
                {
                    "data": {
                        "createVirtualParticipant": {
                            "id": "vp-1",
                            "meetingName": kwargs["meeting_name"],
                            "meetingPlatform": "ZOOM",
                            "meetingId": "123",
                            "status": "SCHEDULED",
                        }
                    }
                }
            )

        sfn = MagicMock()
        sfn.start_sync_execution.return_value = {"status": "SUCCEEDED", "output": "{}"}
        ssm = MagicMock()
        ssm.get_parameter.return_value = {
            "Parameter": {
                "Value": json.dumps({"LMAVirtualParticipantSchedulerStateMachine": "arn"})
            }
        }

        def client(name, *args, **kw):  # noqa: ARG001
            return {"stepfunctions": sfn, "ssm": ssm}.get(name, MagicMock())

        with (
            patch.object(module, "resolve_user_sub", return_value=None),
            patch.object(module, "has_zoom_credentials", return_value=False),
            patch("boto3.Session", return_value=MagicMock()),
            patch("boto3.client", side_effect=client),
            patch("botocore.auth.SigV4Auth", return_value=MagicMock()),
            patch("requests.post", side_effect=post),
        ):
            module.execute(**kwargs)
        return sent[0]["variables"]["input"]


class TestStartMeetingNow(_ToolHarness):
    BASE = {"meeting_name": "Weekly", "meeting_platform": "zoom", "meeting_id": "123"}

    def test_the_agenda_is_stored_on_the_vp_row(self):
        vp_input = self.run_tool(start_meeting_now, **self.BASE, agenda="Budget\nHiring")
        self.assertEqual(vp_input["agenda"], "Budget\nHiring")

    def test_without_an_agenda_the_row_is_written_as_before(self):
        vp_input = self.run_tool(start_meeting_now, **self.BASE)
        self.assertNotIn("agenda", vp_input)


class TestScheduleMeeting(_ToolHarness):
    def base(self):
        when = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        return {
            "meeting_name": "Weekly",
            "meeting_platform": "teams",
            "meeting_id": "123",
            "scheduled_time": when,
        }

    def test_the_agenda_is_stored_on_the_vp_row(self):
        vp_input = self.run_tool(schedule_meeting, **self.base(), agenda=["Budget", "Hiring"])
        self.assertEqual(vp_input["agenda"], "Budget\nHiring")
        self.assertTrue(vp_input["isScheduled"])

    def test_without_an_agenda_the_row_is_written_as_before(self):
        vp_input = self.run_tool(schedule_meeting, **self.base())
        self.assertNotIn("agenda", vp_input)


class TestToolSchemas(unittest.TestCase):
    def test_both_vp_tools_offer_an_optional_agenda(self):
        tools = _mcp_tools()
        for name in ("start_meeting_now", "schedule_meeting"):
            schema = tools[name]["inputSchema"]
            self.assertEqual(schema["properties"]["agenda"]["type"], "string", name)
            self.assertNotIn("agenda", schema.get("required", []), name)

    def test_the_handler_passes_the_agenda_to_both_tools(self):
        source = (HERE.parent / "index.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('agenda=tool_input.get("agenda"),'), 2)


if __name__ == "__main__":
    unittest.main()
