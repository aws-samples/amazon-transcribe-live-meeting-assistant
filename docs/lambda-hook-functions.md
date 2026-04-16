---
title: "Lambda Hook Functions"
---

# Lambda Hook Functions

## Table of Contents

- [Overview](#overview)
- [Transcript Processing Hook](#transcript-processing-hook)
  - [Registration](#registration)
  - [Processing Mode](#processing-mode)
  - [Input Event Schema](#input-event-schema)
  - [Return Value](#return-value)
  - [OriginalTranscript Field](#originaltranscript-field)
  - [Example](#example)
  - [Important Considerations](#important-considerations)
  - [Cumulative Transcript](#cumulative-transcript)
- [FetchTranscript Utility Lambda](#fetchtranscript-utility-lambda)
  - [Parameters](#parameters)
  - [Example Payload](#example-payload)
- [End-of-Call Summary Hook](#end-of-call-summary-hook)
- [Related Documentation](#related-documentation)

## Overview

LMA provides extensibility through user-provided Lambda functions at key points in the meeting processing pipeline. These hooks allow you to customize transcript processing, integrate with external systems, and implement custom summarization logic without modifying the core LMA infrastructure.

## Transcript Processing Hook

### Registration

To enable custom transcript processing, set the **Lambda Hook Function ARN for Custom Transcript Segment Processing** CloudFormation parameter to the ARN of your Lambda function. This registers your function to be invoked for each transcript segment as it is produced.

### Processing Mode

The **Lambda Hook Function Mode Non-Partial only** parameter controls which transcript segments are sent to your hook:

- **true** (default, recommended): Only final (non-partial) transcript segments are processed. This is the recommended setting as it avoids redundant invocations for interim results.
- **false**: All transcript segments are processed, including partial (interim) results. Use this only if you need to act on partial transcriptions in real time.

### Input Event Schema

Your Lambda function receives an event with the following structure:

```json
{
  "Transcript": "My personal identifier is ABCDEF.",
  "Channel": "CALLER",
  "TransactionId": "634b0a5d-...",
  "CallId": "888660e1-...",
  "SegmentId": "a71ca594-...",
  "StartTime": "27.42",
  "EndTime": "30.955",
  "IsPartial": false,
  "EventType": "ADD_TRANSCRIPT_SEGMENT",
  "CreatedAt": "2022-10-18T21:51:23.172Z",
  "ExpiresAfter": 1671313884
}
```

### Return Value

Your function must return the same structure with fields optionally modified. The modified **Transcript** field value is what gets displayed in the UI and stored in DynamoDB.

### OriginalTranscript Field

If your function includes an **OriginalTranscript** field in the returned event, this value (not the modified Transcript) is used as input to the meeting assistant. This is useful when you want to redact PII from the displayed transcript but still allow the meeting assistant to process the original, unredacted text.

### Example

A minimal Python example that converts the displayed transcript to uppercase while preserving the original for the meeting assistant:

```python
import json

def lambda_handler(event, context):
    print(json.dumps(event))
    event["OriginalTranscript"] = event["Transcript"]
    event["Transcript"] = event["Transcript"].upper()
    return event
```

### Important Considerations

The Lambda function is called **synchronously**. If it fails or times out, the transcript segment is dropped entirely. Keep your hook functions lightweight and fast to avoid impacting the real-time transcription experience.

### Cumulative Transcript

If your hook function needs access to prior transcript context (for example, to make decisions based on the full conversation so far), use the FetchTranscript utility Lambda described below.

## FetchTranscript Utility Lambda

The FetchTranscript utility Lambda allows you to retrieve the transcript for any completed or in-progress call.

The ARN for this function is provided in the stack output **FetchTranscriptLambdaArn**.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| CallId | string | Yes | The call ID to look up |
| ProcessTranscript | bool | No | Condenses sequential speaker utterances, removes filler words (uhh, uhm), and strips HTML |
| TokenCount | int | No | Trims the transcript to this many tokens (words, punctuation, newlines) |

### Example Payload

```json
{
  "CallId": "2359fb61-...",
  "TokenCount": 1024,
  "ProcessTranscript": true
}
```

## End-of-Call Summary Hook

LMA supports custom end-of-call summarization via a Lambda function. For details on configuring the LAMBDA option for transcript summarization, see [Transcript Summarization](transcript-summarization.md).

## Related Documentation

- [Transcript Summarization](transcript-summarization.md)
- [Transcription & Translation](transcription-and-translation.md)
