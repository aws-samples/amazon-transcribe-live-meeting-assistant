---
title: "Transcript Summarization"
---

# Transcript Summarization

## Table of Contents

- [Overview](#overview)
- [BEDROCK Option (Default)](#bedrock-option-default)
  - [Prompt Template Storage](#prompt-template-storage)
  - [Attribute Naming Convention](#attribute-naming-convention)
  - [Customizing Prompts](#customizing-prompts)
  - [Admin UI](#admin-ui)
- [LAMBDA Option](#lambda-option)
  - [Lambda Function Requirements](#lambda-function-requirements)
  - [Multi-Section Summaries](#multi-section-summaries)
  - [Minimal Python Example](#minimal-python-example)
- [FetchTranscript Utility Lambda](#fetchtranscript-utility-lambda)
- [Related Documentation](#related-documentation)

## Overview

LMA summarizes meeting transcripts automatically when a meeting ends, and on-demand via UI buttons during or after meetings. Two summarization options are available: **BEDROCK** (default) and **LAMBDA**.

## BEDROCK Option (Default)

The BEDROCK option uses the selected Amazon Bedrock foundation model (default: Claude Haiku 4.5) to generate meeting summaries from prompt templates.

### Prompt Template Storage

Prompt templates are stored in DynamoDB as two separate items:

1. **Default prompt templates** -- Ship with LMA and may change with new releases. View the current defaults via the stack output **LLMDefaultPromptSummaryTemplate**.
2. **Custom prompt templates** -- User-defined prompts that persist across stack updates and are never overwritten by LMA upgrades. View and edit via the stack output **LLMCustomPromptSummaryTemplate** or through the admin UI.

Custom prompts always take precedence over default prompts when both exist for the same attribute name.

### Attribute Naming Convention

Summary attributes use the `N#Label` format, where:

- **N** is a sequence number that controls the display order in the UI.
- **Label** is the human-readable name shown as the section heading.

Examples:
- `1#Summary`
- `2#Action Items`
- `3#Key Decisions`

### Customizing Prompts

Each attribute's value contains the prompt template text that instructs the model how to generate that section of the summary.

- Use the `{transcript}` placeholder in your prompt template where the meeting transcript should be inserted.
- Use `<br>` for newlines within the prompt template.

**To add a new summary section:** Create a new attribute in the Custom prompts DynamoDB item with a unique `N#Label` name (for example, `4#Follow-Up Questions`).

**To override a default section:** Create an attribute in the Custom prompts item with the same name as the default attribute you want to override (for example, `1#Summary` with your custom prompt text).

**To remove a default section:** Create an attribute in the Custom prompts item with the same name as the default attribute and set its value to an empty string or `NONE`.

### Admin UI

Administrators can view and edit prompt templates directly in the LMA web interface at:

```
/#/configuration/transcript-summary
```

This provides a convenient way to manage prompts without directly editing DynamoDB items.

## LAMBDA Option

The LAMBDA option lets you provide your own Lambda function to generate meeting summaries with complete control over the summarization logic.

### Lambda Function Requirements

Configure the LAMBDA option by setting the **EndOfCallLambdaHookFunctionArn** CloudFormation parameter to the ARN of your Lambda function.

Your Lambda function:
- Receives the `CallId` in the event payload.
- Must return a JSON object with a `summary` key: `{"summary": "..."}`.
- The summary value supports Markdown formatting.

### Multi-Section Summaries

To return a summary with multiple sections, return a JSON-encoded string as the `summary` value. The string should contain key-value pairs where keys are section labels and values are the section content. LMA will parse this and display each section separately in the UI.

### Minimal Python Example

```python
import json

def lambda_handler(event, context):
    print(json.dumps(event))
    call_id = event.get("CallId")
    # Implement your summarization logic here
    summary = "Placeholder for actual summary"
    return {"summary": summary}
```

## FetchTranscript Utility Lambda

LMA provides a utility Lambda function for retrieving processed meeting transcripts. This is especially useful when building custom summarization Lambda functions.

The function ARN is available in the stack output **FetchTranscriptLambdaArn**.

**Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `CallId` | String | Yes | The unique identifier of the meeting/call. |
| `ProcessTranscript` | Boolean | No | When `true`, condenses sequential utterances from the same speaker, removes filler words, and strips HTML tags. |
| `TokenCount` | Integer | No | Trims the returned transcript to approximately N tokens. Useful for staying within model context limits. |

**Example invocation payload:**

```json
{
  "CallId": "2359fb61-...",
  "TokenCount": 1024,
  "ProcessTranscript": true
}
```

## Related Documentation

- [Meeting Assistant](meeting-assistant.md)
- [Lambda Hook Functions](lambda-hook-functions.md)
