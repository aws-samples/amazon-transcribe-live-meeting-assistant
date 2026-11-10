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

The template declares 68 parameters.

| Kind | Parameters | What changing one does |
|---|---|---|
| Resource | 49 | Decides which resources the stack creates or how they are built: whether a nested stack or resource exists, an IAM permission, networking, capacity, a retention rule or an alarm. Changing it is a stack update that changes resources. |
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

## Meeting Assist Options

| Parameter | Default | Kind |
|---|---|---|
| [`MeetingAssistService`](#meetingassistservice) | `STRANDS_BEDROCK` | Resource |
| [`TavilyApiKey`](#tavilyapikey) | (empty) | Credential |
| [`TavilyApiKeySecretArn`](#tavilyapikeysecretarn) | (empty) | Credential |

### `MeetingAssistService`

Console label: *Meeting Assist Service*

- **Default:** `STRANDS_BEDROCK`
- **Allowed values:** `STRANDS_BEDROCK`, `STRANDS_BEDROCK_WITH_KB (Create)`, `STRANDS_BEDROCK_WITH_KB (Use Existing)`
- **Kind:** Resource. Decides whether the meeting assistant's knowledge base is created, used from an existing one, or absent.

Choose the meeting assistant service. STRANDS_BEDROCK provides agentic capabilities with built-in tools. STRANDS_BEDROCK_WITH_KB (Create) automatically creates a Bedrock Knowledge Base for document search. STRANDS_BEDROCK_WITH_KB (Use Existing) uses an existing Bedrock Knowledge Base you provide.

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

## Meeting Assist Voice Assistant

| Parameter | Default | Kind |
|---|---|---|
| [`ElevenLabsApiKey`](#elevenlabsapikey) | (empty) | Credential |
| [`ElevenLabsApiKeySecretArn`](#elevenlabsapikeysecretarn) | (empty) | Credential |
| [`SimliApiKey`](#simliapikey) | (empty) | Credential |
| [`SimliApiKeySecretArn`](#simliapikeysecretarn) | (empty) | Credential |
| [`AmazonNovaSonicRegion`](#amazonnovasonicregion) | (empty) | Resource |
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

### `SimliApiKey`

Console label: *Simli API Key for Avatar (Optional)*

- **Default:** (empty)
- **Allowed values:** Any text
- **Kind:** Credential. Stored in a Secrets Manager secret the Virtual Participant is given.

(Optional) API key for Simli avatar. The avatar feature requires BOTH this key and the Simli face ID on the Configuration page (Virtual Participant section) — leave both blank to disable the avatar, or set both to enable an animated lip-synced avatar as the Virtual Participant's camera feed. Get your API key from https://simli.com. Leave blank to disable avatar.

### `SimliApiKeySecretArn`

Console label: *Existing Secret ARN Holding the Simli API Key (Optional)*

- **Default:** (empty)
- **Allowed values:** Must be empty, or the ARN of a Secrets Manager secret (arn:aws:secretsmanager:&lt;region&gt;:&lt;account&gt;:secret:&lt;name&gt;).
- **Kind:** Credential. Grants the Virtual Participant read access to the named secret.

(Optional) ARN of an existing Secrets Manager secret whose value is the Simli API key. Use this instead of SimliApiKey when the key is already provisioned in Secrets Manager. Supply one form or the other, not both. The Simli face ID on the Configuration page (Virtual Participant section) is still required alongside it. A secret encrypted with your own KMS key must grant kms:Decrypt in that key's policy to whichever role reads it: the MicroVM launcher function's role under VPLaunchType MICROVM (the default), or the Virtual Participant task execution role on FARGATE and EC2. See docs/infrastructure-and-security.md#bringing-your-own-secret.

### `AmazonNovaSonicRegion`

Console label: *Amazon Nova Sonic Region (blank = this stack's region)*

- **Default:** (empty)
- **Allowed values:** Must be empty, or an AWS Region name such as us-east-1.
- **Kind:** Resource. Names the region in the Virtual Participant's permission to invoke Amazon Nova Sonic.

Region to reach Amazon Nova Sonic in, when it differs from the region this stack is deployed to. Leave empty to use the stack's own region. Nova Sonic is available in fewer regions than LMA itself, so a deployment constrained to a particular region for compliance can keep everything else local and reach the voice assistant elsewhere. Only used when the voice assistant on the Configuration page (Virtual Participant section) is Amazon Nova Sonic.

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
| [`TranscribeContentRedactionType`](#transcribecontentredactiontype) | `PII` | Read at run time |

### `IsPartialTranscriptEnabled`

Console label: *Enable Partial Transcripts*

- **Default:** `true`
- **Allowed values:** `true`, `false`
- **Kind:** No effect. Partial transcripts are always processed.

Enable partial transcripts to receive low latency evolving transcriptions for each conversation turn. Set to false to process only the final version of each conversation turn.

### `TranscribeContentRedactionType`

Console label: *Content Redaction Type for Transcription*

- **Default:** `PII`
- **Allowed values:** `PII`
- **Kind:** Read at run time. Read by the WebSocket transcriber for each streaming session, and by the Virtual Participant when it starts.

Type of content redaction from Amazon Transcribe transcription output.

## ASR MicroVM speech engine (default transcription engine)

| Parameter | Default | Kind |
|---|---|---|
| [`EnableMicrovmAsr`](#enablemicrovmasr) | `true` | Resource |

### `EnableMicrovmAsr`

Console label: *Deploy the ASR MicroVM speech engine*

- **Default:** `true`
- **Allowed values:** `true`, `false`
- **Kind:** Resource. Decides whether the ASR MicroVM engine's nested stack exists, in a Region with AWS Lambda MicroVMs.

Deploys the ASR MicroVM speech engine (open-source models on AWS Lambda MicroVMs in this account), the default transcription engine for Stream Audio, Chrome extension, Desktop Capture and Virtual Participant meetings; it also provides the speaker embeddings voice enrollment needs. Deployed only in Regions with AWS Lambda MicroVMs (us-east-1, us-east-2, us-west-2, ap-northeast-1, eu-west-1 -- not GovCloud); elsewhere this setting has no effect and every meeting uses Amazon Transcribe. Set false to use Amazon Transcribe for every meeting. Each source's engine can be changed in the Transcription Engine section of the Configuration page. See docs/microvm-asr.md.

## Voice enrollment (Beta; biometric; off by default)

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

Let people enroll their voice so they are labelled by name in meetings from their first word. A voiceprint is biometric data about a person: while this is false the enrollment table, the consent log and the consent portal do not exist in the deployment at all. Turning it on creates obligations that are yours -- biometric data is separately regulated in many jurisdictions and typically requires informed consent before collection, a published retention schedule and deletion on request. See docs/voice-enrollment.md. Matching also needs the ASR MicroVM engine (EnableMicrovmAsr, deployed in Regions with AWS Lambda MicroVMs), since Amazon Transcribe exposes no speaker embedding.

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
| [`EndOfCallTranscriptSummary`](#endofcalltranscriptsummary) | `BEDROCK` | Resource |
| [`EndOfCallLambdaHookFunctionArn`](#endofcalllambdahookfunctionarn) | (empty) | Resource |
| [`StartOfCallLambdaHookFunctionArn`](#startofcalllambdahookfunctionarn) | (empty) | Resource |
| [`PostCallSummaryLambdaHookFunctionArn`](#postcallsummarylambdahookfunctionarn) | (empty) | Resource |

### `TranscriptLambdaHookFunctionArn`

Console label: *Lambda Hook Function ARN for Custom Transcript Segment Processing (existing)*

- **Default:** (empty)
- **Allowed values:** Matches `^(|arn:aws:lambda:.*)$`
- **Kind:** Resource. Grants the meeting event processor permission to invoke the named function.

(Optional) If present, the specified Lambda function is invoked by the LMA Call Event Processor Lambda function for each completed (non-partial) transcript segment. The function can capture and/or modify the text of the transcript, for example to implement custom redaction logic, profanity filtering, or custom rules to highlight patterns in the transcript.

### `EndOfCallTranscriptSummary`

Console label: *End of Call Transcript Summary*

- **Default:** `BEDROCK`
- **Allowed values:** `BEDROCK`, `LAMBDA`
- **Kind:** Resource. Decides whether the Bedrock summarizer exists or a Lambda hook is invoked instead.

Set to enable call summarization by a Large Language Model. Currently only BEDROCK and LAMBDA are supported. BEDROCK option uses the model chosen on the Configuration page (Amazon Bedrock Models section). The LAMBDA option requires you to provide a function ARN below.

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

## Configuration

| Parameter | Default | Kind |
|---|---|---|
| [`ConfigurationPreset`](#configurationpreset) | `standard` | Resource |
| [`CustomConfigPath`](#customconfigpath) | (empty) | Resource |

### `ConfigurationPreset`

Console label: *Configuration Preset*

- **Default:** `standard`
- **Allowed values:** `standard`, `internal-meetings-notify`, `regulated-consent-required`, `regulated-notify`, `none`
- **Kind:** Resource. Decides what the configuration seeder writes as the deployment's default configuration, which is re-written when it changes.

The configuration preset the deployment's default configuration is built from, which sets consent, retention and privacy together: standard (the shipped defaults), internal-meetings-notify (participants are notified and may object; audio and the meeting chat are captured), regulated-consent-required (participants must consent first, the transcript is deleted 15 minutes after the summary, no audio is recorded, personal information is redacted, and Amazon Bedrock runs within this Region's geography; LogLevel and the retention-day parameters are set here, not by the preset) or regulated-notify (as regulated-consent-required, except that participants are notified, capture runs from the start and stops when one objects). Each preset is also offered on the Configuration page as a read-only Managed profile. none is accepted as standard, for stacks deployed with it. CustomConfigPath is applied over this when provided.

### `CustomConfigPath`

Console label: *Custom Configuration Path*

- **Default:** (empty)
- **Allowed values:** Matches `^(|s3://[^/]+/.+)$`
- **Kind:** Resource. Decides what the configuration seeder lays over the default configuration, and grants the seeder read access to the file.

Optional s3://bucket/key of a configuration file, JSON or YAML, laid over ConfigurationPreset to make the deployment's default configuration: an object of configuration section name to settings, or a file exported from the Configuration page. It is checked against the configuration schemas, and the deployment fails if it does not match. It is read again on every stack update, so it must stay readable for the life of the stack, and an edit to it applies on the next update. Leave blank to use the preset alone.

## Retention Settings

| Parameter | Default | Kind |
|---|---|---|
| [`EnableVideoRecording`](#enablevideorecording) | `true` | Resource |
| [`MeetingRecordExpirationInDays`](#meetingrecordexpirationindays) | `90` | Configuration page limit |
| [`TranscriptionExpirationInDays`](#transcriptionexpirationindays) | `90` | Resource |
| [`AudioRecordingExpirationInDays`](#audiorecordingexpirationindays) | `90` | Resource |
| [`CloudWatchLogsExpirationInDays`](#cloudwatchlogsexpirationindays) | `90` | Resource |
| [`VPSchedulerStateMachineLogsExpirationInDays`](#vpschedulerstatemachinelogsexpirationindays) | `7` | Resource |
| [`EnableDataRetentionOnDelete`](#enabledataretentionondelete) | `true` | Resource |

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
| [`AppSyncFieldLogLevel`](#appsyncfieldloglevel) | `ERROR` | Resource |

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

Logging level for the Lambda functions and containers. At INFO (the default) and above, log lines record identifiers, sizes and outcomes rather than meeting content. DEBUG also logs meeting content: the summary prompt with its transcript, meeting assistant requests and answers, and the voice assistants' heard and spoken text; use it only while diagnosing a problem.

### `AppSyncFieldLogLevel`

Console label: *GraphQL API Log Level*

- **Default:** `ERROR`
- **Allowed values:** `NONE`, `ERROR`, `INFO`, `DEBUG`, `ALL`
- **Kind:** Resource. Sets the GraphQL API's log level and whether its log entries carry request content.

What the GraphQL API writes to its CloudWatch log group, /aws/appsync/apis/&lt;api id&gt;. ERROR logs requests that fail, without their content. DEBUG and ALL also log each request's content, which includes transcript text; use them only while diagnosing a problem. The log group keeps entries for CloudWatchLogsExpirationInDays.

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
- **Kind:** Deployment check. Decides whether the deployment checks that the shipped default Amazon Bedrock models and the embedding model can be invoked.

Specifies if access to the shipped default Amazon Bedrock models (summaries, meeting assistant and its knowledge bases) and the knowledge base embedding model is validated during deployment. Setting this parameter to "false" is useful for automated deployments (e.g. AWS Workshop Studio) where Amazon Bedrock model access permissions may not be set up yet.

<!-- END GENERATED PARAMETER REFERENCE -->

## Notes on specific parameters

### Choosing Amazon Bedrock models

The models are settings of the Configuration page since 0.4.0 (Amazon Bedrock
Models section); see
[Choosing Amazon Bedrock models](configuration-guide.md#choosing-amazon-bedrock-models).
`ModelValidation` checks, when the stack is created and on an update that changes
`ModelValidation` or the list of models it checks (the update to 0.4.0 is one),
access to the
shipped defaults of that section that every deployment calls (the summary model,
and the meeting assistant and knowledge base model) and to the knowledge base
embedding model. It does not check the key frame text model or a model chosen
later on the Configuration page. Set it to `false` for a deployment that uses
other models and has no access to the shipped ones.

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

### The ASR MicroVM speech engine

`EnableMicrovmAsr` is the only deploy-time question for this engine, and it
defaults to `true`: the ASR MicroVM engine is the default transcription engine for
Stream Audio, Chrome extension, Desktop Capture and Virtual Participant meetings,
with Amazon Transcribe as the alternative and the fallback. The nested stack is
created only in a Region with AWS Lambda MicroVMs (us-east-1, us-east-2,
us-west-2, ap-northeast-1 and eu-west-1, the Region list in the Conditions
of `lma-main.yaml`); in any other Region, GovCloud included, the
parameter has no effect and every meeting uses Amazon Transcribe. Set it to
`false` to use Amazon Transcribe for every meeting.

A meeting this engine transcribes does not go through Amazon Transcribe, so the
redaction, custom vocabulary, custom language model and language identification
settings are not available on it, and it transcribes English only; a meeting whose
Amazon Transcribe settings use any of them, or another language, is therefore kept on
Amazon Transcribe. It is the default because voice enrollment and per-voice speaker
separation need it, not because its transcripts are better: its quality is below
Amazon Transcribe's and has not been benchmarked. Two more values are fixed in the `AsrDefaults`
mapping in `lma-main.yaml` rather than asked: `ModelBundle`, which models the
MicroVM image is built from (`parakeet-tdt-v3-live-nemotron35-sortformer` by
default), and `MaxMeetingSeconds` (`14400`), the hard lifetime ceiling per MicroVM.
Which meetings use the engine is chosen in the Transcription Engine section of the
Configuration page, with no stack update. The bundles, their licences and how one
is selected are described in [On-demand ASR & Speaker Diarization](microvm-asr.md).

### Voice enrollment (Beta)

> [!IMPORTANT]
> Off by default, and turning it on means storing biometric data about people.
> Read [Voice Enrollment](voice-enrollment.md) before enabling it.

While `EnableVoiceEnrollment` is `false` the tables, the consent portal and the
matching step do not exist in the deployment at all. Both enrolling and matching
need the ASR MicroVM engine (`EnableMicrovmAsr=true`, the default, in a Region
with AWS Lambda MicroVMs), because that engine is what computes speaker
embeddings; with enrollment on and that engine absent, the tables, the portal and the
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
