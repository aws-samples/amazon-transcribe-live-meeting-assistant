---
title: "MCP API Key Authentication"
---

# MCP Server API Key Authentication

## Overview

LMA's MCP server supports two authentication methods, both deployed automatically when MCP is enabled:

1. **3LO (OAuth)** via Cognito + BedrockAgentCore Gateway — for interactive clients like Amazon Quick Suite and Claude Desktop
2. **API Key** via REST API Gateway + Lambda authorizer — for headless/programmatic MCP clients, or Quick Suite via bearer token

Users generate personal API keys from the LMA UI (MCP Servers Configuration page). The API key endpoint speaks the full MCP JSON-RPC 2.0 protocol (streamable HTTP), so standard MCP clients can connect directly.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     MCP Clients                             │
│  (Quick Suite, Claude Desktop, curl, custom clients)        │
└──────────┬──────────────────────────┬───────────────────────┘
           │ OAuth (3LO)              │ API Key (Bearer / x-api-key)
           ▼                          ▼
┌─────────────────────┐   ┌──────────────────────────────┐
│ BedrockAgentCore    │   │ REST API Gateway             │
│ Gateway (CUSTOM_JWT)│   │ (REQUEST authorizer)         │
└─────────┬───────────┘   └──────────┬───────────────────┘
          │                          │
          │                ┌─────────▼──────────────┐
          │                │ MCPApiKeyAuthorizer     │
          │                │ Lambda                  │
          │                │ - Checks Authorization: │
          │                │   Bearer or x-api-key   │
          │                │ - SHA-256 hash → DynamoDB│
          │                │ - Returns user context   │
          │                └─────────┬───────────────┘
          │                          │
          ▼                          ▼
┌─────────────────────────────────────────────────────────────┐
│                MCPServerAnalyticsFunction                    │
│  - BedrockAgentCore path: raw tool input from gateway       │
│  - API Gateway path: full MCP JSON-RPC 2.0 protocol         │
│    (initialize, tools/list, tools/call, ping)               │
│  - UBAC enforced on both paths                              │
└─────────────────────────────────────────────────────────────┘
```

## Connecting with a User Identity

Every tool the MCP server exposes is scoped to the calling user: a request returns that user's meetings, and the scope widens to all meetings in the account only for a member of the **Admin** group. `MCPServerAnalyticsFunction` therefore resolves a caller from each request and refuses the request when it cannot. What it reads from the event:

- **API key path** — `requestContext.authorizer.userId`, `username` and `isAdmin`, which the REQUEST authorizer put there after resolving the API key.
- **OAuth (3LO) path** — `requestContext.authorizer.claims`, taking the user from `sub`, the display name from `cognito:username` (or `email`) and group membership from `cognito:groups`.

A request on either path that does not resolve to a user is answered with `403` and a message naming the API key endpoint. A client that receives that response should connect to the `MCPServerApiKeyEndpoint` REST endpoint with a personal API key, which supplies a caller on every request. See [Key Generation Flow](#key-generation-flow) and [Connecting from Quick Suite](#connecting-from-quick-suite).

> **To confirm on a deployed stack:** whether the BedrockAgentCore Gateway (`CUSTOM_JWT` authorizer, `GATEWAY_IAM_ROLE` credential provider) includes the caller's Cognito claims in the event it sends to its Lambda target has not been verified against a live deployment. Where it does not, the OAuth path returns the `403` described above and the API key endpoint is the route that works for those clients. Worth confirming before a release.

Group membership is read from the `cognito:groups` claim, which Cognito supplies either as a list or as a comma-separated string; both are accepted, and the comparison is an exact match on a whole group name (a group called `Administrators`, for instance, is not the `Admin` group).

## Request Flow (API Key Path)

```
MCP client connects to REST API Gateway endpoint
  → POST with Authorization: Bearer <key> (or x-api-key: <key>)
  → REQUEST authorizer Lambda invoked (no IdentitySource — always invoked)
    → Extracts token from Authorization header (strips "Bearer ") or x-api-key header
    → SHA-256 hashes the token
    → Looks up hash in MCPApiKeysTable (DynamoDB)
    → Returns IAM Allow policy + { userId, username, isAdmin } in context
  → MCPServerAnalyticsFunction receives API Gateway proxy event
    → Detects JSON-RPC 2.0 message (jsonrpc + method fields in body)
    → Routes to MCP protocol handler:
      - initialize → returns server capabilities + protocolVersion
      - notifications/initialized → empty 200 acknowledgment
      - tools/list → returns 8 tool definitions with inputSchema
      - tools/call → routes to tool implementation, returns MCP result
      - ping → returns empty result
    → User context from authorizer enforces UBAC on tool calls
```

## Key Generation Flow

```
User clicks "Generate API Key" on MCP Servers Configuration page
  → AppSync mutation: generateMCPApiKey
  → MCPApiKeyManagerFunction Lambda
    → Verifies user doesn't already have a key (queries UserIdIndex GSI)
    → Generates key: "lma_" + uuid4
    → SHA-256 hashes the key
    → Stores in DynamoDB: { KeyHash (PK), UserId, Username, IsAdmin, CreatedAt, KeyPrefix, Enabled }
    → Returns plaintext key to user (shown once in modal with copy-to-clipboard)
```

## Files

| File | Purpose |
|------|---------|
| `lma-ai-stack/deployment/lma-ai-stack.yaml` | All CF resources: MCPApiKeysTable, MCPApiKeyAuthorizerFunction, MCPApiKeyManagerFunction, MCPApiKeyRestApi (REQUEST authorizer, no IdentitySource), AppSync data source + 3 resolvers, MCPServerApiKeyEndpoint output |
| `lma-main.yaml` | MCPServerApiKeyEndpoint output (pass-through from nested stack) |
| `lma-ai-stack/source/appsync/schema.graphql` | MCPApiKey type, GenerateMCPApiKeyOutput type, generateMCPApiKey/revokeMCPApiKey mutations, listMCPApiKeys query |
| `lma-ai-stack/source/lambda_functions/mcp_api_key_authorizer/index.py` | REQUEST authorizer — accepts both `Authorization: Bearer` and `x-api-key` headers, hashes token, DynamoDB lookup, returns IAM policy + user context |
| `lma-ai-stack/source/lambda_functions/mcp_api_key_manager/index.py` | AppSync resolver — generate/list/revoke per-user API keys, one key per user enforced |
| `lma-ai-stack/source/lambda_functions/mcp_analytics/index.py` | MCP JSON-RPC 2.0 protocol handler (initialize, tools/list, tools/call, ping) for the API Gateway path; on the BedrockAgentCore path, resolves the caller from the forwarded claims (see [Connecting with a User Identity](#connecting-with-a-user-identity)) |
| `lma-ai-stack/source/ui/src/graphql/mutations.js` | generateMCPApiKey, revokeMCPApiKey, listMCPApiKeys GraphQL operations |
| `lma-ai-stack/source/ui/src/components/mcp-servers-page/MCPApiKeySection.jsx` | Cloudscape UI component for key management |
| `lma-ai-stack/source/ui/src/components/mcp-servers-page/MCPServersPage.jsx` | Integrated MCPApiKeySection at top of page |
| `utilities/test-mcp-api-key.sh` | End-to-end test script for the API key endpoint |
| `docs/mcp-api-key-auth.md` | This document |

## MCP Protocol Support

The API key endpoint implements the MCP streamable HTTP transport (JSON-RPC 2.0 over HTTP POST). Supported methods:

| Method | Description |
|--------|-------------|
| `initialize` | Returns server info (name: `lma-mcp-server`) and capabilities |
| `notifications/initialized` | Client acknowledgment, returns empty 200 |
| `tools/list` | Returns all 8 tool definitions with inputSchema |
| `tools/call` | Executes a tool by name with arguments, returns MCP content result |
| `ping` | Health check, returns empty result |

### Tools Exposed

| Tool | Description |
|------|-------------|
| `search_lma_meetings` | Search transcripts/summaries with natural language |
| `get_meeting_transcript` | Get full transcript (json, text, or srt format) |
| `get_meeting_summary` | Get AI summary with action items and topics |
| `list_meetings` | List meetings with date and status filters |
| `list_active_meetings` | List the meetings that are running now (API key endpoint only) |
| `schedule_meeting` | Schedule a future meeting with virtual participant |
| `start_meeting_now` | Start a meeting immediately with virtual participant |
| `get_virtual_participant_status` | Poll the status of a virtual participant launched by `start_meeting_now` |

### `list_active_meetings`

Lists the meetings that are running now, so that a client knows which meeting to ask about. It is offered by the API key endpoint only: the OAuth gateway keeps its seven tools.

| Argument | Description |
|----------|-------------|
| `limit` | How many meetings to return. Default 10, from 1 to 50; a number outside that range is moved to the nearest end. A whole number, a numeric string such as `"5"`, or a float with no fraction is accepted. A boolean, text that is not a number, a fraction and a value that is not finite are refused with `limit must be an integer`. |

A meeting is listed when its status is `STARTED` or `TRANSCRIBING`, it was created in the last 24 hours, and the caller may read it. An uploaded recording is `TRANSCRIBING` while it is processed; it is not a meeting that is being held and is not listed. The newest meeting comes first. Each meeting has:

| Field | Value |
|-------|-------|
| `meetingId` | The id to pass to other tools |
| `meetingName` | The meeting's name |
| `status` | `STARTED` or `TRANSCRIBING` |
| `startedAt` | When the meeting was created |
| `lastActivityAt` | When the meeting's record was last updated, or `startedAt` when it never was. A meeting whose last activity is long ago may have ended without being closed; LMA ends such a meeting after several hours |
| `owner` | The meeting's owner |
| `consent` | `{mode, status, captureAllowed}` from the meeting's consent record, or `null` when it has none or when the record holds none of those three fields |
| `meetingUrl`, `virtualParticipantUrl` | Links into the LMA web app |

No transcript text is returned. The answer is `{"meetings": [...], "count": n}`.

**Who sees what.** An Admin sees every running meeting. Anyone else sees the meetings they own (named as the meeting's owner or its agent, as `list_meetings` decides) and the meetings shared with them. This is wider than `list_meetings`, `get_meeting_transcript` and `get_meeting_summary`, which show a person only the meetings they own.

**When the list may be incomplete.** At most 1,000 of the most recent meetings of the last 24 hours are checked. When that limit ended the search and there were more meetings left, the answer has `"truncated": true` and a `note`; a running meeting that started earlier in the window may then be missing. When some meetings could not be read the `note` says "Some meetings could not be read; try again." If the lookup fails, the error is `Failed to list active meetings`. A meeting whose start time was stamped by a device with a clock ahead of the server's is listed only once the server's clock reaches that time.

## DynamoDB Table Schema

**Table: `${LMAStackName}-MCPApiKeys`**

| Attribute | Type | Description |
|-----------|------|-------------|
| `KeyHash` | S (PK) | SHA-256 hash of the API key |
| `UserId` | S | Cognito username |
| `Username` | S | Cognito username/email |
| `IsAdmin` | S | `"true"` or `"false"` |
| `KeyPrefix` | S | First 12 chars of key (e.g. `lma_a1b2c3d4`) for UI display |
| `CreatedAt` | S | ISO 8601 timestamp |
| `Enabled` | S | `"true"` or `"false"` |

**GSI: `UserIdIndex`** — Partition key: `UserId`, projects all attributes. Used for listing a user's keys and enforcing one-key-per-user.

## API Gateway Authorizer Details

- **Type**: `REQUEST` (not TOKEN) — receives all headers, no IdentitySource filter
- **TTL**: 0 (no caching — since the identity source varies between headers)
- **Token extraction order**: `authorizationToken` (TOKEN mode compat) → `headers.x-api-key` → `headers.Authorization` (strips `Bearer ` prefix)
- **On success**: returns IAM Allow policy with `context: { userId, username, isAdmin }`
- **On failure**: returns IAM Deny policy

## UI

Located on the **MCP Servers Configuration** page (`/configuration/mcp-servers`):

- **MCP API Key** container at the top of the page
- Shows key prefix with masked suffix + creation date if a key exists
- "Generate API Key" button (disabled when a key already exists)
- Generated key shown in a modal with CopyToClipboard and warning it won't be shown again
- "Revoke" button with confirmation modal
- The **Hosted MCP Access** tab, including this API key container, is available to every signed-in user; the **External MCP Servers** tab on the same page is read-only for users outside the Admin group (see [MCP Servers › Admin UI](mcp-servers.md#admin-ui))
- The left navigation still lists **Configuration › MCP Servers** for Admin users only, so a non-admin user needs the direct link (`/#/configuration/mcp-servers`) to reach their API key. Surfacing the link for all users is a follow-up.

## Security Considerations

- API keys are less secure than 3LO — no token expiration, no refresh rotation
- Keys are SHA-256 hashed at rest — DynamoDB compromise doesn't expose usable keys
- Users can only manage their own keys (enforced by AppSync identity context)
- API Gateway throttling: 50 burst, 100 sustained requests/sec
- Keys can be revoked immediately by the user
- `lma_` prefix makes keys identifiable if leaked in logs
- The authorizer logs each request's ID, HTTP method and resource path, and names a matched key only by its stored 12-character prefix (the prefix shown in the key list); at the default `AppSyncFieldLogLevel` (`ERROR`) the AppSync API's logs exclude resolver arguments and results, including the key returned when one is generated
- REQUEST authorizer with no IdentitySource means every request invokes the authorizer Lambda (no bypass via missing headers)

## Testing

```bash
# Run the end-to-end test
./utilities/test-mcp-api-key.sh <your-api-key> [api-gateway-url]

# Tests: initialize (Bearer), initialize (x-api-key), tools/list,
#        tools/call (list_meetings), ping, bad key rejection
```

The integration suite's `integ-tests/test_mcp_api_key_logging.py` generates a
key as the Cognito test user, calls the endpoint with it and with an unknown
key, revokes it, and checks what the authorizer's and the AppSync API's log
groups recorded. It runs in `make integ-tests` whenever a test user is
configured; see [integ-tests/README.md](../integ-tests/README.md).

## Connecting from Quick Suite

Add as a remote MCP server in Quick Suite:
- **Transport**: SSE/HTTP
- **URL**: The `MCPServerApiKeyEndpoint` stack output (e.g. `https://xxx.execute-api.us-west-2.amazonaws.com/mcp`)
- **Token**: Your LMA API key (e.g. `lma_a1b2c3d4-e5f6-7890-abcd-ef1234567890`)

Quick Suite sends `Authorization: Bearer <key>` on all requests through its streamable HTTP client.

> **To confirm on a deployed stack:** this has not been verified against a live Quick Suite tenant, and the Quick Suite setup guide documents only user-authentication (OAuth) for a remote MCP server. If your Quick Suite version has no bearer-token or API-key field, use Quick Desktop, Claude Desktop or another MCP client for the API key route. LMA's endpoint accepts the key over `Authorization: Bearer` or `x-api-key` regardless of the client.
