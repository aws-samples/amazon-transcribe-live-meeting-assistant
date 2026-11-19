#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.

"""
MCP Server Manager Lambda Function
Handles installation and management of MCP servers from the public registry
Account-level management (all users share installed servers)

Authorization is enforced at two independent layers, mirroring the
`user_management` resolver:
  1. The AppSync schema pins the management mutations to the "Admin" Cognito
     group (`@aws_cognito_user_pools(cognito_groups: ["Admin"])`)
  2. This handler re-checks the caller's `cognito:groups` claim, so the
     resolver directive is never the only control
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import boto3
import mcp_library
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource("dynamodb")
codebuild = boto3.client("codebuild")

# Environment variables
MCP_SERVERS_TABLE = os.environ.get("MCP_SERVERS_TABLE", "")
CODEBUILD_PROJECT = os.environ.get("CODEBUILD_PROJECT", "")
MAX_SERVERS_PER_ACCOUNT = 5
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "Admin")

# Installed servers are account-wide and their packages are baked into the shared
# meeting-assist Lambda layer, so changing them is an administrator operation.
ADMIN_ONLY_FIELDS = frozenset(
    {"installMCPServer", "uninstallMCPServer", "updateMCPServer", "updateMCPServerCredentials"}
)

# The library export and import (see mcp_library.py): for the Admin group, and
# for an IAM-signed caller, which is how the LMA SDK and CLI call AppSync. The
# export carries no credentials, and an import stores none.
LIBRARY_FIELDS = frozenset({"exportMCPServerLibrary", "importMCPServerLibrary"})

# AuthConfig holds the credential material for servers that require
# authentication (bearer tokens, OAuth client credentials, custom headers and
# environment variables). It is write-only: it is supplied on install and read at
# runtime directly from DynamoDB by the meeting-assist function, so the read path
# here projects it away and reports presence instead.
CREDENTIAL_FIELDS = ("AuthConfig",)

# Accepted package specifier forms. The MCP layer CodeBuild buildspec in
# lma-ai-stack/deployment/lma-ai-stack.yaml re-applies an equivalent pattern to
# every line it reads out of DynamoDB, so both layers agree on what a specifier
# is and neither depends on the other having run.
#
# Invariant: a stored NpmPackage is a bare distribution name with at most one
# version pin, drawn from letters, digits, '.', '_' and '-' (plus the single npm
# scope separator). Anything else is rejected rather than rewritten.
PYPI_PACKAGE_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*((==|>=|<=|~=)[A-Za-z0-9][A-Za-z0-9.*+_-]*)?"
)
NPM_PACKAGE_PATTERN = re.compile(
    r"(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(@[A-Za-z0-9][A-Za-z0-9.*+_-]*)?"
)
# Remote servers store their endpoint in NpmPackage instead of a package name;
# nothing installs them, so they only need to be an http(s) URL within the same
# length bound as a package specifier. Invariant: the stored endpoint holds no
# whitespace and no control characters. Kept identical to the copy in
# oauth_manager/index.py, which a test asserts.
HTTP_ENDPOINT_PATTERN = re.compile(r"https?://[^\s\x00-\x1f\x7f]+")

MAX_PACKAGE_SPECIFIER_LENGTH = 214

PACKAGE_SPECIFIER_HELP = (
    "Accepted forms are 'name' and 'name<operator>version' -- where the operator "
    "is '==', '>=', '<=' or '~=' -- for pypi packages, or 'name', '@scope/name' "
    "and 'name@version' for npm packages. "
    "Names and versions may contain letters, digits, '.', '_' and '-' only. "
    "Registry URLs, local paths, VCS references and multiple specifiers are not "
    "accepted -- install the published package by name instead."
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


def _is_iam_caller(event: Dict[str, Any]) -> bool:
    """The request is IAM-signed: it carries the signing principal's ARN and no user-pool claims.

    AppSync builds the identity block itself, so a client cannot make a
    user-pool request look like one. Kept apart from _get_caller_identity,
    which oauth_manager holds an identical copy of.
    """
    identity = event.get("identity") or {}
    return bool(identity.get("userArn")) and not identity.get("claims")


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


def validate_package_specifier(package: Any, package_type: str) -> str:
    """Return `package` unchanged if it is an accepted specifier, else raise.

    Values are rejected rather than rewritten so that what is stored is exactly
    what the caller asked for.
    """
    if package_type == "streamable-http":
        # Remote servers are addressed by URL and are never installed.
        if (
            not isinstance(package, str)
            or len(package) > MAX_PACKAGE_SPECIFIER_LENGTH
            or not HTTP_ENDPOINT_PATTERN.fullmatch(package)
        ):
            raise ValidationError(
                "ServerUrl must be an http:// or https:// URL with no spaces and at most "
                f"{MAX_PACKAGE_SPECIFIER_LENGTH} characters for streamable-http servers"
            )
        return package

    if not isinstance(package, str) or not package:
        raise ValidationError(f"NpmPackage is required. {PACKAGE_SPECIFIER_HELP}")
    if len(package) > MAX_PACKAGE_SPECIFIER_LENGTH:
        raise ValidationError(
            f"NpmPackage must be {MAX_PACKAGE_SPECIFIER_LENGTH} characters or fewer."
        )
    pattern = NPM_PACKAGE_PATTERN if package_type == "npm" else PYPI_PACKAGE_PATTERN
    if not pattern.fullmatch(package):
        raise ValidationError(
            f"NpmPackage '{package}' is not an accepted package specifier. {PACKAGE_SPECIFIER_HELP}"
        )
    return package


def redact_server(server: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return a copy of a stored server row without its credential material.

    Invariant: nothing the read path returns contains a credential. The boolean
    `HasAuthConfig` is substituted so callers can still tell whether a server has
    credentials configured.
    """
    if not server:
        return server
    redacted = {
        k: v for k, v in server.items() if k not in CREDENTIAL_FIELDS and k != "ImportedAuth"
    }
    redacted["HasAuthConfig"] = any(bool(server.get(field)) for field in CREDENTIAL_FIELDS)
    if server.get("CredentialsRequired"):
        # What the credentials form of an imported server opens with: re-read
        # through the library export's allowlist, so it holds no credential.
        redacted["ImportedAuthSettings"] = json.dumps(
            mcp_library.export_auth(
                {
                    "ImportedAuth": server.get("ImportedAuth"),
                    "RequiredAuthType": server.get("RequiredAuthType"),
                }
            )
        )
    return redacted


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
    input_data: Dict[str, Any] = {}
    try:
        # AuthConfig is credential material, so the request is not logged verbatim.
        input_data = event.get("arguments", {}).get("input", {})
        logger.info(
            "Install MCP server request: %s",
            {k: v for k, v in input_data.items() if k not in CREDENTIAL_FIELDS},
        )

        # Use AWS Account ID for account-level management
        account_id = ACCOUNT_ID or os.environ.get("AWS_ACCOUNT_ID", "unknown")

        server_id = input_data.get("ServerId")
        name = input_data.get("Name")
        npm_package = input_data.get("NpmPackage")
        version = input_data.get("Version", "latest")
        transport = input_data.get("Transport", ["stdio"])
        requires_auth = input_data.get("RequiresAuth", False)
        auth_config = input_data.get("AuthConfig")
        package_type = input_data.get(
            "PackageType", "pypi"
        )  # Default to pypi for backward compatibility

        if not all([server_id, name, npm_package]):
            return {
                "ServerId": server_id or "unknown",
                "Success": False,
                "Message": "Missing required fields: ServerId, Name, NpmPackage",
            }

        # Validate before anything is stored: the layer build installs whatever
        # NpmPackage holds, so only accepted specifiers are ever written.
        try:
            npm_package = validate_package_specifier(npm_package, package_type)
        except ValidationError as exc:
            logger.warning("Rejected MCP server package specifier for %s: %s", server_id, exc)
            return {"ServerId": server_id, "Success": False, "Message": str(exc)}

        # Check if table is configured
        if not MCP_SERVERS_TABLE:
            return {
                "ServerId": server_id,
                "Success": False,
                "Message": "MCP Servers table not configured",
            }

        table = dynamodb.Table(MCP_SERVERS_TABLE)

        # Check account's current server count
        response = table.query(
            KeyConditionExpression="AccountId = :accountId",
            ExpressionAttributeValues={":accountId": account_id},
        )

        current_servers = response.get("Items", [])
        if len(current_servers) >= MAX_SERVERS_PER_ACCOUNT:
            return {
                "ServerId": server_id,
                "Success": False,
                "Message": f"Maximum {MAX_SERVERS_PER_ACCOUNT} servers allowed per account. Please uninstall a server first.",
            }

        # Check if server already installed
        if any(s.get("ServerId") == server_id for s in current_servers):
            return {"ServerId": server_id, "Success": False, "Message": "Server already installed"}

        # Store server configuration in DynamoDB
        now = datetime.utcnow().isoformat() + "Z"
        server_url = input_data.get("ServerUrl")  # For HTTP servers

        item = {
            "AccountId": account_id,
            "ServerId": server_id,
            "Name": name,
            "NpmPackage": npm_package,
            "PackageType": package_type,
            "Version": version,
            "Transport": transport,
            "RequiresAuth": requires_auth,
            "InstalledAt": now,
            "UpdatedAt": now,
        }

        # HTTP servers are immediately active (no build needed)
        if package_type == "streamable-http":
            item["Status"] = "ACTIVE"
            if server_url:
                item["ServerUrl"] = server_url
        else:
            item["Status"] = "INSTALLING"

        if auth_config:
            item["AuthConfig"] = auth_config

        table.put_item(Item=item)
        logger.info(f"Stored MCP server config: {server_id}")

        # Trigger CodeBuild only for package-based servers (not HTTP servers)
        build_id = None
        if CODEBUILD_PROJECT and package_type != "streamable-http":
            try:
                build_response = codebuild.start_build(
                    projectName=CODEBUILD_PROJECT,
                    environmentVariablesOverride=[
                        {"name": "ACCOUNT_ID", "value": account_id, "type": "PLAINTEXT"},
                        {"name": "ACTION", "value": "INSTALL_MCP_SERVER", "type": "PLAINTEXT"},
                        {"name": "SERVER_ID", "value": server_id, "type": "PLAINTEXT"},
                    ],
                )
                build_id = build_response["build"]["id"]
                logger.info(f"Started CodeBuild: {build_id}")

                # Update status to BUILDING
                table.update_item(
                    Key={"AccountId": account_id, "ServerId": server_id},
                    UpdateExpression="SET #status = :status, BuildId = :buildId",
                    ExpressionAttributeNames={"#status": "Status"},
                    ExpressionAttributeValues={":status": "BUILDING", ":buildId": build_id},
                )
            except Exception as build_error:
                logger.error(f"Failed to start CodeBuild: {build_error}")
                # Update status to FAILED
                table.update_item(
                    Key={"AccountId": account_id, "ServerId": server_id},
                    UpdateExpression="SET #status = :status, ErrorMessage = :error",
                    ExpressionAttributeNames={"#status": "Status"},
                    ExpressionAttributeValues={":status": "FAILED", ":error": str(build_error)},
                )
                return {
                    "ServerId": server_id,
                    "Success": False,
                    "Message": f"Failed to start build: {str(build_error)}",
                }

        # Return appropriate message based on server type
        if package_type == "streamable-http":
            return {
                "ServerId": server_id,
                "Success": True,
                "Message": "HTTP server activated immediately (no build required)",
            }
        else:
            return {
                "ServerId": server_id,
                "Success": True,
                "Message": "Server installation started",
                "BuildId": build_id,
            }

    except Exception as e:
        logger.error(f"Error installing MCP server: {str(e)}")
        return {
            "ServerId": input_data.get("ServerId", "unknown"),
            "Success": False,
            "Message": f"Installation failed: {str(e)}",
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
        # Only the server id is logged: nothing else of the request is needed.
        logger.info("Uninstall MCP server request: %s", event.get("arguments", {}).get("serverId"))

        server_id = event.get("arguments", {}).get("serverId")

        # Use AWS Account ID
        account_id = ACCOUNT_ID or os.environ.get("AWS_ACCOUNT_ID", "unknown")

        if not server_id:
            return {"ServerId": "unknown", "Success": False, "Message": "ServerId is required"}

        if not MCP_SERVERS_TABLE:
            return {
                "ServerId": server_id,
                "Success": False,
                "Message": "MCP Servers table not configured",
            }

        table = dynamodb.Table(MCP_SERVERS_TABLE)

        # Delete server configuration
        table.delete_item(Key={"AccountId": account_id, "ServerId": server_id})

        logger.info(f"Deleted MCP server: {server_id} from account {account_id}")

        # Trigger CodeBuild to rebuild Strands Lambda layer
        if CODEBUILD_PROJECT:
            try:
                codebuild.start_build(
                    projectName=CODEBUILD_PROJECT,
                    environmentVariablesOverride=[
                        {"name": "ACCOUNT_ID", "value": account_id, "type": "PLAINTEXT"},
                        {"name": "ACTION", "value": "UNINSTALL_MCP_SERVER", "type": "PLAINTEXT"},
                    ],
                )
                logger.info("Started CodeBuild for uninstall")
            except Exception as build_error:
                logger.warning(f"Failed to start CodeBuild: {build_error}")
                # Continue anyway - server is deleted from DB

        return {
            "ServerId": server_id,
            "Success": True,
            "Message": "Server uninstalled successfully",
        }

    except Exception as e:
        logger.error(f"Error uninstalling MCP server: {str(e)}")
        return {
            "ServerId": event.get("arguments", {}).get("serverId", "unknown"),
            "Success": False,
            "Message": f"Uninstallation failed: {str(e)}",
        }


def update_mcp_server(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Update an MCP server to a new version

    Args:
        event: GraphQL resolver event with input containing:
            - ServerId: Server to update
            - Version: New version to install

    Returns:
        UpdateMCPServerOutput with success status and build ID
    """
    input_data: Dict[str, Any] = {}
    try:
        # Extract input. AuthConfig is credential material and the event also
        # carries the caller's claims, so neither is logged verbatim.
        input_data = event.get("arguments", {}).get("input", {})
        logger.info(
            "Update MCP server request: %s",
            {k: v for k, v in input_data.items() if k not in CREDENTIAL_FIELDS},
        )

        server_id = input_data.get("ServerId")
        new_version = input_data.get("Version")

        # Use AWS Account ID
        account_id = ACCOUNT_ID or os.environ.get("AWS_ACCOUNT_ID", "unknown")

        if not server_id or not new_version:
            return {
                "ServerId": server_id or "unknown",
                "Success": False,
                "Message": "Missing required fields: ServerId, Version",
            }

        if not MCP_SERVERS_TABLE:
            return {
                "ServerId": server_id,
                "Success": False,
                "Message": "MCP Servers table not configured",
            }

        table = dynamodb.Table(MCP_SERVERS_TABLE)

        # Get current server config
        response = table.get_item(Key={"AccountId": account_id, "ServerId": server_id})

        if "Item" not in response:
            return {"ServerId": server_id, "Success": False, "Message": "Server not found"}

        # Update version and status
        now = datetime.utcnow().isoformat() + "Z"
        table.update_item(
            Key={"AccountId": account_id, "ServerId": server_id},
            UpdateExpression="SET Version = :version, #status = :status, UpdatedAt = :updated",
            ExpressionAttributeNames={"#status": "Status"},
            ExpressionAttributeValues={
                ":version": new_version,
                ":status": "UPDATING",
                ":updated": now,
            },
        )

        logger.info(f"Updated server {server_id} to version {new_version}")

        # Trigger CodeBuild to rebuild layer with new version
        build_id = None
        if CODEBUILD_PROJECT:
            try:
                build_response = codebuild.start_build(
                    projectName=CODEBUILD_PROJECT,
                    environmentVariablesOverride=[
                        {"name": "ACCOUNT_ID", "value": account_id, "type": "PLAINTEXT"},
                        {"name": "ACTION", "value": "UPDATE_MCP_SERVER", "type": "PLAINTEXT"},
                        {"name": "SERVER_ID", "value": server_id, "type": "PLAINTEXT"},
                    ],
                )
                build_id = build_response["build"]["id"]
                logger.info(f"Started CodeBuild for update: {build_id}")

                # Update with build ID
                table.update_item(
                    Key={"AccountId": account_id, "ServerId": server_id},
                    UpdateExpression="SET BuildId = :buildId",
                    ExpressionAttributeValues={":buildId": build_id},
                )
            except Exception as build_error:
                logger.error(f"Failed to start CodeBuild: {build_error}")
                # Revert status
                table.update_item(
                    Key={"AccountId": account_id, "ServerId": server_id},
                    UpdateExpression="SET #status = :status, ErrorMessage = :error",
                    ExpressionAttributeNames={"#status": "Status"},
                    ExpressionAttributeValues={":status": "FAILED", ":error": str(build_error)},
                )
                return {
                    "ServerId": server_id,
                    "Success": False,
                    "Message": f"Failed to start build: {str(build_error)}",
                }

        return {
            "ServerId": server_id,
            "Success": True,
            "Message": f"Server update to version {new_version} started",
            "BuildId": build_id,
        }

    except Exception as e:
        logger.error(f"Error updating MCP server: {str(e)}")
        return {
            "ServerId": input_data.get("ServerId", "unknown"),
            "Success": False,
            "Message": f"Update failed: {str(e)}",
        }


def list_installed_servers(event: Dict[str, Any], context: Any) -> list:
    """
    List all MCP servers installed for the account
    (Account-level management - all users share installed servers)

    Returns:
        List of MCPServer objects
    """
    try:
        logger.info("List installed MCP servers request")

        # Use AWS Account ID for account-level management
        account_id = ACCOUNT_ID or os.environ.get("AWS_ACCOUNT_ID", "unknown")

        if not MCP_SERVERS_TABLE:
            logger.warning("MCP Servers table not configured")
            return []

        table = dynamodb.Table(MCP_SERVERS_TABLE)

        # Query all servers for this account
        response = table.query(
            KeyConditionExpression="AccountId = :accountId",
            ExpressionAttributeValues={":accountId": account_id},
        )

        servers = response.get("Items", [])
        logger.info(f"Found {len(servers)} installed servers for account {account_id}")

        return [redact_server(server) for server in servers]

    except Exception as e:
        logger.error(f"Error listing installed servers: {str(e)}")
        return []


def get_mcp_server(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Get details of a specific installed MCP server
    (Account-level management)

    Returns:
        MCPServer object or None
    """
    try:
        server_id = event.get("arguments", {}).get("serverId")

        # Use AWS Account ID
        account_id = ACCOUNT_ID or os.environ.get("AWS_ACCOUNT_ID", "unknown")

        if not server_id or not MCP_SERVERS_TABLE:
            return None

        table = dynamodb.Table(MCP_SERVERS_TABLE)

        response = table.get_item(Key={"AccountId": account_id, "ServerId": server_id})

        return redact_server(response.get("Item"))

    except Exception as e:
        logger.error(f"Error getting MCP server: {str(e)}")
        return None


def _validated_package(package: Any, package_type: str) -> str:
    """validate_package_specifier, raising the library's error for an import entry."""
    try:
        return validate_package_specifier(package, package_type)
    except ValidationError as exc:
        raise mcp_library.LibraryError(str(exc)) from None


def _installed(table: Any, account_id: str) -> list:
    response = table.query(
        KeyConditionExpression="AccountId = :accountId",
        ExpressionAttributeValues={":accountId": account_id},
    )
    return response.get("Items", [])


def export_mcp_server_library(event: Dict[str, Any], context: Any) -> str:
    """The installed servers as a library document (AWSJSON), with no credentials."""
    del event, context
    account_id = ACCOUNT_ID or os.environ.get("AWS_ACCOUNT_ID", "unknown")
    servers = _installed(dynamodb.Table(MCP_SERVERS_TABLE), account_id) if MCP_SERVERS_TABLE else []
    document = mcp_library.export_library(servers)
    logger.info("Exported an MCP server library of %s servers", len(document["servers"]))
    return json.dumps(document)


def _start_build(account_id: str, server_id: str, action: str) -> Optional[str]:
    if not CODEBUILD_PROJECT:
        return None
    response = codebuild.start_build(
        projectName=CODEBUILD_PROJECT,
        environmentVariablesOverride=[
            {"name": "ACCOUNT_ID", "value": account_id, "type": "PLAINTEXT"},
            {"name": "ACTION", "value": action, "type": "PLAINTEXT"},
            {"name": "SERVER_ID", "value": server_id, "type": "PLAINTEXT"},
        ],
    )
    return response["build"]["id"]


def _definition(server: "mcp_library.ImportedServer") -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "Name": server.name,
        "NpmPackage": server.package,
        "PackageType": server.package_type,
        "Version": server.version,
        "Transport": server.transport,
        "RequiresAuth": server.requires_auth,
    }
    if server.server_url:
        item["ServerUrl"] = server.server_url
    if server.description:
        item["Description"] = server.description
    return item


def _create_item(account_id: str, step: "mcp_library.ImportStep", now: str) -> Dict[str, Any]:
    server = step.server
    assert server is not None
    item = {
        "AccountId": account_id,
        "ServerId": step.server_id,
        **_definition(server),
        "InstalledAt": now,
        "UpdatedAt": now,
        "Status": "ACTIVE" if server.package_type == "streamable-http" else "INSTALLING",
    }
    # No AuthConfig: the credentials are entered on this deployment. The
    # settings that are not credentials (header names, OAuth client settings)
    # are kept as ImportedAuth, so a later export still lists them.
    if server.needs_credentials:
        item["CredentialsRequired"] = True
        item["RequiredAuthType"] = server.auth_type
        item["ImportedAuth"] = json.dumps(server.auth)
    return item


def _update_request(
    existing: Dict[str, Any], step: "mcp_library.ImportStep", now: str
) -> Tuple[Dict[str, Any], bool]:
    """The update_item arguments for an import that updates a server, and whether to rebuild."""
    server = step.server
    assert server is not None
    definition = _definition(server)
    names = {f"#{k}": k for k in definition}
    values: Dict[str, Any] = {f":{k}": v for k, v in definition.items()}
    values[":updatedAt"] = now
    sets = [f"#{k} = :{k}" for k in definition] + ["UpdatedAt = :updatedAt"]
    removes = [
        k
        for k, present in (("ServerUrl", server.server_url), ("Description", server.description))
        if not present
    ]
    if server.needs_credentials and not step.keep_credentials:
        # The stored credentials are for another type of authentication.
        sets += [
            "CredentialsRequired = :required",
            "RequiredAuthType = :authType",
            "ImportedAuth = :importedAuth",
        ]
        values[":required"] = True
        values[":authType"] = server.auth_type
        values[":importedAuth"] = json.dumps(server.auth)
        removes.append("AuthConfig")
    elif server.auth_type == mcp_library.AUTH_NONE:
        # The library says the server needs no credentials: nothing is waited
        # for. Credentials the server already stores are kept, with its
        # RequiresAuth, while its endpoint is unchanged, and removed when the
        # entry moves it elsewhere (keep_credentials, from plan_import).
        removes += ["CredentialsRequired", "RequiredAuthType", "ImportedAuth"]
        if not step.keep_credentials:
            removes.append("AuthConfig")
        elif existing.get("AuthConfig"):
            values[":RequiresAuth"] = True
    # Only what the layer build installs starts a build.
    changed = any(
        existing.get(k) != definition[k] for k in ("NpmPackage", "PackageType", "Version")
    )
    rebuild = server.package_type != "streamable-http" and changed
    if rebuild:
        sets.append("#status = :status")
        names["#status"] = "Status"
        values[":status"] = "UPDATING"
    expression = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
    return (
        {
            "UpdateExpression": expression,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        },
        rebuild,
    )


def _apply_step(table: Any, account_id: str, step: "mcp_library.ImportStep") -> Dict[str, Any]:
    """Carry out one create or update of an import; the result reported for it."""
    server = step.server
    assert server is not None
    now = datetime.utcnow().isoformat() + "Z"
    key = {"AccountId": account_id, "ServerId": step.server_id}
    if step.action == "create":
        table.put_item(
            Item=_create_item(account_id, step, now),
            ConditionExpression="attribute_not_exists(ServerId)",
        )
        rebuild = server.package_type != "streamable-http"
        needs_credentials = server.needs_credentials
    else:
        existing = table.get_item(Key=key).get("Item", {})
        request, rebuild = _update_request(existing, step, now)
        table.update_item(Key=key, **request)
        needs_credentials = server.needs_credentials and (
            not step.keep_credentials or bool(existing.get("CredentialsRequired"))
        )
    result = {
        "Name": step.name,
        "ServerId": step.server_id,
        "Action": "created" if step.action == "create" else "updated",
        "CredentialsRequired": needs_credentials,
        "Message": "",
    }
    if rebuild:
        try:
            build_id = _start_build(
                account_id,
                step.server_id,
                "INSTALL_MCP_SERVER" if step.action == "create" else "UPDATE_MCP_SERVER",
            )
            result["Message"] = f"Layer build started ({build_id})" if build_id else ""
            if build_id:
                table.update_item(
                    Key=key,
                    UpdateExpression="SET BuildId = :buildId",
                    ExpressionAttributeValues={":buildId": build_id},
                )
        except Exception as build_error:  # pylint: disable=broad-except
            logger.error("Failed to start CodeBuild for %s: %s", step.server_id, build_error)
            table.update_item(
                Key=key,
                UpdateExpression="SET #status = :status, ErrorMessage = :error",
                ExpressionAttributeNames={"#status": "Status"},
                ExpressionAttributeValues={":status": "FAILED", ":error": str(build_error)},
            )
            result["Message"] = f"Failed to start build: {build_error}"
    return result


def _skipped(name: str, server_id: str, action: str, message: str) -> Dict[str, Any]:
    return {
        "Name": name,
        "ServerId": server_id,
        "Action": action,
        "CredentialsRequired": False,
        "Message": message,
    }


def import_mcp_server_library(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Create or update the servers a library document lists, matched by name.

    No credential is stored, whatever the document holds: a server that needs
    credentials is marked CredentialsRequired, which the meeting assistant does
    not load, until an Admin enters them (updateMCPServerCredentials).
    """
    del context
    account_id = ACCOUNT_ID or os.environ.get("AWS_ACCOUNT_ID", "unknown")
    if not MCP_SERVERS_TABLE:
        return {"Success": False, "Message": "MCP Servers table not configured", "Servers": []}
    table = dynamodb.Table(MCP_SERVERS_TABLE)
    library = event.get("arguments", {}).get("library")
    try:
        plan = mcp_library.plan_import(
            library, _installed(table, account_id), _validated_package, MAX_SERVERS_PER_ACCOUNT
        )
    except mcp_library.LibraryError as exc:
        return {"Success": False, "Message": str(exc), "Servers": []}
    results = []
    for step in plan.steps:
        if step.action == "skip":
            results.append(_skipped(step.name, "", "skipped", step.message))
            continue
        try:
            results.append(_apply_step(table, account_id, step))
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Import of MCP server %s failed: %s", step.name, exc)
            results.append(_skipped(step.name, step.server_id, "failed", str(exc)))
    logger.info("Imported an MCP server library: %s", {r["Name"]: r["Action"] for r in results})
    failed = [r for r in results if r["Action"] == "failed"]
    return {
        "Success": not failed,
        "Message": f"{len(results) - len(failed)} of {len(results)} servers processed",
        "Servers": results,
    }


def update_mcp_server_credentials(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Store an installed server's credentials, and clear CredentialsRequired."""
    del context
    input_data = event.get("arguments", {}).get("input", {}) or {}
    server_id = input_data.get("ServerId")
    auth_config = input_data.get("AuthConfig")
    account_id = ACCOUNT_ID or os.environ.get("AWS_ACCOUNT_ID", "unknown")
    if not server_id or not auth_config:
        return {
            "ServerId": server_id or "unknown",
            "Success": False,
            "Message": "ServerId and AuthConfig are required",
        }
    try:
        parsed = json.loads(auth_config) if isinstance(auth_config, str) else auth_config
    except ValueError:
        parsed = None
    auth_type = parsed.get("authType") if isinstance(parsed, dict) else None
    if auth_type not in mcp_library.AUTH_TYPES or auth_type == mcp_library.AUTH_NONE:
        return {
            "ServerId": server_id,
            "Success": False,
            "Message": "AuthConfig must be an object with a known authType",
        }
    if not MCP_SERVERS_TABLE:
        return {
            "ServerId": server_id,
            "Success": False,
            "Message": "MCP Servers table not configured",
        }
    table = dynamodb.Table(MCP_SERVERS_TABLE)
    existing = table.get_item(Key={"AccountId": account_id, "ServerId": server_id}).get("Item")
    if not existing:
        return {"ServerId": server_id, "Success": False, "Message": "Server not found"}
    required = existing.get("RequiredAuthType")
    if required in mcp_library.AUTH_TYPES and required != auth_type:
        return {
            "ServerId": server_id,
            "Success": False,
            "Message": f"This server needs {required} credentials, not {auth_type}",
        }
    try:
        table.update_item(
            Key={"AccountId": account_id, "ServerId": server_id},
            UpdateExpression=(
                "SET AuthConfig = :config, RequiresAuth = :requires, UpdatedAt = :updated "
                "REMOVE CredentialsRequired, RequiredAuthType, ImportedAuth"
            ),
            ConditionExpression="attribute_exists(ServerId)",
            ExpressionAttributeValues={
                ":config": auth_config,
                ":requires": True,
                ":updated": datetime.utcnow().isoformat() + "Z",
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return {"ServerId": server_id, "Success": False, "Message": "Server not found"}
        raise
    logger.info("Stored credentials for MCP server %s", server_id)
    return {"ServerId": server_id, "Success": True, "Message": "Credentials saved"}


def handler(event: Dict[str, Any], context: Any) -> Any:
    """
    Main Lambda handler - routes to appropriate function based on field name
    """
    field_name = event.get("info", {}).get("fieldName", "")

    logger.info(f"MCP Server Manager - Field: {field_name}")

    if field_name in ADMIN_ONLY_FIELDS:
        try:
            _require_admin(_get_caller_identity(event))
        except ForbiddenError as exc:
            # Surface as an AppSync error so the client sees an authorization failure
            # rather than a generic operation result.
            raise Exception(f"Unauthorized: {exc}") from exc

    if field_name in LIBRARY_FIELDS:
        if not _is_iam_caller(event):
            try:
                _require_admin(_get_caller_identity(event))
            except ForbiddenError as exc:
                raise Exception(f"Unauthorized: {exc}") from exc

    if field_name == "installMCPServer":
        return install_mcp_server(event, context)
    elif field_name == "uninstallMCPServer":
        return uninstall_mcp_server(event, context)
    elif field_name == "updateMCPServer":
        return update_mcp_server(event, context)
    elif field_name == "listInstalledMCPServers":
        return list_installed_servers(event, context)
    elif field_name == "getMCPServer":
        return get_mcp_server(event, context)
    elif field_name == "exportMCPServerLibrary":
        return export_mcp_server_library(event, context)
    elif field_name == "importMCPServerLibrary":
        return import_mcp_server_library(event, context)
    elif field_name == "updateMCPServerCredentials":
        return update_mcp_server_credentials(event, context)
    else:
        logger.error(f"Unknown field name: {field_name}")
        return {"Success": False, "Message": f"Unknown operation: {field_name}"}
