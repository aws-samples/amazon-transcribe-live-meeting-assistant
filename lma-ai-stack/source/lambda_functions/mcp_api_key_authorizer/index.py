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


def request_summary(event):
    """The fields of a request that are logged: never its headers or token."""
    request_context = event.get("requestContext") or {}
    return {
        "requestId": request_context.get("requestId", ""),
        "httpMethod": event.get("httpMethod", ""),
        "resource": event.get("resource") or event.get("path", ""),
    }


def extract_token(event):
    """The API key from authorizationToken, x-api-key or Authorization: Bearer."""
    token = event.get("authorizationToken", "")
    if token:
        return token
    headers = event.get("headers") or {}
    token = headers.get("x-api-key", "") or headers.get("X-Api-Key", "")
    if token:
        return token
    auth = headers.get("Authorization", "") or headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""


def handler(event, context):
    summary = request_summary(event)
    method_arn = event.get("methodArn", "")
    token = extract_token(event)

    if not token or not TABLE_NAME:
        logger.info("Request without an API key: %s", json.dumps(summary))
        return deny_policy(method_arn)

    # Nothing derived from the presented key is logged. A matched key is named
    # by the KeyPrefix stored with it, as the key manager and key list name it.
    key_hash = hashlib.sha256(token.encode()).hexdigest()

    try:
        table = dynamodb.Table(TABLE_NAME)
        resp = table.get_item(Key={"KeyHash": key_hash})
        item = resp.get("Item")

        if not item or item.get("Enabled") != "true":
            logger.info("Key not found or disabled: %s", json.dumps(summary))
            return deny_policy(method_arn)

        logger.info(
            "Authorized user: %s, keyPrefix=%s: %s",
            item.get("Username"),
            item.get("KeyPrefix", ""),
            json.dumps(summary),
        )
        return allow_policy(
            method_arn,
            {
                "userId": item.get("UserId", ""),
                "username": item.get("Username", ""),
                "isAdmin": item.get("IsAdmin", "false"),
            },
        )

    except Exception as e:  # pylint: disable=broad-except
        # The exception type only: its message is not logged.
        logger.error("Auth error (%s): %s", type(e).__name__, json.dumps(summary))
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
