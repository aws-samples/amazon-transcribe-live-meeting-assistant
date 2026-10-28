---
title: "Stack Updates & Upgrades"
---

# Stack Updates & Upgrades

## Table of Contents

- [Overview](#overview)
- [Update Steps](#update-steps)
- [Template URLs by Region](#template-urls-by-region)
- [Building from Source](#building-from-source)
- [What Is Preserved Across Updates](#what-is-preserved-across-updates)
- [What May Change](#what-may-change)
- [Data Retention On Stack Deletion](#data-retention-on-stack-deletion)
- [Version Migration Notes](#version-migration-notes)
  - [v0.3.11](#v0311)
  - [v0.3.0](#v030)
  - [v0.2.0](#v020)
- [Related Documentation](#related-documentation)

## Overview

This guide covers how to update an existing LMA stack to a new version or change its configuration. Stack updates allow you to adopt new features, apply bug fixes, and adjust parameters without deleting and recreating the stack.

## Update Steps

### Using LMA CLI (Recommended)

The simplest way to update your stack:

```bash
# Update from the latest published template for your region
lma-cli deploy --stack-name LMA --wait

# Or update from local code changes
lma-cli deploy --stack-name LMA --from-code . --wait
```

The CLI auto-selects the correct template for your region and streams deployment events in real-time.

### Using AWS Console

1. Log into the AWS Console.
2. Navigate to **CloudFormation** and select your LMA stack.
3. Choose **Update** and then **Replace current template**.
4. Enter the template URL for your region (see table below).
5. Review and adjust parameters as needed.
6. Click **Next** twice, check the IAM acknowledgement boxes, and click **Update stack**.

## Template URLs by Region

| Region | Template URL |
|--------|-------------|
| US East (N. Virginia) | `https://s3.us-east-1.amazonaws.com/aws-ml-blog-us-east-1/artifacts/lma/lma-main.yaml` |
| US West (Oregon) | `https://s3.us-west-2.amazonaws.com/aws-ml-blog-us-west-2/artifacts/lma/lma-main.yaml` |
| Asia Pacific (Tokyo) | `https://s3.ap-northeast-1.amazonaws.com/aws-bigdata-blog-replica-ap-northeast-1/artifacts/lma/lma-main.yaml` |
| Europe (Ireland) | `https://s3.eu-west-1.amazonaws.com/aws-bigdata-blog-replica-eu-west-1/artifacts/lma/lma-main.yaml` |

## Building from Source

If you are building from source, use `lma-cli deploy --from-code .` to build and deploy in one step, or use `lma-cli publish` to build first and then deploy with the resulting template URL.

```bash
# Build and deploy in one step
lma-cli deploy --stack-name LMA --from-code . --wait

# Or publish first, then deploy separately
lma-cli publish --source-dir . --region us-east-1
lma-cli deploy --stack-name LMA --template-url <url-from-publish-output> --wait
```

The legacy `publish.sh` script is also still available — use its template URL output with `lma-cli deploy --template-url <url>`.

## What Is Preserved Across Updates

The following data and configuration are preserved when you update or upgrade your stack:

- **Custom prompt templates** stored in DynamoDB
- **User data** including meetings, transcripts, and recordings
- **Cognito user accounts** (v0.2.0 and later)
- **Installed MCP servers**

## What May Change

The following may be modified during an update:

- Default prompt templates
- Infrastructure resources (Lambda functions, ECS tasks, etc.)
- Lambda function code

## Data Retention On Stack Deletion

The `EnableDataRetentionOnDelete` parameter (default `true`) decides what happens
to the resources that hold durable data when the stack itself is deleted. With it
set to `true`, deleting the stack leaves the following behind in your account
rather than removing them; the resources it deliberately does not cover are listed
after the table.

| Resource | Stack | Contents |
|----------|-------|----------|
| `EventSourcingTable` | AI stack | Meetings, transcripts and summaries |
| `VirtualParticipantTable` | AI stack | Virtual participant records and schedules |
| `MCPServersTable` | AI stack | Installed MCP server configurations |
| `MCPApiKeysTable` | AI stack | Hashed per-user MCP API keys |
| `OAuthStateTable` | AI stack | In-flight OAuth authorization state |
| `VPTaskRegistry` | AI stack | Virtual participant task registry |
| `DomSelectorCache` | AI stack | Cached meeting-platform UI selectors |
| `VPProfilesBucket` | AI stack | Virtual participant browser profiles |
| `CallEventProcessorDiscardedRecordsQueue` | AI stack | Transcript records the pipeline did not apply, awaiting triage |
| `RecordingsBucket` | Main stack | Meeting audio and video recordings, and transcript files |
| `LoggingBucket` | Main stack | S3 server access logs and load balancer logs |
| `CustomerManagedEncryptionKey` | Main stack | The KMS key every other retained resource is encrypted with |
| `UserPool` | Cognito stack | User accounts and group memberships |
| `IdentityPool` | Cognito stack | Identity pool and the identity ids issued from it |
| `ConfigurationTable` | AI stack | Runtime configuration edited from the web UI: the deployment baseline, the profiles holding an administrator's edits, and the pointer naming the active one. See [Unified Configuration Store](configuration-store.md) |
| `LLMPromptTemplateTable` | LLM template stack | The troubleshooting agent's system prompt, which it reads from here; and the summary prompt templates as they were stored before `ConfigurationTable`. Still written by its own stack so a deployment can be rolled back, and read by the configuration seeder when `Config#custom` is absent. Note that a prompt edit saved after the upgrade lives only in `ConfigurationTable`, so a rollback reverts to the prompts as they were before it |
| `ChatButtonConfigTable` | Chat button stack | The chat button definitions as they were stored before `ConfigurationTable`. Still written by its own stack so a deployment can be rolled back, and read by the configuration seeder while `Config#custom` holds no chat buttons. A button saved after the upgrade lives only in `ConfigurationTable`, so a rollback reverts to the buttons as they were before it |
| `NovaSonicConfigTable` | Nova Sonic stack | The voice assistant's prompt and model settings as they were stored before `ConfigurationTable`. Still written by its own stack so a deployment can be rolled back, read by the configuration seeder while `Config#custom` holds no Nova Sonic settings, and read by the Virtual Participant only when `ConfigurationTable` cannot be read or holds no shipped Nova Sonic settings. A setting saved after the upgrade lives only in `ConfigurationTable`, so a rollback reverts to the settings as they were before it |
| `AsrConfigTable` | ASR MicroVM stack | Diarization operating point tuned from the ASR Config page |
| `S3VectorBucket`, `S3VectorIndex` | Bedrock KB stack | Knowledge base vector store and its embeddings |

`CustomerManagedEncryptionKey` matters more than it looks: without the key, the
retained tables and buckets cannot be read. Keep it for as long as you keep
anything encrypted with it.

Retained resources continue to incur storage charges and must be removed by hand
when you no longer need them — see [Cleanup](cleanup.md). Set
`EnableDataRetentionOnDelete` to `false` if you would rather a stack deletion
remove everything.

### What the parameter does not cover

The parameter governs everything that holds durable user data. These resources are
deleted with the stack either way, deliberately:

| Resource | Stack | Why it is not retained |
|----------|-------|------------------------|
| `WebAppBucket` | AI stack | Build artifacts a redeployment regenerates; emptied on deletion regardless of this parameter |
| `MCPServerExternalAppClient` | AI stack | Configuration rather than data; recreated by a redeployment |
| `TranscriberCallEventTable` | Transcriber stack | Short-lived, TTL'd handoff state for the Post Call Analytics path, not user data. Its template does not read this parameter at all |
| `AsrImageSourceBucket` | ASR MicroVM stack | Per-stack build context for the MicroVM image, rebuilt from the repository on the next deployment |
| `CustomHeaderNameSecret`, `CustomHeaderValueSecret` | Transcriber stack | CloudFront origin-verification secrets, regenerated per deployment; retaining them would leave Secrets Manager entries that a new stack cannot use |
| `CategorySNSTopic` | AI stack | A topic, not a store — it holds no messages between deliveries. Any subscriptions you added to it by hand are lost with it |

One consequence to be aware of when deleting a stack: `AsrImageSourceBucket` has
versioning enabled and, unlike `WebAppBucket`, no custom resource that empties it
first, so a stack deletion can fail on a non-empty bucket. Empty it by hand and
retry the deletion if that happens.

The retention setting is stored as a CloudFormation resource attribute, so
changing the parameter on an existing stack only takes effect once the stack
update that carries it has completed.

## Version Migration Notes

### v0.3.11

Every Lambda function and MicroVM in the deployment now logs to a log group the
stack declares, with the retention set by `CloudWatchLogsExpirationInDays` and
the deployment's customer-managed key. That includes the two Bedrock Knowledge
Base stacks, whose functions had no declared group at all and so logged to
implicit ones. Nothing breaks on upgrade, but five things are worth knowing
before you click **Update**.

**The MicroVM log groups are renamed, and a deployment using MicroVM compute
rebuilds its image.** CloudFormation cannot take ownership of a log group that
already exists, so these two could not be declared under the names the service
had already created:

| Logs written before this upgrade | Logs written after |
|---|---|
| `/aws/lambda-microvms/<stack>-vp` | `/aws/lambda-microvms/<stack>-vp-logs` |
| `/aws/lambda-microvms/<stack>-asr` | `/aws/lambda-microvms/<stack>-asr-logs` |

The new groups receive both the image build output and the output of every
MicroVM — each Virtual Participant and each ASR session — launched after the
upgrade. A MicroVM that was already running when you upgraded keeps logging to
the old group until it ends, and so does one launched during the update before
its launcher function was updated. CloudFormation updates each launcher only
after that stack's image rebuild finishes, so most launches during an update
still go to the old group.

Changing where a MicroVM image logs is an image update, so the Virtual
Participant image (roughly 8–10 minutes) and the ASR image (longer — it
re-downloads the model bundle) are rebuilt as part of this upgrade. **The ASR
rebuild depends on an external model host being reachable at upgrade time.** If
that download fails all five of its retries, the image build fails and the
nested ASR stack rolls back — and because the rollback has to put the old log
group name back, it is itself another update that may rebuild against the same
unreachable host. If that leaves the nested stack in `UPDATE_ROLLBACK_FAILED`,
run `aws cloudformation continue-update-rollback` on it and retry the upgrade
later. Deployments not using MicroVM compute are unaffected.

**Nothing is migrated, and the old groups are left behind.** Logs already in the
implicit `/aws/lambda/<function-name>` groups, and in the two MicroVM groups
above, stay exactly where they are. CloudFormation does not own them, this
upgrade does not delete them, and they still have no expiry — so they are
permanent leftovers until you delete them. See [Cleanup](cleanup.md) for the
prefixes to sweep.

**VPC Flow Log retention now follows your setting.** The VPC stack applied its
own 14-day default whatever `CloudWatchLogsExpirationInDays` was set to at the
top level, because the parameter was never passed down to it. If your top-level
value is higher than 14 — the default is 90 — your VPC Flow Logs will start
retaining for longer, and costing more, after this upgrade. Lower it on the VPC
stack's behalf by lowering the top-level parameter if that is not what you want.

**Three log groups are removed if the feature they belong to is currently off.**
`BedrockSummaryLambdaLogGroup`, `CognitoUserPoolEmailDomainVerifyFunctionLogGroup`
and `PCAIntegrationFunctionLogGroup` now exist only when the function that writes
to them exists. If your deployment has the Bedrock summariser, the sign-up email
domain restriction or the PCA integration switched **off**, the corresponding
group is deleted by this upgrade — along with anything it still holds from a
period when the feature was on. Export what you need from those three before
upgrading. The alternative was leaving an unowned group behind to block its own
re-creation when the feature is switched back on.

**One new parameter.** `VPSchedulerStateMachineLogsExpirationInDays` (default 7)
sets retention for the scheduled Virtual Participant state machine's execution
history. The default matches the value that was previously hardcoded, so no
deployment changes unless you raise it.

#### The Meeting Assistant's knowledge-base model

A separate change in this release splits one Bedrock model parameter into two,
and a deployment that changed the Meeting Assistant's model should set the new
one **before** updating.

The Meeting Assistant's two knowledge-base tools — `document_search` over your
documents and `meeting_history` over past meeting transcripts — do not call
Bedrock the way the agent does. They ask a Bedrock Knowledge Base to generate an
answer from what it retrieves, and a knowledge base drives that with
orchestration and generation prompt templates it supplies itself. Those templates
cover fewer models than the agent's own API does, and which models they cover is
a property of the individual model rather than of its family or version — Claude
Opus 4.1 is refused where Claude Opus 4.6 is accepted. Until now both jobs read
one parameter, so the model you picked for the agent was also the model the
knowledge bases tried to generate with. The new
`MeetingAssistKnowledgeBaseModelID` names the second job separately and offers
only the seven ids verified against a live knowledge base, defaulting to
`global.anthropic.claude-haiku-4-5-20251001-v1:0`.

What to set depends on what your agent is running on now. Every row below is a
value v0.3.10 offered, so one of them is the deployment you are upgrading:

| Your current `MeetingAssistServiceBedrockModelID` | What the knowledge-base tools were doing | What to set |
|---|---|---|
| `global.anthropic.claude-haiku-4-5-20251001-v1:0` (the default) | Generating on Claude Haiku 4.5 | Nothing — the new parameter's default is the same model |
| Claude Sonnet 4.5, Sonnet 4.6 or Opus 4.6, in either profile form | Generating on that model | The same id, which the new parameter offers |
| Claude Opus 4.1 | Being refused on every call | Any offered id; the default is the safe pick |
| Any Nova model, or Claude Opus 4 | Never established | Any offered id; the default is the safe pick |

The Claude 5.5 models are not in that table because v0.3.10 did not offer them
for the agent either — they and this parameter arrive in the same release. They
are selectable for the agent only; retrieval always uses
`MeetingAssistKnowledgeBaseModelID`, so choosing one for the agent leaves the
knowledge-base tools alone.

**Enable access to the value you choose before updating.** Deploy-time parameter
validation now converses with the knowledge-base model as well as the agent's, so
a stack whose account has never had access to the id in the new parameter is
rejected at parameter validation instead of part-way through the update. A
deployment that kept the agent default already has access to Haiku 4.5 and is
unaffected. This check depends on `ModelValidation`, which defaults to `true`;
an automated deployment that sets it to `false` gets no up-front check, and a
model it cannot reach shows up as a failing knowledge-base tool during a meeting
instead.

If a knowledge base is ever given a model it cannot generate with — reachable
only by deploying `lma-meetingassist-setup-stack/template.yaml` on its own, since
the top-level parameter does not offer one — the tool it backs is not offered to
the agent at all, and the function's log names the model and the parameter to
change. Listing past meetings chronologically still works in that state, because
the `recent_meetings_list` tool reads DynamoDB and involves no knowledge base.
See [The knowledge-base model](meeting-assistant.md#the-knowledge-base-model).

### v0.3.0

This is a major consolidation release. The following features and integrations have been **removed**:

- QnABot
- Amazon Lex
- Bedrock Agent
- Amazon Q Business
- OpenSearch
- Chrome browser extension
- Healthcare domain support
- Claude 3.x models (Claude 4+ only)

Only the **Strands Bedrock agent** remains as the meeting assistant service. If you relied on any of the removed features, they will no longer be available after upgrading to v0.3.0.

### v0.2.0

**Breaking change**: The Cognito user pool is deleted and recreated during this upgrade. All existing users must re-register after the update completes. See [User-Based Access Control](user-based-access-control.md) for details on user management.

## Related Documentation

- [Prerequisites & Deployment](prerequisites-and-deployment.md)
- [CloudFormation Parameters Reference](cloudformation-parameters.md)
- [Cleanup](cleanup.md)
