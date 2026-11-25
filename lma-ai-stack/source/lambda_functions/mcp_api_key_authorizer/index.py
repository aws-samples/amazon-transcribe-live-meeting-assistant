"""
MCP API Key Authorizer Lambda
Validates per-user API keys for the REST API Gateway MCP endpoint.
Hashes incoming key, looks up in DynamoDB, returns IAM policy with user context.

The context carries the key's scope, which the MCP server applies to each tool
call: "read" or "read-write" for a key generated with one, and "legacy" for a
key stored before keys had scopes (no Scope attribute), which keeps the tools
the server offered such keys.

The key owner's role is read from the user pool on every request, not from the
key record: a key is refused once its owner's role is not one the role matrix
lists for MCP access (appsync/role-matrix.json, the "mcp-access" capability), or
once the owner no longer exists, and the context's isAdmin follows the owner's
current groups. The authorizer's result is not cached (its TTL is 0), so a role
change applies to the next request.
"""

import hashlib
import json
import logging
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

dynamodb = boto3.resource("dynamodb")
TABLE_NAME = os.environ.get("MCP_API_KEYS_TABLE", "")
USER_POOL_ID = os.environ.get("USER_POOL_ID", "")
cognito = boto3.client(
    "cognito-idp", config=Config(retries={"mode": "adaptive", "max_attempts": 3})
)

ADMIN = "Admin"
PARTICIPANT = "Participant"
USER = "User"
# The roles of the "mcp-access" capability in appsync/role-matrix.json.
MCP_ROLES = (ADMIN, USER)

READ_WRITE = "read-write"
LEGACY = "legacy"


def key_scope(item):
    """A stored key's scope: legacy when it has none, and read for any value but read-write."""
    scope = item.get("Scope")
    if scope in (None, ""):
        return LEGACY
    return READ_WRITE if scope == READ_WRITE else "read"


def effective_role(groups):
    """A role from group names: Admin, then Participant, then User (role-matrix.json)."""
    if ADMIN in groups:
        return ADMIN
    if PARTICIPANT in groups:
        return PARTICIPANT
    return USER


def owner_role(username):
    """The key owner's role now, or None when the user pool no longer has them.

    Any other Cognito error propagates, and the handler denies the request.
    """
    groups = []
    kwargs = {"UserPoolId": USER_POOL_ID, "Username": username, "Limit": 60}
    while True:
        try:
            page = cognito.admin_list_groups_for_user(**kwargs)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "UserNotFoundException":
                return None
            raise
        groups.extend(group.get("GroupName") for group in page.get("Groups", []))
        if not page.get("NextToken"):
            return effective_role(groups)
        kwargs["NextToken"] = page["NextToken"]


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

    if not TABLE_NAME:
        logger.error("MCP_API_KEYS_TABLE is not set; denying: %s", json.dumps(summary))
        return deny_policy(method_arn)
    if not USER_POOL_ID:
        logger.error("USER_POOL_ID is not set; denying: %s", json.dumps(summary))
        return deny_policy(method_arn)

    if not token:
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

        owner = item.get("Username") or item.get("UserId", "")
        role = owner_role(owner)
        if role not in MCP_ROLES:
            logger.info(
                "Key owner %s has role %s, not admitted to MCP; keyPrefix=%s: %s",
                owner,
                role or "none (no such user)",
                item.get("KeyPrefix", ""),
                json.dumps(summary),
            )
            return deny_policy(method_arn)

        logger.info(
            "Authorized user: %s, role=%s, keyPrefix=%s, scope=%s: %s",
            item.get("Username"),
            role,
            item.get("KeyPrefix", ""),
            key_scope(item),
            json.dumps(summary),
        )
        return allow_policy(
            method_arn,
            {
                "userId": item.get("UserId", ""),
                "username": item.get("Username", ""),
                "isAdmin": "true" if role == ADMIN else "false",
                "role": role,
                "scope": key_scope(item),
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
