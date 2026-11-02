---
title: "CloudFormation Parameters Reference"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# CloudFormation Parameters Reference

This page lists every parameter of the main LMA template, `lma-main.yaml`, in the
groups and order the CloudFormation console shows them. You set these when you
create the stack, and change them with a stack update; see
[Stack Updates & Upgrades](stack-updates-and-upgrades.md).

Some of LMA's behaviour is not set here at all but on the web UI's
**Configuration** page, where an Admin changes it while the stack runs. That page,
and which settings it holds, are described in the
[Configuration Guide](configuration-guide.md).

## How to read this page

Each parameter has a **kind**, which says what changing it does:

- **Resource** parameters decide which resources the stack creates or how they
  are built — whether a nested stack exists, an IAM permission, networking,
  capacity, a retention rule, an alarm. A stack update that changes one changes
  resources.
- **Configuration page default** parameters set the default of a setting an
  Admin can change on the Configuration page while the stack runs. The entry
  names the setting. The stack writes the parameter's value into the
  configuration as that setting's default, so a stack update that changes the
  parameter applies wherever nobody has changed the setting on the page, and a
  value saved on the page takes precedence until it is reset to the default
  there. The parameter is kept so that existing deploy commands, pipelines and
  new installs work as before. A parameter that also reaches a resource, such as
  a bucket lifecycle rule, stays a **Resource** parameter, and its entry names
  the setting it is also the default of.
- **Configuration page limit** parameters are the default of a setting and also
  its upper limit: an Admin can set a lower value on the Configuration page and
  not a higher one, and every reader uses the smaller of the two. Only the
  retention settings work this way; see
  [Retention: settings the stack limits](configuration-guide.md#retention-settings-the-stack-limits).
- **Read at run time** parameters reach the application only as a value it
  reads: an environment variable of a function or task, or a deployment setting.
  A stack update that changes one changes only the configuration of whatever
  reads it; for a value built into the web app, that means the web app is
  rebuilt. The entry says what reads it and when, because that is when a change
  takes effect: a function reads its environment on each cold start, and a
  Virtual Participant reads its own when it starts. Where the entry says a value
  is checked while the stack deploys, a value that fails the check fails the
  update.
- **Credential** parameters are secrets, the ARNs of secrets, or the identifier
  paired with one. They are held in AWS Secrets Manager.
- A **Deployment check** is read only while the stack is being deployed.
- **No effect** marks a parameter the template still declares and nothing reads,
  kept so that deploy commands and pipelines that pass it go on working.

The reference below is generated from the template by
`scripts/cloudformation_parameters_doc.py`, and a test fails when the two
disagree. The description under each parameter is the one the console shows.

<!-- BEGIN GENERATED PARAMETER REFERENCE: edit lma-main.yaml or scripts/cloudformation_parameters_doc.py, then run that script -->

The template declares 92 parameters.

| Kind | Parameters | What changing one does |
|---|---|---|
| Resource | 47 | Decides which resources the stack creates or how they are built: whether a nested stack or resource exists, an IAM permission, networking, capacity, a retention rule or an alarm. Changing it is a stack update that changes resources. |
| Configuration page default | 26 | The default of a setting an Admin can change on the web UI's Configuration page while the stack runs; see the [Configuration Guide](configuration-guide.md). The stack writes the parameter's value into the configuration as that setting's default, so a stack update that changes it applies wherever the setting has not been changed on the page. A value saved on the page takes precedence until it is reset to the default there. |
| Configuration page limit | 1 | The default and the upper limit of a setting an Admin can lower, and not raise, on the web UI's Configuration page while the stack runs; see [Retention: settings the stack limits](configuration-guide.md#retention-settings-the-stack-limits). Every reader uses the smaller of the saved value and the parameter's, so a stack update that lowers the parameter applies at once. |
| Read at run time | 5 | Passed to application code as an environment variable or a deployment setting, and only read by that code. Changing it is a stack update that changes only the configuration of whatever reads it; for a value built into the web app, that means the web app is rebuilt. Where the entry says a value is checked while the stack deploys, a value that fails the check fails the update. |
| Credential | 11 | A secret, the ARN of a secret, or the identifier paired with one. Values are stored in AWS Secrets Manager; see [Integration Credentials](infrastructure-and-security.md#integration-credentials). |
| Deployment check | 1 | Read only while the stack is deployed, by a check that runs before anything else is created. |
| No effect | 1 | Declared by the template and read by nothing. Kept so that existing deploy commands and pipelines that pass it keep working. |

## Web UI Authentication

| Parameter | Default | Kind |
|---|---|---|
| [`AdminEmail`](#adminemail) | (required) | Resource |
| [`AllowedSignUpEmailDomain`](#allowedsignupemaildomain) | (empty) | Resource |

### `AdminEmail`

Console label: *Admin Email Address*

- **Default:** (required)
- **Allowed values:** Matches `^[\w.+-]+@([\w-]+\.)+[\w-]{2,6}$`
- **Kind:** Resource. Creates the administrator's Cognito user and subscribes the address to the operational alerts topic.

Email address of the admin user (e.g. jdoe+admin@example.com) that has access to all meetings for administrative purposes. An initial temporary password will be automatically sent to this user via email.

### `AllowedSignUpEmailDomain`

Console label: *Authorized Account Email Domain(s)*

- **Default:** (empty)
- **Allowed values:** Matches `^(|([\w-]+\.)+[\w-]{2,6}(, *([\w-]+\.)+[\w-]{2,6})*)$`
- **Kind:** Resource. Decides whether the Cognito sign-up check exists, and is read by it and by user management.

Email address domain (example.com) or comma separated list of email domains (example1.com, example2.com) allowed to signin and signup using the web UI. If left empty, signup via the web UI is disabled and users will have to be created using Cognito.

## Virtual Participant Startup Optimization

| Parameter | Default | Kind |
|---|---|---|
| [`VPLaunchType`](#vplaunchtype) | `MICROVM` | Resource |
| [`VPInstanceType`](#vpinstancetype) | `t3.medium` | Resource |
| [`VPMinInstances`](#vpmininstances) | `1` | Resource |
| [`VPMaxInstances`](#vpmaxinstances) | `10` | Resource |

### `VPLaunchType`

Console label: *VP Launch Type (LAMBDA MICROVM, FARGATE or EC2)*

- **Default:** `MICROVM`
- **Allowed values:** `FARGATE`, `EC2`, `MICROVM`
- **Kind:** Resource. Decides whether Virtual Participants run on EC2 hosts, Fargate tasks or Lambda MicroVMs, and which of those resources exist.

Compute platform for Virtual Participant containers. MICROVM (default): AWS Lambda MicroVMs — each meeting runs in its own Firecracker microVM resumed from a snapshot. Live view ready in ~20-25s, billed per meeting rather than per warm instance, and needs no load balancer, no autoscaling and no OS patching. Requires a region where Lambda MicroVMs are available (us-east-1, us-east-2, us-west-2, ap-northeast-1, eu-west-1 — not GovCloud); meetings are capped at 8 hours. EC2: Warm instances with cached images; ~$33/month per always-on instance, plus autoscaling and OS patching to manage. Use where MicroVMs are unavailable and startup latency matters. FARGATE: Serverless, ~$2/month base, but 1-2 minute startup. Use where MicroVMs are unavailable and cost matters more than latency.

### `VPInstanceType`

Console label: *VP EC2 Instance Type*

- **Default:** `t3.medium`
- **Allowed values:** `t3.medium`, `t3.large`, `t3.xlarge`, `c5.large`, `c5.xlarge`, `c5.2xlarge`, `m5.large`, `m5.xlarge`
- **Kind:** Resource. Sets the instance type of the Virtual Participant EC2 launch template.

EC2 instance type for Virtual Participant (only used if VPLaunchType=EC2). t3.medium recommended for basic transcription. For voice assistant + Simli avatar workloads, use c5.xlarge or larger for sustained CPU performance (no burstable throttling).

### `VPMinInstances`

Console label: *VP Minimum Warm Instances*

- **Default:** `1`
- **Allowed values:** 0 to 10
- **Kind:** Resource. Sets the minimum and desired size of the Virtual Participant Auto Scaling group.

Minimum number of warm EC2 instances (only used if VPLaunchType=EC2). Set to 1 for fastest startup, 0 to save costs when not in use.

### `VPMaxInstances`

Console label: *VP Maximum Instances*

- **Default:** `10`
- **Allowed values:** 1 to 100
- **Kind:** Resource. Sets the maximum size of the Virtual Participant Auto Scaling group.

Maximum number of EC2 instances for scaling (only used if VPLaunchType=EC2).

## Virtual Participant Meeting-End Detection (Teams)

| Parameter | Default | Kind |
|---|---|---|
| [`VPAttendeePollMs`](#vpattendeepollms) | `20000` | Configuration page default |
| [`VPPollsBeforeEnd`](#vppollsbeforeend) | `3` | Configuration page default |
| [`VPPollsBeforeEndMissing`](#vppollsbeforeendmissing) | `15` | Configuration page default |

### `VPAttendeePollMs`

Console label: *VP Teams Meeting-End Poll Interval (ms)*

- **Default:** `20000`
- **Allowed values:** 1000 to 120000
- **Kind:** Configuration page default. Configuration page: **Virtual Participant › Teams: milliseconds between meeting checks**. Read by the Virtual Participant when it starts, from its task environment.

How often a Teams Virtual Participant samples the meeting UI to decide whether the meeting is still running, in milliseconds. Leave at the default unless advised otherwise. This is the default; an Admin can change it at run time on the Configuration page (Virtual Participant section), which takes precedence.

### `VPPollsBeforeEnd`

Console label: *VP Teams Polls Before Leaving An Empty Meeting*

- **Default:** `3`
- **Allowed values:** 1 to 90
- **Kind:** Configuration page default. Configuration page: **Virtual Participant › Teams: checks alone before leaving**. Read by the Virtual Participant when it starts, from its task environment.

Consecutive polls on which a Teams meeting must show one or fewer attendees before the Virtual Participant leaves. 3 polls at the default cadence is about 60 seconds. This is the default; an Admin can change it at run time on the Configuration page (Virtual Participant section), which takes precedence.

### `VPPollsBeforeEndMissing`

Console label: *VP Teams Polls Before Leaving An Unreadable Meeting*

- **Default:** `15`
- **Allowed values:** 1 to 90
- **Kind:** Configuration page default. Configuration page: **Virtual Participant › Teams: checks without a meeting before leaving**. Read by the Virtual Participant when it starts, from its task environment.

Consecutive polls on which a Teams meeting must show neither a readable attendee count nor any in-meeting controls before the Virtual Participant leaves. 15 polls at the default cadence is about 5 minutes. Raise this if participants report the Virtual Participant leaving meetings that are still in progress. A value producing a tolerance outside 4 to 30 minutes at the chosen cadence is adjusted into that range, so at the default cadence anything below 12 is raised. This is the default; an Admin can change it at run time on the Configuration page (Virtual Participant section), which takes precedence.

## Meeting Assist Options

| Parameter | Default | Kind |
|---|---|---|
| [`MeetingAssistService`](#meetingassistservice) | `STRANDS_BEDROCK` | Resource |
| [`MeetingAssistServiceBedrockModelID`](#meetingassistservicebedrockmodelid) | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | Configuration page default |
| [`MeetingAssistKnowledgeBaseModelID`](#meetingassistknowledgebasemodelid) | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | Configuration page default |
| [`TavilyApiKey`](#tavilyapikey) | (empty) | Credential |
| [`TavilyApiKeySecretArn`](#tavilyapikeysecretarn) | (empty) | Credential |
| [`AssistantWakePhraseRegEx`](#assistantwakephraseregex) | `(OK\|Okay)[.,! ]*[Aa]ssistant` | Configuration page default |

### `MeetingAssistService`

Console label: *Meeting Assist Service*

- **Default:** `STRANDS_BEDROCK`
- **Allowed values:** `STRANDS_BEDROCK`, `STRANDS_BEDROCK_WITH_KB (Create)`, `STRANDS_BEDROCK_WITH_KB (Use Existing)`
- **Kind:** Resource. Decides whether the meeting assistant's knowledge base is created, used from an existing one, or absent.

Choose the meeting assistant service. STRANDS_BEDROCK provides agentic capabilities with built-in tools. STRANDS_BEDROCK_WITH_KB (Create) automatically creates a Bedrock Knowledge Base for document search. STRANDS_BEDROCK_WITH_KB (Use Existing) uses an existing Bedrock Knowledge Base you provide.

### `MeetingAssistServiceBedrockModelID`

Console label: *Meeting Assist Service Bedrock Model ID*

- **Default:** `global.anthropic.claude-haiku-4-5-20251001-v1:0`
- **Allowed values:** `amazon.nova-micro-v1:0`, `amazon.nova-lite-v1:0`, `amazon.nova-pro-v1:0`, `us.amazon.nova-premier-v1:0`, `global.amazon.nova-2-lite-v1:0`, `us.amazon.nova-2-lite-v1:0`, `us.anthropic.claude-opus-4-20250514-v1:0`, `us.anthropic.claude-opus-4-1-20250805-v1:0`, `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `us.anthropic.claude-sonnet-4-6`, `us.anthropic.claude-opus-4-6-v1`, `us.anthropic.claude-haiku-5-5`, `us.anthropic.claude-sonnet-5-5`, `us.anthropic.claude-opus-5-5`, `global.anthropic.claude-haiku-4-5-20251001-v1:0`, `global.anthropic.claude-sonnet-4-5-20250929-v1:0`, `global.anthropic.claude-sonnet-4-6`, `global.anthropic.claude-opus-4-6-v1`, `global.anthropic.claude-haiku-5-5`, `global.anthropic.claude-sonnet-5-5`, `global.anthropic.claude-opus-5-5`
- **Kind:** Configuration page default. Configuration page: **Amazon Bedrock Models › Meeting assistant**. Read by the meeting assistant and the troubleshooting agent. Their Amazon Bedrock permissions cover every model, so the choice changes no permission.

Select a Bedrock LLM model for the meeting assistant. Includes Nova models (cost-effective) and Claude 4+ models (cutting-edge). Some models require inference profiles. The Claude 5 generation reasons before it answers, which raises answer quality but also raises output token use several-fold, so it costs more per meeting than Claude 4.5 at the same size. This is the default; an Admin can change it at run time on the Configuration page (Amazon Bedrock Models section), which takes precedence.

### `MeetingAssistKnowledgeBaseModelID`

Console label: *Meeting Assist Knowledge Base Bedrock Model ID*

- **Default:** `global.anthropic.claude-haiku-4-5-20251001-v1:0`
- **Allowed values:** `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `us.anthropic.claude-sonnet-4-6`, `us.anthropic.claude-opus-4-6-v1`, `global.anthropic.claude-haiku-4-5-20251001-v1:0`, `global.anthropic.claude-sonnet-4-5-20250929-v1:0`, `global.anthropic.claude-sonnet-4-6`, `global.anthropic.claude-opus-4-6-v1`
- **Kind:** Configuration page default. Configuration page: **Amazon Bedrock Models › Meeting assistant knowledge bases**. Read by the meeting assistant's knowledge-base tools.

Select the Bedrock model the Meeting Assistant's knowledge bases use to generate an answer from what they retrieve -- the document-search tool and the past-meeting-search tool. This is separate from MeetingAssistServiceBedrockModelID so that the agent can run on any model it supports, including models a knowledge base cannot generate with. Whether a knowledge base can generate with a model is a property of that model, so only models verified against a live knowledge base are offered here. Enable access to the model you pick in the Amazon Bedrock console for your region. This is the default; an Admin can change it at run time on the Configuration page (Amazon Bedrock Models section), which takes precedence.

### `TavilyApiKey`

Console label: *Tavily API Key for Web Search (STRANDS_BEDROCK only)*

- **Default:** (empty)
- **Allowed values:** Must be empty or a valid Tavily API key (20+ characters)
- **Kind:** Credential. Stored in a Secrets Manager secret the meeting assistant reads.

(Optional - STRANDS_BEDROCK only) API key for Tavily web search service to enable web search tool. Get your API key from https://tavily.com. When provided, the Strands agent can search the web for current information during meetings. Leave blank to disable web search capability.

### `TavilyApiKeySecretArn`

Console label: *Existing Secret ARN Holding the Tavily API Key (Optional)*

- **Default:** (empty)
- **Allowed values:** Must be empty, or the ARN of a Secrets Manager secret (arn:aws:secretsmanager:&lt;region&gt;:&lt;account&gt;:secret:&lt;name&gt;).
- **Kind:** Credential. Grants the meeting assistant read access to the named secret.

(Optional) ARN of an existing Secrets Manager secret whose value is the Tavily API key. Use this instead of TavilyApiKey when the key is already provisioned in Secrets Manager. Supply one form or the other, not both. A secret encrypted with your own KMS key must grant kms:Decrypt to the meeting-assist function's role in that key's policy.

### `AssistantWakePhraseRegEx`

Console label: *Meeting Assist Wake Phrase Regular Expression*

- **Default:** `(OK|Okay)[.,! ]*[Aa]ssistant`
- **Allowed values:** Any text
- **Kind:** Configuration page default. Configuration page: **Meeting Assistant › Wake phrase (regular expression)**. Written to the deployment settings and read by the meeting event processor.

A regular expression for the wake phrase that invokes the Meeting Assistant. The shipped pattern matches 'OK Assistant'. This is the default; an Admin can change it at run time on the Configuration page (Meeting Assistant section), which takes precedence.

## Meeting Assist Voice Assistant

| Parameter | Default | Kind |
|---|---|---|
| [`ElevenLabsApiKey`](#elevenlabsapikey) | (empty) | Credential |
| [`ElevenLabsApiKeySecretArn`](#elevenlabsapikeysecretarn) | (empty) | Credential |
| [`ElevenLabsAgentId`](#elevenlabsagentid) | (empty) | Configuration page default |
| [`SimliApiKey`](#simliapikey) | (empty) | Credential |
| [`SimliApiKeySecretArn`](#simliapikeysecretarn) | (empty) | Credential |
| [`SimliFaceId`](#simlifaceid) | (empty) | Configuration page default |
| [`VoiceAssistantProvider`](#voiceassistantprovider) | `none` | Configuration page default |
| [`AmazonNovaSonicRegion`](#amazonnovasonicregion) | (empty) | Resource |
| [`VoiceAssistantActivationMode`](#voiceassistantactivationmode) | `wake_phrase` | Configuration page default |
| [`VoiceAssistantWakePhrases`](#voiceassistantwakephrases) | `hey alex,ok alex,hi alex,hello alex` | Configuration page default |
| [`VoiceAssistantActivationDuration`](#voiceassistantactivationduration) | `30` | Configuration page default |
| [`ZoomMeetingSdkClientId`](#zoommeetingsdkclientid) | (empty) | Credential |
| [`ZoomMeetingSdkClientSecret`](#zoommeetingsdkclientsecret) | (empty) | Credential |
| [`ZoomMeetingSdkClientSecretArn`](#zoommeetingsdkclientsecretarn) | (empty) | Credential |
| [`AcsConnectionString`](#acsconnectionstring) | (empty) | Credential |
| [`AcsConnectionStringSecretArn`](#acsconnectionstringsecretarn) | (empty) | Credential |

### `ElevenLabsApiKey`

Console label: *ElevenLabs API Key for Virtual Participant Voice (Optional)*

- **Default:** (empty)
- **Allowed values:** Must be empty or a valid ElevenLabs API key (20+ characters)
- **Kind:** Credential. Stored in a Secrets Manager secret the Virtual Participant is given.

(Optional) API key for ElevenLabs text-to-speech service to enable Virtual Participant voice responses. Get your API key from https://elevenlabs.io. When provided, the Virtual Participant can speak agent responses in meetings using natural-sounding voices. Leave blank to disable TTS capability.

### `ElevenLabsApiKeySecretArn`

Console label: *Existing Secret ARN Holding the ElevenLabs API Key (Optional)*

- **Default:** (empty)
- **Allowed values:** Must be empty, or the ARN of a Secrets Manager secret (arn:aws:secretsmanager:&lt;region&gt;:&lt;account&gt;:secret:&lt;name&gt;).
- **Kind:** Credential. Grants the Virtual Participant read access to the named secret.

(Optional) ARN of an existing Secrets Manager secret whose value is the ElevenLabs API key. Use this instead of ElevenLabsApiKey when the key is already provisioned in Secrets Manager. Supply one form or the other, not both. A secret encrypted with your own KMS key must grant kms:Decrypt in that key's policy to whichever role reads it: the MicroVM launcher function's role under VPLaunchType MICROVM (the default), or the Virtual Participant task execution role on FARGATE and EC2. See docs/infrastructure-and-security.md#bringing-your-own-secret.

### `ElevenLabsAgentId`

Console label: *ElevenLabs Agent ID for Custom Personality (Optional)*

- **Default:** (empty)
- **Allowed values:** Must be empty or a valid ElevenLabs Agent ID
- **Kind:** Configuration page default. Configuration page: **Virtual Participant › ElevenLabs agent ID**. Read by the Virtual Participant when its voice assistant starts.

(Optional) ElevenLabs Conversational AI Agent ID for customized voice assistant behavior. Create an agent at https://elevenlabs.io/app/conversational-ai and copy its ID here. Leave blank to use default conversational AI without custom personality. This is the default; an Admin can change it at run time on the Configuration page (Virtual Participant section), which takes precedence.

### `SimliApiKey`

Console label: *Simli API Key for Avatar (Optional)*

- **Default:** (empty)
- **Allowed values:** Any text
- **Kind:** Credential. Stored in a Secrets Manager secret the Virtual Participant is given.

(Optional) API key for Simli avatar. The avatar feature requires BOTH SimliApiKey and SimliFaceId — leave both blank to disable the avatar, or set both to enable an animated lip-synced avatar as the Virtual Participant's camera feed. Get your API key from https://simli.com. Leave blank to disable avatar.

### `SimliApiKeySecretArn`

Console label: *Existing Secret ARN Holding the Simli API Key (Optional)*

- **Default:** (empty)
- **Allowed values:** Must be empty, or the ARN of a Secrets Manager secret (arn:aws:secretsmanager:&lt;region&gt;:&lt;account&gt;:secret:&lt;name&gt;).
- **Kind:** Credential. Grants the Virtual Participant read access to the named secret.

(Optional) ARN of an existing Secrets Manager secret whose value is the Simli API key. Use this instead of SimliApiKey when the key is already provisioned in Secrets Manager. Supply one form or the other, not both. SimliFaceId is still required alongside it. A secret encrypted with your own KMS key must grant kms:Decrypt in that key's policy to whichever role reads it: the MicroVM launcher function's role under VPLaunchType MICROVM (the default), or the Virtual Participant task execution role on FARGATE and EC2. See docs/infrastructure-and-security.md#bringing-your-own-secret.

### `SimliFaceId`

Console label: *Simli Face ID for Avatar Appearance (Optional)*

- **Default:** (empty)
- **Allowed values:** Any text
- **Kind:** Configuration page default. Configuration page: **Virtual Participant › Simli face ID**. Read by the Virtual Participant when its avatar starts.

(Optional) Simli Face ID for the avatar appearance. Required when SimliApiKey is set — the avatar is enabled only when BOTH parameters are non-empty. Get this from your Simli dashboard at https://simli.com. This is the default; an Admin can change it at run time on the Configuration page (Virtual Participant section), which takes precedence.

### `VoiceAssistantProvider`

Console label: *Voice Assistant Provider (none, elevenlabs, amazon_nova_sonic)*

- **Default:** `none`
- **Allowed values:** `none`, `elevenlabs`, `amazon_nova_sonic`
- **Kind:** Configuration page default. Configuration page: **Virtual Participant › Voice assistant**. Read by the Virtual Participant when it starts.

Voice AI assistant provider for Virtual Participant. Choose 'none' to disable voice responses (default), 'elevenlabs' for ElevenLabs Conversational AI (requires API key above), or 'amazon_nova_sonic' for Amazon Nova Sonic, which needs no API key and is configured in the Nova Sonic Voice Assistant section of the Configuration page in the LMA UI. This is the default; an Admin can change it at run time on the Configuration page (Virtual Participant section), which takes precedence.

### `AmazonNovaSonicRegion`

Console label: *Amazon Nova Sonic Region (blank = this stack's region)*

- **Default:** (empty)
- **Allowed values:** Must be empty, or an AWS Region name such as us-east-1.
- **Kind:** Resource. Names the region in the Virtual Participant's permission to invoke Amazon Nova Sonic.

Region to reach Amazon Nova Sonic in, when it differs from the region this stack is deployed to. Leave empty to use the stack's own region. Nova Sonic is available in fewer regions than LMA itself, so a deployment constrained to a particular region for compliance can keep everything else local and reach the voice assistant elsewhere. Only used when VoiceAssistantProvider is amazon_nova_sonic.

### `VoiceAssistantActivationMode`

Console label: *Voice Assistant Activation Mode*

- **Default:** `wake_phrase`
- **Allowed values:** `always_active`, `wake_phrase`, `strands_tool (coming soon)`
- **Kind:** Configuration page default. Configuration page: **Virtual Participant › Voice assistant activation mode**. Read by the Virtual Participant when it starts, and by the Configuration page to say whether the Nova Sonic meeting modes apply.

How the voice assistant is activated. 'always_active' for interviewer/meeting leader use cases (agent leads conversation), 'wake_phrase' to activate only when wake phrase is detected (max 300 seconds), or 'strands_tool' for intelligent activation by Strands agent. This is the default; an Admin can change it at run time on the Configuration page (Virtual Participant section), which takes precedence.

### `VoiceAssistantWakePhrases`

Console label: *Voice Assistant Wake Phrases (comma-separated)*

- **Default:** `hey alex,ok alex,hi alex,hello alex`
- **Allowed values:** Any text
- **Kind:** Configuration page default. Configuration page: **Virtual Participant › Wake phrases**. Read by the Virtual Participant when it starts.

Comma-separated list of wake phrases for voice assistant activation (case-insensitive). Only used when VoiceAssistantActivationMode is 'wake_phrase'. Example: "hey alex,ok alex,hi alex" This is the default; an Admin can change it at run time on the Configuration page (Virtual Participant section), which takes precedence.

### `VoiceAssistantActivationDuration`

Console label: *Voice Assistant Activation Duration (seconds)*

- **Default:** `30`
- **Allowed values:** 5 to 300
- **Kind:** Configuration page default. Configuration page: **Virtual Participant › Seconds the voice assistant stays active**. Read by the Virtual Participant when it starts.

How long the voice assistant stays active after wake phrase detection (in seconds). Only used when VoiceAssistantActivationMode is 'wake_phrase'. Default is 30 seconds. Maximum is 300 seconds. This is the default; an Admin can change it at run time on the Configuration page (Virtual Participant section), which takes precedence.

### `ZoomMeetingSdkClientId`

Console label: *Zoom Meeting SDK Client ID (Optional)*

- **Default:** (empty)
- **Allowed values:** Any text
- **Kind:** Credential. The identifier paired with the Zoom Meeting SDK client secret; with a secret, Zoom meetings join through the SDK.

(Optional) Zoom Meeting SDK app Client ID. When both ZoomMeetingSdkClientId and ZoomMeetingSdkClientSecret are provided, the Virtual Participant joins Zoom meetings via the Zoom Meeting Web SDK instead of automating the public web client. Requires Zoom approval/allowlist of the SDK app for meetings outside the app owner's own account. Leave both blank to use the default web-client join.

### `ZoomMeetingSdkClientSecret`

Console label: *Zoom Meeting SDK Client Secret (Optional)*

- **Default:** (empty)
- **Allowed values:** Any text
- **Kind:** Credential. Stored in a Secrets Manager secret the Virtual Participant is given.

(Optional) Zoom Meeting SDK app Client Secret. Paired with ZoomMeetingSdkClientId. Used to sign the SDK join signature.

### `ZoomMeetingSdkClientSecretArn`

Console label: *Existing Secret ARN Holding the Zoom Meeting SDK Client Secret (Optional)*

- **Default:** (empty)
- **Allowed values:** Must be empty, or the ARN of a Secrets Manager secret (arn:aws:secretsmanager:&lt;region&gt;:&lt;account&gt;:secret:&lt;name&gt;).
- **Kind:** Credential. Grants the Virtual Participant read access to the named secret.

(Optional) ARN of an existing Secrets Manager secret whose value is the Zoom Meeting SDK app client secret. Use this instead of ZoomMeetingSdkClientSecret when the secret is already provisioned in Secrets Manager. Supply one form or the other, not both. ZoomMeetingSdkClientId is still required alongside it. A secret encrypted with your own KMS key must grant kms:Decrypt in that key's policy to whichever role reads it: the MicroVM launcher function's role under VPLaunchType MICROVM (the default), or the Virtual Participant task execution role on FARGATE and EC2. See docs/infrastructure-and-security.md#bringing-your-own-secret.

### `AcsConnectionString`

Console label: *Azure Communication Services Connection String for Teams SDK Join (Optional)*

- **Default:** (empty)
- **Allowed values:** Must be blank, or a full Azure Communication Services connection string of the form endpoint=https://&lt;resource&gt;.communication.azure.com/;accesskey=&lt;key&gt; (copy the "Connection string" field from Settings &gt; Keys in the Azure portal — not the endpoint or key on their own).
- **Kind:** Credential. Stored in a Secrets Manager secret the Virtual Participant is given.

(Optional) Azure Communication Services connection string (endpoint=https://...;accesskey=...). When provided, the Virtual Participant joins Microsoft Teams meetings via the ACS calling SDK (Teams interoperability) instead of automating the Teams web client. Leave blank to use the default web-client join.

### `AcsConnectionStringSecretArn`

Console label: *Existing Secret ARN Holding the ACS Connection String (Optional)*

- **Default:** (empty)
- **Allowed values:** Must be empty, or the ARN of a Secrets Manager secret (arn:aws:secretsmanager:&lt;region&gt;:&lt;account&gt;:secret:&lt;name&gt;).
- **Kind:** Credential. Grants the Virtual Participant read access to the named secret.

(Optional) ARN of an existing Secrets Manager secret whose value is the full Azure Communication Services connection string. Use this instead of AcsConnectionString when the connection string is already provisioned in Secrets Manager. Supply one form or the other, not both. A secret encrypted with your own KMS key must grant kms:Decrypt in that key's policy to whichever role reads it: the MicroVM launcher function's role under VPLaunchType MICROVM (the default), or the Virtual Participant task execution role on FARGATE and EC2. See docs/infrastructure-and-security.md#bringing-your-own-secret.

## Meeting Assist Bedrock Guardrails

| Parameter | Default | Kind |
|---|---|---|
| [`BedrockGuardrailId`](#bedrockguardrailid) | (empty) | Resource |
| [`BedrockGuardrailVersion`](#bedrockguardrailversion) | `DRAFT` | Read at run time |

### `BedrockGuardrailId`

Console label: *Bedrock Guardrail Id (optional)*

- **Default:** (empty)
- **Allowed values:** Matches `^(|[a-z0-9]+)$`
- **Kind:** Resource. Names the guardrail in the meeting assistant's and troubleshooting agent's permission to apply it.

Optionally provide the *Id* (not name) of an *existing* Bedrock Guardrail to be applied to the Strands meeting assistant agent and Knowledge Base queries.

### `BedrockGuardrailVersion`

Console label: *Bedrock Guardrail Version (optional)*

- **Default:** `DRAFT`
- **Allowed values:** Matches `^(|(([1-9][0-9]{0,7})|(DRAFT)))$`
- **Kind:** Read at run time. Read by the meeting assistant and the troubleshooting agent. The permission to apply the guardrail exists only while both it and BedrockGuardrailId are set.

If you provided a Bedrock Guardrail Id above, provide the corresponding Guardrail version here.

## Meeting Assist Bedrock Knowledge Base

| Parameter | Default | Kind |
|---|---|---|
| [`BedrockKnowledgeBaseId`](#bedrockknowledgebaseid) | (empty) | Resource |
| [`BedrockKnowledgeBaseS3BucketName`](#bedrockknowledgebases3bucketname) | (empty) | Resource |
| [`BedrockKnowledgeBaseS3DocumentUploadFolderPrefix`](#bedrockknowledgebases3documentuploadfolderprefix) | (empty) | Resource |

### `BedrockKnowledgeBaseId`

Console label: *Bedrock Knowledge Base Id (existing)*

- **Default:** (empty)
- **Allowed values:** Matches `^(|[0-9a-zA-Z]{10})$`
- **Kind:** Resource. Names the existing knowledge base the meeting assistant is wired to when MeetingAssistService uses one; a template rule requires it then.

If MeetingAssistService is 'STRANDS_BEDROCK_WITH_KB (Use Existing)', provide the knowledge base *Id* (not name) of an *existing* Bedrock Knowledge Base to be used for document search.

### `BedrockKnowledgeBaseS3BucketName`

Console label: *S3 bucket with knowledge base source documents (optional).*

- **Default:** (empty)
- **Allowed values:** Any text
- **Kind:** Resource. Becomes the data source of the knowledge base the stack creates, and the bucket that knowledge base may read.

If MeetingAssistService is 'STRANDS_BEDROCK_WITH_KB (Create)', provide the bucket name of an existing bucket with documents to ingest, or leave blank if you don't need an S3 data source.

### `BedrockKnowledgeBaseS3DocumentUploadFolderPrefix`

Console label: *S3 prefix(es) for your content (optional)*

- **Default:** (empty)
- **Allowed values:** Any text
- **Kind:** Resource. Sets the inclusion prefixes of that data source.

If MeetingAssistService is 'STRANDS_BEDROCK_WITH_KB (Create)', provide comma separated list of optional S3 prefixes with the paths to your source documents e.g. "prefix1/, prefix2/". If you leave blank, all documents in your S3 bucket will be scanned.

## Transcript Knowledge Base Configuration

| Parameter | Default | Kind |
|---|---|---|
| [`TranscriptKnowledgeBase`](#transcriptknowledgebase) | `BEDROCK_KNOWLEDGE_BASE (Create)` | Resource |

### `TranscriptKnowledgeBase`

Console label: *Transcript Knowledge Base*

- **Default:** `BEDROCK_KNOWLEDGE_BASE (Create)`
- **Allowed values:** `DISABLED`, `BEDROCK_KNOWLEDGE_BASE (Create)`
- **Kind:** Resource. Decides whether the meeting-history knowledge base is created.

Use meeting transcripts as knowledge base content

## LMA Hosted MCP Server

| Parameter | Default | Kind |
|---|---|---|
| [`EnableMCP`](#enablemcp) | `true` | Resource |
| [`MCPServerCallbackURLs`](#mcpservercallbackurls) | `https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback,https://us-west-2.quicksight.aws.amazon.com/sn/oauthcallback,http://localhost:3000/callback` | Resource |

### `EnableMCP`

Console label: *Enable MCP Server Integration*

- **Default:** `true`
- **Allowed values:** `true`, `false`
- **Kind:** Resource. Decides whether the hosted MCP server and its resources exist.

Enable MCP server integration for external application access (Amazon Quick Suite, Claude Desktop, etc.). Provides standardized API for agentic systems.

### `MCPServerCallbackURLs`

Console label: *MCP Server OAuth Callback URLs*

- **Default:** `https://us-east-1.quicksight.aws.amazon.com/sn/oauthcallback,https://us-west-2.quicksight.aws.amazon.com/sn/oauthcallback,http://localhost:3000/callback`
- **Allowed values:** Any text
- **Kind:** Resource. Sets the OAuth callback URLs of the MCP server's Cognito app client.

Comma-separated list of OAuth callback URLs for MCP clients. Default includes Quick Suite (us-east-1, us-west-2) and localhost for testing. Add your custom MCP client callback URLs here. Example: https://my-app.com/callback,https://another-app.com/oauth/callback

## Amazon Transcribe Configuration

| Parameter | Default | Kind |
|---|---|---|
| [`IsPartialTranscriptEnabled`](#ispartialtranscriptenabled) | `true` | No effect |
| [`IsContentRedactionEnabled`](#iscontentredactionenabled) | `false` | Configuration page default |
| [`TranscribeLanguageCode`](#transcribelanguagecode) | `en-US` | Configuration page default |
| [`TranscribeLanguageOptions`](#transcribelanguageoptions) | `en-US, es-US` | Configuration page default |
| [`TranscribePreferredLanguage`](#transcribepreferredlanguage) | `None` | Configuration page default |
| [`TranscribeContentRedactionType`](#transcribecontentredactiontype) | `PII` | Read at run time |
| [`ShowSpeakerLabel`](#showspeakerlabel) | `false` | Configuration page default |
| [`TranscribePiiEntityTypes`](#transcribepiientitytypes) | `BANK_ACCOUNT_NUMBER,BANK_ROUTING,CREDIT_DEBIT_NUMBER,CREDIT_DEBIT_CVV,CREDIT_DEBIT_EXPIRY,PIN,EMAIL,ADDRESS,NAME,PHONE,SSN` | Configuration page default |
| [`CustomVocabularyName`](#customvocabularyname) | (empty) | Configuration page default |
| [`CustomLanguageModelName`](#customlanguagemodelname) | (empty) | Configuration page default |

### `IsPartialTranscriptEnabled`

Console label: *Enable Partial Transcripts*

- **Default:** `true`
- **Allowed values:** `true`, `false`
- **Kind:** No effect. Partial transcripts are always processed.

Enable partial transcripts to receive low latency evolving transcriptions for each conversation turn. Set to false to process only the final version of each conversation turn.

### `IsContentRedactionEnabled`

Console label: *Enable Content Redaction for Transcripts*

- **Default:** `false`
- **Allowed values:** `true`, `false`
- **Kind:** Configuration page default. Configuration page: **Amazon Transcribe › Redact personal information**. Read by the WebSocket transcriber for each streaming session, and by the Virtual Participant when it starts. Redaction applies only for the languages Amazon Transcribe redacts.

Enable content redaction from Amazon Transcribe transcription output. This is only used when the 'en-US' language is selected in the TranscribeLanguageCode parameter. This is the default; an Admin can change it at run time on the Configuration page (Amazon Transcribe section), which takes precedence.

### `TranscribeLanguageCode`

Console label: *Language for Transcription*

- **Default:** `en-US`
- **Allowed values:** `identify-language`, `identify-multiple-languages`, `en-US`, `es-US`, `en-GB`, `fr-CA`, `fr-FR`, `en-AU`, `it-IT`, `de-DE`, `pt-BR`, `ja-JP`, `ko-KR`, `zh-CN`, `hi-IN`, `th-TH`
- **Kind:** Configuration page default. Configuration page: **Amazon Transcribe › Language**. Read by the WebSocket transcriber for each streaming session, and by the Virtual Participant when it starts; checked once while the stack is deployed.

Language code or language identification setting for Amazon Transcribe. NOTE: Language identification can't be combined with custom language models or redaction. This is the default; an Admin can change it at run time on the Configuration page (Amazon Transcribe section), which takes precedence.

### `TranscribeLanguageOptions`

Console label: *Transcribe Language Options*

- **Default:** `en-US, es-US`
- **Allowed values:** Unsupported language code or invalid syntax. Allowed values are: en-US, es-US, en-GB, fr-CA, fr-FR, en-AU, it-IT, de-DE, pt-BR, ja-JP, ko-KR, zh-CN, hi-IN, th-TH
- **Kind:** Configuration page default. Configuration page: **Amazon Transcribe › Language options**. Read by the WebSocket transcriber for each streaming session, and by the Virtual Participant when it starts; checked once while the stack is deployed.

When using language identification, specify two or more language codes, e.g. "en-US, es-US" that represent the languages that may be used in your meeting. Including more than five is not recommended. Enter only one language dialect per language - e.g. 'en-US, en-AU' is not allowed. Allowed values are: en-US, es-US, en-GB, fr-CA, fr-FR, en-AU, it-IT, de-DE, pt-BR, ja-JP, ko-KR, zh-CN, hi-IN, th-TH NOTE: The identify-language feature has not yet been applied to the virtual participant. If selected, the virtual participant will use the value of TranscribePreferredLanguage (if set) or 'en-US' (by default). This is the default; an Admin can change it at run time on the Configuration page (Amazon Transcribe section), which takes precedence.

### `TranscribePreferredLanguage`

Console label: *Transcribe Preferred Language*

- **Default:** `None`
- **Allowed values:** `None`, `en-US`, `es-US`, `en-GB`, `fr-CA`, `fr-FR`, `en-AU`, `it-IT`, `de-DE`, `pt-BR`, `ja-JP`, `ko-KR`, `zh-CN`, `hi-IN`, `th-TH`
- **Kind:** Configuration page default. Configuration page: **Amazon Transcribe › Preferred language**. Read by the WebSocket transcriber for each streaming session, and by the Virtual Participant when it starts; checked once while the stack is deployed.

Optionally select a preferred language when using language identification - must be one of the languages entered for TranscribeLanguageOptions. This is the default; an Admin can change it at run time on the Configuration page (Amazon Transcribe section), which takes precedence.

### `TranscribeContentRedactionType`

Console label: *Content Redaction Type for Transcription*

- **Default:** `PII`
- **Allowed values:** `PII`
- **Kind:** Read at run time. Read by the WebSocket transcriber for each streaming session, and by the Virtual Participant when it starts.

Type of content redaction from Amazon Transcribe transcription output.

### `ShowSpeakerLabel`

Console label: *Default for Speaker Identification on Streaming Sessions*

- **Default:** `false`
- **Allowed values:** `true`, `false`
- **Kind:** Configuration page default. Configuration page: **Amazon Transcribe › Identify speakers within a channel by default**. Read by the WebSocket transcriber for each streaming session.

Default for Amazon Transcribe speaker partitioning (diarization) on WebSocket streaming sessions - the desktop capture app and the Stream Audio browser tab. When enabled, Transcribe tells apart individual voices within an audio channel and each distinct voice is appended to the channel's speaker name as "(spk_0)", "(spk_1)", and so on. Works best with five or fewer voices per channel. This is only the DEFAULT: clients that offer per-channel speaker identification settings (the desktop capture app and the Stream Audio tab) send their own choice per meeting, which takes precedence. Leave "false" unless you want it on for clients that do not. This is the default; an Admin can change it at run time on the Configuration page (Amazon Transcribe section), which takes precedence.

### `TranscribePiiEntityTypes`

Console label: *Transcription PII Redaction Entity Types*

- **Default:** `BANK_ACCOUNT_NUMBER,BANK_ROUTING,CREDIT_DEBIT_NUMBER,CREDIT_DEBIT_CVV,CREDIT_DEBIT_EXPIRY,PIN,EMAIL,ADDRESS,NAME,PHONE,SSN`
- **Allowed values:** Any text
- **Kind:** Configuration page default. Configuration page: **Amazon Transcribe › Entity types to redact**. Read by the WebSocket transcriber for each streaming session.

Select the PII entity types you want to identify or redact. Remove the values that you don't want to redact from the default. DO NOT ADD CUSTOM VALUES HERE. This is the default; an Admin can change it at run time on the Configuration page (Amazon Transcribe section), which takes precedence.

### `CustomVocabularyName`

Console label: *Transcription Custom Vocabulary Name*

- **Default:** (empty)
- **Allowed values:** Any text
- **Kind:** Configuration page default. Configuration page: **Amazon Transcribe › Custom vocabulary**. Read by the WebSocket transcriber for each streaming session, and by the Virtual Participant when it starts; checked once while the stack is deployed.

The name of the vocabulary to use when processing the transcription job. Leave blank if no custom vocabulary to be used. If yes, the custom vocabulary must pre-exist in your account. This is the default; an Admin can change it at run time on the Configuration page (Amazon Transcribe section), which takes precedence.

### `CustomLanguageModelName`

Console label: *Transcription Custom Language Model Name*

- **Default:** (empty)
- **Allowed values:** Any text
- **Kind:** Configuration page default. Configuration page: **Amazon Transcribe › Custom language model**. Read by the WebSocket transcriber for each streaming session; checked once while the stack is deployed.

The name of the custom language model to use when processing the transcription job. Leave blank if no custom language model is to be used. If specified, the custom language model must pre-exist in your account, match the Language Code selected above, and use the 'Narrow Band' base model. This is the default; an Admin can change it at run time on the Configuration page (Amazon Transcribe section), which takes precedence.

## On-demand speech engine (EXPERIMENTAL)

| Parameter | Default | Kind |
|---|---|---|
| [`EnableMicrovmAsr`](#enablemicrovmasr) | `false` | Resource |

### `EnableMicrovmAsr`

Console label: *Deploy the on-demand speech engine (EXPERIMENTAL)*

- **Default:** `false`
- **Allowed values:** `true`, `false`
- **Kind:** Resource. Decides whether the on-demand speech engine's nested stack exists.

Deploys the EXPERIMENTAL on-demand speech engine (open-source models on AWS Lambda MicroVMs in this account) alongside Amazon Transcribe. Nothing changes until an admin moves Stream Audio, Chrome extension and Desktop Capture meetings, or Virtual Participants, onto it in the Transcription Engine section of the Configuration page. English only; requires a region with AWS Lambda MicroVMs. See docs/microvm-asr.md.

## Voice enrollment (biometric; off by default)

| Parameter | Default | Kind |
|---|---|---|
| [`EnableVoiceEnrollment`](#enablevoiceenrollment) | `false` | Resource |
| [`VoiceEnrollmentRetentionDays`](#voiceenrollmentretentiondays) | `365` | Read at run time |
| [`VoiceEnrollmentTermsVersion`](#voiceenrollmenttermsversion) | `1` | Read at run time |

### `EnableVoiceEnrollment`

Console label: *Enable voice enrollment (stores biometric data)*

- **Default:** `false`
- **Allowed values:** `true`, `false`
- **Kind:** Resource. Decides whether the enrollment table, the consent log and the enrollment portal exist.

Let people enrol their voice so they are labelled by name in meetings from their first word. A voiceprint is biometric data about a person: while this is false the enrollment table, the consent log and the consent portal do not exist in the deployment at all. Turning it on creates obligations that are yours -- biometric data is separately regulated in many jurisdictions and typically requires informed consent before collection, a published retention schedule and deletion on request. See docs/voice-enrollment.md. Matching also needs EnableMicrovmAsr=true, since Amazon Transcribe exposes no speaker embedding.

### `VoiceEnrollmentRetentionDays`

Console label: *Voice Enrollment Consent Validity In Days*

- **Default:** `365`
- **Allowed values:** 1 to 3650
- **Kind:** Read at run time. Read by the voice-enrollment functions. It stays a stack parameter by design: the period is stated in the consent terms each enrollment agrees to, so a change is a change of terms.

How long a voice-enrollment consent stands before it must be renewed. When it lapses the voiceprint is deleted and the person is asked to consent again. Consent that never expires is not meaningfully informed.

### `VoiceEnrollmentTermsVersion`

Console label: *Voice Enrollment Terms Version*

- **Default:** `1`
- **Allowed values:** Matches `^[A-Za-z0-9._-]{1,32}$`
- **Kind:** Read at run time. Read by the voice-enrollment functions. It stays a stack parameter by design: a change asks everybody to consent again.

Bump this to require everybody to re-consent. An enrollment whose consent is to a superseded version stops being used for matching until its owner agrees to the current terms.

## WebSocket Transcriber Service Scaling

| Parameter | Default | Kind |
|---|---|---|
| [`TranscriberDesiredTaskCount`](#transcriberdesiredtaskcount) | `1` | Resource |
| [`TranscriberAutoScalingEnabled`](#transcriberautoscalingenabled) | `false` | Resource |
| [`TranscriberMinTaskCount`](#transcribermintaskcount) | `1` | Resource |
| [`TranscriberMaxTaskCount`](#transcribermaxtaskcount) | `4` | Resource |
| [`TranscriberScalingCpuTargetPercent`](#transcriberscalingcputargetpercent) | `35` | Resource |

### `TranscriberDesiredTaskCount`

Console label: *WebSocket Transcriber Task Count*

- **Default:** `1`
- **Allowed values:** 1 to 20
- **Kind:** Resource. Sets the task count of the transcriber's ECS service.

Number of Fargate tasks running the WebSocket transcriber service (used by the Stream Audio page and the desktop capture apps). Leave at 1 unless EnableVideoRecording is false: with video recording enabled, the desktop video stream arrives as a separate connection that must reach the task holding the meeting's audio session, which only a single task guarantees.

### `TranscriberAutoScalingEnabled`

Console label: *Enable WebSocket Transcriber Auto Scaling*

- **Default:** `false`
- **Allowed values:** `true`, `false`
- **Kind:** Resource. Decides whether the transcriber's auto scaling target and policy exist.

Scale the WebSocket transcriber service on average CPU utilization. Requires EnableVideoRecording to be false, for the same reason as TranscriberDesiredTaskCount. Capacity is only ever added automatically, so that scaling never interrupts a meeting in progress; it stays at its high water mark until you reduce it deliberately, or until you set this back to "false" and the task count returns to TranscriberDesiredTaskCount.

### `TranscriberMinTaskCount`

Console label: *WebSocket Transcriber Minimum Tasks*

- **Default:** `1`
- **Allowed values:** 1 to 20
- **Kind:** Resource. Sets the minimum capacity of the transcriber's auto scaling target.

Minimum number of WebSocket transcriber tasks when TranscriberAutoScalingEnabled is true.

### `TranscriberMaxTaskCount`

Console label: *WebSocket Transcriber Maximum Tasks*

- **Default:** `4`
- **Allowed values:** 1 to 50
- **Kind:** Resource. Sets the maximum capacity of the transcriber's auto scaling target.

Maximum number of WebSocket transcriber tasks when TranscriberAutoScalingEnabled is true.

### `TranscriberScalingCpuTargetPercent`

Console label: *WebSocket Transcriber Target CPU Utilization (%)*

- **Default:** `35`
- **Allowed values:** 10 to 90
- **Kind:** Resource. Sets the target of the transcriber's scaling policy.

Average CPU utilization that WebSocket transcriber auto scaling aims to hold. Kept below the load at which a task reports itself unhealthy to the load balancer.

## Transcript Event Processing Configuration

| Parameter | Default | Kind |
|---|---|---|
| [`TranscriptLambdaHookFunctionArn`](#transcriptlambdahookfunctionarn) | (empty) | Resource |
| [`TranscriptLambdaHookFunctionNonPartialOnly`](#transcriptlambdahookfunctionnonpartialonly) | `true` | Configuration page default |
| [`EndOfCallTranscriptSummary`](#endofcalltranscriptsummary) | `BEDROCK` | Resource |
| [`AgendaDerivationAfterSeconds`](#agendaderivationafterseconds) | `300` | Configuration page default |
| [`BedrockModelId`](#bedrockmodelid) | `global.anthropic.claude-sonnet-5-5` | Configuration page default |
| [`KeyframeTextModelId`](#keyframetextmodelid) | `amazon.nova-lite-v1:0` | Configuration page default |
| [`EndOfCallLambdaHookFunctionArn`](#endofcalllambdahookfunctionarn) | (empty) | Resource |
| [`StartOfCallLambdaHookFunctionArn`](#startofcalllambdahookfunctionarn) | (empty) | Resource |
| [`PostCallSummaryLambdaHookFunctionArn`](#postcallsummarylambdahookfunctionarn) | (empty) | Resource |

### `TranscriptLambdaHookFunctionArn`

Console label: *Lambda Hook Function ARN for Custom Transcript Segment Processing (existing)*

- **Default:** (empty)
- **Allowed values:** Matches `^(|arn:aws:lambda:.*)$`
- **Kind:** Resource. Grants the meeting event processor permission to invoke the named function.

(Optional) If present, the specified Lambda function is invoked by the LMA Call Event Processor Lambda function for each completed (non-partial) transcript segment. The function can capture and/or modify the text of the transcript, for example to implement custom redaction logic, profanity filtering, or custom rules to highlight patterns in the transcript.

### `TranscriptLambdaHookFunctionNonPartialOnly`

Console label: *Lambda Hook Function Mode Non-Partial only*

- **Default:** `true`
- **Allowed values:** `true`, `false`
- **Kind:** Configuration page default. Configuration page: **Meeting Processing › Send only finished segments to the transcript Lambda hook**. Read by the meeting event processor.

Specifies if Transcript Lambda Hook Function (if specified) is invoked for Non-Partial transcript segments only (true), or for both Partial and Non-Partial transcript segments (false). This is the default; an Admin can change it at run time on the Configuration page (Meeting Processing section), which takes precedence.

### `EndOfCallTranscriptSummary`

Console label: *End of Call Transcript Summary*

- **Default:** `BEDROCK`
- **Allowed values:** `BEDROCK`, `LAMBDA`
- **Kind:** Resource. Decides whether the Bedrock summarizer exists or a Lambda hook is invoked instead.

Set to enable call summarization by a Large Language Model. Currently only BEDROCK and LAMBDA are supported. BEDROCK option requires you to choose one of the supported model IDs from the provided list (BedrockModelId). The LAMBDA option requires you to provide a function ARN below.

### `AgendaDerivationAfterSeconds`

Console label: *Seconds into a Meeting to Look for a Stated Agenda*

- **Default:** `300`
- **Allowed values:** 0 to 3600
- **Kind:** Configuration page default. Configuration page: **Meeting Agenda › Seconds into a meeting to look for a stated agenda**. Read by the meeting event processor, for each meeting with no agenda.

Seconds into a meeting with no agenda at which its transcript is read once for an agenda somebody stated, and saved to the meeting if one is found. 0 turns the automatic pass off; the Agenda panel's Derive agenda button works either way. This is the default; an Admin can change it at run time on the Configuration page (Meeting Agenda section), which takes precedence.

### `BedrockModelId`

Console label: *Bedrock Model ID for Summaries and Meeting Insights*

- **Default:** `global.anthropic.claude-sonnet-5-5`
- **Allowed values:** `amazon.nova-micro-v1:0`, `amazon.nova-lite-v1:0`, `amazon.nova-pro-v1:0`, `us.amazon.nova-premier-v1:0`, `global.amazon.nova-2-lite-v1:0`, `us.amazon.nova-2-lite-v1:0`, `us.anthropic.claude-opus-4-20250514-v1:0`, `us.anthropic.claude-opus-4-1-20250805-v1:0`, `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `us.anthropic.claude-sonnet-4-6`, `us.anthropic.claude-opus-4-6-v1`, `us.anthropic.claude-haiku-5-5`, `us.anthropic.claude-sonnet-5-5`, `us.anthropic.claude-opus-5-5`, `global.anthropic.claude-haiku-4-5-20251001-v1:0`, `global.anthropic.claude-sonnet-4-5-20250929-v1:0`, `global.anthropic.claude-sonnet-4-6`, `global.anthropic.claude-opus-4-6-v1`, `global.anthropic.claude-haiku-5-5`, `global.anthropic.claude-sonnet-5-5`, `global.anthropic.claude-opus-5-5`
- **Kind:** Configuration page default. Configuration page: **Amazon Bedrock Models › Summaries and meeting insights**. Read by the summarizer and the functions that parse invitations, name speakers, derive agendas, answer transcript knowledge-base queries and analyse MCP usage. Their Amazon Bedrock permissions cover every model, so the choice changes no permission.

If EndOfCallTranscriptSummary is BEDROCK, then choose a model ID from the list of supported models. Includes Nova models (cost-effective) and Claude 4+ (cutting-edge). Some models require inference profiles. The Claude 5 generation reasons before it answers, which raises summary quality but also raises output token use several-fold, so it costs more per meeting than Claude 4.5 at the same size. This is the default; an Admin can change it at run time on the Configuration page (Amazon Bedrock Models section), which takes precedence.

### `KeyframeTextModelId`

Console label: *Bedrock Model ID for Reading Key Frames of the Shared Screen*

- **Default:** `amazon.nova-lite-v1:0`
- **Allowed values:** `amazon.nova-lite-v1:0`, `amazon.nova-pro-v1:0`, `us.amazon.nova-premier-v1:0`, `global.amazon.nova-2-lite-v1:0`, `us.amazon.nova-2-lite-v1:0`, `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, `us.anthropic.claude-sonnet-4-6`, `us.anthropic.claude-haiku-5-5`, `us.anthropic.claude-sonnet-5-5`, `global.anthropic.claude-haiku-4-5-20251001-v1:0`, `global.anthropic.claude-sonnet-4-5-20250929-v1:0`, `global.anthropic.claude-sonnet-4-6`, `global.anthropic.claude-haiku-5-5`, `global.anthropic.claude-sonnet-5-5`
- **Kind:** Configuration page default. Configuration page: **Amazon Bedrock Models › Key frame text**. Read by the function that reads the text of each key frame.

Bedrock model that reads the text of each key frame of the shared screen, once, when the frame is stored (docs/meeting-video.md). The text is what the meeting assistant reads first, what the meeting page searches, and what the meeting-history knowledge base indexes. Amazon Nova Lite read every word of the measured frames at about $0.0002 a frame. This is the default; an Admin can change it at run time on the Configuration page (Amazon Bedrock Models section), which takes precedence.

### `EndOfCallLambdaHookFunctionArn`

Console label: *Lambda Hook Function ARN for Custom End of Call Processing (existing)*

- **Default:** (empty)
- **Allowed values:** Matches `^(|arn:aws:lambda:.*)$`
- **Kind:** Resource. Grants the summary functions permission to invoke the named function.

(Optional) If 'End Of Call Transcript Summary' is LAMBDA, provide ARN for a Lambda function. The specified Lambda function is invoked by the LMA Call Event Processor Lambda function for end of call event. The function is passed en event with CallId as input. This function can implement custom logic that is relevant to end of call processing, for example, creating a call summary.

### `StartOfCallLambdaHookFunctionArn`

Console label: *Lambda Hook Function ARN for Custom Start of Call Processing (existing)*

- **Default:** (empty)
- **Allowed values:** Matches `^(|arn:aws:lambda:.*)$`
- **Kind:** Resource. Grants the meeting event processor permission to invoke the named function.

(Optional) The specified Lambda function is invoked by the LMA Call Event Processor Lambda function for beginning or start of call event. This function can implement custom logic that is relevant to beginning of call processing, for example, retrieving call summary details logged into a case in a CRM.

### `PostCallSummaryLambdaHookFunctionArn`

Console label: *Lambda Hook Function ARN for Custom Post Processing, after the Call Transcript Summary is processed (existing)*

- **Default:** (empty)
- **Allowed values:** Matches `^(|arn:aws:lambda:.*)$`
- **Kind:** Resource. Grants the meeting event processor permission to invoke the named function.

(Optional) The specified Lambda function is invoked by the LMA Call Event Processor Lambda function after the call summary is processed. This function can implement custom logic that is relevant to post processing, for example, updating the call summary to a CRM system.

## Amazon CloudFront Configuration

| Parameter | Default | Kind |
|---|---|---|
| [`CloudFrontPriceClass`](#cloudfrontpriceclass) | `PriceClass_100` | Resource |
| [`CloudFrontAllowedGeos`](#cloudfrontallowedgeos) | (empty) | Resource |

### `CloudFrontPriceClass`

Console label: *CloudFront Price Class*

- **Default:** `PriceClass_100`
- **Allowed values:** `PriceClass_100`, `PriceClass_200`, `PriceClass_All`
- **Kind:** Resource. Sets the price class of the web app's and the WebSocket transcriber's CloudFront distributions.

Specify the CloudFront price class. See https://aws.amazon.com/cloudfront/pricing/ for a description of each price class.

### `CloudFrontAllowedGeos`

Console label: *CloudFront Allowed Geographies*

- **Default:** (empty)
- **Allowed values:** Comma separated list of uppercase two letter country codes or empty
- **Kind:** Resource. Sets the geographic restriction of the web app's CloudFront distribution.

Specify a comma separated list of two letter country codes (uppercase ISO 3166-1) that are allowed to access the web user interface via CloudFront. For example: US,CA. Leave empty if you do not want geo restrictions to be applied.

## Retention Settings

| Parameter | Default | Kind |
|---|---|---|
| [`EnableAudioRecording`](#enableaudiorecording) | `true` | Configuration page default |
| [`EnableVideoRecording`](#enablevideorecording) | `true` | Resource |
| [`MeetingRecordExpirationInDays`](#meetingrecordexpirationindays) | `90` | Configuration page limit |
| [`TranscriptionExpirationInDays`](#transcriptionexpirationindays) | `90` | Resource |
| [`AudioRecordingExpirationInDays`](#audiorecordingexpirationindays) | `90` | Resource |
| [`CloudWatchLogsExpirationInDays`](#cloudwatchlogsexpirationindays) | `90` | Resource |
| [`VPSchedulerStateMachineLogsExpirationInDays`](#vpschedulerstatemachinelogsexpirationindays) | `7` | Resource |
| [`MeetingInactivityTimeoutInMinutes`](#meetinginactivitytimeoutinminutes) | `240` | Resource |
| [`MeetingInactivityLookbackInDays`](#meetinginactivitylookbackindays) | `2` | Configuration page default |
| [`EnableDataRetentionOnDelete`](#enabledataretentionondelete) | `true` | Resource |

### `EnableAudioRecording`

Console label: *Record Meeting Audio*

- **Default:** `true`
- **Allowed values:** `true`, `false`
- **Kind:** Configuration page default. Configuration page: **Meeting Recording › Record meeting audio**. Read by the WebSocket transcriber as the default for each meeting, and by the Virtual Participant when it starts.

Enable or disable recording of audio during a meeting. If set to false, the meeting will not be recorded. If set to true, the meeting will be recorded. This is the default; an Admin can change it at run time on the Configuration page (Meeting Recording section), which takes precedence.

### `EnableVideoRecording`

Console label: *Record Meeting Video*

- **Default:** `true`
- **Allowed values:** `true`, `false`
- **Kind:** Resource. Sizes the transcriber's task for video, and limits the transcriber to one task through a template rule.

Enables meeting video (screen recording) in addition to audio, for BOTH video sources: the Virtual Participant and the Desktop Capture App. For the Virtual Participant this is the deployment-wide default and users can override it per meeting with the "Record meeting video" toggle in the LMA UI; for the Desktop Capture App it is a hard on/off (users opt in per machine in the app's Settings, and the server discards video when this is false). Video recordings consume additional CPU, ephemeral storage, and S3 storage. Leaving this at "true" also pins the WebSocket transcriber service to a single task and forbids transcriber auto scaling, because Desktop Capture App video arrives on a separate connection that must reach the task holding that meeting's audio session; TranscriberDesiredTaskCount above 1 and TranscriberAutoScalingEnabled are rejected while this is "true". Only the Desktop Capture App imposes that constraint — Virtual Participant video does not — so scaling the transcriber means giving up desktop video recording, not video recording in principle.

### `MeetingRecordExpirationInDays`

Console label: *Meeting Record Expiration In Days*

- **Default:** `90`
- **Allowed values:** Any number
- **Kind:** Configuration page limit. Configuration page: **Retention › Days to keep meeting records**. Read by every function and task that writes meeting, summary or usage records, to set when each record expires.

Number of days to retain meeting records. Meetings will be automatically deleted from LMA after this time. Specify partial days like 0.5 to retain meeting records for half a day. This is the default and the upper limit; an Admin can set a shorter period at run time on the Configuration page (Retention section), never a longer one.

### `TranscriptionExpirationInDays`

Console label: *Transcription Expiration In Days*

- **Default:** `90`
- **Allowed values:** Any number
- **Kind:** Resource. Sets the lifecycle rules for transcript files and key-frame images on the recordings bucket, and is read by the functions that write transcript segments, to set when each expires. Also the default and upper limit of the Configuration page's **Retention › Days to keep transcript segments**.

Number of days to retain turn by turn meeting transcriptions. Transcriptions will be automatically deleted from LMA after this time. Specify partial days like 0.5 to retain transcriptions for half a day. Also sets the lifecycle rules for transcript files and key-frame images in the recordings bucket, which the Configuration page does not change. This is the default and the upper limit; an Admin can set a shorter period at run time on the Configuration page (Retention section), never a longer one.

### `AudioRecordingExpirationInDays`

Console label: *Audio Recording Expiration In Days*

- **Default:** `90`
- **Allowed values:** Any number
- **Kind:** Resource. Sets the lifecycle rules for audio and video recordings on the recordings bucket.

Whole number of days after which audio recordings will be deleted from the Recordings bucket.

### `CloudWatchLogsExpirationInDays`

Console label: *CloudWatch Logs Expiration In Days*

- **Default:** `90`
- **Allowed values:** `1`, `3`, `5`, `7`, `14`, `30`, `60`, `90`, `120`, `150`, `180`, `365`, `400`, `545`, `731`, `1096`, `1827`, `2192`, `2557`, `2922`, `3288`, `3653`
- **Kind:** Resource. Sets the retention of every log group the stack declares.

Number of days to retain CloudWatch log records. Logs will be automatically deleted after this time.

### `VPSchedulerStateMachineLogsExpirationInDays`

Console label: *Virtual Participant Scheduler State Machine Logs Expiration In Days*

- **Default:** `7`
- **Allowed values:** `1`, `3`, `5`, `7`, `14`, `30`, `60`, `90`, `120`, `150`, `180`, `365`, `400`, `545`, `731`, `1096`, `1827`, `2192`, `2557`, `2922`, `3288`, `3653`
- **Kind:** Resource. Sets the retention of the scheduled Virtual Participant state machine's log group.

Number of days to retain the scheduled Virtual Participant state machine's execution history logs. Shorter than CloudWatchLogsExpirationInDays by default because the state machine logs every state transition of every scheduled Virtual Participant. Raise it while investigating a scheduled Virtual Participant that did not start.

### `MeetingInactivityTimeoutInMinutes`

Console label: *Meeting Inactivity Timeout In Minutes*

- **Default:** `240`
- **Allowed values:** 0 to 10080
- **Kind:** Resource. Decides whether the inactive-meeting reaper and its schedule exist, and is read by the reaper. Also the default of the Configuration page's **Meeting Processing › Minutes without speech before a meeting is ended**.

Number of minutes a meeting may go without producing a finalized transcript segment before LMA ends it on the meeting's behalf. A meeting whose client disconnects without sending its end-of-meeting event stays "In Progress" until this timeout elapses. A meeting that is still connected but silent for longer than the timeout is also ended, so keep it comfortably longer than the longest quiet stretch your meetings have. Set to 0 to leave such meetings open; the reaper that ends them is then not deployed, and the Configuration page's timeout setting has no effect. This is the default; an Admin can change it at run time on the Configuration page (Meeting Processing section), which takes precedence while the stack parameter is above 0.

### `MeetingInactivityLookbackInDays`

Console label: *Meeting Inactivity Lookback In Days*

- **Default:** `2`
- **Allowed values:** 1 to 90
- **Kind:** Configuration page default. Configuration page: **Meeting Processing › Days to look back for meetings that were never ended**. Read by the inactive-meeting reaper.

How far back to look for meetings to close. A meeting that started longer ago than this is never closed automatically, so raise it temporarily if you are updating a deployment that has accumulated older meetings stuck in progress. Higher values read more meeting records on every run. A whole number of days: a fractional value is not used, and the reaper looks back 2 days instead. This is the default; an Admin can change it at run time on the Configuration page (Meeting Processing section), which takes precedence.

### `EnableDataRetentionOnDelete`

Console label: *Enable Data Retention On Stack Deletion*

- **Default:** `true`
- **Allowed values:** `true`, `false`
- **Kind:** Resource. Sets the deletion policy of the stack's tables, buckets, user pool and keys.

Enable data retention on stack deletion for compliance requirements. When true (default): DynamoDB tables, S3 buckets (recordings and logs), and encryption key will be retained. When false: All resources deleted for clean stack removal. Enterprise customers with compliance requirements should set to true.

## AppSync Configuration

| Parameter | Default | Kind |
|---|---|---|
| [`EnableAppSyncApiCache`](#enableappsyncapicache) | `false` | Resource |
| [`AppSyncApiCacheInstanceType`](#appsyncapicacheinstancetype) | `SMALL` | Resource |

### `EnableAppSyncApiCache`

Console label: *Enable AppSync Api Cache*

- **Default:** `false`
- **Allowed values:** `true`, `false`
- **Kind:** Resource. Decides whether the AppSync API cache exists.

Enable or disable the AppSync Api Cache.

### `AppSyncApiCacheInstanceType`

Console label: *AppSync Api Cache Instance Type*

- **Default:** `SMALL`
- **Allowed values:** `SMALL`, `MEDIUM`, `LARGE`, `XLARGE`
- **Kind:** Resource. Sets the instance type of the AppSync API cache.

If you set the EnableAppSyncApiCache is true, choose a value that meets your workload requirements. This parameter is ignored if the EnableAppSyncApiCache parameter is set to false (default behavior).

## VPC Configuration

| Parameter | Default | Kind |
|---|---|---|
| [`UseExistingVPC`](#useexistingvpc) | `false` | Resource |
| [`VPC`](#vpc) | (empty) | Resource |
| [`PublicSubnet1`](#publicsubnet1) | (empty) | Resource |
| [`PublicSubnet2`](#publicsubnet2) | (empty) | Resource |
| [`PrivateSubnet1`](#privatesubnet1) | (empty) | Resource |
| [`PrivateSubnet2`](#privatesubnet2) | (empty) | Resource |

### `UseExistingVPC`

Console label: *Use Existing VPC*

- **Default:** `false`
- **Allowed values:** `true`, `false`
- **Kind:** Resource. Decides whether the stack creates its own VPC.

Use an existing VPC for the solution instead of creating a new one. If set to true, the VPC ID and subnets must be provided. DO NOT CHANGE VALUE WHEN UPDATING AN EXISTING STACK.

### `VPC`

Console label: *Existing VPC ID*

- **Default:** (empty)
- **Allowed values:** Matches `^(|vpc-[a-zA-Z0-9-]+)$`
- **Kind:** Resource. The VPC the load balancers, security groups and tasks are placed in.

ID of the VPC to use for the solution. Required if UseExistingVPC is true. DO NOT CHANGE VALUE WHEN UPDATING AN EXISTING STACK.

### `PublicSubnet1`

Console label: *Existing Public Subnet 1*

- **Default:** (empty)
- **Allowed values:** Matches `^(|subnet-[a-zA-Z0-9-]+)$`
- **Kind:** Resource. A subnet the load balancers are placed in.

ID of the public subnet in Availability Zone 1. Required if UseExistingVPC is true. DO NOT CHANGE VALUE WHEN UPDATING AN EXISTING STACK.

### `PublicSubnet2`

Console label: *Existing Public Subnet 2*

- **Default:** (empty)
- **Allowed values:** Matches `^(|subnet-[a-zA-Z0-9-]+)$`
- **Kind:** Resource. A subnet the load balancers are placed in.

ID of the public subnet in Availability Zone 2. Required if UseExistingVPC is true. DO NOT CHANGE VALUE WHEN UPDATING AN EXISTING STACK.

### `PrivateSubnet1`

Console label: *Existing Private Subnet 1*

- **Default:** (empty)
- **Allowed values:** Matches `^(|subnet-[a-zA-Z0-9-]+)$`
- **Kind:** Resource. A subnet the transcriber and Virtual Participant tasks are placed in.

ID of the private subnet in Availability Zone 1. Required if UseExistingVPC is true. DO NOT CHANGE VALUE WHEN UPDATING AN EXISTING STACK.

### `PrivateSubnet2`

Console label: *Existing Private Subnet 2*

- **Default:** (empty)
- **Allowed values:** Matches `^(|subnet-[a-zA-Z0-9-]+)$`
- **Kind:** Resource. A subnet the transcriber and Virtual Participant tasks are placed in.

ID of the private subnet in Availability Zone 2. Required if UseExistingVPC is true. DO NOT CHANGE VALUE WHEN UPDATING AN EXISTING STACK.

## Operational Monitoring

| Parameter | Default | Kind |
|---|---|---|
| [`EnableOperationalAlarms`](#enableoperationalalarms) | `true` | Resource |
| [`TranscribeConcurrentStreamAlarmThreshold`](#transcribeconcurrentstreamalarmthreshold) | `20` | Resource |
| [`LogLevel`](#loglevel) | `INFO` | Read at run time |

### `EnableOperationalAlarms`

Console label: *Create Operational CloudWatch Alarms*

- **Default:** `true`
- **Allowed values:** `true`, `false`
- **Kind:** Resource. Decides whether the operational alarms exist.

Create the CloudWatch alarms that watch the transcription pipeline and notify the operational alerts SNS topic. Set to 'false' if you forward these signals from somewhere else, or if your organisation manages alarms outside the application stack. The operational dashboard and the alerts topic are created either way, so turning this off removes the notifications rather than the visibility. See docs/monitoring.md.

### `TranscribeConcurrentStreamAlarmThreshold`

Console label: *Transcribe Concurrent Stream Alarm Threshold*

- **Default:** `20`
- **Allowed values:** at least 1
- **Kind:** Resource. Sets the threshold of the concurrent-stream alarm and the dashboard line drawn at it.

Number of concurrent Amazon Transcribe streaming sessions at which the 'transcribe-concurrent-streams-near-quota' alarm notifies. The default of 20 is 80% of the default account quota of 25 concurrent streams, which leaves headroom to request an increase before meetings start being refused. If you raise the quota, raise this with it — the alarm has no way to read your account's current quota, so left at 20 it keeps notifying at a level that is no longer close to the limit.

### `LogLevel`

Console label: *Log Level*

- **Default:** `INFO`
- **Allowed values:** `DEBUG`, `INFO`, `WARN`, `ERROR`
- **Kind:** Read at run time. Read by each function and task's logging when the process starts, so a change applies as processes are replaced.

Default logging level for all Lambda functions. Set to DEBUG for troubleshooting, INFO for normal operation.

## Security Configuration

| Parameter | Default | Kind |
|---|---|---|
| [`PermissionsBoundaryArn`](#permissionsboundaryarn) | (empty) | Resource |

### `PermissionsBoundaryArn`

Console label: *Permissions Boundary ARN*

- **Default:** (empty)
- **Allowed values:** Any text
- **Kind:** Resource. Attached as the permissions boundary of every IAM role the stack creates.

(Optional) ARN of an existing IAM managed policy to use as permissions boundary for IAM roles. If provided, this policy will limit the maximum permissions that can be granted to IAM roles in this stack, providing an additional layer of security.

## Workshop options

| Parameter | Default | Kind |
|---|---|---|
| [`ModelValidation`](#modelvalidation) | `true` | Deployment check |

### `ModelValidation`

Console label: *Amazon Bedrock Model Validation*

- **Default:** `true`
- **Allowed values:** `true`, `false`
- **Kind:** Deployment check. Decides whether the deployment checks that the chosen Amazon Bedrock models can be invoked.

Specifies if Amazon Bedrock model access is validated during deployment. Setting this parameter to "false" is useful for automated deployments (e.g. AWS Workshop Studio) where Amazon Bedrock model access permissions may not be set up yet.

<!-- END GENERATED PARAMETER REFERENCE -->

## Notes on specific parameters

### Choosing Amazon Bedrock models

`BedrockModelId` (summaries, pasted meeting invitations, the speaker-naming pass,
agenda derivation and the transcript knowledge-base query) and
`MeetingAssistServiceBedrockModelID` (the meeting assistant and the
troubleshooting agent) offer the same list but have different defaults, because
they are chosen for different jobs. `BedrockModelId` defaults to
`global.anthropic.claude-sonnet-5-5`: summarisation is batch work where the
quality of the answer is what matters. `MeetingAssistServiceBedrockModelID`
defaults to `global.anthropic.claude-haiku-4-5-20251001-v1:0`, because the
assistant answers mid-meeting and is sometimes spoken aloud, so its time to first
token matters most; measured against a real transcript it begins replying in about
0.9 s, against 1.2 s for Sonnet 5.5 and 2.7 s for Haiku 5.5, both of which reason
before answering.

Summaries cost more on Sonnet 5.5 than on Haiku 4.5 — roughly 27% more input
tokens, and two to three times the output tokens, for a noticeably longer
summary. The Claude 5 generation reasons before it answers and the reasoning is
billed as output, so one summary of the same meeting consumes roughly three to
four times the output tokens it does on Claude 4.5 at the same size, and its
tokenizer counts a given transcript as roughly a quarter more input tokens. If
cost matters more than the last increment of quality, use
`global.anthropic.claude-haiku-4-5-20251001-v1:0`.

`MeetingAssistKnowledgeBaseModelID` offers a shorter list. It names the model the
meeting assistant's knowledge bases generate an answer with, and a Bedrock
Knowledge Base can be driven with fewer models than the agent itself runs on —
per individual model, not by family or version — so the list is a record of what
has been verified rather than a rule. See
[The knowledge-base model](meeting-assistant.md#the-knowledge-base-model).

`KeyframeTextModelId` offers only image-capable models, because the model reads a
picture of the shared screen. See [Meeting Video and Key Frames](meeting-video.md).

A `global.` profile routes to whichever region has capacity; a `us.` profile keeps
inference within the United States. Pick the one your data-residency position
allows. Whichever you choose, enable access to that model in the Amazon Bedrock
console for the region you deploy into first — see
[Amazon Bedrock Model Access](prerequisites-and-deployment.md#amazon-bedrock-model-access).
`ModelValidation` checks that access while the stack deploys.

Claude 3.x models were withdrawn from these lists in v0.3.0. A stack still
configured with one cannot be updated until the parameter is changed to a
supported value — see [Version Migration Notes](stack-updates-and-upgrades.md#v030).

### Scaling the WebSocket transcriber

The five `Transcriber*` parameters size the Fargate service that terminates the
browser and desktop-app WebSocket connections (the Stream Audio tab, the Desktop
Capture App and the browser extension). They do not affect the Virtual
Participant.

Running more than one task requires `EnableVideoRecording` to be `false`, and the
stack rejects the combination at deploy time rather than failing per meeting.
With video recording enabled, the desktop capture app sends meeting video over a
second WebSocket connection, and only the task already hosting that meeting's
audio session can attach it. The load balancer in front of the service does not
pin a client to a task, and cookie-based stickiness is not available because the
CloudFront distribution in front of it forwards no cookies, so with several tasks
the video connection is refused whenever it lands on a different task from the
audio. `EnableVideoRecording` also governs Virtual Participant video, which has no
such constraint, so scaling the transcriber means giving up *desktop app* video
recording specifically. Audio is unaffected either way: a meeting's audio uses one
connection for its whole lifetime.

Auto scaling adds capacity but never removes it. Taking a task away would end the
meetings it is hosting once the load balancer's deregistration delay elapses, and
a meeting can run for hours. So capacity ratchets up and stays there: while
`TranscriberAutoScalingEnabled` is `true`, Application Auto Scaling owns the task
count and a stack update does not reset it to `TranscriberDesiredTaskCount`. To
bring it back down, either set `TranscriberAutoScalingEnabled` to `false` — which
hands the count back to CloudFormation and applies `TranscriberDesiredTaskCount` —
or reduce it directly with `aws ecs update-service --desired-count` or
`aws application-autoscaling register-scalable-target`. Budget for the high water
mark, not the average. The CPU target is kept below the load at which a task
reports itself unhealthy to the load balancer, so that scaling out happens before
tasks start being replaced.

A rolling deployment briefly runs up to twice the task count, which is the one
window where more than one task exists even with video recording enabled. That is
harmless in practice: the draining task keeps serving the sessions already
attached to it and the new task only receives new meetings, so no meeting is ever
split across the two.

### The on-demand speech engine

> **EXPERIMENTAL — not production ready.** Transcript quality is below Amazon
> Transcribe's and defaults may change between releases. Amazon Transcribe remains
> the recommended engine for production meetings.

`EnableMicrovmAsr` is the only deploy-time question for this engine. A meeting it
transcribes does not go through Amazon Transcribe, so the redaction, custom
vocabulary, custom language model and language identification parameters do not
apply to it. Two more values are fixed in the `AsrDefaults` mapping in
`lma-main.yaml` rather than asked: `ModelBundle`, which models the MicroVM image is
built from (`parakeet-tdt-v3-live-nemotron35-sortformer` by default), and
`MaxMeetingSeconds` (`14400`), the hard lifetime ceiling per MicroVM. Which
meetings use the engine is chosen in the Transcription Engine section of the
Configuration page, with no stack update. The bundles, their licences and how one
is selected are described in [On-demand ASR & Speaker Diarization](microvm-asr.md).

### Voice enrollment

> [!IMPORTANT]
> Off by default, and turning it on means storing biometric data about people.
> Read [Voice Enrollment](voice-enrollment.md) before enabling it.

While `EnableVoiceEnrollment` is `false` the tables, the consent portal and the
matching step do not exist in the deployment at all. Both enrolling and matching
need `EnableMicrovmAsr=true`, because that engine is what computes speaker
embeddings; with enrollment on and that engine off, the tables, the portal and the
consent log all exist and work, and the step that would create a voiceprint
reports that the engine is missing.

`VoiceEnrollmentRetentionDays` and `VoiceEnrollmentTermsVersion` are read only by
application code, but they are stack parameters on purpose and are not on the
Configuration page. The retention period is stated in the consent terms, and each
enrollment records the terms it agreed to, so changing either value asks everybody
to consent again. That should be a deliberate deployment, not a save in the web UI.

### Virtual Participant capacity and meeting-end detection

`VPInstanceType`, `VPMinInstances` and `VPMaxInstances` apply only to
`VPLaunchType=EC2`. Under `MICROVM`, the default, there are no hosts to size or
scale: each meeting gets its own MicroVM, billed for its lifetime. See
[MicroVM launch type](virtual-participant.md#microvm-launch-type-default).

`VPAttendeePollMs`, `VPPollsBeforeEnd` and `VPPollsBeforeEndMissing` apply to the
Teams browser join path only, and no deployment should normally need to change
them — see
[How the VP decides a meeting has ended](virtual-participant.md#how-the-vp-decides-a-meeting-has-ended)
for what each signal measures and why the two thresholds differ so much.

### Reaching Amazon Nova Sonic in another region

Setting `AmazonNovaSonicRegion` also grants the Virtual Participant permission to
invoke the Nova model in that region. Only the Nova Sonic model moves: the
Virtual Participant's other Bedrock calls, the configuration it reads and the
meeting-assistant function the voice assistant calls as a tool all stay in the
stack's region. Nothing checks that Nova Sonic is available in the region you
name — check the
[Amazon Bedrock model support by region](https://docs.aws.amazon.com/bedrock/latest/userguide/models-regions.html)
table first. A region where the model is not enabled fails at the first voice
interaction, with a Bedrock validation error in the Virtual Participant's logs,
not at deploy time.

### Closing meetings that were never ended

`MeetingInactivityTimeoutInMinutes` backs the scheduled reaper that ends meetings
whose client disconnected without sending its end-of-meeting event; otherwise
those meetings show as "In Progress" for as long as their record is retained. The
reaper runs every 15 minutes, so a meeting can remain in progress for the timeout
plus one interval. Its liveness signal is the meeting record's last update, which
a transcript segment refreshes, so a meeting that is still connected but has
produced no finalized speech for longer than the timeout is also ended; keep the
value comfortably longer than the longest quiet stretch your meetings have.
Meetings still being processed by the upload pipeline are left to the upload
finalizer. The parameter decides whether the reaper exists; while it does, the
timeout itself can be changed in the Meeting Processing section of the
Configuration page, with the parameter as its default.

`MeetingInactivityLookbackInDays` bounds how much history each run examines. A
meeting that started before the window is never closed automatically, so if you
are updating a deployment that has accumulated older meetings stuck in progress,
raise it once on the Configuration page (Meeting Processing section), let a run
complete, and put it back; no stack update is needed. Each run also stops after
reading 500 candidate meetings, newest first. See
[Troubleshooting → Meeting Stuck In Progress](troubleshooting.md#meeting-stuck-in-progress).

### Monitoring

`EnableOperationalAlarms` creates the CloudWatch alarms that notify the
operational alerts SNS topic; the topic and the dashboard are created either way,
so `false` removes the notifications rather than the visibility. The default
`TranscribeConcurrentStreamAlarmThreshold` is 80% of the default account quota of
25 concurrent streams; raise it if you raise the quota, because the alarm cannot
read it. See [Monitoring & Alerting](monitoring.md).

## Related Documentation

- [Configuration Guide](configuration-guide.md) — the settings an Admin changes on
  the Configuration page
- [Prerequisites & Deployment](prerequisites-and-deployment.md)
- [Stack Updates & Upgrades](stack-updates-and-upgrades.md)
- [Integration Credentials](infrastructure-and-security.md#integration-credentials)
