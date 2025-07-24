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
