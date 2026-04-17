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
| BedrockKnowledgeBaseWebCrawlerUrls | URLs for the Knowledge Base web crawler data source | (none) | Comma-separated URLs |
| BedrockKnowledgeBaseWebCrawlerScope | Scope of web crawling | DEFAULT | DEFAULT, HOST_ONLY, SUBDOMAINS |
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

## End-of-Call Summary

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| EndOfCallTranscriptSummary | Method used to generate end-of-call summaries | BEDROCK | BEDROCK, LAMBDA |
| BedrockModelId | Bedrock model used for summarization | Claude Haiku 4.5 | Supported Bedrock model IDs |
| EndOfCallLambdaHookFunctionArn | ARN of a custom Lambda function for summarization (when using LAMBDA mode) | (none) | Valid Lambda ARN |

## Virtual Participant

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| VPLaunchType | Compute launch type for Virtual Participant tasks | EC2 | EC2, FARGATE |
| VPInstanceType | EC2 instance type for Virtual Participant | t3.medium | t3.medium, t3.large, t3.xlarge, c5.large, c5.xlarge, c5.2xlarge, m5.large, m5.xlarge |
| VPMinInstances | Minimum number of EC2 instances in the VP Auto Scaling group | 1 | Positive integer |
| VPMaxInstances | Maximum number of EC2 instances in the VP Auto Scaling group | 5 | Positive integer |

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
