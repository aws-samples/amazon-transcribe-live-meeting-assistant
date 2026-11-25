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

Last use: an authorized request records when the key was last used (LastUsedAt
on the key's item, shown in the MCP API Key container) and when its owner was last
active (LastActiveAt on the owner's item in the Cognito stack's sign-in table,
USER_SIGN_IN_TABLE, keyed by Cognito sub), at most once per 15 minutes per key.
Each write is one bounded attempt, and a failure is logged without changing the
request's policy.
"""

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

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

# The Cognito stack's sign-in table; empty when it has none, in which case only
# the key's own last use is recorded.
USER_SIGN_IN_TABLE = os.environ.get("USER_SIGN_IN_TABLE", "")
# A key's last use, and its owner's last activity, are written at most once in
# this many seconds per key.
USE_INTERVAL_SECONDS = 15 * 60
# One attempt of at most 1.5 seconds per call, for the calls that record use.
RECORD_CONFIG = Config(
    connect_timeout=0.5,
    read_timeout=1,
    retries={"mode": "standard", "total_max_attempts": 1},
)
# Key hashes this execution environment recorded within the interval, with when:
# the requests that follow make no call to record.
_recently_used = {}
RECENT_MAX = 10000
_record_dynamodb = None
_record_cognito = None


def _record_clients():
    global _record_dynamodb, _record_cognito  # pylint: disable=global-statement
    if _record_dynamodb is None:
        _record_dynamodb = boto3.client("dynamodb", config=RECORD_CONFIG)
    if _record_cognito is None:
        _record_cognito = boto3.client("cognito-idp", config=RECORD_CONFIG)
    return _record_dynamodb, _record_cognito


def iso_utc(moment):
    """A time in the form the tables hold (UTC, AppSync AWSDateTime)."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _is_condition_failure(error):
    return (
        isinstance(error, ClientError)
        and error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    )


def _owner_sub(item, owner, cognito_client):
    """The key owner's Cognito sub: stored with the key, or read from the user pool."""
    if item.get("UserSub"):
        return item["UserSub"]
    user = cognito_client.admin_get_user(UserPoolId=USER_POOL_ID, Username=owner)
    for attribute in user.get("UserAttributes") or []:
        if attribute.get("Name") == "sub":
            return attribute.get("Value") or ""
    return ""


def record_use(item, owner, now=None):
    """Record the key's last use and its owner's last activity. Never raises."""
    key_hash = item.get("KeyHash", "")
    try:
        clock = time.monotonic()
        seen = _recently_used.get(key_hash)
        if not key_hash or (seen is not None and clock - seen < USE_INTERVAL_SECONDS):
            return
        now = now or datetime.now(timezone.utc)
        values = {
            ":at": {"S": iso_utc(now)},
            ":cutoff": {"S": iso_utc(now - timedelta(seconds=USE_INTERVAL_SECONDS))},
        }
        dynamodb_client, cognito_client = _record_clients()
        try:
            dynamodb_client.update_item(
                TableName=TABLE_NAME,
                Key={"KeyHash": {"S": key_hash}},
                UpdateExpression="SET LastUsedAt = :at",
                # Not on a key revoked meanwhile, and at most once per interval.
                ConditionExpression=(
                    "attribute_exists(KeyHash) AND "
                    "(attribute_not_exists(LastUsedAt) OR LastUsedAt < :cutoff)"
                ),
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if not _is_condition_failure(error):
                raise
            # Recorded within the interval by another execution environment.
            _remember(key_hash, clock)
            return
        _remember(key_hash, clock)
        if not USER_SIGN_IN_TABLE:
            return
        sub = _owner_sub(item, owner, cognito_client)
        if not sub:
            return
        try:
            dynamodb_client.update_item(
                TableName=USER_SIGN_IN_TABLE,
                Key={"UserSub": {"S": sub}},
                UpdateExpression="SET LastActiveAt = :at",
                ConditionExpression="attribute_not_exists(LastActiveAt) OR LastActiveAt < :cutoff",
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if not _is_condition_failure(error):
                raise
    except Exception as error:  # pylint: disable=broad-except
        logger.warning(
            "Could not record the use of key %s (%s)",
            item.get("KeyPrefix", ""),
            type(error).__name__,
        )


def _remember(key_hash, clock):
    if len(_recently_used) >= RECENT_MAX:
        _recently_used.clear()
    _recently_used[key_hash] = clock


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
        record_use(item, owner)
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
