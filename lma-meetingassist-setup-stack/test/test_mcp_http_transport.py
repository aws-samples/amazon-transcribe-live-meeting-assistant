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

import asyncio
import contextlib
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


class _FakeHttpClient:
    def __init__(self, headers, events):
        self.headers = headers
        self.events = events

    async def __aenter__(self):
        self.events.append("client-open")
        return self

    async def __aexit__(self, *exc):
        self.events.append("client-closed")


def _current_api(events):
    def create_mcp_http_client(headers=None):
        return _FakeHttpClient(headers, events)

    @contextlib.asynccontextmanager
    async def streamable_http_client(url, *, http_client=None):
        events.append(("transport-open", url, http_client.headers))
        yield "streams"
        events.append("transport-closed")

    return {"create_mcp_http_client": create_mcp_http_client, "streamable_http_client": streamable_http_client}


async def _open(factory):
    async with factory() as streams:
        return streams


def test_current_api_passes_headers_through_the_http_client(monkeypatch):
    events = []
    _install_fake(monkeypatch, **_current_api(events))
    factory = mcp_server_loader.streamable_http_transport(
        "https://example.test/mcp", {"Authorization": "Bearer t"}
    )
    assert events == []  # nothing is opened until the client starts
    assert asyncio.run(_open(factory)) == "streams"
    assert events[1] == ("transport-open", "https://example.test/mcp", {"Authorization": "Bearer t"})


def test_the_http_client_is_closed_with_the_transport(monkeypatch):
    events = []
    _install_fake(monkeypatch, **_current_api(events))
    factory = mcp_server_loader.streamable_http_transport("https://example.test/mcp", None)
    asyncio.run(_open(factory))
    asyncio.run(_open(factory))
    assert [e for e in events if isinstance(e, str)] == [
        "client-open", "transport-closed", "client-closed",
        "client-open", "transport-closed", "client-closed",
    ]


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
    events = []
    _install_fake(monkeypatch, **_current_api(events))
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
    assert asyncio.run(_open(created["transport"])) == "streams"
    assert events[1] == ("transport-open", "https://mcp.deepwiki.com/mcp", {"X-Key": "k"})


def test_installed_mcp_offers_the_api_the_factory_uses():
    module = pytest.importorskip(MODULE)
    if hasattr(module, "streamable_http_client"):
        assert hasattr(module, "create_mcp_http_client")
    else:
        assert hasattr(module, "streamablehttp_client")
