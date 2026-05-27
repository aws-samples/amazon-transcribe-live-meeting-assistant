# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
Shared helpers for resolving the LMA user behind an MCP request:
  - resolve_user_sub: email/username → Cognito sub
  - has_zoom_credentials: does the user have stored Zoom creds?

Both start_meeting_now and schedule_meeting need these to set the VP row's
owner and to pass userSub / userZoomSub into the launched ECS task.
"""

import logging
import os
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()


def resolve_user_sub(email_or_username: str) -> Optional[str]:
    """Resolve a Cognito user identifier (email or username) to its sub.

    Returns None when the user pool isn't configured or the user isn't found.
    Best-effort — failures here only mean the MCP-launched VP joins as a
    guest instead of using stored Zoom credentials, so they should never
    block the join / schedule itself.
    """
    pool_id = os.environ.get("USER_POOL_ID")
    if not pool_id or not email_or_username:
        return None
    cognito = boto3.client("cognito-idp")
    try:
        resp = cognito.admin_get_user(UserPoolId=pool_id, Username=email_or_username)
        for attr in resp.get("UserAttributes", []):
            if attr.get("Name") == "sub":
                return attr.get("Value")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code != "UserNotFoundException":
            logger.warning("admin_get_user failed for %s: %s", email_or_username, exc)
    try:
        resp = cognito.list_users(
            UserPoolId=pool_id,
            Filter=f'email = "{email_or_username}"',
            Limit=1,
        )
        users = resp.get("Users", [])
        if users:
            for attr in users[0].get("Attributes", []):
                if attr.get("Name") == "sub":
                    return attr.get("Value")
    except ClientError as exc:
        logger.warning("list_users failed for %s: %s", email_or_username, exc)
    return None


def has_zoom_credentials(cognito_sub: str) -> bool:
    """Check whether the user has stored Zoom credentials.

    Returns False on any error so we always fall back to guest join.
    """
    if not cognito_sub:
        return False
    stack_name = os.environ.get("LMA_STACK_NAME")
    if not stack_name:
        return False
    secret_name = f"{stack_name}/zoom-credentials/{cognito_sub}"
    sm = boto3.client("secretsmanager")
    try:
        described = sm.describe_secret(SecretId=secret_name)
        return not described.get("DeletedDate")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("ResourceNotFoundException", "InvalidRequestException"):
            logger.warning("describe_secret failed for %s: %s", secret_name, exc)
        return False
