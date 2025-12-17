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


def load_http_mcp_server(server_id: str, server_url: str, auth_config: dict = None):
    """
    Load an HTTP-based MCP server (streamable-http transport)
    
    HTTP servers don't require installation - they're external services.
    We just need to connect to them via HTTP transport.
    
    Args:
        server_id: Server identifier
        server_url: HTTP endpoint URL
        auth_config: Optional authentication configuration with headers
        
    Returns:
        MCPClient object (not tools) for managed integration
    """
    try:
        from strands.tools.mcp import MCPClient
        from mcp.client.streamable_http import streamablehttp_client
        
        logger.info(f"Loading HTTP MCP server: {server_id} at {server_url}")
        
        # Extract headers from auth config
        headers = auth_config.get('headers', {}) if auth_config else {}
        
        if headers:
            logger.info(f"Using auth headers: {list(headers.keys())}")
        
        # Create MCP client with HTTP transport
        # Note: streamablehttp_client only supports url and headers, not params
        mcp_client = MCPClient(
            lambda: streamablehttp_client(
                url=server_url,
                headers=headers
            ),
            prefix=server_id.replace('/', '_').replace('.', '_')
        )
        
        logger.info(f"Created HTTP MCP client for {server_id}")
        return mcp_client
            
    except Exception as e:
        logger.warning(f"Failed to load HTTP MCP server {server_id}: {e}")
        import traceback
        logger.debug(f"Traceback: {traceback.format_exc()}")
        return None


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
    Load MCP server clients for the current AWS account using Strands MCPClient
    
    This function:
    1. Queries DynamoDB for ACTIVE MCP servers
    2. Dynamically discovers each server's Python module entry point
    3. Spawns each server as subprocess with stdio transport
    4. Creates Strands MCPClient instances
    5. Returns list of MCPClient objects for managed integration with Agent
    
    Returns:
        List of MCPClient objects (not tools) for use with Agent's managed integration
    """
    mcp_clients = []
    
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
                
                # Parse auth config and apply headers generically (MCP spec compliant)
                http_auth_config = {'headers': {}}
                
                if auth_config:
                    try:
                        import json
                        auth_data = json.loads(auth_config) if isinstance(auth_config, str) else auth_config
                        auth_type = auth_data.get('authType', 'bearer')  # Default to bearer
                        
                        logger.info(f"Auth config for {server_id}: authType={auth_type}")
                        
                        # Generic auth handling per MCP spec - no hardcoded server names
                        if auth_type == 'bearer':
                            # Bearer token authentication (RFC 6750)
                            token = auth_data.get('token', '')
                            if token:
                                http_auth_config['headers']['Authorization'] = f"Bearer {token}"
                                logger.info(f"Applied Bearer token authentication for {server_id}")
                            else:
                                logger.warning(f"Bearer auth configured but no token found for {server_id}")
                        
                        elif auth_type == 'custom_headers':
                            # Custom headers - user provides complete header dict
                            custom_headers = auth_data.get('headers', {})
                            if custom_headers and isinstance(custom_headers, dict):
                                http_auth_config['headers'].update(custom_headers)
                                logger.info(f"Applied custom headers for {server_id}: {list(custom_headers.keys())}")
                            else:
                                logger.warning(f"Custom headers auth configured but no valid headers found for {server_id}")
                        
                        elif auth_type == 'oauth2':
                            # OAuth 2.1 - use access token from stored credentials
                            oauth_data = auth_data.get('oauth', {})
                            access_token = oauth_data.get('accessToken', '')
                            if access_token:
                                http_auth_config['headers']['Authorization'] = f"Bearer {access_token}"
                                logger.info(f"Applied OAuth 2.1 access token for {server_id}")
                                
                                # TODO: Check if token is expired and refresh if needed
                                # expires_at = oauth_data.get('expiresAt', 0)
                                # if time.time() > expires_at:
                                #     access_token = refresh_oauth_token(oauth_data)
                            else:
                                logger.warning(f"OAuth auth configured but no access token found for {server_id}")
                        
                        else:
                            logger.warning(f"Unknown auth type '{auth_type}' for {server_id}")
                    
                    except Exception as e:
                        logger.warning(f"Could not parse auth config for {server_id}: {e}")
                        import traceback
                        logger.debug(f"Traceback: {traceback.format_exc()}")
                else:
                    logger.info(f"No auth config found for HTTP server {server_id}")
                
                # Load HTTP server - returns MCPClient object
                http_client = load_http_mcp_server(server_id, server_url, http_auth_config)
                if http_client:
                    mcp_clients.append(http_client)
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
                
                # Add environment variables from auth config for PyPI servers
                # PyPI servers use env vars, not HTTP headers
                if auth_config:
                    try:
                        import json
                        auth_data = json.loads(auth_config) if isinstance(auth_config, str) else auth_config
                        auth_type = auth_data.get('authType', 'bearer')
                        
                        # For PyPI servers, environment variables are used instead of headers
                        if auth_type == 'bearer':
                            # Single token - use common env var name
                            token = auth_data.get('token', '')
                            if token:
                                env['MCP_API_KEY'] = token
                                logger.info(f"Set MCP_API_KEY environment variable for {server_id}")
                        
                        elif auth_type == 'custom_headers':
                            # For PyPI servers, "headers" actually means environment variables
                            env_vars = auth_data.get('headers', {})
                            if env_vars and isinstance(env_vars, dict):
                                env.update(env_vars)
                                logger.info(f"Set custom environment variables for {server_id}: {list(env_vars.keys())}")
                        
                        elif auth_type == 'env_vars':
                            # Explicit environment variables
                            env_vars = auth_data.get('env', {})
                            if env_vars and isinstance(env_vars, dict):
                                env.update(env_vars)
                                logger.info(f"Set environment variables for {server_id}: {list(env_vars.keys())}")
                    
                    except Exception as e:
                        logger.warning(f"Could not parse auth config for PyPI server {server_id}: {e}")
                
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
                
                # Store client for managed integration (don't extract tools here)
                mcp_clients.append(mcp_client)
                logger.info(f"Created MCP client for {server_id}")
                
            except Exception as e:
                logger.warning(f"Failed to create MCP client for {server_id}: {e}")
                import traceback
                logger.debug(f"Traceback: {traceback.format_exc()}")
                continue
        
        logger.info(f"Created {len(mcp_clients)} MCP clients from {len(servers)} MCP servers")
        
    except Exception as e:
        logger.error(f"Error creating MCP clients: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
    
    return mcp_clients