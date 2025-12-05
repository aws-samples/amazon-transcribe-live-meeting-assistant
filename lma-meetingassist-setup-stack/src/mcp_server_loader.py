#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
MCP Server Loader for Strands Lambda
Loads and initializes MCP servers installed via the Public Registry
"""

import os
import sys
import json
import boto3
import logging
import subprocess
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Add /opt/nodejs/node_modules to NODE_PATH for layer packages
if '/opt/nodejs/node_modules' not in os.environ.get('NODE_PATH', ''):
    os.environ['NODE_PATH'] = '/opt/nodejs/node_modules:' + os.environ.get('NODE_PATH', '')

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')

# Environment variables
MCP_SERVERS_TABLE = os.environ.get('MCP_SERVERS_TABLE', '')
ACCOUNT_ID = os.environ.get('AWS_ACCOUNT_ID', '')


def get_installed_servers() -> List[Dict[str, Any]]:
    """
    Query DynamoDB for account's installed MCP servers
    
    Returns:
        List of server configurations with Status='ACTIVE'
    """
    if not MCP_SERVERS_TABLE or not ACCOUNT_ID:
        logger.warning("MCP Servers table or Account ID not configured")
        return []
    
    try:
        table = dynamodb.Table(MCP_SERVERS_TABLE)
        
        response = table.query(
            KeyConditionExpression='AccountId = :accountId',
            FilterExpression='#status = :status',
            ExpressionAttributeNames={'#status': 'Status'},
            ExpressionAttributeValues={
                ':accountId': ACCOUNT_ID,
                ':status': 'ACTIVE'
            }
        )
        
        servers = response.get('Items', [])
        logger.info(f"Found {len(servers)} active MCP servers for account {ACCOUNT_ID}")
        
        return servers
        
    except Exception as e:
        logger.error(f"Error querying MCP servers: {e}")
        return []


def create_mcp_server_tools(server_config: Dict[str, Any]) -> List[Any]:
    """
    Create Strands tools from an installed MCP server
    
    Args:
        server_config: Server configuration from DynamoDB
        
    Returns:
        List of Strands tool functions
    """
    from strands import tool
    
    server_id = server_config.get('ServerId', 'unknown')
    npm_package = server_config.get('NpmPackage', '')
    auth_config = server_config.get('AuthConfig', {})
    
    logger.info(f"Creating tools for MCP server: {server_id}")
    logger.info(f"  Package: {npm_package}")
    
    # For now, return empty list - full MCP client integration coming next
    # This is a placeholder that will be expanded to:
    # 1. Spawn MCP server process (npx {npm_package})
    # 2. Connect via stdio
    # 3. List available tools
    # 4. Wrap each tool as a Strands @tool function
    
    tools = []
    
    try:
        # TODO: Implement MCP client connection
        # from mcp import Client, StdioServerParameters
        # 
        # server = StdioServerParameters(
        #     command="npx",
        #     args=["-y", npm_package],
        #     env=auth_config
        # )
        # 
        # async with Client(server) as client:
        #     tools_list = await client.list_tools()
        #     
        #     for mcp_tool in tools_list:
        #         @tool
        #         def dynamic_tool(**kwargs):
        #             return client.call_tool(mcp_tool.name, kwargs)
        #         
        #         tools.append(dynamic_tool)
        
        logger.info(f"Created {len(tools)} tools for {server_id}")
        
    except Exception as e:
        logger.error(f"Error creating tools for {server_id}: {e}")
    
    return tools


def load_account_mcp_servers() -> List[Any]:
    """
    Load all installed MCP servers for the account and return their tools
    
    Returns:
        List of Strands tool functions from all installed servers
    """
    all_tools = []
    
    try:
        servers = get_installed_servers()
        
        if not servers:
            logger.info("No MCP servers installed for this account")
            return []
        
        logger.info(f"Loading {len(servers)} MCP servers...")
        
        for server in servers:
            server_id = server.get('ServerId', 'unknown')
            try:
                tools = create_mcp_server_tools(server)
                all_tools.extend(tools)
                logger.info(f"✓ Loaded {len(tools)} tools from {server_id}")
            except Exception as e:
                logger.error(f"Failed to load server {server_id}: {e}")
                continue
        
        logger.info(f"Total MCP tools loaded: {len(all_tools)}")
        
    except Exception as e:
        logger.error(f"Error loading MCP servers: {e}")
    
    return all_tools