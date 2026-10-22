---
title: "Monitoring & Alerting"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Monitoring & Alerting

LMA deploys an SNS topic for operational alerts, 12 alarms that notify it, and
one CloudWatch dashboard per deployment. Everything described here is created by
`lma-main.yaml` as part of the normal stack deployment — there is nothing extra
to install — but **one manual step is required before any alert is delivered**:
the email subscription SNS creates for your `AdminEmail` address starts in
`PendingConfirmation` and stays there until somebody clicks the link in the
confirmation message. See [Subscribing to the alerts topic](#subscribing-to-the-alerts-topic).

For log groups, console navigation and the symptom-by-symptom guide to specific
failures, see [Troubleshooting](troubleshooting.md). This page covers what is
watched automatically and who hears about it.

## What is watched

Every alarm on this page reads a metric that an AWS service publishes on its
own. None of them depend on LMA emitting anything, which means they work on a
deployment that has never been touched and they keep working if application code
changes. The consequence is that they cover infrastructure health well and
application behaviour only indirectly: there is no alarm for "a meeting produced
no transcript", because nothing publishes a metric for that yet. Application
metrics are a separate piece of work, and the dashboard has a band reserved for
them.

### The alarm table

Thresholds are given as the metric, the statistic and the window. "Tuned by"
names the CloudFormation parameter that changes the alarm's behaviour, with its
default in brackets; where the only parameter is `EnableOperationalAlarms`, the
threshold is fixed in the template because any non-zero value is already a
problem.

| Alarm | Fires when | Topic | Tuned by |
|---|---|---|---|
| `discarded-records-queue-depth` | The queue holding Kinesis records the Call Event Processor could not process has one or more messages visible (`Maximum` over 5 min, `> 0`). Also notifies on recovery. | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `call-event-processor-errors` | The Call Event Processor failed one or more invocations (`Sum` over 5 min, `>= 1`). | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `call-event-processor-throttles` | Lambda refused to start an invocation for want of account concurrency (`Sum` over 5 min, `> 0`). | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `kinesis-consumer-iterator-age` | The consumer is more than 5 minutes behind the stream, in two consecutive 5-minute windows (`Maximum`, `> 300000` ms). | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `kinesis-write-throughput-exceeded` | A write to the call data stream was rejected for exceeding shard throughput (`Sum` over 5 min, `> 0`). | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `transcriber-no-healthy-targets` | The transcriber load balancer has no target passing its health check, for two consecutive 1-minute windows (`Minimum`, `< 1`). Also notifies on recovery. | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `transcriber-service-no-tasks` | The transcriber ECS service is running no tasks, for two consecutive 5-minute windows (`Minimum`, `< 1`). Also notifies on recovery. | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `appsync-5xx-errors` | The GraphQL API returned five or more server errors (`Sum` over 5 min, `>= 5`). | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `transcribe-concurrent-streams-near-quota` | Concurrent Amazon Transcribe streaming sessions in this region reach the configured level (`Maximum` over 5 min, `>= threshold`). | `AlertsTopic` | `TranscribeConcurrentStreamAlarmThreshold` (`20`) |
| `stale-meeting-reaper-not-invoked` | The stale-meeting reaper recorded no invocations in each of two consecutive hours (`Sum`, `<= 0`). Also notifies on recovery. Not created at all when the reaper is switched off. | `AlertsTopic` | `MeetingInactivityTimeoutInMinutes` (`240`; `0` removes the alarm) |
| `bedrock-throttling` | Amazon Bedrock throttled five or more invocations (`Sum` over 5 min, `>= 5`). | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `vp-launch-failures` | The Virtual Participant launch state machine failed or timed out an execution (`Sum` over 5 min, `>= 1`). | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |

Each alarm carries an `AlarmDescription` written as the first few steps of a
runbook, because the description is the only context that travels with the
notification email. If you receive an alert and want to know what to check,
read the body of the message before opening the console.

### Notes on individual alarms

**Queue depth is not a rate.** Nothing drains the discarded-records queue
automatically; messages sit there for their 14-day retention. So
`discarded-records-queue-depth` does not clear on its own — the depth stays
where it is until somebody purges the queue. That is why it also notifies on
recovery: the queue returning to empty means a person acted, which is itself
worth knowing. The message bodies carry the shard and sequence-number range of
the failed batch rather than the records themselves, so finding out what was
lost means reading the Call Event Processor's log group for the same period.

**The concurrent-streams threshold cannot read your quota.** Amazon Transcribe's
default account quota is 25 concurrent streaming sessions, and
`TranscribeConcurrentStreamAlarmThreshold` defaults to 20 — eighty per cent of
it, which leaves time to request an increase before meetings start being
refused. If you raise the quota, raise this parameter with it. There is no API
the alarm can consult, so left at 20 against a quota of 200 it notifies at a
level that is no longer close to anything. The count is account-wide and
region-wide, so other Transcribe workloads in the same account consume the same
quota, and LMA opens one streaming session per audio channel, which means a
stereo meeting counts as two.

**A fresh install produces one `stale-meeting-reaper-not-invoked` alert.** That
alarm watches for the *absence* of invocations, so it treats missing data as
breaching. CloudWatch treats an evaluation range with no data at all as
breaching regardless of how many evaluation periods are configured, so a stack
that has just been created notifies once and then clears as soon as the reaper's
first scheduled run lands. This is expected and needs no action. When
`MeetingInactivityTimeoutInMinutes` is `0`, the reaper's schedule rule is left
in place but disabled on purpose, and the alarm is not created at all — so
turning the reaper off does not leave an alarm permanently red.

**Healthy-target count and task count are a pair.** An ALB target group
publishes `HealthyHostCount` only while it has registered targets, so when the
transcriber service has no tasks at all the target group publishes nothing and
`transcriber-no-healthy-targets` reads as healthy. `transcriber-service-no-tasks`
is what covers that case. Treat the two as one signal: either firing means new
meetings are being refused.

**`vp-launch-failures` watches the launch, not the meeting.** It reads the Step
Functions state machine that starts a Virtual Participant, so it tells you a bot
was asked for and did not start. It cannot tell you that a bot started, joined,
and then left early — ECS publishes no metric for a task that stopped
unexpectedly, and the launch-and-admission detail lives in the Virtual
Participant's own log group and meeting record.

## Subscribing to the alerts topic

The stack creates one SNS topic, encrypted with the deployment's
customer-managed KMS key. Its ARN and a console link are stack outputs:

- `SNSAlertsTopicARN`
- `SNSAlertsTopicConsoleUrl`

An email subscription for the `AdminEmail` address is created with the stack.
**SNS email subscriptions start in `PendingConfirmation` and deliver nothing
until confirmed.** SNS sends a message titled "AWS Notification - Subscription
Confirmation" to that address, with a link that must be clicked; until somebody
does, every alarm can fire and nobody will hear about it. Check the
subscription's status on the topic's **Subscriptions** tab in the console — if it
reads `PendingConfirmation`, look for that email, including in a spam folder, and
use **Request confirmation** on the subscription to send it again if it has been
lost. The confirmation link expires after three days.

A **headless or CI deployment therefore starts with no working subscriber**, even
though the subscription resource exists: nobody is reading the mailbox that was
asked to confirm. If you deploy from a pipeline and want alerts, add a
subscription after deployment that needs no human confirmation — an HTTPS
endpoint, a Lambda function, or an SQS queue are all confirmed automatically —
rather than relying on the email one.

### Alerting a team rather than one person

The `AdminEmail` subscription is a starting point, not a design. One person's
mailbox is a single point of failure for every alert this stack raises, and it is
also an address that eventually belongs to somebody who has left. Point the topic
at something the team owns:

- **A distribution list or shared mailbox.** Add a second email subscription for
  the alias and confirm it from the shared mailbox. Both subscriptions can
  coexist; remove the individual one once the alias is confirmed.
- **A chat channel.** AWS Chatbot subscribes to an SNS topic and posts to Slack
  or Microsoft Teams, which also gives you a visible history of what fired and
  when. Configure it against the `SNSAlertsTopicARN` output.
- **An on-call system.** PagerDuty, Opsgenie and similar accept an SNS
  subscription over HTTPS. Those subscriptions confirm themselves, so they work
  for a pipeline-deployed stack.
- **A queue, for anything that should be processed rather than read.** An SQS
  subscription lets you fan out to your own tooling without giving that tooling
  permission to the topic.

Because the topic is encrypted with the customer-managed key, anything you give
permission to publish to it also needs `kms:GenerateDataKey*` and `kms:Decrypt`
on that key. Subscribers do not: decryption happens before delivery.

### Turning the alarms off

`EnableOperationalAlarms` defaults to `true`. Setting it to `false` removes all
of the alarms on a stack update and leaves the topic and the dashboard in place,
which is the right choice if your organisation manages alarms centrally and
would otherwise have two systems notifying for the same event. It removes the
notifications rather than the visibility — the dashboard still works, because it
reads the same service-published metrics directly.

## The dashboard

One dashboard is created per deployment, named `<stack-name>-<region>`. Its name
and a console link are stack outputs:

- `OperationalDashboardName`
- `OperationalDashboardConsoleUrl`

Widget titles are written to say what the number means where a bare number would
mislead. Reading top to bottom:

| Widget | What it shows | Read it for |
|---|---|---|
| Transcribe concurrent streaming sessions | `ConcurrentStreamsCount`, with a horizontal annotation at the alarm threshold | How close the account is to the streaming quota. Account-wide, so a spike you cannot explain may belong to another workload. |
| Kinesis call data stream | `IncomingRecords` and `WriteProvisionedThroughputExceeded` | Whether the pipeline is accepting writes. Record volume alone tells you how busy the deployment is, not whether it is healthy; rejected writes are the signal. |
| Consumer lag | Stream `GetRecords.IteratorAgeMilliseconds` and the Call Event Processor's event-source `IteratorAge`, annotated at the alarm threshold | How far behind the live meeting the transcript is. Two series because the stream's own age is published only for polling readers, and LMA reads through an enhanced fan-out consumer whose lag Lambda reports instead. |
| Discarded records queue | `ApproximateNumberOfMessagesVisible`, annotated at zero | Open work. Depth never falls on its own, so any value above zero is a batch that needs investigating, not a backlog that will clear. |
| Call Event Processor | `Invocations`, `Errors`, `Throttles`, `ConcurrentExecutions` and `Duration` p99 | The health of the one consumer between Kinesis and the web UI. Rising duration with flat invocations usually precedes rising lag. |
| Amazon Bedrock | `Invocations`, `InvocationThrottles`, `InvocationClientErrors` and `InvocationLatency` p99 | Whether summaries and the Meeting Assistant are getting through. Read without a model dimension on purpose: Bedrock publishes these both ways, and a request throttled before the model is resolved carries no model id, so filtering by model undercounts. |
| ECS running tasks | `RunningTaskCount` for the transcriber service and the Virtual Participant cluster | Capacity. The Virtual Participant cluster is empty by design under the default `VPLaunchType=MICROVM`, where participants run on Lambda MicroVMs rather than ECS tasks — zero there is not a fault. |
| Virtual Participant memory used | `MemoryUtilized` for the VP task definition family, peak and average | Whether a participant stuck in `INITIALIZING` is on a starved host. [Troubleshooting](troubleshooting.md) describes this check; the widget is the same query without the console navigation. |
| Transcriber target health | `HealthyHostCount` and `UnHealthyHostCount` | Whether new meetings can connect at all. Zero healthy targets refuses every new connection. |
| Transcriber load balancer | `TargetResponseTime` p99 and `HTTPCode_Target_5XX_Count` | Errors at the edge. Response time is of limited use here: a meeting is one long-lived WebSocket, so what is being timed is the upgrade handshake, not the meeting. |
| GraphQL API | `4XXError`, `5XXError` and `Latency` p99 | What the web UI is experiencing. 4XX is usually an expired client token and is not alarmed; 5XX is a failing resolver and is. |
| Application metrics | Nothing yet — a reserved band | Per-meeting counts, transcript-segment latency and assistant turn latency will appear here once the application publishes them. |
| Four error-log tables | The most recent 50 lines matching `/ERROR/` in the Call Event Processor, the Virtual Participant launch state machine, the Virtual Participant container and the WebSocket transcriber container log groups | A first look without leaving the dashboard. These are CloudWatch Logs Insights queries, so they run on demand and are billed per query, not continuously. |

The dashboard is created whether or not `EnableOperationalAlarms` is `true`.
CloudWatch includes three dashboards per account at no charge; beyond that
dashboards are billed monthly, which is worth knowing if you run several LMA
stacks in one account.

## Related documentation

- [Troubleshooting](troubleshooting.md) — log group names, console navigation, and the symptom-by-symptom guide
- [CloudFormation Parameters Reference](cloudformation-parameters.md) — every stack parameter, including the two added for alarms
- [Well-Architected Framework Assessment](well-architected.md) — where this fits in the Operational Excellence pillar
- [LMA Load Simulator](../utilities/load-simulator/README.md) — generating load to see these metrics move
