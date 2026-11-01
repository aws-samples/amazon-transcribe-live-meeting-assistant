---
title: "Meeting Assistant"
---

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
  - [The knowledge-base model](#the-knowledge-base-model)
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

Each token is published through the `addChatToken` AppSync mutation and delivered to the browser over an `onAddChatToken` subscription, which is keyed on both the meeting and the message. The subscription is accepted from the people who can open the meeting: its owner, the users it is shared with, and Admins. All the tokens of one answer are published over a single connection to AppSync, opened when the answer starts and closed when it finishes. Callers that do not subscribe -- the Virtual Participant's voice assistant invokes the assistant directly and speaks the complete answer it gets back -- are answered without a token stream being published, since the mutation broadcasts to subscribers and stores nothing. The answer itself is identical either way. This covers the token stream only: the assistant's thinking steps are published on every path, including that one, where there is likewise no subscriber to receive them.

### Chat Shortcut Buttons

Pre-configured suggestion buttons appear in the chat panel, allowing users to trigger common queries with a single click. These buttons are configurable by administrators via the LMA UI settings. See [Chat Shortcut Button Configuration](#chat-shortcut-button-configuration) for details.

## Built-in Tools

The Meeting Assistant has access to the following tools:

| Tool | Description |
|---|---|
| `current_meeting_transcript` | Retrieves and summarizes the current meeting transcript. Use this to answer questions about what has been discussed in the ongoing meeting. It can prefix each line with its time from the start of the meeting (`[MM:SS]`), which the assistant uses when an answer should say when something was discussed. |
| `get_meeting_agenda` | Reads the current meeting's agenda, numbered as the meeting page lists it, with where it came from. The assistant uses it for questions about the agenda and for progress checks — each item reported as covered, in progress or remaining, with the transcript times where it was discussed — and says so plainly when the meeting has no agenda rather than inventing one. Offered in every tool scope, including questions typed in the meeting chat. See [Meeting Agenda](meeting-agenda.md#asking-the-assistant-about-the-agenda). |
| `get_meeting_frames` | Reads the key frames of the meeting's shared screen — the last few, those in a time range, or the one on screen at a moment — each with what was said while it was on screen, and passes the pictures themselves to the model when it reads images (every model the stack offers except Amazon Nova Micro, which gets the times and transcript only). It answers "summarize that slide", "translate the last slide into French" or "show me the last two slides"; in the meeting page's chat the answer can show the frames, while in the meeting chat and by voice it describes them. Offered in every tool scope, including questions typed in the meeting chat. See [Meeting Video and Key Frames](meeting-video.md#asking-the-assistant-about-the-shared-screen). |
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

- Amazon Nova Micro, Nova Lite, Nova Pro, Nova Premier and Nova 2 Lite
- Anthropic Claude Haiku 4.5, Sonnet 4.5, Sonnet 4.6, Opus 4, Opus 4.1 and Opus 4.6
- Anthropic Claude Haiku 5.5, Sonnet 5.5 and Opus 5.5

Each Claude and Nova entry is offered in both its `global.` and its `us.` inference-profile form; pick the one that suits where your deployment runs.

The model selection affects response quality, latency, and cost. Lighter models (Nova Micro, Claude Haiku) are faster and less expensive, while more capable models (Claude Opus, Claude Sonnet) provide higher-quality responses for complex queries.

The Claude 5 generation reasons before it answers. That improves answers on questions that need several steps, but the reasoning is itself billed as output, so the same question costs several times as much output as it does on Claude 4.5 at the same size. Claude 5 models also use a different tokenizer that counts a given transcript as roughly a quarter more input tokens. Expect a higher per-meeting bill if you move to one, and treat the quality gain as the thing you are buying.

### The knowledge-base model

The assistant's two knowledge-base tools -- `document_search` over your documents and `meeting_history` over past meeting transcripts -- do not use the agent's model. A Bedrock Knowledge Base generates its own answer from what it retrieves, driven by the orchestration and generation prompt templates the knowledge base supplies, and those templates do not cover every model the agent's own API will accept. The model they use is set by the separate **MeetingAssistKnowledgeBaseModelID** parameter:

| Model IDs offered |
|---|
| `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `us.anthropic.claude-sonnet-4-6`, `us.anthropic.claude-opus-4-6-v1` |
| `global.anthropic.claude-haiku-4-5-20251001-v1:0`, `global.anthropic.claude-sonnet-4-5-20250929-v1:0`, `global.anthropic.claude-sonnet-4-6`, `global.anthropic.claude-opus-4-6-v1` |

Whether a knowledge base can generate with a given model is a property of that individual model and cannot be read off its family or version: Claude Opus 4.1 is refused, for instance, while Claude Opus 4.6 is accepted. The list above is therefore not a rule but a record -- each of those IDs has been verified by a live knowledge-base call -- and it is deliberately shorter than the agent's list. A model being added to **MeetingAssistServiceBedrockModelID** does not make it available here.

The parameter defaults to `global.anthropic.claude-haiku-4-5-20251001-v1:0`. Releases before v0.3.11 had no parameter for this job and these tools generated with whatever **MeetingAssistServiceBedrockModelID** was set to, so a deployment that selected a non-default agent model needs this one set when it updates -- see the [v0.3.11 migration notes](stack-updates-and-upgrades.md#v0311) for which value to choose. Because the two parameters are independent, you can run the agent on any model it supports -- including the Claude 5 generation, which is not offered for retrieval -- and leave retrieval on a model that works; the knowledge-base tools keep working either way. Enable access to both models in the Amazon Bedrock console for your region: the deployment converses with each of them before the nested stacks are created, and reports one your account cannot reach as a parameter-validation failure. That check runs only while **ModelValidation** is `true`, its default; with it set to `false` a model you cannot reach is discovered when a tool first uses it.

If the knowledge-base model is ever set to a model a knowledge base cannot generate with -- only reachable by deploying `lma-meetingassist-setup-stack/template.yaml` on its own, since the top-level parameter does not offer one -- the affected tool is not offered to the agent at all, and the function's log records the model ID and the parameter to change. Past meetings can still be listed chronologically in that state, because the `recent_meetings_list` tool reads DynamoDB and does not involve a knowledge base.

## Bedrock Guardrails

Optionally configure Amazon Bedrock Guardrails to apply content filtering and safety controls to the assistant's responses.

Create the guardrail in the Amazon Bedrock console first, in the same AWS account and Region as the LMA stack, then set the following CloudFormation parameters:

- **BedrockGuardrailId** -- The ID of your Bedrock Guardrail (for example `abcd1234efgh`). Empty by default, which means no guardrail is applied.
- **BedrockGuardrailVersion** -- The guardrail version to use. Defaults to `DRAFT`, which always resolves to the working draft of the guardrail. Set a numeric version (for example `1`) to pin a published version.

Both parameters must have a value for a guardrail to be applied. If **BedrockGuardrailId** is left empty -- the default -- the assistant behaves exactly as it does without the feature, and no guardrail permissions are granted to the assistant's IAM role. If you set an ID but blank the version, no guardrail is applied; the function logs a warning naming the ID at startup. When both are set, the deployment scopes the role's `bedrock:ApplyGuardrail` permission to that single guardrail ARN.

When configured, the guardrail is attached to all five paths the assistant uses to reach Amazon Bedrock:

- the Strands agent's Bedrock model, which covers normal streaming and non-streaming chat turns;
- the direct `Converse` fallback used if the agent cannot be constructed, so the fallback is not a way to reach the model without the guardrail;
- all three Knowledge Base `RetrieveAndGenerate` calls: the document-search tool, the meeting-history tool, and the per-meeting transcript knowledge base query.

On the two `Converse` paths the guardrail trace is enabled, so intervention is reported by Bedrock in the response trace. The `RetrieveAndGenerate` API has no trace option, so on the three Knowledge Base paths an intervention surfaces as the guardrail's configured blocked-input or blocked-output message in the returned text.

If the assistant starts returning blocked or masked content after you enable a guardrail, review the guardrail's filter and topic policies in the Bedrock console -- LMA passes the request through unchanged and does not override guardrail decisions.

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

### Where the key is stored

The stack keeps the key in AWS Secrets Manager, in a secret named
`<stack-name>/integrations/tavily-api-key` encrypted with the deployment's
customer-managed KMS key. The meeting assist Lambda is given the secret's ARN
in `TAVILY_API_KEY_SECRET_ARN` and reads the key itself on the first web search
each execution environment performs, caching it from then on. Whether the
`web_search` tool is offered to the assistant at all is decided from the ARN
alone, so a deployment that has not configured a key makes no Secrets Manager
call.

If you already keep the key in Secrets Manager, set **TavilyApiKeySecretArn**
to that secret's ARN instead of **TavilyApiKey** — supply one form or the
other, not both.

### Rotating the key

Generate a new key at [tavily.com](https://tavily.com), put it in the
**TavilyApiKey** parameter, and apply the stack update, which stores a new
version of the secret. You can also store the new version directly in Secrets
Manager instead — if you do, set the CloudFormation parameter to match, or the
next stack update writes the old value back.

The new key then takes effect **as the function's execution environments
recycle**, because each one caches the key on its first web search. How long
that takes is up to Lambda and depends on your traffic; under steady use an
environment can stay warm for hours. A stack update does not hasten it: the
function is configured with the secret's ARN, and that does not change when the
value does, so there is nothing for Lambda to replace. To put the new key in
use immediately, change the function's configuration directly, which is what
makes Lambda start new execution environments:

```bash
aws lambda update-function-configuration \
  --function-name STRANDS-<stack-name>-MeetingAssist \
  --description "Tavily key rotated $(date -u +%FT%TZ)"
```

This differs from the four Virtual Participant credentials, which are read at
the start of each meeting and so are picked up by the next meeting to start.
See [Integration Credentials](infrastructure-and-security.md#rotating-a-credential).

## Related Documentation

- [MCP Servers](mcp-servers.md)
- [Transcript Summarization](transcript-summarization.md)
- [Voice Assistant](voice-assistant.md)
