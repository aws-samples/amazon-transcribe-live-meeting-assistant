# Using a Lambda function to optionally provide custom logic for transcript processing

## Overview

LMA optional custom logic via a user-provided Lambda function, to support the following features:
1. Modify Transcriptions in real time using custom logic, for example, implement your redaction or profanity filtering rules.
2. Choose to process non-partial (final) transcript segments only, or both partial (not final) and non-partial (final) transcript segments.
3. Log or save transcript segments to an external data store.

To use this feature:
1. Implement a Lambda function with the desired business logic.
2. Use CloudFormation to register the Lambda with your LCA stack - **Lambda Hook Function ARN for Custom Transcript Segment Processing (existing)**.
3. Use CloudFormation to choose whether to call your function for NonPartial segments only, or all segments - **Lambda Hook Function Mode Non-Partial only**.


## Transcript Lambda function requirements

Your Lambda function will be invoked by the LMA AISTACK CallEventProcessor function for each transcription message read from the incoming KDS stream. The message passed as the input event to your Lambda looks like this:

```
{
    "Transcript": "My personal identifier is ABCDEF.",
    "Channel": "CALLER",
    "TransactionId": "634b0a5d-2f8e-482f-bde3-d3275355c500",
    "CallId": "888660e1-70a6-410e-a765-1d20517db270",
    "SegmentId": "a71ca594-0a75-4506-9713-7dafaf3f9326",
    "StartTime": "27.42",
    "EndTime": "30.955",
    "IsPartial": false,
    "EventType": "ADD_TRANSCRIPT_SEGMENT",
    "CreatedAt": "2022-10-18T21:51:23.172Z",
    "ExpiresAfter": 1671313884
}
```

Your Lambda implements the required business logic, and returns the same event structure with fields optionally modified. The example below shows
the "Transcript" field modified to redact the personal identifier using custom business logic.
```
{
    "Transcript": "My personal identifier is [PIN].",
    "Channel": "CALLER",
    "TransactionId": "634b0a5d-2f8e-482f-bde3-d3275355c500",
    "CallId": "888660e1-70a6-410e-a765-1d20517db270",
    "SegmentId": "a71ca594-0a75-4506-9713-7dafaf3f9326",
    "StartTime": "27.42",
    "EndTime": "30.955",
    "IsPartial": false,
    "EventType": "ADD_TRANSCRIPT_SEGMENT",
    "CreatedAt": "2022-10-18T21:51:23.172Z",
    "ExpiresAfter": 1671313884
}
``` 

The modified value of "Transcript" is subsequently displayed in the LMA UI, and stored in the LMA DynamoDB event sourcing table.   
  
The modified version is also used by default as input to the Agent Assist Lex bot or Lambda function, if Agent Assist is enabled. To use the original, unmodified transcript for Agent Assist, your function must add an additional field, `OriginalTranscript`, to the returned message. When the returned messsage contains the `OriginalTranscript` field, this value is used as input to Agent Assist. Example:

```
{
    "OriginalTranscript": "My personal identifier is ABCDEF.",
    "Transcript": "My personal identifier is [PIN].",
    "Channel": "CALLER",
    "TransactionId": "634b0a5d-2f8e-482f-bde3-d3275355c500",
    "CallId": "888660e1-70a6-410e-a765-1d20517db270",
    "SegmentId": "a71ca594-0a75-4506-9713-7dafaf3f9326",
    "StartTime": "27.42",
    "EndTime": "30.955",
    "IsPartial": false,
    "EventType": "ADD_TRANSCRIPT_SEGMENT",
    "CreatedAt": "2022-10-18T21:51:23.172Z",
    "ExpiresAfter": 1671313884
}
``` 

Here is a minimal example of a valid custom Transcript Lambda hook function, written in Python. 
```
import json

def lambda_handler(event, context):
    print(json.dumps(event))
    event["OriginalTranscript"] = event["Transcript"]
    event["Transcript"] = event["Transcript"].upper()
    print(json.dumps(event))
    return event
``` 

This example function trivially converts the transcript to uppercase while preserving the original for use with agent assist. Your function will be much smarter, and will implement your custom rules or models.

The CallEventProcessor function calls your Lambda syncronously, waiting for a valid response. If your Lambda fails or times out before completing, the CallEventProcessor Lambda throws an exception and drops the transcript. Be sure to test your function properly using test events, and log messages so you can check that it functions as expected.  
Your function is invoked for every non-partial transcript segment (by default), or for every partial and non-partial segment (by setting **Lambda Hook Function Mode Non-Partial only** to *false*). Make your function as lightweight and fast as possible to minimize latency and cost. 

## Accessing cumulative transcript for the call
You can use the [Fetch Transcript](./FetchTranscriptLambda.md) function to retrieve the cumulative transcript of the call, up to but not including the current transcription segment. This may be useful when your processing function requires prior context to process the current segment.

## Register the Lambda function with LMA

Use the LMA CloudFormation template parameter **Lambda Hook Function ARN for Custom Transcript Segment Processing (existing)** to set the ARN value for your custom Lambda hook function when ceating a new LMA stack, or when updating an existing one. You find the ARN for your Lambda in the AWS Lambda console - it has this format:
```
arn:aws:lambda:us-east-1:<accountId>:function:<functionName>
```

Use the LMA CloudFormation template parameter **Lambda Hook Function Mode Non-Partial only** to choose whether to call your function for NonPartial segments only (`true`, recommended) , or all segments (`false`).

