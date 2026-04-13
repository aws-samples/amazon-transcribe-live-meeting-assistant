# Meeting Assistant

## Table of Contents

- [Overview](#overview)
- [Service Modes](#service-modes)
- [Activation Methods](#activation-methods)
  - [Wake Phrase](#wake-phrase)
  - [Chat Interface](#chat-interface)
  - [Chat Shortcut Buttons](#chat-shortcut-buttons)
- [Built-in Tools](#built-in-tools)
- [Knowledge Base Configuration](#knowledge-base-configuration)
  - [S3 Data Source](#s3-data-source)
  - [Web Crawler](#web-crawler)
- [Model Selection](#model-selection)
- [Bedrock Guardrails](#bedrock-guardrails)
- [Custom LLM Prompt Templates](#custom-llm-prompt-templates)
- [Chat Shortcut Button Configuration](#chat-shortcut-button-configuration)
- [Tavily Web Search](#tavily-web-search)
- [Related Documentation](#related-documentation)

## Overview

The LMA Meeting Assistant is an agentic AI assistant powered by the Strands Agents SDK with Amazon Bedrock. It provides real-time, context-aware assistance during meetings by leveraging a set of built-in tools that can access the current meeting transcript, search knowledge bases, query past meetings, and perform web research.

## Service Modes

LMA supports three service modes for the Meeting Assistant, configured during stack deployment:

| Mode | Description |
|---|---|
| **STRANDS_BEDROCK** | Agent with built-in tools and no knowledge base. The assistant can access meeting transcripts, search past meetings, and perform web searches, but does not have access to a document knowledge base. |
| **STRANDS_BEDROCK_WITH_KB (Create)** | Agent with built-in tools plus an automatically created Bedrock Knowledge Base. LMA provisions a new KB and populates it from S3 documents and/or web-crawled pages that you specify. |
| **STRANDS_BEDROCK_WITH_KB (Use Existing)** | Agent with built-in tools plus your existing Bedrock Knowledge Base. Provide the KB ID of a knowledge base you have already created and manage independently. |

## Activation Methods

### Wake Phrase

Say **"OK Assistant"** during a meeting to activate the assistant by voice. The assistant detects the wake phrase in the transcript, processes your spoken question, and inserts its response inline in the transcript.

The wake phrase pattern is configurable via the **Meeting Assist Wake Phrase Regular Expression** CloudFormation parameter. The default regex matches "OK Assistant" and common variations.

### Chat Interface

Type questions directly in the chat panel on the meeting details page. The assistant streams its response in real time with token-by-token streaming, providing a conversational experience. The chat interface provides full access to all built-in tools.

### Chat Shortcut Buttons

Pre-configured suggestion buttons appear in the chat panel, allowing users to trigger common queries with a single click. These buttons are configurable by administrators via the LMA UI settings. See [Chat Shortcut Button Configuration](#chat-shortcut-button-configuration) for details.

## Built-in Tools

The Meeting Assistant has access to the following tools:

| Tool | Description |
|---|---|
| `current_meeting_transcript` | Retrieves and summarizes the current meeting transcript. Use this to answer questions about what has been discussed in the ongoing meeting. |
| `document_search` | Searches the Bedrock Knowledge Base for relevant documents. Available only when a Knowledge Base is configured (STRANDS_BEDROCK_WITH_KB modes). |
| `meeting_history` | Performs semantic search across past meeting transcripts. User-Based Access Control (UBAC) is enforced, so users only see results from their own meetings unless they are admins. |
| `recent_meetings_list` | Returns a chronological list of recent meetings, providing an overview of meeting activity. |
| `web_search` | Performs web searches via Tavily to find current information from the internet. Requires a Tavily API key to be configured. |
| `control_vnc_preview` | Shows or hides the Virtual Participant (VP) browser screen on the meeting page, allowing users to see what the VP browser is displaying. |
| `control_vp_browser` | Controls the VP browser to navigate web pages and perform web research during meetings, enabling the assistant to look up information in real time. |

## Knowledge Base Configuration

When using **STRANDS_BEDROCK_WITH_KB (Create)** mode, you can configure data sources for the automatically created Bedrock Knowledge Base.

### S3 Data Source

Provide an S3 bucket name and optional key prefixes to index documents stored in S3. The Knowledge Base will ingest and index these documents, making them searchable via the `document_search` tool.

### Web Crawler

Provide one or more seed URLs for the web crawler to index. Configure the crawl scope to control how broadly the crawler follows links:

| Scope | Description |
|---|---|
| `DEFAULT` | Crawls pages linked from the seed URLs with default depth and breadth limits. |
| `HOST_ONLY` | Restricts crawling to pages on the same host as the seed URLs. |
| `SUBDOMAINS` | Crawls pages on the same domain and its subdomains. |

## Model Selection

Choose the Bedrock foundation model that powers the Meeting Assistant. Set the model via the **MeetingAssistServiceBedrockModelID** CloudFormation parameter. Supported models include:

- Amazon Nova Micro
- Amazon Nova Lite
- Amazon Nova Pro
- Anthropic Claude Sonnet 4.5
- Anthropic Claude Opus 4
- Anthropic Claude Haiku 4.5

The model selection affects response quality, latency, and cost. Lighter models (Nova Micro, Claude Haiku 4.5) are faster and less expensive, while more capable models (Claude Opus 4, Claude Sonnet 4.5) provide higher-quality responses for complex queries.

## Bedrock Guardrails

Optionally configure Amazon Bedrock Guardrails to apply content filtering and safety controls to the assistant's responses.

Set the following CloudFormation parameters:

- **BedrockGuardrailId** -- The ID of your Bedrock Guardrail.
- **BedrockGuardrailVersion** -- The version of the guardrail to use.

When configured, all assistant requests are evaluated against the guardrail policies before responses are returned.

## Custom LLM Prompt Templates

The Meeting Assistant uses prompt templates stored in DynamoDB to guide its behavior. LMA ships with default prompts that work well for general use cases.

- **Default prompts** are provided with LMA and may be updated with new releases.
- **Custom prompts** override the defaults and persist across stack updates. They are never overwritten by LMA upgrades.

Administrators can view and edit custom prompt templates via the LMA admin UI at:

```
/#/configuration/transcript-summary
```

Custom prompts allow you to tailor the assistant's behavior, tone, and output format to your organization's needs.

## Chat Shortcut Button Configuration

Chat shortcut buttons provide one-click access to common queries in the chat panel. Administrators can configure these buttons via the LMA UI:

- **Add** new shortcut buttons with custom labels and prompts.
- **Edit** existing button labels and associated prompts.
- **Delete** buttons that are no longer needed.

Shortcut buttons are shared across all users and appear in the chat interface for every meeting.

## Tavily Web Search

The `web_search` tool enables the Meeting Assistant to search the web for current information during meetings. This tool is powered by Tavily.

To enable web search, set the **TavilyApiKey** CloudFormation parameter to your Tavily API key. Without a valid API key, the `web_search` tool will not be available to the assistant.

## Related Documentation

- [MCP Servers](mcp-servers.md)
- [Transcript Summarization](transcript-summarization.md)
- [Voice Assistant](voice-assistant.md)
