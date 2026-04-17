---
title: "Troubleshooting"
---

# Troubleshooting

## Table of Contents

- [Overview](#overview)
- [CloudFormation Deployment Failures](#cloudformation-deployment-failures)
- [Service Quotas](#service-quotas)
- [Runtime Monitoring via CloudWatch](#runtime-monitoring-via-cloudwatch)
- [Common Issues](#common-issues)
  - [Deployment Fails on Nested Stack](#deployment-fails-on-nested-stack)
  - [Meeting Stuck In Progress](#meeting-stuck-in-progress)
  - [No Transcription Appearing](#no-transcription-appearing)
  - [Meeting Assistant Not Responding](#meeting-assistant-not-responding)
  - [VP Fails to Join Meeting](#vp-fails-to-join-meeting)
  - [Voice Assistant Connection Issues](#voice-assistant-connection-issues)
  - [MCP Server Installation Fails](#mcp-server-installation-fails)
- [Cost Assessment](#cost-assessment)
- [Related Documentation](#related-documentation)

## Overview

This guide covers monitoring and troubleshooting for LMA deployments, including common deployment failures, runtime issues, and cost estimation.

## CloudFormation Deployment Failures

When a CloudFormation deployment fails:

1. Check the **Events** tab on the failed stack in the CloudFormation console.
2. **Always navigate into failed nested stacks** to find the root cause. The parent stack error is often generic; the nested stack Events tab contains the specific failure reason.
3. Common causes include:
   - **Service quotas exceeded**: Elastic IPs, NAT gateways, or other resource limits
   - **Insufficient IAM permissions**: The deploying user or role lacks required permissions
   - **Bedrock model access not granted**: You must explicitly enable model access in the Bedrock console before deployment

For additional guidance, see [Troubleshooting CloudFormation](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/troubleshooting.html) in the AWS documentation.

## Service Quotas

Be aware of the following service quota considerations:

- **Amazon Transcribe**: Default limit of 25 concurrent transcription streams. This directly limits the number of concurrent meetings. Request a quota increase through the AWS Service Quotas console if needed.
- **Fargate tasks**: Check your account limits for concurrent Fargate tasks.
- **NAT gateways and Elastic IPs**: Verify you have available capacity in the target region.

## Runtime Monitoring via CloudWatch

Use the following paths to access logs for each LMA component:

- **WebSocket Fargate task**: ECS console > Clusters > LMA-WEBSOCKETTRANSCRIBERSTACK-xxxx-TranscribingCluster > Tasks > Logs > View in CloudWatch
- **Call Event Processor Lambda**: Lambda console > AISTACK-CallEventProcessor > Monitor > View logs in CloudWatch
- **AppSync API**: AppSync console > CallAnalytics-LMA > Monitoring > View logs in CloudWatch
- **Step Functions**: For VP scheduling issues, check the Step Functions execution history in the Step Functions console

## Common Issues

### Deployment Fails on Nested Stack

Navigate to the specific failed nested stack and check its Events tab for the root cause. The parent stack typically shows a generic "nested stack failed" error that is not actionable on its own.

### Meeting Stuck In Progress

The Virtual Participant ECS task may have crashed. This issue was addressed in v0.3.0 with automatic cleanup on uncaught errors. If a meeting remains stuck, you can manually end it by updating the meeting record in DynamoDB.

### No Transcription Appearing

1. Check the WebSocket Fargate task logs for errors.
2. Verify that audio is being streamed from the client.
3. Check Amazon Transcribe service limits to ensure you have not exceeded the concurrent stream quota.

### Meeting Assistant Not Responding

1. Check the Call Event Processor Lambda logs for errors.
2. Verify that Bedrock model access has been granted in the Bedrock console for the configured model.
3. Review the Strands agent logs for agent-specific errors.

### VP Fails to Join Meeting

1. Check Step Functions execution logs for scheduling or state machine errors.
2. Check the ECS task logs for the specific VP task.
3. Verify that the meeting URL and credentials are correct.
4. Check for platform-specific issues (Zoom, Teams, Chime, etc.).

### Voice Assistant Connection Issues

Check the VP task logs for WebSocket or Bedrock session errors. Voice assistant sessions auto-refresh, but may occasionally require a manual restart of the meeting. Persistent issues may indicate network connectivity problems or service disruptions.

### MCP Server Installation Fails

1. Check the CodeBuild logs for build and installation errors.
2. Verify that the MCP server package is compatible with the LMA environment.
3. Note that there is a maximum of 5 MCP servers per account.

## Cost Assessment

LMA costs depend on usage patterns and configuration. The following are approximate estimates:

- **Base infrastructure**: ~$10/month (Fargate WebSocket server at 0.25 vCPU + VPC networking)
- **VP EC2 instances**: ~$33/month per warm instance (t3.medium)
- **Per-meeting usage**: ~$0.17 per 5-minute call (varies based on options selected)

Key AWS service pricing pages for detailed cost estimation:

- [Amazon Transcribe Pricing](https://aws.amazon.com/transcribe/pricing/)
- [Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)
- [Amazon Translate Pricing](https://aws.amazon.com/translate/pricing/)
- [AWS AppSync Pricing](https://aws.amazon.com/appsync/pricing/)
- [Amazon DynamoDB Pricing](https://aws.amazon.com/dynamodb/pricing/)
- [AWS Lambda Pricing](https://aws.amazon.com/lambda/pricing/)
- [Amazon S3 Pricing](https://aws.amazon.com/s3/pricing/)
- [Amazon Cognito Pricing](https://aws.amazon.com/cognito/pricing/)

Use **AWS Cost Explorer** or **Bill Details** in the AWS Billing console for actual spend tracking.

## Related Documentation

- [Infrastructure & Security](infrastructure-and-security.md)
- [CloudFormation Parameters Reference](cloudformation-parameters.md)
