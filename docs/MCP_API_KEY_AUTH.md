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
      - tools/list → returns 6 tool definitions with inputSchema
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
| `lma-ai-stack/source/lambda_functions/mcp_analytics/index.py` | MCP JSON-RPC 2.0 protocol handler (initialize, tools/list, tools/call, ping) for API Gateway path; BedrockAgentCore path unchanged |
| `lma-ai-stack/source/ui/src/graphql/mutations.js` | generateMCPApiKey, revokeMCPApiKey, listMCPApiKeys GraphQL operations |
| `lma-ai-stack/source/ui/src/components/mcp-servers-page/MCPApiKeySection.jsx` | Cloudscape UI component for key management |
| `lma-ai-stack/source/ui/src/components/mcp-servers-page/MCPServersPage.jsx` | Integrated MCPApiKeySection at top of page |
| `utilities/test-mcp-api-key.sh` | End-to-end test script for the API key endpoint |
| `docs/MCP_API_KEY_AUTH.md` | This document |

## MCP Protocol Support

The API key endpoint implements the MCP streamable HTTP transport (JSON-RPC 2.0 over HTTP POST). Supported methods:

| Method | Description |
|--------|-------------|
| `initialize` | Returns server info (name: `lma-mcp-server`) and capabilities |
| `notifications/initialized` | Client acknowledgment, returns empty 200 |
| `tools/list` | Returns all 6 tool definitions with inputSchema |
| `tools/call` | Executes a tool by name with arguments, returns MCP content result |
| `ping` | Health check, returns empty result |

### Tools Exposed

| Tool | Description |
|------|-------------|
| `search_lma_meetings` | Search transcripts/summaries with natural language |
| `get_meeting_transcript` | Get full transcript (json, text, or srt format) |
| `get_meeting_summary` | Get AI summary with action items and topics |
| `list_meetings` | List meetings with date/participant/status filters |
| `schedule_meeting` | Schedule a future meeting with virtual participant |
| `start_meeting_now` | Start a meeting immediately with virtual participant |

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
- Currently on the admin-only Configuration page; consider making accessible to all users

## Security Considerations

- API keys are less secure than 3LO — no token expiration, no refresh rotation
- Keys are SHA-256 hashed at rest — DynamoDB compromise doesn't expose usable keys
- Users can only manage their own keys (enforced by AppSync identity context)
- API Gateway throttling: 50 burst, 100 sustained requests/sec
- Keys can be revoked immediately by the user
- `lma_` prefix makes keys identifiable if leaked in logs
- REQUEST authorizer with no IdentitySource means every request invokes the authorizer Lambda (no bypass via missing headers)

## Testing

```bash
# Run the end-to-end test
./utilities/test-mcp-api-key.sh <your-api-key> [api-gateway-url]

# Tests: initialize (Bearer), initialize (x-api-key), tools/list,
#        tools/call (list_meetings), ping, bad key rejection
```

## Connecting from Quick Suite

Add as a remote MCP server in Quick Suite:
- **Transport**: SSE/HTTP
- **URL**: The `MCPServerApiKeyEndpoint` stack output (e.g. `https://xxx.execute-api.us-west-2.amazonaws.com/mcp`)
- **Token**: Your LMA API key (e.g. `lma_a1b2c3d4-e5f6-7890-abcd-ef1234567890`)

Quick Suite sends `Authorization: Bearer <key>` on all requests via `streamablehttp_client`.
