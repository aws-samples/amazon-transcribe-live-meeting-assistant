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
| `LLMPromptTemplateTable` | LLM template stack | Prompt templates edited from the web UI |
| `ChatButtonConfigTable` | Chat button stack | Chat button definitions edited from the web UI |
| `NovaSonicConfigTable` | Nova Sonic stack | Voice assistant prompt and model settings |
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
