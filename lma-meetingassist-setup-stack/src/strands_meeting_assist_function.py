#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Strands-based Meeting Assistant Lambda Function
Provides a lightweight alternative to QnABot using AWS Strands SDK
Supports dynamic MCP server loading from Public Registry
"""

import json
import os
import boto3
from boto3.dynamodb.conditions import Key, Attr
from typing import Dict, Any, Optional
import logging
from datetime import datetime

# Import MCP server loader
try:
    from mcp_server_loader import load_account_mcp_servers
    MCP_LOADER_AVAILABLE = True
except ImportError as e:
    logging.warning(f"MCP server loader not available: {e}")
    MCP_LOADER_AVAILABLE = False

# Import thinking tool wrapper
try:
    from thinking_tool_wrapper import wrap_tool_with_thinking, get_next_sequence
    THINKING_WRAPPER_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Thinking tool wrapper not available: {e}")
    THINKING_WRAPPER_AVAILABLE = False

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')
bedrock_agent_runtime = boto3.client('bedrock-agent-runtime')

# Get AppSync endpoint from environment
APPSYNC_GRAPHQL_URL = os.environ.get('APPSYNC_GRAPHQL_URL', '')
EVENT_API_HTTP_URL = os.environ.get('EVENT_API_HTTP_URL', '')
ENABLE_STREAMING = os.environ.get('ENABLE_STREAMING', 'false').lower() == 'true'


class ThinkingStepHookProvider:
    """Hook provider to track tool usage and send thinking steps to AppSync"""
    
    def __init__(self, call_id: str, message_id: str):
        self.call_id = call_id
        self.message_id = message_id
        self.sequence_counter = 100  # Start at 100 to avoid conflicts with manual sequences
    
    def register_hooks(self, registry):
        """Register hook callbacks for tool lifecycle events"""
        from strands.hooks.events import BeforeToolCallEvent, AfterToolCallEvent
        
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.on_after_tool_call)
    
    def on_before_tool_call(self, event):
        """Called before a tool is executed"""
        try:
            logger.info(f"🔧 Hook: on_before_tool_call FIRED!")
            
            if not APPSYNC_GRAPHQL_URL:
                logger.warning("   APPSYNC_GRAPHQL_URL not configured, skipping thinking step")
                return
            
            # Get tool name - MCPAgentTool uses tool_name property, regular tools use name
            if event.selected_tool:
                tool_name = getattr(event.selected_tool, 'tool_name', None) or getattr(event.selected_tool, 'name', None) or 'unknown'
            else:
                tool_name = 'unknown'
            
            # Get tool input
            tool_input = {}
            if hasattr(event.tool_use, 'input'):
                tool_input = event.tool_use.input
            elif hasattr(event.tool_use, 'arguments'):
                tool_input = event.tool_use.arguments
            
            logger.info(f"   Tool name: {tool_name}")
            logger.info(f"   Tool input: {tool_input}")
            
            thinking_step = {
                'type': 'tool_use',
                'tool_name': tool_name,
                'tool_input': tool_input,
                'timestamp': datetime.utcnow().isoformat()
            }
            
            logger.info(f"🔧 Hook: Sending thinking step for tool - {tool_name}")
            send_thinking_step_to_appsync(self.call_id, self.message_id, thinking_step, self.sequence_counter)
            self.sequence_counter += 1
            logger.info(f"✓ Hook: Thinking step sent successfully")
            
        except Exception as e:
            logger.error(f"❌ Error in on_before_tool_call hook: {str(e)}")
            import traceback
            logger.error(f"   Traceback: {traceback.format_exc()}")
    
    def on_after_tool_call(self, event):
        """Called after a tool is executed"""
        try:
            logger.info(f"✅ Hook: on_after_tool_call FIRED!")
            
            if not APPSYNC_GRAPHQL_URL:
                logger.warning("   APPSYNC_GRAPHQL_URL not configured, skipping thinking step")
                return
            
            # Get tool name - MCPAgentTool uses tool_name property, regular tools use name
            if event.selected_tool:
                tool_name = getattr(event.selected_tool, 'tool_name', None) or getattr(event.selected_tool, 'name', None) or 'unknown'
            else:
                tool_name = 'unknown'
            
            # Check if there was an exception
            is_error = getattr(event.result, 'is_error', False)
            
            # Get result content - try multiple possible attributes
            result_content = ''
            if hasattr(event.result, 'content'):
                result_content = str(event.result.content)[:500]
            elif hasattr(event.result, 'output'):
                result_content = str(event.result.output)[:500]
            elif hasattr(event.result, 'text'):
                result_content = str(event.result.text)[:500]
            else:
                result_content = str(event.result)[:500]
            
            logger.info(f"   Tool name: {tool_name}")
            logger.info(f"   Success: {not is_error}")
            logger.info(f"   Result (truncated): {result_content[:100]}")
            
            thinking_step = {
                'type': 'tool_result',
                'tool_name': tool_name,
                'result': result_content,
                'success': not is_error,
                'timestamp': datetime.utcnow().isoformat()
            }
            
            logger.info(f"✅ Hook: Sending result thinking step for tool - {tool_name}")
            send_thinking_step_to_appsync(self.call_id, self.message_id, thinking_step, self.sequence_counter)
            self.sequence_counter += 1
            logger.info(f"✓ Hook: Result thinking step sent successfully")
            
        except Exception as e:
            logger.error(f"❌ Error in on_after_tool_call hook: {str(e)}")
            import traceback
            logger.error(f"   Traceback: {traceback.format_exc()}")


def create_document_search_tool(kb_id: str, kb_region: str, kb_account_id: str, model_id: str,
                                call_id: str = None, message_id: str = None):
    """Factory function to create document search tool with closure"""
    from strands import tool
    
    @tool
    def document_search(query: str) -> str:
        """Search company knowledge base for internal documents, policies, procedures, or reference materials.
        
        Use this when:
        - User asks about company-specific information
        - Need to reference internal documentation
        - Questions about products, services, policies
        
        Args:
            query: The search query for company documents
            
        Returns:
            Relevant information from company knowledge base
        """
        if not kb_id:
            return "Document retrieval not configured - no knowledge base available"
        
        try:
            # Send tool use thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_use',
                    'tool_name': 'document_search',
                    'tool_input': {'query': query},
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 0)
            
            logger.info(f"Document search tool executing: {query}")
            
            # Determine model ARN based on model type
            if model_id.startswith("anthropic"):
                model_arn = f"arn:aws:bedrock:{kb_region}::foundation-model/{model_id}"
            else:
                model_arn = f"arn:aws:bedrock:{kb_region}:{kb_account_id}:inference-profile/{model_id}"
            
            # Query knowledge base
            kb_input = {
                "input": {'text': query},
                "retrieveAndGenerateConfiguration": {
                    'knowledgeBaseConfiguration': {
                        'knowledgeBaseId': kb_id,
                        'modelArn': model_arn
                    },
                    'type': 'KNOWLEDGE_BASE'
                }
            }
            
            response = bedrock_agent_runtime.retrieve_and_generate(**kb_input)
            result = response.get("output", {}).get("text", "No results found in knowledge base")
            
            # Send tool result thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'document_search',
                    'result': result[:200],
                    'success': True,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            logger.info(f"Document search result: {result[:100]}...")
            return result
            
        except Exception as e:
            logger.error(f"Error in document search: {str(e)}")
            return f"Error retrieving documents: {str(e)}"
    
    return document_search


def create_recent_meetings_tool(dynamodb_table_name: str, user_email: str, call_id: str = None, message_id: str = None):
    """Factory function to create recent meetings list tool with closure"""
    from strands import tool
    from datetime import datetime, timedelta
    
    @tool
    def recent_meetings_list(limit: int = 10) -> str:
        """Get a chronological list of recent meetings sorted by date (most recent first).
        
        Use this when:
        - User asks about "last meeting", "most recent meeting", or "latest meeting"
        - Need to establish chronological context before semantic search
        - Want to list recent meetings in time order
        - Comparing current meeting to previous meetings
        
        Args:
            limit: Number of recent meetings to return (default 10, max 50)
            
        Returns:
            JSON list of recent meetings with CallId, date, and duration
        """
        if not dynamodb_table_name:
            return "Recent meetings list not configured - no DynamoDB table available"
        
        if not user_email:
            return "Recent meetings list requires user authentication"
        
        try:
            # Send tool use thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_use',
                    'tool_name': 'recent_meetings_list',
                    'tool_input': {'limit': limit},
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 0)
            
            # Limit to reasonable range
            limit = min(max(1, limit), 50)
            
            logger.info(f"Recent meetings tool executing: limit={limit} for user: {user_email}")
            
            table = dynamodb.Table(dynamodb_table_name)
            
            # Query recent date shards (last 30 days) to find meetings
            # DynamoDB structure: PK = cls#YYYY-MM-DD#s#NN, SK = ts#timestamp#callid
            meetings = []
            shards_per_day = 6  # 4-hour shards
            
            # Start from today and go backwards
            current_date = datetime.utcnow()
            
            for days_back in range(30):  # Look back 30 days
                query_date = current_date - timedelta(days=days_back)
                date_str = query_date.strftime('%Y-%m-%d')
                
                # Query each shard for this date (in reverse order for efficiency)
                for shard in range(shards_per_day - 1, -1, -1):
                    pk = f"cls#{date_str}#s#{shard:02d}"
                    
                    try:
                        response = table.query(
                            KeyConditionExpression=Key('PK').eq(pk),
                            FilterExpression=Attr('Owner').eq(user_email),
                            ScanIndexForward=False  # Sort descending (most recent first)
                        )
                        
                        for item in response.get('Items', []):
                            meetings.append({
                                'CallId': item.get('CallId', 'Unknown'),
                                'Date': item.get('CreatedAt', 'Unknown'),
                                'Duration': f"{int(item.get('TotalConversationDurationMillis', 0) / 1000)}s",
                                'SK': item.get('SK', '')  # For sorting
                            })
                            
                            # Stop if we have enough
                            if len(meetings) >= limit:
                                break
                                
                    except Exception as e:
                        logger.warning(f"Error querying shard {pk}: {e}")
                        continue
                    
                    if len(meetings) >= limit:
                        break
                
                if len(meetings) >= limit:
                    break
            
            # Sort all collected meetings by SK (timestamp) descending
            meetings.sort(key=lambda x: x.get('SK', ''), reverse=True)
            meetings = meetings[:limit]
            
            # Remove SK from output (internal use only)
            for m in meetings:
                m.pop('SK', None)
            
            if not meetings:
                result = "No recent meetings found"
            else:
                result = json.dumps(meetings, indent=2)
                logger.info(f"Recent meetings returned {len(meetings)} meetings")
            
            # Send tool result thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'recent_meetings_list',
                    'result': result[:200],
                    'success': True,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return result
            
        except Exception as e:
            logger.error(f"Error in recent meetings list: {str(e)}")
            error_msg = f"Error retrieving recent meetings: {str(e)}"
            
            # Send error thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'recent_meetings_list',
                    'result': error_msg,
                    'success': False,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return error_msg
    
    return recent_meetings_list


def create_meeting_history_tool(transcript_kb_id: str, kb_region: str, kb_account_id: str,
                                model_id: str, user_email: str, call_id: str = None, message_id: str = None):
    """Factory function to create meeting history tool with closure"""
    from strands import tool
    
    @tool
    def meeting_history(query: str, call_ids: str = "") -> str:
        """Search past meeting transcripts and summaries with semantic search and user-based access control.
        
        Use this when:
        - User asks about meetings on specific topics (semantic search)
        - Need detailed content from past meetings
        - Following up on recent_meetings_list with specific CallIds
        - Searching for action items, decisions, or discussions
        
        Args:
            query: The search query for past meetings
            call_ids: Optional comma-separated CallIds to limit search to specific meetings (use after recent_meetings_list)
            
        Returns:
            Detailed information from past meetings the user has access to
        """
        if not transcript_kb_id:
            return "Meeting history not configured - no transcript knowledge base available"
        
        if not user_email:
            return "Meeting history requires user authentication"
        
        try:
            # Send tool use thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_use',
                    'tool_name': 'meeting_history',
                    'tool_input': {'query': query, 'call_ids': call_ids},
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 0)
            
            logger.info(f"Meeting history tool executing: {query} for user: {user_email}")
            logger.info(f"🔍 DEBUG: KB Configuration:")
            logger.info(f"   transcript_kb_id: {transcript_kb_id}")
            logger.info(f"   kb_region: {kb_region}")
            logger.info(f"   model_id: {model_id}")
            logger.info(f"   user_email (filter): {user_email}")
            
            # Determine model ARN based on model type
            if model_id.startswith("anthropic"):
                model_arn = f"arn:aws:bedrock:{kb_region}::foundation-model/{model_id}"
            else:
                model_arn = f"arn:aws:bedrock:{kb_region}:{kb_account_id}:inference-profile/{model_id}"
            
            logger.info(f"   model_arn: {model_arn}")
            
            # Query with user-based access control filter
            kb_input = {
                "input": {'text': query},
                "retrieveAndGenerateConfiguration": {
                    'knowledgeBaseConfiguration': {
                        'knowledgeBaseId': transcript_kb_id,
                        'modelArn': model_arn,
                        "retrievalConfiguration": {
                            "vectorSearchConfiguration": {
                                "filter": {
                                    "equals": {
                                        "key": "Owner",
                                        "value": user_email
                                    }
                                }
                            }
                        }
                    },
                    'type': 'KNOWLEDGE_BASE'
                }
            }
            
            logger.info(f"🔍 DEBUG: KB Input: {json.dumps(kb_input, indent=2)}")
            
            response = bedrock_agent_runtime.retrieve_and_generate(**kb_input)
            
            logger.info(f"🔍 DEBUG: KB Response (full): {json.dumps(response, indent=2, default=str)}")
            
            result = response.get("output", {}).get("text", "No past meetings found matching your query")
            
            # Check if this is a guardrail response
            if "Sorry, I am unable to assist you with this request" in result:
                logger.error(f"❌ GUARDRAIL TRIGGERED! Response: {result}")
                logger.error(f"   This indicates the Knowledge Base has guardrails blocking the query")
                logger.error(f"   Query: {query}")
                logger.error(f"   User email filter: {user_email}")
            
            # Send tool result thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'meeting_history',
                    'result': result[:200],
                    'success': True,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            logger.info(f"Meeting history result: {result[:100]}...")
            return result
            
        except Exception as e:
            logger.error(f"Error in meeting history search: {str(e)}")
            error_msg = f"Error retrieving meeting history: {str(e)}"
            
            # Send error thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'meeting_history',
                    'result': error_msg,
                    'success': False,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return error_msg
    
    return meeting_history


def create_current_meeting_transcript_tool(call_id: str, dynamodb_table_name: str, message_id: str = None):
    """Factory function to create current meeting transcript tool with closure"""
    from strands import tool
    
    @tool
    def current_meeting_transcript(lines: int = 20, mode: str = "recent") -> str:
        """Get transcript from the current meeting.
        
        Use this when:
        - User asks about what was said in THIS meeting
        - Need to reference recent statements or discussions
        - Summarizing, extracting action items, or analyzing current meeting
        - User asks about topics, decisions, or questions from current meeting
        - Request is ambiguous but likely meeting-related
        
        DO NOT use for:
        - General knowledge questions unrelated to the meeting
        - Questions about past meetings (use meeting_history instead)
        - Web searches or current events (use web_search instead)
        
        Args:
            lines: Number of recent transcript lines to return (default 20, max 100)
            mode: "recent" for last N lines, "full" for complete transcript, "semantic" for relevant excerpts
            
        Returns:
            Meeting transcript text formatted with speaker names
        """
        if not dynamodb_table_name:
            return "Current meeting transcript not available - no DynamoDB table configured"
        
        try:
            # Send tool use thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_use',
                    'tool_name': 'current_meeting_transcript',
                    'tool_input': {'lines': lines, 'mode': mode},
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 0)
            
            # Limit to reasonable range
            lines = min(max(1, lines), 100)
            
            logger.info(f"Current meeting transcript tool executing: lines={lines}, mode={mode}")
            
            # Fetch full transcript
            full_transcript = fetch_meeting_transcript(call_id, dynamodb_table_name)
            
            if not full_transcript:
                result = "No transcript available for current meeting yet."
            elif mode == "full":
                result = full_transcript
            elif mode == "recent":
                # Return last N lines
                transcript_lines = full_transcript.split('\n')
                recent_lines = transcript_lines[-lines:] if len(transcript_lines) > lines else transcript_lines
                result = '\n'.join(recent_lines)
                logger.info(f"Returning {len(recent_lines)} recent transcript lines")
            else:
                # Default to recent
                transcript_lines = full_transcript.split('\n')
                recent_lines = transcript_lines[-lines:] if len(transcript_lines) > lines else transcript_lines
                result = '\n'.join(recent_lines)
            
            # Send tool result thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'current_meeting_transcript',
                    'result': result[:200],
                    'success': True,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return result
            
        except Exception as e:
            logger.error(f"Error in current meeting transcript: {str(e)}")
            error_msg = f"Error retrieving current meeting transcript: {str(e)}"
            
            # Send error thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'current_meeting_transcript',
                    'result': error_msg,
                    'success': False,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return error_msg
    
    return current_meeting_transcript


def create_vnc_preview_control_tool(call_id: str, appsync_url: str, message_id: str = None):
    """Factory function to create VNC preview control tool"""
    from strands import tool
    from gql import gql
    from asst_gql_client import AppsyncRequestsGqlClient
    
    @tool
    def control_vnc_preview(action: str) -> str:
        """Control the VNC live view preview window on the meeting page.
        
        This tool allows you to show or hide the Virtual Participant's browser
        screen directly on the meeting page. The user can watch in real-time
        what the VP is doing.
        
        Use this when:
        - User asks to "show me what the bot is doing"
        - User wants to "see the virtual participant screen"
        - User asks to "open the live view" or "show live preview"
        - User wants to "close the preview" or "hide the screen"
        - User wants to monitor VP browser activity
        
        Args:
            action: Either "open" to show preview or "close" to hide it
            
        Returns:
            Success message confirming the action
            
        Examples:
            - User: "show me the virtual participant"
            - User: "open the live view"
            - User: "close the preview"
        """
        if action not in ['open', 'close']:
            return "Invalid action. Please use 'open' to show the preview or 'close' to hide it."
        
        try:
            # Send tool use thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_use',
                    'tool_name': 'control_vnc_preview',
                    'tool_input': {'action': action},
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 0)
            
            logger.info(f"VNC preview control: {action} for call {call_id}")
            
            # Initialize AppSync client
            appsync_client = AppsyncRequestsGqlClient(
                url=appsync_url,
                fetch_schema_from_transport=False
            )
            
            # GraphQL mutation
            mutation = gql("""
            mutation ToggleVNCPreview($input: ToggleVNCPreviewInput!) {
                toggleVNCPreview(input: $input) {
                    CallId
                    Action
                    Success
                    Timestamp
                }
            }
            """)
            
            variables = {
                'input': {
                    'CallId': call_id,
                    'Show': action == 'open'
                }
            }
            
            result = appsync_client.execute(mutation, variable_values=variables)
            
            if result.get('toggleVNCPreview', {}).get('Success'):
                action_past = "opened" if action == "open" else "closed"
                success_msg = f"✓ VNC live preview {action_past} successfully. The user can now {'see' if action == 'open' else 'no longer see'} the Virtual Participant's browser screen on the meeting page."
                
                # Send tool result thinking step
                if call_id and message_id and APPSYNC_GRAPHQL_URL:
                    from datetime import datetime
                    thinking_step = {
                        'type': 'tool_result',
                        'tool_name': 'control_vnc_preview',
                        'result': success_msg,
                        'success': True,
                        'timestamp': datetime.utcnow().isoformat()
                    }
                    send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
                
                return success_msg
            else:
                error_msg = f"Failed to {action} VNC preview. Please try again."
                
                # Send error thinking step
                if call_id and message_id and APPSYNC_GRAPHQL_URL:
                    from datetime import datetime
                    thinking_step = {
                        'type': 'tool_result',
                        'tool_name': 'control_vnc_preview',
                        'result': error_msg,
                        'success': False,
                        'timestamp': datetime.utcnow().isoformat()
                    }
                    send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
                
                return error_msg
                
        except Exception as e:
            logger.error(f"Error controlling VNC preview: {str(e)}")
            error_msg = f"Error controlling VNC preview: {str(e)}"
            
            # Send error thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'control_vnc_preview',
                    'result': error_msg,
                    'success': False,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return error_msg
    
    return control_vnc_preview


def create_vp_browser_control_tool(call_id: str, event_api_http_url: str, message_id: str = None):
    """Factory function to create VP browser control tool using Event API HTTP endpoint"""
    from strands import tool
    import uuid
    from datetime import datetime
    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    import hashlib
    
    @tool
    def control_vp_browser(action: str, url: str = "") -> str:
        """Control the Virtual Participant's browser to perform web searches and research during meetings.
        
        🔧 DEBUG: Tool entry point - parameters will be logged
        
        This tool allows you to navigate to websites and take screenshots through the Virtual Participant.
        Perfect for visual research, fact-checking, and gathering information during meetings.
        
        **IMPORTANT:** Before using this tool, you MUST first use control_vnc_preview(action="open")
        to open the VNC live preview window so the user can see the browser activity.
        
        Use this when:
        - User asks to "search for X online" or "look up X"
        - User wants to "show me X website" or "open X"
        - Need to fact-check information with visual proof
        - User wants screenshots or visual evidence
        - Researching products, competitors, or references
        
        Available actions:
        - "open_url": Open a URL in a new foreground tab (requires url parameter)
        - "screenshot": Take a screenshot of current page
        
        Args:
            action: The browser action to perform
            url: URL to open (for open_url action)
            
        Returns:
            Result of the browser action
            
        Examples:
            - User: "search for AWS pricing"
              → control_vp_browser(action="open_url", url="https://aws.amazon.com/pricing")
            
            - User: "show me the competitor's website"
              → control_vp_browser(action="open_url", url="https://competitor.com")
            
            - User: "take a screenshot"
              → control_vp_browser(action="screenshot")
        """
        # Send tool use thinking step
        if call_id and message_id and APPSYNC_GRAPHQL_URL:
            from datetime import datetime
            thinking_step = {
                'type': 'tool_use',
                'tool_name': 'control_vp_browser',
                'tool_input': {'action': action, 'url': url},
                'timestamp': datetime.utcnow().isoformat()
            }
            send_thinking_step_to_appsync(call_id, message_id, thinking_step, 0)
        
        logger.info("=" * 80)
        logger.info("🔧 VP BROWSER CONTROL TOOL CALLED")
        logger.info(f"  Action: {action}")
        logger.info(f"  URL: {url}")
        logger.info(f"  CallId: {call_id}")
        logger.info(f"  Event API HTTP URL configured: {bool(event_api_http_url)}")
        logger.info(f"  Event API HTTP URL value: {event_api_http_url}")
        logger.info("=" * 80)
        
        valid_actions = ['open_url', 'screenshot']
        
        if action not in valid_actions:
            logger.error(f"❌ Invalid action: {action}")
            return f"Invalid action. Please use one of: {', '.join(valid_actions)}"
        
        # Validate required parameters
        if action == 'open_url' and not url:
            logger.error("❌ Missing URL for open_url action")
            return "URL parameter is required for open_url action"
        
        if not event_api_http_url:
            logger.error("❌ Event API HTTP URL not configured!")
            logger.error(f"   EVENT_API_HTTP_URL env var: {os.environ.get('EVENT_API_HTTP_URL', 'NOT SET')}")
            return "Event API not configured - VP browser control unavailable"
        
        logger.info("✓ All validations passed, proceeding with Event API publish")
        
        try:
            logger.info(f"🚀 Starting VP browser control: {action} for call {call_id}")
            
            # For open_url, we need to use new_page with url parameter
            # According to chrome-devtools-mcp docs, new_page can accept a url parameter
            # which will create a new page AND navigate to that URL
            
            if action == 'open_url':
                command_id = str(uuid.uuid4())
                timestamp = datetime.utcnow().isoformat() + 'Z'
                
                # Use new_page with url parameter to create new page and navigate
                event_payload = {
                    'commandId': command_id,
                    'CallId': call_id,
                    'toolName': 'new_page',
                    'arguments': json.dumps({
                        'url': url  # new_page accepts url parameter to navigate immediately
                    }),
                    'requestedBy': 'strands-agent',
                    'timestamp': timestamp,
                    'status': 'pending'
                }
                
                call_id_hash = hashlib.sha256(call_id.encode()).hexdigest()[:16]
                channel_name = f'/mcp-commands/{call_id_hash}'
                
                logger.info(f"Publishing MCP new_page command with URL to Event API")
                logger.info(f"  Original CallId: {call_id}")
                logger.info(f"  Channel hash: {call_id_hash}")
                logger.info(f"  Channel: {channel_name}")
                logger.info(f"  Event API HTTP URL: {event_api_http_url}")
                logger.info(f"  Command (new_page with url): {command_id} -> {url}")
                
                body = json.dumps({
                    'channel': channel_name,
                    'events': [json.dumps(event_payload)]
                })
                
                logger.info(f"  Request body: {body}")
                
                # Create AWS request for SigV4 signing
                request = AWSRequest(
                    method='POST',
                    url=f"{event_api_http_url}/event",
                    data=body,
                    headers={
                        'Content-Type': 'application/json',
                        'accept': 'application/json, text/javascript',
                        'content-encoding': 'amz-1.0'
                    }
                )
                
                # Sign request with SigV4
                credentials = boto3.Session().get_credentials()
                SigV4Auth(credentials, 'appsync', os.environ.get('AWS_REGION', 'us-east-1')).add_auth(request)
                
                logger.info(f"  Sending HTTP POST to: {event_api_http_url}/event")
                
                # Send HTTP request
                response = requests.post(
                    f"{event_api_http_url}/event",
                    data=body,
                    headers=dict(request.headers)
                )
                
                logger.info(f"📥 Response received:")
                logger.info(f"  Status: {response.status_code}")
                logger.info(f"  Headers: {dict(response.headers)}")
                logger.info(f"  Body: {response.text}")
                
                if response.status_code != 200:
                    logger.error(f"❌ Event API returned non-200 status: {response.status_code}")
                    logger.error(f"   Response: {response.text}")
                
                response.raise_for_status()
                
                logger.info("=" * 80)
                logger.info("✅ NEW_PAGE COMMAND PUBLISHED SUCCESSFULLY")
                logger.info(f"✅ Creating new page with URL: {url}")
                logger.info(f"✅ Command: {command_id}")
                logger.info("=" * 80)
                
                success_msg = f"✓ Opening {url} in new tab in Virtual Participant browser"
                
                # Send tool result thinking step
                if call_id and message_id and APPSYNC_GRAPHQL_URL:
                    from datetime import datetime
                    thinking_step = {
                        'type': 'tool_result',
                        'tool_name': 'control_vp_browser',
                        'result': success_msg,
                        'success': True,
                        'timestamp': datetime.utcnow().isoformat()
                    }
                    send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
                
                return success_msg
                
            elif action == 'screenshot':
                tool_name = 'take_screenshot'
                arguments = {'format': 'png', 'quality': 90}
            else:
                return f"Action {action} not yet implemented"
            
            # For screenshot, send single command
            command_id = str(uuid.uuid4())
            timestamp = datetime.utcnow().isoformat() + 'Z'
            
            event_payload = {
                'commandId': command_id,
                'CallId': call_id,
                'toolName': tool_name,
                'arguments': json.dumps(arguments),
                'requestedBy': 'strands-agent',
                'timestamp': timestamp,
                'status': 'pending'
            }
            
            # Publish to Event API channel via HTTP
            # Use SHA256 hash of CallId (first 16 chars) for channel name
            call_id_hash = hashlib.sha256(call_id.encode()).hexdigest()[:16]
            channel_name = f'/mcp-commands/{call_id_hash}'
            
            logger.info(f"Publishing MCP command to Event API")
            logger.info(f"  Original CallId: {call_id}")
            logger.info(f"  Channel hash: {call_id_hash}")
            logger.info(f"  Channel: {channel_name}")
            logger.info(f"  Event API HTTP URL: {event_api_http_url}")
            logger.info(f"  Event payload: {json.dumps(event_payload)}")
            
            # Prepare HTTP request body
            body = json.dumps({
                'channel': channel_name,
                'events': [json.dumps(event_payload)]
            })
            
            logger.info(f"  Request body: {body}")
            
            # Create AWS request for SigV4 signing
            request = AWSRequest(
                method='POST',
                url=f"{event_api_http_url}/event",
                data=body,
                headers={
                    'Content-Type': 'application/json',
                    'accept': 'application/json, text/javascript',
                    'content-encoding': 'amz-1.0'
                }
            )
            
            # Sign request with SigV4
            credentials = boto3.Session().get_credentials()
            SigV4Auth(credentials, 'appsync', os.environ.get('AWS_REGION', 'us-east-1')).add_auth(request)
            
            logger.info(f"  Sending HTTP POST to: {event_api_http_url}/event")
            
            # Send HTTP request
            response = requests.post(
                f"{event_api_http_url}/event",
                data=body,
                headers=dict(request.headers)
            )
            
            logger.info(f"📥 Response received:")
            logger.info(f"  Status: {response.status_code}")
            logger.info(f"  Headers: {dict(response.headers)}")
            logger.info(f"  Body: {response.text}")
            
            if response.status_code != 200:
                logger.error(f"❌ Event API returned non-200 status: {response.status_code}")
                logger.error(f"   Response: {response.text}")
            
            response.raise_for_status()
            
            logger.info("=" * 80)
            logger.info("✅ EVENT PUBLISHED SUCCESSFULLY TO EVENT API")
            logger.info(f"✅ MCP command sent: {command_id}")
            logger.info(f"✅ Channel: {channel_name}")
            logger.info(f"✅ Tool: {tool_name}")
            logger.info("=" * 80)
            
            if action == 'screenshot':
                success_msg = f"✓ Screenshot captured. Command ID: {command_id}"
            else:
                success_msg = f"✓ Browser action '{action}' completed. Command ID: {command_id}"
            
            # Send tool result thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'control_vp_browser',
                    'result': success_msg,
                    'success': True,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return success_msg
                
        except Exception as e:
            logger.error("=" * 80)
            logger.error("❌ EXCEPTION IN VP BROWSER CONTROL")
            logger.error(f"   Exception type: {type(e).__name__}")
            logger.error(f"   Exception message: {str(e)}")
            logger.error(f"   Action attempted: {action}")
            logger.error(f"   CallId: {call_id}")
            logger.error("=" * 80)
            import traceback
            logger.error(f"   Full traceback:\n{traceback.format_exc()}")
            error_msg = f"Error controlling Virtual Participant browser: {str(e)}"
            
            # Send error thinking step
            if call_id and message_id and APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'tool_result',
                    'tool_name': 'control_vp_browser',
                    'result': error_msg,
                    'success': False,
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            
            return error_msg
    
    return control_vp_browser


def send_chat_token_to_appsync(call_id: str, message_id: str, token: str, is_complete: bool, sequence: int):
    """
    Send a chat token to AppSync for real-time streaming
    """
    try:
        if not APPSYNC_GRAPHQL_URL:
            logger.warning("APPSYNC_GRAPHQL_URL not configured, skipping token streaming")
            return
        
        from asst_gql_client import AppsyncRequestsGqlClient
        from datetime import datetime
        from gql import gql
        
        # Initialize AppSync client
        appsync_client = AppsyncRequestsGqlClient(
            url=APPSYNC_GRAPHQL_URL,
            fetch_schema_from_transport=False
        )
        
        # GraphQL mutation - parse it into an AST
        mutation = gql("""
        mutation AddChatToken($input: AddChatTokenInput!) {
            addChatToken(input: $input) {
                CallId
                MessageId
                Token
                IsComplete
                Sequence
                Timestamp
                ThinkingStep {
                    Type
                    Content
                    ToolName
                    ToolInput
                    ToolResult
                    Success
                }
            }
        }
        """)
        
        variables = {
            'input': {
                'CallId': call_id,
                'MessageId': message_id,
                'Token': token,
                'IsComplete': is_complete,
                'Sequence': sequence
            }
        }
        
        # Execute mutation
        result = appsync_client.execute(mutation, variable_values=variables)
        logger.debug(f"Sent token {sequence} to AppSync: {token[:50]}...")
        
    except Exception as e:
        logger.error(f"Error sending token to AppSync: {str(e)}")


def send_thinking_step_to_appsync(call_id: str, message_id: str, thinking_step: Dict[str, Any], sequence: int):
    """
    Send a thinking step via the existing addChatToken mutation (piggyback on token stream)
    
    This reuses the existing subscription infrastructure instead of creating a separate one.
    The thinking step is embedded in the ChatToken's ThinkingStep field.
    
    Args:
        call_id: The call/meeting ID
        message_id: The message ID for this conversation
        thinking_step: Dict containing thinking step data
        sequence: Sequence number for ordering
    """
    try:
        if not APPSYNC_GRAPHQL_URL:
            logger.warning("APPSYNC_GRAPHQL_URL not configured, skipping thinking step streaming")
            return
        
        from asst_gql_client import AppsyncRequestsGqlClient
        from datetime import datetime
        from gql import gql
        
        # Initialize AppSync client
        appsync_client = AppsyncRequestsGqlClient(
            url=APPSYNC_GRAPHQL_URL,
            fetch_schema_from_transport=False
        )
        
        # Use addChatToken mutation but with empty token and thinking step data
        mutation = gql("""
        mutation AddChatToken($input: AddChatTokenInput!) {
            addChatToken(input: $input) {
                CallId
                MessageId
                Token
                IsComplete
                Sequence
                Timestamp
                ThinkingStep {
                    Type
                    Content
                    ToolName
                    ToolInput
                    ToolResult
                    Success
                }
            }
        }
        """)
        
        # Create thinking step data
        thinking_step_data = {
            'Type': thinking_step.get('type', 'unknown'),
            'Content': thinking_step.get('content'),
            'ToolName': thinking_step.get('tool_name'),
            'ToolInput': json.dumps(thinking_step.get('tool_input', {})) if thinking_step.get('tool_input') else None,
            'ToolResult': thinking_step.get('result'),
            'Success': thinking_step.get('success', True)
        }
        
        variables = {
            'input': {
                'CallId': call_id,
                'MessageId': message_id,
                'Token': '',  # Empty token for thinking steps
                'IsComplete': False,
                'Sequence': sequence,
                'ThinkingStep': thinking_step_data
            }
        }
        
        # Execute mutation
        result = appsync_client.execute(mutation, variable_values=variables)
        logger.debug(f"Sent thinking step {sequence} ({thinking_step.get('type')}) via ChatToken")
        
    except Exception as e:
        logger.error(f"Error sending thinking step to AppSync: {str(e)}")

def fetch_meeting_transcript(call_id: str, dynamodb_table_name: str) -> str:
    """
    Fetch the meeting transcript from DynamoDB
    """
    try:
        table = dynamodb.Table(dynamodb_table_name)
        pk = f'trs#{call_id}'
        
        # Query for transcript segments
        response = table.query(
            KeyConditionExpression=Key('PK').eq(pk),
            FilterExpression=(
                (Attr('Channel').eq('AGENT') | Attr('Channel').eq('CALLER') | Attr('Channel').eq('AGENT_ASSISTANT')) 
                & Attr('IsPartial').eq(False)
            )
        )
        
        # Sort by EndTime and format transcript
        items = sorted(response.get('Items', []), key=lambda x: x.get('EndTime', 0))
        
        transcript_parts = []
        for item in items:
            speaker = item.get('Speaker', 'Unknown')
            transcript = item.get('Transcript', '')
            channel = item.get('Channel', '')
            
            # Format based on channel
            if channel == 'AGENT_ASSISTANT':
                transcript_parts.append(f"MeetingAssistant: {transcript}")
            else:
                transcript_parts.append(f"{speaker}: {transcript}")
        
        full_transcript = '\n'.join(transcript_parts)
        logger.info(f"Fetched transcript for {call_id}: {len(full_transcript)} characters")
        
        return full_transcript
        
    except Exception as e:
        logger.error(f"Error fetching transcript: {str(e)}")
        return ""

def query_knowledge_base(user_input: str, call_id: str) -> str:
    """
    Query Bedrock Knowledge Base for relevant context
    """
    try:
        kb_id = os.environ.get('KB_ID')
        if not kb_id:
            return ""
        
        model_id = os.environ.get('MODEL_ID', 'global.anthropic.claude-haiku-4-5-20251001-v1:0')
        kb_region = os.environ.get('KB_REGION', os.environ.get('AWS_REGION'))
        kb_account_id = os.environ.get('KB_ACCOUNT_ID')
        
        # Determine model ARN based on model type
        if model_id.startswith("anthropic"):
            model_arn = f"arn:aws:bedrock:{kb_region}::foundation-model/{model_id}"
        else:
            model_arn = f"arn:aws:bedrock:{kb_region}:{kb_account_id}:inference-profile/{model_id}"
        
        # Query knowledge base
        kb_input = {
            "input": {
                'text': user_input
            },
            "retrieveAndGenerateConfiguration": {
                'knowledgeBaseConfiguration': {
                    'knowledgeBaseId': kb_id,
                    'modelArn': model_arn,
                    "retrievalConfiguration": {
                        "vectorSearchConfiguration": {
                            "filter": {
                                "equals": {
                                    "key": "CallId",
                                    "value": call_id
                                }
                            }
                        }
                    }
                },
                'type': 'KNOWLEDGE_BASE'
            }
        }
        
        logger.info(f"Querying KB with input: {kb_input}")
        
        response = bedrock_agent_runtime.retrieve_and_generate(**kb_input)
        
        # Extract response text
        kb_response = response.get("output", {}).get("text", "")
        
        logger.info(f"KB response: {kb_response}")
        
        return kb_response
        
    except Exception as e:
        logger.error(f"Error querying knowledge base: {str(e)}")
        return ""

def handler(event, context):
    """
    Lambda handler for Strands-based meeting assistance with tools
    
    Expected event structure:
    {
        "transcript": "meeting transcript context",
        "userInput": "user question or request",
        "callId": "unique call identifier",
        "userEmail": "user@example.com"
    }
    """
    try:
        logger.info(f"Strands Meeting Assist - Processing event: {json.dumps(event)}")
        
        # Extract parameters from event - handle both 'text' and 'userInput' for compatibility
        user_input = event.get('userInput', '') or event.get('text', '')
        call_id = event.get('callId', '') or event.get('call_id', '')
        conversation_history = event.get('conversation_history', [])
        dynamodb_table_name = event.get('dynamodb_table_name', '')
        user_email = event.get('userEmail', '') or event.get('user_email', '')
        
        if not user_input:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': 'userInput or text is required'
                })
            }
        
        # Get environment variables for tools
        kb_id = os.environ.get('KB_ID', '')
        transcript_kb_id = os.environ.get('TRANSCRIPT_KB_ID', '')
        kb_region = os.environ.get('KB_REGION', os.environ.get('AWS_REGION'))
        kb_account_id = os.environ.get('KB_ACCOUNT_ID', '')
        model_id = os.environ.get('MODEL_ID', 'global.anthropic.claude-haiku-4-5-20251001-v1:0')
        tavily_api_key = os.environ.get('TAVILY_API_KEY', '')
        
        # Fetch meeting transcript from DynamoDB
        transcript = fetch_meeting_transcript(call_id, dynamodb_table_name) if dynamodb_table_name else event.get('transcript', '')
        
        # Get message ID early for tool wrappers and status updates
        transcript_segment_args = event.get('transcript_segment_args', {})
        message_id = transcript_segment_args.get('MessageId') or transcript_segment_args.get('SegmentId', f"msg-{call_id}")
        
        # Initialize tools list (includes both regular tools and MCP clients)
        tools = []
        
        # Load installed MCP servers (account-level) - returns MCPClient objects
        if MCP_LOADER_AVAILABLE:
            try:
                # Send status update: Loading MCP servers
                if APPSYNC_GRAPHQL_URL:
                    from datetime import datetime
                    thinking_step = {
                        'type': 'status',
                        'content': 'Loading MCP servers...',
                        'timestamp': datetime.utcnow().isoformat()
                    }
                    send_thinking_step_to_appsync(call_id, message_id, thinking_step, 0)
                
                logger.info("Loading installed MCP servers...")
                mcp_clients = load_account_mcp_servers()
                tools.extend(mcp_clients)
                logger.info(f"Loaded {len(mcp_clients)} MCP clients from installed MCP servers")
                logger.info("Using Strands Managed Integration (experimental) - MCP clients will manage their own lifecycle")
                
                # Send status update: MCP servers loaded
                if APPSYNC_GRAPHQL_URL:
                    from datetime import datetime
                    thinking_step = {
                        'type': 'status',
                        'content': f'Loaded {len(mcp_clients)} MCP servers',
                        'timestamp': datetime.utcnow().isoformat()
                    }
                    send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
            except Exception as e:
                logger.warning(f"Failed to load MCP servers: {e}")
                # Send error status
                if APPSYNC_GRAPHQL_URL:
                    from datetime import datetime
                    thinking_step = {
                        'type': 'status',
                        'content': f'Failed to load MCP servers: {str(e)}',
                        'timestamp': datetime.utcnow().isoformat()
                    }
                    send_thinking_step_to_appsync(call_id, message_id, thinking_step, 1)
        
        # Add web search tool if API key provided (auto-enable)
        if tavily_api_key:
            try:
                from tavily import TavilyClient
                from strands import tool
                
                # Create Tavily client in closure
                tavily_client = TavilyClient(api_key=tavily_api_key)
                
                # Capture call_id and message_id in closure for thinking steps
                _call_id = call_id
                _message_id = message_id
                
                @tool
                def web_search(query: str) -> str:
                    """Search the web for current information, news, facts, or data.
                    
                    Use this when:
                    - User asks about current events, prices, or statistics
                    - Information is not in knowledge bases
                    - Need real-time or recent information
                    
                    Args:
                        query: The search query string
                        
                    Returns:
                        Search results with titles, content, and source URLs
                    """
                    try:
                        # Send tool_use thinking step
                        if THINKING_WRAPPER_AVAILABLE and APPSYNC_GRAPHQL_URL:
                            from datetime import datetime
                            sequence = get_next_sequence(_message_id)
                            thinking_step = {
                                'type': 'tool_use',
                                'tool_name': 'web_search',
                                'tool_input': {'query': query},
                                'timestamp': datetime.utcnow().isoformat()
                            }
                            logger.info(f"🔧 Sending tool_use thinking step: web_search")
                            send_thinking_step_to_appsync(_call_id, _message_id, thinking_step, sequence)
                        
                        logger.info(f"Web search tool executing: {query}")
                        response = tavily_client.search(query, max_results=3)
                        
                        # Format results
                        results = []
                        for result in response.get('results', []):
                            title = result.get('title', '')
                            content = result.get('content', '')
                            url = result.get('url', '')
                            results.append(f"{title}\n{content}\nSource: {url}")
                        
                        if not results:
                            formatted_results = "No web search results found"
                        else:
                            formatted_results = "\n\n".join(results)
                        
                        # Send tool_result thinking step
                        if THINKING_WRAPPER_AVAILABLE and APPSYNC_GRAPHQL_URL:
                            from datetime import datetime
                            sequence = get_next_sequence(_message_id)
                            thinking_step = {
                                'type': 'tool_result',
                                'tool_name': 'web_search',
                                'result': formatted_results[:500],  # Truncate
                                'success': True,
                                'timestamp': datetime.utcnow().isoformat()
                            }
                            logger.info(f"✓ Sending tool_result thinking step: web_search")
                            send_thinking_step_to_appsync(_call_id, _message_id, thinking_step, sequence)
                        
                        logger.info(f"Web search returned {len(results)} results")
                        return formatted_results
                        
                    except Exception as e:
                        # Send error thinking step
                        if THINKING_WRAPPER_AVAILABLE and APPSYNC_GRAPHQL_URL:
                            from datetime import datetime
                            sequence = get_next_sequence(_message_id)
                            thinking_step = {
                                'type': 'tool_result',
                                'tool_name': 'web_search',
                                'result': f"Error: {str(e)}",
                                'success': False,
                                'timestamp': datetime.utcnow().isoformat()
                            }
                            send_thinking_step_to_appsync(_call_id, _message_id, thinking_step, sequence)
                        
                        logger.error(f"Error in web search: {str(e)}")
                        return f"Error performing web search: {str(e)}"
                
                tools.append(web_search)
                logger.info("Web search tool enabled")
            except Exception as e:
                logger.warning(f"Could not enable web search tool: {str(e)}")
        
        # Add document retrieval tool if KB configured
        if kb_id:
            tools.append(create_document_search_tool(
                kb_id=kb_id,
                kb_region=kb_region,
                kb_account_id=kb_account_id,
                model_id=model_id,
                call_id=call_id,
                message_id=message_id
            ))
            logger.info("Document retrieval tool enabled")
        
        # Add recent meetings list tool if DynamoDB table and transcript KB configured
        if dynamodb_table_name and transcript_kb_id and user_email:
            tools.append(create_recent_meetings_tool(
                dynamodb_table_name=dynamodb_table_name,
                user_email=user_email,
                call_id=call_id,
                message_id=message_id
            ))
            logger.info("Recent meetings list tool enabled")
        
        # Add meeting history tool if transcript KB configured
        if transcript_kb_id and user_email:
            tools.append(create_meeting_history_tool(
                transcript_kb_id=transcript_kb_id,
                kb_region=kb_region,
                kb_account_id=kb_account_id,
                model_id=model_id,
                user_email=user_email,
                call_id=call_id,
                message_id=message_id
            ))
            logger.info("Meeting history tool enabled")
        
        # Add current meeting transcript tool
        if dynamodb_table_name:
            tools.append(create_current_meeting_transcript_tool(
                call_id=call_id,
                dynamodb_table_name=dynamodb_table_name,
                message_id=message_id
            ))
            logger.info("Current meeting transcript tool enabled")
        
        # Add VNC preview control tool (available to all users)
        if APPSYNC_GRAPHQL_URL:
            tools.append(create_vnc_preview_control_tool(
                call_id=call_id,
                appsync_url=APPSYNC_GRAPHQL_URL,
                message_id=message_id
            ))
            logger.info("VNC preview control tool enabled")
        
        # Add VP browser control tool (available to all users)
        if EVENT_API_HTTP_URL:
            tools.append(create_vp_browser_control_tool(
                call_id=call_id,
                event_api_http_url=EVENT_API_HTTP_URL,
                message_id=message_id
            ))
            logger.info("VP browser control tool enabled (Event API HTTP)")
        
        # Initialize Strands Agent
        try:
            from strands import Agent
            from strands.models import BedrockModel
            
            # Get message ID for streaming - use MessageId if provided, otherwise fall back to SegmentId
            transcript_segment_args = event.get('transcript_segment_args', {})
            message_id = transcript_segment_args.get('MessageId') or transcript_segment_args.get('SegmentId', f"msg-{call_id}")
            
            logger.info(f"Using MessageId for streaming: {message_id}")
            logger.info(f"Tools enabled: {len(tools)}")
            
            # Send status update: Initializing agent
            if APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'status',
                    'content': 'Initializing agent...',
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 2)
            
            # Configure Bedrock model with streaming
            # Note: Extended thinking mode not supported by all models
            bedrock_model = BedrockModel(
                model_id=model_id,
                temperature=0.3,
                streaming=ENABLE_STREAMING
            )
            logger.info(f"Bedrock model configured: {model_id}")
            
            # Create hook provider for tracking tool usage
            thinking_hook = ThinkingStepHookProvider(call_id=call_id, message_id=message_id)
            logger.info(f"Created ThinkingStepHookProvider for call_id={call_id}, message_id={message_id}")
            
            # Create agent with tools (includes MCPClient objects for managed integration)
            # Per Strands docs: "The MCPClient implements the experimental ToolProvider interface,
            # enabling direct usage in the Agent constructor with automatic lifecycle management"
            agent = Agent(
                model=bedrock_model,
                system_prompt=get_meeting_assistant_prompt_with_tools() if tools else get_meeting_assistant_prompt(),
                tools=tools if tools else None,
                hooks=[thinking_hook]  # Add hook provider to track all tool usage
            )
            
            logger.info(f"Agent created with {len(tools)} tools/clients and hook provider (managed integration)")
            logger.info(f"Hook provider registered: {thinking_hook}")
            
            # Send status update: Agent ready
            if APPSYNC_GRAPHQL_URL:
                from datetime import datetime
                thinking_step = {
                    'type': 'status',
                    'content': f'Agent ready with {len(tools)} tools',
                    'timestamp': datetime.utcnow().isoformat()
                }
                send_thinking_step_to_appsync(call_id, message_id, thinking_step, 3)
            
            # Prepare context for the agent with conversation history
            if conversation_history:
                # Format last 10 messages (5 pairs) for context
                recent_history = conversation_history[-10:]
                history_text = "\n\nPrevious conversation:\n"
                for msg in recent_history:
                    role = "User" if msg.get('role') == 'user' else "Assistant"
                    content = msg.get('content', '')
                    history_text += f"{role}: {content}\n"
                
                context_message = f"{history_text}\n\nCurrent User Request: {user_input}"
                logger.info(f"Including {len(recent_history)} messages from conversation history")
            else:
                context_message = f"User Request: {user_input}"
            
            # Handle streaming vs non-streaming
            if ENABLE_STREAMING and APPSYNC_GRAPHQL_URL:
                logger.info("Streaming mode enabled - sending tokens to AppSync")
                
                # Import asyncio for async streaming
                import asyncio
                
                async def stream_response():
                    from datetime import datetime
                    sequence = 0
                    thinking_sequence = 0
                    full_response = []
                    
                    # Use stream_async to get async iterator
                    async for event in agent.stream_async(context_message):
                        logger.debug(f"Stream event received: {type(event)} - {event}")
                        
                        # Handle different event types from Strands SDK
                        if isinstance(event, dict):
                            event_type = event.get('type', 'unknown')
                            
                            # Reasoning/thinking events
                            if event_type == 'thinking' or event_type == 'reasoning':
                                thinking_step = {
                                    'type': 'reasoning',
                                    'content': event.get('content', event.get('text', '')),
                                    'timestamp': datetime.utcnow().isoformat()
                                }
                                send_thinking_step_to_appsync(
                                    call_id=call_id,
                                    message_id=message_id,
                                    thinking_step=thinking_step,
                                    sequence=thinking_sequence
                                )
                                thinking_sequence += 1
                            
                            # Tool use events
                            elif event_type == 'tool_use' or event_type == 'tool_call':
                                tool_name = event.get('name', event.get('tool_name', 'unknown'))
                                tool_input = event.get('input', event.get('parameters', {}))
                                thinking_step = {
                                    'type': 'tool_use',
                                    'tool_name': tool_name,
                                    'tool_input': tool_input,
                                    'timestamp': datetime.utcnow().isoformat()
                                }
                                send_thinking_step_to_appsync(
                                    call_id=call_id,
                                    message_id=message_id,
                                    thinking_step=thinking_step,
                                    sequence=thinking_sequence
                                )
                                thinking_sequence += 1
                                logger.info(f"Tool use detected: {tool_name}")
                            
                            # Tool result events
                            elif event_type == 'tool_result' or event_type == 'tool_response':
                                tool_name = event.get('name', event.get('tool_name', 'unknown'))
                                result = event.get('content', event.get('result', ''))
                                is_error = event.get('is_error', False)
                                thinking_step = {
                                    'type': 'tool_result',
                                    'tool_name': tool_name,
                                    'result': str(result)[:500],  # Truncate long results
                                    'success': not is_error,
                                    'timestamp': datetime.utcnow().isoformat()
                                }
                                send_thinking_step_to_appsync(
                                    call_id=call_id,
                                    message_id=message_id,
                                    thinking_step=thinking_step,
                                    sequence=thinking_sequence
                                )
                                thinking_sequence += 1
                                logger.info(f"Tool result received: {tool_name} - Success: {not is_error}")
                            
                            # Final response data tokens
                            elif 'data' in event:
                                token_text = event['data']
                                full_response.append(token_text)
                                
                                # Send token to AppSync
                                send_chat_token_to_appsync(
                                    call_id=call_id,
                                    message_id=message_id,
                                    token=token_text,
                                    is_complete=False,
                                    sequence=sequence
                                )
                                sequence += 1
                        
                        # Handle string events (simple text tokens)
                        elif isinstance(event, str):
                            full_response.append(event)
                            send_chat_token_to_appsync(
                                call_id=call_id,
                                message_id=message_id,
                                token=event,
                                is_complete=False,
                                sequence=sequence
                            )
                            sequence += 1
                    
                    # Send completion token
                    send_chat_token_to_appsync(
                        call_id=call_id,
                        message_id=message_id,
                        token='',
                        is_complete=True,
                        sequence=sequence
                    )
                    
                    logger.info(f"Streaming complete. Response tokens: {sequence}, Thinking steps: {thinking_sequence}")
                    return ''.join(full_response)
                
                # Run the async streaming function
                response_text = asyncio.run(stream_response())
                
            else:
                # Non-streaming mode (current behavior)
                response = agent(context_message)
                
                # Convert AgentResult to string if needed
                if hasattr(response, 'text'):
                    response_text = response.text
                elif hasattr(response, '__str__'):
                    response_text = str(response)
                else:
                    response_text = response
            
            logger.info(f"Strands agent response: {response_text}")
            
            # Format response for LMA
            return {
                'message': response_text,
                'callId': call_id,
                'source': 'strands_bedrock'
            }
            
        except ImportError as e:
            logger.error(f"Strands SDK not available: {e}")
            # Fallback to direct Bedrock call if Strands is not available
            return fallback_bedrock_response(transcript, user_input, call_id)
            
    except Exception as e:
        logger.error(f"Error in Strands meeting assist: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': f'Internal error: {str(e)}',
                'callId': call_id
            })
        }

def get_meeting_assistant_prompt() -> str:
    """
    Returns the system prompt for the meeting assistant
    """
    return """You are an AI assistant helping participants during a live meeting. Your role is to:

1. Answer questions based on the meeting context and transcript
2. Provide helpful information relevant to the discussion
3. Keep responses concise and focused (under 100 words when possible)
4. If you don't have enough context from the meeting transcript, use your general knowledge
5. Be professional and supportive

When responding:
- Reference specific parts of the meeting transcript when relevant
- Provide actionable insights when possible
- Ask clarifying questions if the request is ambiguous
- Maintain a helpful and professional tone"""


def get_meeting_assistant_prompt_with_tools() -> str:
    """
    Enhanced system prompt for agent with tools
    """
    return """You are an AI assistant helping participants during a live meeting. Your role is to:

1. Intelligently determine when meeting context is needed
2. Use available tools to provide accurate, up-to-date information
3. Keep responses concise and focused (under 100 words when possible)
4. Be professional and supportive

Tool usage guidelines:

**For VP browser control (IMPORTANT - TWO STEP PROCESS):**
When using control_vp_browser, you MUST ALWAYS:
1. FIRST call control_vnc_preview(action="open") to open the live preview window
2. THEN call control_vp_browser(action="open_url", url="...") to open the website

Example workflow:
- User: "show me apple.com"
  Step 1: control_vnc_preview(action="open")  ← REQUIRED FIRST
  Step 2: control_vp_browser(action="open_url", url="https://apple.com")

This ensures the user can see the browser activity in real-time.

**For current meeting queries (USE current_meeting_transcript tool):**
- Summarizing THIS meeting
- Extracting action items from THIS meeting
- Analyzing topics discussed in THIS meeting
- Fact-checking recent statements in THIS meeting
- Responding to questions about what was just said
- When request is ambiguous but likely meeting-related
- Default to using this tool for most meeting-related queries

Examples:
- "Summarize this meeting" → current_meeting_transcript(mode="full")
- "What were the action items?" → current_meeting_transcript(mode="full")
- "What did we just discuss?" → current_meeting_transcript(lines=20, mode="recent")
- "Fact check that statement" → current_meeting_transcript(lines=10, mode="recent")

**For past meetings queries:**
- "Last meeting" → recent_meetings_list(limit=1) → meeting_history with CallId
- "Meetings about X" → meeting_history(query="X")
- "Have we discussed this before?" → meeting_history(query="topic")

**For general queries (DO NOT use current_meeting_transcript):**
- General knowledge questions
- Current events, weather, prices → web_search
- Company policies, procedures → document_search
- Questions explicitly unrelated to the meeting

**Decision logic:**
1. If question mentions "this meeting", "current meeting", "just said", "action items", "summarize" → USE current_meeting_transcript
2. If question is about past meetings → USE meeting_history or recent_meetings_list
3. If question is general knowledge or current events → USE web_search or answer directly
4. If ambiguous → DEFAULT to using current_meeting_transcript (most queries are meeting-related)

Always maintain a helpful and professional tone."""

def fallback_bedrock_response(transcript: str, user_input: str, call_id: str) -> Dict[str, Any]:
    """
    Fallback function that uses direct Bedrock API calls if Strands is not available
    """
    try:
        bedrock_client = boto3.client('bedrock-runtime')
        model_id = os.environ.get('MODEL_ID', 'global.anthropic.claude-haiku-4-5-20251001-v1:0')
        
        # Prepare the prompt
        prompt = f"""You are an AI assistant helping during a meeting.

Meeting Context:
{transcript if transcript else "No meeting transcript available yet."}

User Request: {user_input}

Please provide a helpful response based on the meeting context. Keep it concise and professional."""

        # Prepare the message for Bedrock
        message = {
            "role": "user",
            "content": [{"text": prompt}]
        }
        
        # Call Bedrock
        response = bedrock_client.converse(
            modelId=model_id,
            messages=[message],
            inferenceConfig={
                'temperature': 0.3,
                'maxTokens': 500
            }
        )
        
        # Extract response text
        response_text = response['output']['message']['content'][0]['text']
        
        logger.info(f"Fallback Bedrock response: {response_text}")
        
        return {
            'message': response_text,
            'callId': call_id,
            'source': 'bedrock_fallback'
        }
        
    except Exception as e:
        logger.error(f"Fallback Bedrock call failed: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': f'Fallback error: {str(e)}',
                'callId': call_id
            })
        }
