#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
OAuth Manager Lambda Function
Handles OAuth 2.1 initialization and callback for MCP servers
Supports PKCE with automatic fallback to OAuth 2.0

The callback writes to the account-wide MCP servers table -- it updates the
credential on an existing row and creates the row when OAuth is configured
before installation -- so it is subject to the same two authorization layers as
the MCPServerManager function:
  1. The AppSync schema pins both mutations to the "Admin" Cognito group
     (`@aws_cognito_user_pools(cognito_groups: ["Admin"])`)
  2. This handler re-checks the caller's `cognito:groups` claim, so the resolver
     directive is never the only control
"""

import base64
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Dict
from urllib.parse import urlencode

import boto3
import requests

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
kms = boto3.client("kms")

# Environment variables
OAUTH_STATE_TABLE = os.environ.get("OAUTH_STATE_TABLE", "")
MCP_SERVERS_TABLE = os.environ.get("MCP_SERVERS_TABLE", "")
KMS_KEY_ID = os.environ.get("KMS_KEY_ID", "")
OAUTH_CALLBACK_URL = os.environ.get("OAUTH_CALLBACK_URL", "")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

# Both fields write to the account-wide MCP servers table, so both are
# administrator operations. Kept in step with mcp_server_manager.ADMIN_ONLY_FIELDS.
ADMIN_ONLY_FIELDS = frozenset({"initOAuthFlow", "handleOAuthCallback"})

# Mirrors MAX_SERVERS_PER_ACCOUNT in mcp_server_manager/index.py: the callback can
# create a server row, so it honours the same per-account limit.
MAX_SERVERS_PER_ACCOUNT = 5

# A server identifier is either a registry name (a dotted name, optionally with a
# single namespace separator, as published by the MCP registry) or the server's
# own http(s) endpoint, which the UI falls back to when a registry name is not
# known. Invariant: a stored identifier is drawn from letters, digits, '.', '_',
# '-' and at most one '/', or is a whitespace-free http(s) URL.
SERVER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?")
HTTP_ENDPOINT_PATTERN = re.compile(r"https?://\S+")

MAX_SERVER_ID_LENGTH = 214

SERVER_ID_HELP = (
    "A server identifier is a published registry name (letters, digits, '.', '_', "
    "'-' and at most one '/') or the server's http(s) endpoint URL."
)


class ForbiddenError(Exception):
    """Raised when the caller is not authorized."""


class ValidationError(Exception):
    """Raised when input validation fails."""


def _get_caller_identity(event: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the caller's Cognito groups / username from the AppSync event.

    `cognito:groups` arrives as a list from the Cognito user-pool authorizer and
    as a comma-joined string from some token shapes, so both are normalized.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or identity.get("groups") or []
    if isinstance(groups, str):
        groups = [g.strip() for g in groups.split(",") if g.strip()]
    elif not isinstance(groups, (list, tuple)):
        groups = []
    username = claims.get("cognito:username") or identity.get("username") or claims.get("sub") or ""
    return {
        "username": username,
        "groups": list(groups),
        "is_admin": ADMIN_GROUP in groups,
    }


def _require_admin(caller: Dict[str, Any]) -> None:
    """Re-check the caller's group membership inside the function.

    Keeps the schema directive from being the only place the constraint exists.
    """
    if not caller["is_admin"]:
        logger.warning(
            "Caller '%s' is not a member of the %s group (groups=%s)",
            caller["username"],
            ADMIN_GROUP,
            caller["groups"],
        )
        raise ForbiddenError(f"Only members of the {ADMIN_GROUP} group can manage MCP servers")


def validate_server_id(server_id: Any) -> str:
    """Return `server_id` unchanged if it is an accepted identifier, else raise.

    The callback can create a server row keyed on this value, and the row's
    NpmPackage field holds it too, so it is checked before anything is stored.
    Values are rejected rather than rewritten.
    """
    if not isinstance(server_id, str) or not server_id:
        raise ValidationError(f"serverId is required. {SERVER_ID_HELP}")
    if len(server_id) > MAX_SERVER_ID_LENGTH:
        raise ValidationError(f"serverId must be {MAX_SERVER_ID_LENGTH} characters or fewer.")
    if SERVER_ID_PATTERN.fullmatch(server_id) or HTTP_ENDPOINT_PATTERN.fullmatch(server_id):
        return server_id
    raise ValidationError(f"serverId '{server_id}' is not an accepted identifier. {SERVER_ID_HELP}")


def validate_server_url(server_url: Any) -> str:
    """Return `server_url` unchanged if it is an accepted endpoint, else raise."""
    if (
        not isinstance(server_url, str)
        or len(server_url) > MAX_SERVER_ID_LENGTH
        or not HTTP_ENDPOINT_PATTERN.fullmatch(server_url)
    ):
        raise ValidationError(
            "serverUrl must be an http:// or https:// URL with no spaces and at most "
            f"{MAX_SERVER_ID_LENGTH} characters"
        )
    return server_url


def encrypt_token(token: str) -> str:
    """Encrypt token using KMS"""
    try:
        response = kms.encrypt(KeyId=KMS_KEY_ID, Plaintext=token.encode("utf-8"))
        return base64.b64encode(response["CiphertextBlob"]).decode("utf-8")
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
        # Extract input from GraphQL resolver event. The event carries the
        # caller's claims, so only the request's own fields are logged, and the
        # PKCE challenge is left out of them.
        input_data = event.get("arguments", {}).get("input", {})
        logger.info(
            "Init OAuth flow request: %s",
            {k: v for k, v in input_data.items() if k != "codeChallenge"},
        )

        server_id = input_data.get("serverId")
        provider = input_data.get("provider")
        client_id = input_data.get("clientId")
        authorization_url = input_data.get("authorizationUrl")
        token_url = input_data.get("tokenUrl")
        scopes = input_data.get("scopes", [])
        code_challenge = input_data.get("codeChallenge")
        server_url = input_data.get("serverUrl")
        server_name = input_data.get("serverName")

        if not all([server_id, client_id, authorization_url, token_url]):
            return {"success": False, "error": "Missing required fields"}

        # The callback can turn these values into a server row, so they are
        # checked here as well as there: the flow does not start with an
        # identifier that could not be stored.
        try:
            server_id = validate_server_id(server_id)
            if server_url:
                server_url = validate_server_url(server_url)
        except ValidationError as exc:
            logger.warning("Rejected OAuth flow request: %s", exc)
            return {"success": False, "error": str(exc)}

        # Generate state for CSRF protection
        import uuid

        state = str(uuid.uuid4())

        # Store state in DynamoDB with TTL (10 minutes)
        state_table = dynamodb.Table(OAUTH_STATE_TABLE)
        state_item = {
            "State": state,
            "ServerId": server_id,
            "AccountId": ACCOUNT_ID,
            "Provider": provider,
            "ClientId": client_id,
            "TokenUrl": token_url,
            "CodeChallenge": code_challenge,
            "CreatedAt": datetime.utcnow().isoformat(),
            "ExpiresAt": int(time.time()) + 600,  # 10 minutes TTL
        }
        if server_url:
            state_item["ServerUrl"] = server_url
        if server_name:
            state_item["ServerName"] = server_name
        state_table.put_item(Item=state_item)

        # Build authorization URL with PKCE
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": OAUTH_CALLBACK_URL,
            "state": state,
            "scope": " ".join(scopes) if scopes else "",
        }

        # Add PKCE parameters (OAuth 2.1)
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"

        # Build URL
        query_string = urlencode(params)
        full_auth_url = f"{authorization_url}?{query_string}"

        logger.info(f"OAuth flow initialized for server {server_id}")

        return {"authorizationUrl": full_auth_url, "state": state}

    except Exception as e:
        logger.error(f"Error initializing OAuth flow: {str(e)}")
        return {"success": False, "error": str(e)}


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
        logger.info("OAuth callback request")

        # Extract input
        input_data = event.get("arguments", {}).get("input", {})

        code = input_data.get("code")
        state = input_data.get("state")
        code_verifier = input_data.get("codeVerifier")

        if not all([code, state]):
            return {"success": False, "error": "Missing required fields"}

        # Verify state and get stored data
        state_table = dynamodb.Table(OAUTH_STATE_TABLE)
        response = state_table.get_item(Key={"State": state})

        if "Item" not in response:
            return {"success": False, "error": "Invalid or expired state"}

        state_data = response["Item"]
        server_id = state_data["ServerId"]
        account_id = state_data["AccountId"]
        client_id = state_data["ClientId"]
        token_url = state_data["TokenUrl"]
        code_challenge = state_data.get("CodeChallenge")

        # Exchange code for tokens
        token_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": OAUTH_CALLBACK_URL,
            "client_id": client_id,
        }

        # Add PKCE code_verifier if we used code_challenge
        if code_challenge and code_verifier:
            token_data["code_verifier"] = code_verifier

        # Request tokens
        try:
            token_response = requests.post(
                token_url,
                data=token_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=(5, 30),
            )

            if token_response.status_code != 200:
                error_msg = token_response.text
                logger.error(f"Token exchange failed: {error_msg}")

                # Check if PKCE not supported, retry without it
                if code_verifier and "invalid_request" in error_msg.lower():
                    logger.info("PKCE not supported, retrying without code_verifier")
                    del token_data["code_verifier"]
                    token_response = requests.post(
                        token_url,
                        data=token_data,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        timeout=(5, 30),
                    )

                    if token_response.status_code != 200:
                        return {
                            "success": False,
                            "error": f"Token exchange failed: {token_response.text}",
                        }
                else:
                    return {"success": False, "error": f"Token exchange failed: {error_msg}"}

            tokens = token_response.json()

        except Exception as e:
            logger.error(f"Token request failed: {e}")
            return {"success": False, "error": f"Token request failed: {str(e)}"}

        # Encrypt tokens
        encrypted_access = encrypt_token(tokens["access_token"])
        encrypted_refresh = encrypt_token(tokens.get("refresh_token", ""))

        # Calculate expiration
        expires_at = int(time.time()) + tokens.get("expires_in", 3600)

        # Get or create server config
        servers_table = dynamodb.Table(MCP_SERVERS_TABLE)
        server_response = servers_table.get_item(
            Key={"AccountId": account_id, "ServerId": server_id}
        )

        # Build OAuth config
        auth_config = {
            "authType": "oauth2",
            "oauth": {
                "provider": state_data["Provider"],
                "clientId": client_id,
                "tokenUrl": token_url,
                "accessToken": encrypted_access,
                "refreshToken": encrypted_refresh,
                "expiresAt": expires_at,
                "tokenType": tokens.get("token_type", "Bearer"),
                "lastRefreshed": datetime.utcnow().isoformat(),
            },
        }

        if "Item" in server_response:
            # Server exists - update auth config
            logger.info(f"Updating existing server {server_id} with OAuth tokens")
            servers_table.update_item(
                Key={"AccountId": account_id, "ServerId": server_id},
                UpdateExpression="SET AuthConfig = :config, UpdatedAt = :updated",
                ExpressionAttributeValues={
                    ":config": json.dumps(auth_config),
                    ":updated": datetime.utcnow().isoformat(),
                },
            )
        else:
            # Server doesn't exist - create it with OAuth config
            # This happens when OAuth is done before installation. The row is
            # account-wide, so it goes through the same checks as an install:
            # the identifier is validated and the per-account limit applies.
            try:
                server_id = validate_server_id(server_id)
                if "ServerUrl" in state_data:
                    state_data["ServerUrl"] = validate_server_url(state_data["ServerUrl"])
            except ValidationError as exc:
                logger.warning("Rejected OAuth server entry for '%s': %s", server_id, exc)
                return {"success": False, "error": str(exc)}

            existing = servers_table.query(
                KeyConditionExpression="AccountId = :accountId",
                ExpressionAttributeValues={":accountId": account_id},
            )
            if len(existing.get("Items", [])) >= MAX_SERVERS_PER_ACCOUNT:
                logger.warning(
                    "Account %s is at the %s server limit; not creating %s",
                    account_id,
                    MAX_SERVERS_PER_ACCOUNT,
                    server_id,
                )
                return {
                    "success": False,
                    "error": (
                        f"Maximum {MAX_SERVERS_PER_ACCOUNT} servers allowed per account. "
                        "Please uninstall a server first."
                    ),
                }

            logger.info(f"Creating new server entry {server_id} with OAuth tokens")
            now = datetime.utcnow().isoformat() + "Z"

            server_item = {
                "AccountId": account_id,
                "ServerId": server_id,
                "Name": state_data.get("ServerName", server_id),
                "NpmPackage": server_id,  # Required by GraphQL schema
                "PackageType": "streamable-http",  # OAuth servers are typically HTTP
                "Version": "latest",
                "Transport": ["streamable-http"],
                "Status": "ACTIVE",  # HTTP servers are immediately active
                "AuthConfig": json.dumps(auth_config),
                "RequiresAuth": True,
                "InstalledAt": now,
                "UpdatedAt": now,
            }

            # Add ServerUrl if provided
            if "ServerUrl" in state_data:
                server_item["ServerUrl"] = state_data["ServerUrl"]

            servers_table.put_item(Item=server_item)

        # Clean up state
        state_table.delete_item(Key={"State": state})

        logger.info(f"OAuth flow completed for server {server_id}")

        return {"success": True, "serverId": server_id}

    except Exception as e:
        logger.error(f"Error handling OAuth callback: {str(e)}")
        import traceback

        logger.error(f"Traceback: {traceback.format_exc()}")
        return {"success": False, "error": str(e)}


def handler(event: Dict[str, Any], context: Any) -> Any:
    """
    Main Lambda handler - routes to appropriate function based on field name
    """
    field_name = event.get("info", {}).get("fieldName", "")

    logger.info(f"OAuth Manager - Field: {field_name}")

    if field_name in ADMIN_ONLY_FIELDS:
        try:
            _require_admin(_get_caller_identity(event))
        except ForbiddenError as exc:
            # Surface as an AppSync error so the client sees an authorization failure
            # rather than a generic operation result.
            raise Exception(f"Unauthorized: {exc}") from exc

    if field_name == "initOAuthFlow":
        return init_oauth_flow(event, context)
    elif field_name == "handleOAuthCallback":
        return handle_oauth_callback(event, context)
    else:
        logger.error(f"Unknown field name: {field_name}")
        return {"success": False, "error": f"Unknown operation: {field_name}"}
