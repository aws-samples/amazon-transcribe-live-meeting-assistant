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

Stack deletion removes most resources automatically. However, certain resources are intentionally retained to prevent accidental data loss.

## Retained Resources

The following resources are **not** deleted when the stack is removed:

- **S3 buckets** (recordings, build artifacts)
- **DynamoDB tables** (meeting data, configuration)
- **CloudWatch Log groups**
- **KMS keys** (if `EnableDataRetentionOnDelete` was set to true)

These resources are retained as a safety measure to prevent irreversible loss of meeting data and recordings.

## Manual Cleanup

To fully remove all LMA resources after stack deletion, complete the following steps:

1. **Delete S3 buckets**: Find buckets with "lma" in the name. You must empty each bucket before deleting it.
2. **Delete DynamoDB tables**: Remove any DynamoDB tables associated with the LMA deployment.
3. **Delete CloudWatch Log groups**: Remove log groups with the `/<stack-name>/`, `/aws/lambda/LMA-`, `/ecs/LMA-`, `/aws/lambda-microvms/<stack-name>-` or `/aws/vendedlogs/states/<stack-name>` prefixes.

   Two of these outlive the stack whether or not you set `EnableDataRetentionOnDelete`, so they need sweeping on every deployment that has ever been upgraded past v0.3.11:

   - The implicit `/aws/lambda/<function-name>` groups that Lambda created for the functions which had no declared log group before v0.3.11. Those functions now log elsewhere, nothing was migrated, and these groups have no expiry, so whatever they hold is kept indefinitely with no stack to delete it.
   - `/aws/lambda-microvms/<stack-name>-vp` and `-asr`, the MicroVM log groups used before v0.3.11. The stack now writes to `-vp-logs` and `-asr-logs` and does not own the originals. See [Stack Updates & Upgrades](stack-updates-and-upgrades.md#v0311).
4. **Schedule KMS key deletion**: If applicable, schedule deletion of any KMS keys created by the stack. KMS keys have a mandatory waiting period of 7 to 30 days before they are permanently deleted.

## Related Documentation

- [Stack Updates & Upgrades](stack-updates-and-upgrades.md)
