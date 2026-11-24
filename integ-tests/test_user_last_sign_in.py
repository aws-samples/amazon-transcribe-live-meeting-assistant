# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
# See the LICENSE file in the project root for full license information.
"""Last sign-in times for user access reviews, against a live stack.

See docs/user-management.md#last-sign-in for the behaviour asserted.

Default run (no meeting needed, under a minute, self-cleaning). Two temporary
Cognito users are created with a permanent password and no invitation message: an
Admin and a plain user. Each signs in through the app client with SRP, as the web
UI does, which runs the Cognito stack's PostAuthentication trigger. Then:

``test_listusers_returns_each_users_recorded_sign_in``
    ``listUsers``, called as the temporary Admin, returns a last sign-in time for
    both users that is no earlier than just before they signed in, along with
    their roles, and reports the times available.
``test_lma_users_list_returns_the_same_times``
    ``lma users list --format json`` and ``--format csv``, run through the CLI
    itself with the caller's AWS credentials, list the same two times.
``test_deleting_a_user_removes_their_sign_in_time``
    ``deleteUser``, called as the temporary Admin, deletes the plain user and
    their item in the sign-in table.

Both users and their sign-in items are removed in teardown.

Needs pycognito (integ-tests/requirements.txt) and the stack's app client to
allow SRP sign-in, as the web UI's does.
"""

from __future__ import annotations

import csv
import io
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

#: Clock skew allowed between this host and AWS when comparing sign-in times.
SKEW = timedelta(seconds=60)

LIST_USERS = (
    "query ListUsers { listUsers { users { username email role status enabled "
    "createdAt lastSignInAt } lastSignInAvailable } }"
)
DELETE_USER = (
    "mutation DeleteUser($input: DeleteUserInput!) { deleteUser(input: $input) { username success } }"
)


def _password() -> str:
    """A password satisfying the pool's policy, for a user that lives a minute."""
    body = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
    return f"Ls!{body}9aZ"


def _email_domain(client) -> str:
    """A domain the pool accepts: the first of AllowedSignUpEmailDomain, else example.com."""
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


def _graphql(url: str, token: str, query: str, variables: dict | None = None) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 - https stack endpoint
            body = json.loads(response.read() or b"null")
    except urllib.error.HTTPError as err:
        raise AssertionError(f"GraphQL HTTP {err.code}: {err.read()[:400]!r}") from err
    assert isinstance(body, dict) and not body.get("errors"), f"GraphQL errors: {body}"
    return body["data"]


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def signed_in(client, outputs):
    """Two temporary users, an Admin and a plain user, each signed in once; removed after.

    Yields ``{role: {"username", "sub", "token"}}`` and the time just before the
    sign-ins.
    """
    pytest.importorskip("pycognito", reason="pip install -r integ-tests/requirements.txt")
    from pycognito import Cognito  # pylint: disable=import-outside-toplevel

    cognito = client.session.client("cognito-idp")
    pool = client.vp.user_pool_id
    app_client = outputs["CognitoUserPoolClientId"]
    table = client.users.sign_in_table()
    dynamodb = client.session.client("dynamodb")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    domain = _email_domain(client)
    made: dict[str, dict[str, Any]] = {}
    try:
        for role in ("admin", "member"):
            name = f"integ-signin-{role}-{stamp}@{domain}"
            created = cognito.admin_create_user(
                UserPoolId=pool,
                Username=name,
                MessageAction="SUPPRESS",
                UserAttributes=[
                    {"Name": "email", "Value": name},
                    {"Name": "email_verified", "Value": "true"},
                ],
            )["User"]
            sub = next(a["Value"] for a in created["Attributes"] if a["Name"] == "sub")
            made[role] = {"username": created["Username"], "sub": sub, "password": _password()}
            cognito.admin_set_user_password(
                UserPoolId=pool, Username=name, Password=made[role]["password"], Permanent=True
            )
        cognito.admin_add_user_to_group(
            UserPoolId=pool, Username=made["admin"]["username"], GroupName="Admin"
        )
        before = datetime.now(timezone.utc)
        for user in made.values():
            session = Cognito(pool, app_client, username=user["username"], user_pool_region=client.region)
            session.authenticate(password=user.pop("password"))
            user["token"] = session.id_token
        yield made, before
    finally:
        for user in made.values():
            try:
                cognito.admin_delete_user(UserPoolId=pool, Username=user["username"])
            except cognito.exceptions.UserNotFoundException:
                pass
            except Exception as error:  # noqa: BLE001
                print(f"could not delete the temporary user {user['username']}: {error}")
            try:
                dynamodb.delete_item(TableName=table, Key={"UserSub": {"S": user["sub"]}})
            except Exception as error:  # noqa: BLE001
                print(f"could not delete the sign-in item of {user['username']}: {error}")


@pytest.fixture(scope="module")
def listed(signed_in, outputs) -> dict:
    users, _before = signed_in
    data = _graphql(_graphql_url(outputs), users["admin"]["token"], LIST_USERS)
    return data["listUsers"]


def _assert_recent(value: str | None, before: datetime, who: str) -> None:
    assert value, f"no last sign-in recorded for {who}"
    at = _parse(value)
    now = datetime.now(timezone.utc)
    assert before - SKEW <= at <= now + SKEW, f"{who}'s last sign-in {value} is not this run's"


def test_listusers_returns_each_users_recorded_sign_in(signed_in, listed) -> None:
    users, before = signed_in
    assert listed["lastSignInAvailable"] is True
    by_name = {u["username"]: u for u in listed["users"]}
    for role, expected_role in (("admin", "Admin"), ("member", "User")):
        row = by_name[users[role]["username"]]
        assert row["role"] == expected_role
        assert row["status"] == "CONFIRMED"
        _assert_recent(row["lastSignInAt"], before, users[role]["username"])


def test_lma_users_list_returns_the_same_times(client, signed_in, listed) -> None:
    users, _before = signed_in
    from_api = {u["username"]: u["lastSignInAt"] for u in listed["users"]}
    # Standard error apart from standard output, so a warning cannot break the
    # JSON or CSV parse: the default from Click 8.2, which no longer takes the
    # argument; Click 8.1 (allowed by the CLI's pin) needs it.
    try:
        runner = CliRunner(mix_stderr=False)
    except TypeError:
        runner = CliRunner()
    base = ["--stack-name", client.stack_name]
    if client.region:
        base += ["--region", client.region]

    as_json = runner.invoke(main, [*base, "users", "list", "--format", "json"])
    assert as_json.exit_code == 0, as_json.output
    result = json.loads(as_json.stdout)
    assert result["last_sign_in_available"] is True
    cli_times = {u["username"]: u["last_sign_in_at"] for u in result["users"]}

    as_csv = runner.invoke(main, [*base, "users", "list", "--format", "csv"])
    assert as_csv.exit_code == 0, as_csv.output
    csv_times = {
        row["username"]: row["last_sign_in_at"] for row in csv.DictReader(io.StringIO(as_csv.stdout))
    }

    for user in users.values():
        name = user["username"]
        assert cli_times[name] == from_api[name], name
        assert csv_times[name] == from_api[name], name


def test_deleting_a_user_removes_their_sign_in_time(client, signed_in, outputs) -> None:
    users, _before = signed_in
    member = users["member"]
    table = client.users.sign_in_table()
    dynamodb = client.session.client("dynamodb")
    key = {"UserSub": {"S": member["sub"]}}
    assert "Item" in dynamodb.get_item(TableName=table, Key=key, ConsistentRead=True)

    data = _graphql(
        _graphql_url(outputs),
        users["admin"]["token"],
        DELETE_USER,
        {"input": {"username": member["username"]}},
    )

    assert data["deleteUser"] == {"username": member["username"], "success": True}
    assert "Item" not in dynamodb.get_item(TableName=table, Key=key, ConsistentRead=True)
