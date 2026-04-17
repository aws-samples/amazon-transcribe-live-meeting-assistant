"""
MCP API Key Manager Lambda
Handles generate/list/revoke of per-user MCP API keys via AppSync.
"""

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))

dynamodb = boto3.resource('dynamodb')
TABLE_NAME = os.environ.get('MCP_API_KEYS_TABLE', '')


def handler(event, context):
    field = event.get('info', {}).get('fieldName', '')
    identity = event.get('identity', {})
    username = identity.get('username', '')
    groups = identity.get('groups') or []
    is_admin = 'Admin' in groups

    logger.info(f"API Key Manager - field={field}, user={username}")

    if not username:
        return error('Not authenticated')

    if field == 'generateMCPApiKey':
        return generate_key(username, is_admin)
    elif field == 'revokeMCPApiKey':
        prefix = event.get('arguments', {}).get('keyPrefix', '')
        return revoke_key(username, prefix)
    elif field == 'listMCPApiKeys':
        return list_keys(username)
    else:
        return error(f'Unknown operation: {field}')


def generate_key(username, is_admin):
    table = dynamodb.Table(TABLE_NAME)

    # Check if user already has a key
    existing = table.query(
        IndexName='UserIdIndex',
        KeyConditionExpression=Key('UserId').eq(username),
    ).get('Items', [])

    if existing:
        return error('You already have an API key. Revoke it first to generate a new one.')

    # Generate key
    raw_key = f"lma_{uuid.uuid4()}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    prefix = raw_key[:12]
    now = datetime.now(timezone.utc).isoformat()

    table.put_item(Item={
        'KeyHash': key_hash,
        'UserId': username,
        'Username': username,
        'IsAdmin': 'true' if is_admin else 'false',
        'KeyPrefix': prefix,
        'CreatedAt': now,
        'Enabled': 'true',
    })

    logger.info(f"Generated API key for {username}, prefix={prefix}")

    return {
        'keyValue': raw_key,
        'keyPrefix': prefix,
        'createdAt': now,
    }


def revoke_key(username, prefix):
    table = dynamodb.Table(TABLE_NAME)

    # Find user's key matching prefix
    items = table.query(
        IndexName='UserIdIndex',
        KeyConditionExpression=Key('UserId').eq(username),
    ).get('Items', [])

    target = next((i for i in items if i.get('KeyPrefix') == prefix), None)
    if not target:
        return error('Key not found')

    table.delete_item(Key={'KeyHash': target['KeyHash']})
    logger.info(f"Revoked API key for {username}, prefix={prefix}")
    return True


def list_keys(username):
    table = dynamodb.Table(TABLE_NAME)
    items = table.query(
        IndexName='UserIdIndex',
        KeyConditionExpression=Key('UserId').eq(username),
    ).get('Items', [])

    return [{
        'keyPrefix': i.get('KeyPrefix', ''),
        'createdAt': i.get('CreatedAt', ''),
        'enabled': i.get('Enabled') == 'true',
    } for i in items]


def error(msg):
    raise Exception(msg)
