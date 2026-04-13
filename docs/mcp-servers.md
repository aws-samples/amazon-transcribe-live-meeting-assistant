# MCP Servers

## Table of Contents

- [Overview](#overview)
- [What is MCP](#what-is-mcp)
- [Authentication Methods](#authentication-methods)
- [Admin UI](#admin-ui)
- [Built-in LMA MCP Tools](#built-in-lma-mcp-tools)
- [MCP Layer Rebuild](#mcp-layer-rebuild)
- [OAuth Callback URLs](#oauth-callback-urls)
- [Available Integrations](#available-integrations)

## Overview

LMA's Strands agent supports dynamic loading of MCP (Model Context Protocol) servers, enabling integration with external tools and services. MCP extends the meeting assistant's capabilities beyond its built-in tools, allowing it to interact with CRMs, scheduling platforms, code repositories, and more during live meetings.

## What is MCP

The Model Context Protocol (MCP) is an open protocol for connecting AI assistants to external tools and data sources. Rather than hard-coding every integration, MCP provides a standardized way for AI systems to discover and invoke tools at runtime.

LMA supports MCP servers that provide tools the Strands agent can call during meetings. When an MCP server is installed, its tools become available to the meeting assistant automatically. For example, installing a Salesforce MCP server lets the assistant look up customer records, while a scheduling MCP server enables booking meetings directly from the conversation.

## Authentication Methods

LMA supports several authentication methods for connecting to MCP servers:

- **OAuth 2.1 with PKCE** (recommended for web services) — The most secure option for browser-based OAuth flows. Uses Proof Key for Code Exchange to prevent authorization code interception.
- **OAuth 2.0 fallback** — Standard OAuth 2.0 authorization code flow for services that do not yet support OAuth 2.1.
- **Bearer token** — Simple token-based authentication for services that issue long-lived API tokens.
- **Custom header authentication** — Allows specifying arbitrary authentication headers for services with non-standard auth schemes.
- **OAuth Client Credentials** (machine-to-machine) — For server-to-server integrations that do not require user interaction.
- **Automatic token refresh** — LMA automatically refreshes OAuth tokens before they expire, ensuring uninterrupted access to MCP server tools during long meetings.

## Admin UI

The MCP Servers configuration page is available at `/#/configuration/mcp-servers` and is accessible to admin users.

The page contains two tabs:

### Public Registry

Browse and install MCP servers from the public registry at [modelcontextprotocol.io](https://modelcontextprotocol.io). The registry provides a curated list of MCP servers with descriptions, available tools, and installation instructions. You can search, install, update, and uninstall servers directly from this tab.

### Custom Servers

Configure custom MCP server endpoints that are not listed in the public registry. This is useful for internal or proprietary MCP servers deployed within your organization.

### Key Features

- View the list of available tools provided by each installed MCP server
- Install, update, and uninstall MCP servers
- Maximum of **5 MCP servers** per account

## Built-in LMA MCP Tools

The following tools are available to the Strands agent without installing any additional MCP servers:

| Tool | Description |
|------|-------------|
| `list_meetings` | List recent meetings |
| `search_lma_meetings` | Semantic search across meetings |
| `get_meeting_summary` | Get summary for a specific meeting |
| `get_meeting_transcript` | Get transcript for a specific meeting |
| `start_meeting_now` | Start a new Virtual Participant meeting |
| `schedule_meeting` | Schedule a future VP meeting |

These built-in tools give the meeting assistant access to your meeting history and the ability to manage Virtual Participant sessions without any additional configuration.

## MCP Layer Rebuild

LMA uses a CloudFormation Custom Resource to automatically rebuild the MCP Lambda layer on stack create and update operations. This ensures that the correct native binaries are compiled for the Lambda execution environment.

Installed MCP servers and their configurations are preserved across stack updates, so you do not need to reinstall or reconfigure your MCP servers after updating LMA.

## OAuth Callback URLs

OAuth callback URLs are configurable for:

- **Quick Suite** — Pre-configured callback URL for Amazon Quick Suite OAuth flows
- **Custom OAuth clients** — Configurable callback URLs for third-party OAuth providers

These callback URLs are used during the OAuth authorization flow to redirect the user back to the LMA application after granting permissions.

## Available Integrations

LMA provides setup guides for several popular MCP server integrations:

- [Salesforce MCP Setup](salesforce-mcp-setup.md) — Full CRUD operations on Salesforce objects (accounts, contacts, opportunities, leads, and more)
- [Amazon Quick Suite MCP Setup](quicksuite-mcp-setup.md) — Search, retrieval, and scheduling capabilities across Amazon Quick Suite services
- [DeepWiki MCP Setup](deepwiki-mcp-setup.md) — Repository documentation search for accessing code documentation during meetings
- **Custom MCP servers** — Install from the public registry or configure a custom endpoint

---

See also: [Meeting Assistant](meeting-assistant.md)
