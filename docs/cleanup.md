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
3. **Delete CloudWatch Log groups**: Remove log groups with `/aws/lambda/LMA-` or `/ecs/LMA-` prefixes.
4. **Schedule KMS key deletion**: If applicable, schedule deletion of any KMS keys created by the stack. KMS keys have a mandatory waiting period of 7 to 30 days before they are permanently deleted.

## Related Documentation

- [Stack Updates & Upgrades](stack-updates-and-upgrades.md)
