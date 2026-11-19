# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Unit tests for `lma mcp export` and `lma mcp import`."""

import json
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from lma_cli.cli import main

LIBRARY = {
    "kind": "lma-mcp-server-library",
    "version": 1,
    "exportedAt": "2026-11-19T00:00:00Z",
    "servers": [
        {"name": "Docs", "auth": {"authType": "bearer", "token": ""}, "credentialsToReenter": ["auth.token"]},
        {"name": "Open", "auth": {"authType": "none"}, "credentialsToReenter": []},
    ],
}


@pytest.fixture
def runner():
    return CliRunner()


def _run(runner, args, client):
    return runner.invoke(main, ["mcp", *args], obj={"client_factory": lambda **kw: client})


def test_export_writes_the_library_to_a_file(runner, tmp_path):
    client = MagicMock()
    client.mcp.export_library.return_value = LIBRARY
    target = tmp_path / "library.json"
    result = _run(runner, ["export", "--output", str(target)], client)
    assert result.exit_code == 0, result.output
    assert json.loads(target.read_text()) == LIBRARY
    assert "1 of them need their credentials entered again" in result.output


def test_export_writes_to_standard_output_by_default(runner):
    client = MagicMock()
    client.mcp.export_library.return_value = LIBRARY
    result = _run(runner, ["export"], client)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == LIBRARY


def test_import_sends_the_file_and_reports_servers_needing_credentials(runner, tmp_path):
    source = tmp_path / "library.json"
    source.write_text(json.dumps(LIBRARY))
    client = MagicMock()
    client.mcp.import_library.return_value = {
        "Success": True,
        "Message": "2 of 2 servers processed",
        "Servers": [
            {"Name": "Docs", "Action": "created", "CredentialsRequired": True, "Message": ""},
            {"Name": "Open", "Action": "updated", "CredentialsRequired": False, "Message": ""},
        ],
    }
    result = _run(runner, ["import", str(source)], client)
    assert result.exit_code == 0, result.output
    assert client.mcp.import_library.call_args.args[0] == LIBRARY
    assert "Docs: created (credentials must be entered on the MCP Servers page)" in result.output
    assert "Open: updated" in result.output


def test_import_of_a_file_that_is_not_json_fails_without_a_call(runner, tmp_path):
    source = tmp_path / "library.json"
    source.write_text("not json")
    client = MagicMock()
    result = _run(runner, ["import", str(source)], client)
    assert result.exit_code == 2
    client.mcp.import_library.assert_not_called()


def test_a_failed_import_exits_non_zero(runner, tmp_path):
    source = tmp_path / "library.json"
    source.write_text(json.dumps(LIBRARY))
    client = MagicMock()
    client.mcp.import_library.return_value = {"Success": False, "Message": "nope", "Servers": []}
    result = _run(runner, ["import", str(source)], client)
    assert result.exit_code == 1
