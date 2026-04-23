---
title: "Salesforce MCP Server Setup Guide"
---

# Salesforce MCP Server Setup Guide

## Overview

This guide walks you through setting up the Salesforce Platform MCP server
with OAuth 2.1 authentication in LMA. Once connected, AI agents in LMA can
query and interact with Salesforce data during meetings using the tools and
prompts provided by Salesforce's built-in MCP servers.

## Prerequisites

- LMA deployed with OAuth 2.1 support (0.2.23 and above)
- A Salesforce org with the Platform MCP feature enabled (Developer Edition,
  Sandbox, or Production)
- Admin access to Salesforce Setup

## Step 1: Get Your OAuth Callback URL

1. Log into your LMA application
2. Go to AWS CloudFormation console
3. Find your LMA stack
4. Go to the **Outputs** tab
5. Copy the value of `OAuthCallbackUrl`
   - Should look like: `https://your-cloudfront-domain.cloudfront.net/#/oauth/callback`

## Step 2: Create Salesforce Connected App

### 2.1 Navigate to Setup

1. Log into your Salesforce org
2. Click the **gear icon** (⚙️) in the top right
3. Select **Setup**

### 2.2 Create Connected App

1. In the Quick Find box, search for **"App Manager"**
2. Click **New Connected App**
3. Fill in the basic information:
   - **Connected App Name:** `LMA MCP Integration`
   - **API Name:** `LMA_MCP_Integration` (auto-filled)
   - **Contact Email:** Your email address

### 2.3 Configure OAuth Settings

1. Check **Enable OAuth Settings**

2. **Callback URL:** Paste your OAuth Callback URL from Step 1
   ```
   https://your-cloudfront-domain.cloudfront.net/#/oauth/callback
   ```

3. **Selected OAuth Scopes:** Add these three scopes:
   - ✅ **Access Salesforce Platform MCP services** (`mcp_api`)
   - ✅ **Perform requests at any time** (`refresh_token`, `offline_access`)

4. **Enable Authorization Code and Credentials Flow:** ✅ Checked

5. **Require Proof Key for Code Exchange (PKCE):** ✅ Checked

6. **Issue JSON Web Token (JWT)-based access tokens for named users:** ✅ Checked

7. **Refresh Token Policy:**
   - Select: **Expire refresh token after specific time**
   - **Refresh Token Validity Period:** `365` Days

8. Click **Save**

### 2.4 Get Consumer Key

1. After saving, click **Continue**
2. Click **Manage Consumer Details**
3. Verify your identity (may require 2FA code)
4. Copy the **Consumer Key** (this is your OAuth Client ID)
   - Example: `[YOUR_CONSUMER_KEY_HERE]...`
5. You don't need the Consumer Secret for this flow (PKCE is used instead).

## Step 3: Activate at Least One MCP Server in Salesforce

Salesforce's Platform MCP feature exposes a catalog of server definitions
under **Setup → MCP Servers**. Activate the ones you want to use.

1. In Salesforce Setup, search for **MCP Servers**
2. Pick a server — common built-ins include:
   - **sobject-all** — full CRUD + SOQL + SOSL (9 tools, 2 prompts)
   - **sobject-reads** — read-only subset
   - **sobject-mutations** — write-only subset
   - **sobject-deletes** — delete-only subset
   - **metadata-experts** — schema / describe helpers
   - **salesforce-api-context** — Salesforce REST API context
   - **data-cloud-queries** — CDP / Data Cloud
   - **engagement-interaction** — engagement events
3. Take note of the server's **API Name** — e.g., `platform.sobject-all`.
   The dot in the API name becomes a slash in the URL path, so
   `platform.sobject-all` → `/platform/sobject-all`.
4. Click **Activate**.

The Server URL to use in LMA is:

```
https://api.salesforce.com/platform/mcp/v1/platform/<server-api-name>
```

For `sobject-all` that is:

```
https://api.salesforce.com/platform/mcp/v1/platform/sobject-all
```

## Step 4: Add Salesforce MCP Server in LMA

### 4.1 Navigate to MCP Servers

1. Log into your LMA application
2. Go to **Configuration** → **MCP Servers**
3. Click the **Custom Servers** tab

### 4.2 Add Custom HTTP Server

1. Fill in the server details:
   - **Server Name:** `Salesforce` (or any name you prefer)
   - **Server URL:** `https://api.salesforce.com/platform/mcp/v1/platform/sobject-all`
     (replace `sobject-all` with the API name of whichever MCP server you
     activated in Step 3)
   - **Description (Optional):** `Salesforce Platform MCP — sobject-all`

2. Check ✅ **This server requires authentication**

3. Click **Add Server**

### 4.3 Configure OAuth Authentication

The authentication modal will open automatically:

1. **Authentication Type:** Select `OAuth 2.1 with PKCE (User Authorization)`

2. **OAuth Provider:** Select `Salesforce`
   - Authorization and Token URLs are pre-filled with
     `https://login.salesforce.com/services/oauth2/{authorize,token}`
   - Scopes are pre-filled with: `mcp_api refresh_token offline_access`

3. **Client ID:** Paste your Consumer Key from Step 2.4

4. **Scopes:** Verify the scopes are `mcp_api refresh_token offline_access`

5. Click **Authorize with OAuth**

### 4.4 Complete Authorization

1. A popup window will open with Salesforce login
2. Log into your Salesforce org (if not already logged in)
3. Review the permissions being requested
4. Click **Allow**
5. The popup will show "✅ Authorization complete!" and close automatically

## Step 5: Verify Installation

### 5.1 Check Installed Servers

1. Go to **Configuration** → **MCP Servers**
2. Click the **Installed Servers** tab
3. You should see your `Salesforce` entry with:
   - Status: `ACTIVE`
   - Package Type: `streamable-http`
   - Authentication: OAuth 2.1

### 5.2 Test in Chat

1. Start or join a meeting
2. Open the chat assistant
3. Ask: `"What Salesforce tools do you have?"`
4. For the `sobject-all` server you should see nine tools:
   - `getUserInfo` — current user identity
   - `soqlQuery` — execute SOQL
   - `find` — SOSL cross-object search
   - `getObjectSchema` — schema / describe
   - `listRecentSobjectRecords` — recently viewed records
   - `getRelatedRecords` — parent→child traversal
   - `createSobjectRecord`, `updateSobjectRecord`, `updateRelatedRecord`

5. Test a query: `"Who am I in Salesforce?"` (invokes `getUserInfo`)
6. Or: `"List recent accounts in Salesforce"` (invokes `listRecentSobjectRecords`)

## Troubleshooting

Two helpers are included in the repo to debug each step independently of LMA:

- `scripts/test-salesforce-mcp.sh` — take a token file (raw JWT or the
  KMS-encrypted blob stored in LMA's DynamoDB), decrypt if needed, decode
  the JWT, and POST MCP `initialize` against candidate URLs. On `403` it
  auto-pulls the required scopes from Salesforce's RFC 9728
  `oauth-protected-resource` metadata and reports any missing scope.

- `scripts/sf-mcp-oauth-e2e.py` — pure-stdlib Python, runs the full
  OAuth 2.1 + PKCE flow with a loopback listener on
  `http://localhost:8765/oauth/callback`, then exercises
  `initialize → tools/list → prompts/list → tools/call`. Subcommands:
  `listen`, `exchange`, `mcp`, `all`. See `--help`.

### Issue: `404 {"error":{"code":404,"message":"Server definition not found for: <name>"}}`

The MCP server name in the URL doesn't match an activated server in your
org.

**Fix:**
- The path segment after `/v1/` must match the server's **API Name** in
  Setup → MCP Servers, with dots replaced by slashes. For
  `platform.sobject-all` the URL path ends with `/platform/sobject-all`.
- Confirm the server is **Active** (not Inactive) in Setup → MCP Servers.

### Issue: `403 {"error":{"code":403,"message":"OAuth invalid scope"}}`

The token does not have the `mcp_api` scope.

**Fix:**
- Add `mcp_api` (plus `refresh_token` / `offline_access`) to the Connected
  App's Selected OAuth Scopes and save.
- In LMA, delete and re-add (or re-authorize) the Salesforce MCP server so
  a fresh token is minted with the new scope. Refreshing the existing token
  will not pick up newly granted scopes — a full re-authorization is
  required.

### Issue: `401 {"errors":[{"message":"Invalid token"}]}`

Token expired, issued for the wrong org, or missing the
`https://api.salesforce.com` audience.

**Fix:** Re-authorize from LMA. If the problem persists, confirm the
Connected App is in the correct org and that the JWT `sfap_op` claim
includes `MCPService` (org is entitled for Platform MCP).

### Issue: `redirect_uri_mismatch`

The callback URL in Salesforce doesn't match the one being sent.

**Fix:**
- The callback URL in Salesforce must include the `#` character:
  `https://domain/#/oauth/callback`.

### Issue: `invalid_client_id`

Wrong Consumer Key entered.

**Fix:**
- Go to Salesforce → App Manager → your Connected App → Manage Consumer
  Details, copy the correct Consumer Key, and re-enter it in LMA.

## Token Management

### Automatic Token Refresh

- Access tokens expire after **2 hours**
- LMA automatically refreshes tokens **5 minutes before expiration** using
  the stored refresh token
- Refresh tokens last **365 days** (configurable in Salesforce)
- No manual intervention needed

### Token Storage

- Access and refresh tokens are encrypted with AWS KMS before being written
  to DynamoDB
- Only accessible by the LMA Lambda functions that connect to MCP servers

## Available Salesforce MCP Servers and Tools

The exact set of servers, tools and prompts depends on your org edition and
which servers you activate. For `sobject-all` in a current Developer org:

**Tools**

- `getUserInfo` — current user identity, role, timezone, preferences
- `soqlQuery` — execute a SOQL query
- `find` — SOSL cross-object text search
- `getObjectSchema` — LLM-optimized schema / describe
- `listRecentSobjectRecords` — recently viewed/modified records
- `getRelatedRecords` — traverse parent→child relationships
- `createSobjectRecord` — create a record
- `updateSobjectRecord` — update a record by ID
- `updateRelatedRecord` — update a child record via parent navigation

**Prompts**

- `accountReviewBriefing` — renders an account-review template
  (takes `AccountName`)
- `revenueReconciliationAnalysis` — renders a revenue-reconciliation
  template (takes `Lookback Days`, `Minimum Opportunity Amount`)

The other read/write/metadata-specific servers (`sobject-reads`,
`sobject-mutations`, `metadata-experts`, `salesforce-api-context`,
`data-cloud-queries`, `engagement-interaction`) publish narrower subsets of
the above.

## Security Considerations

### OAuth 2.1 with PKCE

- **PKCE (Proof Key for Code Exchange)** protects against authorization
  code interception
- Required by Salesforce for public clients

### Token Security

- Access and refresh tokens are encrypted with KMS before storage
- DynamoDB table encrypted at rest
- Tokens only accessible by authorized Lambda functions
- Tokens are scoped to the user who authorized (one token per user)

### Permissions

- All MCP operations respect the authorized user's Salesforce permissions
  and field-level security
- Access can be revoked from Salesforce Setup → Connected Apps OAuth Usage
  at any time

## Revoking Access

1. Go to Salesforce Setup
2. Search for **Connected Apps OAuth Usage**
3. Find **LMA MCP Integration**
4. Click **Revoke** next to your username
5. Delete the server in LMA

## Advanced Configuration

### Sandbox or My Domain

The **MCP Server URL** is always on the unified gateway:

```
https://api.salesforce.com/platform/mcp/v1/platform/<server-api-name>
```

Salesforce routes the call to your org based on the bearer token; leave the
host as `api.salesforce.com`.

Your My Domain only affects the OAuth endpoints. For a Sandbox or a custom
domain, override the Authorization / Token URLs in LMA:

- Production: `https://login.salesforce.com/services/oauth2/{authorize,token}`
- Sandbox: `https://test.salesforce.com/services/oauth2/{authorize,token}`
- Custom: `https://<your-domain>.my.salesforce.com/services/oauth2/{authorize,token}`

### Multiple MCP Servers From One Org

If you activate several MCP servers in Setup → MCP Servers (e.g.
`sobject-reads` and `metadata-experts`), add each one as a separate entry
in LMA → Configuration → MCP Servers, with its own URL
(`…/v1/platform/sobject-reads`, `…/v1/platform/metadata-experts`, etc.) and
re-authorize once per entry. They can share the same Connected App.

## Support

- **LMA OAuth implementation:** see the troubleshooting section above and
  try the bundled `scripts/sf-mcp-oauth-e2e.py` tester
- **Salesforce MCP server:** see the Salesforce Platform MCP documentation
- **OAuth configuration:** see Salesforce's Connected Apps and OAuth flow
  documentation

## Summary

✅ **What You Get:**
- AI assistant can query and mutate Salesforce data during meetings
- Automatic token refresh (no manual intervention)
- Secure OAuth 2.1 with PKCE
- Works with any Salesforce org that has Platform MCP enabled

✅ **What You Need:**
- Salesforce admin access (to create the Connected App and activate MCP
  servers)
- Scopes `mcp_api`, `refresh_token`, `offline_access` on the Connected App
- The Consumer Key from the Connected App
- At least one Active MCP server in Setup → MCP Servers
