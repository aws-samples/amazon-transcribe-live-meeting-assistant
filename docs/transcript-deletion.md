---
title: "Transcript Deletion After the Summary"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Transcript Deletion After the Summary

Some organisations need a meeting's transcript to exist only while it is being
used: to power the meeting assistant during the meeting and to write the
summary at its end. LMA can delete each meeting's transcript permanently once
the meeting is summarized, either immediately or 15, 30 or 60 minutes later,
and keep the summary.

This is a stronger guarantee than a short retention period. The **Days to keep
transcript segments** setting gives each segment a DynamoDB time to live, and
DynamoDB removes expired items in the background, typically within a few days
of their expiry rather than at it. Deletion after the summary does not rely on
that: it deletes the transcript at the minute it is due.

- [Turning it on](#turning-it-on)
- [When the transcript is deleted](#when-the-transcript-is-deleted)
- [What is deleted and what is kept](#what-is-deleted-and-what-is-kept)
- [What users see](#what-users-see)
- [How it is carried out](#how-it-is-carried-out)
- [Copies outside the transcript store](#copies-outside-the-transcript-store)
- [Limitations](#limitations)

## Turning it on

The choice is the **Retention › Delete the transcript after the summary**
setting on the Configuration page, which an Admin can change at any time. It
ships set to Off. The choices are:

| Choice | Effect |
|---|---|
| Off (the default) | The transcript is kept for **Days to keep transcript segments**, as before. |
| Immediately | The transcript is deleted as soon as the summary is stored. |
| 15, 30 or 60 minutes after the summary | The transcript is deleted that long after the summary is stored, which leaves time to read or download it. |

A saved change applies to the next meeting that ends: the deletion reads the
setting as saved each time a meeting ends or is summarized. A meeting that has
already ended keeps the deletion time it was given, and a meeting that ended
while the setting was off keeps its transcript for the days set above.

## When the transcript is deleted

- **When the summary is stored**, the delay starts counting. With
  **Immediately**, the transcript is deleted at that point.
- **If no summary has been stored 60 minutes after the meeting ends** (the
  summary failed, or is still running), the delay counts from then instead.
  The transcript is deleted whether or not a summary was ever written.
- **On a deployment with no end-of-meeting summary** (`EndOfCallTranscriptSummary`
  set to `LAMBDA` with no `EndOfCallLambdaHookFunctionArn`), the delay counts
  from the end of the meeting.
- **After a deletion made immediately**, the meeting is checked once more ten
  minutes later. That removes anything written for the meeting after the
  deletion, such as a meeting assistant answer that was still being written or
  a segment the speech engine corrected after the meeting ended.

A scheduled deletion runs at its time even if the setting has since been
turned off. Participants may have been told the transcript would be deleted,
and turning the setting off does not undo that promise for meetings that have
already ended.

## What is deleted and what is kept

Deleted, for the meeting:

- every turn of the transcript, partial or final, from the meeting's
  transcript records in DynamoDB;
- every meeting assistant answer stored with the transcript;
- the meeting's **Issues detected** field, which quotes the transcript (it is
  set only by Transcribe Call Analytics);
- the transcript file in the recordings bucket that the summary step writes for
  the transcript knowledge base, its metadata file, and, for an uploaded
  meeting, the Amazon Transcribe output file. Every version of each file is
  deleted, not only the current one;
- those files' entries in the transcript knowledge base, when the deployment
  has one.

Kept:

- the summary, on the meeting and in the transcript knowledge base, so the
  meeting can still be found and asked about;
- the meeting record: its title, times, participants, agenda, speaker names,
  categories and sentiment;
- category markers, which hold only a category's name;
- the audio and video recordings, which follow `AudioRecordingExpirationInDays`.
  Turn **Record meeting audio** off on the Configuration page if the audio must
  not be kept either;
- shared-screen key frames and the text read from them, which follow
  **Days to keep transcript segments**;
- the consent record, which is the evidence that participants were told.

## What users see

While deletion is pending, the meeting page shows above the transcript when it
will be deleted, so anyone who needs a copy can download it first. Once it is
deleted, the page says when, and the transcript download is no longer offered.

After the deletion:

- **Regenerate** on the summary is refused, because a new summary would be
  written from no transcript and replace the one that was kept. Editing the
  summary by hand still works.
- The meeting assistant has no transcript of the meeting to read. Questions
  about past meetings are answered from their summaries.
- The meeting is counted the same way in the meetings list and in usage and
  cost records.

## How it is carried out

The call event processor tells the transcript deletion function when each
meeting ends and when its summary is stored. The function either deletes the
transcript there and then, or creates a one-time
[EventBridge Scheduler](https://docs.aws.amazon.com/scheduler/latest/UserGuide/what-is-scheduler.html)
schedule that invokes it at the minute the deletion is due. The schedule
deletes itself once it has run. A deletion is only ever moved earlier, never
later, so a repeated end-of-meeting event cannot postpone it. The time each
meeting is due is written onto the meeting record, as
`TranscriptDeletionScheduledFor`.

When the deletion has run, the meeting record also carries
`TranscriptDeletedAt`, `TranscriptSegmentsDeleted` and
`TranscriptFilesDeleted`. Together these are a record that the deletion
happened, readable through `getCall`.

Every step is safe to repeat. A deletion that fails is retried: by Lambda for
an asynchronous invocation, and by EventBridge Scheduler for a schedule, for up
to 24 hours. A deletion that still has not completed is kept on a dead-letter
queue, and the `transcript-deletion-dead-letter-queue-depth` alarm notifies the
operational alerts topic (see [Monitoring & Alerting](monitoring.md)). The
alarm's description says how to run the deletion again. The usual cause is a
recordings bucket the stack did not create, with S3 Object Lock or a bucket
policy that does not allow the deletion.

As a backstop, while deletion is on each transcript segment is also written
with a DynamoDB time to live of at most two days. If a deletion never ran at
all, DynamoDB still removes the segments, on its own schedule.

## Copies outside the transcript store

The deletion removes the transcript from where LMA stores it. Copies also pass
through these places, each with its own lifetime:

| Where | How long | What to set |
|---|---|---|
| The Kinesis call data stream | 24 hours, the shortest Kinesis allows | Nothing; records age out on their own |
| The meetings table's DynamoDB stream | 24 hours | Nothing; records age out on their own |
| The meetings table's point-in-time recovery | 35 days: deleted items can be restored from a backup until then | Point-in-time recovery is a safety protection and stays on. If your policy forbids restorable copies, raise it with whoever owns the deployment's backup policy |
| Lambda and container logs | `CloudWatchLogsExpirationInDays` | Keep `LogLevel` at `INFO` or above. At `DEBUG`, functions log the transcript text they handle |
| The GraphQL API's log group, `/aws/appsync/apis/<api id>` | `CloudWatchLogsExpirationInDays` | Keep `AppSyncFieldLogLevel` at `ERROR` (the default) or `NONE`. At `DEBUG` or `ALL`, the API logs each request's content, transcript segments included |
| Amazon Bedrock model invocation logging | As configured for the account | Off by default. If the account has it on, every prompt is logged, including summary prompts that contain the whole transcript |
| The summary | Kept with the meeting | Summary prompts can be written not to quote the transcript; see [Transcript Summarization](transcript-summarization.md) |

## Limitations

- The setting applies to the whole deployment. There is no per-meeting choice.
- The delay is counted to the minute: EventBridge Scheduler runs a one-time
  schedule within the minute it is set for.
- A meeting that ended before the setting was turned on keeps its transcript
  for **Days to keep transcript segments**.
- With a recordings bucket the stack did not create, the transcript files are
  only deleted if the bucket allows it.
- A transcript knowledge base the stack did not create is asked to remove the
  files' entries. If it does not ingest from the recordings bucket, there is
  nothing to remove.
