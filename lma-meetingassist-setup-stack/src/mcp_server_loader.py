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
import time
import base64
import json
import requests

logger = logging.getLogger(__name__)

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')
kms = boto3.client('kms')

# Environment variables
MCP_SERVERS_TABLE = os.environ.get('MCP_SERVERS_TABLE', '')
AWS_ACCOUNT_ID = os.environ.get('AWS_ACCOUNT_ID', '')
KMS_KEY_ID = os.environ.get('KMS_KEY_ID', '')


def decrypt_token(encrypted_token: str) -> str:
    """Decrypt token using KMS"""
    try:
        decrypted = kms.decrypt(
            CiphertextBlob=base64.b64decode(encrypted_token),
            KeyId=KMS_KEY_ID
        )
        return decrypted['Plaintext'].decode('utf-8')
    except Exception as e:
        logger.error(f"Token decryption failed: {e}")
        raise


def encrypt_token(token: str) -> str:
    """Encrypt token using KMS"""
    try:
        encrypted = kms.encrypt(
            KeyId=KMS_KEY_ID,
            Plaintext=token.encode('utf-8')
        )
        return base64.b64encode(encrypted['CiphertextBlob']).decode('utf-8')
    except Exception as e:
        logger.error(f"Token encryption failed: {e}")
        raise


def get_client_credentials_token(oauth_config: dict) -> str:
    """
    Get OAuth token using client credentials grant
    
    Args:
        oauth_config: OAuth configuration with clientId, clientSecret, tokenUrl
        
    Returns:
        Access token
    """
    try:
        logger.info("Getting token with client credentials")
        
        # Decrypt client secret
        client_secret = decrypt_token(oauth_config['clientSecret'])
        
        # Request token with client credentials
        token_data = {
            'grant_type': 'client_credentials',
            'client_id': oauth_config['clientId'],
            'client_secret': client_secret,
        }
        
        # Add scopes if provided
        if oauth_config.get('scopes'):
            token_data['scope'] = ' '.join(oauth_config['scopes'])
        
        token_response = requests.post(
            oauth_config['tokenUrl'],
            data=token_data,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30
        )
        
        if token_response.status_code != 200:
            raise Exception(f"Token request failed: {token_response.text}")
        
        tokens = token_response.json()
        return tokens['access_token']
        
    except Exception as e:
        logger.error(f"Client credentials token request failed: {e}")
        raise


def refresh_oauth_token_inline(server_id: str, account_id: str, oauth_config: dict) -> str:
    """
    Refresh OAuth token inline (just-in-time refresh)
    
    Args:
        server_id: MCP server identifier
        account_id: AWS account ID
        oauth_config: OAuth configuration from AuthConfig
        
    Returns:
        New decrypted access token
    """
    try:
        logger.info(f"Refreshing OAuth token for {server_id}")
        
        # Check grant type
        grant_type = oauth_config.get('grantType', 'authorization_code')
        
        if grant_type == 'client_credentials':
            # For client credentials, just get a new token
            return get_client_credentials_token(oauth_config)
        
        # For authorization code flow, use refresh token
        # Decrypt refresh token
        refresh_token = decrypt_token(oauth_config['refreshToken'])
        
        # Request new tokens from OAuth provider
        token_response = requests.post(
            oauth_config['tokenUrl'],
            data={
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token,
                'client_id': oauth_config['clientId'],
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=30
        )
        
        if token_response.status_code != 200:
            raise Exception(f"Token refresh failed: {token_response.text}")
        
        tokens = token_response.json()
        
        # Encrypt new tokens
        encrypted_access = encrypt_token(tokens['access_token'])
        # Some providers return new refresh token, others reuse existing
        encrypted_refresh = encrypt_token(
            tokens.get('refresh_token', refresh_token)
        )
        
        # Calculate new expiration
        expires_at = int(time.time()) + tokens.get('expires_in', 3600)
        
        # Update DynamoDB
        table = dynamodb.Table(MCP_SERVERS_TABLE)
        
        # Get current AuthConfig
        response = table.get_item(
            Key={'AccountId': account_id, 'ServerId': server_id}
        )
        
        if 'Item' in response:
            server = response['Item']
            auth_config_str = server.get('AuthConfig', '{}')
            auth_data = json.loads(auth_config_str) if isinstance(auth_config_str, str) else auth_config_str
            
            # Update OAuth tokens
            if 'oauth' not in auth_data:
                auth_data['oauth'] = {}
            
            auth_data['oauth']['accessToken'] = encrypted_access
            auth_data['oauth']['refreshToken'] = encrypted_refresh
            auth_data['oauth']['expiresAt'] = expires_at
            auth_data['oauth']['lastRefreshed'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            
            # Save back to DynamoDB
            table.update_item(
                Key={'AccountId': account_id, 'ServerId': server_id},
                UpdateExpression='SET AuthConfig = :config, UpdatedAt = :updated',
                ExpressionAttributeValues={
                    ':config': json.dumps(auth_data),
                    ':updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                }
            )
        
        logger.info(f"Token refreshed for {server_id}, expires at {expires_at}")
        return tokens['access_token']
        
    except Exception as e:
        logger.error(f"Token refresh failed for {server_id}: {e}")
        raise


def get_valid_oauth_token(server_id: str, account_id: str, oauth_config: dict) -> str:
    """
    Get valid OAuth access token with just-in-time refresh
    
    This function checks if the token is expired or expiring soon (< 5 minutes)
    and refreshes it if necessary.
    
    Args:
        server_id: MCP server identifier
        account_id: AWS account ID
        oauth_config: OAuth configuration from AuthConfig
        
    Returns:
        Valid decrypted access token
    """
    try:
        grant_type = oauth_config.get('grantType', 'authorization_code')
        
        # For client credentials, check if we have a token
        if grant_type == 'client_credentials':
            # Check if we have a cached token
            if 'accessToken' in oauth_config:
                access_token = decrypt_token(oauth_config['accessToken'])
                expires_at = oauth_config.get('expiresAt', 0)
                time_until_expiry = expires_at - time.time()
                
                if time_until_expiry > 300:  # More than 5 minutes
                    logger.info(f"Using cached client credentials token for {server_id}")
                    return access_token
            
            # Get new token with client credentials
            logger.info(f"Getting new client credentials token for {server_id}")
            return get_client_credentials_token(oauth_config)
        
        # For authorization code flow, decrypt and check expiration
        access_token = decrypt_token(oauth_config['accessToken'])
        
        # Check expiration (refresh if < 5 minutes remaining)
        expires_at = oauth_config.get('expiresAt', 0)
        time_until_expiry = expires_at - time.time()
        
        if time_until_expiry < 300:  # Less than 5 minutes
            logger.info(f"Token expiring in {int(time_until_expiry)}s for {server_id}, refreshing...")
            access_token = refresh_oauth_token_inline(server_id, account_id, oauth_config)
        else:
            logger.info(f"Token valid for {int(time_until_expiry)}s for {server_id}")
        
        return access_token
        
    except Exception as e:
        logger.error(f"Failed to get valid OAuth token for {server_id}: {e}")
        raise


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
                            # OAuth 2.1 - get valid access token with just-in-time refresh
                            oauth_data = auth_data.get('oauth', {})
                            try:
                                # This handles token expiration check and refresh automatically
                                access_token = get_valid_oauth_token(server_id, AWS_ACCOUNT_ID, oauth_data)
                                http_auth_config['headers']['Authorization'] = f"Bearer {access_token}"
                                logger.info(f"Applied OAuth 2.1 access token for {server_id}")
                            except Exception as e:
                                logger.error(f"Failed to get OAuth token for {server_id}: {e}")
                                # Skip this server but continue loading others
                                continue
                        
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
            try:
                logger.info(f"Loading PyPI MCP server: {server_id} ({package_name})")
                
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
                        
                        elif auth_type == 'env_vars':
                            # Environment variables for PyPI servers (new standard)
                            env_vars = auth_data.get('env', {})
                            if env_vars and isinstance(env_vars, dict):
                                env.update(env_vars)
                                logger.info(f"Set environment variables for {server_id}: {list(env_vars.keys())}")
                            else:
                                logger.warning(f"env_vars auth configured but no 'env' field found for {server_id}")
                        
                        elif auth_type == 'custom_headers':
                            # Backward compatibility: check both 'env' and 'headers' fields
                            # This handles old installations that used 'custom_headers' for PyPI servers
                            env_vars = auth_data.get('env', {}) or auth_data.get('headers', {})
                            if env_vars and isinstance(env_vars, dict):
                                env.update(env_vars)
                                logger.info(f"Set environment variables for {server_id} (legacy custom_headers): {list(env_vars.keys())}")
                            else:
                                logger.warning(f"custom_headers auth configured but no env vars found for {server_id}")
                    
                    except Exception as e:
                        logger.warning(f"Could not parse auth config for PyPI server {server_id}: {e}")
                
                # Try to find the console script entry point
                entry_point = discover_console_script_path(package_name)
                
                if entry_point:
                    module_path, func_name = entry_point
                    logger.info(f"Using console script entry point: {module_path}:{func_name}")
                    
                    # Check if console script file exists in /opt/python/bin/
                    script_name = None
                    try:
                        dist = importlib.metadata.distribution(package_name)
                        for ep in dist.entry_points:
                            if ep.group == 'console_scripts':
                                script_name = ep.name
                                break
                    except:
                        pass
                    
                    if script_name:
                        script_path = f"/opt/python/bin/{script_name}"
                        # Check if script actually exists
                        if os.path.exists(script_path):
                            logger.info(f"Using console script: {script_path}")
                            mcp_client = MCPClient(
                                lambda script=script_path, environment=env: stdio_client(
                                    StdioServerParameters(
                                        command=sys.executable,
                                        args=[script],
                                        env=environment
                                    )
                                ),
                                prefix=package_name.replace('-', '_')
                            )
                        else:
                            # Script doesn't exist, use module execution
                            logger.info(f"Console script {script_path} not found, using module execution")
                            mcp_client = MCPClient(
                                lambda mod=module_path, fn=func_name, environment=env: stdio_client(
                                    StdioServerParameters(
                                        command=sys.executable,
                                        args=["-c", f"from {mod} import {fn}; import asyncio; asyncio.run({fn}())"],
                                        env=environment
                                    )
                                ),
                                prefix=package_name.replace('-', '_')
                            )
                    else:
                        # No script name, use module execution
                        logger.info(f"No console script name found, using module execution: {module_path}:{func_name}")
                        mcp_client = MCPClient(
                            lambda mod=module_path, fn=func_name, environment=env: stdio_client(
                                StdioServerParameters(
                                    command=sys.executable,
                                    args=["-c", f"from {mod} import {fn}; import asyncio; asyncio.run({fn}())"],
                                    env=environment
                                )
                            ),
                            prefix=package_name.replace('-', '_')
                        )
                else:
                    # No entry point found, try running as module with __main__
                    logger.info(f"No entry point found, trying python -m {package_name.replace('-', '_')}")
                    mcp_client = MCPClient(
                        lambda pkg=package_name.replace('-', '_'), environment=env: stdio_client(
                            StdioServerParameters(
                                command=sys.executable,
                                args=["-m", pkg],
                                env=environment
                            )
                        ),
                        prefix=package_name.replace('-', '_')
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