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
from typing import Dict, Any
import logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')
bedrock_agent_runtime = boto3.client('bedrock-agent-runtime')

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
    Lambda handler for Strands-based meeting assistance
    
    Expected event structure:
    {
        "transcript": "meeting transcript context",
        "userInput": "user question or request",
        "callId": "unique call identifier"
    }
    """
    try:
        logger.info(f"Strands Meeting Assist - Processing event: {json.dumps(event)}")
        
        # Extract parameters from event - handle both 'text' and 'userInput' for compatibility
        user_input = event.get('userInput', '') or event.get('text', '')
        call_id = event.get('callId', '') or event.get('call_id', '')
        dynamodb_table_name = event.get('dynamodb_table_name', '')
        
        if not user_input:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': 'userInput or text is required'
                })
            }
        
        # Fetch meeting transcript from DynamoDB
        transcript = fetch_meeting_transcript(call_id, dynamodb_table_name) if dynamodb_table_name else event.get('transcript', '')
        
        # Query knowledge base if configured
        kb_context = query_knowledge_base(user_input, call_id)
        
        # Initialize Strands Agent
        try:
            from strands import Agent
            from strands.models import BedrockModel
            
            # Get model configuration from environment variables
            model_id = os.environ.get('MODEL_ID', 'anthropic.claude-3-haiku-20240307-v1:0')
            
            # Configure Bedrock model
            bedrock_model = BedrockModel(
                model_id=model_id,
                temperature=0.3,
                streaming=False
            )
            
            # Create agent with meeting assistant prompt
            agent = Agent(
                model=bedrock_model,
                system_prompt=get_meeting_assistant_prompt()
            )
            
            # Prepare context for the agent
            context_message = f"""
Meeting Transcript:
{transcript if transcript else "No meeting transcript available yet."}

{f"Knowledge Base Context:\n{kb_context}\n" if kb_context else ""}
User Request: {user_input}
"""
            
            # Get response from Strands agent
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
