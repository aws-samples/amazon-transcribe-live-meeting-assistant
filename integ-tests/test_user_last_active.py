# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Last active times for user access reviews, against a live stack.

See docs/user-management.md#last-active for the behaviour asserted.

Default run (no meeting needed, about a minute, self-cleaning). One temporary
Admin is created with a permanent password and no invitation message, and signs
in through the app client with SRP, as the web UI does. Then:

``test_a_sign_in_records_the_last_activity``
    The pre token generation trigger has set LastActiveAt in the sign-in table.
``test_a_token_refresh_records_the_last_activity``
    With LastActiveAt moved an hour back, a refresh-token renewal (what a client
    left open does about hourly) sets it to now again; ``listUsers`` and
    ``lma users list --format json`` report that time.
``test_an_mcp_api_key_call_records_the_key_and_the_owner``
    With LastActiveAt moved back again, one MCP call with a newly generated API
    key sets it to now, and ``listMCPApiKeys`` reports the key's ``lastUsedAt``.
    Skipped (as an optional feature) on a stack without the MCP API key endpoint.

The user, their sign-in item and their API key are removed in teardown.

Needs pycognito (integ-tests/requirements.txt) and the stack's app client to
allow SRP sign-in, as the web UI's does.
"""

from __future__ import annotations

import json
import secrets
import string
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from click.testing import CliRunner
from lma_cli.cli import main

#: Clock skew allowed between this host and AWS when comparing times.
SKEW = timedelta(seconds=60)
#: How far back a recorded time is moved, so the next activity must write again.
BACKDATE = timedelta(hours=1)

LIST_USERS = "query ListUsers { listUsers { users { username lastSignInAt lastActiveAt } lastSignInAvailable } }"
LIST_KEYS = "query { listMCPApiKeys { keyPrefix lastUsedAt } }"
GENERATE_KEY = "mutation { generateMCPApiKey { keyValue keyPrefix } }"
REVOKE_KEY = "mutation Revoke($p: String!) { revokeMCPApiKey(keyPrefix: $p) }"
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _password() -> str:
    body = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
    return f"La!{body}9aZ"


def _email_domain(client) -> str:
    described = client.session.client("cloudformation").describe_stacks(StackName=client.stack_name)
    allowed = next(
        (
            p.get("ParameterValue", "")
            for p in described["Stacks"][0].get("Parameters", [])
            if p["ParameterKey"] == "AllowedSignUpEmailDomain"
        ),
        "",
    )
    domains = [d.strip() for d in allowed.split(",") if d.strip()]
    return domains[0] if domains else "example.com"


def _graphql_url(outputs: dict) -> str:
    for part in outputs.get("LocalUITestingEnv", "").split():
        if part.startswith("VITE_APPSYNC_GRAPHQL_URL="):
            return part.split("=", 1)[1]
    raise AssertionError("the stack has no VITE_APPSYNC_GRAPHQL_URL in its LocalUITestingEnv output")


def _post(url: str, headers: dict, body: dict) -> tuple[int, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 - https stack endpoint
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, None


def _graphql(url: str, token: str, query: str, variables: dict | None = None) -> dict:
    status, body = _post(url, {"Authorization": token}, {"query": query, "variables": variables or {}})
    # Only the errors, never the body: a generate call's body holds the key.
    errors = body.get("errors") if isinstance(body, dict) else None
    assert status == 200 and isinstance(body, dict) and not errors, f"GraphQL HTTP {status}: {errors}"
    return body["data"]


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@pytest.fixture(scope="module")
def admin(client, outputs):
    """A temporary Admin signed in once with SRP; removed, with their sign-in item, after."""
    pytest.importorskip("pycognito", reason="pip install -r integ-tests/requirements.txt")
    from pycognito import Cognito  # pylint: disable=import-outside-toplevel

    cognito = client.session.client("cognito-idp")
    pool = client.vp.user_pool_id
    table = client.users.sign_in_table()
    dynamodb = client.session.client("dynamodb")
    name = f"integ-active-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}@{_email_domain(client)}"
    user: dict[str, Any] = {}
    try:
        created = cognito.admin_create_user(
            UserPoolId=pool,
            Username=name,
            MessageAction="SUPPRESS",
            UserAttributes=[{"Name": "email", "Value": name}, {"Name": "email_verified", "Value": "true"}],
        )["User"]
        user["username"] = created["Username"]
        user["sub"] = next(a["Value"] for a in created["Attributes"] if a["Name"] == "sub")
        password = _password()
        cognito.admin_set_user_password(UserPoolId=pool, Username=name, Password=password, Permanent=True)
        cognito.admin_add_user_to_group(UserPoolId=pool, Username=name, GroupName="Admin")
        user["before"] = datetime.now(timezone.utc)
        session = Cognito(pool, outputs["CognitoUserPoolClientId"], username=name, user_pool_region=client.region)
        session.authenticate(password=password)
        user["session"] = session
        user["table"] = table
        user["dynamodb"] = dynamodb
        yield user
    finally:
        if user.get("username"):
            try:
                cognito.admin_delete_user(UserPoolId=pool, Username=user["username"])
            except Exception as error:  # noqa: BLE001
                print(f"could not delete the temporary user {user['username']}: {error}")
        if user.get("sub"):
            try:
                dynamodb.delete_item(TableName=table, Key={"UserSub": {"S": user["sub"]}})
            except Exception as error:  # noqa: BLE001
                print(f"could not delete the sign-in item of {user.get('username')}: {error}")


def _last_active(user: dict) -> str | None:
    item = user["dynamodb"].get_item(
        TableName=user["table"], Key={"UserSub": {"S": user["sub"]}}, ConsistentRead=True
    ).get("Item", {})
    return (item.get("LastActiveAt") or {}).get("S")


def _backdate(user: dict) -> datetime:
    """Move the recorded last activity an hour back; return the time just after."""
    user["dynamodb"].update_item(
        TableName=user["table"],
        Key={"UserSub": {"S": user["sub"]}},
        UpdateExpression="SET LastActiveAt = :at",
        ExpressionAttributeValues={":at": {"S": _iso(datetime.now(timezone.utc) - BACKDATE)}},
    )
    return datetime.now(timezone.utc)


def _assert_since(value: str | None, since: datetime, what: str) -> None:
    assert value, f"no last active time recorded after {what}"
    at = _parse(value)
    assert since - SKEW <= at <= datetime.now(timezone.utc) + SKEW, f"last active {value} is not from {what}"


def test_a_sign_in_records_the_last_activity(admin) -> None:
    _assert_since(_last_active(admin), admin["before"], "the sign-in")


def test_a_token_refresh_records_the_last_activity(client, admin, outputs) -> None:
    since = _backdate(admin)

    admin["session"].renew_access_token()

    recorded = _last_active(admin)
    _assert_since(recorded, since, "the token refresh")

    data = _graphql(_graphql_url(outputs), admin["session"].id_token, LIST_USERS)["listUsers"]
    assert data["lastSignInAvailable"] is True
    row = next(u for u in data["users"] if u["username"] == admin["username"])
    assert row["lastActiveAt"] == recorded

    try:
        runner = CliRunner(mix_stderr=False)
    except TypeError:
        runner = CliRunner()
    base = ["--stack-name", client.stack_name] + (["--region", client.region] if client.region else [])
    invoked = runner.invoke(main, [*base, "users", "list", "--format", "json"])
    assert invoked.exit_code == 0, invoked.output
    listed = {u["username"]: u for u in json.loads(invoked.stdout)["users"]}
    assert listed[admin["username"]]["last_active_at"] == recorded


def test_an_mcp_api_key_call_records_the_key_and_the_owner(admin, outputs, request) -> None:
    endpoint = outputs.get("MCPServerApiKeyEndpoint")
    if not endpoint:
        request.node.add_marker(pytest.mark.optional_feature)
        pytest.skip("the stack has no MCP API key endpoint (EnableMCP=false)")
    url = _graphql_url(outputs)
    token = admin["session"].id_token
    generated = _graphql(url, token, GENERATE_KEY)["generateMCPApiKey"]
    prefix = generated["keyPrefix"]
    try:
        since = _backdate(admin)

        status, _body = _post(endpoint, {"x-api-key": generated["keyValue"]}, TOOLS_LIST)

        assert status == 200, f"the MCP call with the new key returned HTTP {status}"
        _assert_since(_last_active(admin), since, "the MCP API key call")
        keys = _graphql(url, token, LIST_KEYS)["listMCPApiKeys"] or []
        key = next(k for k in keys if k["keyPrefix"] == prefix)
        _assert_since(key["lastUsedAt"], since, "the MCP API key call (the key's last use)")
    finally:
        _graphql(url, token, REVOKE_KEY, {"p": prefix})
