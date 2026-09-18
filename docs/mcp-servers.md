---
title: "MCP Servers"
---

# MCP Servers

## Table of Contents

- [Overview](#overview)
- [What is MCP](#what-is-mcp)
- [Authentication Methods](#authentication-methods)
- [Admin UI](#admin-ui)
- [Package Specifiers](#package-specifiers)
- [Stored Server Credentials](#stored-server-credentials)
- [Built-in LMA MCP Tools](#built-in-lma-mcp-tools)
- [MCP Layer Rebuild](#mcp-layer-rebuild)
- [OAuth Callback URLs](#oauth-callback-urls)
- [Available Integrations](#available-integrations)
- [Upgrade Notes](#upgrade-notes)

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

The MCP Servers configuration page is available at `/#/configuration/mcp-servers`.

Installed MCP servers are account-wide — they apply to every meeting — so installing, updating and uninstalling them, and configuring a server's OAuth credentials, are reserved for members of the **Admin** Cognito group. The `installMCPServer`, `updateMCPServer`, `uninstallMCPServer`, `initOAuthFlow` and `handleOAuthCallback` GraphQL operations accept Admin callers only, and the `MCPServerManager` and `OAuthManager` Lambda functions each independently re-check the caller's group membership before acting. Signed-in users who are not in the Admin group see the installed servers read-only: the Public Registry and Custom Server tabs and the per-server Update/Uninstall actions are not shown. The per-user connection details on the **Hosted MCP Access** tab remain available to every user.

The page contains two tabs:

### Public Registry

Browse and install MCP servers from the public registry at [modelcontextprotocol.io](https://modelcontextprotocol.io). The registry provides a curated list of MCP servers with descriptions, available tools, and installation instructions. You can search, install, update, and uninstall servers directly from this tab.

### Custom Servers

Configure custom MCP server endpoints that are not listed in the public registry. This is useful for internal or proprietary MCP servers deployed within your organization.

### Key Features

- View the list of available tools provided by each installed MCP server
- Install, update, and uninstall MCP servers (Admin group)
- Maximum of **5 MCP servers** per account

## Package Specifiers

A server that LMA installs into its MCP Lambda layer is identified by a published package name, optionally with a single version pin. The accepted forms are:

| Package type | Accepted forms |
|--------------|----------------|
| `pypi` | `name`, `name==version`, `name>=version`, `name<=version`, `name~=version` |
| `npm` | `name`, `@scope/name`, `name@version` |
| `streamable-http` | an `http://` or `https://` endpoint URL (nothing is installed) |

Names and versions may contain letters, digits, `.`, `_` and `-`. Anything else — a registry URL, a local path, a `git+` reference, extras such as `pkg[extra]`, several specifiers on one line, or pip flags — is rejected with a message naming the accepted form, rather than being rewritten into something acceptable. The same pattern is applied again by the layer build, which logs and skips any stored entry that is not a plain package specifier, so installing a server by name is the only supported route.

## Stored Server Credentials

Servers that require authentication store their credential material (bearer token, OAuth client credentials, custom headers, environment variables) in the `AuthConfig` field of the MCP servers DynamoDB table. That field is write-only from the API's point of view: it is supplied on `installMCPServer`/`updateMCPServer` and read at runtime directly from DynamoDB by the meeting-assist function, and it is not part of the `MCPServer` type that `listInstalledMCPServers` returns. The read path returns `HasAuthConfig: Boolean` instead, so a client can tell whether a server has credentials configured without receiving them.

If you have built a custom client that selected `AuthConfig` from `listInstalledMCPServers`, select `HasAuthConfig` instead and re-enter the credential through the UI (or an `updateMCPServer` call) if you need to change it. The LMA UI never read the field, so no change is needed for the bundled UI.

## Built-in LMA MCP Tools

The following tools are available to the Strands agent without installing any additional MCP servers:

| Tool | Description |
|------|-------------|
| `list_meetings` | List recent meetings |
| `search_lma_meetings` | Semantic search across meetings |
| `get_meeting_summary` | Get summary for a specific meeting |
| `get_meeting_transcript` | Get transcript for a specific meeting |
| `start_meeting_now` | Launch a Virtual Participant into a meeting now (Zoom, Teams, Webex, Chime, Google Meet). Defaults to using stored Zoom credentials when present so the VP signs in to Zoom rather than joining as a guest. |
| `get_virtual_participant_status` | Poll the status of a VP launched via `start_meeting_now`. Returns the granular status (e.g. `JOINING`, `MANUAL_ACTION_REQUIRED`, `ACTIVE`, `FAILED`), a human-readable summary, the live VNC viewer URL, the meeting URL, and any `errorMessage` or `manualActionMessage`. Designed to be polled by the agent after a `start_meeting_now` call so it can verbalize *"the VP is in the meeting"* — or surface a CAPTCHA/2FA challenge to the user with the viewer URL — without further prompting. |
| `schedule_meeting` | Schedule a future VP meeting |

These built-in tools give the meeting assistant access to your meeting history and the ability to manage Virtual Participant sessions without any additional configuration.

When `start_meeting_now` returns a VP id, the agent should poll `get_virtual_participant_status` periodically until the status reaches `ACTIVE` (success) or `FAILED` (with `errorMessage`) or `MANUAL_ACTION_REQUIRED` (in which case the agent should surface the `manualActionMessage` and `virtualParticipantUrl` to the user so they can complete the challenge). See [Virtual Participant › Status Lifecycle](virtual-participant.md#status-lifecycle) and [Manual Action Required](virtual-participant.md#manual-action-required-captcha-2fa-sso) for the full state machine.

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
- [Amazon Quick MCP Setup](amazon-quick-mcp-setup.md) — Connect LMA to Amazon Quick Suite (web, OAuth) or Quick Desktop (native, API key)
- [DeepWiki MCP Setup](deepwiki-mcp-setup.md) — Repository documentation search for accessing code documentation during meetings
- **Custom MCP servers** — Install from the public registry or configure a custom endpoint

## Upgrade Notes

No data migration is required, but on an existing deployment note the following after updating:

- **Servers already installed stay installed.** Every row in the MCP servers table is kept as-is and the Strands agent keeps loading those servers. What changes is who can alter the set: from this release the install, update, uninstall and OAuth mutations accept callers in the **Admin** group. If a server was installed or authorized by someone who is not in that group, add them to the Admin group (User Management page) or have an administrator make future changes.
- **A server whose OAuth credential expires needs an Admin user to re-authorize it.** The OAuth flow is started from the MCP Servers page, which is where the Admin-group requirement applies; a completed flow still refreshes tokens automatically without further interaction.
- **`AuthConfig` is no longer part of the read API.** `listInstalledMCPServers` and `getMCPServer` return `HasAuthConfig: Boolean` in its place. Stored credentials are untouched and are still used at runtime; only the API response shape changed. Update any custom client that selected `AuthConfig`. See [Stored Server Credentials](#stored-server-credentials).
- **Stored package specifiers are re-checked at build time.** If an existing row holds something other than a plain package specifier (see [Package Specifiers](#package-specifiers)), the MCP layer build logs `SKIPPING entry that is not a plain package specifier` and carries on rather than failing the stack update. Uninstall that server and reinstall it by published package name.
- **External MCP clients must connect with a user identity.** See [MCP API Key Authentication](mcp-api-key-auth.md#connecting-with-a-user-identity).

---

See also: [Meeting Assistant](meeting-assistant.md)
