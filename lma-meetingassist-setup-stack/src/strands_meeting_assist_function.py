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
from typing import Dict, Any
import logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

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
        transcript = event.get('transcript', '')
        user_input = event.get('userInput', '') or event.get('text', '')
        call_id = event.get('callId', '') or event.get('call_id', '')
        
        if not user_input:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': 'userInput or text is required'
                })
            }
        
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
Meeting Context:
{transcript if transcript else "No meeting transcript available yet."}

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
