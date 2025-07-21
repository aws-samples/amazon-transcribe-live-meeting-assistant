from strands import Agent
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client

def main():
    # Connect to the quiz MCP server
    print("\nConnecting to MCP Server...")
    mcp_search_server = MCPClient(lambda: streamablehttp_client("http://localhost:8000/mcp"))

    try:
        with mcp_search_server:

            # Create the subject expert agent with a system prompt
            subject_expert = Agent(
                system_prompt="""You are a Computer Science Subject Expert with access to 
                an external search service. You can search content or fetch a url, retrieve 
                content for yor user, and check their answers.

                Rules:
                - You must use the tools provided to you by the MCP server.
                - You must NOT make up your own content or answers.
                """
            )

            # List the tools available on the MCP server...
            mcp_tools = mcp_search_server.list_tools_sync()
            print(f"Available tools: {[tool.tool_name for tool in mcp_tools]}")

            # ... and add them to the agent
            subject_expert.tool_registry.process_tools(mcp_tools)

            # Start an interactive learning session
            print("\n👨‍💻 LMA Strands Agent with Search MCP Integration")
            print("=" * 50)
            print("\n📋 Try: 'What AWS services are available?' or 'Give me the latest AWS services'")

            while True:
                user_input = input("\n🎯 Your request: ")
                
                if user_input.lower() in ["exit", "quit", "bye"]:
                    print("👋 Happy searching!")
                    break
                
                print("\n🤔 Processing...\n")
                subject_expert(user_input)
               
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        print("💡 Make sure the search service is running: python server.py")

if __name__ == "__main__":
    main()