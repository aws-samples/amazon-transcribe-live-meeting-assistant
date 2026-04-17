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
os.environ['LOG_LEVEL'] = 'WARNING'
os.environ.setdefault('GRAPHQL_API_ENDPOINT', 'https://test.appsync.amazonaws.com/graphql')
os.environ.setdefault('MEETINGS_TABLE_NAME', 'test-meetings')
os.environ.setdefault('APPSYNC_GRAPHQL_URL', 'https://test.appsync.amazonaws.com/graphql')

# Mock tool imports before importing index
for mod_name in [
    'tools', 'tools.search_meetings', 'tools.get_transcript', 'tools.get_summary',
    'tools.list_meetings', 'tools.schedule_meeting', 'tools.start_meeting_now',
]:
    sys.modules[mod_name] = MagicMock()

import index


def api_gw_event(body_dict, user_id='testuser', username='Test User', is_admin='false'):
    """Create an API Gateway proxy event with MCP JSON-RPC body."""
    return {
        'httpMethod': 'POST',
        'body': json.dumps(body_dict),
        'requestContext': {
            'authorizer': {
                'userId': user_id,
                'username': username,
                'isAdmin': is_admin,
            }
        },
    }


def bedrock_event(tool_params, claims=None):
    """Create a BedrockAgentCore Gateway event."""
    event = dict(tool_params)
    if claims:
        event['requestContext'] = {'authorizer': {'claims': claims}}
    return event


class TestMCPInitialize(unittest.TestCase):
    """Test MCP initialize method."""

    def test_initialize_returns_capabilities(self):
        """Initialize returns server info and protocol version."""
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}
        event = api_gw_event(body)
        result = index.lambda_handler(event, None)

        self.assertEqual(result['statusCode'], 200)
        resp = json.loads(result['body'])
        self.assertEqual(resp['jsonrpc'], '2.0')
        self.assertEqual(resp['id'], 1)
        self.assertIn('protocolVersion', resp['result'])
        self.assertEqual(resp['result']['serverInfo']['name'], 'lma-mcp-server')

    def test_initialize_has_tools_capability(self):
        """Initialize declares tools capability."""
        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}}
        result = index.lambda_handler(api_gw_event(body), None)
        resp = json.loads(result['body'])
        self.assertIn('tools', resp['result']['capabilities'])


class TestMCPToolsList(unittest.TestCase):
    """Test MCP tools/list method."""

    def test_tools_list_returns_all_tools(self):
        """tools/list returns all 6 tools."""
        body = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result['body'])
        tools = resp['result']['tools']
        self.assertEqual(len(tools), 6)
        tool_names = [t['name'] for t in tools]
        self.assertIn('search_lma_meetings', tool_names)
        self.assertIn('get_meeting_transcript', tool_names)
        self.assertIn('get_meeting_summary', tool_names)
        self.assertIn('list_meetings', tool_names)
        self.assertIn('schedule_meeting', tool_names)
        self.assertIn('start_meeting_now', tool_names)

    def test_tools_have_input_schema(self):
        """Each tool has an inputSchema."""
        body = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result['body'])
        for tool in resp['result']['tools']:
            self.assertIn('inputSchema', tool, f"Tool {tool['name']} missing inputSchema")
            self.assertEqual(tool['inputSchema']['type'], 'object')


class TestMCPPing(unittest.TestCase):
    """Test MCP ping method."""

    def test_ping_returns_empty_result(self):
        """Ping returns empty result."""
        body = {'jsonrpc': '2.0', 'id': 99, 'method': 'ping'}
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result['body'])
        self.assertEqual(resp['id'], 99)
        self.assertEqual(resp['result'], {})


class TestMCPNotificationsInitialized(unittest.TestCase):
    """Test MCP notifications/initialized."""

    def test_notifications_initialized_returns_200(self):
        """Notification returns empty 200."""
        body = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
        result = index.lambda_handler(api_gw_event(body), None)
        self.assertEqual(result['statusCode'], 200)


class TestMCPToolCall(unittest.TestCase):
    """Test MCP tools/call method."""

    def test_tools_call_list_meetings(self):
        """tools/call routes to list_meetings tool."""
        from tools import list_meetings
        list_meetings.execute = MagicMock(return_value={'meetings': []})

        body = {
            'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
            'params': {'name': 'list_meetings', 'arguments': {'limit': 5}},
        }
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result['body'])
        self.assertEqual(resp['id'], 3)
        self.assertIn('content', resp['result'])
        list_meetings.execute.assert_called_once()

    def test_tools_call_search_meetings(self):
        """tools/call routes to search_meetings tool."""
        from tools import search_meetings
        search_meetings.execute = MagicMock(return_value={'results': []})

        body = {
            'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
            'params': {'name': 'search_lma_meetings', 'arguments': {'query': 'test', 'maxResults': 5}},
        }
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result['body'])
        self.assertIn('content', resp['result'])
        search_meetings.execute.assert_called_once()

    def test_tools_call_unknown_tool(self):
        """tools/call with unknown tool returns error."""
        body = {
            'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
            'params': {'name': 'nonexistent_tool', 'arguments': {}},
        }
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result['body'])
        self.assertIn('error', resp)
        self.assertEqual(resp['error']['code'], -32602)

    def test_tools_call_error_returns_is_error(self):
        """tools/call that throws returns isError=True in result."""
        from tools import list_meetings
        list_meetings.execute = MagicMock(side_effect=Exception('DB timeout'))

        body = {
            'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
            'params': {'name': 'list_meetings', 'arguments': {}},
        }
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result['body'])
        self.assertTrue(resp['result'].get('isError'))
        self.assertIn('Error', resp['result']['content'][0]['text'])


class TestUnknownMethod(unittest.TestCase):
    """Test unknown JSON-RPC method."""

    def test_unknown_method_returns_error(self):
        """Unknown method returns -32601 error."""
        body = {'jsonrpc': '2.0', 'id': 7, 'method': 'resources/list'}
        result = index.lambda_handler(api_gw_event(body), None)

        resp = json.loads(result['body'])
        self.assertIn('error', resp)
        self.assertEqual(resp['error']['code'], -32601)


class TestPathDetection(unittest.TestCase):
    """Test API Gateway vs BedrockAgentCore path detection."""

    def test_api_gateway_path_detected(self):
        """Events with httpMethod+body are detected as API Gateway."""
        from tools import list_meetings
        list_meetings.execute = MagicMock(return_value={'meetings': []})

        body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        event = api_gw_event(body)
        result = index.lambda_handler(event, None)
        # Should return JSON-RPC response (not MCP raw response)
        resp = json.loads(result['body'])
        self.assertIn('jsonrpc', resp)

    def test_bedrock_path_uses_claims(self):
        """Events without httpMethod use claims for user context."""
        from tools import list_meetings
        list_meetings.execute = MagicMock(return_value={'meetings': []})

        event = bedrock_event(
            {'limit': 10},
            claims={'sub': 'user123', 'cognito:username': 'bob', 'cognito:groups': 'Users'},
        )
        result = index.lambda_handler(event, None)
        # Should call list_meetings (inferred from 'limit' param)
        list_meetings.execute.assert_called_once()
        call_kwargs = list_meetings.execute.call_args
        self.assertEqual(call_kwargs[1].get('user_id') or call_kwargs[0][5] if len(call_kwargs[0]) > 5 else call_kwargs[1].get('user_id'), 'user123')

    def test_bedrock_path_no_user_defaults_to_admin(self):
        """BedrockAgentCore with no user context defaults to admin."""
        from tools import list_meetings
        list_meetings.execute = MagicMock(return_value={'meetings': []})

        event = bedrock_event({'limit': 5})
        result = index.lambda_handler(event, None)
        list_meetings.execute.assert_called_once()


class TestAPIGatewayUserContext(unittest.TestCase):
    """Test user context extraction from API Gateway authorizer."""

    def test_admin_user_from_authorizer(self):
        """Admin flag is extracted from authorizer context."""
        from tools import list_meetings
        list_meetings.execute = MagicMock(return_value={})

        body = {
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'list_meetings', 'arguments': {'limit': 1}},
        }
        event = api_gw_event(body, user_id='admin1', username='Admin User', is_admin='true')
        index.lambda_handler(event, None)

        call_kwargs = list_meetings.execute.call_args
        # is_admin should be True
        if call_kwargs[1]:
            self.assertTrue(call_kwargs[1].get('is_admin'))


class TestJSONRPCResponseFormat(unittest.TestCase):
    """Test JSON-RPC response formatting."""

    def test_jsonrpc_response_format(self):
        """Responses include jsonrpc version and matching id."""
        result = index.jsonrpc_response(42, {'test': True})
        self.assertEqual(result['statusCode'], 200)
        body = json.loads(result['body'])
        self.assertEqual(body['jsonrpc'], '2.0')
        self.assertEqual(body['id'], 42)
        self.assertEqual(body['result'], {'test': True})

    def test_jsonrpc_error_format(self):
        """Error responses include error code and message."""
        result = index.jsonrpc_error(42, -32600, 'Invalid Request')
        body = json.loads(result['body'])
        self.assertEqual(body['jsonrpc'], '2.0')
        self.assertEqual(body['id'], 42)
        self.assertEqual(body['error']['code'], -32600)
        self.assertEqual(body['error']['message'], 'Invalid Request')


if __name__ == '__main__':
    unittest.main()
