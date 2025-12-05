#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
MCP Server Manager Lambda Function
Handles installation and management of MCP servers from the public registry
Account-level management (all users share installed servers)
"""

import json
import os
import boto3
from datetime import datetime
from typing import Dict, Any
import logging

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')
codebuild = boto3.client('codebuild')

# Environment variables
MCP_SERVERS_TABLE = os.environ.get('MCP_SERVERS_TABLE', '')
CODEBUILD_PROJECT = os.environ.get('CODEBUILD_PROJECT', '')
MAX_SERVERS_PER_ACCOUNT = 5
ACCOUNT_ID = os.environ.get('AWS_ACCOUNT_ID', '')


def install_mcp_server(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Install an MCP server from the public registry
    
    Args:
        event: GraphQL resolver event with input containing:
            - ServerId: Unique identifier for the server
            - Name: Display name
            - NpmPackage: NPM package name
            - Version: Package version
            - Transport: List of supported transports
            - RequiresAuth: Whether authentication is required
            - AuthConfig: Authentication configuration (optional)
    
    Returns:
        InstallMCPServerOutput with success status and build ID
    """
    try:
        logger.info(f"Install MCP server request: {json.dumps(event)}")
        
        # Extract input from GraphQL resolver event
        input_data = event.get('arguments', {}).get('input', {})
        
        # Use AWS Account ID for account-level management
        account_id = ACCOUNT_ID or os.environ.get('AWS_ACCOUNT_ID', 'unknown')
        
        server_id = input_data.get('ServerId')
        name = input_data.get('Name')
        npm_package = input_data.get('NpmPackage')
        version = input_data.get('Version', 'latest')
        transport = input_data.get('Transport', ['stdio'])
        requires_auth = input_data.get('RequiresAuth', False)
        auth_config = input_data.get('AuthConfig')
        
        if not all([server_id, name, npm_package]):
            return {
                'ServerId': server_id or 'unknown',
                'Success': False,
                'Message': 'Missing required fields: ServerId, Name, NpmPackage'
            }
        
        # Check if table is configured
        if not MCP_SERVERS_TABLE:
            return {
                'ServerId': server_id,
                'Success': False,
                'Message': 'MCP Servers table not configured'
            }
        
        table = dynamodb.Table(MCP_SERVERS_TABLE)
        
        # Check account's current server count
        response = table.query(
            KeyConditionExpression='AccountId = :accountId',
            ExpressionAttributeValues={':accountId': account_id}
        )
        
        current_servers = response.get('Items', [])
        if len(current_servers) >= MAX_SERVERS_PER_ACCOUNT:
            return {
                'ServerId': server_id,
                'Success': False,
                'Message': f'Maximum {MAX_SERVERS_PER_ACCOUNT} servers allowed per account. Please uninstall a server first.'
            }
        
        # Check if server already installed
        if any(s.get('ServerId') == server_id for s in current_servers):
            return {
                'ServerId': server_id,
                'Success': False,
                'Message': 'Server already installed'
            }
        
        # Store server configuration in DynamoDB
        now = datetime.utcnow().isoformat() + 'Z'
        item = {
            'AccountId': account_id,
            'ServerId': server_id,
            'Name': name,
            'NpmPackage': npm_package,
            'Version': version,
            'Transport': transport,
            'RequiresAuth': requires_auth,
            'Status': 'INSTALLING',
            'InstalledAt': now,
            'UpdatedAt': now
        }
        
        if auth_config:
            item['AuthConfig'] = auth_config
        
        table.put_item(Item=item)
        logger.info(f"Stored MCP server config: {server_id}")
        
        # Trigger CodeBuild to rebuild Strands Lambda layer
        build_id = None
        if CODEBUILD_PROJECT:
            try:
                build_response = codebuild.start_build(
                    projectName=CODEBUILD_PROJECT,
                    environmentVariablesOverride=[
                        {
                            'name': 'ACCOUNT_ID',
                            'value': account_id,
                            'type': 'PLAINTEXT'
                        },
                        {
                            'name': 'ACTION',
                            'value': 'INSTALL_MCP_SERVER',
                            'type': 'PLAINTEXT'
                        },
                        {
                            'name': 'SERVER_ID',
                            'value': server_id,
                            'type': 'PLAINTEXT'
                        }
                    ]
                )
                build_id = build_response['build']['id']
                logger.info(f"Started CodeBuild: {build_id}")
                
                # Update status to BUILDING
                table.update_item(
                    Key={'AccountId': account_id, 'ServerId': server_id},
                    UpdateExpression='SET #status = :status, BuildId = :buildId',
                    ExpressionAttributeNames={'#status': 'Status'},
                    ExpressionAttributeValues={
                        ':status': 'BUILDING',
                        ':buildId': build_id
                    }
                )
            except Exception as build_error:
                logger.error(f"Failed to start CodeBuild: {build_error}")
                # Update status to FAILED
                table.update_item(
                    Key={'AccountId': account_id, 'ServerId': server_id},
                    UpdateExpression='SET #status = :status, ErrorMessage = :error',
                    ExpressionAttributeNames={'#status': 'Status'},
                    ExpressionAttributeValues={
                        ':status': 'FAILED',
                        ':error': str(build_error)
                    }
                )
                return {
                    'ServerId': server_id,
                    'Success': False,
                    'Message': f'Failed to start build: {str(build_error)}'
                }
        
        return {
            'ServerId': server_id,
            'Success': True,
            'Message': 'Server installation started',
            'BuildId': build_id
        }
        
    except Exception as e:
        logger.error(f"Error installing MCP server: {str(e)}")
        return {
            'ServerId': input_data.get('ServerId', 'unknown'),
            'Success': False,
            'Message': f'Installation failed: {str(e)}'
        }


def uninstall_mcp_server(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Uninstall an MCP server
    
    Args:
        event: GraphQL resolver event with serverId argument
    
    Returns:
        UninstallMCPServerOutput with success status
    """
    try:
        logger.info(f"Uninstall MCP server request: {json.dumps(event)}")
        
        server_id = event.get('arguments', {}).get('serverId')
        
        # Use AWS Account ID
        account_id = ACCOUNT_ID or os.environ.get('AWS_ACCOUNT_ID', 'unknown')
        
        if not server_id:
            return {
                'ServerId': 'unknown',
                'Success': False,
                'Message': 'ServerId is required'
            }
        
        if not MCP_SERVERS_TABLE:
            return {
                'ServerId': server_id,
                'Success': False,
                'Message': 'MCP Servers table not configured'
            }
        
        table = dynamodb.Table(MCP_SERVERS_TABLE)
        
        # Delete server configuration
        table.delete_item(
            Key={
                'AccountId': account_id,
                'ServerId': server_id
            }
        )
        
        logger.info(f"Deleted MCP server: {server_id} from account {account_id}")
        
        # Trigger CodeBuild to rebuild Strands Lambda layer
        if CODEBUILD_PROJECT:
            try:
                codebuild.start_build(
                    projectName=CODEBUILD_PROJECT,
                    environmentVariablesOverride=[
                        {
                            'name': 'ACCOUNT_ID',
                            'value': account_id,
                            'type': 'PLAINTEXT'
                        },
                        {
                            'name': 'ACTION',
                            'value': 'UNINSTALL_MCP_SERVER',
                            'type': 'PLAINTEXT'
                        }
                    ]
                )
                logger.info("Started CodeBuild for uninstall")
            except Exception as build_error:
                logger.warning(f"Failed to start CodeBuild: {build_error}")
                # Continue anyway - server is deleted from DB
        
        return {
            'ServerId': server_id,
            'Success': True,
            'Message': 'Server uninstalled successfully'
        }
        
    except Exception as e:
        logger.error(f"Error uninstalling MCP server: {str(e)}")
        return {
            'ServerId': event.get('arguments', {}).get('serverId', 'unknown'),
            'Success': False,
            'Message': f'Uninstallation failed: {str(e)}'
        }


def list_installed_servers(event: Dict[str, Any], context: Any) -> list:
    """
    List all MCP servers installed by the current user
    
    Returns:
        List of MCPServer objects
    """
    try:
        logger.info("List installed MCP servers request")
        
        identity = event.get('identity', {})
        user_id = identity.get('username', 'unknown')
        
        if not MCP_SERVERS_TABLE:
            logger.warning("MCP Servers table not configured")
            return []
        
        table = dynamodb.Table(MCP_SERVERS_TABLE)
        
        # Query all servers for this user
        response = table.query(
            KeyConditionExpression='UserId = :userId',
            ExpressionAttributeValues={':userId': user_id}
        )
        
        servers = response.get('Items', [])
        logger.info(f"Found {len(servers)} installed servers for user {user_id}")
        
        return servers
        
    except Exception as e:
        logger.error(f"Error listing installed servers: {str(e)}")
        return []


def get_mcp_server(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Get details of a specific installed MCP server
    
    Returns:
        MCPServer object or None
    """
    try:
        server_id = event.get('arguments', {}).get('serverId')
        identity = event.get('identity', {})
        user_id = identity.get('username', 'unknown')
        
        if not server_id or not MCP_SERVERS_TABLE:
            return None
        
        table = dynamodb.Table(MCP_SERVERS_TABLE)
        
        response = table.get_item(
            Key={
                'UserId': user_id,
                'ServerId': server_id
            }
        )
        
        return response.get('Item')
        
    except Exception as e:
        logger.error(f"Error getting MCP server: {str(e)}")
        return None


def handler(event: Dict[str, Any], context: Any) -> Any:
    """
    Main Lambda handler - routes to appropriate function based on field name
    """
    field_name = event.get('info', {}).get('fieldName', '')
    
    logger.info(f"MCP Server Manager - Field: {field_name}")
    
    if field_name == 'installMCPServer':
        return install_mcp_server(event, context)
    elif field_name == 'uninstallMCPServer':
        return uninstall_mcp_server(event, context)
    elif field_name == 'listInstalledMCPServers':
        return list_installed_servers(event, context)
    elif field_name == 'getMCPServer':
        return get_mcp_server(event, context)
    else:
        logger.error(f"Unknown field name: {field_name}")
        return {
            'Success': False,
            'Message': f'Unknown operation: {field_name}'
        }