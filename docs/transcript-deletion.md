---
title: "Transcript and Recording Deletion After the Summary"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Transcript and Recording Deletion After the Summary

Some organisations need a meeting's transcript to exist only while it is being
used: to power the meeting assistant during the meeting and to write the
summary at its end. LMA can delete each meeting's transcript permanently once
the meeting is summarized, either immediately or 15, 30 or 60 minutes later,
and keep the summary.

The meeting's audio and video recordings can be deleted the same way, by a
setting of their own. Nothing in LMA reads a recording after the meeting has
ended and been summarized, other than a person playing or downloading it, so a
deployment that keeps the summary and the transcript can still choose not to
keep the recordings, and the other way round. The two settings are independent:
either can be on without the other.

This is a stronger guarantee than a short retention period. The **Days to keep
transcript segments** setting gives each segment a DynamoDB time to live, and
DynamoDB removes expired items in the background, typically within a few days
of their expiry rather than at it. Deletion after the summary does not rely on
that: it deletes the transcript at the minute it is due. In the same way,
deleting the recordings after the summary does not wait for the recordings
bucket's lifecycle rule, which removes recordings after
`AudioRecordingExpirationInDays` days.

See also [Privacy and Data Handling Controls](privacy-and-data-handling.md),
which brings together the consent options, what LMA stores and for how long,
and the deletion controls.

- [Turning it on](#turning-it-on)
- [When the transcript and the recordings are deleted](#when-the-transcript-and-the-recordings-are-deleted)
- [What is deleted and what is kept](#what-is-deleted-and-what-is-kept):
  [the transcript](#the-transcript) and [the recordings](#the-recordings)
- [What users see](#what-users-see)
- [How it is carried out](#how-it-is-carried-out)
- [Copies outside LMA's stores](#copies-outside-lmas-stores)
- [Limitations](#limitations)

## Turning it on

There are two settings in the **Retention** section of the Configuration page,
which an Admin can change at any time:

- **Delete the transcript after the summary** deletes the transcript.
- **Delete the recordings after the summary** deletes the audio and video
  recordings.

Both ship set to Off, and both offer the same choices:

| Choice | Effect |
|---|---|
| Off (the default) | The transcript is kept for **Days to keep transcript segments**, and the recordings for `AudioRecordingExpirationInDays`, as before. |
| Immediately | Deleted as soon as the summary is stored. |
| 15, 30 or 60 minutes after the summary | Deleted that long after the summary is stored, which leaves time to read, play or download it. |

A saved change applies to the next meeting that ends: the deletion reads each
setting as saved each time a meeting ends or is summarized. A meeting that has
already ended keeps the deletion time it was given, and a meeting that ended
while a setting was off keeps what that setting governs for its usual period.
A deployment that is updated to a version with the recordings setting keeps its
recordings as before: the setting starts as Off unless a preset that sets it is
applied.

The configuration presets set both:

| Preset | Delete the transcript after the summary | Delete the recordings after the summary |
|---|---|---|
| **Standard**, **Internal meetings – notify participants** | Off | Off |
| **Regulated – consent required, transcript not retained**, **Regulated – notify with opt-out, transcript not retained** | 15 minutes after the summary | Immediately |
| **Team-hosted (external meetings)**, **Customer account** | Off | Off |

The two Regulated presets also turn **Record meeting audio** and **Record screen
video from streaming clients** off, so under them most meetings have no
recording at all. A recording can still be made: a Virtual Participant records
video unless its **Record meeting video** toggle is off or the stack's
`EnableVideoRecording` parameter is `false`, which no preset can set. Those
presets delete any such recording as soon as the summary is stored, rather than
after the transcript's 15 minutes, because the presets are meant to keep no
recording, and nothing that comes after the summary needs one. The transcript
keeps its 15 minutes, in which it can be read or downloaded.

See [Configuration presets](configuration-guide.md#configuration-presets),
which also lists the retention stack parameters a preset cannot set, and
[Meeting Consent](meeting-consent.md).

## When the transcript and the recordings are deleted

The transcript and the recordings are each deleted by the rules below, counted
from the same events but each with its own setting's delay, so one can be
deleted while the other is kept, or later.

- **When the summary is stored**, the delay starts counting. With
  **Immediately**, the transcript (or the recordings) is deleted at that point.
- **If no summary has been stored 60 minutes after the meeting ends** (the
  summary step failed, or is still running), the delay counts from then
  instead. The transcript is deleted whether or not a summary was ever written.
  Until then an Admin can regenerate a failed summary from the meeting page;
  once the transcript is deleted, Regenerate is refused.
- **On a deployment with no end-of-meeting summary** (`EndOfCallTranscriptSummary`
  set to `LAMBDA` with no `EndOfCallLambdaHookFunctionArn`), the delay counts
  from the end of the meeting.
- **After a deletion made immediately**, the meeting is checked once more ten
  minutes later. That removes anything written for the meeting after the
  deletion, such as a meeting assistant answer that was still being written or
  a segment the speech engine corrected after the meeting ended.

- **When the meeting is reopened** by a Virtual Participant
  [rejoin](virtual-participant.md#rejoin), or continued from the Desktop Capture
  App ([Continue an existing meeting](desktop-capture-app.md#continue-an-existing-meeting)),
  while the setting is on, a pending
  deletion is cancelled once the rejoined session starts capturing, because the
  meeting is being captured again. The rejoined session's end schedules the
  deletion afresh, by the rules above. A summary written while the meeting is
  in progress, such as the earlier session's summary arriving after the
  rejoined session has started, neither deletes the transcript nor schedules
  a deletion; the rejoined session's own summary does. A cancel that reaches the deletion
  function only after that session has ended changes nothing. While the
  setting is off, a rejoin keeps the pending deletion: it runs at its time,
  during the rejoined session if that is still going on, as the next paragraph
  describes. A rejoin whose session never starts (its launch fails, or
  the Virtual Participant is not admitted to the meeting) does not reopen the
  meeting, and the pending deletion runs at its time. A meeting whose
  transcript has already been deleted cannot be reopened, and a deletion that
  runs between the rejoin and the start of its session stops the meeting from
  reopening: the relaunched Virtual Participant is ended. Transcript segments
  that arrive for a meeting whose transcript has been deleted are not written.

A scheduled deletion runs at its time even if the setting has since been
turned off. Participants may have been told the transcript would be deleted,
and turning the setting off does not undo that promise for meetings that have
already ended.

The recordings follow the same rules for the delay, the summary wait, a
deployment with no summary, the check ten minutes after a deletion made
immediately, and a rejoin or continued meeting that starts capturing while a
deletion is pending. Three things differ, because a recording is stored after
its session has ended rather than during it:

- **A recording stored after the deletion is deleted when it arrives.** The
  WebSocket transcriber stores a session's audio, and its screen video, after
  the session's END, and a video can take longer to store than the summary
  takes to write. Once a meeting's recordings have been deleted, any recording
  stored for it afterwards is deleted as soon as its link reaches the meeting
  record, whatever the setting says by then.
- **This includes a later session's recordings.** A meeting whose recordings
  have been deleted can still be rejoined or continued, unlike one whose
  transcript has been deleted. Its later sessions' recordings are deleted as
  they arrive, so they are not kept until that session's summary and are not
  offered for playback or download.
- **Reopening is not refused.** Deleting the recordings does not stop a meeting
  from being rejoined, continued or summarized again.

## What is deleted and what is kept

### The transcript

Deleted, for the meeting, by **Delete the transcript after the summary**:

- every turn of the transcript, partial or final, from the meeting's
  transcript records in DynamoDB, including the meeting chat messages a
  Virtual Participant added to it;
- every meeting assistant answer stored with the transcript;
- the **Issues detected** field, which quotes the transcript, on a meeting
  recorded by a version of LMA before 0.4.0 that still carries it;
- the transcript file in the recordings bucket that the summary step writes for
  the transcript knowledge base, its metadata file, and, for an uploaded
  meeting, the Amazon Transcribe output file. Every version of each file is
  deleted, not only the current one;
- those files' entries in the transcript knowledge base, when the deployment
  has one;
- the platform participant id on each consent reply linked to its speaker by
  that id (a chat reply on Teams through Azure Communication Services or the
  Zoom Meeting SDK); the link keeps its basis and when it was made, and no
  longer names a speaker;
- the quote in each piece of [checklist](meeting-context-and-checklist.md)
  evidence, which is transcript content. Its segment id and time are kept, and
  a quote sent after the deletion is not stored;
- the meeting's [notifications](meeting-notifications.md) of category
  `guidance`, which may quote the transcript, including the guidance an MCP
  client pushed with `push_meeting_guidance`;
- the meeting's [whiteboards](meeting-whiteboards.md), every version of them
  and the assistant's notes, which are drawn from the transcript; this includes
  whiteboards written entirely by hand.

Kept:

- the summary, on the meeting and in the transcript knowledge base, so the
  meeting can still be found and asked about. Whatever the summary quotes from
  the transcript, or from the meeting chat (a link a participant shared, for
  example), stays in it;
- the meeting record: its title, times, participants, agenda, speaker names
  and sentiment, and its labels, audience, meeting context and checklist
  (without the evidence quotes);
- the audio and video recordings, which follow **Delete the recordings after
  the summary**, below, and otherwise `AudioRecordingExpirationInDays`. Turn
  **Record meeting audio** off on the Configuration page if the audio must not
  be recorded at all;
- shared-screen key frames and the text read from them, which follow
  **Days to keep transcript segments** in full;
- the consent record, which is the evidence that participants were told,
  including the transcript line each spoken reply was linked to (its segment
  id and stored speaker, not its words, and the stored speaker each chat reply
  was linked to, without its platform participant id; see
  [Which speaker gave a reply](meeting-consent.md#which-speaker-gave-a-reply));
- the meeting's notifications of other categories, and each user's dismissals
  and mutes.

### The recordings

Deleted, for the meeting, by **Delete the recordings after the summary**, every
version and delete marker of each file:

- every audio recording the meeting record links to, in every capture session
  of a meeting that was rejoined or continued, whether it was stored by the
  WebSocket transcriber, a Virtual Participant or an upload;
- the raw audio the WebSocket transcriber stores beside each of its WAV
  recordings, and a recording it stored for the meeting without linking it (a
  second recording of the same session, stored beside the first under the
  meeting's name and a time);
- every meeting video recording the meeting record links to, from a Virtual
  Participant or from a streaming client, in every session;
- a Virtual Participant's video segments still under `lma-video-chunks/`;
- the meeting record's links to the recordings, so that the meeting page no
  longer offers them to play or download, and an archive prepared from the page
  afterwards says why they are not in it.

Kept:

- the summary, and the transcript, which follows its own setting;
- shared-screen key frames and the text read from them, which follow **Days to
  keep transcript segments**. They are taken from the video while the meeting
  runs, so they do not depend on the recording;
- chat attachments, which follow the transcript;
- the consent record and its evidence images;
- the meeting record, its notifications and its whiteboards;
- a download archive prepared before the deletion, for the day it is kept (see
  [Copies outside LMA's stores](#copies-outside-lmas-stores)).

## What users see

While deletion is pending, the meeting page shows above the transcript when it
will be deleted, so anyone who needs a copy can download it first. Once it is
deleted, the page says when, and the transcript download is no longer offered.
A meeting page that is open when the meeting ends shows the deletion time a few
seconds after the summary appears. It shows that the transcript has been deleted
the next time it is loaded.

The **Details** tab's **Capture and processing** section says the same under
**Transcript retention**: when the transcript is to be deleted or was deleted,
that the **Delete the transcript after the summary** setting did it, how many
transcript segments and files were removed, and that the summary and the
meeting record are kept while the recordings follow their own setting. For a
meeting whose transcript is
not deleted after the summary, it says that the transcript is kept for the days
set to keep transcript segments.

For the recordings, while a deletion is pending and the meeting has a
recording, the meeting page shows above the transcript when the recordings will
be deleted, so they can be played or downloaded first. Once they are deleted,
the page says when, the player and the recording downloads are no longer
offered, and the **Details** tab's **Capture and processing** section says
under **Recording retention** that the **Delete the recordings after the
summary** setting deleted them, how many recording files were removed, and
what is kept. Its **Audio recording** and **Video recording** rows read **None
kept: recordings deleted after the summary**. A meeting whose recordings are not
deleted after the summary is shown as keeping them as the deployment's
recording retention says.

After the transcript is deleted:

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

The recordings are deleted by the same function, on the same events, with a
schedule of their own, so moving or cancelling one deletion never changes the
other. The meeting record carries `RecordingDeletionScheduledFor` while a
deletion is due, then `RecordingsDeletedAt` and `RecordingFilesDeleted`, also
readable through `getCall`. The call event processor also tells the function
each time it stores a recording link on a meeting; for a meeting whose
recordings have been deleted, the function then deletes the new recording and
its link. The deletion removes the links only once it has deleted the files
they name, and only if no link was added meanwhile: if one was, it reads the
meeting again and deletes that recording too.

Every step is safe to repeat. A deletion that fails is retried: by Lambda for
an asynchronous invocation, and by EventBridge Scheduler for a schedule, for up
to 24 hours. A recording link the call event processor cannot pass on to the
function fails the record, which the stream delivers again. A deletion that
still has not completed, of either kind, is kept on a dead-letter queue, and the
`transcript-deletion-dead-letter-queue-depth` alarm notifies the operational
alerts topic (see [Monitoring & Alerting](monitoring.md)). Until it is run
again, a failed recording deletion leaves the meeting's links in place, so the
recordings are still shown on the meeting page rather than silently missing.
The alarm's description says how to run each kind of deletion again.

As a backstop, while deletion is on each transcript segment and meeting
assistant answer is also written with a DynamoDB time to live of at most two
days. Key frames keep their usual expiry. If a deletion never ran at
all, DynamoDB still removes the segments, on its own schedule.

## Copies outside LMA's stores

The deletion removes the transcript, and the recordings, from where LMA stores
them. Copies also pass through these places, each with its own lifetime:

| Where | How long | What to set |
|---|---|---|
| The Kinesis call data stream | 24 hours, the shortest Kinesis allows | Nothing; records age out on their own |
| The meetings table's DynamoDB stream | 24 hours | Nothing; records age out on their own |
| The meetings table's point-in-time recovery | 35 days: deleted items can be restored from a backup until then | Point-in-time recovery is a safety protection and stays on. If your policy forbids restorable copies, raise it with whoever owns the deployment's backup policy |
| Lambda and container logs | `CloudWatchLogsExpirationInDays` | Keep `LogLevel` at `INFO` (the default) or above. At `DEBUG`, functions log the transcript text they handle |
| The GraphQL API's log group, `/aws/appsync/apis/<api id>` | `CloudWatchLogsExpirationInDays` | Keep `AppSyncFieldLogLevel` at `ERROR` (the default) or `NONE`. At `DEBUG` or `ALL`, the API logs each request's content, transcript segments included |
| Amazon Bedrock model invocation logging | As configured for the account | Off by default. If the account has it on, every prompt is logged, including summary prompts that contain the whole transcript |
| The summary | Kept with the meeting | Summary prompts can be written not to quote the transcript; see [Transcript Summarization](transcript-summarization.md) |
| A download archive (**Download › Everything (.zip)**) prepared before the deletion | One day after it was prepared, up to about two | Nothing; the archive's lifecycle rule removes it. A person who downloaded it, or a recording, keeps their copy |
| The WebSocket transcriber's and the Virtual Participant's local working files | Removed by the container once the recording is stored | Nothing |

## Limitations

- Each setting applies to the whole deployment. There is no per-meeting choice.
- The delay is counted to the minute: EventBridge Scheduler runs a one-time
  schedule within the minute it is set for.
- A meeting that ended before the setting was turned on keeps its transcript
  for **Days to keep transcript segments**, and its recordings for
  `AudioRecordingExpirationInDays`.
- A meeting video is found only through its link on the meeting record: the
  producers turn the meeting's ID into a file name by replacing characters, so
  a video's name is not proof of which meeting it belongs to. A video whose
  link never reached the meeting record (its upload succeeded but the event
  announcing it was lost) is not deleted after the summary, and follows
  `AudioRecordingExpirationInDays`. The WebSocket transcriber's audio is also
  found by its file name, which follows a fixed pattern for each meeting.
