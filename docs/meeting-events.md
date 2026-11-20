---
title: "Meeting Lifecycle Events"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Meeting Lifecycle Events

LMA publishes an event to an Amazon EventBridge event bus of its own when a
meeting starts, when it ends, when its summary is stored, and when a Virtual
Participant's status changes. A system that follows up meetings can add a rule
to the bus and receive these as they happen, instead of polling
`list_meetings` to find out.

The events carry identifiers and states only. They never carry a transcript, a
summary, an agenda, the meeting context, the checklist, a meeting name, or any
participant's name or email address. A consumer that wants the content reads it
through the API it would use anyway, under that API's own access rules.

## Table of Contents

- [The event bus](#the-event-bus)
- [The events](#the-events)
- [The detail](#the-detail)
- [Virtual Participant statuses](#virtual-participant-statuses)
- [Reopened meetings](#reopened-meetings)
- [Delivery and deduplication](#delivery-and-deduplication)
- [Example: email on Summary Ready for one label](#example-email-on-summary-ready-for-one-label)
- [Polling instead of subscribing](#polling-instead-of-subscribing)
- [The summary-ready notification](#the-summary-ready-notification)
- [Monitoring](#monitoring)
- [How it is built](#how-it-is-built)
- [Related documentation](#related-documentation)

## The event bus

The stack creates a custom event bus named `<stack name>-meeting-events`. It
does not use the account's default bus, so that who may publish to the bus, and
who may add rules to it, can be governed separately from everything else in the
account. Its name and ARN are the stack outputs `MeetingEventBusName` and
`MeetingEventBusArn`; use the outputs rather than building the name.

The stack parameter `EnableMeetingEvents` (default `true`) decides whether the
bus exists. Publishing is on by default because it has no effect until someone
adds a rule: an event that no rule matches goes nowhere, and EventBridge charges
for custom events published (see [Amazon EventBridge
pricing](https://aws.amazon.com/eventbridge/pricing/)), which at a few events
per meeting is a negligible amount. Setting it to `false` removes the bus, the
publisher of Virtual Participant events and every function's permission to
publish. Remove the rules you added to the bus before you set it to `false` or
delete the stack.

Of the roles this stack creates, only those of the call event processor, the
summary regenerator and the Virtual Participant status publisher are granted
`events:PutEvents` on the bus: on this bus alone, with the `events:source`
condition key set to `lma`. Publishing to the bus is governed by IAM in the
stack's account, as for any bus without a resource policy: grant
`events:PutEvents` on it only to what should publish meeting events.

The bus is encrypted with an AWS owned key, as EventBridge encrypts every bus
by default. The events hold no meeting content.

## The events

Every event has `source` `lma` and one of four `detail-type`s:

| `detail-type` | When it is published |
|---|---|
| `Meeting Started` | The meeting record is created and transcription begins, whatever started it: Stream Audio, the browser extension, the desktop app, a Virtual Participant, an uploaded recording or a streaming client. Also when a reopened meeting starts a later session; see [Reopened meetings](#reopened-meetings). |
| `Meeting Ended` | A session's end closes the meeting and the meeting reaches `ENDED`. This includes a meeting ended by the End button, by its Virtual Participant leaving, by the stale meeting reaper, and an uploaded recording finishing. |
| `Summary Ready` | A summary is stored: the end-of-meeting summary, or a regenerated one. Not published when a summary run produced nothing usable, when the meeting had nothing to summarise, or when somebody edits the summary text by hand. |
| `Virtual Participant Status Changed` | A Virtual Participant's status changes, including to `MANUAL_ACTION_REQUIRED`, when it needs a person to solve a CAPTCHA, sign in, or complete a verification step. One event per change; a write that leaves the status as it was publishes nothing. |

`Meeting Ended` is not published again for a meeting that is already `ENDED`,
and `Meeting Started` is not published again when a start is redelivered for a
meeting that already exists. A redelivered summary record can publish `Summary
Ready` a second time, with the same `occurredAt`, which the
[deduplication](#delivery-and-deduplication) below removes.

## The detail

```json
{
  "version": 1,
  "meetingId": "4f7c0a52-...",
  "virtualParticipantId": "b2d9...",
  "owner": "user@example.com",
  "labels": {"project": "acme-claims"},
  "audience": "external",
  "status": "ENDED",
  "summaryProfile": "discovery-session",
  "sessionNumber": 1,
  "vpStatus": "MANUAL_ACTION_REQUIRED",
  "manualActionType": "CAPTCHA",
  "occurredAt": "2026-11-19T16:00:00.000Z"
}
```

No single event carries every field. **A field that does not apply, or is not
known yet, is left out**, never sent as `null` or empty: a meeting with no
labels has no `labels`, a meeting event has no `vpStatus`, and a Virtual
Participant event has no `meetingId` until the Virtual Participant has created
its meeting.

| Field | On | Meaning |
|---|---|---|
| `version` | every event | `1`. A change that removes or redefines a field increases it; a new optional field does not. |
| `meetingId` | every event, once known | The meeting's id (its `CallId`), as `list_meetings`, `get_meeting_summary` and the meeting page use. |
| `virtualParticipantId` | Virtual Participant events, and `Meeting Started` for a meeting a Virtual Participant started | The Virtual Participant's id. A meeting's other events do not carry it; its Virtual Participant events carry both ids. |
| `owner` | every event, once known | The meeting's owner, as stored on the meeting or the Virtual Participant (a user name, usually an email address). |
| `labels` | when the meeting has labels | The meeting's labels as `{key: value}`; see [Meeting Context and Checklist](meeting-context-and-checklist.md). |
| `audience` | when set | `internal` or `external`. |
| `status` | meeting events | The meeting's status when the event was published: normally `STARTED` on `Meeting Started` (an uploaded recording can already read `TRANSCRIBING`), `ENDED` on `Meeting Ended`, and the status as stored on `Summary Ready` (normally `ENDED`; a summary regenerated during a meeting carries the live status). |
| `summaryProfile` | when the meeting has one | The summary profile the meeting is summarised with; absent for the default. See [Summary profiles](transcript-summarization.md#summary-profiles). |
| `sessionNumber` | `Meeting Started`, `Meeting Ended` | The capture session that started or ended: `1` for a meeting's first session, `2` for its first rejoin, and so on. |
| `vpStatus` | Virtual Participant events | The status the Virtual Participant moved to; see [Virtual Participant statuses](#virtual-participant-statuses). |
| `manualActionType` | Virtual Participant events with `vpStatus` `MANUAL_ACTION_REQUIRED` | What kind of help it needs; see below. |
| `occurredAt` | every event | When the transition happened, in UTC (ISO 8601, milliseconds). The event's own EventBridge `time` is set to the same moment. |

For `Meeting Started`, `occurredAt` is when the meeting record was created, or
when the later session started. For `Meeting Ended` it is when the meeting was
marked `ENDED`; for `Summary Ready`, when the summary was stored. For a Virtual
Participant event it is when the Virtual Participant record was written.

## Virtual Participant statuses

`vpStatus` carries the status exactly as LMA stores it on the Virtual
Participant record, the same value the Virtual Participants page shows:

| `vpStatus` | Meaning |
|---|---|
| `SCHEDULED` | Scheduled for a later time and waiting for it. |
| `INITIALIZING` | Being launched; compute is being allocated. |
| `WAITING_FOR_CAPACITY` | Waiting for compute capacity to start on. |
| `BOOTING` | Its container or MicroVM is starting. |
| `REGISTERING_NETWORK` | Registering its network endpoint for the live view. |
| `VNC_READY` | The live view in LMA is ready. |
| `HYDRATING_PROFILE` | Restoring its saved browser profile. |
| `LAUNCHING_BROWSER` | Starting its browser. |
| `WARMING_PROFILE` | Warming up a new browser profile before joining. |
| `CONNECTING` | Connecting to the meeting platform. |
| `JOINING` | Joining, or waiting to be admitted. |
| `JOINED` | Admitted to the meeting. |
| `ACTIVE` | In the meeting and recording. |
| `MANUAL_ACTION_REQUIRED` | Needs a person; see `manualActionType`. |
| `COMPLETED` | Left the meeting normally. Final. |
| `ENDED` | Ended from outside, for example with the End button. Final. |
| `CANCELLED` | A scheduled Virtual Participant was cancelled before it launched. Final. |
| `FAILED` | Could not join or stopped unexpectedly. Final. |

`manualActionType` is one of the values LMA writes today: `CAPTCHA`, `LOGIN`
(a sign-in or verification step, including a second factor), `PASSCODE` (the
meeting passcode was not accepted), `LOGIN_REQUIRED`, `SSO_REDIRECT`, `BLOCKED`
and `OTHER`. Treat the list as open: a platform change can add a value, and a
consumer should handle one it does not recognise as "needs a person".

A Virtual Participant event's `labels`, `audience` and `summaryProfile` are the
ones it was started or scheduled with, which it puts on the meeting it creates.

## Reopened meetings

A meeting can be ended and then continued: its owner or an Admin rejoins it
with a Virtual Participant (see [Virtual Participant](virtual-participant.md)),
which starts a second capture session on the same meeting. Each session
publishes its own pair of events, with the same `meetingId`:

- `Meeting Started` with `sessionNumber` `2` when the second session starts,
  timed by when it started;
- `Meeting Ended` with `sessionNumber` `2` when it ends.

So a consumer that wants "the meeting is over" should act on `Meeting Ended`
and be ready for a later `Meeting Started` for the same meeting; one that wants
"the meeting is over for good" has no such event, because any ended meeting can
be continued. The end of an earlier session that arrives after a later session
has started (a redelivery, or the earlier Virtual Participant still shutting
down) publishes nothing, because it does not end the meeting. The summary of
the continued meeting is stored again when it ends, so `Summary Ready` follows.

## Delivery and deduplication

**From the bus to its targets, delivery is at least once.** EventBridge can
deliver an event to a target more than once, so consumers must deduplicate:

- meeting events on `meetingId` + `detail-type` + `occurredAt` (and
  `sessionNumber`, when present);
- Virtual Participant events on `virtualParticipantId` + `vpStatus` +
  `occurredAt`.

**LMA can publish the same event twice.** A summary record that is redelivered
on the call data stream is stored again and publishes `Summary Ready` again.
It carries the same `occurredAt` as the first, because the time is the
summary run's own, so the deduplication key above removes it.

**Publishing to the bus is best effort.** LMA publishes after the transition is
stored, with a few quick retries, and never fails a meeting because the bus
could not be reached; an unreachable bus adds at most a few seconds to
processing the record that made the transition. An event that still cannot be published is logged
and counted in the `MeetingEventsPublishFailed` metric (see
[Monitoring](#monitoring)), and is not sent again. A consumer that must not miss
a transition reconciles by [polling](#polling-instead-of-subscribing) from time
to time.

**Order is not guaranteed.** Events from different functions can arrive in a
different order from the one they happened in; use `occurredAt`.

## Example: email on Summary Ready for one label

A rule that emails a topic's subscribers whenever a meeting labelled
`project: acme-claims` has its summary stored. The message carries the
meeting's id; the summary itself is read in LMA.

```yaml
Parameters:
  MeetingEventBusArn:
    Type: String
    Description: The LMA stack's MeetingEventBusArn output.

Resources:
  SummaryReadyTopic:
    Type: AWS::SNS::Topic
    Properties:
      KmsMasterKeyId: alias/aws/sns
      Subscription:
        - Protocol: email
          Endpoint: team@example.com

  SummaryReadyTopicPolicy:
    Type: AWS::SNS::TopicPolicy
    Properties:
      Topics: [!Ref SummaryReadyTopic]
      PolicyDocument:
        Statement:
          - Effect: Allow
            Principal:
              Service: events.amazonaws.com
            Action: sns:Publish
            Resource: !Ref SummaryReadyTopic
            Condition:
              ArnEquals:
                aws:SourceArn: !GetAtt SummaryReadyRule.Arn

  SummaryReadyRule:
    Type: AWS::Events::Rule
    Properties:
      EventBusName: !Ref MeetingEventBusArn
      EventPattern:
        source: [lma]
        detail-type: [Summary Ready]
        detail:
          labels:
            project: [acme-claims]
      Targets:
        - Id: email
          Arn: !Ref SummaryReadyTopic
          InputTransformer:
            InputPathsMap:
              meeting: $.detail.meetingId
            InputTemplate: '"The summary of meeting <meeting> is ready in LMA."'
```

The same rule with the AWS CLI:

```bash
aws events put-rule --event-bus-name <MeetingEventBusName> --name summary-ready-acme \
  --event-pattern '{"source":["lma"],"detail-type":["Summary Ready"],"detail":{"labels":{"project":["acme-claims"]}}}'
```

A rule for every person-needed prompt uses `"detail-type": ["Virtual Participant
Status Changed"]` and `"detail": {"vpStatus": ["MANUAL_ACTION_REQUIRED"]}`.

## Polling instead of subscribing

A client that cannot receive EventBridge events, such as a desktop app, can
find most of the same transitions with the MCP tool `list_meetings` (see
[MCP Tools Reference](mcp-tools-reference.md)):

- **Meeting started:** `startDate` (or the default window) and each meeting's
  `startTime`.
- **Meeting ended:** `endedSince`, and each meeting's `status` and `endTime`.
- **Summary ready:** `summaryReady` on each meeting, with `updatedSince`.

Polling does not see everything the events do:

- A **regenerated summary** does not change the meeting's `updatedAt`, so
  `updatedSince` does not find it, and nothing in the answer says a summary was
  replaced.
- A **Virtual Participant's status** is not part of `list_meetings`, and the
  Virtual Participant tools have no "changed since" filter, so a client has to
  read each Virtual Participant's status to notice a change, including one that
  needs a person.
- A **later session** of a reopened meeting starts and ends without
  `list_meetings` saying which session it is.

## The summary-ready notification

When a summary is stored, LMA also publishes an in-app [meeting
notification](meeting-notifications.md) on the meeting, category `meeting`,
type `summary_ready`, titled "The meeting summary is ready" (or "The meeting
summary was regenerated"). It reaches the people who can see the meeting, on
its page, whether or not anything subscribes to the bus, and it is not switched
off with `EnableMeetingEvents`. A regenerated summary updates the same
notification rather than adding a second one. To stop seeing it, mute the
`meeting` category on one meeting or on every meeting from the Notifications
panel.

The notification and the bus are separate mechanisms: the notification is for
people using LMA, the bus is for other systems.

## Monitoring

Two metrics, in the stack's metric namespace, count what is put on the bus:
`MeetingEventsPublished` and `MeetingEventsPublishFailed`. The operational
dashboard graphs both, and the `meeting-events-publish-failures` alarm notifies
the alerts topic when five or more events fail in a 5-minute window. Each
failure is logged by the publishing function with the detail-type, the meeting
or Virtual Participant id, and the error code. See
[Monitoring & Alerting](monitoring.md).

## How it is built

- `Meeting Started`, `Meeting Ended` and the end-of-meeting `Summary Ready` are
  published by the call event processor, which every capture source's start and
  end reaches and which stores the end-of-meeting summary. Each is published on
  the write that made the transition. A redelivered start or end finds the
  transition already made and publishes nothing; a redelivered summary record
  is stored again and publishes again, with the same `occurredAt`.
- A regenerated summary's `Summary Ready` is published by the summary
  regenerator once the new summary is stored.
- `Virtual Participant Status Changed` is published by a small function that
  reads the Virtual Participant table's DynamoDB stream. Several components
  write a Virtual Participant's status (its container, both launch paths, the
  task reaper and the API), and the stream sees each of their writes, so none
  of them publishes for itself.

The Virtual Participant table's stream has two consumers: this publisher and
the Virtual Participant scheduler. DynamoDB recommends no more than two readers
per stream shard, so anything else that needs the stream must share one of
these two functions rather than add a third reader.

## Related documentation

- [Meeting Notifications](meeting-notifications.md)
- [Lambda Hook Functions](lambda-hook-functions.md)
- [MCP Tools Reference](mcp-tools-reference.md)
- [Virtual Participant](virtual-participant.md)
- [Monitoring & Alerting](monitoring.md)
- [CloudFormation Parameters Reference](cloudformation-parameters.md)
