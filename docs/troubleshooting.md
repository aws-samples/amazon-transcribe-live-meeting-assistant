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
  - [VP Stuck at MANUAL_ACTION_REQUIRED](#vp-stuck-at-manual_action_required)
  - [VP Stuck in INITIALIZING](#vp-stuck-in-initializing)
  - [VP Stuck in WAITING_FOR_CAPACITY](#vp-stuck-in-waiting_for_capacity)
  - [Zoom Bot-Detection Block](#zoom-bot-detection-block)
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
5. **Check the VP detail page** for an `errorMessage`. The VP writes a human-readable failure reason to the DDB record (e.g. *"Meeting join failed: …"*, *"Zoom login failed: invalid credentials"*, *"ECS RunTask soft-failure: agent not connected"*) and surfaces it on the detail page's troubleshooting card.

### VP Stuck at MANUAL_ACTION_REQUIRED

This status means the VP hit a CAPTCHA, 2FA prompt, SSO redirect, an unknown consent dialog, or Zoom's bot-detection challenge that needs human input. To resolve:

1. Open the meeting detail page for the affected VP (the Flashbar alert at the top of the LMA UI links directly to it).
2. Open the **Live Virtual Participant View** panel and toggle **View Only** off so the noVNC viewer accepts your input.
3. Complete the challenge as described in the on-screen banner — type the OTP, solve the CAPTCHA, sign in via SSO, click the consent button, etc.
4. The VP detects the resolved state and continues automatically. The default timeout is 3 minutes; if no human response arrives, the VP fails the meeting cleanly with `errorMessage` set to *"Manual action timed out"*.

To get notified about MANUAL_ACTION events when you're not watching the LMA tab, grant the browser notification permission in the Web UI — the VP UI fires a desktop notification + audio chime when the status flips.

### VP Stuck in INITIALIZING

Most often caused by an ECS soft-failure: RunTask returns HTTP 200 but with a non-empty `failures` array (typically *"Container instance ... agent connection lost"* or *"Insufficient memory available"*). The state machine catches this explicitly in v0.3.4+ and writes `FAILED` to the VP record with the failure reason. If you're on an older version, check the ECS console > Tasks > Stopped for the failure reason on the task that should have launched, and consider upgrading.

If the task did launch but the VP is stuck in `INITIALIZING` without progressing to `BOOTING`, ECS Container Insights → Memory Utilized for the VP task definition family will show whether the host is starved (memory ~95%+ usually means a too-small instance type — see the [Virtual Participant](virtual-participant.md#ec2-instance-types) docs for sizing guidance).

A different symptom — VP sat at `INITIALIZING` *forever* with no error — was caused by a `MarkVPFailed` cleanup-state DDB AttributeValue shape bug fixed in v0.3.4+. If you see this on a v0.3.3-or-earlier deployment, the corresponding ECS task will have stopped with `RESOURCE:MEMORY` or similar in the AWS Step Functions execution log; manually mark the VP `FAILED` in DynamoDB and upgrade.

### VP Stuck in WAITING_FOR_CAPACITY

This means the cluster is full and the capacity-provider auto-scaler is launching a new EC2 host. Expect 60-90 seconds before the status transitions to `BOOTING`. If it stays at `WAITING_FOR_CAPACITY` longer:

1. Check the ASG console for **`LMA-*-VP-ASG`**. If `DesiredCapacity` is already at `VPMaxInstances`, raise the parameter or wait for an active VP to finish.
2. Check ASG **Activity history** for launch failures (e.g. *"Insufficient capacity in availability zone"*). Switch to a different instance type via `VPInstanceType` if the current one is unavailable in your region.
3. Check the EC2 console for new instances in `pending` state — userdata bootstrap (yum security update, ECR docker pull) takes ~30 seconds, then the ECS agent registers.

### Zoom Bot-Detection Block

Symptom: the VP fails with the Zoom dialog *"We detected you may be a bot. Automated bots aren't allowed to join this meeting or webinar..."*.

Resolutions, in order of effectiveness:

1. **Add Zoom credentials to LMA** (per-user, opt-in) — open the Create Virtual Participant modal and use the **Zoom account** card. A signed-in session bypasses most bot detection. See [Zoom Sign-in & Bot-Detection Hardening](zoom-credentials-and-bot-detection.md).
2. **Sign in to Zoom on your laptop with the same account at least once** before relying on LMA. Brand-new accounts whose only activity is joining from AWS IP ranges can still trip detection.
3. **Remove and re-save the credentials** if you previously saved them and now hit blocks — this also wipes the user's persisted Chromium profile in S3, clearing any stale cookies.

The residual risk is AWS egress IP reputation, which cannot be fully mitigated from inside the container. If many users in your org see blocks even when signed in, route VP egress through a NAT or residential-proxy provider.

### Voice Assistant Connection Issues

Check the VP task logs for WebSocket or Bedrock session errors. Voice assistant sessions auto-refresh, but may occasionally require a manual restart of the meeting. Persistent issues may indicate network connectivity problems or service disruptions.

### MCP Server Installation Fails

1. Check the CodeBuild logs for build and installation errors.
2. Verify that the MCP server package is compatible with the LMA environment.
3. Note that there is a maximum of 5 MCP servers per account.

## Cost Assessment

LMA costs depend on usage patterns and configuration. The following are approximate estimates:

- **Base infrastructure**: ~$10/month (Fargate WebSocket server at 0.25 vCPU + VPC networking)
- **VP EC2 instances**: ~$30/month per warm `t3.medium` instance (default; capacity-provider scales 1→`VPMaxInstances` on demand). Bump `VPInstanceType` to `t3.large` (~$60/month) for 3-VPs-per-host density. Set `VPMinInstances=0` to scale down to zero when idle and pay only when a VP is requested (~60-90s cold-start added to first VP).
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
