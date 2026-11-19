# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT-0 License.
"""Unit tests for client.mcp: the MCP server library export and import."""

import json
from unittest.mock import MagicMock

import pytest

from lma_sdk.exceptions import LMAAppSyncError

LIBRARY = {
    "kind": "lma-mcp-server-library",
    "version": 1,
    "exportedAt": "2026-11-19T00:00:00Z",
    "servers": [{"name": "Docs", "auth": {"authType": "bearer", "token": ""}}],
}


@pytest.fixture
def appsync(client):
    mock = MagicMock(name="appsync")
    client._appsync_ops = mock
    return mock


def test_the_export_is_read_from_the_stack_and_parsed(client, appsync):
    appsync.graphql.return_value = {"exportMCPServerLibrary": json.dumps(LIBRARY)}
    assert client.mcp.export_library() == LIBRARY
    assert "exportMCPServerLibrary" in appsync.graphql.call_args.args[0]


def test_an_export_that_is_not_a_library_is_refused(client, appsync):
    appsync.graphql.return_value = {"exportMCPServerLibrary": json.dumps({"kind": "other"})}
    with pytest.raises(LMAAppSyncError):
        client.mcp.export_library()


@pytest.mark.parametrize("given", [LIBRARY, json.dumps(LIBRARY)], ids=["dict", "text"])
def test_the_import_sends_the_library_as_json(client, appsync, given):
    appsync.graphql.return_value = {
        "importMCPServerLibrary": {"Success": True, "Servers": [{"Name": "Docs", "Action": "created"}]}
    }
    result = client.mcp.import_library(given)
    assert result["Success"] is True
    variables = appsync.graphql.call_args.kwargs["variables"]
    assert json.loads(variables["library"]) == LIBRARY


@pytest.mark.parametrize(
    "document",
    [{"kind": "lma-skill-library", "version": 1}, {**LIBRARY, "version": 2}, []],
    ids=["other-kind", "other-version", "not-an-object"],
)
def test_a_document_that_is_not_a_library_is_refused_before_any_call(client, appsync, document):
    with pytest.raises(ValueError):
        client.mcp.import_library(document)
    appsync.graphql.assert_not_called()
