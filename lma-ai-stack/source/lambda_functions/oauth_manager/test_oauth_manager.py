#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the OAuth manager resolver (no AWS calls)."""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

import pytest

os.environ.setdefault("OAUTH_STATE_TABLE", "oauth-state")
os.environ.setdefault("MCP_SERVERS_TABLE", "mcp-servers")
os.environ.setdefault("AWS_ACCOUNT_ID", "111122223333")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

sys.path.insert(0, str(Path(__file__).parent))

with mock.patch("boto3.resource"), mock.patch("boto3.client"):
    import index  # noqa: E402


# ---------------------------------------------------------------------------
# Server identifier validation
# ---------------------------------------------------------------------------

ACCEPTED_SERVER_IDS = [
    "awslabs.aws-documentation-mcp-server",
    "io.github.owner/server-name",
    "salesforce",
    "Server_1",
    "https://example.test/mcp",
    "http://example.test:8080/mcp",
]

# Values the resolver must reject, mirroring the table in mcp_server_manager. The
# accepted set is a fixed character class, so the branches to cover are:
# whitespace, a character outside that class, a leading dash, more than one
# namespace separator, a scheme that is not http(s), and the empty value.
REJECTED_SERVER_IDS = [
    "server one",
    "server\tname",
    "server#name",
    "server%name",
    "server+name",
    "-server",
    "a/b/c",
    "server\\other",
    "server\nserver2",
    "ftp://example.test/mcp",
    "",
]


@pytest.mark.parametrize("server_id", ACCEPTED_SERVER_IDS)
def test_accepts_registry_name_or_endpoint_url(server_id: str) -> None:
    assert index.validate_server_id(server_id) == server_id


@pytest.mark.parametrize("server_id", REJECTED_SERVER_IDS)
def test_rejects_identifier_that_is_not_a_name_or_endpoint(server_id: str) -> None:
    with pytest.raises(index.ValidationError):
        index.validate_server_id(server_id)


def test_rejects_an_identifier_longer_than_the_limit() -> None:
    with pytest.raises(index.ValidationError):
        index.validate_server_id("a" * (index.MAX_SERVER_ID_LENGTH + 1))


def test_rejects_a_non_string_identifier() -> None:
    with pytest.raises(index.ValidationError):
        index.validate_server_id({"id": "server"})


def test_error_message_names_the_accepted_form() -> None:
    with pytest.raises(index.ValidationError) as excinfo:
        index.validate_server_id("a/b/c")
    assert "registry name" in str(excinfo.value)


@pytest.mark.parametrize("url", ["https://example.test/mcp", "http://example.test/mcp"])
def test_accepts_an_http_endpoint_as_the_server_url(url: str) -> None:
    assert index.validate_server_url(url) == url


@pytest.mark.parametrize("url", ["example.test/mcp", "ftp://example.test", "", None])
def test_rejects_a_server_url_that_is_not_an_http_endpoint(url: Any) -> None:
    with pytest.raises(index.ValidationError):
        index.validate_server_url(url)


# ---------------------------------------------------------------------------
# Admin group check
# ---------------------------------------------------------------------------

ADMIN_ONLY = ["initOAuthFlow", "handleOAuthCallback"]


def _event(field: str, claims: Optional[Dict[str, Any]] = None, **input_fields: Any) -> dict:
    event: dict = {"info": {"fieldName": field}, "arguments": {"input": dict(input_fields)}}
    if claims is not None:
        event["identity"] = {"claims": claims}
    return event


@pytest.mark.parametrize("groups", [["Admin"], ["Users", "Admin"], "Admin", "Users,Admin"])
def test_admin_group_accepted_as_list_and_as_comma_joined_string(groups: Any) -> None:
    caller = index._get_caller_identity(_event("initOAuthFlow", {"cognito:groups": groups}))
    assert caller["is_admin"] is True


@pytest.mark.parametrize(
    "groups", [[], ["Users"], "Users", "Users,Guests", "Administrators", None, 7]
)
def test_non_admin_group_claims_are_not_admin(groups: Any) -> None:
    caller = index._get_caller_identity(_event("initOAuthFlow", {"cognito:groups": groups}))
    assert caller["is_admin"] is False


def test_absent_identity_is_not_admin() -> None:
    assert index._get_caller_identity({"info": {"fieldName": "initOAuthFlow"}})["is_admin"] is False


@pytest.mark.parametrize("field", ADMIN_ONLY)
@pytest.mark.parametrize("claims", [None, {}, {"cognito:groups": ["Users"]}, {"sub": "u1"}])
def test_oauth_fields_require_admin_group(field: str, claims: Optional[dict]) -> None:
    with pytest.raises(Exception, match="Unauthorized"):
        index.handler(_event(field, claims, serverId="s1"), None)


@pytest.mark.parametrize(
    ("field", "operation"),
    [
        ("initOAuthFlow", "init_oauth_flow"),
        ("handleOAuthCallback", "handle_oauth_callback"),
    ],
)
def test_oauth_fields_run_for_an_admin_caller(field: str, operation: str) -> None:
    event = _event(field, {"cognito:groups": ["Admin"], "cognito:username": "root"}, serverId="s1")

    with mock.patch.object(index, operation, return_value={"success": True}) as routed:
        assert index.handler(event, None) == {"success": True}

    routed.assert_called_once()


SIBLING = Path(__file__).resolve().parent.parent / "mcp_server_manager" / "index.py"


def _sibling_source() -> str:
    if not SIBLING.is_file():
        pytest.skip(f"{SIBLING} not found")
    return SIBLING.read_text(encoding="utf-8")


def test_the_admin_check_matches_the_mcp_server_manager_copy() -> None:
    """The two functions are packaged separately, so the check is duplicated.

    Nothing imports across function directories at runtime, so this compares the
    two copies; a change to one without the other fails here. The comparison is
    on the parsed tree rather than the source text, so reformatting one file or
    rewording a comment does not turn the suite red for no behavioural reason.
    """
    sibling_text = _sibling_source()

    def behaviour_of(text: str, names: tuple[str, ...]) -> Dict[str, str]:
        found = {
            node.name: ast.dump(node)
            for node in ast.parse(text).body
            if isinstance(node, ast.FunctionDef) and node.name in names
        }
        assert set(found) == set(names), f"missing {set(names) - set(found)}"
        return found

    shared = ("_get_caller_identity", "_require_admin")
    own_text = Path(index.__file__).read_text(encoding="utf-8")
    assert behaviour_of(own_text, shared) == behaviour_of(sibling_text, shared)


def test_the_per_account_server_limit_matches_the_mcp_server_manager_copy() -> None:
    assert f"MAX_SERVERS_PER_ACCOUNT = {index.MAX_SERVERS_PER_ACCOUNT}" in _sibling_source()


def test_the_length_bound_matches_the_mcp_server_manager_copy() -> None:
    """Both values bound the same stored field, so they have to be the same number."""
    assert f"MAX_PACKAGE_SPECIFIER_LENGTH = {index.MAX_SERVER_ID_LENGTH}" in _sibling_source()


def test_the_endpoint_pattern_matches_the_mcp_server_manager_copy() -> None:
    """Both validate the endpoint that ends up in the same stored field."""
    sibling_pattern = re.search(
        r"HTTP_ENDPOINT_PATTERN = re\.compile\(r\"(?P<pattern>[^\"]+)\"\)", _sibling_source()
    )
    assert sibling_pattern, "mcp_server_manager no longer defines HTTP_ENDPOINT_PATTERN"
    assert sibling_pattern.group("pattern") == index.HTTP_ENDPOINT_PATTERN.pattern


@pytest.mark.parametrize("control", ["\x00", "\x01", "\x1f", "\x7f", "\n", "\t", " "])
def test_an_endpoint_holds_no_whitespace_or_control_characters(control: str) -> None:
    with pytest.raises(index.ValidationError):
        index.validate_server_url(f"https://example.test/mcp{control}x")


# ---------------------------------------------------------------------------
# Writes to the account-wide servers table
# ---------------------------------------------------------------------------

ADMIN_CLAIMS = {"cognito:groups": ["Admin"], "cognito:username": "root"}

STATE_ROW = {
    "State": "state-1",
    "ServerId": "io.github.owner/server-name",
    "AccountId": "111122223333",
    "Provider": "example",
    "ClientId": "client-1",
    "TokenUrl": "https://example.test/token",
    "CodeChallenge": "challenge",
}


class _FakeTokenResponse:
    status_code = 200
    text = ""

    @staticmethod
    def json() -> Dict[str, Any]:
        return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}


def _tables(state_table: mock.Mock, servers_table: mock.Mock):
    """Return a Table() side effect that hands out the table asked for."""

    def table(name: str) -> mock.Mock:
        return state_table if name == index.OAUTH_STATE_TABLE else servers_table

    return table


def _init_event(**overrides: Any) -> dict:
    fields = {
        "serverId": "io.github.owner/server-name",
        "provider": "example",
        "clientId": "client-1",
        "authorizationUrl": "https://example.test/authorize",
        "tokenUrl": "https://example.test/token",
        "codeChallenge": "challenge",
    }
    fields.update(overrides)
    return _event("initOAuthFlow", ADMIN_CLAIMS, **fields)


def _init_tables(existing_servers: list, server_exists: bool = False) -> tuple:
    state_table = mock.Mock()
    servers_table = mock.Mock()
    servers_table.get_item.return_value = {"Item": {"ServerId": "s"}} if server_exists else {}
    servers_table.query.return_value = {"Items": existing_servers}
    return state_table, servers_table


def test_init_stores_state_for_an_accepted_identifier() -> None:
    state_table, servers_table = _init_tables([])

    with mock.patch.object(
        index.dynamodb, "Table", side_effect=_tables(state_table, servers_table)
    ):
        result = index.handler(_init_event(), None)

    assert "authorizationUrl" in result
    state_table.put_item.assert_called_once()


def test_init_refuses_when_the_account_is_at_the_server_limit() -> None:
    """The limit is reported before the user is sent to the provider."""
    existing = [{"ServerId": f"s{n}"} for n in range(index.MAX_SERVERS_PER_ACCOUNT)]
    state_table, servers_table = _init_tables(existing)

    with mock.patch.object(
        index.dynamodb, "Table", side_effect=_tables(state_table, servers_table)
    ):
        result = index.handler(_init_event(), None)

    assert result["success"] is False
    assert str(index.MAX_SERVERS_PER_ACCOUNT) in result["error"]
    state_table.put_item.assert_not_called()


def test_init_allows_an_installed_server_to_be_reauthorized_at_the_limit() -> None:
    """Configuring credentials for a server that already exists consumes no slot."""
    existing = [{"ServerId": f"s{n}"} for n in range(index.MAX_SERVERS_PER_ACCOUNT)]
    state_table, servers_table = _init_tables(existing, server_exists=True)

    with mock.patch.object(
        index.dynamodb, "Table", side_effect=_tables(state_table, servers_table)
    ):
        result = index.handler(_init_event(), None)

    assert "authorizationUrl" in result
    state_table.put_item.assert_called_once()


@pytest.mark.parametrize("server_id", ["a/b/c", "server one", "-server"])
def test_init_rejects_an_unacceptable_identifier_before_storing_state(server_id: str) -> None:
    state_table = mock.Mock()

    with mock.patch.object(index.dynamodb, "Table", return_value=state_table):
        result = index.handler(_init_event(serverId=server_id), None)

    assert result["success"] is False
    state_table.put_item.assert_not_called()


def test_init_rejects_a_server_url_that_is_not_an_http_endpoint() -> None:
    state_table = mock.Mock()

    with mock.patch.object(index.dynamodb, "Table", return_value=state_table):
        result = index.handler(_init_event(serverUrl="example.test/mcp"), None)

    assert result["success"] is False
    state_table.put_item.assert_not_called()


def _run_callback(state_row: Dict[str, Any], existing_servers: list) -> tuple:
    state_table = mock.Mock()
    state_table.get_item.return_value = {"Item": dict(state_row)}
    servers_table = mock.Mock()
    servers_table.get_item.return_value = {}
    servers_table.query.return_value = {"Items": existing_servers}

    event = _event("handleOAuthCallback", ADMIN_CLAIMS, code="c", state="state-1", codeVerifier="v")

    with (
        mock.patch.object(index.dynamodb, "Table", side_effect=_tables(state_table, servers_table)),
        mock.patch.object(index.requests, "post", return_value=_FakeTokenResponse()),
        mock.patch.object(index, "encrypt_token", return_value="encrypted"),
    ):
        result = index.handler(event, None)

    return result, servers_table, state_table


def test_callback_creates_a_server_row_for_an_accepted_identifier() -> None:
    result, servers_table, state_table = _run_callback(STATE_ROW, [])

    assert result["success"] is True
    servers_table.put_item.assert_called_once()
    item = servers_table.put_item.call_args.kwargs["Item"]
    assert item["ServerId"] == STATE_ROW["ServerId"]
    assert item["PackageType"] == "streamable-http"
    state_table.delete_item.assert_called_once()


def test_callback_honours_the_per_account_server_limit() -> None:
    existing = [{"ServerId": f"s{n}"} for n in range(index.MAX_SERVERS_PER_ACCOUNT)]

    result, servers_table, state_table = _run_callback(STATE_ROW, existing)

    assert result["success"] is False
    assert str(index.MAX_SERVERS_PER_ACCOUNT) in result["error"]
    servers_table.put_item.assert_not_called()
    state_table.delete_item.assert_called_once()


def test_callback_rejects_an_unacceptable_identifier_before_creating_a_row() -> None:
    result, servers_table, state_table = _run_callback({**STATE_ROW, "ServerId": "a/b/c"}, [])

    assert result["success"] is False
    servers_table.put_item.assert_not_called()
    state_table.delete_item.assert_called_once()


def test_callback_rejects_a_stored_server_url_that_is_not_an_http_endpoint() -> None:
    result, servers_table, state_table = _run_callback(
        {**STATE_ROW, "ServerUrl": "example.test/mcp"}, []
    )

    assert result["success"] is False
    servers_table.put_item.assert_not_called()
    state_table.delete_item.assert_called_once()


def test_callback_updating_an_existing_row_leaves_the_count_alone() -> None:
    state_table = mock.Mock()
    state_table.get_item.return_value = {"Item": dict(STATE_ROW)}
    servers_table = mock.Mock()
    servers_table.get_item.return_value = {"Item": {"ServerId": STATE_ROW["ServerId"]}}

    event = _event("handleOAuthCallback", ADMIN_CLAIMS, code="c", state="state-1", codeVerifier="v")

    with (
        mock.patch.object(index.dynamodb, "Table", side_effect=_tables(state_table, servers_table)),
        mock.patch.object(index.requests, "post", return_value=_FakeTokenResponse()),
        mock.patch.object(index, "encrypt_token", return_value="encrypted"),
    ):
        result = index.handler(event, None)

    assert result["success"] is True
    servers_table.update_item.assert_called_once()
    servers_table.put_item.assert_not_called()
    servers_table.query.assert_not_called()
