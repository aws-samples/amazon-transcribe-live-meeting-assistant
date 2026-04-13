# Amazon Quick Suite MCP Action Connector Setup Guide

## Overview

This guide walks you through configuring an MCP action connector in Amazon Quick Suite to access Live Meeting Assistant (LMA) meeting data. Once configured, Quick Suite can search meeting transcripts, retrieve summaries, list meetings, and even schedule or start meetings with LMA's virtual participant feature.

## Why Connect Quick Suite to LMA?

Integrating LMA with Quick Suite enables powerful AI-driven workflows:

### Use Cases

**Meeting Intelligence Queries**
- "Find all meetings where we discussed the Q4 roadmap"
- "What action items came out of yesterday's standup?"
- "Show me meetings with Sarah from last week"

**Automated Meeting Summaries**
- Quick Suite can retrieve and summarize key points from any LMA meeting
- Extract action items with owners and due dates
- Identify key topics and decisions

**Virtual Participant Automation**
- Schedule LMA virtual participants to join future meetings automatically
- Start immediate meeting recordings via Quick Suite workflows
- Integrate meeting capture into broader business processes

**Cross-System Intelligence**
- Combine meeting insights with data from other Quick Suite connectors (Salesforce, Jira, etc.)
- Create automated workflows triggered by meeting events
- Answer questions that span multiple systems (e.g., "What did we decide about the customer issue mentioned in the Salesforce case?")

## Prerequisites

- LMA deployed with MCP Server enabled (v0.2.23 or later)
  - **Important**: During LMA deployment, the `EnableMCP` parameter must be set to `true` (this is the default)
  - If you already have LMA deployed, see [Enabling MCP Server on Existing Deployment](#enabling-mcp-server-on-existing-deployment) below
- Amazon Quick Suite **Enterprise** subscription (required to create action connectors)
- Amazon Quick Suite **Professional or Enterprise** subscription (required to invoke action connectors)
- Quick Suite Author role or higher
- Admin access to LMA CloudFormation stack outputs

### Enabling MCP Server on Existing Deployment

If you deployed LMA before v0.2.23 or with MCP Server disabled, follow these steps:

1. Log into the **AWS Management Console**
2. Navigate to **CloudFormation**
3. Select your LMA stack (default name: `LMA`)
4. Click **Update**
5. Choose **Use current template**
6. Click **Next**
7. Find the **Enable MCP Server Integration** parameter and set it to `true`
8. Click **Next** through the remaining screens
9. Check the acknowledgment boxes and click **Update stack**
10. Wait for the stack update to complete (~10-15 minutes)
11. Return to the **Outputs** tab to see the MCP configuration values

## Authentication: Three-Legged OAuth (3LO)

LMA's MCP server uses **Three-Legged OAuth 2.0 (Authorization Code Flow)** with Amazon Cognito as the identity provider. This ensures secure, user-authenticated access to meeting data.

### How 3LO Works with LMA

1. **User Authorization**: Quick Suite redirects the user to LMA's Cognito login page
2. **User Authenticates**: User logs in with their LMA credentials
3. **Authorization Code**: Cognito returns an authorization code to Quick Suite
4. **Token Exchange**: Quick Suite exchanges the code for access and refresh tokens
5. **Authenticated Requests**: Quick Suite uses the access token to call LMA's MCP tools

### User-Based Access Control (UBAC)

LMA enforces user-based access control through the JWT tokens:
- **Non-admin users** can only access their own meetings
- **Admin users** can access all meetings in the system
- Access control is enforced at the MCP tool level using the `sub` claim from the JWT

## Step 1: Gather LMA MCP Server Configuration

1. Log into the **AWS Management Console**
2. Navigate to **CloudFormation**
3. Select your **LMA main stack** (default name: `LMA`)
4. Click the **Outputs** tab
5. Copy the following values (you'll need them in Step 2):

| Output Key | Description | Example Value |
|------------|-------------|---------------|
| `MCPServerEndpoint` | MCP server URL | `https://gateway-abc123.bedrock-agentcore.us-east-1.amazonaws.com/mcp` |
| `MCPServerClientId` | OAuth Client ID | `1a2b3c4d5e6f7g8h9i0j` |
| `MCPServerClientSecret` | OAuth Client Secret | `secret123...` (store securely!) |
| `MCPServerTokenURL` | OAuth token endpoint | `https://lma-domain.auth.us-east-1.amazoncognito.com/oauth2/token` |
| `MCPServerAuthorizationURL` | OAuth authorization endpoint | `https://lma-domain.auth.us-east-1.amazoncognito.com/oauth2/authorize` |

> **Note**: These outputs are only visible if you deployed LMA with `EnableMCP` set to `true` (default).

> **Security Note**: The `MCPServerClientSecret` is sensitive. Store it securely and never commit it to version control.

## Step 2: Create MCP Integration in Quick Suite

### 2.1 Navigate to Integrations

1. Log into the **Amazon Quick Suite console**
2. Click **Integrations** in the left navigation
3. Click the **Add** button (plus "+" icon)

### 2.2 Configure Integration Details

On the **Create Integration** page:

1. **Name**: `LMA Meeting Assistant`
2. **Description**: `Access Live Meeting Assistant transcripts, summaries, and meeting data`
3. **MCP server endpoint**: Paste the `MCPServerEndpoint` value from Step 1
   ```
   https://gateway-abc123.bedrock-agentcore.us-east-1.amazonaws.com/mcp
   ```
4. Click **Next**

### 2.3 Configure Authentication

1. Select authentication method: **User authentication (OAuth)**

2. Choose configuration approach: **Manual configuration**
   > Note: LMA's Cognito setup does not support Dynamic Client Registration (DCR)

3. Enter OAuth configuration:
   - **Client ID**: Paste `MCPServerClientId` from Step 1
   - **Client Secret**: Paste `MCPServerClientSecret` from Step 1
   - **Token URL**: Paste `MCPServerTokenURL` from Step 1
   - **Auth URL**: Paste `MCPServerAuthorizationURL` from Step 1
   - **Redirect URL**: Use Quick Suite's callback URL (provided by Quick Suite)

4. Click **Create and continue**

### 2.4 Authorize the Integration

1. You'll be redirected to the **LMA Cognito login page**
2. Log in with your LMA credentials (email and password)
3. Review the permissions requested
4. Click **Allow** to authorize Quick Suite to access your LMA data
5. You'll be redirected back to Quick Suite

### 2.5 Review Integration Capabilities

Quick Suite will connect to the LMA MCP server and discover available tools:

**Available Actions:**
- ✅ `search_lma_meetings` - Search across meeting transcripts
- ✅ `get_meeting_transcript` - Retrieve full transcript
- ✅ `get_meeting_summary` - Get AI-generated summary and action items
- ✅ `list_meetings` - List meetings with filters
- ✅ `schedule_meeting` - Schedule future meeting with virtual participant
- ✅ `start_meeting_now` - Start immediate meeting with virtual participant

**Data Access:**
- ✅ Meeting transcripts and summaries stored in LMA's Bedrock Knowledge Base

Click **Next** to continue.

### 2.6 Share Integration (Optional)

To allow other users in your organization to use this integration:

1. Click **Share integration**
2. Select users or groups
3. Click **Save**

> **Note**: Each user will need to authenticate with their own LMA credentials when they first use the integration.

### 2.7 Complete Integration Setup

1. Click **Done** to finish the integration setup
2. Quick Suite will begin creating the action connector
3. Wait for the status to change from **Creating** to **Active** (this may take 1-2 minutes)
4. Once active, the integration is ready to use

## Step 3: Test the Integration

### 3.1 First-Time Authentication

The first time you (or any user) invokes the LMA action connector:

1. Quick Suite will display a **Sign in** button
2. Click **Sign in** to start the OAuth authorization flow
3. You'll be redirected to the **LMA Cognito login page**
4. Log in with your LMA credentials (email and password)
5. Review the permissions requested
6. Click **Allow** to authorize Quick Suite to access your LMA data
7. You'll be redirected back to Quick Suite

> **Note**: This authentication is per-user. Each user must complete this flow once to connect their LMA account.

### 3.2 Test Search Functionality

1. In Quick Suite, open the **Chat** interface
2. Try a natural language query:
   ```
   Search my LMA meetings for discussions about the product roadmap
   ```
3. Quick Suite should invoke the `search_lma_meetings` tool and return results

### 3.3 Test Meeting Retrieval

1. Get a meeting ID from your LMA UI (format: `CallId` like `abc123-def456-ghi789`)
2. In Quick Suite, ask:
   ```
   Get the transcript for LMA meeting abc123-def456-ghi789
   ```
3. Quick Suite should invoke `get_meeting_transcript` and return the full transcript

### 3.4 Test Meeting Listing

1. In Quick Suite, ask:
   ```
   List my recent LMA meetings from the past week
   ```
2. Quick Suite should invoke `list_meetings` with appropriate date filters

## Step 4: Using LMA Tools in Quick Suite

### Search Meetings

**Natural Language:**
```
Find all meetings where we discussed AWS security best practices
```

**Tool Parameters:**
- `query`: "AWS security best practices"
- `maxResults`: 10 (default)
- `startDate`: (optional) ISO 8601 date
- `endDate`: (optional) ISO 8601 date

### Get Meeting Transcript

**Natural Language:**
```
Show me the full transcript for meeting abc123-def456-ghi789
```

**Tool Parameters:**
- `meetingId`: "abc123-def456-ghi789" (required)
- `format`: "text" | "json" | "srt" (default: "text")

### Get Meeting Summary

**Natural Language:**
```
Summarize meeting abc123-def456-ghi789 with action items
```

**Tool Parameters:**
- `meetingId`: "abc123-def456-ghi789" (required)
- `includeActionItems`: true (default)
- `includeTopics`: true (default)

### List Meetings

**Natural Language:**
```
Show me all meetings with Sarah from last month
```

**Tool Parameters:**
- `startDate`: "2025-01-01T00:00:00Z" (optional)
- `endDate`: "2025-01-31T23:59:59Z" (optional)
- `participant`: "Sarah" (optional)
- `status`: "ENDED" | "IN_PROGRESS" | "ALL" (default: "ALL")
- `limit`: 20 (default)

### Schedule Meeting (Virtual Participant)

**Natural Language:**
```
Schedule an LMA virtual participant for my Zoom meeting 123456789 tomorrow at 2pm
```

**Tool Parameters:**
- `meetingName`: "Product Review Meeting" (required)
- `meetingPlatform`: "Zoom" | "Teams" | "Chime" | "Webex" (required)
- `meetingId`: "123456789" (required - numeric ID only)
- `scheduledTime`: "2025-02-01T14:00:00Z" (required - ISO 8601)
- `meetingPassword`: "secret123" (optional)

### Start Meeting Now (Virtual Participant)

**Natural Language:**
```
Start recording my Zoom meeting 987654321 right now
```

**Tool Parameters:**
- `meetingName`: "Emergency Standup" (required)
- `meetingPlatform`: "Zoom" | "Teams" | "Chime" | "Webex" (required)
- `meetingId`: "987654321" (required - numeric ID only)
- `meetingPassword`: "secret456" (optional)

## Troubleshooting

### Issue: "Cannot connect to MCP server"

**Symptoms:**
- Quick Suite shows connection error during setup
- Integration creation fails
- MCP Server outputs not visible in CloudFormation

**Solutions:**
1. Verify `EnableMCP` parameter was set to `true` during LMA deployment (default)
2. Check CloudFormation Outputs tab on the **main LMA stack** - if MCP outputs are missing, update the stack:
   - Go to CloudFormation → LMA stack → Update
   - Ensure **Enable MCP Server Integration** parameter is set to `true`
   - Complete the stack update
3. Verify `MCPServerEndpoint` is correct and accessible
4. Confirm the LMA stack deployed successfully (check CloudFormation status)
5. Test the endpoint manually:
   ```bash
   curl -I https://gateway-abc123.bedrock-agentcore.us-east-1.amazonaws.com/mcp
   ```

### Issue: "Authentication failed" or "Invalid credentials"

**Symptoms:**
- OAuth login fails
- "Invalid client_id or client_secret" error

**Solutions:**
1. Double-check `MCPServerClientId` and `MCPServerClientSecret` from CloudFormation outputs (main LMA stack)
2. Verify you're using the correct Cognito user credentials (email/password from LMA)
3. Ensure the user exists in the LMA Cognito User Pool
4. Verify OAuth scopes are configured in the Cognito client (should be `openid email profile` - pre-configured by LMA)
5. Verify `MCPServerTokenURL` and `MCPServerAuthorizationURL` match your AWS region

### Issue: "Access denied" or "Permission denied"

**Symptoms:**
- Authentication succeeds but tool calls fail
- "User does not have access to this meeting" error

**Solutions:**
1. **Non-admin users** can only access their own meetings
   - Verify the meeting was created by the authenticated user
   - Check the `Owner` field in LMA matches the user's Cognito username
2. **Admin users** can access all meetings
   - Verify the user is in the "Admin" Cognito group
   - Check CloudFormation parameter `AdminEmail` matches the user's email
3. Review LMA's User-Based Access Control (UBAC) documentation

### Issue: "Tool not found" or "Unknown tool"

**Symptoms:**
- Quick Suite doesn't show expected LMA tools
- Tool invocation fails with "not found" error

**Solutions:**
1. Refresh the integration in Quick Suite:
   - Go to **Integrations** → Select LMA integration
   - Click **Actions** → **Refresh tools**
2. Verify the MCP server is running:
   - Check CloudWatch logs for `MCPServerAnalytics` Lambda function
   - Look for errors in `/LMA/lambda/MCPServerAnalytics` log group
3. Confirm the Lambda function has correct environment variables:
   - `CALLS_TABLE`, `TRANSCRIPT_KB_ID`, `MODEL_ARN`

### Issue: "Operation timeout" or "HTTP 424 error"

**Symptoms:**
- Tool calls timeout after 60 seconds
- Quick Suite shows "Operation failed" error

**Solutions:**
1. This is a Quick Suite limitation (60-second timeout)
2. For large transcripts, use pagination or filters:
   - Reduce `maxResults` in search queries
   - Use date filters to narrow results
   - Request specific meeting IDs instead of broad searches
3. Check Lambda function timeout (should be 900 seconds)
4. Review CloudWatch logs for Lambda execution time

### Issue: "No meetings found" or empty results

**Symptoms:**
- Search returns no results
- List meetings returns empty array

**Solutions:**
1. Verify meetings exist in LMA:
   - Log into LMA UI and check the meetings list
   - Confirm meetings have completed (status = "ENDED")
2. Check date filters:
   - Ensure `startDate` and `endDate` are in ISO 8601 format
   - Verify timezone offsets (use UTC: `2025-01-29T00:00:00Z`)
3. For search queries:
   - Verify `TRANSCRIPT_KB_ID` is configured in Lambda environment
   - Check that the Bedrock Knowledge Base sync has completed
   - Allow 15-30 minutes after meeting ends for KB indexing
4. Review UBAC permissions (see "Access denied" above)

### Issue: "Invalid meeting ID format"

**Symptoms:**
- "Meeting not found" error
- "Invalid meetingId parameter" error

**Solutions:**
1. Meeting IDs in LMA are UUIDs (format: `abc123-def456-ghi789`)
2. Get the correct meeting ID from:
   - LMA UI meetings list (copy the `CallId`)
   - `list_meetings` tool response
3. Do not use meeting names or timestamps as IDs

### Issue: Virtual participant tools not working

**Symptoms:**
- `schedule_meeting` or `start_meeting_now` fails
- "Virtual participant feature not available" error

**Solutions:**
1. Verify the Virtual Participant stack is deployed:
   - Check for `LMA-VIRTUALPARTICIPANTSTACK` in CloudFormation
2. Ensure meeting platform is supported:
   - Zoom, Teams, Chime, Webex (case-sensitive)
3. Meeting ID format:
   - Must be numeric only (e.g., "123456789")
   - Do not include platform-specific prefixes
4. Check EventBridge scheduler permissions:
   - Lambda needs `states:StartExecution` permission
   - Verify `VPScheduleGroupName` parameter is correct

### Issue: "Custom HTTP headers not supported"

**Symptoms:**
- Error about unsupported headers
- Authentication headers rejected

**Solutions:**
1. This is a Quick Suite limitation (no custom headers)
2. LMA's MCP server uses standard OAuth bearer tokens (supported)
3. Do not attempt to add custom headers in Quick Suite configuration
4. Authentication is handled automatically via OAuth flow

### Debugging Tips

**Enable Detailed Logging:**
1. Go to CloudFormation → LMA stack → Parameters
2. Update `CloudWatchLogsExpirationInDays` if needed
3. Check Lambda logs in CloudWatch:
   - Log group: `/LMA/lambda/MCPServerAnalytics`
   - Look for `INFO`, `WARNING`, and `ERROR` level messages

**Test OAuth Flow Manually:**
```bash
# 1. Get authorization code (open in browser)
https://lma-domain.auth.us-east-1.amazoncognito.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&response_type=code&scope=openid+email+profile&redirect_uri=YOUR_CALLBACK_URL

# 2. Exchange code for tokens
curl -X POST https://lma-domain.auth.us-east-1.amazoncognito.com/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code" \
  -d "client_id=YOUR_CLIENT_ID" \
  -d "client_secret=YOUR_CLIENT_SECRET" \
  -d "code=AUTHORIZATION_CODE" \
  -d "redirect_uri=YOUR_CALLBACK_URL"

# 3. Test MCP endpoint with access token
curl -X POST https://gateway-abc123.bedrock-agentcore.us-east-1.amazonaws.com/mcp \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "test search", "maxResults": 5}'
```

**Check IAM Permissions:**
1. Verify `MCPServerGatewayExecutionRole` has:
   - `lambda:InvokeFunction` on `MCPServerAnalyticsFunction`
   - Trust relationship with `bedrock-agentcore.amazonaws.com`
2. Verify `MCPServerAnalyticsFunction` has:
   - `dynamodb:Query`, `dynamodb:GetItem` on `EventSourcingTable`
   - `s3:GetObject` on recordings bucket
   - `bedrock:Retrieve`, `bedrock:RetrieveAndGenerate` on Knowledge Base
   - `bedrock:InvokeModel` on foundation models

## Additional Resources

- [LMA GitHub Repository](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant)
- [Amazon Quick Suite MCP Integration Documentation](https://docs.aws.amazon.com/quicksuite/latest/userguide/mcp-integration.html)
- [LMA User-Based Access Control (UBAC)](../lma-ai-stack/README_UBAC.md)
- [Model Context Protocol (MCP) Specification](https://modelcontextprotocol.io/)
- [Amazon Cognito OAuth 2.0 Documentation](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-app-integration.html)

## Security Best Practices

1. **Protect Client Secrets**: Never commit `MCPServerClientSecret` to version control
2. **Use HTTPS Only**: All OAuth and MCP communication must use HTTPS
3. **Rotate Credentials**: Periodically rotate Cognito client secrets
4. **Limit Scope**: Only grant necessary OAuth scopes (`openid email profile`)
5. **Monitor Access**: Review CloudWatch logs for unauthorized access attempts
6. **UBAC Enforcement**: Ensure non-admin users can only access their own meetings
7. **Token Expiration**: Access tokens expire after 1 hour (refresh tokens valid for 30 days)

## Support

For issues or questions:
1. Check the [Troubleshooting](#troubleshooting) section above
2. Review CloudWatch logs for detailed error messages
3. Open an issue on the [LMA GitHub repository](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/issues)
4. Contact your AWS support team for Quick Suite-specific issues
