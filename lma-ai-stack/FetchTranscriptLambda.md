# FetchTranscript Utility Lambda

LMA provides a utility Lambda function to retrieve the call transcript for a given call id. This Lambda is available for you to invoke from your own custom (LAMBDA) based summarization functions, or from custom Lambda functions used as TranscriptLambdaHooks or custom Lambda based Agent Assist functions to retrieve cumulative transcripts for any completed or in-progress call.

The ARN for the `FetchTranscript` lambda is provided in the LMA stack outputs as `FetchTranscriptLambdaArn`.

The `FetchTranscript` Lambda function accepts 3 parameters:

**CallId** (required, string) - This is the Call ID of the call to look up the transcripts for.

**ProcessTranscript** (optional, bool) - If true, the transcript will condense sequential speaker utterances into a single utterance, remove filler words (uhh, uhm), and remove any HTML that was added to the transcript. 

**TokenCount** (optional) - If this number is provided or greater than zero, the FetchTranscript function will trim the summary to this number of tokens. Tokens are words, punctuation, and new lines.

Example Lambda event payload:

```
{
      "CallId": "2359fb61-f612-4fe9-bce2-839061c328f9",
      "TokenCount": 1024,
      "ProcessTranscript": True
  }
```

