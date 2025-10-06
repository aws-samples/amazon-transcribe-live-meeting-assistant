#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
QnABot Lambda Hook for STRANDS Backend
Integrates QnABot UI with STRANDS intelligence
"""

import json
import os
import boto3
import logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
lambda_client = boto3.client('lambda')

def handler(event, context):
    """
    QnABot Lambda Hook that calls STRANDS Lambda as backend
    
    Expected event structure from QnABot:
    {
        "req": {
            "question": "user question",
            "session": {
                "qnabotcontext": {
                    "previous": {...}
                }
            },
            "_event": {
                "requestAttributes": {
                    "callId": "meeting-id"
                }
            }
        },
        "res": {
            "message": "",
            "session": {...}
        }
    }
    """
    try:
        logger.info(f"QnABot STRANDS Hook - Processing event: {json.dumps(event)}")
        
        # Extract QnABot request
        req = event.get('req', {})
        res = event.get('res', {})
        
        # Get user question
        question = req.get('question', '')
        
        # Get callId from request attributes
        request_attributes = req.get('_event', {}).get('requestAttributes', {})
        call_id = request_attributes.get('callId', '')
        
        if not question:
            res['message'] = "I didn't receive a question. Please try again."
            return event
        
        # Get STRANDS Lambda ARN from environment
        strands_lambda_arn = os.environ.get('STRANDS_LAMBDA_ARN')
        if not strands_lambda_arn:
            logger.error("STRANDS_LAMBDA_ARN not configured")
            res['message'] = "Meeting assistant is not properly configured."
            return event
        
        # Get DynamoDB table name for transcript fetching
        dynamodb_table_name = os.environ.get('DYNAMODB_TABLE_NAME', '')
        
        # Prepare payload for STRANDS Lambda
        strands_payload = {
            'text': question,
            'call_id': call_id,
            'dynamodb_table_name': dynamodb_table_name,
            'dynamodb_pk': f"c#{call_id}"
        }
        
        logger.info(f"Calling STRANDS Lambda: {strands_lambda_arn}")
        logger.info(f"STRANDS payload: {json.dumps(strands_payload)}")
        
        # Invoke STRANDS Lambda
        response = lambda_client.invoke(
            FunctionName=strands_lambda_arn,
            InvocationType='RequestResponse',
            Payload=json.dumps(strands_payload)
        )
        
        # Parse STRANDS response
        strands_response = json.loads(response['Payload'].read())
        
        logger.info(f"STRANDS response: {json.dumps(strands_response)}")
        
        # Extract message from STRANDS response
        message = strands_response.get('message', 'No response from STRANDS')
        
        # Set QnABot response
        res['message'] = message
        
        # Optionally add markdown formatting
        res['session']['appContext'] = {
            'altMessages': {
                'markdown': message
            }
        }
        
        logger.info(f"Returning QnABot response: {message}")
        
        return event
        
    except Exception as e:
        logger.error(f"Error in QnABot STRANDS hook: {str(e)}")
        res['message'] = f"I encountered an error processing your request. Please try again."
        return event
