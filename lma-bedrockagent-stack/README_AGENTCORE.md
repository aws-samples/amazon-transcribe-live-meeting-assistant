# LMA Strands Agent and MCP Server - Bedrock AgentCore Deployment

This directory contains a Strands agent and MCP server implementation that has been updated to be compatible with AWS Bedrock AgentCore Runtime.

## Overview

The implementation consists of two main components:

1. **Strands Search Agent** (`src/strands-agent/search-agent/`) - An AI agent that can search the web and fetch content
2. **DuckDuckGo MCP Server** (`src/mcp-servers/duckduckgo-mcp/`) - A Model Context Protocol server that provides search and content fetching tools

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                 Bedrock AgentCore Runtime                   │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────────────┐    ┌─────────────────────────────┐ │
│  │   Strands Agent     │    │      MCP Server             │ │
│  │                     │    │                             │ │
│  │ - BedrockAgentCore  │◄──►│ - FastMCP Server            │ │
│  │   App Wrapper       │    │ - DuckDuckGo Search         │ │
│  │ - HTTP Endpoints    │    │ - Content Fetching          │ │
│  │ - MCP Client        │    │ - Rate Limiting             │ │
│  └─────────────────────┘    └─────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## Key Features

### Strands Agent
- **AgentCore Compatible**: Uses `BedrockAgentCoreApp` wrapper for HTTP service compatibility
- **Environment-based Configuration**: MCP server URL configured via environment variables
- **Dual Mode Operation**: Can run locally for development or in AgentCore Runtime
- **Streaming Support**: Supports both synchronous and asynchronous streaming responses
- **Error Handling**: Comprehensive error handling and logging

### MCP Server
- **Stateless HTTP**: Compatible with AgentCore's stateless requirements
- **Rate Limiting**: Built-in rate limiting to prevent API abuse
- **Search Tools**: DuckDuckGo web search functionality
- **Content Fetching**: Webpage content extraction and parsing
- **Logging**: Comprehensive logging for debugging and monitoring

## How Endpoint Discovery Works

### Development Mode
```python
# Local development - hardcoded localhost
mcp_server_url = "http://localhost:8000/mcp"
```

### Production Mode (AgentCore Runtime)
```python
# Production - environment variable from deployment
mcp_server_url = os.getenv('MCP_SERVER_URL', 'http://localhost:8000/mcp')
```

The deployment script automatically:
1. Deploys the MCP server and gets its runtime ARN
2. Constructs the AgentCore endpoint URL
3. Passes this URL to the Strands agent as an environment variable

## Deployment

### Prerequisites

1. **AWS CLI** configured with appropriate permissions
2. **Docker** installed and running
3. **Python 3.11+** with pip
4. **bedrock-agentcore-starter-toolkit** (installed automatically by script)

### Quick Deployment

The `publish.sh` script now supports both CloudFormation template publishing and AgentCore deployment:

```bash
# Make the deployment script executable
chmod +x publish.sh

# For CloudFormation template publishing only:
./publish.sh <cfn_bucket> <cfn_prefix> <region> [public]

# For CloudFormation template publishing AND AgentCore deployment:
./publish.sh <cfn_bucket> <cfn_prefix> <region> [public] deploy-agentcore
```

**Examples:**
```bash
# Publish CloudFormation template only
./publish.sh my-bucket my-prefix us-east-1

# Publish CloudFormation template and deploy AgentCore components
./publish.sh my-bucket my-prefix us-east-1 public deploy-agentcore
```

### Manual Deployment Steps

#### 1. Deploy MCP Server

```bash
cd src/mcp-servers/duckduckgo-mcp

# Install dependencies
pip install -r requirements.txt

# Configure for AgentCore
agentcore configure --entrypoint server.py --protocol MCP --execution-role <IAM_ROLE_ARN>

# Deploy
agentcore launch
```

#### 2. Deploy Strands Agent

```bash
cd src/strands-agent/search-agent

# Install dependencies
pip install -r requirements.txt

# Configure with MCP server URL
agentcore configure \
    --entrypoint search_agent.py \
    --execution-role <IAM_ROLE_ARN> \
    --environment-variables MCP_SERVER_URL=<MCP_SERVER_URL>

# Deploy
agentcore launch
```

## Testing

### Local Testing

```bash
# Terminal 1: Start MCP Server
cd src/mcp-servers/duckduckgo-mcp
python server.py

# Terminal 2: Test Strands Agent
cd src/strands-agent/search-agent
python search_agent.py
```

### Production Testing

```bash
# Test deployed agent
agentcore invoke '{"prompt": "Search for the latest AWS services"}' --agent-arn <AGENT_ARN>

# Or use boto3
python -c "
import boto3
import json

client = boto3.client('bedrock-agentcore')
response = client.invoke_agent_runtime(
    agentRuntimeArn='<AGENT_ARN>',
    runtimeSessionId='test-session',
    payload=json.dumps({'prompt': 'Hello world'}).encode()
)
print(response)
"
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MCP_SERVER_URL` | URL of the MCP server | `http://localhost:8000/mcp` |
| `AWS_REGION` | AWS region for deployment | `us-east-1` |
| `AGENTCORE_RUNTIME` | Flag indicating AgentCore runtime | Not set |

### IAM Permissions

The deployment script creates IAM roles with the following permissions:

#### MCP Server Role
- CloudWatch Logs (create/write)
- Basic execution permissions

#### Strands Agent Role
- CloudWatch Logs (create/write)
- AgentCore runtime invocation
- Basic execution permissions

## Monitoring

### CloudWatch Logs

Both components log to CloudWatch:
- **MCP Server**: `/aws/bedrock-agentcore/mcp-server`
- **Strands Agent**: `/aws/bedrock-agentcore/strands-agent`

### Key Metrics to Monitor

- Request latency
- Error rates
- Token usage
- Session duration
- Tool invocation frequency

## Troubleshooting

### Common Issues

1. **Connection Refused**: Check MCP server is running and accessible
2. **Authentication Errors**: Verify IAM roles and permissions
3. **Rate Limiting**: DuckDuckGo may block requests - implement backoff
4. **Memory Issues**: Increase container memory allocation if needed

### Debug Mode

Enable debug logging by setting:
```python
logging.basicConfig(level=logging.DEBUG)
```

## Development

### Adding New Tools to MCP Server

```python
@mcp.tool()
async def new_tool(param: str, ctx: Context) -> str:
    """Description of the new tool"""
    # Implementation
    return result
```

### Extending the Strands Agent

```python
# Add custom system prompts
agent = Agent(
    system_prompt="Your custom prompt here..."
)

# Add custom tools
agent.tool_registry.add_tool(custom_tool)
```

## Security Considerations

1. **Network Security**: AgentCore provides secure networking between components
2. **Authentication**: OAuth/Bearer tokens required for production
3. **Rate Limiting**: Built-in protection against abuse
4. **Input Validation**: Validate all user inputs
5. **Logging**: Avoid logging sensitive information

## Cost Optimization

1. **Right-sizing**: Choose appropriate container sizes
2. **Auto-scaling**: Configure based on usage patterns
3. **Monitoring**: Track usage and optimize accordingly
4. **Caching**: Implement caching for frequently accessed data

## Next Steps

1. **Production Deployment**: Deploy to production environment
2. **Integration**: Integrate with existing LMA components
3. **Monitoring**: Set up comprehensive monitoring and alerting
4. **Testing**: Implement comprehensive test suite
5. **Documentation**: Create user documentation and API references

## Support

For issues and questions:
1. Check CloudWatch logs for error details
2. Review the troubleshooting section
3. Consult AWS Bedrock AgentCore documentation
4. Open an issue in the project repository
