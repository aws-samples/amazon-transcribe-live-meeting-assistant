#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
AgentCore Gateway Manager Lambda Function
Manages the lifecycle of AWS Bedrock AgentCore Gateway for MCP protocol integration
"""

import json
import boto3
import cfnresponse
import time
import logging
import os
from typing import Any, Dict
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


def handler(event, context):
    """
    CloudFormation custom resource handler for AgentCore Gateway.
    Manages CREATE, UPDATE, and DELETE operations.
    """
    logger.info(f"Received event: {json.dumps(event, default=str)}")
    
    props = event.get('ResourceProperties', {})
    stack_name = props.get('StackName', 'UNKNOWN')
    gateway_name = f"{stack_name}-MCP-Gateway"
    
    try:
        request_type = event['RequestType']
        
        if request_type == 'Delete':
            delete_gateway(props, gateway_name)
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, 
                           physicalResourceId=gateway_name)
            return
        
        # Create or Update
        gateway_config = create_or_update_gateway(props, gateway_name)
        
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {
            'GatewayUrl': gateway_config.get('gateway_url'),
            'GatewayId': gateway_config.get('gateway_id'),
            'GatewayArn': gateway_config.get('gateway_arn')
        }, physicalResourceId=gateway_name)
    
    except Exception as e:
        logger.error(f"Error: {str(e)}", exc_info=True)
        
        # Check if bedrock-agentcore service is unavailable
        if 'bedrock-agentcore' in str(e).lower() and \
           ('access' in str(e).lower() or 'not available' in str(e).lower()):
            logger.warning("bedrock-agentcore service unavailable - continuing without MCP gateway")
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                'GatewayUrl': 'N/A - Service not available in region',
                'GatewayId': 'N/A',
                'GatewayArn': 'N/A'
            }, physicalResourceId=gateway_name)
        else:
            cfnresponse.send(event, context, cfnresponse.FAILED, {},
                           physicalResourceId=gateway_name,
                           reason=str(e)[:1024])


def create_or_update_gateway(props: Dict[str, Any], gateway_name: str) -> Dict[str, str]:
    """
    Create or update AgentCore Gateway.
    If gateway exists, return existing configuration.
    Otherwise, create new gateway.
    """
    region = props['Region']
    lambda_arn = props['LambdaArn']
    user_pool_id = props['UserPoolId']
    client_id = props['ClientId']
    execution_role_arn = props['ExecutionRoleArn']
    
    control_client = boto3.client("bedrock-agentcore-control", region_name=region)
    
    # Check if gateway already exists
    try:
        resp = control_client.list_gateways(maxResults=100)
        existing_gateways = [g for g in resp.get("items", []) 
                           if g.get("name") == gateway_name]
        
        if existing_gateways:
            gateway_id = existing_gateways[0].get('gatewayId')
            logger.info(f"Gateway {gateway_name} already exists with ID: {gateway_id}")
            
            # Get full gateway details
            gateway_details = control_client.get_gateway(gatewayIdentifier=gateway_id)
            
            return {
                'gateway_url': gateway_details.get('gatewayUrl'),
                'gateway_id': gateway_details.get('gatewayId'),
                'gateway_arn': gateway_details.get('gatewayArn')
            }
    
    except ClientError as e:
        logger.warning(f"Error checking for existing gateway: {e}")
    
    # Gateway doesn't exist, create new one
    logger.info(f"Creating new gateway: {gateway_name}")
    
    # Create JWT authorizer configuration
    authorizer_config = {
        "customJWTAuthorizer": {
            "discoveryUrl": f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}/.well-known/openid-configuration",
            "allowedClients": [client_id]
        }
    }
    
    # Create gateway
    create_response = control_client.create_gateway(
        name=gateway_name,
        gatewayType="MCP",
        authorizerConfig=authorizer_config,
        executionRoleArn=execution_role_arn
    )
    
    gateway_id = create_response.get('gatewayId')
    gateway_url = create_response.get('gatewayUrl')
    gateway_arn = create_response.get('gatewayArn')
    
    logger.info(f"Gateway created with ID: {gateway_id}, URL: {gateway_url}")
    
    # Wait for gateway to be ready
    logger.info("Waiting for gateway to be ready...")
    time.sleep(10)
    
    # Add Lambda target to gateway
    logger.info(f"Adding Lambda target: {lambda_arn}")
    
    try:
        control_client.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name="LMAAnalyticsTarget",
            targetType="LAMBDA",
            targetPayload={
                "lambdaArn": lambda_arn,
                "toolSchema": {
                    "inlinePayload": [
                        {
                            "name": "search_lma_meetings",
                            "description": "Search across all meeting transcripts and summaries using natural language queries",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "Natural language search query"
                                    },
                                    "startDate": {
                                        "type": "string",
                                        "description": "Optional ISO 8601 start date"
                                    },
                                    "endDate": {
                                        "type": "string",
                                        "description": "Optional ISO 8601 end date"
                                    },
                                    "maxResults": {
                                        "type": "number",
                                        "description": "Maximum results to return",
                                        "default": 10
                                    }
                                },
                                "required": ["query"]
                            }
                        },
                        {
                            "name": "get_meeting_transcript",
                            "description": "Retrieve the complete transcript for a specific meeting",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "meetingId": {
                                        "type": "string",
                                        "description": "Meeting ID (CallId)"
                                    },
                                    "format": {
                                        "type": "string",
                                        "enum": ["json", "text", "srt"],
                                        "description": "Output format",
                                        "default": "text"
                                    }
                                },
                                "required": ["meetingId"]
                            }
                        },
                        {
                            "name": "get_meeting_summary",
                            "description": "Get AI-generated summary and action items for a meeting",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "meetingId": {
                                        "type": "string",
                                        "description": "Meeting ID (CallId)"
                                    },
                                    "includeActionItems": {
                                        "type": "boolean",
                                        "description": "Include action items",
                                        "default": true
                                    },
                                    "includeTopics": {
                                        "type": "boolean",
                                        "description": "Include key topics",
                                        "default": true
                                    }
                                },
                                "required": ["meetingId"]
                            }
                        },
                        {
                            "name": "list_meetings",
                            "description": "List meetings with optional filters",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "startDate": {
                                        "type": "string",
                                        "description": "ISO 8601 start date"
                                    },
                                    "endDate": {
                                        "type": "string",
                                        "description": "ISO 8601 end date"
                                    },
                                    "participant": {
                                        "type": "string",
                                        "description": "Filter by participant name"
                                    },
                                    "status": {
                                        "type": "string",
                                        "enum": ["STARTED", "ENDED", "ALL"],
                                        "default": "ALL"
                                    },
                                    "limit": {
                                        "type": "number",
                                        "default": 20,
                                        "maximum": 100
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        )
        
        logger.info("Lambda target added successfully")
        
    except ClientError as e:
        logger.error(f"Error adding Lambda target: {e}")
        # Continue anyway - gateway is created
    
    # Wait for target to be ready
    time.sleep(5)
    
    return {
        'gateway_url': gateway_url,
        'gateway_id': gateway_id,
        'gateway_arn': gateway_arn
    }


def delete_gateway(props: Dict[str, Any], gateway_name: str):
    """
    Delete AgentCore Gateway and all its targets.
    """
    try:
        region = props['Region']
        control_client = boto3.client("bedrock-agentcore-control", region_name=region)
        
        # Find gateway by name
        resp = control_client.list_gateways(maxResults=100)
        gateways = [g for g in resp.get("items", []) if g.get("name") == gateway_name]
        
        if not gateways:
            logger.info(f"Gateway {gateway_name} not found - already deleted")
            return
        
        gateway_id = gateways[0].get("gatewayId")
        logger.info(f"Deleting gateway: {gateway_id}")
        
        # Step 1: Delete all targets first
        try:
            targets_resp = control_client.list_gateway_targets(gatewayIdentifier=gateway_id)
            targets = targets_resp.get("items", [])
            
            logger.info(f"Found {len(targets)} targets to delete")
            
            for target in targets:
                target_id = target["targetId"]
                logger.info(f"Deleting target: {target_id}")
                
                try:
                    control_client.delete_gateway_target(
                        gatewayIdentifier=gateway_id,
                        targetId=target_id
                    )
                    time.sleep(2)  # Wait between deletions
                except ClientError as e:
                    logger.warning(f"Error deleting target {target_id}: {e}")
            
            # Wait for all targets to be deleted
            if targets:
                logger.info("Waiting for targets to be deleted...")
                time.sleep(10)
        
        except ClientError as e:
            logger.warning(f"Error managing targets: {e}")
        
        # Step 2: Delete the gateway
        try:
            control_client.delete_gateway(gatewayIdentifier=gateway_id)
            logger.info(f"Gateway {gateway_id} deleted successfully")
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                logger.info("Gateway already deleted")
            else:
                raise
    
    except Exception as e:
        logger.error(f"Gateway deletion failed: {e}")
        # Don't fail CloudFormation delete - log and continue