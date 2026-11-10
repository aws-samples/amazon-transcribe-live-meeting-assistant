---
title: "Cleanup"
---

# Cleanup

## Table of Contents

- [Overview](#overview)
- [Deleting the Stack](#deleting-the-stack)
- [Retained Resources](#retained-resources)
- [Manual Cleanup](#manual-cleanup)
- [Related Documentation](#related-documentation)

## Overview

This guide describes how to delete an LMA deployment and clean up all associated AWS resources.

## Deleting the Stack

To delete the LMA stack:

1. Open the **AWS CloudFormation** console.
2. Select the LMA stack.
3. Choose **Delete**.
4. Confirm the deletion.

Stack deletion removes most resources automatically. What it leaves behind is
decided by the `EnableDataRetentionOnDelete` stack parameter, `true` by default,
and by a few resources that are kept whatever the parameter says.

## Retained Resources

**Data stores follow `EnableDataRetentionOnDelete`.** With `true`, the stack's
DynamoDB tables (meetings, configuration, Virtual Participants and the others), its
S3 buckets (recordings, server access logging, configuration history and Virtual
Participant browser profiles), the transcript knowledge base's vector bucket and
index, and the customer-managed AWS KMS key are kept, with their contents. With
`false`, the S3 buckets are emptied of every object version and deleted, and the
tables, the vector bucket and index, and the key are deleted with the stack; AWS
KMS deletes a key only after its waiting period. The web UI's bucket is emptied and
deleted either way.

**Most CloudWatch log groups are deleted with the stack.** The log groups of the
Lambda functions, the CodeBuild projects, the VPC flow logs and the other
declared groups belong to the stack and are deleted with it, whatever
`EnableDataRetentionOnDelete` says.

**Kept whatever `EnableDataRetentionOnDelete` says.** These hold meeting-time
output or audit records, and are kept on every stack deletion. In the names,
`<stack-name>` is the name of the nested stack that owns the resource, which
begins with the name of the main stack:

| Resource | Name | Kept until |
|---|---|---|
| The WebSocket transcriber's container log group | `/<stack-name>/fargate/TranscriberWebsocket` | Its entries expire under `CloudWatchLogsExpirationInDays`; the group stays until you delete it |
| The Virtual Participant's container log group | `/<stack-name>/fargate/VirtualParticipant` | As above |
| The scheduled Virtual Participant state machine's log group | `/aws/vendedlogs/states/<stack-name>` | Entries expire under `VPSchedulerStateMachineLogsExpirationInDays` |
| The GraphQL API's log group | `/aws/appsync/apis/<api id>` | Created by AppSync, not owned by the stack. Entries expire under `CloudWatchLogsExpirationInDays` |
| The voice enrollment table and the voice consent log (only with `EnableVoiceEnrollment=true`) | DynamoDB tables whose names begin with `<stack-name>-` | Until you delete them. The voice consent log is the audit trail of consent given and withdrawn |

**Kept only when `EnableDataRetentionOnDelete` is `true`.** The MicroVM log
groups, `/aws/lambda-microvms/<stack-name>-vp-logs` (Virtual Participants on
Lambda MicroVMs) and `/aws/lambda-microvms/<stack-name>-asr-logs` (the on-demand
speech engine), are kept with `true` and deleted with `false`.

A deployment that has ever been upgraded to v0.4.0 or later from an earlier
version may also have log groups that the stack does not own at all; step 3 below
lists them. See [Privacy and Data Handling Controls](privacy-and-data-handling.md#deleting-a-stack)
for what each kept resource holds.

## Manual Cleanup

To fully remove all LMA resources after stack deletion, complete the following steps:

1. **Delete S3 buckets** (if `EnableDataRetentionOnDelete` was `true`): find buckets with the stack's name in their name. The buckets are versioned, so empty each one of every object version and delete marker (the console's **Empty** action does this) before deleting it. Delete the transcript knowledge base's vector bucket as well.
2. **Delete DynamoDB tables**: remove the DynamoDB tables whose names begin with the stack's name, including the voice enrollment tables if voice enrollment was deployed.
3. **Delete CloudWatch Log groups**: remove log groups with the `/<stack-name>/`, `/aws/lambda/LMA-`, `/ecs/LMA-`, `/aws/lambda-microvms/<stack-name>-`, `/aws/vendedlogs/states/<stack-name>` or `/aws/appsync/apis/<api id>` prefixes.

   Three of these outlive the stack whether or not you set `EnableDataRetentionOnDelete`, so they need sweeping on every deployment that has ever been upgraded to v0.4.0 or later:

   - The implicit `/aws/lambda/<function-name>` groups that Lambda created for the functions which had no declared log group before v0.4.0. Those functions now log elsewhere, nothing was migrated, and these groups have no expiry, so whatever they hold is kept indefinitely with no stack to delete it.
   - `/aws/lambda-microvms/<stack-name>-vp` and `-asr`, the MicroVM log groups used before v0.4.0. The stack now writes to `-vp-logs` and `-asr-logs` and does not own the originals. See [Stack Updates & Upgrades](stack-updates-and-upgrades.md#v040).
   - `/aws/codebuild/<project>`, where the stack's CodeBuild projects wrote their build output before v0.4.0. Builds now log to `/<stack-name>/codebuild/<project>`, which the stack owns; the originals have no expiry. Five of the six project names are generated by CloudFormation and do not contain the stack name: they are a shortened resource name and a random suffix, so the groups start `/aws/codebuild/LMAUICodeBuildProject-`, `/aws/codebuild/WebsocketTranscriberCodeBui-`, `/aws/codebuild/VirtualParticipantBuildProj-`, `/aws/codebuild/BrowserExtensionCodeBuildPr-` and `/aws/codebuild/DesktopCaptureAppCodeBuildP-`. If more than one LMA stack exists in the account and region, match the suffix against the stack's CodeBuild projects before deleting. The sixth is `/aws/codebuild/<stack-name>-AISTACK-<id>-MCP-Layer-Builder`.
4. **Schedule KMS key deletion**: If applicable, schedule deletion of any KMS keys created by the stack. KMS keys have a mandatory waiting period of 7 to 30 days before they are permanently deleted.

## Related Documentation

- [Stack Updates & Upgrades](stack-updates-and-upgrades.md)
