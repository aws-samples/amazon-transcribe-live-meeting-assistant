"""
Unit tests for MCP Analytics Lambda JSON-RPC protocol handler.
Tests MCP protocol messages (initialize, tools/list, tools/call, ping),
API Gateway vs BedrockAgentCore path detection, and tool routing.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock

# Set env vars before importing
os.environ["LOG_LEVEL"] = "WARNING"
os.environ.setdefault("GRAPHQL_API_ENDPOINT", "https://test.appsync.amazonaws.com/graphql")
os.environ.setdefault("MEETINGS_TABLE_NAME", "test-meetings")
os.environ.setdefault("APPSYNC_GRAPHQL_URL", "https://test.appsync.amazonaws.com/graphql")
os.environ.setdefault("LMA_WEB_APP_URL", "https://test-lma.example.com")

# Mock tool imports before importing index
for mod_name in [
    "tools",
    "tools.search_meetings",
    "tools.get_transcript",
    "tools.get_summary",
    "tools.list_meetings",
    "tools.list_active_meetings",
    "tools.get_meeting_agenda",
    "tools.get_meeting_frames",
    "tools.get_live_context",
    "tools.schedule_meeting",
    "tools.start_meeting_now",
]:
    sys.modules[mod_name] = MagicMock()

import index  # noqa: E402


def api_gw_event(body_dict, user_id="testuser", username="Test User", is_admin="false"):
    """Create an API Gateway proxy event with MCP JSON-RPC body."""
    return {
        "httpMethod": "POST",
        "body": json.dumps(body_dict),
        "requestContext": {
            "authorizer": {
                "userId": user_id,
                "username": username,
                "isAdmin": is_admin,
            }
        },
    }


def bedrock_event(tool_params, claims=None):
    """Create a BedrockAgentCore Gateway event."""
    event = dict(tool_params)
    if claims:
        event["requestContext"] = {"authorizer": {"claims": claims}}
    return event


class TestMCPInitialize(unittest.TestCase):
    """Test MCP initialize method."""

    def test_initialize_returns_capabilities(self):
        """Initialize returns server info and protocol version."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        event = api_gw_event(body)
        result = index.lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 200)
        resp = json.loads(result["body"])
        self.assertEqual(resp["jsonrpc"], "2.0")
        self.assertEqual(resp["id"], 1)
        self.assertIn("protocolVersion", resp["result"])
        self.assertEqual(resp["result"]["serverInfo"]["name"], "lma-mcp-server")

    def test_initialize_has_tools_capability(self):
        """Initialize declares tools capability."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        result = index.lambda_handler(api_gw_event(body), None)
        resp = json.loads(result["body"])
        self.assertIn("tools", resp["result"]["capabilities"])


class TestMCPToolsList(unittest.TestCase):
    """Test MCP tools/list method."""

    def test_tools_list_returns_all_tools(self):
        """tools/list returns all 10 tools."""
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result["body"])
        tools = resp["result"]["tools"]
        self.assertEqual(len(tools), 10)
        tool_names = [t["name"] for t in tools]
        self.assertIn("search_lma_meetings", tool_names)
        self.assertIn("get_meeting_transcript", tool_names)
        self.assertIn("get_meeting_summary", tool_names)
        self.assertIn("list_meetings", tool_names)
        self.assertIn("list_active_meetings", tool_names)
        self.assertIn("get_meeting_agenda", tool_names)
        self.assertIn("get_meeting_frames", tool_names)
        self.assertIn("schedule_meeting", tool_names)
        self.assertIn("start_meeting_now", tool_names)
        self.assertIn("get_virtual_participant_status", tool_names)

    def test_tools_have_input_schema(self):
        """Each tool has an inputSchema."""
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result["body"])
        for tool in resp["result"]["tools"]:
            self.assertIn("inputSchema", tool, f"Tool {tool['name']} missing inputSchema")
            self.assertEqual(tool["inputSchema"]["type"], "object")


class TestMCPPing(unittest.TestCase):
    """Test MCP ping method."""

    def test_ping_returns_empty_result(self):
        """Ping returns empty result."""
        body = {"jsonrpc": "2.0", "id": 99, "method": "ping"}
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result["body"])
        self.assertEqual(resp["id"], 99)
        self.assertEqual(resp["result"], {})


class TestMCPNotificationsInitialized(unittest.TestCase):
    """Test MCP notifications/initialized."""

    def test_notifications_initialized_returns_200(self):
        """Notification returns empty 200."""
        body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        result = index.lambda_handler(api_gw_event(body), None)
        self.assertEqual(result["statusCode"], 200)


class TestMCPToolCall(unittest.TestCase):
    """Test MCP tools/call method."""

    def test_tools_call_list_meetings(self):
        """tools/call routes to list_meetings tool."""
        from tools import list_meetings

        list_meetings.execute = MagicMock(return_value={"meetings": []})

        body = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_meetings", "arguments": {"limit": 5}},
        }
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result["body"])
        self.assertEqual(resp["id"], 3)
        self.assertIn("content", resp["result"])
        list_meetings.execute.assert_called_once()

    def test_tools_call_search_meetings(self):
        """tools/call routes to search_meetings tool."""
        from tools import search_meetings

        search_meetings.execute = MagicMock(return_value={"results": []})

        body = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "search_lma_meetings",
                "arguments": {"query": "test", "maxResults": 5},
            },
        }
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result["body"])
        self.assertIn("content", resp["result"])
        search_meetings.execute.assert_called_once()

    def test_tools_call_list_active_meetings(self):
        """tools/call routes to list_active_meetings with the caller's identity."""
        from tools import list_active_meetings

        list_active_meetings.execute = MagicMock(return_value={"meetings": [], "count": 0})

        body = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "list_active_meetings", "arguments": {"limit": 5}},
        }
        result = index.lambda_handler(
            api_gw_event(body, user_id="u1", username="U One", is_admin="true"), None
        )

        resp = json.loads(result["body"])
        self.assertEqual(json.loads(resp["result"]["content"][0]["text"])["count"], 0)
        list_active_meetings.execute.assert_called_once_with(limit=5, user_id="u1", is_admin=True)

    def test_tools_call_list_active_meetings_without_arguments_passes_no_limit(self):
        from tools import list_active_meetings

        list_active_meetings.execute = MagicMock(return_value={"meetings": [], "count": 0})

        body = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "list_active_meetings"},
        }
        index.lambda_handler(api_gw_event(body), None)

        list_active_meetings.execute.assert_called_once_with(
            limit=None, user_id="testuser", is_admin=False
        )

    def test_tools_call_list_active_meetings_does_not_run_list_meetings(self):
        from tools import list_active_meetings, list_meetings

        list_active_meetings.execute = MagicMock(return_value={})
        list_meetings.execute = MagicMock(return_value={})

        body = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "list_active_meetings", "arguments": {}},
        }
        index.lambda_handler(api_gw_event(body), None)

        list_meetings.execute.assert_not_called()

    def test_a_refused_limit_is_reported_as_a_tool_error(self):
        from tools import list_active_meetings

        list_active_meetings.execute = MagicMock(side_effect=ValueError("limit must be an integer"))

        body = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "list_active_meetings", "arguments": {"limit": True}},
        }
        resp = json.loads(index.lambda_handler(api_gw_event(body), None)["body"])

        self.assertTrue(resp["result"]["isError"])
        self.assertIn("limit must be an integer", resp["result"]["content"][0]["text"])

    def test_tools_call_get_meeting_agenda(self):
        """tools/call routes to get_meeting_agenda with the caller's identity."""
        from tools import get_meeting_agenda

        get_meeting_agenda.execute = MagicMock(return_value={"hasAgenda": False})

        body = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "get_meeting_agenda", "arguments": {"meetingId": "m1"}},
        }
        result = index.lambda_handler(
            api_gw_event(body, user_id="u1", username="U One", is_admin="true"), None
        )

        resp = json.loads(result["body"])
        self.assertFalse(json.loads(resp["result"]["content"][0]["text"])["hasAgenda"])
        get_meeting_agenda.execute.assert_called_once_with(
            meeting_id="m1", user_id="u1", is_admin=True
        )

    def test_tools_call_get_meeting_frames(self):
        """tools/call routes to get_meeting_frames with every argument and the identity."""
        from tools import get_meeting_frames

        get_meeting_frames.execute = MagicMock(return_value={"count": 0})

        body = {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "get_meeting_frames",
                "arguments": {"meetingId": "m1", "last": 5, "fromSeconds": 1.5, "toSeconds": 90},
            },
        }
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result["body"])
        self.assertEqual(json.loads(resp["result"]["content"][0]["text"])["count"], 0)
        get_meeting_frames.execute.assert_called_once_with(
            meeting_id="m1",
            last=5,
            from_seconds=1.5,
            to_seconds=90,
            user_id="testuser",
            is_admin=False,
        )

    def test_tools_call_get_meeting_frames_without_optional_arguments_passes_none(self):
        from tools import get_meeting_frames

        get_meeting_frames.execute = MagicMock(return_value={})

        body = {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {"name": "get_meeting_frames", "arguments": {"meetingId": "m1"}},
        }
        index.lambda_handler(api_gw_event(body), None)

        get_meeting_frames.execute.assert_called_once_with(
            meeting_id="m1",
            last=None,
            from_seconds=None,
            to_seconds=None,
            user_id="testuser",
            is_admin=False,
        )

    def test_the_new_tools_do_not_run_each_other_or_list_meetings(self):
        from tools import get_meeting_agenda, get_meeting_frames, list_meetings

        for module in (get_meeting_agenda, get_meeting_frames, list_meetings):
            module.execute = MagicMock(return_value={})

        for id_, name in ((15, "get_meeting_agenda"), (16, "get_meeting_frames")):
            body = {
                "jsonrpc": "2.0",
                "id": id_,
                "method": "tools/call",
                "params": {"name": name, "arguments": {"meetingId": "m1"}},
            }
            index.lambda_handler(api_gw_event(body), None)

        get_meeting_agenda.execute.assert_called_once()
        get_meeting_frames.execute.assert_called_once()
        list_meetings.execute.assert_not_called()

    def test_a_refusal_from_a_new_tool_is_reported_as_a_tool_error(self):
        from tools import get_meeting_agenda, get_meeting_frames

        get_meeting_agenda.execute = MagicMock(
            side_effect=PermissionError("Access denied to this meeting")
        )
        get_meeting_frames.execute = MagicMock(
            side_effect=ValueError("last must be a whole number")
        )

        for id_, name in ((17, "get_meeting_agenda"), (18, "get_meeting_frames")):
            body = {
                "jsonrpc": "2.0",
                "id": id_,
                "method": "tools/call",
                "params": {"name": name, "arguments": {"meetingId": "m1"}},
            }
            resp = json.loads(index.lambda_handler(api_gw_event(body), None)["body"])

            self.assertTrue(resp["result"]["isError"])
            self.assertTrue(resp["result"]["content"][0]["text"].startswith("Error: "))

    def test_tools_call_unknown_tool(self):
        """tools/call with unknown tool returns error."""
        body = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        }
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result["body"])
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32602)

    def test_tools_call_error_returns_is_error(self):
        """tools/call that throws returns isError=True in result."""
        from tools import list_meetings

        list_meetings.execute = MagicMock(side_effect=Exception("DB timeout"))

        body = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "list_meetings", "arguments": {}},
        }
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result["body"])
        self.assertTrue(resp["result"].get("isError"))
        self.assertIn("Error", resp["result"]["content"][0]["text"])


class TestGetLiveContextRouting(unittest.TestCase):
    """get_live_context is listed only while its setting is on, and only on this endpoint."""

    def setUp(self):
        from tools import get_live_context

        saved = {name: getattr(get_live_context, name) for name in ("ON", "live_context_state")}
        self.addCleanup(lambda: [setattr(get_live_context, k, v) for k, v in saved.items()])
        get_live_context.ON = "on"
        get_live_context.execute = MagicMock(return_value={"lines": [], "truncated": False})

    def _listed(self, state):
        from tools import get_live_context

        get_live_context.live_context_state = MagicMock(return_value=state)
        body = {"jsonrpc": "2.0", "id": 20, "method": "tools/list", "params": {}}
        tools = json.loads(index.lambda_handler(api_gw_event(body), None)["body"])["result"][
            "tools"
        ]
        return [tool["name"] for tool in tools]

    def test_tools_list_offers_it_while_the_setting_is_on(self):
        names = self._listed("on")
        self.assertIn("get_live_context", names)
        self.assertEqual(len(names), len(index.MCP_TOOLS))

    def test_tools_list_hides_it_while_the_setting_is_off_or_unreadable(self):
        for state in ("off", "unreadable"):
            with self.subTest(state=state):
                names = self._listed(state)
                self.assertNotIn("get_live_context", names)
                self.assertEqual(len(names), len(index.MCP_TOOLS) - 1)

    def test_the_listed_tool_is_read_only_and_states_its_limits(self):
        tool = {t["name"]: t for t in index.MCP_TOOLS}["get_live_context"]
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertEqual(tool["inputSchema"]["required"], ["meetingId"])
        for phrase in ("5 seconds", "segmentId", "truncated", "nextCursor", "60 seconds", "2,000"):
            self.assertIn(phrase, tool["description"])
        self.assertIn("get_meeting_transcript", tool["description"])

    def test_tools_call_routes_every_argument_and_the_caller(self):
        from tools import get_live_context

        body = {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {
                "name": "get_live_context",
                "arguments": {
                    "meetingId": "m-1",
                    "cursor": "abc",
                    "maxLines": 20,
                    "includeChat": False,
                },
            },
        }
        resp = json.loads(
            index.lambda_handler(api_gw_event(body, user_id="u1", is_admin="true"), None)["body"]
        )

        self.assertEqual(json.loads(resp["result"]["content"][0]["text"])["truncated"], False)
        get_live_context.execute.assert_called_once_with(
            meeting_id="m-1",
            cursor="abc",
            max_lines=20,
            include_chat=False,
            user_id="u1",
            is_admin=True,
        )

    def test_tools_call_without_arguments_passes_nothing_in(self):
        from tools import get_live_context

        body = {
            "jsonrpc": "2.0",
            "id": 22,
            "method": "tools/call",
            "params": {"name": "get_live_context"},
        }
        index.lambda_handler(api_gw_event(body), None)

        get_live_context.execute.assert_called_once_with(
            meeting_id=None,
            cursor=None,
            max_lines=None,
            include_chat=None,
            user_id="testuser",
            is_admin=False,
        )

    def test_a_refusal_is_a_tool_error(self):
        from tools import get_live_context

        get_live_context.execute = MagicMock(
            side_effect=PermissionError("Access denied to this meeting")
        )
        body = {
            "jsonrpc": "2.0",
            "id": 23,
            "method": "tools/call",
            "params": {"name": "get_live_context", "arguments": {"meetingId": "m-1"}},
        }
        resp = json.loads(index.lambda_handler(api_gw_event(body), None)["body"])

        self.assertTrue(resp["result"]["isError"])
        self.assertEqual(
            resp["result"]["content"][0]["text"], "Error: Access denied to this meeting"
        )

    def test_the_gateway_path_never_runs_it(self):
        from tools import get_live_context, list_meetings

        list_meetings.execute = MagicMock(return_value={"meetings": []})
        for tool_input in (
            {"meetingId": "m-1"},
            {"meetingId": "m-1", "cursor": "c"},
            {"maxLines": 5},
        ):
            index.lambda_handler(bedrock_event(tool_input, claims={"sub": "user123"}), None)

        get_live_context.execute.assert_not_called()


class TestUnknownMethod(unittest.TestCase):
    """Test unknown JSON-RPC method."""

    def test_unknown_method_returns_error(self):
        """Unknown method returns -32601 error."""
        body = {"jsonrpc": "2.0", "id": 7, "method": "resources/list"}
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result["body"])
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)


class TestPathDetection(unittest.TestCase):
    """Test API Gateway vs BedrockAgentCore path detection."""

    def test_api_gateway_path_detected(self):
        """Events with httpMethod+body are detected as API Gateway."""
        from tools import list_meetings

        list_meetings.execute = MagicMock(return_value={"meetings": []})

        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        event = api_gw_event(body)
        result = index.lambda_handler(event, None)
        # Should return JSON-RPC response (not MCP raw response)
        resp = json.loads(result["body"])
        self.assertIn("jsonrpc", resp)

    def test_bedrock_path_uses_claims(self):
        """Events without httpMethod use claims for user context."""
        from tools import list_meetings

        list_meetings.execute = MagicMock(return_value={"meetings": []})

        event = bedrock_event(
            {"limit": 10},
            claims={"sub": "user123", "cognito:username": "bob", "cognito:groups": "Users"},
        )
        index.lambda_handler(event, None)
        # Should call list_meetings (inferred from 'limit' param)
        list_meetings.execute.assert_called_once()
        call_kwargs = list_meetings.execute.call_args
        self.assertEqual(
            (
                call_kwargs[1].get("user_id") or call_kwargs[0][5]
                if len(call_kwargs[0]) > 5
                else call_kwargs[1].get("user_id")
            ),
            "user123",
        )

    def test_bedrock_path_never_runs_list_active_meetings(self):
        """Tool input on the gateway path is read by its shape, and no shape is this tool's."""
        from tools import list_active_meetings, list_meetings

        list_active_meetings.execute = MagicMock(return_value={})
        list_meetings.execute = MagicMock(return_value={"meetings": []})

        for tool_input in ({"limit": 5}, {}, {"status": "STARTED"}, {"limit": 5, "active": True}):
            index.lambda_handler(bedrock_event(tool_input, claims={"sub": "user123"}), None)

        list_active_meetings.execute.assert_not_called()

    def test_bedrock_path_never_runs_the_agenda_and_frames_tools(self):
        """Tool input on the gateway path is read by its shape, and no shape is theirs."""
        from tools import get_meeting_agenda, get_meeting_frames, list_meetings

        get_meeting_agenda.execute = MagicMock(return_value={})
        get_meeting_frames.execute = MagicMock(return_value={})
        list_meetings.execute = MagicMock(return_value={"meetings": []})

        shapes = (
            {"meetingId": "m1"},
            {"meetingId": "m1", "last": 3},
            {"meetingId": "m1", "fromSeconds": 1, "toSeconds": 9},
        )
        for tool_input in shapes:
            index.lambda_handler(bedrock_event(tool_input, claims={"sub": "user123"}), None)

        get_meeting_agenda.execute.assert_not_called()
        get_meeting_frames.execute.assert_not_called()

    def test_bedrock_path_without_identity_is_rejected(self):
        """A request with no caller identity is answered with 403."""
        from tools import list_meetings

        list_meetings.execute = MagicMock(return_value={"meetings": []})

        result = index.lambda_handler(bedrock_event({"limit": 5}), None)

        self.assertEqual(result["statusCode"], 403)
        list_meetings.execute.assert_not_called()

    def test_bedrock_path_with_empty_claims_is_rejected(self):
        """An empty claims envelope carries no caller either."""
        from tools import list_meetings

        list_meetings.execute = MagicMock(return_value={"meetings": []})

        result = index.lambda_handler(bedrock_event({"limit": 5}, claims={}), None)

        self.assertEqual(result["statusCode"], 403)
        list_meetings.execute.assert_not_called()

    def test_rejection_message_points_at_the_api_key_endpoint(self):
        """The caller is told how to make an identity-carrying request."""
        result = index.lambda_handler(bedrock_event({"limit": 5}), None)

        self.assertIn("API key", json.loads(result["body"])["error"]["message"])

    def test_api_gateway_path_without_authorizer_context_is_rejected(self):
        """Both entry paths answer 403 when the request resolves to no user."""
        from tools import list_meetings

        list_meetings.execute = MagicMock(return_value={"meetings": []})

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_meetings", "arguments": {"limit": 1}},
        }
        event = {"httpMethod": "POST", "body": json.dumps(body), "requestContext": {}}
        result = index.lambda_handler(event, None)

        self.assertEqual(result["statusCode"], 403)
        list_meetings.execute.assert_not_called()

    def test_api_gateway_path_with_empty_authorizer_context_is_rejected(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        event = {
            "httpMethod": "POST",
            "body": json.dumps(body),
            "requestContext": {"authorizer": {}},
        }

        self.assertEqual(index.lambda_handler(event, None)["statusCode"], 403)


class TestAdminGroupClaim(unittest.TestCase):
    """Group membership is matched on whole group names, in both claim shapes."""

    def test_admin_group_as_list(self):
        self.assertTrue(index.is_admin_claim({"cognito:groups": ["Users", "Admin"]}))

    def test_admin_group_as_comma_joined_string(self):
        self.assertTrue(index.is_admin_claim({"cognito:groups": "Users,Admin"}))

    def test_admin_group_as_single_string(self):
        self.assertTrue(index.is_admin_claim({"cognito:groups": "Admin"}))

    def test_non_admin_groups(self):
        for groups in ([], ["Users"], "Users", "Users,Guests", None):
            with self.subTest(groups=groups):
                self.assertFalse(index.is_admin_claim({"cognito:groups": groups}))

    def test_a_group_name_that_merely_contains_admin_is_not_the_admin_group(self):
        for groups in ("Administrators", ["Administrators"], "NotAdmin"):
            with self.subTest(groups=groups):
                self.assertFalse(index.is_admin_claim({"cognito:groups": groups}))

    def test_absent_claim(self):
        self.assertFalse(index.is_admin_claim({}))


class TestAPIGatewayUserContext(unittest.TestCase):
    """Test user context extraction from API Gateway authorizer."""

    def test_admin_user_from_authorizer(self):
        """Admin flag is extracted from authorizer context."""
        from tools import list_meetings

        list_meetings.execute = MagicMock(return_value={})

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_meetings", "arguments": {"limit": 1}},
        }
        event = api_gw_event(body, user_id="admin1", username="Admin User", is_admin="true")
        index.lambda_handler(event, None)

        call_kwargs = list_meetings.execute.call_args
        # is_admin should be True
        if call_kwargs[1]:
            self.assertTrue(call_kwargs[1].get("is_admin"))


class TestReadOnlyHintAnnotations(unittest.TestCase):
    """Verify read-only tools advertise readOnlyHint=True annotations."""

    READ_ONLY_TOOLS = {
        "search_lma_meetings",
        "get_meeting_transcript",
        "get_meeting_summary",
        "list_meetings",
        "list_active_meetings",
        "get_meeting_agenda",
        "get_meeting_frames",
    }
    WRITE_TOOLS = {"schedule_meeting", "start_meeting_now"}

    def _get_tools(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        result = index.lambda_handler(api_gw_event(body), None)
        return json.loads(result["body"])["result"]["tools"]

    def test_read_only_tools_have_annotation(self):
        for tool in self._get_tools():
            if tool["name"] in self.READ_ONLY_TOOLS:
                self.assertEqual(
                    tool.get("annotations", {}).get("readOnlyHint"),
                    True,
                    f"{tool['name']} must declare readOnlyHint=True",
                )

    def test_write_tools_do_not_have_annotation(self):
        for tool in self._get_tools():
            if tool["name"] in self.WRITE_TOOLS:
                self.assertNotEqual(
                    tool.get("annotations", {}).get("readOnlyHint"),
                    True,
                    f"{tool['name']} must NOT declare readOnlyHint=True",
                )


class TestJSONRPCResponseFormat(unittest.TestCase):
    """Test JSON-RPC response formatting."""

    def test_jsonrpc_response_format(self):
        """Responses include jsonrpc version and matching id."""
        result = index.jsonrpc_response(42, {"test": True})
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertEqual(body["id"], 42)
        self.assertEqual(body["result"], {"test": True})

    def test_jsonrpc_error_format(self):
        """Error responses include error code and message."""
        result = index.jsonrpc_error(42, -32600, "Invalid Request")
        body = json.loads(result["body"])
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertEqual(body["id"], 42)
        self.assertEqual(body["error"]["code"], -32600)
        self.assertEqual(body["error"]["message"], "Invalid Request")


if __name__ == "__main__":
    unittest.main()
