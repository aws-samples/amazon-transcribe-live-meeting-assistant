# LMA Meeting Assist

## Introduction

Live Meeting Assist (LMA) provides users with real-time multi-participant audio transcription, optionally translated into their preferred language, and an integrated AI meeting assistant powered by AWS Strands SDK. The meeting assistant uses trusted enterprise data and meeting context to fact-check, look up relevant information, and propose responses. It creates succinct on-demand recaps, insights, and action item lists during and after meetings, securely maintaining an inventory of meeting records.

## Architecture

The meeting assistant uses the **Strands Agents SDK** with Amazon Bedrock as the LLM provider. It supports:

- **STRANDS_BEDROCK** - Agentic meeting assistant with built-in tools (transcript search, web search, document search, meeting history, VP browser control)
- **STRANDS_BEDROCK_WITH_KB (Create)** - Same as above, plus automatically creates a Bedrock Knowledge Base for document search
- **STRANDS_BEDROCK_WITH_KB (Use Existing)** - Same as above, using an existing Bedrock Knowledge Base you provide

## How It Works

The meeting assistant is invoked in one of two ways:

1. **Wake Phrase**: A meeting participant says the wake phrase (defined by the **Meeting Assist Wake Phrase Regular Expression** parameter, defaults to *OK Assistant!*). The response is inserted inline with the meeting transcript.

2. **Chat Interface**: The LMA user interacts with the **Meeting Assist Bot** on the LMA UI by typing a question or using the built-in suggestion buttons. Responses are shown in the chat panel with real-time token streaming.

## Built-in Tools

The Strands agent has access to the following tools:

- **current_meeting_transcript** - Retrieves transcript from the current meeting for summarization, action items, topic analysis
- **document_search** - Searches the Bedrock Knowledge Base for company documents, policies, and reference materials
- **meeting_history** - Searches past meeting transcripts with semantic search and user-based access control
- **recent_meetings_list** - Gets a chronological list of recent meetings
- **web_search** - Searches the web for current information (requires Tavily API key)
- **control_vnc_preview** - Shows/hides the Virtual Participant's browser screen on the meeting page
- **control_vp_browser** - Controls the Virtual Participant's browser for web research during meetings

## MCP Server Integration

The Strands agent supports dynamic loading of MCP (Model Context Protocol) servers, enabling integration with external tools and services. MCP servers can be configured through the LMA UI's MCP Servers settings panel.

## Chat Button Configuration

Admin users can customize the suggestion buttons that appear in the chat interface. Buttons are configured through DynamoDB and can be edited via the LMA UI settings or directly in the DynamoDB console.

## Bedrock Guardrails

Optionally configure Bedrock Guardrails to control and filter the meeting assistant's responses. Provide a Guardrail ID and version during stack deployment.

## Configuration

Key configuration parameters:

- **MeetingAssistService** - Choose the assistant mode (STRANDS_BEDROCK, with KB Create, or with KB Use Existing)
- **MeetingAssistServiceBedrockModelID** - Select the Bedrock model (Nova or Claude 4+)
- **TavilyApiKey** - Optional API key to enable web search
- **BedrockGuardrailId/Version** - Optional guardrail configuration
- **BedrockKnowledgeBaseId** - Required when using "Use Existing" KB mode
