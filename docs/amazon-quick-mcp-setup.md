---
title: "Amazon Quick MCP Setup"
---

# Amazon Quick MCP Setup

This guide covers connecting **Live Meeting Assistant (LMA)** to Amazon's two
Quick products:

| Product | Type | Auth path | Best for |
|---|---|---|---|
| **Amazon Quick Suite** | Web app (Enterprise tier) | OAuth 3LO via Cognito | Multi-user organizations using Quick Suite for chat, dashboards, and connectors |
| **Amazon Quick Desktop** | Native macOS / Windows app | API key (Bearer) | Individual users wanting scheduled agents and a personal knowledge graph |

Both expose the same seven LMA tools (search, transcript, summary, list,
schedule, start-now, VP status). Pick the path below that matches the product
you're connecting.

> **Looking for ways to use LMA from Quick Desktop?** Once you've completed
> the Quick Desktop setup below, see
> [Quick Desktop: LMA Workflows](amazon-quick-desktop-integration.md) for
> conversational workflows (joining meetings, posting recaps to Slack,
> querying past transcripts), pre-built scheduled agents, and the skills
> pack.

---

## Prerequisites (both paths)

- LMA deployed with MCP Server enabled (v0.2.23 or later)
  - During deployment, the `EnableMCP` parameter must be set to `true` (this
    is the default)
  - If you already have LMA deployed without it, see
    [Enabling MCP Server on Existing Deployment](#enabling-mcp-server-on-existing-deployment)
    below
- Admin access to the LMA CloudFormation stack outputs

### Enabling MCP Server on Existing Deployment

If you deployed LMA with MCP Server disabled:

1. Log into the **AWS Management Console**
2. Navigate to **CloudFormation**
3. Select your LMA stack (default name: `LMA`)
4. Click **Update**, choose **Use current template**, click **Next**
5. Find **Enable MCP Server Integration** and set it to `true`
6. Click **Next** through the remaining screens, acknowledge, and **Update stack**
7. Wait ~10–15 minutes for the update to complete
8. Open the **Outputs** tab to see the new MCP configuration values

---

## User-Based Access Control (UBAC)

LMA enforces UBAC across both paths via the JWT or API key user context:

- **Non-admin users** can only access their own meetings
- **Admin users** can access all meetings

UBAC is enforced at the MCP tool level on every call, regardless of which
auth path Quick uses.

---

## Available LMA Tools

Both auth paths expose the same set of tools. Read-only tools are annotated
with `readOnlyHint: true` so MCP clients (including Quick Desktop scheduled
agents) classify them correctly.

| Tool | Read-only | Description |
|---|---|---|
| `search_lma_meetings` | ✅ | Semantic search across meeting transcripts and summaries |
| `get_meeting_transcript` | ✅ | Full transcript in `text`, `json`, or `srt` format |
| `get_meeting_summary` | ✅ | AI-generated summary with action items and topics |
| `list_meetings` | ✅ | Filter by date range, participant, or status |
| `get_virtual_participant_status` | ✅ | Poll VP status (joining, active, manual-action-required, failed) |
| `schedule_meeting` | — | Schedule a future meeting with the LMA virtual participant |
| `start_meeting_now` | — | Start an immediate meeting with the LMA virtual participant |

All tool responses include a `meetingUrl` and (where applicable) a
`virtualParticipantUrl` deep-link back into the LMA web UI for quick drill-down
into the full transcript, recording, or VP viewer.

---

# Path A: Quick Suite (OAuth)

Use this path when connecting LMA to **Amazon Quick Suite** (the web product).
Quick Suite uses three-legged OAuth (3LO) so each user authenticates with
their own LMA Cognito credentials before Quick Suite can call LMA tools on
their behalf.

## Path A Prerequisites

- Amazon Quick Suite **Enterprise** subscription (required to create action connectors)
- Amazon Quick Suite **Professional or Enterprise** subscription (required to invoke action connectors)
- Quick Suite Author role or higher

## Authentication: Three-Legged OAuth (3LO)

LMA's MCP server uses **OAuth 2.0 Authorization Code Flow** with Amazon
Cognito as the identity provider:

1. Quick Suite redirects the user to LMA's Cognito login page
2. The user authenticates with their LMA credentials
3. Cognito returns an authorization code to Quick Suite
4. Quick Suite exchanges the code for access and refresh tokens
5. Quick Suite uses the access token to call LMA's MCP tools

## Step 1: Gather LMA MCP Server Configuration

1. Log into the **AWS Management Console** → **CloudFormation**
2. Select your **main LMA stack** (default name: `LMA`)
3. Click the **Outputs** tab
4. Copy these values:

| Output Key | Description |
|---|---|
| `MCPServerEndpoint` | OAuth-protected MCP server URL (BedrockAgentCore Gateway) |
| `MCPServerClientId` | OAuth Client ID |
| `MCPServerClientSecret` | OAuth Client Secret (sensitive — store securely) |
| `MCPServerTokenURL` | Cognito OAuth token endpoint |
| `MCPServerAuthorizationURL` | Cognito OAuth authorization endpoint |

> These outputs only appear when the stack was deployed with `EnableMCP=true`.

## Step 2: Create the MCP Integration in Quick Suite

### 2.1 Add the integration

1. Log into the **Amazon Quick Suite console**
2. Click **Integrations** in the left navigation
3. Click **Add** (the plus "+" icon)

### 2.2 Configure integration details

1. **Name**: `LMA Meeting Assistant`
2. **Description**: `Access Live Meeting Assistant transcripts, summaries, and meeting data`
3. **MCP server endpoint**: paste the `MCPServerEndpoint` value from Step 1
4. Click **Next**

### 2.3 Configure authentication

1. Authentication method: **User authentication (OAuth)**
2. Configuration approach: **Manual configuration**
   > LMA's Cognito does not support Dynamic Client Registration (DCR).
3. Enter:
   - **Client ID**: `MCPServerClientId`
   - **Client Secret**: `MCPServerClientSecret`
   - **Token URL**: `MCPServerTokenURL`
   - **Auth URL**: `MCPServerAuthorizationURL`
   - **Redirect URL**: use Quick Suite's callback URL (provided by Quick Suite)
4. Click **Create and continue**

### 2.4 Authorize the integration

1. You'll be redirected to the **LMA Cognito login page**
2. Sign in with your LMA email and password
3. Review the requested permissions and click **Allow**
4. You'll be redirected back to Quick Suite

### 2.5 Review discovered tools

Quick Suite will discover the seven LMA tools. Click **Next** to continue.

### 2.6 Share (optional)

To let other users in your organization use this integration:

1. Click **Share integration**
2. Select users or groups
3. Click **Save**

> Each user authenticates with their own LMA credentials the first time
> they use the integration.

### 2.7 Finish

Click **Done**. Quick Suite will create the action connector — wait for the
status to change from **Creating** to **Active** (1–2 minutes).

## Step 3: Test (Quick Suite)

In the Quick Suite **Chat** interface, try natural language queries:

- `Search my LMA meetings for discussions about the product roadmap`
  (calls `search_lma_meetings`)
- `Get the transcript for LMA meeting <CallId>`
  (calls `get_meeting_transcript`)
- `List my recent LMA meetings from the past week`
  (calls `list_meetings`)

The first time any user invokes the connector, they'll see a **Sign in**
button to start their personal OAuth flow.

---

# Path B: Quick Desktop (API Key)

Use this path when connecting LMA to **Amazon Quick Desktop** (the native
macOS/Windows app). Quick Desktop uses a per-user API key with Bearer-token
authentication.

## Path B Prerequisites

- Amazon Quick Desktop installed (macOS or Windows)
- An LMA account (the API key is tied to your individual LMA user)

## Authentication: Per-User API Key

LMA exposes a separate REST API Gateway endpoint for API-key access. Keys
are SHA-256 hashed at rest, scoped to a single user, and revocable from the
LMA UI. See [MCP API Key Authentication](mcp-api-key-auth.md) for the full
architecture.

## Step 1: Generate an LMA API Key

1. Open your LMA web UI
2. Navigate to **Settings → MCP Servers Configuration**
3. In the **Hosted MCP Access** section at the top, click **Generate API Key**
4. Copy the key (format `lma_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`) —
   **it's shown once only**
5. Note the **MCP API Endpoint URL** displayed on the same page (this maps
   to the `MCPServerApiKeyEndpoint` CloudFormation output)

## Step 2: Add LMA as an MCP Server in Quick Desktop

1. Open **Amazon Quick Desktop**
2. Navigate to **Settings → Capabilities → MCP**
3. Click **+ Add MCP / Skill**
4. Configure:
   - **Name**: `Live Meeting Assistant (LMA)`
   - **Endpoint URL**: your **MCP API Endpoint URL** from Step 1
   - **Authentication**: Bearer Token
   - **Token**: your API key from Step 1
5. Click **Connect**

Quick Desktop will discover the seven LMA tools and confirm the connection.

## Step 3: Test (Quick Desktop)

In a new Quick Desktop conversation:

```
Search my LMA meetings for discussions about [any recent topic]
```

Quick should call `search_lma_meetings` and return results.

### Common chat queries

| Query | Tools called |
|---|---|
| "What meetings did I have last week?" | `list_meetings` |
| "Find meetings where we discussed pricing" | `search_lma_meetings` |
| "Summarize yesterday's standup" | `list_meetings` → `get_meeting_summary` |
| "What action items came out of the Acme call?" | `search_lma_meetings` → `get_meeting_summary` |
| "Get the full transcript of meeting XYZ" | `get_meeting_transcript` |
| "Schedule LMA to join my 2pm Zoom tomorrow" | `schedule_meeting` |
| "Catch me up on the standup I missed this morning" | `list_meetings` → `get_meeting_summary` (often combined with Slack/calendar lookups) |

### Next: workflows, agents, and skills

For end-to-end LMA workflows in Quick Desktop — conversational prompts
(*"join my next meeting"*, *"post the action items to Slack"*,
*"find what we agreed about X in past meetings"*), pre-built scheduled
agents, and packaged skills (live coaching, async catch-up) — see:

- [Quick Desktop: LMA Workflows](amazon-quick-desktop-integration.md) —
  conversational workflows, scheduled agent recipes, and the skills pack
- [`amazon-quick-desktop-skills-pack/`](../amazon-quick-desktop-skills-pack/) — installable
  bundle of agents and skills

---

## Tool Reference (Both Paths)

Common parameters for the most-used tools.

### `search_lma_meetings`

- `query` (required) — natural-language query
- `maxResults` — default 10
- `startDate` / `endDate` — optional ISO 8601

### `get_meeting_transcript`

- `meetingId` (required) — the meeting `CallId`
- `format` — `text` (default), `json`, or `srt`

### `get_meeting_summary`

- `meetingId` (required)
- `includeActionItems` — default `true`
- `includeTopics` — default `true`

### `list_meetings`

- `startDate` / `endDate` — ISO 8601
- `participant` — filter by name
- `status` — `ENDED`, `IN_PROGRESS`, or `ALL` (default)
- `limit` — default 20

### `schedule_meeting`

- `meetingName`, `meetingPlatform` (Zoom/Teams/Chime/Webex), `meetingId`
  (numeric), `scheduledTime` (ISO 8601), `meetingPassword` (optional)

### `start_meeting_now`

- `meetingName`, `meetingPlatform`, `meetingId`, `meetingPassword` (optional)
- `useStoredZoomCredentials` — default `true`; when the user has stored Zoom
  credentials in LMA, the VP signs in with them rather than joining as a guest

### `get_virtual_participant_status`

- `virtualParticipantId` (required) — the id returned by `start_meeting_now`
- Poll every 5–10 seconds for ~2 minutes after `start_meeting_now` to detect
  `MANUAL_ACTION_REQUIRED` (CAPTCHA / 2FA / SSO) — surface the
  `manualActionMessage` and `virtualParticipantUrl` to the user so they can
  complete the challenge in the LMA viewer

---

## Troubleshooting

### Cannot connect to MCP server

**Quick Suite (OAuth):**
- Verify `EnableMCP=true` on the LMA stack
- Confirm the `MCPServerEndpoint` is reachable: `curl -I <MCPServerEndpoint>`
- Check that `MCPServerTokenURL` and `MCPServerAuthorizationURL` match your
  AWS region

**Quick Desktop (API key):**
- Confirm the endpoint URL is the **MCP API Endpoint** (the API Gateway URL),
  not the BedrockAgentCore Gateway URL
- Verify the API key hasn't been revoked (check **MCP Servers Configuration**
  in the LMA UI)
- Ensure the auth method is **Bearer Token** (not custom header)

### Authentication failed / Invalid credentials

**Quick Suite:** Double-check Client ID, Client Secret, Token URL, and Auth
URL from CloudFormation outputs. OAuth scopes (`openid email profile`) are
pre-configured by LMA — no manual scope changes needed.

**Quick Desktop:** Regenerate the API key and try again. Old keys are
revoked when a new one is generated (one key per user).

### Access denied / Permission denied

LMA enforces UBAC:
- **Non-admin users** can only access their own meetings — verify the meeting
  `Owner` matches the authenticated user
- **Admin users** can access all meetings — verify the user is in the
  Cognito **Admin** group (matches the `AdminEmail` stack parameter)

### Tool not found / Unknown tool

- **Quick Suite:** Refresh the integration — **Integrations** → select LMA →
  **Actions** → **Refresh tools**
- **Quick Desktop:** Disconnect and reconnect the MCP integration to
  rediscover tools
- Verify the `MCPServerAnalytics` Lambda has the expected env vars
  (`CALLS_TABLE`, `TRANSCRIPT_KB_ID`, `MODEL_ARN`) and check
  `/LMA/lambda/MCPServerAnalytics` CloudWatch logs

### Operation timeout / HTTP 424

Quick Suite has a 60-second tool-call timeout. For large result sets:
- Reduce `maxResults` in search queries
- Use date filters to narrow `list_meetings` results
- Request specific meeting IDs rather than broad searches

The underlying Lambda has a 900-second timeout, so if Quick Suite times out
the call may still complete server-side — but Quick won't see the result.

### No meetings found / empty results

- Confirm meetings exist via the LMA UI
- For semantic search, allow 15–30 minutes after a meeting ends for
  Bedrock Knowledge Base indexing
- Verify date filters use ISO 8601 with explicit timezone
  (e.g. `2026-05-01T00:00:00Z`)
- Re-check UBAC scope (see "Access denied" above)

### Invalid meeting ID format

Meeting IDs in LMA are UUIDs (`abc123-def456-ghi789`). Get them from:
- LMA UI meetings list — copy the `CallId`
- The `list_meetings` tool response

For `schedule_meeting` / `start_meeting_now`, the `meetingId` is the
**platform's numeric meeting ID** (e.g. Zoom `123456789`), not LMA's UUID.

### Virtual participant tools fail

- Confirm the Virtual Participant stack is deployed (look for
  `LMA-VIRTUALPARTICIPANTSTACK` in CloudFormation)
- Supported platforms (case-sensitive): `Zoom`, `Teams`, `Chime`, `Webex`
- Numeric meeting ID only — no platform prefixes

### Custom HTTP headers not supported (Quick Suite)

Quick Suite does not support arbitrary custom headers. LMA's MCP server
uses standard OAuth bearer tokens, which are supported — no custom-header
configuration is required.

---

## Manual OAuth testing (Quick Suite)

Useful when debugging the OAuth path:

```bash
# 1. Get authorization code (open in browser)
https://<lma-domain>.auth.<region>.amazoncognito.com/oauth2/authorize\
?client_id=YOUR_CLIENT_ID\
&response_type=code\
&scope=openid+email+profile\
&redirect_uri=YOUR_CALLBACK_URL

# 2. Exchange the code for tokens
curl -X POST https://<lma-domain>.auth.<region>.amazoncognito.com/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code" \
  -d "client_id=YOUR_CLIENT_ID" \
  -d "client_secret=YOUR_CLIENT_SECRET" \
  -d "code=AUTHORIZATION_CODE" \
  -d "redirect_uri=YOUR_CALLBACK_URL"

# 3. Test the MCP endpoint with the access token
curl -X POST https://<gateway-id>.bedrock-agentcore.<region>.amazonaws.com/mcp \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

---

## Manual API-key testing (Quick Desktop)

```bash
./utilities/test-mcp-api-key.sh <your-api-key> [api-gateway-url]
```

Tests `initialize`, `tools/list`, `tools/call` (`list_meetings`), `ping`,
and bad-key rejection over both `Authorization: Bearer` and `x-api-key`
headers.

---

## Security Best Practices

- **Protect secrets** — never commit `MCPServerClientSecret` or LMA API keys
  to version control
- **HTTPS only** — both OAuth and MCP transport must be HTTPS
- **Rotate** — periodically rotate Cognito client secrets and API keys
- **Limit scope** — keep OAuth scopes at `openid email profile` (LMA default)
- **Revoke when leaked** — API keys can be revoked instantly from the LMA UI
- **UBAC enforcement** — non-admin users can only see their own meetings;
  the JWT `sub` claim or API key user context drives this on every call

---

## Reference

- [MCP Servers Overview](mcp-servers.md)
- [MCP API Key Authentication](mcp-api-key-auth.md) — full architecture for
  the API-key path
- [Quick Desktop: LMA Workflows](amazon-quick-desktop-integration.md) —
  conversational workflows and scheduled agent recipes for Quick Desktop
- [User-Based Access Control](user-based-access-control.md)
- [Model Context Protocol Specification](https://modelcontextprotocol.io/)
- [Amazon Quick Suite MCP Documentation](https://docs.aws.amazon.com/quicksuite/latest/userguide/mcp-integration.html)
