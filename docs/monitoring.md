---
title: "Monitoring & Alerting"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Monitoring & Alerting

LMA deploys an SNS topic for operational alerts, 14 alarms that notify it, and
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

The alarms fall into two groups, and the difference matters when you are reading
one.

Twelve of them read a metric that an AWS service publishes on its own. None of
those depend on LMA emitting anything, which means they work on a deployment
that has never been touched and they keep working if application code changes.
They cover infrastructure health well and application behaviour only indirectly:
they can tell you the Call Event Processor is failing, but not that a particular
meeting produced no transcript.

The remaining two read metrics LMA publishes itself — see
[Metrics LMA publishes](#metrics-lma-publishes) below. Both watch a condition the
code otherwise swallows: a failure that is caught, recorded and not raised, so
no AWS-published metric moves and nothing else would ever mention it. That is
exactly why each is an alarm rather than only a graph. The cost of reading an
application metric is that it depends on application code: if a component stops
publishing, its alarm reads `OK`. Both treat missing data as not breaching, so
an alarm with no data at all looks exactly like one that is simply quiet and
will not be the thing that tells you. A build-time gate checks the wiring that
would cause that, and the dashboard band note says how to tell the two apart by
eye.

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
| `kinesis-consumer-iterator-age` | The consumer is more than 5 minutes behind the stream in two of the last three 5-minute windows (`Maximum`, `> 300000` ms). | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `kinesis-write-throughput-exceeded` | A write to the call data stream was rejected for exceeding shard throughput (`Sum` over 5 min, `> 0`). | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `transcriber-no-healthy-targets` | The transcriber load balancer has no target passing its health check, for two consecutive 1-minute windows (`Minimum`, `< 1`). Also notifies on recovery. | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `transcriber-service-no-tasks` | The transcriber ECS service is running no tasks, for two consecutive 5-minute windows (`Minimum`, `< 1`). Also notifies on recovery. | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `appsync-5xx-errors` | The GraphQL API returned five or more server errors (`Sum` over 5 min, `>= 5`). | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `transcribe-concurrent-streams-near-quota` | Concurrent Amazon Transcribe `StartStreamTranscription` sessions in this region reach the configured level (`Maximum` over 5 min, `>= threshold`). | `AlertsTopic` | `TranscribeConcurrentStreamAlarmThreshold` (`20`) |
| `stale-meeting-reaper-not-invoked` | The stale-meeting reaper recorded no invocations in each of two consecutive hours (`Sum`, `<= 0`). Also notifies on recovery. Not created at all when the reaper is switched off. | `AlertsTopic` | `MeetingInactivityTimeoutInMinutes` (`240`; `0` removes the alarm) |
| `bedrock-throttling` | Amazon Bedrock throttled five or more invocations (`Sum` over 5 min, `>= 5`). | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `vp-launch-failures` | The Virtual Participant launch state machine failed or timed out an execution in any of the last three 5-minute windows (`Sum`, `>= 1`). | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `transcript-segment-write-failures` | One or more transcript segments failed to be written in any of the last three 5-minute windows (`Sum` of `TranscriptSegmentWriteFailed`, `>= 1`). Reads a metric LMA publishes. | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |
| `meetings-without-transcript` | Three or more audio sessions streamed for at least thirty seconds and produced no transcript at all, in any of the last three 5-minute windows (`Sum` of `StreamingSessionsWithoutTranscript`, `>= 3`). Reads a metric LMA publishes. | `AlertsTopic` | `EnableOperationalAlarms` (`true`) |

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
level that is no longer close to anything. LMA opens one streaming session per
audio channel, which means a stereo meeting counts as two.

**What that alarm counts, exactly.** Amazon Transcribe publishes
`ConcurrentStreamsCount` under a single dimension set — `Domain`, `ServiceType`
and `Operation` together — so the alarm is scoped to one operation,
`StartStreamTranscription`, which is the one LMA calls. The count is still
account-wide and region-wide: another workload in the same account calling
`StartStreamTranscription` is included. A workload calling
`StartCallAnalyticsStreamTranscription` or `StartMedicalStreamTranscription`
consumes the same quota but publishes a separate series, so it is *not*
included — if you share the account with such a workload, the alarm will
under-report how close you are. Covering every operation at once would need a
`SEARCH` expression, and CloudWatch does not accept `SEARCH` in an alarm, so
enumerating one operation is the only option. The dashboard's widget has the
same scope.

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

**`transcript-segment-write-failures` is the only notice that a transcript has a
gap.** The Call Event Processor issues the writes for a batch of segments
together and collects the results, including the failures, rather than raising
on them. That is deliberate — one rejected segment should not replay the whole
Kinesis batch — but the consequence is that the record is **not redelivered**,
which is what sets these failures apart from every other failure in the
pipeline. A raised failure is attributed to its record, retried, and finally
described to the event source's on-failure destination, where the
discarded-records-queue-depth alarm picks it up. This one reaches neither on its
own account, so that queue stays empty. It is redelivered only if some other
record in the same batch raises, which redelivers everything after it.

`AWS/Lambda Errors` does not move either way: a raised record failure still
leaves the invocation returning a partial-batch response normally, so that
metric cannot be used to tell the two apart. The transcript simply has a line
missing, on a meeting page with no error on it.

If this alarm fires, read the Call Event Processor's log group for `max retries
on query` over the same window; each of those lines carries the exception type
and the GraphQL `errorType`. Search for that string rather than the handler's
`event processor error`, which reports only failures that were raised — and the
point of this metric is that these were not, so grepping for it finds nothing
in the window that fired. Segments already lost cannot be replayed once their
Kinesis records pass the stream's 24-hour retention, so a sustained rate
is urgent in a way a single occurrence is not.

**`meetings-without-transcript` deliberately ignores short sessions.** The
transcriber counts a session only if it ran for at least thirty seconds and
produced nothing, because below that the overwhelmingly common case is somebody
who started streaming and changed their mind. The alarm then needs three in a
five-minute window, so one genuinely odd meeting does not page anybody while a
pattern does. The usual causes, in the order worth checking: the Amazon
Transcribe concurrent-stream quota being exhausted, which
`transcribe-concurrent-streams-near-quota` should have notified first; a
language code or custom vocabulary the account does not have; and, where the
MicroVM ASR engine is selected on the Transcription Engine page, an engine that
could not start with fallback to Amazon Transcribe switched off.

One case it does not cover: a transcriber task replaced mid-meeting — by a
deployment, or by scale-in — shuts down without running the per-meeting
teardown that emits this metric, so meetings it was carrying are not counted
however little they transcribed. That is the right split of responsibility
rather than a gap to work around: a meeting cut short by the infrastructure is
what `transcriber-service-no-tasks` and the ECS task-count widget are for, and
counting it here would report a transcription fault for something that was not
one.

**Why there is no alarm on how long a transcript takes to appear.**
`TimeToFirstTranscriptSegmentMs` is the obvious thing to alarm on and the wrong
thing. It measures from a meeting's audio session opening to its first
transcript segment, and what dominates that interval is when somebody first
speaks — so a meeting that starts five minutes before anybody talks produces a
five-minute value, and a threshold low enough to catch a real problem would fire
on every quiet meeting. The failure worth alarming on is "no transcript at all",
which `meetings-without-transcript` counts directly. The latency is graphed
instead, where its distribution is informative and a single value is not.

## Metrics LMA publishes

Two of the alarms above, and the bottom band of the dashboard, read metrics the
application emits rather than metrics an AWS service emits. There are seven of
them, and they answer one question end to end: a meeting was asked for — did a
transcript come out of it, how long did that take, and was anything dropped on
the way?

They are published in the CloudWatch namespace **`<stack-name>`** — the name of
the top-level LMA stack, so two deployments in one account never mix. Each
emitting component is told that namespace by a `METRIC_NAMESPACE` environment
variable, which is what makes the dashboard and the alarms work on any stack
name without being edited.

| Metric | Unit | Published by | What it means |
|---|---|---|---|
| `MeetingsStarted` | Count | Call Event Processor | A meeting record was created. Counted once per meeting: the Kinesis stream redelivers routinely, and a redelivered start event finds the record already there and is not counted again. |
| `MeetingsEnded` | Count | Call Event Processor | A meeting was marked ended, counted on the write that closed it. Read against `MeetingsStarted` over a long window rather than a short one — a live meeting is legitimately started and not yet ended. |
| `TranscriptSegmentsWritten` | Count | Call Event Processor | Transcript segment writes that AppSync accepted. Counts partial segments as well as final ones, so it tracks pipeline throughput rather than the number of things said. |
| `TranscriptSegmentWriteFailed` | Count | Call Event Processor | Transcript segment writes that failed. Alarmed; see the note above for why nothing else reports these. |
| `StreamingSessionsStarted` | Count | WebSocket transcriber | A live audio session opened for a meeting, and the denominator for the metric below. One per audio session, not per meeting: a client that drops its socket and reconnects for the same meeting opens a second session and is counted again, where `MeetingsStarted` is not. Video sockets are separate and are not counted. Lower than `MeetingsStarted` on a deployment that also transcribes uploaded recordings, which never open a live session. |
| `TimeToFirstTranscriptSegmentMs` | Milliseconds | WebSocket transcriber | Milliseconds from a meeting's audio session opening to its first transcript segment reaching Kinesis, emitted once per audio session, so a client that reconnects produces a second value measured from the reconnect. Graphed, not alarmed; see the note above. |
| `StreamingSessionsWithoutTranscript` | Count | WebSocket transcriber | A session closed having produced no transcript segment at all, after running long enough that silence is a fault. Alarmed. |

### How they are published, and what that implies

The records are written in [CloudWatch's embedded metric
format](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format.html):
a line of JSON in the emitting component's own log group, which CloudWatch Logs
turns into a metric at ingestion. Nothing calls `PutMetricData`, and no IAM role
gained `cloudwatch:PutMetricData` — AWS documents the embedded format as needing
only the log-writing permission these components already have. Three
consequences are worth knowing before you read a graph:

- **The log group is the audit trail.** Every datapoint is still a log event, so
  a number you do not believe can be traced to the record that produced it with
  a Logs Insights query over the Call Event Processor's or the transcriber's log
  group, filtering on the metric's name.
- **A count can be slightly out, in either direction.** AWS documents the
  format as guaranteeing at-least-once delivery with occasional duplicates, so
  any of these can read high. Two have their own reasons to drift.
  `MeetingsStarted` can read low: it skips a start event that finds the meeting
  already stored, and a first delivery whose own earlier attempt stored the
  meeting and then failed in transit is indistinguishable from a redelivery.
  `TranscriptSegmentsWritten` can read high: when one record in a Kinesis batch
  raises, every record after it is redelivered, and segments already written by
  those later records are written and counted again. None of these is a billing
  input, so the trade is the right one — but do not read either as an exact
  census.
- **Datapoints can arrive a little late**, because the log has to be flushed and
  ingested first, and neither Lambda nor the ECS `awslogs` driver lets the
  application control when. Both alarms on these metrics therefore need only one
  breaching period out of the last three, which is what AWS recommends for this
  case: it absorbs a datapoint landing in the period after the one it belongs to
  without missing the event.

### What is not published yet

The Virtual Participant, the MicroVM ASR engine and the Meeting Assistant
publish nothing. That means there is still no metric for the fraction of
Virtual Participant joins that fail and on which trigger, for how often ASR
falls back to Amazon Transcribe, or for assistant and Bedrock latency and
failure rates. Those remain diagnosable only from the log groups
[Troubleshooting](troubleshooting.md) names. They are a separate piece of work,
deliberately: the Virtual Participant's exit-reason taxonomy wants a metric
dimension, and whether its value space is closed enough to be one needs
checking before it becomes a billed dimension on a custom metric.

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
on that key. Subscribers do not: decryption happens before delivery. The stack's
key policy already grants CloudWatch those two actions, which is what lets an
alarm action reach an encrypted topic. The grant is not narrowed by a condition
key, because AWS does not document CloudWatch populating `aws:SourceAccount` or
`aws:SourceArn` when it calls KMS on an alarm action's behalf, and a condition
that did not match would drop every notification silently. If you deploy inside
an AWS Organization and want that statement narrowed, `aws:SourceOrgID` is the
condition key AWS documents for it.

### Turning the alarms off

`EnableOperationalAlarms` defaults to `true`. Setting it to `false` removes all
of the alarms on a stack update and leaves the topic and the dashboard in place,
which is the right choice if your organisation manages alarms centrally and
would otherwise have two systems notifying for the same event. It removes the
notifications rather than the visibility — the dashboard still works, because it
reads the same service-published metrics directly.

One consequence worth expecting: because the topic and its subscription are
created either way, a deployment with `EnableOperationalAlarms=false` still
sends the `AdminEmail` address a subscription-confirmation message, for a topic
that nothing will publish to. Confirming it anyway is the better choice — it
means switching the alarms back on starts delivering immediately, rather than
needing a second round trip through somebody's inbox.

### What it costs

In us-east-1, a standard-resolution CloudWatch alarm costs $0.10 per alarm
metric per month, with the first ten alarm metrics included at no charge. **An
alarm built on a metric-math expression bills per metric referenced in the
expression, not per alarm**, so the 14 alarms here are 16 billable alarm metrics
— twelve reading a single metric each, plus two metric-math alarms reading two
metrics each. That is $0.60 per month for an account with the free allowance
still available, and $1.60 per month for one that has already used it elsewhere.

The seven metrics LMA publishes are **custom metrics**, which are billed
separately from the alarms that read them: $0.30 per metric per month in
us-east-1 for the first 10,000, so $2.10 per month per stack, with the first ten
custom metrics in an account included at no charge. None of them carries a
dimension, which is the thing to keep an eye on if you add more: a custom metric
is billed per unique combination of name and dimension values, so a dimension
with a hundred values is a hundred metrics. Their records are also log events,
so they add log ingestion and storage — a few hundred bytes per meeting and per
batch of transcript segments, which is immaterial next to the transcript log
volume itself.

The dashboard references 37 metrics, comfortably inside the metric allowance for
a free dashboard, and CloudWatch includes three dashboards per account at no
charge. An account that already has three custom dashboards pays $3.00 per month
for this one, which is the realistic case for anyone running several LMA stacks
in one account. Logs Insights queries in the four error-log widgets are billed
per query when the widget runs, not continuously. See
[Amazon CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) for
other regions and for current rates.

## The dashboard

One dashboard is created per deployment, named `<stack-name>-<region>`. Its name
and a console link are stack outputs:

- `OperationalDashboardName`
- `OperationalDashboardConsoleUrl`

Widget titles are written to say what the number means where a bare number would
mislead. Reading top to bottom:

| Widget | What it shows | Read it for |
|---|---|---|
| Transcribe concurrent streaming sessions | `ConcurrentStreamsCount` for `StartStreamTranscription`, with a horizontal annotation at the alarm threshold | How close the account is to the streaming quota. Account-wide, so a spike you cannot explain may belong to another workload — but only one that calls the same operation; see the note above on what this series excludes. |
| Kinesis call data stream | `IncomingRecords` and `WriteProvisionedThroughputExceeded` | Whether the pipeline is accepting writes. Record volume alone tells you how busy the deployment is, not whether it is healthy; rejected writes are the signal. |
| Consumer lag | `SubscribeToShardEvent.MillisBehindLatest` for the stream's enhanced fan-out consumer, and the Call Event Processor's event-source `IteratorAge`, annotated at the alarm threshold | How far behind the live meeting the transcript is. Two series reporting the same lag by different routes, so either alone is enough. Note the *stream-level* `GetRecords.IteratorAgeMilliseconds` is deliberately not shown: LMA reads the stream through an enhanced fan-out consumer, which uses `SubscribeToShard`, so that metric reflects whatever else happens to poll the stream rather than this consumer's lag. |
| Discarded records queue | `ApproximateNumberOfMessagesVisible`, annotated at zero | Open work. Depth never falls on its own, so any value above zero is a batch that needs investigating, not a backlog that will clear. |
| Call Event Processor | `Invocations`, `Errors`, `Throttles`, `ConcurrentExecutions` and `Duration` p99 | The health of the one consumer between Kinesis and the web UI. Rising duration with flat invocations usually precedes rising lag. |
| Amazon Bedrock | `Invocations`, `InvocationThrottles`, `InvocationClientErrors` and `InvocationLatency` p99 | Whether summaries and the Meeting Assistant are getting through. Read without a model dimension on purpose: Bedrock publishes these both ways, and a request throttled before the model is resolved carries no model id, so filtering by model undercounts. |
| ECS tasks | `RunningTaskCount` for the transcriber *service*, and cluster-wide `TaskCount` for the Virtual Participant cluster | Capacity. Two different metrics because `RunningTaskCount` is only published per service: the Virtual Participant cluster starts a task per meeting with `RunTask` rather than through a service, so it has no `RunningTaskCount` series under any `VPLaunchType` — including `FARGATE` and `EC2`, where the service that does exist holds `DesiredCount: 0` and only keeps a load-balancer target-group association. `TaskCount` is the cluster-wide metric and does count those per-meeting tasks. It is legitimately zero whenever no Virtual Participant is in a meeting, and permanently zero under the default `VPLaunchType=MICROVM`, where participants run on Lambda MicroVMs and are not ECS tasks at all. |
| Virtual Participant memory used | `MemoryUtilized` for the VP task definition family, peak and average | Whether a participant stuck in `INITIALIZING` is on a starved host. [Troubleshooting](troubleshooting.md) describes this check; the widget is the same query without the console navigation. |
| Transcriber target health | `HealthyHostCount` and `UnHealthyHostCount` | Whether new meetings can connect at all. Zero healthy targets refuses every new connection. |
| Transcriber load balancer | `TargetResponseTime` p99 and `HTTPCode_Target_5XX_Count` | Errors at the edge. Response time is of limited use here: a meeting is one long-lived WebSocket, so what is being timed is the upgrade handshake, not the meeting. |
| GraphQL API | `4XXError`, `5XXError` and `Latency` p99 | What the web UI is experiencing. 4XX is usually an expired client token and is not alarmed; 5XX is a failing resolver and is. |
| Application metrics (band header) | A note naming the namespace these widgets read | Everything below this line comes from LMA rather than from an AWS service. If a series below is empty while meetings are running, the component is not publishing rather than nothing having happened — check that `METRIC_NAMESPACE` is set on it. |
| Meetings | `MeetingsStarted`, `MeetingsEnded` and `StreamingSessionsStarted` | Whether meetings are happening, and whether they are finishing. Started and ended will not match over a short window, because a live meeting has started and not ended; a persistent gap over hours is meetings that never closed, which is what the stale-meeting reaper exists for. Live audio sessions are lower than meetings started on a deployment that also transcribes uploaded recordings. |
| Time to first transcript segment | `TimeToFirstTranscriptSegmentMs` at minimum, average, p90 and p99 | How long a meeting waits for its transcript to begin. Read the shape, not a value: the interval is dominated by when somebody first speaks, so the minimum is the closest thing here to a measure of the pipeline itself, and a rising p90 with a flat minimum usually means meetings are starting earlier rather than the pipeline slowing. |
| Transcript segment writes | `TranscriptSegmentsWritten` and `TranscriptSegmentWriteFailed`, annotated at the alarm threshold | Whether the transcript is reaching the meeting page. Any value on the failure series is a line that is simply missing, with no error on the meeting page and nothing on the discarded-records queue. |
| Audio sessions that produced no transcript | `StreamingSessionsWithoutTranscript` against `StreamingSessionsStarted`, annotated at the alarm threshold | Sessions that streamed audio and got nothing back. Sessions under thirty seconds are not counted, so a value here is a session that ran long enough for silence to be a fault rather than a user who changed their mind — though a muted room or a meeting abandoned at minute two still lands here, which is why the alarm needs three in a window. Read against sessions opened: one in fifty is a different problem from one in two. |
| Four error-log tables | The most recent 50 lines matching `/ERROR/` in the Call Event Processor, the Virtual Participant launch state machine, the Virtual Participant container and the WebSocket transcriber container log groups | A first look without leaving the dashboard. These are CloudWatch Logs Insights queries, so they run on demand and are billed per query, not continuously. |

The dashboard is created whether or not `EnableOperationalAlarms` is `true`.
See [What it costs](#what-it-costs) above for the dashboard and alarm charges.

## Related documentation

- [Troubleshooting](troubleshooting.md) — log group names, console navigation, and the symptom-by-symptom guide
- [CloudFormation Parameters Reference](cloudformation-parameters.md) — every stack parameter, including the two added for alarms
- [Well-Architected Framework Assessment](well-architected.md) — where this fits in the Operational Excellence pillar
- [LMA Load Simulator](../utilities/load-simulator/README.md) — generating load to see these metrics move
