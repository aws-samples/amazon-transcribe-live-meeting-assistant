---
title: "Transcription & Translation"
---

# Transcription & Translation

## Table of Contents

- [How Transcription Works](#how-transcription-works)
- [Speaker Attribution](#speaker-attribution)
- [Language Configuration](#language-configuration)
  - [Single Language](#single-language)
  - [Automatic Single Language Identification](#automatic-single-language-identification)
  - [Automatic Multi-Language Identification](#automatic-multi-language-identification)
  - [Setting the Language](#setting-the-language)
- [Live Translation](#live-translation)
- [Content Redaction (PII)](#content-redaction-pii)
- [Custom Vocabulary](#custom-vocabulary)
- [Custom Language Models](#custom-language-models)
- [Audio Recording](#audio-recording)
- [Partial vs Non-Partial Transcripts](#partial-vs-non-partial-transcripts)
- [Configuration Parameters](#configuration-parameters)
- [Related Documentation](#related-documentation)

## How Transcription Works

LMA captures audio from the browser and processes it through a multi-stage pipeline to produce real-time transcriptions:

1. **Browser audio capture** -- The LMA browser extension or client captures audio as two-channel stereo (microphone + incoming audio).
2. **WebSocket server (Fargate)** -- Audio is streamed over a WebSocket connection to a server running on AWS Fargate.
3. **Amazon Transcribe** -- The Fargate server forwards the audio stream to Amazon Transcribe for real-time speech-to-text conversion.
4. **Amazon Kinesis** -- Transcription results are published to a Kinesis Data Stream.
5. **Call Event Processor Lambda** -- A Lambda function consumes events from the Kinesis stream, processes them, and writes results to DynamoDB and publishes updates via AppSync.
6. **UI** -- The LMA web application receives real-time updates via AppSync subscriptions and renders the transcript for the user.

## Speaker Attribution

LMA uses two-channel stereo audio to separate speakers:

- **Channel 1 (microphone)** -- Captures the LMA user's voice from their microphone input.
- **Channel 2 (incoming audio)** -- Captures the audio from other meeting participants (the incoming audio source).

This two-channel approach allows Amazon Transcribe to attribute speech to the correct speaker without relying solely on speaker diarization, providing more accurate and reliable speaker labels in the transcript.

## Language Configuration

LMA supports several language configuration modes for transcription, controlled by the **Language for Transcription** CloudFormation parameter.

### Single Language

When you know the language that will be spoken in your meetings, select a specific language code. LMA supports 16+ languages, including:

- `en-US` (English, US)
- `en-GB` (English, UK)
- `en-AU` (English, Australia)
- `fr-FR` (French)
- `de-DE` (German)
- `it-IT` (Italian)
- `pt-BR` (Portuguese, Brazil)
- `ja-JP` (Japanese)
- `ko-KR` (Korean)
- `zh-CN` (Chinese, Simplified)
- And more

Setting a specific language provides the best transcription accuracy for that language.

### Automatic Single Language Identification

Select this option when meetings may be in different languages, but each meeting uses only one language. Amazon Transcribe will automatically detect the spoken language at the start of the session and use that language for the entire transcription.

### Automatic Multi-Language Identification

Select this option when participants may switch between languages during a single meeting. Amazon Transcribe will continuously identify and adapt to language changes throughout the session.

### Setting the Language

Configure the language mode using the **Language for Transcription** CloudFormation parameter when deploying or updating the LMA stack.

## Live Translation

LMA supports live translation of transcripts into 75+ languages via Amazon Translate. Users select their preferred target language directly in the LMA web UI. Translation is performed client-side, translating the transcribed text as it appears in the interface. This allows each user to independently choose their own target language without affecting other participants.

## Content Redaction (PII)

LMA can automatically redact personally identifiable information (PII) from transcripts using Amazon Transcribe's built-in content redaction.

**Enabling redaction:** Set the **Enable Content Redaction for Transcripts** CloudFormation parameter to `true`.

**Supported languages:** Content redaction is supported for the following languages only:

- `en-US` (English, US)
- `en-AU` (English, Australia)
- `en-GB` (English, UK)
- `es-US` (Spanish, US)

**Redactable entity types:**

| Entity Type | Description |
|---|---|
| `BANK_ACCOUNT_NUMBER` | Bank account numbers |
| `BANK_ROUTING` | Bank routing numbers |
| `CREDIT_DEBIT_NUMBER` | Credit or debit card numbers |
| `CREDIT_DEBIT_CVV` | Credit or debit card CVV codes |
| `CREDIT_DEBIT_EXPIRY` | Credit or debit card expiration dates |
| `PIN` | Personal identification numbers |
| `EMAIL` | Email addresses |
| `ADDRESS` | Physical addresses |
| `NAME` | Personal names |
| `PHONE` | Phone numbers |
| `SSN` | Social Security numbers |

## Custom Vocabulary

Custom vocabularies improve transcription accuracy for domain-specific terms, proprietary names, or technical jargon that Amazon Transcribe may not recognize by default.

To use a custom vocabulary:

1. Create the custom vocabulary in the Amazon Transcribe console first. Follow the [Amazon Transcribe documentation](https://docs.aws.amazon.com/transcribe/latest/dg/custom-vocabulary.html) for instructions.
2. Set the **Transcription Custom Vocabulary Name** CloudFormation parameter to the name of your custom vocabulary when deploying or updating the LMA stack.

## Custom Language Models

For more advanced customization, you can train a custom language model in Amazon Transcribe to improve recognition of domain-specific speech patterns.

Set the **Transcription Custom Language Model Name** CloudFormation parameter to the name of your trained custom language model.

## Audio Recording

LMA can optionally record meeting audio as stereo WAV files stored in Amazon S3.

- **Format:** Stereo WAV files preserving the two-channel audio (microphone and incoming).
- **Storage:** Recordings are stored in the S3 bucket created by the LMA stack.
- **Retention:** Configurable via the **Record Expiration In Days** CloudFormation parameter. The default retention period is 90 days, after which recordings are automatically deleted by an S3 lifecycle policy.
- **Playback:** Recordings are accessible via the audio player in the meeting details view of the LMA UI.

## Partial vs Non-Partial Transcripts

Amazon Transcribe produces two types of transcript results:

- **Partial transcripts** provide low-latency, evolving text that updates as more audio is processed. Words may change as Transcribe refines its predictions. These give users an immediate sense of what is being said.
- **Non-partial transcripts** are the final, stable results for a segment of speech. Once a non-partial result is emitted, it will not change.

Both types flow through the LMA pipeline. You can configure Lambda hook functions to process only non-partial transcripts if your use case requires stable text (for example, for summarization or integration with external systems).

## Configuration Parameters

| Parameter Name | Description | Default Value |
|---|---|---|
| Language for Transcription | Language or language identification mode for Amazon Transcribe | `en-US` |
| Enable Content Redaction for Transcripts | Enable PII redaction in transcripts (supported languages only) | `false` |
| Transcription Custom Vocabulary Name | Name of a custom vocabulary created in Amazon Transcribe | (empty) |
| Transcription Custom Language Model Name | Name of a custom language model created in Amazon Transcribe | (empty) |
| Record Expiration In Days | Number of days to retain audio recordings in S3 | `90` |

## Related Documentation

- [Lambda Hook Functions](lambda-hook-functions.md)
- [CloudFormation Parameters Reference](cloudformation-parameters.md)
