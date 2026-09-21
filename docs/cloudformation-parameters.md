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
- [WebSocket Transcriber Service Scaling](#websocket-transcriber-service-scaling)
- [On-demand ASR and Diarization (MicroVM) — EXPERIMENTAL](#on-demand-asr-and-diarization-microvm--experimental)
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
| EnableDataRetentionOnDelete | Retain DynamoDB tables, S3 buckets, the Cognito user pool, and KMS keys when the stack is deleted | true | true, false |
| MeetingInactivityTimeoutInMinutes | Minutes a meeting may go without a finalized transcript segment before LMA ends it on the meeting's behalf. 0 leaves such meetings open | 240 | 0-10080 |
| MeetingInactivityLookbackInDays | How far back the reaper looks for meetings to close. A meeting that started earlier than this is never closed automatically | 2 | 1-90 |

`MeetingInactivityTimeoutInMinutes` backs the scheduled reaper that ends
meetings whose client disconnected without sending its end-of-meeting event —
otherwise those meetings show as "In Progress" for as long as their record is
retained. The reaper runs every 15 minutes, so a meeting can remain in progress
for the timeout plus one interval. Its liveness signal is the meeting record's
last update, which a transcript segment refreshes, so a meeting that is still
connected but has produced no finalized speech for longer than the timeout is
also ended; keep the value comfortably longer than the longest quiet stretch
your meetings have. Meetings still being processed by the upload pipeline are
left to `upload_meeting_finalizer`.

`MeetingInactivityLookbackInDays` bounds how much history each run examines, and
with it how much the reaper can fix. A meeting that started before the window is
never closed automatically, so if you are updating a deployment that has
accumulated older meetings stuck in progress, raise this once, let a run complete,
and put it back. Each run also stops after reading 500 candidate meetings —
meetings are examined newest-first, so the ones that have just become eligible are
always covered, and at any plausible meeting volume the cap is never reached. See
[Troubleshooting → Meeting Stuck In Progress](troubleshooting.md#meeting-stuck-in-progress).

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
| ShowSpeakerLabel | Default for per-channel speaker partitioning (diarization) on WebSocket streaming sessions -- the Stream Audio tab and the Desktop Capture App. Applies to both channels when used. Clients that send their own per-channel choice take precedence, so leave this false unless you want it on for clients that do not. See [Transcription & Translation](transcription-and-translation.md#speaker-identification-within-a-channel). | false | true, false |

## WebSocket Transcriber Service Scaling

These parameters size the Fargate service that terminates the browser and
desktop-app WebSocket connections (the Stream Audio tab, the Desktop Capture App
and the browser extension). They do not affect the Virtual Participant.

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| TranscriberDesiredTaskCount | Number of transcriber tasks the service runs | 1 | 1-20 |
| TranscriberAutoScalingEnabled | Scale the service on average CPU utilization | false | true, false |
| TranscriberMinTaskCount | Minimum task count when auto scaling is enabled | 1 | 1-20 |
| TranscriberMaxTaskCount | Maximum task count when auto scaling is enabled | 4 | 1-50 |
| TranscriberScalingCpuTargetPercent | Average CPU utilization that auto scaling holds | 35 | 10-90 |

Running more than one task requires `EnableVideoRecording` to be `false`, and the
stack rejects the combination at deploy time rather than failing per meeting. With
video recording enabled, the desktop capture app sends meeting video over a
second WebSocket connection, and only the task already hosting that meeting's
audio session can attach it. Note that `EnableVideoRecording` also governs
Virtual Participant video, which has no such constraint, so scaling the
transcriber means giving up *desktop app* video recording specifically. The load balancer in front of the service does not
pin a client to a task, and cookie-based stickiness is not available because the
CloudFront distribution in front of the load balancer forwards no cookies, so
with several tasks the video connection is refused whenever it lands on a
different task from the audio. Audio is unaffected either way: a meeting's audio
uses one connection for its whole lifetime.

Auto scaling adds capacity but never removes it. Taking a task away would end the
meetings it is hosting once the load balancer's deregistration delay elapses, and
a meeting can run for hours. The consequence is that capacity ratchets up and
stays there: while `TranscriberAutoScalingEnabled` is `true`, Application Auto
Scaling owns the task count and a stack update no longer resets it to
`TranscriberDesiredTaskCount`. To bring it back down, either set
`TranscriberAutoScalingEnabled` to `false` — which hands the count back to
CloudFormation and applies `TranscriberDesiredTaskCount` — or reduce it directly
with `aws ecs update-service --desired-count` or
`aws application-autoscaling register-scalable-target`. Budget for the high water
mark, not the average. The CPU target is kept below the load at which a task
reports itself unhealthy to the load balancer, so that scaling out happens before
tasks start being replaced.

Deployments no longer reduce the service below its full task count while tasks
are being replaced. A rolling deployment now briefly runs up to twice the task
count instead — which is the one window where more than one task exists even with
video recording enabled. That is harmless in practice: the draining task keeps
serving the sessions already attached to it and the new task only receives new
meetings, so no meeting is ever split across the two.

## On-demand ASR and Diarization (MicroVM) — EXPERIMENTAL

> **EXPERIMENTAL — not production ready.** Transcript quality is below Amazon
> Transcribe's and defaults may change between releases. Amazon Transcribe remains the
> recommended engine for production meetings.

Alternative streaming engine to Amazon Transcribe, giving per-voice speaker labels.
Off by default. A meeting transcribed by this engine does not go through Amazon
Transcribe, so the redaction, custom vocabulary, custom language model and language
identification parameters above do not apply to it. Requires a region where AWS
Lambda MicroVMs is available. See [On-demand ASR & Speaker Diarization](microvm-asr.md).

| Parameter | Description | Default | Allowed Values |
|-----------|-------------|---------|----------------|
| EnableMicrovmAsr | Deploys the on-demand speech engine alongside Amazon Transcribe. Nothing changes until an admin moves meetings onto it on the Transcription Engine page | false | true, false |

`EnableMicrovmAsr` is the **only** deploy-time question for this engine. Two more
values are fixed in the `AsrDefaults` mapping in `lma-main.yaml` rather than asked:

| Mapping key | Value | Purpose |
|---|---|---|
| `ModelBundle` | `fastconformer-titanet-small` | Which models the MicroVM image is built from, together with their measured diarization operating point |
| `MaxMeetingSeconds` | `14400` | Hard lifetime ceiling per MicroVM, and the cost backstop if a transcriber task dies without releasing one |

Everything that used to be tunable — the similarity threshold, minimum utterance
length, speaker cap, turn-cut behaviour, maximum open row — is now the bundle's
measured operating point or the engine's built-in default. There is nothing for a
deployment to calibrate: a guessed or borrowed threshold fragments one person into
several or merges several into one, so the number ships with the model it was
measured for.

### The two bundles

Both are permissively licensed and redistributable, and share the same ASR model.
Selecting the other one means editing the mapping and updating the stack, which
rebuilds the MicroVM image (~20 minutes).

| Bundle | Models | Licences | Speaker labels |
|--------|--------|----------|----------------|
| `fastconformer-titanet-small` (default) | NVIDIA FastConformer streaming EN 480 ms + TitaNet-small + pyannote segmentation 3.0 | CC-BY-4.0 + CC-BY-4.0 + MIT | Yes — threshold 0.5, minimum utterance 2500 ms, measured on real meeting audio |
| `fastconformer-transcription-only` | NVIDIA FastConformer streaming EN 480 ms | CC-BY-4.0 | No — labelled by audio channel, no speaker weights in the image |

There are deliberately no parameters for supplying a model URL: every model is a
curated entry in the ASR stack's `catalog.json` with its checksum pinned and, for a
speaker model, its operating point measured with `scripts/calibrate.py`. The three
runtime switches (engine for streaming meetings, engine for Virtual Participants, Virtual
Participant voice separation) live on the Transcription Engine admin page and take effect on the
next meeting with no stack update.

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
| VPAttendeePollMs | How often a Teams Virtual Participant samples the meeting UI to decide whether the meeting is still running, in milliseconds. | 20000 | 1000-120000 |
| VPPollsBeforeEnd | Consecutive polls on which a Teams meeting must show one or fewer attendees before the VP leaves — about 60 seconds at the default cadence. | 3 | 1-90 |
| VPPollsBeforeEndMissing | Consecutive polls on which a Teams meeting must show neither a readable attendee count nor any in-meeting controls before the VP leaves — about 5 minutes at the default cadence. Raise it if participants report the VP leaving meetings that are still in progress. | 15 | 1-90 |

`VPInstanceType`, `VPMinInstances` and `VPMaxInstances` apply only to `VPLaunchType=EC2`. Under `MICROVM` there are no hosts to size or scale — each meeting gets its own MicroVM, billed for its lifetime.

The three meeting-end parameters apply to the **Teams browser join path** only, and no deployment should normally need to change them — see [How the VP decides a meeting has ended](virtual-participant.md#how-the-vp-decides-a-meeting-has-ended) for what each signal measures and why the two thresholds differ so much. Changing them updates the VP task definition without rebuilding the container image, so the new values apply to the next meeting.

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
| AmazonNovaSonicRegion | Region to reach Amazon Nova Sonic in, when it differs from the region the stack is deployed to. Leave empty to use the stack's own region. Nova Sonic is available in fewer regions than LMA itself, so a deployment constrained to one region for compliance can keep everything else local and reach the voice assistant elsewhere. Only used when `VoiceAssistantProvider` is `amazon_nova_sonic`. | (empty — use the stack's region) | Empty, or an AWS Region name such as `eu-north-1` |
| ElevenLabsApiKey | API key for ElevenLabs voice assistant | (none) | Valid API key string |
| ElevenLabsAgentId | ElevenLabs conversational agent ID | (none) | Valid agent ID |

Setting `AmazonNovaSonicRegion` also grants the Virtual Participant's task role permission to invoke the Nova model in that region — without it the client would be pointed at a region IAM denies, and the voice assistant would fail to start with an access-denied error rather than anything that reads like a misconfiguration. Only the Nova Sonic model itself moves. The Bedrock calls behind the Virtual Participant's self-healing DOM resolver, the DynamoDB table holding the Nova Sonic configuration you set on the Nova Sonic page, and the meeting-assistant Lambda the voice assistant calls as a tool are all deployed in the stack's region and continue to be reached there.

Nothing validates that Nova Sonic is actually available in the region you name — check the [Amazon Bedrock model support by region](https://docs.aws.amazon.com/bedrock/latest/userguide/models-regions.html) table first. A region where the model is not enabled fails at the first voice interaction with a Bedrock validation error in the Virtual Participant's logs, not at deploy time.

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
