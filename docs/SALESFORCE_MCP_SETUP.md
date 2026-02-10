# Salesforce MCP Server Setup Guide

## Overview

This guide walks you through setting up the Salesforce MCP server with OAuth 2.1 authentication in LMA. The Salesforce MCP server provides AI agents with the ability to query and interact with Salesforce data during meetings.

## Prerequisites

- LMA deployed with OAuth 2.1 support (0.2.23 and above)
- Salesforce account (Developer Edition, Sandbox, or Production)
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

3. **Selected OAuth Scopes:** Add these scopes (in order):
   - ✅ Manage user data via APIs (api)
   - ✅ Perform requests at any time (refresh_token, offline_access)
   - ✅ Access the Salesforce API Platform (sfap_api)
   - ✅ Access Einstein GPT services (einstein_gpt_api)

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
5. **Note:** You don't need the Consumer Secret for this flow

## Step 3: Add Salesforce MCP Server in LMA

### 3.1 Navigate to MCP Servers

1. Log into your LMA application
2. Go to **Configuration** → **MCP Servers**
3. Click the **Custom Servers** tab

### 3.2 Add Custom HTTP Server

1. Fill in the server details:
   - **Server Name:** `Salesforce`
   - **Server URL:** `https://api.salesforce.com/platform/mcp/v1-beta.2/sobject-all`
   - **Description (Optional):** `Salesforce MCP server for working with Salesforce data`

2. Check ✅ **This server requires authentication**

3. Click **Add Server**

### 3.3 Configure OAuth Authentication

The authentication modal will open automatically:

1. **Authentication Type:** Select `OAuth 2.1 with PKCE (User Authorization)`

2. **OAuth Provider:** Select `Salesforce`
   - Authorization and Token URLs will be pre-filled
   - Scopes will be pre-filled with: `api refresh_token offline_access sfap_api einstein_gpt_api`

3. **Client ID:** Paste your Consumer Key from Step 2.4

4. **Scopes:** Verify the scopes are correct (should be pre-filled)

5. Click **Authorize with OAuth**

### 3.4 Complete Authorization

1. A popup window will open with Salesforce login
2. Log into your Salesforce org (if not already logged in)
3. Review the permissions being requested
4. Click **Allow** to grant access
5. The popup will show "✅ Authorization complete!"
6. The popup will close automatically

## Step 4: Verify Installation

### 4.1 Check Installed Servers

1. Go to **Configuration** → **MCP Servers**
2. Click the **Installed Servers** tab
3. You should see **Salesforce** with:
   - Status: `ACTIVE`
   - Package Type: `streamable-http`
   - Authentication: OAuth 2.1

### 4.2 Test in Chat

1. Start or join a meeting
2. Open the chat assistant
3. Ask: `"What tools do you have for Salesforce?"`
4. You should see Salesforce tools listed:
   - `Salesforce_describe_global`
   - `Salesforce_describe_sobject`
   - `Salesforce_soql_query`

5. Test a query: `"List products in Salesforce"`
6. The assistant should query Salesforce and return results!

## Troubleshooting

### Issue: "redirect_uri_mismatch" Error

**Cause:** The callback URL in Salesforce doesn't match the one being sent

**Solution:**
- Verify the callback URL in Salesforce includes the `#` character
- Should be: `https://domain/#/oauth/callback` (with hash)
- NOT: `https://domain/oauth/callback` (without hash)

### Issue: "401 Unauthorized" When Connecting

**Cause:** Missing required OAuth scopes

**Solution:**
- Verify all 5 scopes are selected in Salesforce Connected App
- Most important: `sfap_api` (Salesforce API Platform)
- Delete and re-add the server with correct scopes

### Issue: "invalid_client_id" Error

**Cause:** Wrong Consumer Key entered

**Solution:**
- Go back to Salesforce → App Manager
- Find your Connected App → Manage Consumer Details
- Copy the correct Consumer Key
- Delete the server in LMA and add it again with the correct key

### Issue: Token Expired

**Cause:** Access tokens expire after 2 hours

**Solution:**
- Tokens are automatically refreshed before expiration
- If refresh fails, delete and re-authorize the server
- Refresh tokens last 365 days (1 year)

## Token Management

### Automatic Token Refresh

- Access tokens expire after **2 hours**
- LMA automatically refreshes tokens **5 minutes before expiration**
- Refresh tokens last **365 days** (configurable in Salesforce)
- No manual intervention needed

### Token Storage

- Tokens are encrypted with AWS KMS
- Stored in DynamoDB
- Only accessible by Lambda functions
- Compliant with security best practices

## Available Salesforce Tools

Once connected, the following tools are available to the AI assistant:

### 1. Salesforce_describe_global
Lists all available Salesforce objects (Account, Contact, Opportunity, etc.)

**Example:** `"What Salesforce objects are available?"`

### 2. Salesforce_describe_sobject
Describes the fields and metadata for a specific Salesforce object

**Example:** `"Describe the Account object in Salesforce"`

### 3. Salesforce_soql_query
Executes SOQL queries against Salesforce data

**Example:** `"List all accounts in Salesforce"`
**Example:** `"Show me opportunities closing this month"`
**Example:** `"Find contacts with email containing @example.com"`

### 4. Salesforce_create_record
Creates new records in Salesforce

**Example:** `"Create a new account named 'Acme Corp' with industry 'Technology'"`
**Example:** `"Create a contact named John Doe with email john@example.com"`

### 5. Salesforce_update_record
Updates existing records in Salesforce

**Example:** `"Update account ABC123 to set annual revenue to 1000000"`
**Example:** `"Change the status of opportunity XYZ789 to 'Closed Won'"`

### 6. Salesforce_delete_record
Deletes records from Salesforce

**Example:** `"Delete the test account with ID 001ABC123"`

**Note:** The exact tools available depend on the Salesforce MCP server version and your Salesforce permissions.

## Security Considerations

### OAuth 2.1 with PKCE

- **PKCE (Proof Key for Code Exchange)** protects against authorization code interception
- More secure than OAuth 2.0
- Required by Salesforce for public clients

### Token Security

- Access tokens encrypted with KMS before storage
- Refresh tokens encrypted with KMS before storage
- DynamoDB table encrypted at rest
- Tokens only accessible by authorized Lambda functions

### Permissions

- Users only see their own authorized connections
- Tokens tied to the user who authorized
- Can be revoked in Salesforce at any time

## Revoking Access

To revoke LMA's access to Salesforce:

1. Go to Salesforce Setup
2. Search for **"Connected Apps OAuth Usage"**
3. Find **LMA MCP Integration**
4. Click **Revoke** next to your username
5. Delete the server in LMA

## Advanced Configuration

### Custom Salesforce Instance

If using a Sandbox or custom domain:

1. When adding the server, use your instance URL:
   - Production: `https://api.salesforce.com/platform/mcp/v1-beta.2/sobject-all`
   - Sandbox: `https://test.salesforce.com/platform/mcp/v1-beta.2/sobject-all`
   - Custom: `https://your-domain.my.salesforce.com/platform/mcp/v1-beta.2/sobject-all`

2. Update Authorization URL in OAuth config:
   - Production: `https://login.salesforce.com/services/oauth2/authorize`
   - Sandbox: `https://test.salesforce.com/services/oauth2/authorize`

### Additional Scopes

If you need additional Salesforce permissions, add more scopes in the Connected App:

- `full` - Full access to all data
- `web` - Access to web applications
- `chatter_api` - Access to Chatter
- `custom_permissions` - Access to custom permissions

Then update the scopes in LMA when authorizing.

## Support

For issues specific to:
- **LMA OAuth implementation:** Check LMA documentation
- **Salesforce MCP server:** Check [Salesforce MCP documentation](https://developer.salesforce.com/docs/platform/mcp)
- **OAuth configuration:** Check [Salesforce OAuth documentation](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_flows.htm)

## Summary

✅ **What You Get:**
- AI assistant can query Salesforce data during meetings
- Automatic token refresh (no manual intervention)
- Secure OAuth 2.1 with PKCE
- Works with any Salesforce org (Developer, Sandbox, Production)

✅ **What You Need:**
- Salesforce admin access (to create Connected App)
- 5 minutes to set up
- Consumer Key from Salesforce

That's it! Your AI assistant can now access Salesforce data during meetings.