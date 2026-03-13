# DeepWiki MCP Server Setup Guide

## Overview

The DeepWiki MCP server provides programmatic access to DeepWiki's public repository documentation and AI-powered search capabilities. This guide shows you how to integrate DeepWiki with LMA's voice assistant and Strands agent.

## What is DeepWiki?

DeepWiki indexes public GitHub repositories and provides AI-powered documentation search and Q&A capabilities. When integrated with LMA, your voice assistant can answer questions about any public repository's documentation in real-time during meetings.

## What is MCP?

The Model Context Protocol (MCP) is an open standard that enables AI applications to securely connect to MCP-compatible data sources and tools. Think of MCP like a USB-C port for AI applications - a standardized way to connect AI apps to different services.

## DeepWiki MCP Server Details

- **Base URL**: `https://mcp.deepwiki.com/`
- **Authentication**: None required (public repositories only)
- **Cost**: Free
- **Protocol**: Streamable HTTP (recommended)

## Available Tools

The DeepWiki MCP server offers three main tools:

### 1. read_wiki_structure
Get a list of documentation topics for a GitHub repository.

**Use Case**: "Hey Alex, what documentation topics are available for the aws-samples/amazon-transcribe-live-meeting-assistant repository?"

### 2. read_wiki_contents
View documentation about a GitHub repository.

**Use Case**: "Hey Alex, show me the getting started guide for the aws-samples/amazon-transcribe-live-meeting-assistant repository."

### 3. ask_question
Ask any question about a GitHub repository and get an AI-powered, context-grounded response.

**Use Case**: "Hey Alex, how do I deploy a Lambda function using AWS CDK?"

## Wire Protocols

DeepWiki supports two protocols:

### Streamable HTTP (Recommended)
- **URL**: `https://mcp.deepwiki.com/mcp`
- **Compatibility**: Cloudflare, OpenAI, Claude, LMA
- **Status**: Current and recommended

### SSE (Server-Sent Events) - Deprecated
- **URL**: `https://mcp.deepwiki.com/sse`
- **Status**: Being deprecated, use `/mcp` instead

## Setup in LMA

### Step 1: Access MCP Server Management

1. Log into your LMA web interface
2. Navigate to **MCP Servers** in the left sidebar
3. Click **Install MCP Server**

### Step 2: Configure DeepWiki Server

Fill in the installation form:

| Field | Value |
|-------|-------|
| **Server ID** | `custom/deepwiki` |
| **Name** | `DeepWiki` |
| **Package Name** | `deepwiki` |
| **Package Type** | `streamable-http` |
| **Server URL** | `https://mcp.deepwiki.com/mcp` |
| **Transport** | `http` |
| **Requires Auth** | No (uncheck) |

### Step 3: Install

1. Click **Install**
2. Wait for installation to complete (usually < 30 seconds)
3. Verify status shows **ACTIVE**

### Step 4: Test Integration

#### Test with Chat Interface

1. Open a meeting in LMA
2. Open the chat panel
3. Type: "What are the main features of the aws-samples/amazon-transcribe-live-meeting-assistant repository?"
4. Click **Ask Assistant**
5. The Strands agent will use DeepWiki to search the repository

#### Test with Voice Assistant

1. Start a virtual participant with voice assistant enabled
2. Say: "Hey Alex, what documentation is available for the kubernetes/kubernetes repository?"
3. Alex will use DeepWiki to search and respond with voice

## Example Use Cases

### During Development Meetings

**Scenario**: Team discussing AWS CDK implementation

**Query**: "Hey Alex, how do I create a DynamoDB table with AWS CDK?"

**Result**: Alex searches aws-samples/amazon-transcribe-live-meeting-assistant documentation and provides code examples

### During Architecture Reviews

**Scenario**: Reviewing Kubernetes deployment strategy

**Query**: "Hey Alex, what are the best practices for Kubernetes deployments according to the official docs?"

**Result**: Alex searches kubernetes/kubernetes documentation and provides recommendations

### During Troubleshooting

**Scenario**: Debugging an issue with a library

**Query**: "Hey Alex, search the react/react repository for information about useEffect cleanup functions"

**Result**: Alex searches React documentation and provides relevant information

## Supported Repositories

DeepWiki indexes **public GitHub repositories only**. Popular repositories include:

- **AWS**: aws-samples/amazon-transcribe-live-meeting-assistant, aws/aws-sdk-js, aws/aws-cli
- **Kubernetes**: kubernetes/kubernetes
- **React**: facebook/react
- **Python**: python/cpython
- **And thousands more...**

To check if a repository is indexed, visit [https://deepwiki.com](https://deepwiki.com) and search for it.

## Troubleshooting

### Issue: "Repository not found"

**Cause**: Repository not indexed in DeepWiki

**Solution**:
- Verify repository name format: `owner/repo` (e.g., `aws-samples/amazon-transcribe-live-meeting-assistant`)
- Check if repository is public
- Visit [https://deepwiki.com](https://deepwiki.com) to request indexing

### Issue: "No documentation available"

**Cause**: Repository has no documentation or README

**Solution**:
- Try a different repository
- Use `ask_question` tool instead of `read_wiki_contents`
- Check repository has documentation files

### Issue: MCP server shows "FAILED" status

**Cause**: Installation error

**Solution**:
- Verify Server URL is correct: `https://mcp.deepwiki.com/mcp`
- Check Package Type is `streamable-http`
- Ensure "Requires Auth" is unchecked
- Try uninstalling and reinstalling

### Issue: Voice assistant not using DeepWiki

**Cause**: Tool not available or context unclear

**Solution**:
- Be specific: "search the aws-samples/amazon-transcribe-live-meeting-assistant repository"
- Mention "documentation" or "Deep Wiki" explicitly
- Check MCP server status is ACTIVE
- Review Strands Lambda logs for tool execution

## Architecture

### How It Works

```
User Query (Voice or Chat)
    ↓
LMA Voice Assistant / Strands Agent
    ↓
Decides to use DeepWiki tool
    ↓
HTTP Request to https://mcp.deepwiki.com/mcp
    ↓
DeepWiki searches indexed repository
    ↓
Returns AI-powered answer
    ↓
LMA presents result (voice or text)
```

### Components

- **LMA Voice Assistant**: AWS Nova Sonic 2 with tool use
- **Strands Agent**: Orchestrates tool calls
- **DeepWiki MCP Server**: Remote HTTP service
- **MCP Protocol**: Standardized communication
- **No Authentication**: Public repositories only

## Security & Privacy

### Data Flow

- **Queries**: Sent to DeepWiki's public service
- **Responses**: Public repository documentation only
- **No Authentication**: No API keys or tokens required
- **Public Data**: Only public GitHub repositories accessible

### Considerations

- DeepWiki is a third-party service (not AWS)
- Queries are sent to `mcp.deepwiki.com`
- Only use for public repository documentation
- Do not query private/proprietary information

## Cost

- **DeepWiki Service**: Free
- **LMA Usage**: Standard LMA costs (Bedrock, Lambda, etc.)
- **No Additional Charges**: DeepWiki integration is free

## Comparison with Other MCP Servers

| Feature | DeepWiki | Salesforce | Slack |
|---------|----------|------------|-------|
| **Authentication** | None | OAuth2 | OAuth2 |
| **Data Source** | Public GitHub repos | Your Salesforce org | Your Slack workspace |
| **Cost** | Free | Included | Included |
| **Setup Complexity** | Simple | Moderate | Moderate |
| **Use Case** | Documentation search | CRM data | Team communication |

## Support

### DeepWiki Support
- Website: [https://deepwiki.com](https://deepwiki.com)
- Documentation: [https://deepwiki.com/docs](https://deepwiki.com/docs)

### LMA MCP Integration Support
- LMA Documentation: See main README
- GitHub Issues: [LMA GitHub Repository](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant)

## Summary

✅ **What You Get:**
- AI-powered documentation search during meetings
- Access to thousands of public GitHub repositories
- Natural voice queries: "Hey Alex, search the aws-samples/amazon-transcribe-live-meeting-assistant docs" (requires system prompt for nova to pass correct repository name or url to strands for deepwiki tool use)
- No authentication or API keys required
- Free service

✅ **Setup Steps:**
1. Install DeepWiki MCP server in LMA (2 minutes)
2. Test with chat or voice
3. Start querying documentation in meetings

✅ **Nova Sonic Config Change (Optional):**
For questions about LMA to work with Deepwiki, nova needs to format the query to the strans_agent tool correctly. Refer to docs/AWS_NOVA_SONIC_VOICE_ASSISTANT_SETUP.md for adding a custom Nova Sonic config. For the `promptMode` use inject and for the `systemPrompt` this something like this:

```

IMPORTANT: When users ask about LMA (Live Meeting Assistant) or this app, call strands_agent with this exact format:
"[user's question]. Search for information about Live Meeting Assistant using the deepwiki tool with GitHub URL: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant"

For example, if user asks "what vendors does LMA support?", call strands_agent with:
"what vendors does LMA support? Search for information about Live Meeting Assistant using the deepwiki tool with GitHub URL: https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant"

```

✅ **Example Queries:**
- "What are the main features of the kubernetes/kubernetes repository?"
- "How do I create a Lambda function with AWS CDK?"
- "Search the react/react docs for useEffect best practices"

That's it! Your LMA voice assistant now has access to comprehensive documentation for thousands of open-source projects through DeepWiki.
