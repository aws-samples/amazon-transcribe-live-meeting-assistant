#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Strands-based Meeting Assistant Lambda Function
Provides a lightweight alternative to QnABot using AWS Strands SDK
"""

import json
import os
import boto3
from boto3.dynamodb.conditions import Key, Attr
from typing import Dict, Any, Optional
import logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')
bedrock_agent_runtime = boto3.client('bedrock-agent-runtime')

# Get AppSync endpoint from environment
APPSYNC_GRAPHQL_URL = os.environ.get('APPSYNC_GRAPHQL_URL', '')
ENABLE_STREAMING = os.environ.get('ENABLE_STREAMING', 'false').lower() == 'true'


def create_document_search_tool(kb_id: str, kb_region: str, kb_account_id: str, model_id: str):
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
            
            logger.info(f"Document search result: {result[:100]}...")
            return result
            
        except Exception as e:
            logger.error(f"Error in document search: {str(e)}")
            return f"Error retrieving documents: {str(e)}"
    
    return document_search


def create_recent_meetings_tool(dynamodb_table_name: str, user_email: str):
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
                return "No recent meetings found"
            
            result = json.dumps(meetings, indent=2)
            logger.info(f"Recent meetings returned {len(meetings)} meetings")
            return result
            
        except Exception as e:
            logger.error(f"Error in recent meetings list: {str(e)}")
            return f"Error retrieving recent meetings: {str(e)}"
    
    return recent_meetings_list


def create_meeting_history_tool(transcript_kb_id: str, kb_region: str, kb_account_id: str, 
                                model_id: str, user_email: str):
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
            logger.info(f"Meeting history tool executing: {query} for user: {user_email}")
            
            # Determine model ARN based on model type
            if model_id.startswith("anthropic"):
                model_arn = f"arn:aws:bedrock:{kb_region}::foundation-model/{model_id}"
            else:
                model_arn = f"arn:aws:bedrock:{kb_region}:{kb_account_id}:inference-profile/{model_id}"
            
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
            
            response = bedrock_agent_runtime.retrieve_and_generate(**kb_input)
            result = response.get("output", {}).get("text", "No past meetings found matching your query")
            
            logger.info(f"Meeting history result: {result[:100]}...")
            return result
            
        except Exception as e:
            logger.error(f"Error in meeting history search: {str(e)}")
            return f"Error retrieving meeting history: {str(e)}"
    
    return meeting_history

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
        
        model_id = os.environ.get('MODEL_ID', 'anthropic.claude-3-haiku-20240307-v1:0')
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
        model_id = os.environ.get('MODEL_ID', 'anthropic.claude-3-haiku-20240307-v1:0')
        tavily_api_key = os.environ.get('TAVILY_API_KEY', '')
        
        # Fetch meeting transcript from DynamoDB
        transcript = fetch_meeting_transcript(call_id, dynamodb_table_name) if dynamodb_table_name else event.get('transcript', '')
        
        # Initialize tools list
        tools = []
        
        # Add web search tool if API key provided (auto-enable)
        if tavily_api_key:
            try:
                from tavily import TavilyClient
                from strands import tool
                
                # Create Tavily client in closure
                tavily_client = TavilyClient(api_key=tavily_api_key)
                
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
                            return "No web search results found"
                        
                        formatted_results = "\n\n".join(results)
                        logger.info(f"Web search returned {len(results)} results")
                        return formatted_results
                        
                    except Exception as e:
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
                model_id=model_id
            ))
            logger.info("Document retrieval tool enabled")
        
        # Add recent meetings list tool if DynamoDB table and transcript KB configured
        if dynamodb_table_name and transcript_kb_id and user_email:
            tools.append(create_recent_meetings_tool(
                dynamodb_table_name=dynamodb_table_name,
                user_email=user_email
            ))
            logger.info("Recent meetings list tool enabled")
        
        # Add meeting history tool if transcript KB configured
        if transcript_kb_id and user_email:
            tools.append(create_meeting_history_tool(
                transcript_kb_id=transcript_kb_id,
                kb_region=kb_region,
                kb_account_id=kb_account_id,
                model_id=model_id,
                user_email=user_email
            ))
            logger.info("Meeting history tool enabled")
        
        # Initialize Strands Agent
        try:
            from strands import Agent
            from strands.models import BedrockModel
            
            # Get message ID for streaming - use MessageId if provided, otherwise fall back to SegmentId
            transcript_segment_args = event.get('transcript_segment_args', {})
            message_id = transcript_segment_args.get('MessageId') or transcript_segment_args.get('SegmentId', f"msg-{call_id}")
            
            logger.info(f"Using MessageId for streaming: {message_id}")
            logger.info(f"Tools enabled: {len(tools)}")
            
            # Configure Bedrock model with streaming if enabled
            bedrock_model = BedrockModel(
                model_id=model_id,
                temperature=0.3,
                streaming=ENABLE_STREAMING
            )
            
            # Create agent with tools
            agent = Agent(
                model=bedrock_model,
                system_prompt=get_meeting_assistant_prompt_with_tools() if tools else get_meeting_assistant_prompt(),
                tools=tools if tools else None
            )
            
            # Prepare context for the agent
            context_message = f"""
Meeting Transcript:
{transcript if transcript else "No meeting transcript available yet."}

User Request: {user_input}
"""
            
            # Handle streaming vs non-streaming
            if ENABLE_STREAMING and APPSYNC_GRAPHQL_URL:
                logger.info("Streaming mode enabled - sending tokens to AppSync")
                
                # Import asyncio for async streaming
                import asyncio
                
                async def stream_response():
                    sequence = 0
                    full_response = []
                    
                    # Use stream_async to get async iterator
                    async for event in agent.stream_async(context_message):
                        # Check if this is a data event with text content
                        if isinstance(event, dict) and 'data' in event:
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
                    
                    # Send completion token
                    send_chat_token_to_appsync(
                        call_id=call_id,
                        message_id=message_id,
                        token='',
                        is_complete=True,
                        sequence=sequence
                    )
                    
                    logger.info(f"Streaming complete. Total tokens: {sequence}")
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

1. Answer questions based on the meeting context and transcript
2. Use available tools intelligently to provide accurate, up-to-date information
3. Keep responses concise and focused (under 100 words when possible)
4. Be professional and supportive

Tool usage guidelines:

**For chronological queries (last/recent/latest meetings):**
1. FIRST use recent_meetings_list to get chronologically ordered meetings
2. THEN use meeting_history with specific CallIds if detailed content needed
3. Example: "What was the last meeting about?" → recent_meetings_list(limit=1) → meeting_history with that CallId

**For semantic queries (meetings about specific topics):**
1. Use meeting_history directly for semantic search
2. Optionally use recent_meetings_list first to establish time context
3. Example: "Find meetings about budget" → meeting_history(query="budget")

**For hybrid queries (recent meetings about X):**
1. Use recent_meetings_list to get recent CallIds
2. Use meeting_history with those CallIds to filter by topic
3. Cross-reference results to find recent meetings matching the topic

**Other tools:**
- Use web_search for: current events, latest prices, recent news, real-time data
- Use document_search for: company policies, product info, internal procedures

When using tools:
- Always cite meeting dates when available
- If recent_meetings_list returns no results, inform user no recent meetings found
- Combine information from multiple tools when helpful
- Be explicit about chronological vs semantic relevance

Always maintain a helpful and professional tone."""

def fallback_bedrock_response(transcript: str, user_input: str, call_id: str) -> Dict[str, Any]:
    """
    Fallback function that uses direct Bedrock API calls if Strands is not available
    """
    try:
        bedrock_client = boto3.client('bedrock-runtime')
        model_id = os.environ.get('MODEL_ID', 'anthropic.claude-3-haiku-20240307-v1:0')
        
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
