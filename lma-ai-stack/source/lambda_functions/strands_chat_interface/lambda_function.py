#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
STRANDS Chat Interface Lambda Function
Handles sendChatMessage GraphQL mutation and invokes AsyncAgentAssistOrchestrator
"""

import json
import os
import boto3
import uuid
from datetime import datetime
from typing import Dict, Any
import logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
lambda_client = boto3.client('lambda')

def lambda_handler(event, context):
    """
    Lambda handler for STRANDS chat interface GraphQL resolver
    
    Expected event structure from AppSync:
    {
        "arguments": {
            "input": {
                "CallId": "call-id",
                "Message": "user message"
            }
        },
        "identity": {
            "username": "user@example.com",
            "claims": {...}
        }
    }
    """
    try:
        logger.info(f"STRANDS Chat Interface - Processing event: {json.dumps(event)}")
        
        # Extract parameters from AppSync event
        arguments = event.get('arguments', {})
        input_data = arguments.get('input', {})
        identity = event.get('identity', {})
        
        call_id = input_data.get('CallId', '')
        message = input_data.get('Message', '')
        username = identity.get('username', 'ChatUser')
        
        if not message or not call_id:
            logger.error("Missing required parameters: CallId and Message")
            raise ValueError('CallId and Message are required')
        
        # Get AsyncAgentAssistOrchestrator ARN from environment
        orchestrator_arn = os.environ.get('ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN')
        if not orchestrator_arn:
            logger.error("ASYNC_AGENT_ASSIST_ORCHESTRATOR_ARN not configured")
            raise ValueError('AsyncAgentAssistOrchestrator not configured')
        
        # Generate unique message ID
        message_id = f"chat-{uuid.uuid4()}"
        
        # Create event payload for AsyncAgentAssistOrchestrator
        # Use CHAT_ASSISTANT channel to separate from voice wake phrases
        orchestrator_payload = {
            "CallId": call_id,
            "Channel": "CHAT_ASSISTANT",  # Use dedicated chat channel
            "SegmentId": message_id,
            "MessageId": message_id,  # Pass MessageId for token streaming
            "StartTime": datetime.now().timestamp(),
            "EndTime": datetime.now().timestamp() + 1,
            "Transcript": message,
            "Speaker": username,
            "IsPartial": False,
            "Status": "TRANSCRIBING",
            "CreatedAt": datetime.utcnow().isoformat() + "Z",
            "ExpiresAfter": int(datetime.now().timestamp()) + (90 * 24 * 60 * 60),  # 90 days
            "Owner": username,
        }
        
        logger.info(f"STRANDS Chat Interface - Invoking AsyncAgentAssistOrchestrator: {orchestrator_arn}")
        logger.info(f"STRANDS Chat Interface - Payload: {json.dumps(orchestrator_payload)}")
        
        # Invoke AsyncAgentAssistOrchestrator asynchronously
        # The response will be streamed via addChatToken mutations
        response = lambda_client.invoke(
            FunctionName=orchestrator_arn,
            InvocationType='Event',  # Asynchronous invocation - don't wait for response
            Payload=json.dumps(orchestrator_payload)
        )
        
        logger.info(f"STRANDS Chat Interface - AsyncAgentAssistOrchestrator invoked successfully (async)")
        
        # Return immediately with MessageId so UI can subscribe to token stream
        # The actual response will come via onAddChatToken subscription
        return {
            "MessageId": message_id,
            "Status": "PROCESSING",
            "CallId": call_id,
            "Response": None  # Response will be streamed via tokens
        }
        
    except Exception as e:
        logger.error(f"Error in STRANDS chat interface: {str(e)}")
        # Re-raise the exception so AppSync can handle it properly
        raise e
