# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""What each LMA role is admitted to on a deployed stack, for test_role_access.py.

The role matrix (lma-ai-stack/source/appsync/role-matrix.json) says which roles
may call each GraphQL operation and use each other entry point. This module
creates a throwaway user for each role, signs each in through SRP as the web UI
does, and calls:

* every Query and Mutation as each role the matrix refuses it, with placeholder
  arguments: AppSync must refuse the call at the field, before its resolver runs;
* every Query as each role the matrix admits, except the ones the matrix's probe
  section names: AppSync must not refuse it at the field (the resolver may still
  answer null or an error for a placeholder that names nothing);
* the WebSocket transcriber, the identity pool and, when the stack has one, the
  MCP server's API key endpoint.

Mutations are not called as the roles they admit, since they change data.
Operations are built from schema.graphql: required arguments get a placeholder
of their type, optional ones are left out, and an object result selects only
__typename. build_operation is pure and is checked against the schema offline by
test_ci_plumbing.py, so a schema change that the builder cannot follow fails
there rather than on a deployed stack.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import string
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = REPO_ROOT / "lma-ai-stack" / "source" / "appsync" / "role-matrix.json"
SCHEMA_PATH = REPO_ROOT / "lma-ai-stack" / "source" / "appsync" / "schema.graphql"

# AppSync's scalars and auth directives, which the repository's SDL leaves implicit.
APPSYNC_PRELUDE = """
scalar AWSDate
scalar AWSTime
scalar AWSDateTime
scalar AWSTimestamp
scalar AWSEmail
scalar AWSJSON
scalar AWSURL
scalar AWSPhone
scalar AWSIPAddress
directive @aws_subscribe(mutations: [String!]!) on FIELD_DEFINITION
directive @aws_auth(cognito_groups: [String!]) on FIELD_DEFINITION
directive @aws_cognito_user_pools(cognito_groups: [String!]) on OBJECT | FIELD_DEFINITION
directive @aws_iam on OBJECT | FIELD_DEFINITION
directive @aws_api_key on OBJECT | FIELD_DEFINITION
directive @aws_oidc on OBJECT | FIELD_DEFINITION
directive @aws_lambda on OBJECT | FIELD_DEFINITION
"""

PLACEHOLDER_ID = "integ-rbac-placeholder"
SCALAR_PLACEHOLDERS: dict[str, Any] = {
    "ID": PLACEHOLDER_ID,
    "String": PLACEHOLDER_ID,
    "Int": 1,
    "Float": 1.0,
    "Boolean": False,
    "AWSDate": "2000-01-01",
    "AWSTime": "00:00:00.000Z",
    "AWSDateTime": "2000-01-01T00:00:00.000Z",
    "AWSTimestamp": 0,
    "AWSJSON": "{}",
    "AWSEmail": "nobody@example.invalid",
    "AWSURL": "https://example.invalid/",
    "AWSPhone": "+10000000000",
    "AWSIPAddress": "127.0.0.1",
}


def load_matrix() -> dict:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def load_schema():
    from graphql import build_ast_schema, parse  # pylint: disable=import-outside-toplevel

    return build_ast_schema(parse(APPSYNC_PRELUDE + SCHEMA_PATH.read_text()), assume_valid_sdl=True)


def matrix_roles(matrix: dict) -> dict[str, tuple[str, ...]]:
    """Every AppSync operation and the signed-in roles it admits (none: backend only)."""
    roles = {op: tuple(c["roles"]) for c in matrix["capabilities"] for op in c["appsync"]}
    roles.update({op: () for op in matrix["backendOnly"]["appsync"]})
    return roles


def placeholder(graphql_type) -> Any:
    """A value of the type: required input fields filled, optional ones left out."""
    from graphql import (  # pylint: disable=import-outside-toplevel
        GraphQLEnumType,
        GraphQLInputObjectType,
        GraphQLList,
        GraphQLNonNull,
    )

    if isinstance(graphql_type, GraphQLNonNull):
        return placeholder(graphql_type.of_type)
    if isinstance(graphql_type, GraphQLList):
        return [placeholder(graphql_type.of_type)]
    if isinstance(graphql_type, GraphQLEnumType):
        return next(iter(graphql_type.values))
    if isinstance(graphql_type, GraphQLInputObjectType):
        return {
            name: placeholder(input_field.type)
            for name, input_field in graphql_type.fields.items()
            if isinstance(input_field.type, GraphQLNonNull)
        }
    return SCALAR_PLACEHOLDERS[graphql_type.name]


def build_operation(schema, operation: str) -> tuple[str, dict]:
    """The document and variables that call one root field ("Type.field") with placeholders."""
    from graphql import GraphQLNonNull, get_named_type, is_composite_type  # pylint: disable=import-outside-toplevel

    type_name, field_name = operation.split(".", 1)
    root = schema.get_type(type_name)
    target = root.fields[field_name]
    required = {name: arg for name, arg in target.args.items() if isinstance(arg.type, GraphQLNonNull)}
    definitions = ", ".join(f"${name}: {arg.type}" for name, arg in required.items())
    arguments = ", ".join(f"{name}: ${name}" for name in required)
    selection = " { __typename }" if is_composite_type(get_named_type(target.type)) else ""
    document = (
        f"{type_name.lower()} RoleProbe{f'({definitions})' if definitions else ''} "
        f"{{ {field_name}{f'({arguments})' if arguments else ''}{selection} }}"
    )
    return document, {name: placeholder(arg.type) for name, arg in required.items()}


def refused_at_field(response: dict | None, operation: str) -> bool:
    """Whether AppSync refused the field to the caller (its "Not Authorized" error)."""
    type_name, field_name = operation.split(".", 1)
    expected = f"Not Authorized to access {field_name} on type {type_name}"
    for error in (response or {}).get("errors") or []:
        if error.get("errorType") == "Unauthorized" and expected in (error.get("message") or ""):
            return True
    return False


@dataclass
class Plan:
    """The calls to make: (operation, role, expect_refused)."""

    calls: list[tuple[str, str, bool]] = field(default_factory=list)


def plan_calls(matrix: dict, schema) -> Plan:
    """Every Query and Mutation as each refused role, and every probe-safe Query as each admitted role."""
    probe = matrix["probe"]
    skip_allowed = set(probe["skipAllowed"])
    admin_only_allowed = set(probe["placeholderRefusedForNonAdmins"])
    plan = Plan()
    for operation, admitted in sorted(matrix_roles(matrix).items()):
        type_name = operation.split(".", 1)[0]
        if type_name == "Subscription":
            continue
        assert schema.get_type(type_name).fields.get(operation.split(".", 1)[1]) is not None, operation
        for role in matrix["roles"]:
            if role not in admitted:
                plan.calls.append((operation, role, True))
            elif type_name == "Query" and operation not in skip_allowed:
                if operation in admin_only_allowed and role != "Admin":
                    continue
                plan.calls.append((operation, role, False))
    return plan


# ── a deployed stack ────────────────────────────────────────────────────────


def _password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Rb!" + "".join(secrets.choice(alphabet) for _ in range(20)) + "9a"


@dataclass
class RoleUser:
    role: str
    username: str
    password: str
    tokens: dict = field(default_factory=dict)


@dataclass
class Deployment:
    """What the probes need from the stack, resolved once."""

    session: Any
    outputs: dict
    pool_id: str
    client_id: str
    graphql_url: str
    identity_pool_id: str
    ai_resources: dict
    email_domain: str

    @classmethod
    def find(cls, stack_name: str, region: str | None) -> "Deployment":
        import boto3  # pylint: disable=import-outside-toplevel
        from mcp_api_key_probe import _graphql_url  # pylint: disable=import-outside-toplevel
        from ws_stream_probe import _outputs, _stack_resources  # pylint: disable=import-outside-toplevel

        session = boto3.Session(region_name=region)
        cfn = session.client("cloudformation")
        outputs = _outputs(cfn, stack_name)
        nested = {r["LogicalResourceId"]: r["PhysicalResourceId"] for r in _stack_resources(cfn, stack_name)}
        cognito_outputs = _outputs(cfn, nested["COGNITOSTACK"])
        ai = {r["LogicalResourceId"]: r["PhysicalResourceId"] for r in _stack_resources(cfn, nested["AISTACK"])}
        parameters = cfn.describe_stacks(StackName=stack_name)["Stacks"][0].get("Parameters", [])
        allowed = next(
            (p.get("ParameterValue", "") for p in parameters if p["ParameterKey"] == "AllowedSignUpEmailDomain"),
            "",
        )
        domains = [d.strip() for d in allowed.split(",") if d.strip()]
        return cls(
            session=session,
            outputs=outputs,
            pool_id=cognito_outputs["UserPoolId"],
            client_id=outputs["CognitoUserPoolClientId"],
            graphql_url=_graphql_url(outputs),
            identity_pool_id=cognito_outputs["AppIdentityPoolId"],
            ai_resources=ai,
            # .invalid is reserved (RFC 2606), so no message can reach a mailbox. A
            # stack that restricts sign-up to its domains refuses it even to an
            # administrator, so the first of those is used there; MessageAction
            # SUPPRESS sends no invitation either way.
            email_domain=domains[0] if domains else "example.invalid",
        )


ROLE_ACCOUNTS: tuple[tuple[str, str], ...] = (("admin", "Admin"), ("user", "User"), ("participant", "Participant"))


def create_role_users(
    deployment: Deployment, stamp: str, accounts: tuple[tuple[str, str], ...] = ROLE_ACCOUNTS
) -> list[RoleUser]:
    """A confirmed user with a permanent password for each (label, role); the caller deletes them.

    The label names the account in its username, so a run may make several
    accounts of one role (test_security_access.py makes three Users).
    """
    cognito = deployment.session.client("cognito-idp")
    made: list[RoleUser] = []
    try:
        for label, role in accounts:
            username = f"integ-rbac-{label}-{stamp}@{deployment.email_domain}"
            cognito.admin_create_user(
                UserPoolId=deployment.pool_id,
                Username=username,
                MessageAction="SUPPRESS",
                UserAttributes=[
                    {"Name": "email", "Value": username},
                    {"Name": "email_verified", "Value": "true"},
                ],
            )
            user = RoleUser(role, username, _password())
            made.append(user)
            cognito.admin_set_user_password(
                UserPoolId=deployment.pool_id, Username=username, Password=user.password, Permanent=True
            )
            if role != "User":
                cognito.admin_add_user_to_group(UserPoolId=deployment.pool_id, Username=username, GroupName=role)
    except Exception:
        delete_role_users(deployment, made)
        raise
    return made


def delete_role_users(deployment: Deployment, users: list[RoleUser]) -> list[str]:
    """Delete the users; the ones that could not be deleted are returned for the report."""
    cognito = deployment.session.client("cognito-idp")
    left: list[str] = []
    for user in users:
        try:
            cognito.admin_delete_user(UserPoolId=deployment.pool_id, Username=user.username)
        except Exception:  # noqa: BLE001 - reported, not raised, so every user is tried
            left.append(user.username)
    return left


def sign_in(deployment: Deployment, user: RoleUser) -> None:
    from ws_stream_probe import _mint_tokens  # pylint: disable=import-outside-toplevel

    user.tokens = _mint_tokens(
        deployment.session.region_name, deployment.pool_id, deployment.client_id, user.username, user.password
    )


def claims(token: str) -> dict:
    from mcp_meeting_tools_probe import _claims  # pylint: disable=import-outside-toplevel

    return _claims(token)


def call(deployment: Deployment, user: RoleUser, document: str, variables: dict) -> dict:
    """The raw GraphQL response: data and errors both, never raised."""
    from mcp_api_key_probe import _post  # pylint: disable=import-outside-toplevel

    _, body = _post(
        deployment.graphql_url, {"Authorization": user.tokens["id"]}, {"query": document, "variables": variables}
    )
    return body if isinstance(body, dict) else {}


def transcriber_status(deployment: Deployment, user: RoleUser) -> int:
    """The HTTP status of a WebSocket upgrade with the user's access token: 101 when accepted."""
    import websockets  # pylint: disable=import-outside-toplevel

    url = f"{deployment.outputs['LMAWebsocketEndpoint']}?authorization=Bearer%20{user.tokens['access']}"

    async def attempt() -> int:
        try:
            async with websockets.connect(url, open_timeout=25):
                return 101
        except websockets.exceptions.InvalidStatus as error:
            return error.response.status_code

    return asyncio.run(attempt())


def identity_pool_role(deployment: Deployment, user: RoleUser) -> tuple[str, Any]:
    """The assumed-role ARN of the user's identity pool credentials, and a session using them.

    Raises the identity pool's error when it gives the user no credentials.
    """
    import boto3  # pylint: disable=import-outside-toplevel

    region = deployment.session.region_name
    identity = boto3.client("cognito-identity", region_name=region)
    login = {f"cognito-idp.{region}.amazonaws.com/{deployment.pool_id}": user.tokens["id"]}
    identity_id = identity.get_id(IdentityPoolId=deployment.identity_pool_id, Logins=login)["IdentityId"]
    credentials = identity.get_credentials_for_identity(IdentityId=identity_id, Logins=login)["Credentials"]
    session = boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )
    return session.client("sts").get_caller_identity()["Arn"], session


def settings_readable(deployment: Deployment, session) -> bool:
    """Whether the credentials can read the deployment settings parameter, as the web UI does."""
    from botocore.exceptions import ClientError  # pylint: disable=import-outside-toplevel

    try:
        session.client("ssm").get_parameter(Name=deployment.outputs["LMASettingsParameterName"])
        return True
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "AccessDeniedException":
            return False
        raise


def mcp_status_with_key(deployment: Deployment, user: RoleUser) -> int | None:
    """The MCP endpoint's HTTP status for a key stored for the user; None without the MCP server.

    The key is written straight to the key table, as a key the user generated
    before a change of role would be, and deleted again afterwards.
    """
    from mcp_api_key_probe import _post  # pylint: disable=import-outside-toplevel

    endpoint = deployment.outputs.get("MCPServerApiKeyEndpoint")
    table_name = deployment.ai_resources.get("MCPApiKeysTable")
    if not endpoint or not table_name:
        return None
    raw_key = f"lma_{uuid.uuid4()}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    table = deployment.session.resource("dynamodb").Table(table_name)
    table.put_item(
        Item={
            "KeyHash": key_hash,
            "UserId": user.username,
            "Username": user.username,
            "IsAdmin": "false",
            "KeyPrefix": raw_key[:12],
            "CreatedAt": "2000-01-01T00:00:00+00:00",
            "Enabled": "true",
            "Scope": "read",
        }
    )
    try:
        status, _ = _post(
            endpoint, {"x-api-key": raw_key}, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        return status
    finally:
        table.delete_item(Key={"KeyHash": key_hash})
