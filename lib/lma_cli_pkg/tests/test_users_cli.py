# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Unit tests for `lma users list`.

The SDK is a stand-in: what is asserted here is the command's contract -- the
table, JSON and CSV forms each carry every user's email, role, status, enabled
flag, creation time and last sign-in; a user with no recorded sign-in shows "Not
recorded" rather than a blank; when the times could not be read every user shows
"Unavailable" and a warning goes to standard error, so a redirected file stays
clean. How the users and times are read is tested in
lib/lma_sdk/tests/test_user_operations.py.
"""

import csv
import io
import json
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from lma_cli.cli import main
from lma_sdk.models.users import UserList, UserRecord


@pytest.fixture
def runner():
    # Standard error apart from standard output: the default from Click 8.2, which
    # no longer takes the argument; Click 8.1 (allowed by the pin) needs it.
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:
        return CliRunner()


def _result(**overrides) -> UserList:
    base = dict(
        stack="LMA-team",
        region="us-east-1",
        account="111122223333",
        user_pool_id="us-east-1_pool",
        generated_at="2026-11-24T10:00:00.000Z",
        users=[
            UserRecord(
                username="admin@example.com",
                email="admin@example.com",
                role="Admin",
                status="CONFIRMED",
                enabled=True,
                created_at="2026-01-02T03:04:05.000Z",
                last_sign_in_at="2026-11-20T09:30:00.000Z",
            ),
            UserRecord(
                username="new@example.com",
                email="new@example.com",
                role="User",
                status="FORCE_CHANGE_PASSWORD",
                enabled=False,
                created_at="2026-11-23T12:00:00.000Z",
                last_sign_in_at=None,
            ),
        ],
    )
    base.update(overrides)
    return UserList(**base)


def _run(runner, args, result):
    client = MagicMock()
    client.users.list.return_value = result
    invoked = runner.invoke(main, ["users", "list", *args], obj={"client_factory": lambda **kw: client})
    return invoked, client


def test_the_table_shows_every_column_and_not_recorded_for_no_sign_in(runner, monkeypatch):
    from lma_cli import formatters

    # Wide enough that no cell is folded onto two lines.
    monkeypatch.setattr(formatters.console, "width", 200)
    invoked, _client = _run(runner, [], _result())

    assert invoked.exit_code == 0, invoked.output
    out = invoked.stdout
    for heading in ("Email", "Role", "Status", "Enabled", "Created", "Last sign-in"):
        assert heading in out
    assert "admin@example.com" in out and "Admin" in out
    assert "2026-11-20 09:30:00 UTC" in out
    assert "Not recorded" in out
    assert "FORCE_CHANGE_PASSWORD" in out
    assert "Stack LMA-team (Region us-east-1, account 111122223333)" in out


def test_json_is_the_full_result(runner):
    invoked, _client = _run(runner, ["--format", "json"], _result())

    assert invoked.exit_code == 0, invoked.output
    data = json.loads(invoked.stdout)
    assert data["stack"] == "LMA-team"
    assert data["last_sign_in_available"] is True
    users = {u["email"]: u for u in data["users"]}
    assert users["admin@example.com"]["last_sign_in_at"] == "2026-11-20T09:30:00.000Z"
    assert users["new@example.com"]["last_sign_in_at"] is None
    assert users["new@example.com"]["enabled"] is False


def test_csv_has_a_header_and_one_row_per_user(runner):
    invoked, _client = _run(runner, ["--format", "csv"], _result())

    assert invoked.exit_code == 0, invoked.output
    rows = list(csv.DictReader(io.StringIO(invoked.stdout)))
    assert list(rows[0]) == [
        "email",
        "username",
        "role",
        "status",
        "enabled",
        "created_at",
        "last_sign_in_at",
    ]
    assert rows[0] == {
        "email": "admin@example.com",
        "username": "admin@example.com",
        "role": "Admin",
        "status": "CONFIRMED",
        "enabled": "true",
        "created_at": "2026-01-02T03:04:05.000Z",
        "last_sign_in_at": "2026-11-20T09:30:00.000Z",
    }
    assert rows[1]["last_sign_in_at"] == "Not recorded"
    assert rows[1]["enabled"] == "false"


def test_unavailable_times_are_marked_and_warned_about_on_stderr(runner):
    result = _result(
        last_sign_in_available=False,
        last_sign_in_unavailable_reason="stack LMA-team records no sign-in times",
    )
    for user in result.users:
        user.last_sign_in_at = None

    invoked, _client = _run(runner, ["--format", "csv"], result)

    assert invoked.exit_code == 0, invoked.output
    rows = list(csv.DictReader(io.StringIO(invoked.stdout)))
    assert [row["last_sign_in_at"] for row in rows] == ["Unavailable", "Unavailable"]
    assert "records no sign-in times" in invoked.stderr
    assert "records no sign-in times" not in invoked.stdout


def test_output_writes_the_file(runner, tmp_path):
    target = tmp_path / "users.csv"

    invoked, _client = _run(runner, ["--format", "csv", "--output", str(target)], _result())

    assert invoked.exit_code == 0, invoked.output
    assert target.read_text(encoding="utf-8").startswith("email,username,role,")


def test_output_needs_json_or_csv(runner, tmp_path):
    invoked, client = _run(runner, ["--output", str(tmp_path / "users.txt")], _result())

    assert invoked.exit_code == 2
    client.users.list.assert_not_called()


def test_an_sdk_error_exits_with_status_one(runner):
    from lma_sdk.exceptions import LMAConfigurationError

    client = MagicMock()
    client.users.list.side_effect = LMAConfigurationError("Could not list the users; the caller needs cognito-idp:ListUsers")
    invoked = runner.invoke(main, ["users", "list"], obj={"client_factory": lambda **kw: client})

    assert invoked.exit_code == 1
    assert "cognito-idp:ListUsers" in invoked.stderr


def test_a_stack_given_before_users_is_used(runner):
    seen = {}
    client = MagicMock()
    client.users.list.return_value = _result()

    def factory(**kwargs):
        seen.update(kwargs)
        return client

    invoked = runner.invoke(
        main,
        ["--stack-name", "LMA-other", "users", "list", "--format", "json"],
        obj={"client_factory": factory},
        env={"LMA_STACK_NAME": "LMA-env"},
    )

    assert invoked.exit_code == 0, invoked.output
    assert seen["stack_name"] == "LMA-other"
