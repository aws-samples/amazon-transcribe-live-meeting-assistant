---
title: "CloudFormation Parameters Reference"
---

# CloudFormation Parameters Reference

## Table of Contents

- [Overview](#overview)
- [General](#general)
- [Meeting Assistant](#meeting-assistant)
- [Knowledge Base](#knowledge-base)
- [Transcription](#transcription)
- [End-of-Call Summary](#end-of-call-summary)
- [Virtual Participant](#virtual-participant)
- [Voice Assistant](#voice-assistant)
- [Simli Avatar](#simli-avatar)
- [Audio Recording](#audio-recording)
- [Lambda Hooks](#lambda-hooks)
- [Security and Networking](#security-and-networking)
- [Related Documentation](#related-documentation)

## Overview

This is a complete reference of all LMA CloudFormation stack parameters. These values are set when creating or updating your stack. For the most current and complete list, see the CloudFormation template parameters when creating or updating your stack.

## General

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| AdminEmail | Admin user email address. A temporary password is sent to this address. | (required) | Valid email address |
| AuthorizedAccountEmailDomain | Comma-separated email domains allowed for self-registration | (none) | Comma-separated domain names |
| MeetingRecordExpirationInDays | Number of days to retain meeting data before automatic deletion | 90 | Positive integer |
| CloudWatchLogsExpirationInDays | Number of days to retain CloudWatch Logs | (varies) | Standard CloudWatch retention values |
| EnableDataRetentionOnDelete | Retain DynamoDB tables, S3 buckets, and KMS keys when the stack is deleted | (false) | true, false |

## Meeting Assistant

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| MeetingAssistService | Meeting assistant service type | STRANDS_BEDROCK | STRANDS_BEDROCK, STRANDS_BEDROCK_WITH_KB (Create), STRANDS_BEDROCK_WITH_KB (Use Existing) |
| MeetingAssistServiceBedrockModelID | LLM model used by the meeting assistant | Claude Haiku 4.5 | Supported Bedrock model IDs |
| MeetingAssistWakePhrase | Regular expression pattern that activates the meeting assistant | OK Assistant | Valid regex pattern |
| TavilyApiKey | API key for the Tavily web search tool | (none) | Valid API key string |
| BedrockGuardrailId | Optional Bedrock guardrail identifier | (none) | Valid guardrail ID |
| BedrockGuardrailVersion | Version of the Bedrock guardrail to use | (none) | Valid guardrail version |

## Knowledge Base

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| BedrockKnowledgeBaseId | Existing Bedrock Knowledge Base ID (for Use Existing mode) | (none) | Valid KB ID |
| BedrockKnowledgeBaseS3BucketName | S3 bucket containing documents for the Knowledge Base (for Create mode) | (none) | Valid S3 bucket name |
| BedrockKnowledgeBaseS3Prefix | S3 key prefixes for Knowledge Base documents | (none) | Comma-separated prefixes |
| TranscriptKnowledgeBaseService | Whether to create a Knowledge Base from meeting transcripts | DISABLED | BEDROCK_KNOWLEDGE_BASE (Create), DISABLED |

## Transcription

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| TranscribeLanguageCode | Language code for Amazon Transcribe | en-US | en-US, identify-language, identify-multiple-languages, and other supported language codes |
| TranscriptionCustomVocabularyName | Name of a custom vocabulary in Amazon Transcribe | (none) | Valid custom vocabulary name |
| TranscriptionCustomLanguageModelName | Name of a custom language model in Amazon Transcribe | (none) | Valid custom language model name |
| IsContentRedactionEnabled | Enable automatic PII redaction in transcriptions | false | true, false |
| TranscribeContentRedactionType | Type of content redaction | PII | PII |
| ContentRedactionLanguages | Languages that support content redaction | en-US | en-US, en-AU, en-GB, es-US |

## On-demand ASR and Diarization (MicroVM)

Alternative streaming engine to Amazon Transcribe, giving per-voice speaker labels.
Off by default. A meeting transcribed by this engine does not go through Amazon
Transcribe, so the redaction, custom vocabulary, custom language model and language
identification parameters above do not apply to it. Requires a region where AWS
Lambda MicroVMs is available. See [On-demand ASR & Speaker Diarization](microvm-asr.md).

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| TranscriptionEngine | Deploys the on-demand ASR + diarization stack. Meetings still use Amazon Transcribe unless a client opts in | AmazonTranscribe | AmazonTranscribe, MicrovmAsr |
| AsrModelId | ASR model baked into the MicroVM image (English only by default, NVIDIA Open Model License) | nemotron-streaming-en-0.6b-560ms-int8 | nemotron-streaming-en-0.6b-560ms-int8, Custom |
| AsrSpeakerModelId | Speaker-embedding model used for diarization (CC-BY-4.0); "none" builds a transcription-only image | titanet-small | titanet-small, none |
| AsrBaselineMemoryMiB | Memory per ASR MicroVM; CPU is 1 vCPU per 2 GiB, so 8192 gives 4 vCPU | 8192 | 4096, 8192, 16384 |
| AsrMaxMeetingSeconds | Hard lifetime ceiling per MicroVM and the cost backstop if a transcriber task dies without releasing it | 14400 | 600–28800 |
| AsrMaxSpeakers | Cap on distinct speakers per audio channel (0 discovers as many as appear) | 0 | 0–30 |
| AsrSpeakerThreshold | Cosine similarity above which two utterances are the same speaker; higher splits more eagerly | 0.5 | 0.0–1.0 |
| AsrModelUrl | (Custom model only) URL of the model archive | (none) | Any HTTPS URL |
| AsrModelSha256 | (Custom model only) SHA256 of the archive; required, the build fails without a match | (none) | Hex digest |
| AsrModelEncoderFile / AsrModelDecoderFile / AsrModelJoinerFile / AsrModelTokensFile | (Custom model only) filenames inside the archive | (none) | Filenames |
| AsrSherpaOnnxVersion / AsrOnnxruntimeVersion | (Custom model only) runtime versions that can load the model | (none) | Published versions |

## End-of-Call Summary

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| EndOfCallTranscriptSummary | Method used to generate end-of-call summaries | BEDROCK | BEDROCK, LAMBDA |
| BedrockModelId | Bedrock model used for summarization | Claude Haiku 4.5 | Supported Bedrock model IDs |
| EndOfCallLambdaHookFunctionArn | ARN of a custom Lambda function for summarization (when using LAMBDA mode) | (none) | Valid Lambda ARN |

## Virtual Participant

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| VPLaunchType | Compute launch type for Virtual Participant tasks. `MICROVM` (default) runs each VP in an AWS Lambda MicroVM (Firecracker) instead of an ECS task — see [MicroVM launch type](virtual-participant.md#microvm-launch-type-default) for requirements and trade-offs. | MICROVM | EC2, FARGATE, MICROVM |
| VPInstanceType | EC2 instance type for Virtual Participant. `t3.medium` (default) runs 1 voice + avatar VP (container capped at 3500 MB); the capacity-provider auto-scaler launches additional hosts when concurrent demand exceeds capacity. Bump to `t3.large` or a `c5.*`/`m5.*` instance for more concurrent VPs per host. | t3.medium | t3.medium, t3.large, t3.xlarge, c5.large, c5.xlarge, c5.2xlarge, m5.large, m5.xlarge |
| VPMinInstances | Minimum warm EC2 instances always running. Set to `0` to fully scale down when idle (cold-start adds ~60-90s to the first VP). | 1 | 0-10 |
| VPMaxInstances | Maximum EC2 instances. Capacity-provider managed scaling launches new hosts up to this cap when concurrent demand exceeds the current cluster's capacity. | 10 | 1-100 |

`VPInstanceType`, `VPMinInstances` and `VPMaxInstances` apply only to `VPLaunchType=EC2`. Under `MICROVM` there are no hosts to size or scale — each meeting gets its own MicroVM, billed for its lifetime.

The VP stack also creates these infrastructure resources used by the auto-scaling, AI DOM resolver, and per-user persistent Chromium profile features:

- **`VPCapacityProvider`** ECS capacity provider — wires the EC2 ASG into ECS managed scaling (`TargetCapacity=100`, step size 1-2, instance warmup 90s, `ManagedTerminationProtection=ENABLED`). RunTask drives `CapacityProviderStrategy` instead of `LaunchType=EC2`, so when the cluster is full ECS automatically launches new hosts up to `VPMaxInstances`. The launching VP shows status `WAITING_FOR_CAPACITY` while the auto-scaler provisions a new host.
- **`DomSelectorCache`** DynamoDB table — caches AI-discovered selectors across all VP tasks (30-day TTL on `lastUsedAt`). KMS-encrypted, PAY_PER_REQUEST.
- **`VPProfilesBucket`** S3 bucket — stores per-user persistent Chromium profiles (cookies, "trusted device" markers) keyed by Cognito sub. KMS-encrypted, public access blocked, versioned.

None of these requires user configuration. The AI fallback resolver model is configured via the task-definition env var `BEDROCK_DOM_RESOLVER_MODEL_ID` (default `us.anthropic.claude-haiku-4-5-20251001-v1:0`); set to empty string in the task definition to disable the fallback. See [Virtual Participant → Auto-Scaling](virtual-participant.md#auto-scaling) and [Zoom Sign-in & Join Reliability](zoom-credentials-and-join-reliability.md) for details.

## Voice Assistant

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| VoiceAssistantProvider | Voice assistant provider | none | none, elevenlabs, amazon_nova_sonic |
| VoiceAssistantActivationMode | How the voice assistant is activated | always_active | always_active, wake_phrase |
| VoiceAssistantWakePhrase | Comma-separated wake phrases for the voice assistant | (none) | e.g., "hey alex,ok alex" |
| VoiceAssistantActivationDuration | Duration (in seconds) the voice assistant stays active after wake phrase | 30 | 5-300 |
| ElevenLabsApiKey | API key for ElevenLabs voice assistant | (none) | Valid API key string |
| ElevenLabsAgentId | ElevenLabs conversational agent ID | (none) | Valid agent ID |

## Simli Avatar

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| SimliApiKey | API key for Simli avatar service | (none) | Valid API key string |
| SimliFaceId | Simli face ID for avatar appearance | (none) | Valid face ID |
| SimliTransportMode | Transport mode for Simli avatar video | livekit | livekit, p2p |

## Audio Recording

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| ShouldRecordCall | Enable audio recording of meetings | true | true, false |
| RecordingDisclaimer | Disclaimer text displayed to users when recording is enabled | (none) | Free-form text |

## Lambda Hooks

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| TranscriptLambdaHookFunctionArn | ARN of a Lambda function for custom transcript segment processing | (none) | Valid Lambda ARN |
| TranscriptLambdaHookFunctionNonPartialOnly | Process only final (non-partial) transcript segments | true | true, false |

## Security and Networking

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| InstallationPermissionsBoundaryArn | Optional IAM permissions boundary ARN applied to all created roles | (none) | Valid IAM policy ARN |
| CloudFrontPriceClass | CloudFront distribution price class | PriceClass_100 | PriceClass_100, PriceClass_200, PriceClass_All |
| CloudFrontGeoRestrictions | Comma-separated ISO 3166-1 country codes for geographic access restrictions | (none) | ISO 3166-1 alpha-2 codes |

> **Note:** This is a representative list of parameters. For the most current and complete list, see the CloudFormation template parameters when creating or updating your stack.

## Related Documentation

- [Prerequisites & Deployment](prerequisites-and-deployment.md)
- [Stack Updates & Upgrades](stack-updates-and-upgrades.md)
