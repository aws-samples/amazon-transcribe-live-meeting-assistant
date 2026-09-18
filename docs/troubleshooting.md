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
  - [Discarded Transcript Records](#discarded-transcript-records)
  - [Meeting Assistant Not Responding](#meeting-assistant-not-responding)
  - [VP Fails to Join Meeting](#vp-fails-to-join-meeting)
  - [VP Stuck at MANUAL_ACTION_REQUIRED](#vp-stuck-at-manual_action_required)
  - [VP Stuck in INITIALIZING](#vp-stuck-in-initializing)
  - [VP Stuck in WAITING_FOR_CAPACITY](#vp-stuck-in-waiting_for_capacity)
  - [Zoom Join Blocked](#zoom-join-blocked)
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

A meeting leaves the "In Progress" state when an end-of-meeting event reaches
the Call Event Processor through the Kinesis call data stream. Each meeting
source sends that event as part of its own shutdown, and each has a backstop if
its shutdown never runs:

- **Virtual Participant**: the VP task writes the event when it leaves the
  meeting, and the VP stack's task reaper covers a task that stops without
  doing so (automatic cleanup on uncaught errors was added in v0.3.0).
- **Uploaded recording**: `upload_meeting_finalizer` sends it when the Amazon
  Transcribe batch job reaches `COMPLETED` or `FAILED`.
- **Streamed audio** (Stream Audio tab, Desktop Capture App, browser
  extension): the WebSocket server sends it when the connection closes. If the
  Fargate task hosting the connection goes away without running its close
  handler, the `StaleMeetingReaper` Lambda ends the meeting instead. It runs
  every 15 minutes and ends meetings that have produced no finalized transcript
  segment for longer than `MeetingInactivityTimeoutInMinutes` (default 240),
  so a meeting can show as in progress for up to that long plus one schedule
  interval before it closes on its own.

To see what the reaper is doing, look at the
`/<AISTACK-name>/lambda/StaleMeetingReaperFunction` log group: each run logs a
summary with the number of candidates examined and meetings ended, and one
record per meeting it ended with that meeting's `call_id` and last activity
timestamp. A meeting the reaper considered but left alone is either still
inside the timeout, already `ENDED`, or still owned by the upload pipeline (an
in-flight Transcribe job on a long recording can easily outlast the timeout, so
those meetings are skipped and closed by the finalizer instead).

Setting `MeetingInactivityTimeoutInMinutes` to `0` disables the schedule. With
it disabled, or to end a meeting sooner than the timeout, you can still end a
meeting by hand by updating its record (`PK` = `SK` = `c#<CallId>`) in the event
sourcing DynamoDB table.

### No Transcription Appearing

1. Check the WebSocket Fargate task logs for errors.
2. Verify that audio is being streamed from the client.
3. Check Amazon Transcribe service limits to ensure you have not exceeded the concurrent stream quota.

### Discarded Transcript Records

Transcript events reach the Call Event Processor Lambda as Kinesis batches. When a
record cannot be applied, the processor reports it to the event source mapping,
which retries the shard from that record and — after the retries are exhausted or
the record passes `MaximumRecordAgeInSeconds` (one hour) — describes the batch it
gave up on to an SQS queue whose name contains
`CallEventProcessorDiscardedRecordsQueue` (CloudFormation generates the full name
from the AI stack's name). A record the processor
could not decode at all is not retried, because it would fail the same way every
time and hold up every later meeting on its shard; those records are copied to the
same queue individually, since skipping one does not fail the invocation and so
never reaches the mapping's own destination.

An empty queue is the normal state. If there are messages on it, read them with
**SQS console → the queue → Send and receive messages → Poll for messages**, or
`aws sqs receive-message --queue-url <url> --max-number-of-messages 10`. Two
shapes arrive:

- **From the event source mapping** — a description of a failed batch: the stream
  ARN, the shard id, and the sequence number range it covers. The payloads are not
  included; they are still in the Kinesis stream.
- **From the function** — one message per skipped record, carrying `shardId`,
  `sequenceNumber`, `partitionKey`, `eventID` and the record's `data` exactly as
  Kinesis delivered it (base64). Decoding `data` shows what the producer sent,
  which is usually enough to identify which client emitted it.

To retrieve the original records for a failed batch, use the shard id and the
first sequence number with `aws kinesis get-shard-iterator --shard-iterator-type
AT_SEQUENCE_NUMBER` followed by `get-records`. **This is time-limited:** the call
data stream keeps records for 24 hours, so a batch reported more than a day ago
can no longer be read back from the stream — only the queue message and the
function's log lines remain. Queue messages themselves are kept for 14 days.

The matching log lines are in the `CallEventProcessor` log group: the function
logs a warning naming the reporting decision (`reporting Kinesis batch item
failures`) or the skip (`skipped Kinesis records that could not be decoded`) with
the sequence numbers involved.

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

This status means the VP hit a CAPTCHA, 2FA prompt, SSO redirect, an unknown consent dialog, or another Zoom verification step that needs human input. To resolve:

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

### Zoom Join Blocked

Symptom: the VP fails to join with the Zoom dialog *"We detected you may be a bot. Automated bots aren't allowed to join this meeting or webinar..."*.

Resolutions, in order of effectiveness:

1. **Add Zoom credentials to LMA** (per-user, opt-in) — open the Create Virtual Participant modal and use the **Zoom account** card. A signed-in session joins far more reliably. See [Zoom Sign-in & Join Reliability](zoom-credentials-and-join-reliability.md).
2. **Sign in to Zoom on your laptop with the same account at least once** before relying on LMA. A brand-new account whose only activity is joining from AWS IP ranges is more likely to hit this block.
3. **Remove and re-save the credentials** if you previously saved them and now hit blocks — this also wipes the user's persisted Chromium profile in S3, clearing any stale cookies.

The residual factor is AWS egress IP reputation, which cannot be fully controlled from inside the container. If many users in your org see joins blocked even when signed in, route VP egress through a NAT or residential-proxy provider.

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
