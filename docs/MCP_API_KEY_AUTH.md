# MCP Server API Key Authentication

## Overview

LMA's MCP server currently uses 3LO (three-legged OAuth) via Cognito for authentication. This works well for interactive clients like Amazon Quick Suite and Claude Desktop, but is cumbersome for headless/programmatic MCP clients that just need a simple API key.

This design adds per-user API key authentication as a second endpoint deployed alongside the existing Cognito 3LO gateway whenever MCP is enabled. Users generate their own API keys from the LMA UI settings page.

## Current Architecture

- `MCPServerAnalyticsFunction` Lambda implements the MCP tools (search, transcript, summary, list, schedule, start meeting)
- `AWS::BedrockAgentCore::Gateway` with `AuthorizerType: CUSTOM_JWT` handles auth via Cognito
- Cognito `MCPServerExternalAppClient` provides OAuth authorization code flow
- User identity (for UBAC) comes from JWT claims in the gateway event
- Key files:
  - `lma-ai-stack/deployment/lma-ai-stack.yaml` — all MCP resources (gateway, Lambda, Cognito client)
  - `lma-main.yaml` — top-level parameters and outputs
  - `lma-ai-stack/source/lambda_functions/mcp_analytics/index.py` — MCP tools Lambda
- Existing condition: `ShouldEnableMCPServer` / `ShouldEnableMCP` gates all MCP resources
- BedrockAgentCore Gateway does NOT support API key auth natively (only `CUSTOM_JWT`, `AWS_IAM`, `NONE`)

## Design

### Approach

Deploy a separate REST API Gateway with a Lambda authorizer alongside the existing BedrockAgentCore Gateway. Both invoke the same `MCPServerAnalyticsFunction`. Per-user API keys are stored (hashed) in DynamoDB and managed via AppSync mutations from the UI.

### Request Flow

```
MCP client sends request with x-api-key header
  → REST API Gateway
  → Lambda Authorizer (MCPApiKeyAuthorizerFunction)
    → SHA-256 hashes the key
    → Looks up hash in DynamoDB (MCPApiKeysTable)
    → Finds userId, username, isAdmin
    → Returns IAM allow policy + userId/username/isAdmin in context
  → MCPServerAnalyticsFunction
    → Extracts userId from requestContext.authorizer
    → UBAC works as normal
```

### Key Generation Flow

```
User clicks "Generate API Key" in LMA UI settings
  → AppSync mutation: generateMCPApiKey
  → MCPApiKeyManagerFunction Lambda
    → Generates key: "lma_" + uuid4 (e.g. lma_a1b2c3d4-e5f6-7890-abcd-ef1234567890)
    → SHA-256 hashes the key
    → Stores in DynamoDB: { KeyHash (PK), UserId, Username, IsAdmin, CreatedAt, KeyPrefix }
    → KeyPrefix = first 8 chars (e.g. "lma_a1b2") for display/identification in UI
    → Returns plaintext key to user (only time it's ever shown)
```

### Key Design Decisions

- **Hash keys, don't store plaintext** — if DynamoDB is compromised, keys aren't usable
- **DynamoDB over Secrets Manager** — SM costs $0.40/secret/month and has 500k limit per account. DynamoDB is essentially free at this scale and supports the hash-lookup pattern
- **One key per user** (simplest starting point, can expand to multiple later)
- **Key format**: `lma_` prefix + UUID for easy identification
- **KeyPrefix stored** for UI display so users can identify their key without seeing the full value
- **Both endpoints always deployed** when MCP is enabled (no separate toggle — API key is less secure but useful, and the 3LO endpoint is always there for clients that support it)

## Implementation Status

All backend changes are implemented. UI changes are a separate effort.

### Files Modified

| File | Change |
|------|--------|
| `lma-ai-stack/deployment/lma-ai-stack.yaml` | Added MCPApiKeysTable, MCPApiKeyAuthorizerFunction, MCPApiKeyManagerFunction, MCPApiKeyRestApi (REST API Gateway with TOKEN authorizer), AppSync data source + 3 resolvers, MCPServerApiKeyEndpoint output |
| `lma-main.yaml` | Added MCPServerApiKeyEndpoint output |
| `lma-ai-stack/source/appsync/schema.graphql` | Added MCPApiKey type, GenerateMCPApiKeyOutput type, generateMCPApiKey/revokeMCPApiKey mutations, listMCPApiKeys query |
| `lma-ai-stack/source/lambda_functions/mcp_api_key_authorizer/index.py` | New — hashes x-api-key, looks up in DynamoDB, returns IAM policy + user context |
| `lma-ai-stack/source/lambda_functions/mcp_api_key_manager/index.py` | New — generate/list/revoke per-user API keys via AppSync |
| `lma-ai-stack/source/lambda_functions/mcp_analytics/index.py` | Updated — detects API Gateway proxy events, extracts user context from Lambda authorizer |
| `lma-ai-stack/source/ui/src/graphql/mutations.js` | Added generateMCPApiKey, revokeMCPApiKey, listMCPApiKeys GraphQL operations |
| `lma-ai-stack/source/ui/src/components/mcp-servers-page/MCPApiKeySection.jsx` | New — API key generate/revoke/list UI component |
| `lma-ai-stack/source/ui/src/components/mcp-servers-page/MCPServersPage.jsx` | Updated — integrated MCPApiKeySection at top of page |

### UI

Added to the MCP Servers Configuration page (accessible to all authenticated users):

- **MCP API Key** container section at the top of the page
- Shows current key prefix + masked suffix + created date if a key exists
- "Generate API Key" button (disabled if key already exists)
- Generated key shown in a modal with CopyToClipboard and a warning it won't be shown again
- "Revoke" button with confirmation modal

## DynamoDB Table Schema

**Table: `${LMAStackName}-MCPApiKeys`**

| Attribute | Type | Description |
|-----------|------|-------------|
| `KeyHash` | S (PK) | SHA-256 hash of the API key |
| `UserId` | S | Cognito user sub |
| `Username` | S | Cognito username/email |
| `IsAdmin` | S | "true" or "false" |
| `KeyPrefix` | S | First 8 chars of key for display |
| `CreatedAt` | S | ISO 8601 timestamp |
| `Enabled` | S | "true" or "false" |

**GSI: `UserIdIndex`**
- Partition key: `UserId`
- Projects all attributes
- Used by: list keys for a user, check if user already has a key

## Security Considerations

- API keys are less secure than 3LO — no token expiration, no refresh rotation
- Keys are hashed (SHA-256) at rest — compromise of DynamoDB doesn't expose usable keys
- Users can only manage their own keys (enforced by AppSync identity context)
- API Gateway throttling limits abuse (50 burst, 100 sustained requests/sec)
- Keys can be revoked immediately by the user or an admin
- The `lma_` prefix makes keys identifiable if leaked in logs

## Implementation Order

All items implemented:

1. ✅ CloudFormation: DynamoDB table, authorizer Lambda, REST API Gateway, permissions
2. ✅ CloudFormation: API key manager Lambda, AppSync schema/resolvers
3. ✅ Lambda code: authorizer, key manager, update mcp_analytics
4. ✅ Outputs: API key endpoint URL in both templates
5. ✅ UI: MCP API Key section on MCP Servers Configuration page
