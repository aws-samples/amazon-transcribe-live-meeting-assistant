"""
MCP API Key Authorizer Lambda
Validates per-user API keys for the REST API Gateway MCP endpoint.
Hashes incoming key, looks up in DynamoDB, returns IAM policy with user context.
"""

import hashlib
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")
TABLE_NAME = os.environ.get("MCP_API_KEYS_TABLE", "")


def handler(event, context):
    logger.info(f"Authorizer event: {json.dumps(event)}")
    method_arn = event.get("methodArn", "")

    # Support both x-api-key header and Authorization: Bearer <key>
    token = event.get("authorizationToken", "")
    if not token:
        headers = event.get("headers", {})
        token = headers.get("x-api-key", "") or headers.get("X-Api-Key", "")
        if not token:
            auth = headers.get("Authorization", "") or headers.get("authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]

    if not token or not TABLE_NAME:
        return deny_policy(method_arn)

    key_hash = hashlib.sha256(token.encode()).hexdigest()

    try:
        table = dynamodb.Table(TABLE_NAME)
        resp = table.get_item(Key={"KeyHash": key_hash})
        item = resp.get("Item")

        if not item or item.get("Enabled") != "true":
            logger.info("Key not found or disabled")
            return deny_policy(method_arn)

        logger.info(f"Authorized user: {item.get('Username')}")
        return allow_policy(
            method_arn,
            {
                "userId": item.get("UserId", ""),
                "username": item.get("Username", ""),
                "isAdmin": item.get("IsAdmin", "false"),
            },
        )

    except Exception as e:
        logger.error(f"Auth error: {e}")
        return deny_policy(method_arn)


def allow_policy(method_arn, context):
    arn_parts = method_arn.split(":")
    region = arn_parts[3]
    account_id = arn_parts[4]
    api_gw_arn = arn_parts[5].split("/")
    api_id = api_gw_arn[0]
    stage = api_gw_arn[1]
    resource_arn = f"arn:aws:execute-api:{region}:{account_id}:{api_id}/{stage}/*"

    return {
        "principalId": context["userId"],
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": "Allow",
                    "Resource": resource_arn,
                }
            ],
        },
        "context": context,
    }


def deny_policy(method_arn):
    return {
        "principalId": "unauthorized",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": "Deny",
                    "Resource": method_arn or "*",
                }
            ],
        },
    }
