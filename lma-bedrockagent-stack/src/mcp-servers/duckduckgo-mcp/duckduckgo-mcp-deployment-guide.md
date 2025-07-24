# DuckDuckGo MCP Server Deployment Guide for Amazon Bedrock AgentCore Runtime

This guide walks you through deploying the DuckDuckGo MCP server to Amazon Bedrock AgentCore Runtime using AWS profile `ccm-prod`.

## Prerequisites

1. **AWS CLI configured with ccm-prod profile**
   ```bash
   aws configure --profile ccm-prod
   # Verify profile is working
   aws sts get-caller-identity --profile ccm-prod
   ```

2. **Required tools installation**
   ```bash
   # Install the AgentCore starter toolkit
   pip install bedrock-agentcore-starter-toolkit
   
   # Install MCP dependencies (if not already installed)
   pip install mcp
   ```

3. **Docker installed** (for containerization)

## Step 1: Prepare the MCP Server Structure

Your current DuckDuckGo MCP server structure is already well-organized:

```
lma-bedrockagent-stack/src/mcp-servers/duckduckgo-mcp/
├── Dockerfile
├── __init__.py
├── requirements.txt
└── server.py
```

## Step 2: Set Up Authentication (Cognito User Pool)

Create a Cognito user pool for authentication. Save this as `setup_cognito.sh`:

```bash
#!/bin/bash

# Set the AWS profile
export AWS_PROFILE=ccm-prod

# Create User Pool and capture Pool ID directly
export POOL_ID=$(aws cognito-idp create-user-pool \
  --pool-name "DuckDuckGoMCPUserPool" \
  --policies '{"PasswordPolicy":{"MinimumLength":8}}' \
  --region us-east-1 | jq -r '.UserPool.Id')

# Create App Client and capture Client ID directly
export CLIENT_ID=$(aws cognito-idp create-user-pool-client \
  --user-pool-id $POOL_ID \
  --client-name "DuckDuckGoMCPClient" \
  --no-generate-secret \
  --explicit-auth-flows "ALLOW_USER_PASSWORD_AUTH" "ALLOW_REFRESH_TOKEN_AUTH" \
  --region us-east-1 | jq -r '.UserPoolClient.ClientId')

# Create User
aws cognito-idp admin-create-user \
  --user-pool-id $POOL_ID \
  --username "mcpuser" \
  --temporary-password "Temp123!" \
  --region us-east-1 \
  --message-action SUPPRESS > /dev/null

# Set Permanent Password
aws cognito-idp admin-set-user-password \
  --user-pool-id $POOL_ID \
  --username "mcpuser" \
  --password "MCPPassword123!" \
  --region us-east-1 \
  --permanent > /dev/null

# Authenticate User and capture Access Token
export BEARER_TOKEN=$(aws cognito-idp initiate-auth \
  --client-id "$CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME='mcpuser',PASSWORD='MCPPassword123!' \
  --region us-east-1 | jq -r '.AuthenticationResult.AccessToken')

# Output the required values
echo "Pool ID: $POOL_ID"
echo "Discovery URL: https://cognito-idp.us-east-1.amazonaws.com/$POOL_ID/.well-known/openid-configuration"
echo "Client ID: $CLIENT_ID"
echo "Bearer Token: $BEARER_TOKEN"

# Save these values for later use
echo "export POOL_ID=$POOL_ID" > cognito_config.env
echo "export CLIENT_ID=$CLIENT_ID" >> cognito_config.env
echo "export BEARER_TOKEN=$BEARER_TOKEN" >> cognito_config.env
echo "export DISCOVERY_URL=https://cognito-idp.us-east-1.amazonaws.com/$POOL_ID/.well-known/openid-configuration" >> cognito_config.env
```

Run the script:
```bash
chmod +x setup_cognito.sh
source setup_cognito.sh
```

## Step 3: Configure the MCP Server for Deployment

Navigate to your MCP server directory:
```bash
cd lma-bedrockagent-stack/src/mcp-servers/duckduckgo-mcp
```

Configure the deployment using the AgentCore CLI:
```bash
# Set AWS profile for the session
export AWS_PROFILE=ccm-prod

# Configure the MCP server for deployment
agentcore configure -e server.py --protocol MCP
```

During the configuration process, you'll be prompted for:

1. **Execution Role**: 
   - If you don't have one, press Enter to auto-create
   - The role needs permissions for ECR, CloudWatch, and AgentCore

2. **ECR Repository**: 
   - Press Enter to auto-create a new repository

3. **Dependency File**: 
   - It should auto-detect `requirements.txt`

4. **OAuth Configuration**: 
   - Type `yes`
   - Enter the Discovery URL from Step 2
   - Enter the Client ID from Step 2

## Step 4: Deploy to AWS

Deploy your MCP server:
```bash
# Ensure AWS profile is set
export AWS_PROFILE=ccm-prod

# Launch the deployment
agentcore launch
```

This command will:
1. Build a Docker container with your MCP server
2. Push it to Amazon ECR
3. Create an Amazon Bedrock AgentCore runtime
4. Deploy your server to AWS

After successful deployment, you'll receive an agent runtime ARN like:
```
arn:aws:bedrock-agentcore:us-west-2:ACCOUNT_ID:runtime/duckduckgo-mcp-server-xyz123
```

**Save this ARN** - you'll need it for testing and invocation.

## Step 5: Test Your Deployed MCP Server

### Method 1: Using Python Client

Create a test client `test_deployed_mcp.py`:

```python
import asyncio
import os
import sys
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    # Load configuration
    agent_arn = "YOUR_AGENT_ARN_HERE"  # Replace with actual ARN
    bearer_token = "YOUR_BEARER_TOKEN_HERE"  # Replace with actual token
    
    if not agent_arn or not bearer_token:
        print("Error: Please set agent_arn and bearer_token")
        sys.exit(1)
    
    # URL encode the ARN
    encoded_arn = agent_arn.replace(':', '%3A').replace('/', '%2F')
    mcp_url = f"https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    headers = {
        "authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json"
    }
    
    print(f"Connecting to: {mcp_url}")
    
    async with streamablehttp_client(mcp_url, headers, timeout=120, terminate_on_close=False) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            
            # List available tools
            tools = await session.list_tools()
            print("Available tools:", tools)
            
            # Test the search tool
            search_result = await session.call_tool("search", {
                "query": "Amazon Bedrock AgentCore",
                "max_results": 5
            })
            print("Search result:", search_result)

if __name__ == "__main__":
    asyncio.run(main())
```

### Method 2: Using MCP Inspector

1. Install and run the MCP Inspector:
   ```bash
   npx @modelcontextprotocol/inspector
   ```

2. Open your browser to `http://localhost:6274`

3. Configure the connection:
   - Transport: "Streamable HTTP"
   - URL: `https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/YOUR_ENCODED_ARN/invocations?qualifier=DEFAULT`
   - Authentication: Add your Bearer token
   - Click "Connect"

4. Test the tools:
   - `search`: Test with queries like "Python programming"
   - `fetch_content`: Test with URLs like "https://example.com"

## Step 6: Environment Variables for Easy Access

Create a `.env` file for easy access:
```bash
# Load Cognito configuration
source cognito_config.env

# Add your agent ARN (replace with actual ARN)
echo "export AGENT_ARN=arn:aws:bedrock-agentcore:us-west-2:ACCOUNT_ID:runtime/duckduckgo-mcp-server-xyz123" >> .env
echo "export BEARER_TOKEN=$BEARER_TOKEN" >> .env
echo "export AWS_PROFILE=ccm-prod" >> .env
```

## Troubleshooting

### Common Issues:

1. **Authentication Errors**:
   - Verify your Cognito setup is correct
   - Check that the Bearer token hasn't expired (tokens typically last 1 hour)
   - Regenerate token if needed:
     ```bash
     source cognito_config.env
     export BEARER_TOKEN=$(aws cognito-idp initiate-auth \
       --client-id "$CLIENT_ID" \
       --auth-flow USER_PASSWORD_AUTH \
       --auth-parameters USERNAME='mcpuser',PASSWORD='MCPPassword123!' \
       --region us-east-1 | jq -r '.AuthenticationResult.AccessToken')
     ```

2. **Deployment Failures**:
   - Check IAM permissions for your AWS profile
   - Verify Docker is running
   - Check ECR repository permissions

3. **Connection Issues**:
   - Ensure the agent runtime is in "READY" state
   - Verify the ARN encoding in URLs (: becomes %3A, / becomes %2F)
   - Check AWS region consistency

### Monitoring and Logs

- View CloudWatch logs for your agent runtime
- Use AWS X-Ray for tracing (if enabled)
- Monitor through the AgentCore console

## Next Steps

1. **Integration**: Integrate the deployed MCP server with your AI agents
2. **Scaling**: Configure auto-scaling based on usage patterns
3. **Security**: Implement additional security measures for production use
4. **Monitoring**: Set up comprehensive monitoring and alerting

## Important Notes

- Amazon Bedrock AgentCore is in preview and subject to change
- The MCP server runs on port 8000 inside the container (as configured in your Dockerfile)
- AgentCore expects the MCP endpoint at `/mcp` path
- Sessions are isolated automatically by the platform
- Rate limiting is handled by your DuckDuckGo implementation

Your DuckDuckGo MCP server is now deployed and ready to provide search and content fetching capabilities to AI agents through Amazon Bedrock AgentCore Runtime!
