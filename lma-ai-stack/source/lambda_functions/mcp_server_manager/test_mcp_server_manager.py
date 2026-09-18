#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.
"""Unit tests for the MCP server manager resolver (no AWS calls)."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("MCP_SERVERS_TABLE", "mcp-servers")
os.environ.setdefault("AWS_ACCOUNT_ID", "111122223333")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

sys.path.insert(0, str(Path(__file__).parent))

with mock.patch("boto3.resource"), mock.patch("boto3.client"):
    import index  # noqa: E402


# ---------------------------------------------------------------------------
# Package specifier validation
# ---------------------------------------------------------------------------

ACCEPTED_PYPI_SPECIFIERS = [
    "mcp-server-fetch",
    "awslabs.aws-documentation-mcp-server",
    "requests==2.31.0",
    "mcp>=1.0.0",
    "pkg<=3",
    "pkg~=1.2",
    "Django",
    "pkg==1.2.*",
    "pkg_with_underscores",
]

REJECTED_SPECIFIERS = [
    "requests; rm -rf /tmp",
    "requests && curl http://example.test",
    "requests|tee /tmp/out",
    "requests`id`",
    "requests$(id)",
    "requests${HOME}",
    "-r requirements.txt",
    "--index-url=http://example.test/simple pkg",
    "git+https://example.test/o/r.git",
    "file:///tmp/pkg",
    "https://example.test/pkg.tar.gz",
    "../../etc/passwd",
    "dir/pkg",
    "pkg\\other",
    "pkg other",
    "pkg\npkg2",
    "pkg\tname",
    "requests>=1.0,<2.0",
    "pkg[extra]",
    "pkg==1.0 --extra-index-url http://example.test",
    "pkg=1.0",
    "",
]


@pytest.mark.parametrize("specifier", ACCEPTED_PYPI_SPECIFIERS)
def test_accepts_plain_pypi_specifier(specifier: str) -> None:
    assert index.validate_package_specifier(specifier, "pypi") == specifier


@pytest.mark.parametrize("specifier", REJECTED_SPECIFIERS)
def test_rejects_specifier_that_is_not_a_bare_name_and_version(specifier: str) -> None:
    with pytest.raises(index.ValidationError):
        index.validate_package_specifier(specifier, "pypi")


@pytest.mark.parametrize("specifier", REJECTED_SPECIFIERS)
def test_rejects_the_same_values_for_npm(specifier: str) -> None:
    with pytest.raises(index.ValidationError):
        index.validate_package_specifier(specifier, "npm")


@pytest.mark.parametrize(
    "specifier",
    ["server-filesystem", "@modelcontextprotocol/server-filesystem", "server-everything@1.0.0"],
)
def test_accepts_npm_name_with_optional_scope_and_version(specifier: str) -> None:
    assert index.validate_package_specifier(specifier, "npm") == specifier


def test_npm_scope_separator_is_not_a_general_path_separator() -> None:
    """The single scope slash is allowed; anything else with a slash is not."""
    with pytest.raises(index.ValidationError):
        index.validate_package_specifier("@scope/name/extra", "npm")


def test_rejects_a_specifier_longer_than_the_limit() -> None:
    too_long = "a" * (index.MAX_PACKAGE_SPECIFIER_LENGTH + 1)
    with pytest.raises(index.ValidationError):
        index.validate_package_specifier(too_long, "pypi")


def test_rejects_a_non_string_specifier() -> None:
    with pytest.raises(index.ValidationError):
        index.validate_package_specifier({"name": "pkg"}, "pypi")


def test_error_message_names_the_accepted_form() -> None:
    with pytest.raises(index.ValidationError) as excinfo:
        index.validate_package_specifier("git+https://example.test/o/r.git", "pypi")
    assert "name==version" in str(excinfo.value)


def test_remote_servers_keep_working_with_a_url_in_the_package_field() -> None:
    url = "https://example.test/mcp"
    assert index.validate_package_specifier(url, "streamable-http") == url


def test_remote_server_endpoint_must_be_an_http_url() -> None:
    with pytest.raises(index.ValidationError):
        index.validate_package_specifier("not-a-url", "streamable-http")


# ---------------------------------------------------------------------------
# The build step applies an equivalent pattern
# ---------------------------------------------------------------------------

TEMPLATE = Path(__file__).resolve().parents[3] / "deployment" / "lma-ai-stack.yaml"


def _buildspec_pattern() -> re.Pattern:
    """Extract the regex the MCP layer buildspec applies to each stored value.

    The buildspec uses a POSIX ERE with `grep -E`; the subset used here has the
    same meaning under Python's `re`, so the two can be compared directly.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r"PACKAGE_SPEC_REGEX='(?P<pattern>[^']+)'", text)
    assert match, "buildspec no longer defines PACKAGE_SPEC_REGEX"
    return re.compile(match.group("pattern"))


@pytest.mark.parametrize("specifier", ACCEPTED_PYPI_SPECIFIERS)
def test_build_step_accepts_what_the_resolver_stores(specifier: str) -> None:
    assert _buildspec_pattern().search(specifier)


@pytest.mark.parametrize("specifier", [s for s in REJECTED_SPECIFIERS if s])
def test_build_step_skips_what_the_resolver_would_have_rejected(specifier: str) -> None:
    # `search` mirrors grep's line semantics; the pattern is anchored with ^...$.
    assert not _buildspec_pattern().search(specifier)


# ---------------------------------------------------------------------------
# Admin group check
# ---------------------------------------------------------------------------


def _event(field: str, claims: dict | None = None, **arguments: object) -> dict:
    event: dict = {"info": {"fieldName": field}, "arguments": arguments}
    if claims is not None:
        event["identity"] = {"claims": claims}
    return event


ADMIN_ONLY = ["installMCPServer", "uninstallMCPServer", "updateMCPServer"]


@pytest.mark.parametrize("groups", [["Admin"], ["Users", "Admin"], "Admin", "Users,Admin"])
def test_admin_group_accepted_as_list_and_as_comma_joined_string(groups: object) -> None:
    caller = index._get_caller_identity(_event("installMCPServer", {"cognito:groups": groups}))
    assert caller["is_admin"] is True


@pytest.mark.parametrize(
    "groups", [[], ["Users"], "Users", "Users,Guests", "Administrators", None, 7]
)
def test_non_admin_group_claims_are_not_admin(groups: object) -> None:
    caller = index._get_caller_identity(_event("installMCPServer", {"cognito:groups": groups}))
    assert caller["is_admin"] is False


def test_absent_identity_is_not_admin() -> None:
    assert index._get_caller_identity({"info": {"fieldName": "installMCPServer"}})["is_admin"] is (
        False
    )


@pytest.mark.parametrize("field", ADMIN_ONLY)
@pytest.mark.parametrize("claims", [None, {}, {"cognito:groups": ["Users"]}, {"sub": "u1"}])
def test_management_fields_require_admin_group(field: str, claims: dict | None) -> None:
    with pytest.raises(Exception, match="Unauthorized"):
        index.handler(_event(field, claims, serverId="s1"), None)


@pytest.mark.parametrize(
    ("field", "operation"),
    [
        ("installMCPServer", "install_mcp_server"),
        ("uninstallMCPServer", "uninstall_mcp_server"),
        ("updateMCPServer", "update_mcp_server"),
    ],
)
def test_management_fields_run_for_an_admin_caller(field: str, operation: str) -> None:
    event = _event(field, {"cognito:groups": ["Admin"], "cognito:username": "root"}, serverId="s1")

    with mock.patch.object(index, operation, return_value={"Success": True}) as routed:
        assert index.handler(event, None) == {"Success": True}

    routed.assert_called_once()


def test_read_fields_do_not_require_admin() -> None:
    event = _event("listInstalledMCPServers", {"cognito:groups": ["Users"]})
    with mock.patch.object(index, "list_installed_servers", return_value=[]) as listed:
        assert index.handler(event, None) == []
    assert listed.called


# ---------------------------------------------------------------------------
# Read path projection
# ---------------------------------------------------------------------------

STORED_ROW = {
    "AccountId": "111122223333",
    "ServerId": "s1",
    "Name": "Example",
    "NpmPackage": "mcp-server-fetch",
    "RequiresAuth": True,
    "AuthConfig": '{"authType": "bearer", "token": "s3cret"}',
    "Status": "ACTIVE",
}


def test_read_path_reports_presence_instead_of_credentials() -> None:
    projected = index.redact_server(STORED_ROW)

    assert "AuthConfig" not in projected
    assert projected["HasAuthConfig"] is True
    assert projected["Name"] == "Example"
    assert "s3cret" not in str(projected)


def test_read_path_reports_no_credentials_when_none_are_stored() -> None:
    projected = index.redact_server({k: v for k, v in STORED_ROW.items() if k != "AuthConfig"})

    assert projected["HasAuthConfig"] is False


def test_redaction_leaves_a_missing_row_alone() -> None:
    assert index.redact_server(None) is None


def test_list_and_get_project_every_row() -> None:
    table = mock.Mock()
    table.query.return_value = {"Items": [dict(STORED_ROW), dict(STORED_ROW)]}
    table.get_item.return_value = {"Item": dict(STORED_ROW)}

    with mock.patch.object(index.dynamodb, "Table", return_value=table):
        listed = index.list_installed_servers({}, None)
        fetched = index.get_mcp_server({"arguments": {"serverId": "s1"}}, None)

    assert listed and all("AuthConfig" not in row for row in listed)
    assert "AuthConfig" not in fetched
    assert fetched["HasAuthConfig"] is True


def test_install_rejects_an_unacceptable_specifier_before_storing_anything() -> None:
    table = mock.Mock()
    event = {
        "info": {"fieldName": "installMCPServer"},
        "identity": {"claims": {"cognito:groups": ["Admin"]}},
        "arguments": {
            "input": {
                "ServerId": "s1",
                "Name": "Example",
                "NpmPackage": "git+https://example.test/o/r.git",
                "PackageType": "pypi",
            }
        },
    }

    with mock.patch.object(index.dynamodb, "Table", return_value=table):
        result = index.handler(event, None)

    assert result["Success"] is False
    assert "name==version" in result["Message"]
    table.put_item.assert_not_called()
    table.query.assert_not_called()
