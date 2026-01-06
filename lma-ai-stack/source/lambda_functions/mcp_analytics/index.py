#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
MCP Analytics Lambda Function
Implements MCP protocol tools for LMA meeting data access
Provides 4 core tools: search, get transcript, get summary, list meetings
"""

import json
import logging
import os
from typing import Dict, Any

# Import tool implementations
from tools import search_meetings, get_transcript, get_summary, list_meetings

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
        # Extract user context from JWT claims (provided by AgentCore Gateway)
        # Try multiple possible event structures
        request_context = event.get('requestContext', {})
        authorizer = request_context.get('authorizer', {})
        claims = authorizer.get('claims', {})
        
        # Log what we're finding
        logger.info(f"requestContext keys: {list(request_context.keys())}")
        logger.info(f"authorizer keys: {list(authorizer.keys())}")
        logger.info(f"claims: {claims}")
        
        # Get user ID from JWT sub claim
        user_id = claims.get('sub')
        username = claims.get('cognito:username', claims.get('email', user_id))
        
        # Check if user is admin (member of Admin group)
        groups = claims.get('cognito:groups', '')
        is_admin = 'Admin' in groups if isinstance(groups, str) else 'Admin' in groups
        
        logger.info(f"User: {username}, ID: {user_id}, Admin: {is_admin}")
        
        # AgentCore Gateway passes only the input parameters (not tool name)
        # Infer tool from input parameters
        tool_input = event
        
        # Determine which tool based on input parameters
        if 'query' in tool_input and 'maxResults' in tool_input:
            tool_name = 'search_lma_meetings'
        elif 'meetingId' in tool_input and 'format' in tool_input:
            tool_name = 'get_meeting_transcript'
        elif 'meetingId' in tool_input and ('includeActionItems' in tool_input or 'includeTopics' in tool_input):
            tool_name = 'get_meeting_summary'
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