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
    → Returns IAM Allow policy + { userId, username, isAdmin, scope } in context
  → MCPServerAnalyticsFunction receives API Gateway proxy event
    → Detects JSON-RPC 2.0 message (jsonrpc + method fields in body)
    → Routes to MCP protocol handler:
      - initialize → returns server capabilities + protocolVersion
      - notifications/initialized → empty 200 acknowledgment
      - tools/list → returns the tool definitions the key's scope allows
      - tools/call → refuses a tool the key's scope does not allow, otherwise
        routes to the tool implementation and returns the MCP result
      - ping → returns empty result
    → User context from authorizer enforces UBAC on tool calls
```

## Key Generation Flow

```
User chooses a scope and clicks "Generate API Key" on the MCP Servers Configuration page
  → AppSync mutation: generateMCPApiKey(scope: "read" | "read-write")
  → MCPApiKeyManagerFunction Lambda
    → Verifies user doesn't already have a key (queries UserIdIndex GSI)
    → Generates key: "lma_" + uuid4
    → SHA-256 hashes the key
    → Stores in DynamoDB: { KeyHash (PK), UserId, Username, IsAdmin, CreatedAt, KeyPrefix, Enabled, Scope }
    → Returns plaintext key and its scope to user (shown once in modal with copy-to-clipboard)
```

## Key Scopes

A new API key is generated with one of two scopes. A key created before keys had scopes has the third, `legacy`:

| Scope | UI label | What a client using the key may call |
|-------|----------|--------------------------------------|
| `read` (the default for a new key) | **Read only** | The tools whose MCP annotations declare `readOnlyHint: true`: searching and reading meetings, transcripts, summaries, guidance, costs and Virtual Participant status |
| `read-write` | **Read and write** | Every tool, including those that start, end, share, delete or otherwise change something |
| `legacy` (keys created before scopes; never assigned to a new key) | **Legacy (original tools)** | The seven original tools, including `schedule_meeting` and `start_meeting_now`, as before, and the read-only meeting tools. Not the meeting tools that change something |

A key created before keys had scopes has no `Scope` attribute and keeps working as it did. To use the meeting tools that change something, generate a key with the **Read and write** scope; the MCP Servers page says so beside a legacy key. A key's scope cannot be changed: revoke the key and generate a new one. The API key path offers a client only the tools its key may call in `tools/list`, and answers a call to any other tool with an MCP tool error that names the scope it needs; the tool does not run. A request whose body is a tool's arguments rather than a JSON-RPC message is held to the key's scope in the same way, and refused with HTTP `403`. Being in the **Admin** group does not widen what a `read` or `legacy` key may do.

The scope applies to the API key endpoint. The OAuth (3LO) gateway path carries no key and is unchanged: it serves the seven original tools.

## Access to Meetings

Scope decides which tools a key may call; access decides which meetings each tool acts on. Every meeting tool answers the callers the meeting page answers (the AppSync `getCall` rule): a member of the **Admin** group, the meeting's owner, or a user the meeting is shared with. `get_meeting_summary`, `get_meeting_transcript` and `list_meetings` apply the same rule. A few operations narrow it to the meeting's owner or an Admin, as the meeting page does: deleting a meeting, changing who it is shared with, and reading its consent record. A Virtual Participant is read on the same rule against its own record, and changed (its agenda edited, or ended) by its owner or an Admin only. A meeting that does not exist and one the caller may not see are refused with the same message.

The server checks access itself before calling anything else, because it calls AppSync and LMA's other Lambda functions with its own IAM role, which those treat as a trusted backend. Where a resolver Lambda decides access from the caller's identity (the meeting controls behind deleting and sharing, and the summary regenerator), the server invokes it directly with the MCP caller's identity, so that resolver's own rule applies as well.

## Files

| File | Purpose |
|------|---------|
| `lma-ai-stack/deployment/lma-ai-stack.yaml` | All CF resources: MCPApiKeysTable, MCPApiKeyAuthorizerFunction, MCPApiKeyManagerFunction, MCPApiKeyRestApi (REQUEST authorizer, no IdentitySource), AppSync data source + 3 resolvers, MCPServerApiKeyEndpoint output |
| `lma-main.yaml` | MCPServerApiKeyEndpoint output (pass-through from nested stack) |
| `lma-ai-stack/source/appsync/schema.graphql` | MCPApiKey type, GenerateMCPApiKeyOutput type, generateMCPApiKey/revokeMCPApiKey mutations, listMCPApiKeys query |
| `lma-ai-stack/source/lambda_functions/mcp_api_key_authorizer/index.py` | REQUEST authorizer — accepts both `Authorization: Bearer` and `x-api-key` headers, hashes token, DynamoDB lookup, returns IAM policy + user context |
| `lma-ai-stack/source/lambda_functions/mcp_api_key_manager/index.py` | AppSync resolver — generate/list/revoke per-user API keys, one key per user enforced |
| `lma-ai-stack/source/lambda_functions/mcp_analytics/index.py` | MCP JSON-RPC 2.0 protocol handler (initialize, tools/list, tools/call, ping) for the API Gateway path, including the key scope check; on the BedrockAgentCore path, resolves the caller from the forwarded claims (see [Connecting with a User Identity](#connecting-with-a-user-identity)) |
| `lma-ai-stack/source/lambda_functions/mcp_analytics/tool_definitions.py` | MCP definitions of the meeting tools, with the annotations the scope check reads |
| `lma-ai-stack/source/lambda_functions/mcp_analytics/tools/access.py` | The access rule every meeting tool applies (see [Access to Meetings](#access-to-meetings)) |
| `lma-ai-stack/source/appsync/addMeetingGuidance.request.vtl` | Stores a guidance item under the meeting; published to `onAddMeetingGuidance` |
| `lma-ai-stack/source/ui/src/components/call-panel/MeetingGuidanceBanner.jsx` | The meeting page's guidance notifications |
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
| `tools/list` | Returns the definitions, with inputSchema and annotations, of the tools the key's scope allows |
| `tools/call` | Executes a tool by name with arguments, returns MCP content result; a tool the key's scope does not allow is refused with `isError: true` |
| `ping` | Health check, returns empty result |

### Tools Exposed

The **Scope** column is the key scope a tool needs; **Access** is who it answers for a given meeting or Virtual Participant (see [Access to Meetings](#access-to-meetings)). The MCP description of each tool that cannot be undone, or that returns sensitive content, says so.

| Tool | Scope | Access | Description |
|------|-------|--------|-------------|
| `search_lma_meetings` | read | own meetings (Admin: all) | Search transcripts/summaries with natural language |
| `get_meeting_transcript` | read | owner, shared, Admin | Get full transcript (json, text, or srt format) |
| `get_meeting_summary` | read | owner, shared, Admin | Get AI summary with action items and topics |
| `list_meetings` | read | owner, shared, Admin | List meetings with date/participant/status filters |
| `get_virtual_participant_status` | read | VP owner, shared, Admin | Poll a Virtual Participant's status |
| `schedule_meeting` | read-write (or legacy) | caller | Schedule a future meeting with virtual participant |
| `start_meeting_now` | read-write (or legacy) | caller | Start a meeting immediately with virtual participant |
| `get_live_transcript` | read | owner, shared, Admin | The lines added to a meeting's transcript since a cursor; see [Following a Meeting Live](#following-a-meeting-live) |
| `get_meeting_record` | read | owner, shared, Admin; `consent` owner and Admin only | The sections named in `include`: `details`, `summary`, `transcript`, `chat_links`, `key_frames`, `chat_attachments`, `recordings`, `consent` (default `details` and `summary`); see [Meeting Record Links](#meeting-record-links) |
| `get_meeting_cost` | read | owner, shared, Admin | The meeting's itemised cost estimate, as on its Cost panel |
| `list_meeting_guidance` | read | owner, shared, Admin | The guidance pushed to a meeting, newest first |
| `list_virtual_participants` | read | VP owner, shared, Admin | Virtual Participants the caller can see, newest first; optional `status` filter |
| `push_meeting_guidance` | read-write | owner, shared, Admin | Show a note on the meeting page; see [Meeting Guidance](#meeting-guidance) |
| `update_meeting_agenda` | read-write | meeting: owner, shared, Admin; VP: owner, Admin | Replace the agenda of a meeting in progress (`meeting_id`) or of a scheduled Virtual Participant at least 5 minutes before its launch (`virtual_participant_id`), through the same writes the Agenda panel and the Edit form make |
| `ask_meeting_assistant` | read-write | owner, shared, Admin | Ask the meeting assistant about one meeting, as the meeting page's assistant chat does; the answer is returned and also added to the meeting's assistant conversation |
| `regenerate_meeting_summary` | read-write | owner, shared, Admin | Generate the summary again; the new one replaces the current one, including edits, when it finishes |
| `share_meeting` / `unshare_meeting` | read-write | owner, Admin | Add email addresses to, or remove them from, the people the meeting is shared with |
| `end_meeting` | read-write | VP owner, Admin | End the meeting's Virtual Participant (the End button's path), which ends the meeting. A meeting streamed from a browser tab, the desktop app or the browser extension is ended where it is streamed |
| `end_virtual_participant` | read-write | VP owner, Admin | End a Virtual Participant by its id, or cancel one that is waiting for its scheduled launch |
| `delete_meeting` | read-write | owner, Admin | Permanently delete a meeting and everything stored with it, as the meeting list's Delete does. Nothing is deleted unless `confirm` is `true` |

Virtual Participants are started with `start_meeting_now` and `schedule_meeting`; there is no separate create tool.

The REST endpoint waits at most 29 seconds for an answer (the API Gateway integration limit). The meeting assistant can take longer than that to answer a question that needs several of its tools; the client then receives a gateway timeout, but the assistant still finishes, and its answer is added to the meeting's assistant conversation, where `get_live_transcript` with `include_assistant: true` returns it.

Each tool reuses the code path the meeting page uses: the AppSync mutations `updateCallAgenda`, `updateScheduledVirtualParticipant` and `endVirtualParticipant`; the queries `getCallCost`, `getVideoKeyframes`, `getChatAttachments` and `getMeetingConsent`; the meeting controls Lambda behind `deleteMeetings` and `shareMeetings`; the summary regenerator behind `regenerateCallSummary`; and, for `ask_meeting_assistant`, the AsyncAgentAssistOrchestrator invoked with the same request the meeting page's chat sends. Sharing through the MCP server does not send the `onShareMeetings` notification that refreshes the recipients' open meeting lists; they see the meeting the next time their list loads.

### Following a Meeting Live

The server is a stateless Lambda behind a request/response endpoint, so it cannot stream lines to a client. `get_live_transcript` is read by polling instead:

1. Call it with `meeting_id` only. It returns the meeting's final speech and meeting-chat lines so far (up to `limit`, default 200), a `cursor`, `live` (whether the meeting is still being captured) and `pollAfterSeconds`.
2. Call it again with `since` set to the `cursor` from the previous answer. It returns only the lines added since then, and a new cursor. When `hasMore` is true, call again at once; otherwise wait `pollAfterSeconds` (5 seconds).
3. Stop once `live` is false and a call returns no lines.

Lines are returned in the order they end on the meeting's timeline, each with its `type` (`speech` or `chat`), `channel`, `speaker` (named as the meeting page names it), `text`, `startTime` and `endTime`. Pass `include_assistant: true` to include the meeting assistant's answers as well. The cursor is opaque and belongs to one meeting. The two speech channels are finalized separately, so each call looks back 60 seconds behind the last line it returned and leaves out the lines it has already returned; a line finalized more than 60 seconds later than lines after it is not returned to a client that has already polled past it. Each call reads the meeting's stored transcript, so polling more often than every few seconds adds load without returning lines sooner.

### Meeting Guidance

`push_meeting_guidance(meeting_id, text, title?, level?)` leaves a short note on a meeting for the people following it in LMA. It appears at once on the meeting page, above the tabs, as a dismissible notification for the meeting's owner, the people it is shared with and Admins who have the page open; `level` (`info`, `warning` or `urgent`) sets how prominently it is shown. It is never posted to the meeting's own chat, so the meeting's other participants do not see it. Dismissing a note hides it on that page only. A page opened after a note was pushed does not show it; `list_meeting_guidance` returns every note the meeting has.

Each note is stored in the EventSourcing table under `PK = gd#<CallId>`, `SK = g#<CreatedAt>#<GuidanceId>`, with `Text`, `Title`, `Level`, `Source` (`mcp`), `CreatedBy` (the API key's user), the meeting's `Owner` and `SharedWith`, and the meeting's `ExpiresAfter`, so it expires with the meeting. Anything that wants a meeting's guidance in order queries that one partition. The write is the IAM-only `addMeetingGuidance` mutation, published to the `onAddMeetingGuidance(CallId)` subscription, which is filtered on `Owner` and `SharedWith` like the meeting's other subscriptions.

### Meeting Record Links

Every link `get_meeting_record` returns is a pre-signed S3 URL that works for anyone who holds it until `linksExpireAt`, one hour after the call (`linksExpireInSeconds`: 3600): key frame images (`ImageSignedUrl`, `ThumbnailSignedUrl`), chat attachment downloads (`DownloadUrl`), the audio and video recordings, and the consent record's evidence screenshots (`SignedUrl`). The stored, unsigned S3 locations are not returned. Call the tool again for fresh links. The answer can hold personal information from the meeting; treat it, and the links, as the meeting itself.

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
| `Scope` | S | `"read"` or `"read-write"`; absent on a key created before keys had scopes, which is listed and authorized as `"legacy"` |

**GSI: `UserIdIndex`** — Partition key: `UserId`, projects all attributes. Used for listing a user's keys and enforcing one-key-per-user.

## API Gateway Authorizer Details

- **Type**: `REQUEST` (not TOKEN) — receives all headers, no IdentitySource filter
- **TTL**: 0 (no caching — since the identity source varies between headers)
- **Token extraction order**: `authorizationToken` (TOKEN mode compat) → `headers.x-api-key` → `headers.Authorization` (strips `Bearer ` prefix)
- **On success**: returns IAM Allow policy with `context: { userId, username, isAdmin, scope }`
- **On failure**: returns IAM Deny policy

## UI

Located on the **MCP Servers Configuration** page (`/configuration/mcp-servers`):

- **MCP API Key** container at the top of the page
- Shows key prefix with masked suffix, its scope (**Read only**, **Read and write**, or **Legacy (original tools)** with a note that a new key gives the new scopes) and creation date if a key exists
- Before a key exists, a **Scope** choice (**Read only** by default, or **Read and write**) and the "Generate API Key" button (disabled when a key already exists)
- Generated key shown in a modal with its scope, CopyToClipboard and a warning it won't be shown again
- "Revoke" button with confirmation modal
- The **Hosted MCP Access** tab, including this API key container, is available to every signed-in user; the **External MCP Servers** tab on the same page is read-only for users outside the Admin group (see [MCP Servers › Admin UI](mcp-servers.md#admin-ui))
- The left navigation still lists **Configuration › MCP Servers** for Admin users only, so a non-admin user needs the direct link (`/#/configuration/mcp-servers`) to reach their API key. Surfacing the link for all users is a follow-up.

## Security Considerations

- API keys are less secure than 3LO — no token expiration, no refresh rotation
- Keys are SHA-256 hashed at rest — DynamoDB compromise doesn't expose usable keys
- A new key has the **Read only** scope unless **Read and write** is chosen; give a client a read-write key only when it needs to change something
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

`integ-tests/test_mcp_meeting_tools.py` runs under the same conditions. It
creates a synthetic meeting owned by the test user, generates a read key and
checks that a write tool is refused, then generates a read-write key and polls
`get_live_transcript` with its cursor while segments are added, pushes guidance
and reads it back with `list_meeting_guidance`, reads a recording link from
`get_meeting_record`, and deletes the meeting with `delete_meeting`, checking that
nothing is deleted without `confirm`. It touches no meeting it did not create and
revokes each key it generates.

## Connecting from Quick Suite

Add as a remote MCP server in Quick Suite:
- **Transport**: SSE/HTTP
- **URL**: The `MCPServerApiKeyEndpoint` stack output (e.g. `https://xxx.execute-api.us-west-2.amazonaws.com/mcp`)
- **Token**: Your LMA API key (e.g. `lma_a1b2c3d4-e5f6-7890-abcd-ef1234567890`)

Quick Suite sends `Authorization: Bearer <key>` on all requests through its streamable HTTP client.

> **To confirm on a deployed stack:** this has not been verified against a live Quick Suite tenant, and the Quick Suite setup guide documents only user-authentication (OAuth) for a remote MCP server. If your Quick Suite version has no bearer-token or API-key field, use Quick Desktop, Claude Desktop or another MCP client for the API key route. LMA's endpoint accepts the key over `Authorization: Bearer` or `x-api-key` regardless of the client.
