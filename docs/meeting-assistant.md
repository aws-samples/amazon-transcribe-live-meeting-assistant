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
  - [In-meeting requests](#in-meeting-requests)
- [Knowledge Base Configuration](#knowledge-base-configuration)
  - [S3 Data Source](#s3-data-source)
  - [Web Crawler](#web-crawler)
- [Model Selection](#model-selection)
  - [Reasoning effort](#reasoning-effort)
  - [The knowledge-base model](#the-knowledge-base-model)
  - [Prompt caching](#prompt-caching)
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

Say **"OK Assistant"** during a meeting to activate the assistant by voice. The assistant detects the wake phrase in the transcript, processes your spoken question, and inserts its response inline in the transcript. Anyone in the meeting can say it, so it is answered as an [in-meeting request](#in-meeting-requests): from the current meeting only, unless the In-meeting requests setting is Unrestricted.

The wake phrase pattern is **Wake phrase (regular expression)** in the Meeting Assistant section of the [Configuration page](configuration-guide.md), where an Admin can change it while the stack runs, with no stack update. The default regex matches "OK Assistant" and common variations. A pattern saved on the page that is not a valid regular expression is logged by the call event processor, which uses the shipped pattern until it is corrected.

### Chat Interface

Type questions directly in the chat panel on the meeting details page. The assistant streams its response in real time with token-by-token streaming, providing a conversational experience. The chat interface provides full access to all built-in tools.

Each token is published through the `addChatToken` AppSync mutation and delivered to the browser over an `onAddChatToken` subscription, which is keyed on both the meeting and the message. The subscription is accepted from the people who can open the meeting: its owner, the users it is shared with, and Admins. All the tokens of one answer are published over a single connection to AppSync, opened when the answer starts and closed when it finishes. Callers that do not subscribe -- the Virtual Participant's voice assistant invokes the assistant directly and speaks the complete answer it gets back -- are answered without a token stream being published, since the mutation broadcasts to subscribers and stores nothing. The answer itself is identical either way. This covers the token stream only: the assistant's thinking steps are published on every path, including that one, where there is likewise no subscriber to receive them.

### Chat Shortcut Buttons

Pre-configured suggestion buttons appear in the chat panel, allowing users to trigger common queries with a single click. These buttons are configurable by administrators via the LMA UI settings. See [Chat Shortcut Button Configuration](#chat-shortcut-button-configuration) for details.

## Built-in Tools

The Meeting Assistant has access to the following tools. A request made in the meeting — the meeting chat, the voice assistant or the wake phrase — is offered only `current_meeting_transcript`, `get_meeting_agenda` and `get_meeting_frames` unless **Meeting Assistant › In-meeting requests** offers more; see [In-meeting requests](#in-meeting-requests).

| Tool | Description |
|---|---|
| `current_meeting_transcript` | Retrieves and summarizes the current meeting transcript. Use this to answer questions about what has been discussed in the ongoing meeting. The messages typed in the meeting chat are among the lines, each as `[chat] <sender>: <message>` in time order with the speech. It can prefix each line with its time from the start of the meeting (`[MM:SS]`), which the assistant uses when an answer should say when something was discussed. |
| `get_meeting_agenda` | Reads the current meeting's agenda, numbered as the meeting page lists it, with where it came from. The assistant uses it for questions about the agenda and for progress checks — each item reported as covered, in progress or remaining, with the transcript times where it was discussed — and says so plainly when the meeting has no agenda rather than inventing one. Offered in every tool scope, including questions typed in the meeting chat. See [Meeting Agenda](meeting-agenda.md#asking-the-assistant-about-the-agenda). |
| `get_meeting_frames` | Reads the key frames of the meeting's shared screen — the last few, those in a time range, or the one on screen at a moment — each with what was said while it was on screen, and passes the pictures themselves to the model when it reads images (every model the stack offers except Amazon Nova Micro, which gets the times and transcript only). It answers "summarize that slide", "translate the last slide into French" or "show me the last two slides"; in the meeting page's chat the answer can show the frames, while in the meeting chat and by voice it describes them. Offered in every tool scope, including questions typed in the meeting chat. See [Meeting Video and Key Frames](meeting-video.md#asking-the-assistant-about-the-shared-screen). |
| `document_search` | Searches the Bedrock Knowledge Base for relevant documents. Available only when a Knowledge Base is configured (STRANDS_BEDROCK_WITH_KB modes). |
| `meeting_history` | Performs semantic search across past meeting transcripts. User-Based Access Control (UBAC) is enforced, so users only see results from their own meetings unless they are admins. |
| `recent_meetings_list` | Returns a chronological list of recent meetings, providing an overview of meeting activity. |
| `web_search` | Performs web searches via Tavily to find current information from the internet. Requires a Tavily API key to be configured. |
| `control_vnc_preview` | Shows or hides the Virtual Participant (VP) browser screen on the meeting page, allowing users to see what the VP browser is displaying. |
| `control_vp_browser` | Controls the VP browser to navigate web pages and perform web research during meetings, enabling the assistant to look up information in real time. |

### In-meeting requests

The meeting assistant is asked for things in two ways. The **meeting page's chat** is used by the signed-in LMA user, and is always offered every tool above, acting for that user. An **in-meeting request** is made in the meeting by whoever is in it, and its answer reaches the meeting:

| In-meeting request | Where the answer goes |
|---|---|
| A question typed in the meeting chat (the Virtual Participant's `LMA ask`; see [Asking the Meeting Assistant from Chat](virtual-participant.md#asking-the-meeting-assistant-from-chat)) | Posted in the meeting chat |
| A request spoken to the Virtual Participant's [voice assistant](voice-assistant.md#what-the-voice-assistant-can-look-up) that needs a lookup | Spoken in the meeting |
| A request spoken with the [wake phrase](#wake-phrase) | Added to the meeting's transcript |

None of these can tell who in the meeting asked: a chat sender is known only by the display name the meeting shows, and a spoken request only by the voice that said it. So all three are answered as **Meeting Assistant › In-meeting requests** on the [Configuration page](configuration-guide.md) (`meeting_assist.inMeetingRequestTools`) allows, and a change applies within about five minutes to requests made after that:

- **Current meeting only** (`current_meeting_only`, the default). The answer comes from the current meeting alone, through three tools: `current_meeting_transcript` (what was said, and the messages typed in the meeting chat, each as a line `[chat] <sender>: <message>`), `get_meeting_agenda`, and `get_meeting_frames` (what was shown on the shared screen: each frame's title, its text, when it was on screen and what was said meanwhile). No other tool is offered, and no user is acted for, so past meetings are not searched.
- **Unrestricted** (`unrestricted`). Every tool above, acting for the meeting's LMA user (the Virtual Participant's user for the chat and the voice assistant; the user the transcript was streamed for, for the wake phrase). This is how in-meeting requests were answered before the setting existed. Choose it only where everyone who can join a meeting may read or hear what those tools return.

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

Choose the Bedrock foundation model that powers the Meeting Assistant. An Admin sets it as **Meeting assistant** in the Amazon Bedrock Models section of the [Configuration page](configuration-guide.md), and a change applies within about five minutes, with no stack update. A new deployment uses Claude Haiku 5.5 (`global.anthropic.claude-haiku-5-5`) at a low reasoning effort until the setting is changed; see [Choosing Amazon Bedrock models](configuration-guide.md#choosing-amazon-bedrock-models) for the measurements behind that default. Supported models include:

- Amazon Nova Micro, Nova Lite, Nova Pro and Nova 2 Lite
- Anthropic Claude Haiku 4.5, Sonnet 4.5, Sonnet 4.6, Opus 4.1 and Opus 4.6
- Anthropic Claude Haiku 5.5, Sonnet 5.5 and Opus 5.5

Each Claude and Nova entry is offered in both its `global.` and its `us.` inference-profile form; pick the one that suits where your deployment runs.

A model that reaches the end of its life on Amazon Bedrock is taken off these lists in the next release. A profile saved with such a model keeps the stored value, and the Configuration page shows it as not one of the choices, but every feature reading the setting uses its default model instead and logs a warning naming the setting, until an Admin picks another model. Claude Opus 4 (`us.anthropic.claude-opus-4-20250514-v1:0`) and Nova Premier (`us.amazon.nova-premier-v1:0`) were removed this way in v0.4.0.

The model selection affects response quality, latency, and cost. Lighter models (Nova Micro, Claude Haiku) are faster and less expensive, while more capable models (Claude Opus, Claude Sonnet) provide higher-quality responses for complex queries.

The Claude 5 generation reasons before it answers. That improves answers on questions that need several steps, but the reasoning is itself billed as output, so the same question costs several times as much output as it does on Claude 4.5 at the same size. Claude 5 models also use a different tokenizer that counts a given transcript as roughly a quarter more input tokens. Expect a higher per-meeting bill if you move to one from a Claude 4.5 model of the same size, and treat the quality gain as the thing you are buying. Claude Haiku 5.5 is the exception that sets the default: its per-token prices are a tenth of Claude Haiku 4.5's, which more than offsets the extra tokens.

### Reasoning effort

**Meeting Assistant › Reasoning effort** on the Configuration page sets how much a Claude 5 generation model (Claude Haiku, Sonnet and Opus 5.5) reasons before it answers. It is sent to Amazon Bedrock as `output_config.effort` in the request's `additionalModelRequestFields`, on every model call of an answer, and a change applies within about five minutes to answers started after that.

| Value | Effect |
|---|---|
| `low` (the default) | Answers soonest and costs least. Measured on Claude Haiku 5.5, it still gave better answers than Claude Haiku 4.5 on most meeting-assistant requests. |
| `medium` | The model's own default. |
| `high` | Reasons longest, for questions that need several steps, and costs the most, since reasoning is billed as output. |

The setting is ignored for every other meeting assistant model. Claude Haiku 4.5, Sonnet 4.5, Opus 4.1 and the Amazon Nova models reject the field, and Claude Sonnet 4.6 and Opus 4.6, which accept it, are deliberately left at their own default so that a deployment already running one of them answers as it did before. Reasoning is not turned off altogether at any value: with extended thinking disabled, Claude Haiku 5.5 was measured choosing the assistant's tools less reliably. The troubleshooting agent, which runs on the same model, is not affected by this setting and runs at the model's default effort. The setting composes with prompt caching and with the **Model inference geography** setting: the effort is decided from the model id after the geography mapping, so `us.anthropic.claude-haiku-5-5` is treated the same as `global.anthropic.claude-haiku-5-5`.

### The knowledge-base model

The assistant's two knowledge-base tools -- `document_search` over your documents and `meeting_history` over past meeting transcripts -- do not use the agent's model. A Bedrock Knowledge Base generates its own answer from what it retrieves, driven by the orchestration and generation prompt templates the knowledge base supplies, and those templates do not cover every model the agent's own API will accept. The model they use is the separate **Amazon Bedrock Models › Meeting assistant knowledge bases** setting on the Configuration page:

| Model IDs offered |
|---|
| `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `us.anthropic.claude-sonnet-4-6`, `us.anthropic.claude-opus-4-6-v1` |
| `global.anthropic.claude-haiku-4-5-20251001-v1:0`, `global.anthropic.claude-sonnet-4-5-20250929-v1:0`, `global.anthropic.claude-sonnet-4-6`, `global.anthropic.claude-opus-4-6-v1` |

Whether a knowledge base can generate with a given model is a property of that individual model and cannot be read off its family or version: Claude Opus 4.1 is refused, for instance, while Claude Opus 4.6 is accepted. The list above is therefore not a rule but a record -- each of those IDs has been verified by a live knowledge-base call -- and it is deliberately shorter than the agent's list. A model being added to **Amazon Bedrock Models › Meeting assistant** does not make it available here.

The setting defaults to `global.anthropic.claude-haiku-4-5-20251001-v1:0`. Releases before v0.4.0 had no separate model for this job and these tools generated with the meeting assistant's model, so a deployment that selected a non-default agent model needed this one set when it updated -- see the [v0.4.0 migration notes](stack-updates-and-upgrades.md#v040) for which value to choose. Because the two settings are independent, you can run the agent on any model it supports -- including the Claude 5 generation, which is not offered for retrieval -- and leave retrieval on a model that works; the knowledge-base tools keep working either way. Enable access to both models in the Amazon Bedrock console for your region before you choose them. A model chosen on the Configuration page is not checked before it is used: a model the account cannot invoke makes the assistant or the tool fail when it next runs, with the Bedrock error in its log.

If the knowledge-base model is ever set to a model a knowledge base cannot generate with, the affected tool is not offered to the agent at all, and the function's log records the model ID and where to change it: **Amazon Bedrock Models › Meeting assistant knowledge bases** on the Configuration page. Past meetings can still be listed chronologically in that state, because the `recent_meetings_list` tool reads DynamoDB and does not involve a knowledge base.

### Prompt caching

The assistant marks the stable part of each request for [Amazon Bedrock prompt caching](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html), so a model call that repeats it reads it from the cache at a lower price instead of being billed for it again as ordinary input. The answers themselves do not change. It is on by default and is switched by **Meeting Assistant › Use Amazon Bedrock prompt caching** on the Configuration page, which applies within about five minutes to answers started after that.

What is cached depends on the model, because a request carrying a cache point that the model does not accept is rejected:

| Meeting assistant model | Cache points |
|---|---|
| Claude 4 and later (Haiku 4.5, Sonnet 4.5, Sonnet 4.6, Opus 4.1, Opus 4.6, and the 5.5 models) | After the tool definitions, after the system prompt, and at the end of the latest user message, moved forward on each model call |
| Nova Micro, Nova Lite, Nova Pro and Nova 2 Lite | After the system prompt only, which also covers the tool definitions ahead of it. Nova rejects a cache point among the tool definitions or beside a tool result |
| Any other model | None; the request is sent as before |

The decision is made in `bedrock_compat.prompt_cache_sections`, so a model added to the selectable list later gets no cache points until it is known to accept them.

Most of the saving comes inside a single answer. An answer that uses a tool is several model calls, and each one resends everything the call before it sent plus the tool's result, which is often a slice of the transcript. With the cache point at the end of the latest message, the second and later calls read the earlier part from the cache. Answers within about five minutes of each other (the cache's lifetime, renewed by each read) can also share the cached instructions and tool definitions.

A cache point takes effect only once everything before it reaches the model's minimum: 4,096 tokens for Claude Haiku 4.5, 512 for Claude Haiku 5.5, 512 to 1,024 for the other later Claude models, and about 1,000 for Nova. Measured in us-east-1, the assistant's instructions and built-in tool definitions come to about 3,800 tokens on Claude Haiku 4.5, so on that model they are not cached by themselves; they are cached as part of the longer prefix once a tool has returned a result. On Claude Haiku 5.5, the default, the same instructions and tool definitions are well above the minimum (its tokenizer also counts them as more tokens), so they are cached from the first call of an answer. A request below the minimum is answered normally, with nothing cached and no cache-write charge.

On Claude a cache write is billed at 1.25 times the input price and a cache read at a tenth of it or less; on Nova a write costs nothing and a read is 75% off. A written prefix that is never read again therefore costs a little more on Claude than it would have without caching. That is the case the switch is for. The [meeting cost estimate](meeting-cost-estimate.md) records and prices cache reads and writes separately from ordinary input.

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

Administrators can view and edit custom prompt templates in the **Transcript Summary** section of **Configuration ▸ Configuration** in the LMA admin UI:

```
/#/configuration/settings?section=summary_prompt_templates
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
