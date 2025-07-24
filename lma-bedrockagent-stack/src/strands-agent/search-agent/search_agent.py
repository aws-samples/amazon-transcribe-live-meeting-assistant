import os
import logging
from strands import Agent
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client
from bedrock_agentcore.runtime import BedrockAgentCoreApp

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize BedrockAgentCore app
app = BedrockAgentCoreApp()

# Global agent instance
search_agent = None

def initialize_agent():
    """Initialize the Strands agent with MCP server connection"""
    global search_agent
    
    if search_agent is not None:
        return search_agent
    
    try:
        # Get AgentCore runtime configuration
        agent_runtime_arn = os.getenv('DUCKDUCKGO_MCP_ARN')
        if not agent_runtime_arn:
            raise ValueError("DUCKDUCKGO_MCP_ARN environment variable not set")
            
        # URL encode the ARN for use in the URL
        encoded_arn = agent_runtime_arn.replace(':', '%3A').replace('/', '%2F')
        mcp_server_url = f"https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
        
        logger.info(f"Connecting to MCP Server at: {mcp_server_url}")
        
        # Configure headers with AWS authentication
        headers = {
            "Content-Type": "application/json"
        }
        
        # Connect to the MCP server with authentication
        mcp_search_server = MCPClient(
            lambda: streamablehttp_client(
                mcp_server_url,
                headers=headers,
                timeout=120,
                terminate_on_close=False
            )
        )
        
        with mcp_search_server:
            # Create the search agent with system prompt
            search_agent = Agent(
                system_prompt="""You are an AI Search Assistant with access to web search capabilities. 
                You can search for information and fetch content from URLs to help users find accurate, 
                up-to-date information.

                Rules:
                - Always use the search and fetch_content tools provided by the MCP server
                - Provide accurate, well-sourced information based on search results
                - If you cannot find information, clearly state that
                - Summarize search results in a helpful, organized manner
                - Include relevant URLs when appropriate
                """
            )

            # List and register MCP tools
            mcp_tools = mcp_search_server.list_tools_sync()
            logger.info(f"Available MCP tools: {[tool.tool_name for tool in mcp_tools]}")
            search_agent.tool_registry.process_tools(mcp_tools)
            
        return search_agent
        
    except Exception as e:
        logger.error(f"Failed to initialize agent: {str(e)}")
        raise

@app.entrypoint
def invoke(payload):
    """
    AgentCore Runtime entrypoint for processing user requests
    
    Args:
        payload: JSON payload containing user input
        
    Returns:
        dict: Response containing the agent's output
    """
    try:
        logger.info(f"Received payload: {payload}")
        
        # Initialize agent if not already done
        agent = initialize_agent()
        
        # Extract user message from payload
        user_message = payload.get("prompt", payload.get("message", "Hello"))
        
        if not user_message:
            return {
                "error": "No prompt or message found in payload",
                "status": "error"
            }
        
        logger.info(f"Processing user message: {user_message}")
        
        # Process the message with the agent
        response = agent(user_message)
        
        # Return structured response
        result = {
            "response": str(response),
            "status": "success",
            "agent_type": "search_agent"
        }
        
        logger.info(f"Agent response generated successfully")
        return result
        
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        return {
            "error": str(e),
            "status": "error",
            "agent_type": "search_agent"
        }

@app.entrypoint
async def invoke_streaming(payload):
    """
    Streaming version of the agent invocation for real-time responses
    
    Args:
        payload: JSON payload containing user input
        
    Yields:
        dict: Streaming response events
    """
    try:
        logger.info(f"Received streaming payload: {payload}")
        
        # Initialize agent if not already done
        agent = initialize_agent()
        
        # Extract user message from payload
        user_message = payload.get("prompt", payload.get("message", "Hello"))
        
        if not user_message:
            yield {
                "error": "No prompt or message found in payload",
                "status": "error"
            }
            return
        
        logger.info(f"Processing streaming message: {user_message}")
        
        # Stream the agent response
        stream = agent.stream_async(user_message)
        async for event in stream:
            yield {
                "event": str(event),
                "status": "streaming",
                "agent_type": "search_agent"
            }
            
        # Final completion event
        yield {
            "status": "complete",
            "agent_type": "search_agent"
        }
        
    except Exception as e:
        logger.error(f"Error in streaming request: {str(e)}")
        yield {
            "error": str(e),
            "status": "error",
            "agent_type": "search_agent"
        }

def main():
    """Local development and testing function"""
    try:
        # For local testing, use local MCP server
        os.environ['DUCKDUCKGO_MCP_ARN'] = os.getenv('DUCKDUCKGO_MCP_ARN', 'http://localhost:8000/mcp')
        
        # Initialize agent for local testing
        agent = initialize_agent()
        
        print("\n👨‍💻 LMA Strands Search Agent (AgentCore Compatible)")
        print("=" * 55)
        print("\n📋 Try: 'What are the latest AWS services?' or 'Search for Python tutorials'")
        print("💡 This agent is now compatible with Bedrock AgentCore Runtime!")
        print(f"\n💻 Using MCP Server: {os.getenv('DUCKDUCKGO_MCP_ARN')}")
        
        while True:
            user_input = input("\n🎯 Your request: ")
            
            if user_input.lower() in ["exit", "quit", "bye"]:
                print("👋 Happy searching!")
                break
            
            print("\n🤔 Processing...\n")
            response = agent(user_input)
            print(f"\n🤖 Agent: {response}")
            
    except Exception as e:
        logger.error(f"Error in main: {str(e)}")
        print(f"❌ Error: {e}")
        print("💡 Make sure the MCP server is running and accessible")

if __name__ == "__main__":
    # Check if running in AgentCore runtime or local development
    if os.getenv('AWS_LAMBDA_FUNCTION_NAME') or os.getenv('AGENTCORE_RUNTIME'):
        # Running in AgentCore - start the HTTP server
        logger.info("Starting in AgentCore Runtime mode")
        app.run()
    else:
        # Running locally - start interactive mode
        logger.info("Starting in local development mode")
        main()
