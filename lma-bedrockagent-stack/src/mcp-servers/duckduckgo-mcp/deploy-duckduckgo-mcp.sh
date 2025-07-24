#!/bin/bash

# DuckDuckGo MCP Server Deployment Script for Amazon Bedrock AgentCore
# Usage: ./deploy-duckduckgo-mcp.sh

set -e

echo "🚀 Starting DuckDuckGo MCP Server Deployment to Amazon Bedrock AgentCore"
echo "=================================================================="

# Set AWS profile
export AWS_PROFILE=default
echo "✅ Using AWS profile: $AWS_PROFILE"

# Update AWS CLI to latest version
echo "🔄 Updating AWS CLI..."
pip install --upgrade awscli boto3

# Verify AWS credentials
echo "🔍 Verifying AWS credentials..."
aws sts get-caller-identity --profile default > /dev/null
echo "✅ AWS credentials verified"

# Check if required tools are installed
echo "🔍 Checking required tools..."

if ! command -v agentcore &> /dev/null; then
    echo "❌ AgentCore CLI not found. Installing..."
    pip install --upgrade bedrock-agentcore-starter-toolkit mcp
    echo "✅ AgentCore CLI and dependencies installed"
else
    echo "✅ AgentCore CLI found"
    echo "🔄 Updating AgentCore dependencies..."
    pip install --upgrade bedrock-agentcore-starter-toolkit mcp
    echo "✅ Dependencies updated"
fi

if ! command -v docker &> /dev/null; then
    echo "❌ Docker not found. Please install Docker and try again."
    exit 1
else
    echo "✅ Docker found"
fi

if ! command -v jq &> /dev/null; then
    echo "❌ jq not found. Please install jq and try again."
    echo "   macOS: brew install jq"
    echo "   Ubuntu: sudo apt-get install jq"
    exit 1
else
    echo "✅ jq found"
fi

# Check if we're in the right directory
if [ ! -f "duckduckgo_server.py" ]; then
    echo "❌ DuckDuckGo MCP server files not found. Please run this script from the duckduckgo-mcp directory."
    exit 1
fi

echo "✅ DuckDuckGo MCP server files found"

# Step 1: Configure MCP server for deployment
echo ""
echo "📋 Step 1: Configuring MCP server for deployment..."

# Check if already configured
if [ -f ".agentcore/config.json" ]; then
    echo "🔍 Found existing AgentCore configuration. Do you want to reconfigure? (y/n)"
    read -r reconfigure
    if [ "$reconfigure" = "y" ] || [ "$reconfigure" = "Y" ]; then
        echo "🔄 Reconfiguring..."
        rm -rf .agentcore
    else
        echo "✅ Using existing configuration"
    fi
fi

if [ ! -f ".agentcore/config.json" ]; then
    echo "🔧 Running AgentCore configuration..."
    echo "📝 You will be prompted for:"
    echo "   - Execution Role: Press Enter to auto-create"
    echo "   - ECR Repository: Press Enter to auto-create"
    echo "   - OAuth: Type 'no' (simplified deployment)"
    echo ""
    echo "Press Enter to continue..."
    read -r
    
    agentcore configure -e duckduckgo_server.py --protocol MCP
    echo "✅ AgentCore configuration completed"
fi

# Step 2: Deploy
echo ""
echo "📋 Step 2: Deploying to AWS..."
echo "🚀 Launching deployment (this may take several minutes)..."

agentcore launch

echo ""
echo "✅ Deployment completed!"

# Step 3: Extract and save the Agent ARN
echo ""
echo "📋 Step 3: Extracting deployment information..."

# The ARN should be in the output, but let's also check the config
if [ -f ".agentcore/config.json" ]; then
    AGENT_ARN=$(jq -r '.agent_runtime_arn // empty' .agentcore/config.json)
    if [ -n "$AGENT_ARN" ] && [ "$AGENT_ARN" != "null" ]; then
        echo "✅ Agent Runtime ARN: $AGENT_ARN"
        
        # Save to environment file
        cat > duckduckgo-mcp.env << EOF
export AGENT_ARN=$AGENT_ARN
export AWS_PROFILE=default
EOF
        echo "✅ Environment configuration saved to duckduckgo-mcp.env"
    else
        echo "⚠️  Could not extract Agent ARN from config. Please check the deployment output above."
    fi
fi

# Step 4: Create test client
echo ""
echo "📋 Step 4: Creating test client..."

cat > test_duckduckgo_mcp.py << 'EOF'
import asyncio
import os
import sys
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    # Load environment variables
    agent_arn = os.getenv('AGENT_ARN')
    
    if not agent_arn:
        print("❌ Error: AGENT_ARN environment variable not set")
        print("💡 Run: source duckduckgo-mcp.env")
        sys.exit(1)
    
    # URL encode the ARN
    encoded_arn = agent_arn.replace(':', '%3A').replace('/', '%2F')
    mcp_url = f"https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    headers = {
        "Content-Type": "application/json"
    }
    
    print(f"🔗 Connecting to: {mcp_url}")
    
    try:
        async with streamablehttp_client(mcp_url, headers, timeout=120, terminate_on_close=False) as (
            read_stream,
            write_stream,
            _,
        ):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                
                # List available tools
                print("\n📋 Listing available tools...")
                tools = await session.list_tools()
                print(f"✅ Available tools: {[tool.name for tool in tools.tools]}")
                
                # Test the search tool
                print("\n🔍 Testing search tool...")
                search_result = await session.call_tool("search", {
                    "query": "Amazon Bedrock AgentCore MCP",
                    "max_results": 3
                })
                print(f"✅ Search completed")
                print(f"📄 Search results: {str(search_result.content)[:500]}...")
                
                # Test the fetch_content tool
                print("\n🌐 Testing fetch_content tool...")
                fetch_result = await session.call_tool("fetch_content", {
                    "url": "https://example.com"
                })
                print(f"✅ Content fetch completed: {len(str(fetch_result.content))} characters")
                print(f"📄 Content preview: {str(fetch_result.content)[:200]}...")
                
                print("\n🎉 All tests completed successfully!")
                
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        print("💡 Check that:")
        print("   - The agent runtime is in READY state")
        print("   - The ARN is correct")
        print("   - Your AWS credentials are valid")

if __name__ == "__main__":
    asyncio.run(main())
EOF

echo "✅ Test client created: test_duckduckgo_mcp.py"

# Final instructions
echo ""
echo "🎉 Deployment Complete!"
echo "======================"
echo ""
echo "📋 Next Steps:"
echo "1. Load environment variables:"
echo "   source duckduckgo-mcp.env"
echo ""
echo "2. Test your deployment:"
echo "   python test_duckduckgo_mcp.py"
echo ""
echo "3. Use MCP Inspector for interactive testing:"
echo "   npx @modelcontextprotocol/inspector"
echo ""
echo "📁 Files created:"
echo "   - duckduckgo-mcp.env (Deployment environment)"
echo "   - test_duckduckgo_mcp.py (Test client)"
echo "   - duckduckgo-mcp-deployment-guide.md (Full documentation)"
echo ""
echo "� Your DuckDuckGo MCP server is now deployed and ready to use!"

if [ -n "$AGENT_ARN" ]; then
    echo "📋 Agent Runtime ARN: $AGENT_ARN"
fi
