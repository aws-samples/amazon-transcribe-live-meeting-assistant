#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
MCP Server Loader for Python Lambda
Dynamically loads MCP server tools from installed PyPI packages using Strands MCPClient

This module connects to installed MCP servers via stdio transport and retrieves their tools
for use with Strands agents. Each MCP server runs as a subprocess during tool loading.

Caching Strategy:
- Module-level variables persist across warm Lambda invocations
- DynamoDB server configs are cached with a TTL to avoid re-querying every invocation
- MCPClient objects (with their background threads and connections) are cached and reused
- A config fingerprint detects when servers have been added/removed/changed, triggering rebuild
- MCPClient health is checked before reuse; unhealthy clients are recreated
"""

import os
import boto3
import hashlib
import logging
from typing import List, Optional, Dict, Any, Tuple
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

# ============================================================================
# Module-level cache (persists across warm Lambda invocations)
# ============================================================================
# Maps server_id -> MCPClient object for reuse
_cached_mcp_clients: Dict[str, Any] = {}
# The fingerprint of the last server config set (detects adds/removes/changes)
_cached_config_fingerprint: str = ''
# When the DynamoDB config was last fetched
_config_cache_timestamp: float = 0
# Cached server configs from DynamoDB
_cached_server_configs: List[Dict] = []
# How often to re-check DynamoDB for config changes (seconds)
_CONFIG_CACHE_TTL_SECONDS = 300  # 5 minutes
# Sentinel consumer ID to keep MCP clients alive across Agent lifecycles
# When added as a consumer, prevents Agent.__del__ -> remove_consumer -> stop()
_PERSISTENT_CONSUMER_ID = '__mcp_loader_persistent__'


def _compute_config_fingerprint(servers: List[Dict]) -> str:
    """
    Compute a fingerprint of the server configurations to detect changes.
    
    This allows us to detect when servers have been added, removed, or their
    configuration has changed (e.g., auth tokens rotated, URLs changed).
    
    Args:
        servers: List of server config dicts from DynamoDB
        
    Returns:
        SHA256 hex digest of the sorted server configs
    """
    # Build a stable representation: sort by ServerId, include key fields
    config_parts = []
    for server in sorted(servers, key=lambda s: s.get('ServerId', '')):
        parts = [
            server.get('ServerId', ''),
            server.get('NpmPackage', ''),
            server.get('PackageType', ''),
            server.get('ServerUrl', ''),
            server.get('Status', ''),
            # Include auth config hash (not the full config, for efficiency)
            str(server.get('AuthConfig', '')),
        ]
        config_parts.append('|'.join(parts))
    
    fingerprint_input = '\n'.join(config_parts)
    return hashlib.sha256(fingerprint_input.encode()).hexdigest()[:16]


def _is_mcp_client_healthy(mcp_client) -> bool:
    """
    Check if a cached MCPClient is still healthy and has an active session.
    
    MCPClient runs a background thread with an asyncio event loop.
    We need the session to be active (background thread alive, session initialized)
    for the client to be truly reusable without restart overhead.
    
    Args:
        mcp_client: An MCPClient instance
        
    Returns:
        True if the client has an active session, False if it needs restart
    """
    try:
        # Check if session is active (background thread alive + session initialized)
        if hasattr(mcp_client, '_is_session_active') and mcp_client._is_session_active():
            return True
        
        # Session not active - client was stopped or never started
        logger.debug("MCPClient session not active")
        return False
        
    except Exception as e:
        logger.warning(f"Error checking MCPClient health: {e}")
        return False


def _ensure_client_started(mcp_client, server_id: str) -> bool:
    """
    Ensure an MCPClient is started and has an active session.
    
    If the client was stopped (e.g., by Agent.__del__ -> remove_consumer -> stop()),
    this will restart it. Also adds the persistent consumer to prevent future stops.
    
    Args:
        mcp_client: An MCPClient instance
        server_id: Server identifier for logging
        
    Returns:
        True if client is now active, False if start failed
    """
    try:
        # Add persistent consumer first (prevents Agent.__del__ from stopping it)
        if hasattr(mcp_client, 'add_consumer'):
            mcp_client.add_consumer(_PERSISTENT_CONSUMER_ID)
        
        # Check if already active
        if hasattr(mcp_client, '_is_session_active') and mcp_client._is_session_active():
            logger.debug(f"MCPClient {server_id} already active")
            return True
        
        # Need to start it
        logger.info(f"Starting MCPClient {server_id}...")
        start_time = time.time()
        mcp_client.start()
        elapsed = time.time() - start_time
        logger.info(f"MCPClient {server_id} started in {elapsed:.2f}s")
        
        # Mark as started for the ToolProvider interface
        if hasattr(mcp_client, '_tool_provider_started'):
            mcp_client._tool_provider_started = True
        
        return True
        
    except Exception as e:
        logger.warning(f"Failed to start MCPClient {server_id}: {e}")
        return False


def _cleanup_cached_clients():
    """Stop and clean up all cached MCP clients."""
    global _cached_mcp_clients
    for server_id, client in _cached_mcp_clients.items():
        try:
            if hasattr(client, '_is_session_active') and client._is_session_active():
                client.stop(None, None, None)
                logger.info(f"Stopped cached MCP client: {server_id}")
        except Exception as e:
            logger.warning(f"Error stopping cached MCP client {server_id}: {e}")
    _cached_mcp_clients = {}


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
        
        # Get client secret
        client_secret = oauth_config.get('clientSecret', '')
        
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


def _prepare_auth_for_http_server(server_id: str, auth_config) -> Tuple[Dict, bool]:
    """
    Parse auth config and prepare HTTP headers for an HTTP MCP server.
    
    Args:
        server_id: Server identifier
        auth_config: Raw auth config from DynamoDB
        
    Returns:
        Tuple of (http_auth_config dict, should_skip bool)
        should_skip is True if auth setup failed and server should be skipped
    """
    http_auth_config = {'headers': {}}
    
    if not auth_config:
        logger.info(f"No auth config found for HTTP server {server_id}")
        return http_auth_config, False
    
    try:
        auth_data = json.loads(auth_config) if isinstance(auth_config, str) else auth_config
        auth_type = auth_data.get('authType', 'bearer')
        
        logger.info(f"Auth config for {server_id}: authType={auth_type}")
        
        if auth_type == 'bearer':
            token = auth_data.get('token', '')
            if token:
                http_auth_config['headers']['Authorization'] = f"Bearer {token}"
                logger.info(f"Applied Bearer token authentication for {server_id}")
            else:
                logger.warning(f"Bearer auth configured but no token found for {server_id}")
        
        elif auth_type == 'custom_headers':
            custom_headers = auth_data.get('headers', {})
            if custom_headers and isinstance(custom_headers, dict):
                http_auth_config['headers'].update(custom_headers)
                logger.info(f"Applied custom headers for {server_id}: {list(custom_headers.keys())}")
            else:
                logger.warning(f"Custom headers auth configured but no valid headers found for {server_id}")
        
        elif auth_type == 'oauth2':
            oauth_data = auth_data.get('oauth', {})
            try:
                access_token = get_valid_oauth_token(server_id, AWS_ACCOUNT_ID, oauth_data)
                http_auth_config['headers']['Authorization'] = f"Bearer {access_token}"
                logger.info(f"Applied OAuth 2.1 access token for {server_id}")
            except Exception as e:
                logger.error(f"Failed to get OAuth token for {server_id}: {e}")
                return http_auth_config, True  # Skip this server
        
        elif auth_type == 'oauth_client_credentials':
            oauth_data = auth_data.get('oauth', {})
            try:
                access_token = get_valid_oauth_token(server_id, AWS_ACCOUNT_ID, oauth_data)
                http_auth_config['headers']['Authorization'] = f"Bearer {access_token}"
                logger.info(f"Applied OAuth Client Credentials token for {server_id}")
            except Exception as e:
                logger.error(f"Failed to get OAuth Client Credentials token for {server_id}: {e}")
                return http_auth_config, True  # Skip this server
        
        else:
            logger.warning(f"Unknown auth type '{auth_type}' for {server_id}")
    
    except Exception as e:
        logger.warning(f"Could not parse auth config for {server_id}: {e}")
        import traceback
        logger.debug(f"Traceback: {traceback.format_exc()}")
    
    return http_auth_config, False


def _create_pypi_mcp_client(server_id: str, package_name: str, auth_config):
    """
    Create an MCPClient for a PyPI package-based MCP server.
    
    Args:
        server_id: Server identifier
        package_name: PyPI package name
        auth_config: Raw auth config from DynamoDB
        
    Returns:
        MCPClient object or None if creation failed
    """
    try:
        from strands.tools.mcp import MCPClient
        from mcp import stdio_client, StdioServerParameters
        
        logger.info(f"Loading PyPI MCP server: {server_id} ({package_name})")
        
        # Set PYTHONPATH to include /opt/python so the modules can be found
        env = os.environ.copy()
        python_path = env.get('PYTHONPATH', '')
        if python_path:
            env['PYTHONPATH'] = f"/opt/python:{python_path}"
        else:
            env['PYTHONPATH'] = "/opt/python"
        
        # Add environment variables from auth config for PyPI servers
        if auth_config:
            try:
                auth_data = json.loads(auth_config) if isinstance(auth_config, str) else auth_config
                auth_type = auth_data.get('authType', 'bearer')
                
                if auth_type == 'bearer':
                    token = auth_data.get('token', '')
                    if token:
                        env['MCP_API_KEY'] = token
                        logger.info(f"Set MCP_API_KEY environment variable for {server_id}")
                
                elif auth_type == 'env_vars':
                    env_vars = auth_data.get('env', {})
                    if env_vars and isinstance(env_vars, dict):
                        env.update(env_vars)
                        logger.info(f"Set environment variables for {server_id}: {list(env_vars.keys())}")
                    else:
                        logger.warning(f"env_vars auth configured but no 'env' field found for {server_id}")
                
                elif auth_type == 'custom_headers':
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
        
        logger.info(f"Created MCP client for {server_id}")
        return mcp_client
        
    except Exception as e:
        logger.warning(f"Failed to create MCP client for {server_id}: {e}")
        import traceback
        logger.debug(f"Traceback: {traceback.format_exc()}")
        return None


def _fetch_server_configs() -> List[Dict]:
    """
    Fetch active MCP server configurations from DynamoDB.
    Uses module-level cache with TTL to avoid querying on every invocation.
    
    Returns:
        List of server config dicts from DynamoDB
    """
    global _cached_server_configs, _config_cache_timestamp
    
    now = time.time()
    cache_age = now - _config_cache_timestamp
    
    if _cached_server_configs and cache_age < _CONFIG_CACHE_TTL_SECONDS:
        logger.info(f"Using cached server configs (age: {int(cache_age)}s, TTL: {_CONFIG_CACHE_TTL_SECONDS}s)")
        return _cached_server_configs
    
    logger.info(f"Fetching server configs from DynamoDB (cache {'expired' if _cached_server_configs else 'empty'})")
    
    table = dynamodb.Table(MCP_SERVERS_TABLE)
    
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
    
    _cached_server_configs = servers
    _config_cache_timestamp = now
    
    return servers


def load_account_mcp_servers() -> Tuple[List, bool]:
    """
    Load MCP server clients for the current AWS account using Strands MCPClient.
    
    Uses module-level caching to avoid recreating MCP clients on every Lambda invocation.
    On warm starts with unchanged configs, returns cached clients almost instantly.
    
    Caching behavior:
    - DynamoDB configs are cached with a 5-minute TTL
    - MCPClient objects are cached by server_id and reused across invocations
    - Config fingerprint detects adds/removes/changes, triggering selective rebuild
    - Unhealthy clients are automatically recreated
    
    Returns:
        Tuple of (List of MCPClient objects, bool indicating if cache was used)
        The bool is True if cached clients were returned (warm start), False if freshly created
    """
    global _cached_mcp_clients, _cached_config_fingerprint
    
    if not MCP_SERVERS_TABLE:
        logger.warning("MCP_SERVERS_TABLE not configured")
        return [], False
    
    if not AWS_ACCOUNT_ID:
        logger.warning("AWS_ACCOUNT_ID not configured")
        return [], False
    
    try:
        # Check if required dependencies are available
        try:
            from strands.tools.mcp import MCPClient
            from mcp import stdio_client, StdioServerParameters
        except ImportError as e:
            logger.warning(f"Strands MCP support not available: {e}")
            return [], False
        
        # Fetch server configs (uses cache with TTL)
        servers = _fetch_server_configs()
        
        if not servers:
            logger.info("No active MCP servers found")
            if _cached_mcp_clients:
                _cleanup_cached_clients()
            return [], False
        
        # Check if config has changed since last time
        new_fingerprint = _compute_config_fingerprint(servers)
        config_changed = (new_fingerprint != _cached_config_fingerprint)
        
        if not config_changed and _cached_mcp_clients:
            # Config unchanged - check which clients are still alive
            # Clients may have been stopped by Agent.__del__ -> remove_consumer -> stop()
            # but the persistent consumer should prevent that. If they're still healthy,
            # return immediately (near-zero latency).
            healthy_clients = {}
            needs_restart = {}
            dead_ids = []
            
            for server_id, client in _cached_mcp_clients.items():
                if _is_mcp_client_healthy(client):
                    healthy_clients[server_id] = client
                else:
                    # Client exists but session is not active - try to restart it
                    needs_restart[server_id] = client
            
            # Restart clients that were stopped (e.g., persistent consumer wasn't set)
            for server_id, client in needs_restart.items():
                if _ensure_client_started(client, server_id):
                    healthy_clients[server_id] = client
                else:
                    dead_ids.append(server_id)
                    logger.warning(f"Cached MCP client {server_id} could not be restarted, will recreate")
            
            # Recreate any truly dead clients
            if dead_ids:
                for server in servers:
                    sid = server.get('ServerId', 'unknown')
                    if sid in dead_ids:
                        client = _create_mcp_client_for_server(server)
                        if client and _ensure_client_started(client, sid):
                            healthy_clients[sid] = client
            
            _cached_mcp_clients = healthy_clients
            _cached_config_fingerprint = new_fingerprint
            
            restarted = len(needs_restart) - len(dead_ids)
            if restarted > 0 or dead_ids:
                logger.info(f"Returning {len(healthy_clients)} MCP clients ({restarted} restarted, {len(dead_ids)} recreated)")
            else:
                logger.info(f"Returning {len(healthy_clients)} cached MCP clients (all active, zero restart needed)")
            return list(healthy_clients.values()), True
        
        # Config changed or no cache - rebuild all clients
        if config_changed and _cached_mcp_clients:
            logger.info(f"MCP server config changed (fingerprint: {_cached_config_fingerprint} -> {new_fingerprint}), rebuilding clients")
            _cleanup_cached_clients()
        
        # Create new clients for all servers and pre-start them
        new_clients = {}
        total_start_time = time.time()
        for server in servers:
            server_id = server.get('ServerId', 'unknown')
            client = _create_mcp_client_for_server(server)
            if client:
                # Pre-start the client and add persistent consumer
                # This spawns the subprocess/HTTP connection NOW so the Agent
                # constructor doesn't have to wait for it later
                if _ensure_client_started(client, server_id):
                    new_clients[server_id] = client
                else:
                    logger.warning(f"Failed to pre-start MCP client {server_id}, skipping")
        
        total_elapsed = time.time() - total_start_time
        _cached_mcp_clients = new_clients
        _cached_config_fingerprint = new_fingerprint
        
        logger.info(f"Created and pre-started {len(new_clients)} MCP clients from {len(servers)} servers in {total_elapsed:.2f}s")
        return list(new_clients.values()), False
        
    except Exception as e:
        logger.error(f"Error loading MCP clients: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        
        # If we have cached clients, return them as fallback
        if _cached_mcp_clients:
            logger.info(f"Returning {len(_cached_mcp_clients)} cached MCP clients as fallback after error")
            return list(_cached_mcp_clients.values()), True
        
        return [], False


def _create_mcp_client_for_server(server: Dict) -> Optional[Any]:
    """
    Create an MCPClient for a single server configuration.
    
    Args:
        server: Server config dict from DynamoDB
        
    Returns:
        MCPClient object or None if creation failed
    """
    server_id = server.get('ServerId', 'unknown')
    package_name = server.get('NpmPackage', '')
    package_type = server.get('PackageType', 'pypi')
    server_url = server.get('ServerUrl')
    auth_config = server.get('AuthConfig')
    
    # Handle HTTP servers
    if package_type == 'streamable-http':
        logger.info(f"Loading HTTP MCP server: {server_id}")
        http_auth_config, should_skip = _prepare_auth_for_http_server(server_id, auth_config)
        if should_skip:
            return None
        return load_http_mcp_server(server_id, server_url, http_auth_config)
    
    # Handle PyPI package-based servers
    return _create_pypi_mcp_client(server_id, package_name, auth_config)
