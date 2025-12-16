#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
MCP Server Loader for Python Lambda
Dynamically loads MCP server tools from installed PyPI packages using Strands MCPClient

This module connects to installed MCP servers via stdio transport and retrieves their tools
for use with Strands agents. Each MCP server runs as a subprocess during tool loading.
"""

import os
import boto3
import logging
from typing import List, Optional
import sys
import importlib.metadata
import importlib.util

logger = logging.getLogger(__name__)

# Initialize DynamoDB client
dynamodb = boto3.resource('dynamodb')

# Environment variables
MCP_SERVERS_TABLE = os.environ.get('MCP_SERVERS_TABLE', '')
AWS_ACCOUNT_ID = os.environ.get('AWS_ACCOUNT_ID', '')


def load_http_mcp_server(server_id: str, server_url: str, auth_config: dict = None) -> list:
    """
    Load an HTTP-based MCP server (streamable-http transport)
    
    HTTP servers don't require installation - they're external services.
    We just need to connect to them via HTTP transport.
    
    Args:
        server_id: Server identifier
        server_url: HTTP endpoint URL
        auth_config: Optional authentication configuration with headers and params
        
    Returns:
        List of tools from the HTTP server
    """
    try:
        from strands.tools.mcp import MCPClient
        from mcp.client.streamable_http import streamablehttp_client
        
        logger.info(f"Loading HTTP MCP server: {server_id} at {server_url}")
        
        # Extract headers and params from auth config
        headers = auth_config.get('headers', {}) if auth_config else {}
        params = auth_config.get('params', {}) if auth_config else {}
        
        if headers:
            logger.info(f"Using auth headers: {list(headers.keys())}")
        if params:
            logger.info(f"Using auth params: {list(params.keys())}")
        
        # Create MCP client with HTTP transport
        mcp_client = MCPClient(
            lambda: streamablehttp_client(
                url=server_url,
                headers=headers,
                params=params
            ),
            prefix=server_id.replace('/', '_').replace('.', '_')
        )
        
        # Connect and retrieve tools
        with mcp_client:
            tools = mcp_client.list_tools_sync()
            logger.info(f"Loaded {len(tools)} tools from HTTP server {server_id}")
            return tools
            
    except Exception as e:
        logger.warning(f"Failed to load HTTP MCP server {server_id}: {e}")
        import traceback
        logger.debug(f"Traceback: {traceback.format_exc()}")
        return []


def discover_console_script_path(package_name: str) -> Optional[tuple]:
    """
    Discover the console_scripts entry point and construct full path for execution.
    
    MCP servers register CLI commands via console_scripts. In Lambda layers, these
    scripts are installed in /opt/python/bin/ but that's not in PATH. We need to
    find the entry point and execute it with Python directly.
    
    Args:
        package_name: PyPI package name (e.g., 'mcpcap', 'google-analytics-mcp')
        
    Returns:
        Tuple of (module_path, function_name) to execute, or None if not found
        Example: ('mcpcap.__main__', 'main')
    """
    try:
        dist = importlib.metadata.distribution(package_name)
        entry_points = dist.entry_points
        
        # Look for console_scripts entry points
        for ep in entry_points:
            if ep.group == 'console_scripts':
                # Entry point format: 'mcpcap = mcpcap.__main__:main'
                # We need the module path and function name
                if ':' in ep.value:
                    module_path, func_name = ep.value.split(':', 1)
                    logger.info(f"Found console script for {package_name}: {ep.name} -> {module_path}:{func_name}")
                    return (module_path, func_name)
                
        logger.warning(f"No console_scripts found for {package_name}")
        return None
        
    except Exception as e:
        logger.warning(f"Could not check entry points for {package_name}: {e}")
        return None


def load_account_mcp_servers() -> List:
    """
    Load MCP server tools for the current AWS account using Strands MCPClient
    
    This function:
    1. Queries DynamoDB for ACTIVE MCP servers
    2. Dynamically discovers each server's Python module entry point
    3. Spawns each server as subprocess with stdio transport
    4. Connects via Strands MCPClient to retrieve tools
    5. Returns list of tools that can be used by Strands Agent
    
    Returns:
        List of Strands AgentTool objects from installed MCP servers
    """
    tools = []
    
    if not MCP_SERVERS_TABLE:
        logger.warning("MCP_SERVERS_TABLE not configured")
        return tools
    
    if not AWS_ACCOUNT_ID:
        logger.warning("AWS_ACCOUNT_ID not configured")
        return tools
    
    try:
        # Check if required dependencies are available
        try:
            from strands.tools.mcp import MCPClient
            from mcp import stdio_client, StdioServerParameters
        except ImportError as e:
            logger.warning(f"Strands MCP support not available: {e}")
            return tools
        
        table = dynamodb.Table(MCP_SERVERS_TABLE)
        
        # Query for all active servers for this account
        response = table.query(
            KeyConditionExpression='AccountId = :accountId',
            FilterExpression='#status = :status',
            ExpressionAttributeNames={'#status': 'Status'},
            ExpressionAttributeValues={
                ':accountId': AWS_ACCOUNT_ID,
                ':status': 'ACTIVE'
            }
        )
        
        servers = response.get('Items', [])
        logger.info(f"Found {len(servers)} active MCP servers for account {AWS_ACCOUNT_ID}")
        
        # Load each server's tools
        for server in servers:
            server_id = server.get('ServerId', 'unknown')
            package_name = server.get('NpmPackage', '')  # PyPI package name or HTTP URL
            package_type = server.get('PackageType', 'pypi')  # Default to pypi for backward compatibility
            server_url = server.get('ServerUrl')
            auth_config = server.get('AuthConfig')
            
            # Handle HTTP servers separately
            if package_type == 'streamable-http':
                logger.info(f"Loading HTTP MCP server: {server_id}")
                
                # Parse auth config - support flexible headers and params
                http_auth_config = {'headers': {}, 'params': {}}
                
                if auth_config:
                    try:
                        import json
                        auth_data = json.loads(auth_config) if isinstance(auth_config, str) else auth_config
                        logger.info(f"Auth config for {server_id}: {json.dumps(auth_data, default=str)[:300]}")
                        
                        # Direct headers/params (most flexible - user can specify exactly what they need)
                        if 'headers' in auth_data:
                            http_auth_config['headers'] = auth_data['headers']
                            logger.info(f"Using custom headers from config")
                        
                        if 'params' in auth_data:
                            http_auth_config['params'] = auth_data['params']
                            logger.info(f"Using custom params from config")
                        
                    except Exception as e:
                        logger.warning(f"Could not parse auth config for {server_id}: {e}")
                        import traceback
                        logger.debug(f"Traceback: {traceback.format_exc()}")
                else:
                    logger.info(f"No auth config found for HTTP server {server_id}")
                
                # Load HTTP server with auth config
                http_tools = load_http_mcp_server(server_id, server_url, http_auth_config)
                tools.extend(http_tools)
                continue
            
            # Handle PyPI package-based servers
            # Dynamically discover the console script entry point
            entry_point = discover_console_script_path(package_name)
            
            if not entry_point:
                logger.warning(
                    f"Could not discover entry point for MCP server {server_id} ({package_name}). "
                    f"The package may not have a registered console_scripts entry point."
                )
                continue
            
            module_path, func_name = entry_point
            
            try:
                logger.info(f"Loading PyPI MCP server: {server_id} ({package_name})")
                logger.info(f"Using entry point: {module_path}:{func_name}")
                
                # In Lambda layers, console scripts are installed in /opt/python/bin/
                # We need to add /opt/python to PYTHONPATH so imports work
                # Then execute the entry point function
                
                # Create MCP client with stdio transport
                # Set PYTHONPATH to include /opt/python so the modules can be found
                import os
                env = os.environ.copy()
                python_path = env.get('PYTHONPATH', '')
                if python_path:
                    env['PYTHONPATH'] = f"/opt/python:{python_path}"
                else:
                    env['PYTHONPATH'] = "/opt/python"
                
                mcp_client = MCPClient(
                    lambda mod=module_path, fn=func_name, environment=env: stdio_client(
                        StdioServerParameters(
                            command=sys.executable,
                            args=["-c", f"from {mod} import {fn}; {fn}()"],
                            env=environment
                        )
                    ),
                    prefix=package_name.replace('-', '_')  # Prefix tools to avoid conflicts
                )
                
                # Connect to server and retrieve tools
                with mcp_client:
                    server_tools = mcp_client.list_tools_sync()
                    tools.extend(server_tools)
                    logger.info(f"Loaded {len(server_tools)} tools from {server_id}")
                
                logger.info(f"MCP server {server_id} loaded successfully")
                
            except Exception as e:
                logger.warning(f"Failed to load MCP server {server_id}: {e}")
                import traceback
                logger.debug(f"Traceback: {traceback.format_exc()}")
                continue
        
        logger.info(f"Loaded {len(tools)} tools from {len(servers)} MCP servers")
        
    except Exception as e:
        logger.error(f"Error loading MCP servers: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
    
    return tools