#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""The streamable HTTP transport the agent opens to an HTTP MCP server.

mcp 2.x renamed ``streamablehttp_client`` to ``streamable_http_client`` and
moved request headers onto the HTTP client it is given; the agent's layer takes
whichever mcp release strands-agents resolves to. These tests run the factory
against stand-ins for both APIs, and, when mcp is installed, check that the
installed release still offers one of them.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

import mcp_server_loader  # noqa: E402

MODULE = "mcp.client.streamable_http"


def _install_fake(monkeypatch, **attrs):
    fake = types.ModuleType(MODULE)
    for name, value in attrs.items():
        setattr(fake, name, value)
    for parent in ("mcp", "mcp.client"):
        monkeypatch.setitem(sys.modules, parent, sys.modules.get(parent) or types.ModuleType(parent))
    monkeypatch.setitem(sys.modules, MODULE, fake)


def test_current_api_passes_headers_through_the_http_client(monkeypatch):
    calls = []

    def create_mcp_http_client(headers=None):
        return ("http-client", headers)

    def streamable_http_client(url, *, http_client=None):
        calls.append((url, http_client))
        return "transport"

    _install_fake(
        monkeypatch,
        create_mcp_http_client=create_mcp_http_client,
        streamable_http_client=streamable_http_client,
    )
    factory = mcp_server_loader.streamable_http_transport(
        "https://example.test/mcp", {"Authorization": "Bearer t"}
    )
    assert calls == []  # nothing is opened until the client starts
    assert factory() == "transport"
    assert calls == [("https://example.test/mcp", ("http-client", {"Authorization": "Bearer t"}))]


def test_earlier_api_is_used_when_the_current_one_is_absent(monkeypatch):
    calls = []

    def streamablehttp_client(url, headers=None):
        calls.append((url, headers))
        return "old-transport"

    _install_fake(monkeypatch, streamablehttp_client=streamablehttp_client)
    factory = mcp_server_loader.streamable_http_transport("https://example.test/mcp", None)
    assert factory() == "old-transport"
    assert calls == [("https://example.test/mcp", {})]


def test_load_http_mcp_server_builds_a_client(monkeypatch):
    _install_fake(
        monkeypatch,
        create_mcp_http_client=lambda headers=None: headers,
        streamable_http_client=lambda url, *, http_client=None: (url, http_client),
    )
    created = {}

    class FakeMCPClient:
        def __init__(self, transport, prefix):
            created["transport"] = transport
            created["prefix"] = prefix

    tools_mcp = types.ModuleType("strands.tools.mcp")
    tools_mcp.MCPClient = FakeMCPClient
    for parent in ("strands", "strands.tools"):
        monkeypatch.setitem(sys.modules, parent, sys.modules.get(parent) or types.ModuleType(parent))
    monkeypatch.setitem(sys.modules, "strands.tools.mcp", tools_mcp)

    client = mcp_server_loader.load_http_mcp_server(
        "custom/deepwiki", "https://mcp.deepwiki.com/mcp", {"headers": {"X-Key": "k"}}
    )
    assert isinstance(client, FakeMCPClient)
    assert created["prefix"] == "custom_deepwiki"
    assert created["transport"]() == ("https://mcp.deepwiki.com/mcp", {"X-Key": "k"})


def test_installed_mcp_offers_a_streamable_http_client():
    module = pytest.importorskip(MODULE)
    assert hasattr(module, "streamable_http_client") or hasattr(module, "streamablehttp_client")
