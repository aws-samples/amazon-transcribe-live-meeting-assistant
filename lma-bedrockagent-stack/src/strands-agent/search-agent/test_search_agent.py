#!/usr/bin/env python3
import os
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def test_search_agent():
    """Test the deployed search agent"""
    try:
        # Load environment variables
        agent_arn = os.getenv('AGENT_ARN')
        if not agent_arn:
            print("❌ Error: AGENT_ARN environment variable not set")
            print("💡 Run: source config.env")
            return

        # URL encode the ARN
        encoded_arn = agent_arn.replace(':', '%3A').replace('/', '%2F')
        agent_url = f"https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
        
        print(f"\n🔗 Connecting to agent at: {agent_url}")
        
        # Configure headers
        headers = {
            "Content-Type": "application/json"
        }
        
        # Test queries
        test_queries = [
            "What are the latest AWS services?",
            "Search for Python best practices",
            "Tell me about Amazon Bedrock"
        ]
        
        # Connect and test
        async with streamablehttp_client(agent_url, headers=headers, timeout=120) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                
                print("\n✅ Connected to agent successfully")
                
                for query in test_queries:
                    print(f"\n🔍 Testing query: {query}")
                    response = await session.call_tool("invoke", {
                        "prompt": query
                    })
                    print(f"\n📝 Response: {response.content}")
                    
                print("\n🎉 All tests completed successfully!")

    except Exception as e:
        print(f"\n❌ Error: {str(e)}")
        print("\n💡 Troubleshooting tips:")
        print("1. Check that the agent is deployed and in READY state")
        print("2. Verify the ARN is correct")
        print("3. Ensure AWS credentials are valid")
        print("4. Check that the DuckDuckGo MCP server is accessible")

if __name__ == "__main__":
    print("\n🚀 Testing Search Agent")
    print("=====================")
    asyncio.run(test_search_agent())
