"""
Unit tests for MCP Analytics Lambda JSON-RPC protocol handler.
Tests MCP protocol messages (initialize, tools/list, tools/call, ping),
API Gateway vs BedrockAgentCore path detection, and tool routing.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

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
    "tools.schedule_meeting",
    "tools.start_meeting_now",
]:
    sys.modules[mod_name] = MagicMock()

import index  # noqa: E402


def api_gw_event(body_dict, user_id="testuser", username="Test User", is_admin="false", scope=None):
    """Create an API Gateway proxy event with MCP JSON-RPC body.

    `scope` is the key's scope as the authorizer passes it; None is a key
    stored before keys had scopes, which the authorizer passes without one.
    """
    authorizer = {"userId": user_id, "username": username, "isAdmin": is_admin}
    if scope is not None:
        authorizer["scope"] = scope
    return {
        "httpMethod": "POST",
        "body": json.dumps(body_dict),
        "requestContext": {"authorizer": authorizer},
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

    def test_tools_list_returns_all_tools_to_a_read_write_key(self):
        """tools/list offers a read-write key every tool."""
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        result = index.lambda_handler(api_gw_event(body, scope="read-write"), None)

        resp = json.loads(result["body"])
        tool_names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(tool_names, {tool["name"] for tool in index.TOOLS})
        for name in (
            "search_lma_meetings",
            "get_meeting_transcript",
            "get_meeting_summary",
            "list_meetings",
            "schedule_meeting",
            "start_meeting_now",
            "get_virtual_participant_status",
            "push_meeting_guidance",
            "delete_meeting",
            "get_meeting_context",
            "set_meeting_checklist",
            "update_scheduled_meeting",
            "get_meeting_consent",
        ):
            self.assertIn(name, tool_names)
        self.assertEqual(len(tool_names), 28)
        for removed in ("get_live_transcript", "unshare_meeting"):
            self.assertNotIn(removed, tool_names)

    def test_tools_list_offers_a_legacy_key_the_original_tools_and_the_read_tools(self):
        """A key stored before keys had scopes keeps every tool it was offered."""
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        for scope in (None, "", "legacy"):
            result = index.lambda_handler(api_gw_event(body, scope=scope), None)
            names = {t["name"] for t in json.loads(result["body"])["result"]["tools"]}
            for tool in index.MCP_TOOLS:
                self.assertIn(tool["name"], names, scope)
            self.assertIn("get_meeting_checklist", names)
            self.assertIn("get_meeting_record", names)
            for name in (
                "push_meeting_guidance",
                "delete_meeting",
                "ask_meeting_assistant",
                "set_meeting_context",
                "update_scheduled_meeting",
            ):
                self.assertNotIn(name, names, scope)

    def test_tools_list_offers_a_read_key_only_the_read_only_tools(self):
        """A key with the read scope, or an unknown one, is offered the tools it may call."""
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        for scope in ("read", "admin", "READ-WRITE"):
            result = index.lambda_handler(api_gw_event(body, scope=scope), None)
            tools = json.loads(result["body"])["result"]["tools"]
            self.assertTrue(tools, scope)
            for tool in tools:
                self.assertIs(tool["annotations"]["readOnlyHint"], True, (scope, tool["name"]))
            names = {t["name"] for t in tools}
            self.assertIn("get_meeting_consent", names)
            self.assertNotIn("start_meeting_now", names)
            self.assertNotIn("delete_meeting", names)

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


class TestKeyScopes(unittest.TestCase):
    """Only a read-write key calls a tool that changes something."""

    WRITE_TOOLS = [
        "schedule_meeting",
        "start_meeting_now",
        "push_meeting_guidance",
        "update_meeting_agenda",
        "end_meeting",
        "delete_meeting",
        "ask_meeting_assistant",
        "share_meeting",
        "regenerate_meeting_summary",
        "end_virtual_participant",
        "set_meeting_context",
        "set_meeting_checklist",
        "update_checklist_item",
        "update_meeting_metadata",
        "update_scheduled_meeting",
    ]

    def _call(self, name, arguments=None, scope=None, is_admin="false"):
        body = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {"meetingId": "m1"}},
        }
        event = api_gw_event(body, user_id="alice@example.com", is_admin=is_admin, scope=scope)
        return json.loads(index.lambda_handler(event, None)["body"])

    def test_every_tool_is_classified_by_its_annotation(self):
        """Each tool says whether it is read-only; the write list here is complete."""
        writes = sorted(t["name"] for t in index.TOOLS if not index.is_read_only(t["name"]))
        self.assertEqual(writes, sorted(self.WRITE_TOOLS))
        for tool in index.TOOLS:
            for hint in ("readOnlyHint", "destructiveHint", "idempotentHint"):
                self.assertIn(hint, tool.get("annotations", {}), (tool["name"], hint))

    def test_a_read_key_is_refused_every_write_tool_before_it_runs(self):
        from tools import meeting_actions, schedule_meeting

        meeting_actions.delete_meeting = MagicMock()
        schedule_meeting.execute = MagicMock()
        for scope in ("read", "admin", "READ-WRITE"):
            for name in self.WRITE_TOOLS:
                with self.subTest(scope=scope, tool=name):
                    resp = self._call(name, scope=scope)
                    self.assertTrue(resp["result"]["isError"])
                    self.assertIn("read-write", resp["result"]["content"][0]["text"])
        meeting_actions.delete_meeting.assert_not_called()
        schedule_meeting.execute.assert_not_called()

    def test_an_admin_read_key_is_refused_too(self):
        """Being an Admin does not widen what a read or legacy key may do."""
        for scope in ("read", None):
            resp = self._call(
                "delete_meeting",
                {"meetingId": "m1", "confirm": True},
                scope=scope,
                is_admin="true",
            )
            self.assertTrue(resp["result"]["isError"], scope)

    NEW_WRITE_TOOLS = [
        name for name in WRITE_TOOLS if name not in ("schedule_meeting", "start_meeting_now")
    ]

    def test_a_read_key_is_held_to_its_scope_on_a_body_that_is_not_json_rpc(self):
        """The API key endpoint applies the scope to an argument-shaped body too."""
        from tools import list_meetings, schedule_meeting, start_meeting_now

        schedule_meeting.execute = MagicMock()
        start_meeting_now.execute = MagicMock()
        bodies = [
            {"meetingName": "W", "meetingPlatform": "ZOOM", "meetingId": "1"},
            {
                "meetingName": "W",
                "meetingPlatform": "ZOOM",
                "meetingId": "1",
                "scheduledTime": "2099-01-01T10:00:00Z",
            },
        ]
        for body in bodies:
            result = index.lambda_handler(api_gw_event(body, scope="read"), None)
            self.assertEqual(result["statusCode"], 403)
            self.assertIn("read-write", json.loads(result["body"])["error"]["message"])
        schedule_meeting.execute.assert_not_called()
        start_meeting_now.execute.assert_not_called()
        # A read tool in that shape is still answered.
        list_meetings.execute = MagicMock(return_value={"meetings": []})
        result = index.lambda_handler(api_gw_event({"limit": 5}, scope="read"), None)
        self.assertEqual(result["statusCode"], 200)
        # A legacy key keeps the original tools in that shape, as before.
        start_meeting_now.execute = MagicMock(return_value={"virtualParticipantId": "vp1"})
        result = index.lambda_handler(api_gw_event(bodies[0]), None)
        self.assertEqual(result["statusCode"], 200)
        start_meeting_now.execute.assert_called_once()

    def test_a_legacy_key_keeps_schedule_meeting_and_start_meeting_now(self):
        from tools import schedule_meeting, start_meeting_now

        schedule_meeting.execute = MagicMock(return_value={"virtualParticipantId": "vp1"})
        start_meeting_now.execute = MagicMock(return_value={"virtualParticipantId": "vp2"})
        for scope in (None, "legacy"):
            for name, module in (
                ("schedule_meeting", schedule_meeting),
                ("start_meeting_now", start_meeting_now),
            ):
                with self.subTest(scope=scope, tool=name):
                    module.execute.reset_mock()
                    resp = self._call(
                        name,
                        {"meetingName": "W", "meetingPlatform": "ZOOM", "meetingId": "1"},
                        scope=scope,
                    )
                    self.assertNotIn("isError", resp["result"])
                    module.execute.assert_called_once()

    def test_a_legacy_key_is_refused_the_new_write_tools_before_they_run(self):
        from tools import meeting_actions

        meeting_actions.delete_meeting = MagicMock()
        for scope in (None, "legacy"):
            for name in self.NEW_WRITE_TOOLS:
                with self.subTest(scope=scope, tool=name):
                    resp = self._call(name, scope=scope)
                    self.assertTrue(resp["result"]["isError"])
                    text = resp["result"]["content"][0]["text"]
                    self.assertIn("read-write", text)
                    self.assertIn("before keys had scopes", text)
        meeting_actions.delete_meeting.assert_not_called()

    def test_a_legacy_key_calls_the_new_read_tools(self):
        from tools import access, meeting_record

        access.Caller = MagicMock(side_effect=lambda **kw: kw)
        meeting_record.execute = MagicMock(return_value={"meetingId": "m1"})
        resp = self._call("get_meeting_record", {"meetingId": "m1"}, scope=None)
        self.assertNotIn("isError", resp["result"])
        self.assertEqual(meeting_record.execute.call_args[0][0]["scope"], "legacy")

    def test_a_read_write_key_reaches_the_tool_with_its_caller(self):
        from tools import access, meeting_actions

        access.Caller = MagicMock(side_effect=lambda **kw: kw)
        meeting_actions.delete_meeting = MagicMock(return_value={"deleted": True})
        resp = self._call(
            "delete_meeting", {"meetingId": "m1", "confirm": True}, scope="read-write"
        )
        self.assertNotIn("isError", resp["result"])
        caller, meeting_id = meeting_actions.delete_meeting.call_args[0]
        self.assertEqual(meeting_id, "m1")
        self.assertEqual(meeting_actions.delete_meeting.call_args[1], {"confirm": True})
        self.assertEqual(
            caller, {"user_id": "alice@example.com", "is_admin": False, "scope": "read-write"}
        )

    def test_a_read_key_calls_a_read_tool(self):
        from tools import get_transcript

        get_transcript.execute = MagicMock(return_value={"segments": []})
        resp = self._call(
            "get_meeting_transcript",
            {"meetingId": "m1", "format": "json", "nextToken": "c", "maxSegments": 50},
            scope="read",
        )
        self.assertNotIn("isError", resp["result"])
        get_transcript.execute.assert_called_once()
        kwargs = get_transcript.execute.call_args[1]
        self.assertEqual((kwargs["next_token"], kwargs["max_segments"]), ("c", 50))
        self.assertEqual(kwargs["user_id"], "alice@example.com")


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
            call_kwargs[1].get("user_id") or call_kwargs[0][5]
            if len(call_kwargs[0]) > 5
            else call_kwargs[1].get("user_id"),
            "user123",
        )

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
    }
    WRITE_TOOLS = {"schedule_meeting", "start_meeting_now"}

    def _get_tools(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        result = index.lambda_handler(api_gw_event(body, scope="read-write"), None)
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


class TestServerVersion(unittest.TestCase):
    """serverInfo.version is the deployed LMA version, not a constant."""

    def _version(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        resp = json.loads(index.lambda_handler(api_gw_event(body), None)["body"])
        return resp["result"]["serverInfo"]["version"]

    def test_the_version_is_read_from_the_deployment(self):
        with patch.dict(os.environ, {"LMA_VERSION": "0.4.2"}):
            self.assertEqual(self._version(), "0.4.2")

    def test_a_deployment_without_one_says_unknown(self):
        with patch.dict(os.environ, {"LMA_VERSION": ""}):
            self.assertEqual(self._version(), "unknown")


def gateway_context(tool):
    """The Lambda context the AgentCore Gateway invokes a target with."""
    context = MagicMock()
    context.client_context.custom = {"bedrockAgentCoreToolName": f"LMAAnalyticsTarget___{tool}"}
    return context


class TestGatewayRouting(unittest.TestCase):
    """The gateway names the tool in the Lambda context; every tool is reachable."""

    CLAIMS = {"sub": "user123", "cognito:username": "bob", "cognito:groups": "Users"}

    def test_a_meeting_tool_is_routed_by_its_name_with_the_callers_identity(self):
        from tools import access, preparation

        access.Caller = MagicMock(side_effect=lambda **kw: kw)
        preparation.set_context = MagicMock(return_value={"context": "x"})
        event = bedrock_event({"meetingId": "m1", "context": "x"}, claims=self.CLAIMS)
        result = index.lambda_handler(event, gateway_context("set_meeting_context"))
        self.assertEqual(result["statusCode"], 200)
        caller, meeting_id, context = preparation.set_context.call_args[0]
        self.assertEqual((meeting_id, context), ("m1", "x"))
        # The gateway carries no key, so no key scope narrows it.
        self.assertEqual(caller, {"user_id": "user123", "is_admin": False, "scope": "read-write"})

    def test_every_tool_has_a_handler(self):
        self.assertEqual(set(index.TOOL_HANDLERS), {tool["name"] for tool in index.TOOLS})

    def test_an_original_tool_is_routed_by_its_name_whatever_its_arguments(self):
        from tools import get_transcript

        get_transcript.execute = MagicMock(return_value={"segments": []})
        event = bedrock_event({"meetingId": "m1"}, claims=self.CLAIMS)
        index.lambda_handler(event, gateway_context("get_meeting_transcript"))
        self.assertEqual(get_transcript.execute.call_args[1]["meeting_id"], "m1")

    def test_an_unknown_tool_name_is_refused(self):
        event = bedrock_event({"meetingId": "m1"}, claims=self.CLAIMS)
        result = index.lambda_handler(event, gateway_context("drop_everything"))
        self.assertEqual(result["statusCode"], 400)

    def test_a_named_call_without_an_identity_is_refused(self):
        from tools import preparation

        preparation.set_context = MagicMock()
        event = bedrock_event({"meetingId": "m1", "context": "x"}, claims={})
        result = index.lambda_handler(event, gateway_context("set_meeting_context"))
        self.assertEqual(result["statusCode"], 403)
        preparation.set_context.assert_not_called()

    def test_a_call_without_a_tool_name_is_matched_by_its_arguments(self):
        self.assertIsNone(index.gateway_tool_name(None))
        self.assertIsNone(index.gateway_tool_name(MagicMock()))
        self.assertEqual(index.gateway_tool_name(gateway_context("list_meetings")), "list_meetings")
        self.assertEqual(
            index.infer_tool({"virtualParticipantId": "v"}), "get_virtual_participant_status"
        )
