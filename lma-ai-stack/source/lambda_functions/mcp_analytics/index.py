#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
MCP Analytics Lambda Function
Implements MCP protocol tools for LMA meeting data access
Provides 6 tools: search, get transcript, get summary, list meetings, schedule meeting, start meeting
"""

import json
import logging
import os
from typing import Dict, Any

# Import tool implementations
from tools import search_meetings, get_transcript, get_summary, list_meetings, schedule_meeting, start_meeting_now

logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main handler for MCP Analytics Lambda.
    Routes tool calls to appropriate implementations.
    Enforces user-based access control (UBAC).
    """
    logger.info(f"MCP Analytics full event: {json.dumps(event, default=str)}")
    logger.info(f"Event keys: {list(event.keys())}")
    
    try:
        # Detect API Gateway proxy integration (API key auth path)
        if 'httpMethod' in event and 'body' in event:
            body = event.get('body', '{}')
            tool_input = json.loads(body) if isinstance(body, str) else (body or {})
            authorizer = event.get('requestContext', {}).get('authorizer', {})
            user_id = authorizer.get('userId', 'api-key-user')
            username = authorizer.get('username', 'API Key User')
            is_admin = authorizer.get('isAdmin', 'false') == 'true'
            logger.info(f"API Gateway path - User: {username}, Admin: {is_admin}")

            # Handle MCP JSON-RPC protocol messages
            if 'jsonrpc' in tool_input and 'method' in tool_input:
                return handle_mcp_jsonrpc(tool_input, user_id, username, is_admin)
        else:
            # BedrockAgentCore Gateway path
            request_context = event.get('requestContext', {})
            authorizer = request_context.get('authorizer', {})
            claims = authorizer.get('claims', {})
            
            logger.info(f"requestContext keys: {list(request_context.keys())}")
            logger.info(f"authorizer keys: {list(authorizer.keys())}")
            logger.info(f"claims: {claims}")
            
            user_id = claims.get('sub')
            username = claims.get('cognito:username', claims.get('email', user_id))
            
            groups = claims.get('cognito:groups', '')
            is_admin = 'Admin' in groups if isinstance(groups, str) else 'Admin' in groups
            
            # WORKAROUND: AgentCore Gateway doesn't pass user context
            if not user_id:
                logger.warning("No user context from AgentCore Gateway - treating as admin")
                user_id = "mcp-server-user"
                username = "MCP Server User"
                is_admin = True
            
            tool_input = event
        
        logger.info(f"User: {username}, ID: {user_id}, Admin: {is_admin}")
        
        # Determine which tool based on input parameters
        if 'query' in tool_input and 'maxResults' in tool_input:
            tool_name = 'search_lma_meetings'
        elif 'meetingId' in tool_input and 'format' in tool_input:
            tool_name = 'get_meeting_transcript'
        elif 'meetingId' in tool_input and ('includeActionItems' in tool_input or 'includeTopics' in tool_input):
            tool_name = 'get_meeting_summary'
        elif 'meetingPlatform' in tool_input and 'scheduledTime' in tool_input:
            tool_name = 'schedule_meeting'
        elif 'meetingPlatform' in tool_input and 'meetingName' in tool_input and 'scheduledTime' not in tool_input:
            tool_name = 'start_meeting_now'
        elif 'limit' in tool_input or 'status' in tool_input or 'participant' in tool_input:
            tool_name = 'list_meetings'
        else:
            # Default to list_meetings if we can't determine
            tool_name = 'list_meetings'
        
        logger.info(f"Inferred tool: {tool_name}, Input: {json.dumps(tool_input)}")
        
        # Route to appropriate tool
        if tool_name == 'search_lma_meetings':
            result = search_meetings.execute(
                query=tool_input.get('query'),
                start_date=tool_input.get('startDate'),
                end_date=tool_input.get('endDate'),
                max_results=tool_input.get('maxResults', 10),
                user_id=user_id,
                is_admin=is_admin
            )
        
        elif tool_name == 'get_meeting_transcript':
            result = get_transcript.execute(
                meeting_id=tool_input.get('meetingId'),
                format=tool_input.get('format', 'text'),
                user_id=user_id,
                is_admin=is_admin
            )
        
        elif tool_name == 'get_meeting_summary':
            result = get_summary.execute(
                meeting_id=tool_input.get('meetingId'),
                include_action_items=tool_input.get('includeActionItems', True),
                include_topics=tool_input.get('includeTopics', True),
                user_id=user_id,
                is_admin=is_admin
            )
        
        elif tool_name == 'list_meetings':
            result = list_meetings.execute(
                start_date=tool_input.get('startDate'),
                end_date=tool_input.get('endDate'),
                participant=tool_input.get('participant'),
                status=tool_input.get('status', 'ALL'),
                limit=tool_input.get('limit', 20),
                user_id=user_id,
                is_admin=is_admin
            )
        
        elif tool_name == 'schedule_meeting':
            result = schedule_meeting.execute(
                meeting_name=tool_input.get('meetingName'),
                meeting_platform=tool_input.get('meetingPlatform'),
                meeting_id=tool_input.get('meetingId'),
                scheduled_time=tool_input.get('scheduledTime'),
                meeting_password=tool_input.get('meetingPassword'),
                user_id=user_id,
                is_admin=is_admin
            )
        
        elif tool_name == 'start_meeting_now':
            result = start_meeting_now.execute(
                meeting_name=tool_input.get('meetingName'),
                meeting_platform=tool_input.get('meetingPlatform'),
                meeting_id=tool_input.get('meetingId'),
                meeting_password=tool_input.get('meetingPassword'),
                user_id=user_id,
                is_admin=is_admin
            )
        
        else:
            return error_response(400, f"Unknown tool: {tool_name}")
        
        # Return MCP-formatted success response
        return success_response(result)
    
    except PermissionError as e:
        logger.warning(f"Permission denied: {e}")
        return error_response(403, str(e))
    
    except ValueError as e:
        logger.warning(f"Invalid input: {e}")
        return error_response(400, str(e))
    
    except Exception as e:
        logger.error(f"Error processing tool call: {e}", exc_info=True)
        return error_response(500, f"Internal error: {str(e)}")


MCP_TOOLS = [
    {
        "name": "search_lma_meetings",
        "description": "Search across all meeting transcripts and summaries using natural language queries",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language search query"},
                "startDate": {"type": "string", "description": "Optional ISO 8601 start date"},
                "endDate": {"type": "string", "description": "Optional ISO 8601 end date"},
                "maxResults": {"type": "number", "description": "Maximum results to return"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_meeting_transcript",
        "description": "Retrieve the complete transcript for a specific meeting",
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingId": {"type": "string", "description": "Meeting ID (CallId)"},
                "format": {"type": "string", "description": "Output format (json, text, or srt)"},
            },
            "required": ["meetingId"],
        },
    },
    {
        "name": "get_meeting_summary",
        "description": "Get AI-generated summary and action items for a meeting",
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingId": {"type": "string", "description": "Meeting ID (CallId)"},
                "includeActionItems": {"type": "boolean", "description": "Include action items"},
                "includeTopics": {"type": "boolean", "description": "Include key topics"},
            },
            "required": ["meetingId"],
        },
    },
    {
        "name": "list_meetings",
        "description": "List meetings with optional filters",
        "inputSchema": {
            "type": "object",
            "properties": {
                "startDate": {"type": "string", "description": "ISO 8601 start date"},
                "endDate": {"type": "string", "description": "ISO 8601 end date"},
                "participant": {"type": "string", "description": "Filter by participant name"},
                "status": {"type": "string", "description": "Meeting status filter"},
                "limit": {"type": "number", "description": "Maximum number of meetings to return"},
            },
        },
    },
    {
        "name": "schedule_meeting",
        "description": "Schedule a future meeting with virtual participant",
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingName": {"type": "string", "description": "Name/title of the meeting"},
                "meetingPlatform": {"type": "string", "description": "Platform (Zoom, Teams, Chime, Webex)"},
                "meetingId": {"type": "string", "description": "Meeting ID (numeric ID only)"},
                "scheduledTime": {"type": "string", "description": "ISO 8601 datetime"},
                "meetingPassword": {"type": "string", "description": "Optional meeting password"},
            },
            "required": ["meetingName", "meetingPlatform", "meetingId", "scheduledTime"],
        },
    },
    {
        "name": "start_meeting_now",
        "description": "Start a meeting immediately with virtual participant",
        "inputSchema": {
            "type": "object",
            "properties": {
                "meetingName": {"type": "string", "description": "Name/title of the meeting"},
                "meetingPlatform": {"type": "string", "description": "Platform (Zoom, Teams, Chime, Webex)"},
                "meetingId": {"type": "string", "description": "Meeting ID (numeric ID only)"},
                "meetingPassword": {"type": "string", "description": "Optional meeting password"},
            },
            "required": ["meetingName", "meetingPlatform", "meetingId"],
        },
    },
]


def handle_mcp_jsonrpc(msg, user_id, username, is_admin):
    """Handle MCP JSON-RPC 2.0 protocol messages."""
    method = msg.get('method')
    msg_id = msg.get('id')
    params = msg.get('params', {})

    logger.info(f"MCP JSON-RPC method={method}, id={msg_id}")

    if method == 'initialize':
        return jsonrpc_response(msg_id, {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "lma-mcp-server", "version": "0.2.30"},
        })

    if method == 'notifications/initialized':
        # Client acknowledgment — no response needed for notifications
        return {'statusCode': 200, 'headers': {'Content-Type': 'application/json'}, 'body': ''}

    if method == 'tools/list':
        return jsonrpc_response(msg_id, {"tools": MCP_TOOLS})

    if method == 'tools/call':
        tool_name = params.get('name')
        arguments = params.get('arguments', {})
        return execute_tool_call(msg_id, tool_name, arguments, user_id, username, is_admin)

    if method == 'ping':
        return jsonrpc_response(msg_id, {})

    return jsonrpc_error(msg_id, -32601, f"Method not found: {method}")


def execute_tool_call(msg_id, tool_name, arguments, user_id, username, is_admin):
    """Execute a tool call and return MCP JSON-RPC response."""
    try:
        if tool_name == 'search_lma_meetings':
            result = search_meetings.execute(
                query=arguments.get('query'), start_date=arguments.get('startDate'),
                end_date=arguments.get('endDate'), max_results=arguments.get('maxResults', 10),
                user_id=user_id, is_admin=is_admin)
        elif tool_name == 'get_meeting_transcript':
            result = get_transcript.execute(
                meeting_id=arguments.get('meetingId'), format=arguments.get('format', 'text'),
                user_id=user_id, is_admin=is_admin)
        elif tool_name == 'get_meeting_summary':
            result = get_summary.execute(
                meeting_id=arguments.get('meetingId'),
                include_action_items=arguments.get('includeActionItems', True),
                include_topics=arguments.get('includeTopics', True),
                user_id=user_id, is_admin=is_admin)
        elif tool_name == 'list_meetings':
            result = list_meetings.execute(
                start_date=arguments.get('startDate'), end_date=arguments.get('endDate'),
                participant=arguments.get('participant'), status=arguments.get('status', 'ALL'),
                limit=arguments.get('limit', 20), user_id=user_id, is_admin=is_admin)
        elif tool_name == 'schedule_meeting':
            result = schedule_meeting.execute(
                meeting_name=arguments.get('meetingName'),
                meeting_platform=arguments.get('meetingPlatform'),
                meeting_id=arguments.get('meetingId'),
                scheduled_time=arguments.get('scheduledTime'),
                meeting_password=arguments.get('meetingPassword'),
                user_id=user_id, is_admin=is_admin)
        elif tool_name == 'start_meeting_now':
            result = start_meeting_now.execute(
                meeting_name=arguments.get('meetingName'),
                meeting_platform=arguments.get('meetingPlatform'),
                meeting_id=arguments.get('meetingId'),
                meeting_password=arguments.get('meetingPassword'),
                user_id=user_id, is_admin=is_admin)
        else:
            return jsonrpc_error(msg_id, -32602, f"Unknown tool: {tool_name}")

        return jsonrpc_response(msg_id, {
            "content": [{"type": "text", "text": json.dumps(result, indent=2, default=str)}],
        })
    except Exception as e:
        logger.error(f"Tool call error: {e}", exc_info=True)
        return jsonrpc_response(msg_id, {
            "content": [{"type": "text", "text": f"Error: {str(e)}"}],
            "isError": True,
        })


def jsonrpc_response(msg_id, result):
    """Format a JSON-RPC 2.0 success response."""
    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}),
    }


def jsonrpc_error(msg_id, code, message):
    """Format a JSON-RPC 2.0 error response."""
    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}),
    }


def success_response(result: Any) -> Dict[str, Any]:
    """Format successful MCP response"""
    return {
        'statusCode': 200,
        'headers': {
            'Content-Type': 'application/json'
        },
        'body': json.dumps({
            'content': [
                {
                    'type': 'text',
                    'text': json.dumps(result, indent=2, default=str)
                }
            ]
        })
    }


def error_response(status_code: int, message: str) -> Dict[str, Any]:
    """Format error MCP response"""
    return {
        'statusCode': status_code,
        'headers': {
            'Content-Type': 'application/json'
        },
        'body': json.dumps({
            'error': {
                'message': message,
                'code': status_code
            }
        })
    }