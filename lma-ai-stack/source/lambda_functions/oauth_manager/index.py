#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
OAuth Manager Lambda Function
Handles OAuth 2.1 initialization and callback for MCP servers
Supports PKCE with automatic fallback to OAuth 2.0
"""

import json
import os
import boto3
import base64
import time
import logging
from datetime import datetime
from typing import Dict, Any
import requests
from urllib.parse import urlencode, quote

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')
kms = boto3.client('kms')

# Environment variables
OAUTH_STATE_TABLE = os.environ.get('OAUTH_STATE_TABLE', '')
MCP_SERVERS_TABLE = os.environ.get('MCP_SERVERS_TABLE', '')
KMS_KEY_ID = os.environ.get('KMS_KEY_ID', '')
OAUTH_CALLBACK_URL = os.environ.get('OAUTH_CALLBACK_URL', '')
ACCOUNT_ID = os.environ.get('AWS_ACCOUNT_ID', '')


def encrypt_token(token: str) -> str:
    """Encrypt token using KMS"""
    try:
        response = kms.encrypt(
            KeyId=KMS_KEY_ID,
            Plaintext=token.encode('utf-8')
        )
        return base64.b64encode(response['CiphertextBlob']).decode('utf-8')
    except Exception as e:
        logger.error(f"Token encryption failed: {e}")
        raise


def init_oauth_flow(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Initialize OAuth 2.1 flow with PKCE
    
    Args:
        event: GraphQL resolver event with input containing:
            - serverId: MCP server identifier
            - provider: OAuth provider name
            - clientId: OAuth client ID
            - authorizationUrl: OAuth authorization endpoint
            - tokenUrl: OAuth token endpoint
            - scopes: List of OAuth scopes
            - codeChallenge: PKCE code challenge from frontend
            
    Returns:
        {
            'authorizationUrl': str,
            'state': str
        }
    """
    try:
        logger.info(f"Init OAuth flow request: {json.dumps(event)}")
        
        # Extract input from GraphQL resolver event
        input_data = event.get('arguments', {}).get('input', {})
        
        server_id = input_data.get('serverId')
        provider = input_data.get('provider')
        client_id = input_data.get('clientId')
        authorization_url = input_data.get('authorizationUrl')
        token_url = input_data.get('tokenUrl')
        scopes = input_data.get('scopes', [])
        code_challenge = input_data.get('codeChallenge')
        
        if not all([server_id, client_id, authorization_url, token_url]):
            return {
                'success': False,
                'error': 'Missing required fields'
            }
        
        # Generate state for CSRF protection
        import uuid
        state = str(uuid.uuid4())
        
        # Store state in DynamoDB with TTL (10 minutes)
        state_table = dynamodb.Table(OAUTH_STATE_TABLE)
        state_table.put_item(Item={
            'State': state,
            'ServerId': server_id,
            'AccountId': ACCOUNT_ID,
            'Provider': provider,
            'ClientId': client_id,
            'TokenUrl': token_url,
            'CodeChallenge': code_challenge,
            'CreatedAt': datetime.utcnow().isoformat(),
            'ExpiresAt': int(time.time()) + 600  # 10 minutes TTL
        })
        
        # Build authorization URL with PKCE
        params = {
            'response_type': 'code',
            'client_id': client_id,
            'redirect_uri': OAUTH_CALLBACK_URL,
            'state': state,
            'scope': ' '.join(scopes) if scopes else '',
        }
        
        # Add PKCE parameters (OAuth 2.1)
        if code_challenge:
            params['code_challenge'] = code_challenge
            params['code_challenge_method'] = 'S256'
        
        # Build URL
        query_string = urlencode(params)
        full_auth_url = f"{authorization_url}?{query_string}"
        
        logger.info(f"OAuth flow initialized for server {server_id}")
        
        return {
            'authorizationUrl': full_auth_url,
            'state': state
        }
        
    except Exception as e:
        logger.error(f"Error initializing OAuth flow: {str(e)}")
        return {
            'success': False,
            'error': str(e)
        }


def handle_oauth_callback(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Handle OAuth callback and exchange code for tokens
    
    Args:
        event: GraphQL resolver event with input containing:
            - code: Authorization code from provider
            - state: State parameter for CSRF protection
            - codeVerifier: PKCE code verifier from frontend
            
    Returns:
        {
            'success': bool,
            'serverId': str
        }
    """
    try:
        logger.info(f"OAuth callback request")
        
        # Extract input
        input_data = event.get('arguments', {}).get('input', {})
        
        code = input_data.get('code')
        state = input_data.get('state')
        code_verifier = input_data.get('codeVerifier')
        
        if not all([code, state]):
            return {
                'success': False,
                'error': 'Missing required fields'
            }
        
        # Verify state and get stored data
        state_table = dynamodb.Table(OAUTH_STATE_TABLE)
        response = state_table.get_item(Key={'State': state})
        
        if 'Item' not in response:
            return {
                'success': False,
                'error': 'Invalid or expired state'
            }
        
        state_data = response['Item']
        server_id = state_data['ServerId']
        account_id = state_data['AccountId']
        client_id = state_data['ClientId']
        token_url = state_data['TokenUrl']
        code_challenge = state_data.get('CodeChallenge')
        
        # Exchange code for tokens
        token_data = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': OAUTH_CALLBACK_URL,
            'client_id': client_id,
        }
        
        # Add PKCE code_verifier if we used code_challenge
        if code_challenge and code_verifier:
            token_data['code_verifier'] = code_verifier
        
        # Request tokens
        try:
            token_response = requests.post(
                token_url,
                data=token_data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
            )
            
            if token_response.status_code != 200:
                error_msg = token_response.text
                logger.error(f"Token exchange failed: {error_msg}")
                
                # Check if PKCE not supported, retry without it
                if code_verifier and 'invalid_request' in error_msg.lower():
                    logger.info("PKCE not supported, retrying without code_verifier")
                    del token_data['code_verifier']
                    token_response = requests.post(
                        token_url,
                        data=token_data,
                        headers={'Content-Type': 'application/x-www-form-urlencoded'}
                    )
                    
                    if token_response.status_code != 200:
                        return {
                            'success': False,
                            'error': f'Token exchange failed: {token_response.text}'
                        }
                else:
                    return {
                        'success': False,
                        'error': f'Token exchange failed: {error_msg}'
                    }
            
            tokens = token_response.json()
            
        except Exception as e:
            logger.error(f"Token request failed: {e}")
            return {
                'success': False,
                'error': f'Token request failed: {str(e)}'
            }
        
        # Encrypt tokens
        encrypted_access = encrypt_token(tokens['access_token'])
        encrypted_refresh = encrypt_token(tokens.get('refresh_token', ''))
        
        # Calculate expiration
        expires_at = int(time.time()) + tokens.get('expires_in', 3600)
        
        # Get current server config
        servers_table = dynamodb.Table(MCP_SERVERS_TABLE)
        server_response = servers_table.get_item(
            Key={'AccountId': account_id, 'ServerId': server_id}
        )
        
        if 'Item' not in server_response:
            return {
                'success': False,
                'error': 'Server not found'
            }
        
        server = server_response['Item']
        auth_config = json.loads(server.get('AuthConfig', '{}')) if isinstance(server.get('AuthConfig'), str) else server.get('AuthConfig', {})
        
        # Update with OAuth tokens
        if 'oauth' not in auth_config:
            auth_config['oauth'] = {}
        
        auth_config['authType'] = 'oauth2'
        auth_config['oauth'].update({
            'provider': state_data['Provider'],
            'clientId': client_id,
            'tokenUrl': token_url,
            'accessToken': encrypted_access,
            'refreshToken': encrypted_refresh,
            'expiresAt': expires_at,
            'tokenType': tokens.get('token_type', 'Bearer'),
            'lastRefreshed': datetime.utcnow().isoformat(),
        })
        
        # Update server in DynamoDB
        servers_table.update_item(
            Key={'AccountId': account_id, 'ServerId': server_id},
            UpdateExpression='SET AuthConfig = :config, UpdatedAt = :updated',
            ExpressionAttributeValues={
                ':config': json.dumps(auth_config),
                ':updated': datetime.utcnow().isoformat()
            }
        )
        
        # Clean up state
        state_table.delete_item(Key={'State': state})
        
        logger.info(f"OAuth flow completed for server {server_id}")
        
        return {
            'success': True,
            'serverId': server_id
        }
        
    except Exception as e:
        logger.error(f"Error handling OAuth callback: {str(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return {
            'success': False,
            'error': str(e)
        }


def handler(event: Dict[str, Any], context: Any) -> Any:
    """
    Main Lambda handler - routes to appropriate function based on field name
    """
    field_name = event.get('info', {}).get('fieldName', '')
    
    logger.info(f"OAuth Manager - Field: {field_name}")
    
    if field_name == 'initOAuthFlow':
        return init_oauth_flow(event, context)
    elif field_name == 'handleOAuthCallback':
        return handle_oauth_callback(event, context)
    else:
        logger.error(f"Unknown field name: {field_name}")
        return {
            'success': False,
            'error': f'Unknown operation: {field_name}'
        }