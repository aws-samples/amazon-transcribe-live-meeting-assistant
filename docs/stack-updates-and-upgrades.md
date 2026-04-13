# Stack Updates & Upgrades

## Table of Contents

- [Overview](#overview)
- [Update Steps](#update-steps)
- [Template URLs by Region](#template-urls-by-region)
- [Building from Source](#building-from-source)
- [What Is Preserved Across Updates](#what-is-preserved-across-updates)
- [What May Change](#what-may-change)
- [Version Migration Notes](#version-migration-notes)
  - [v0.3.0](#v030)
  - [v0.2.0](#v020)
- [Related Documentation](#related-documentation)

## Overview

This guide covers how to update an existing LMA stack to a new version or change its configuration. Stack updates allow you to adopt new features, apply bug fixes, and adjust parameters without deleting and recreating the stack.

## Update Steps

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
| AP Southeast (Sydney) | `https://s3.ap-southeast-2.amazonaws.com/aws-bigdata-blog-replica-ap-southeast-2/artifacts/lma/lma-main.yaml` |

## Building from Source

If you are building from source, use the template URL provided in the output of `publish.sh` instead of the URLs listed above.

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
