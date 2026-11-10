---
title: "Privacy and Data Handling Controls"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Privacy and Data Handling Controls

This page brings together, in one place, the controls that decide what LMA tells
a meeting's participants, what it captures and keeps, for how long, where the
content is processed, and what is removed when a meeting or a whole deployment is
deleted. It is written for an administrator or a reviewer deciding how to
configure a deployment for internal meetings, customer-facing meetings or
regulated meetings. Each section summarises a control and links to the page that
describes it in full:

- [Meeting Consent](meeting-consent.md) — the consent modes, the consent record
  and how each capture method runs the consent flow.
- [Transcript Deletion After the Summary](transcript-deletion.md) — deleting a
  meeting's transcript once it is summarized.
- [Configuration Guide](configuration-guide.md) — the runtime settings, the
  [Retention](configuration-guide.md#retention-settings-the-stack-limits) section
  and the [configuration presets](configuration-guide.md#configuration-presets).
- [Infrastructure & Security](infrastructure-and-security.md) — encryption,
  [data retention](infrastructure-and-security.md#data-retention) and
  [where meeting content is processed](infrastructure-and-security.md#where-meeting-content-is-processed).
- [CloudFormation Parameters](cloudformation-parameters.md) — the deploy-time
  parameters named below.

Two kinds of control appear throughout. **Settings** are stored in the
deployment's configuration and changed by an Admin on **Configuration ▸
Configuration** without a redeploy. **Stack parameters** are chosen when the
CloudFormation stack is deployed or updated. Where a control is a stack
parameter, this page says so, because no configuration preset or page can change
it.

LMA records evidence of what participants were told and what they replied. It
does not decide which recording, transcription or data-protection rules apply to
a meeting, and nothing on this page is legal advice. Choose the configuration
with whoever advises your organisation on those rules.

## Contents

- [Consent options](#consent-options)
- [What LMA stores, and for how long](#what-lma-stores-and-for-how-long)
- [Deleting data](#deleting-data)
- [Where content is processed](#where-content-is-processed)
- [Configuration presets and deploy-time controls](#configuration-presets-and-deploy-time-controls)
- [Related](#related)

## Consent options

### The three modes and the two opt-in rules

The **consent mode** is set for the whole deployment in the **Meeting Consent**
section of the Configuration page and, where the deployment allows it, chosen
again for a single meeting when it starts. It ships set to Off.

| Mode | What participants see | When the meeting is recorded and transcribed |
|---|---|---|
| **Off** | Nothing | Always. This is the shipped default. |
| **Notify** | A notice that the meeting is recorded and transcribed, and how to object | From the start, until a participant objects |
| **Opt-in** | A request to agree, with the phrase to reply | Not until the consent rule is met; an objection from anybody stops it again |

An opt-in meeting is granted by one of two **consent rules**:

- **Each participant consents.** Every participant listed as present has replied
  with consent. A participant who joins later and has not replied withholds
  recording and transcription again until they do.
- **The meeting owner attests that everyone agreed.** The person who owns the
  meeting in LMA confirms that everyone agreed, out loud, in the chat or in the
  invitation's terms. An objection from any participant still stops capture.

The other Meeting Consent settings decide the notice and request texts, the
phrases a reply is read by, whether a meeting may be started with a different
mode (**Allow a different choice per meeting**; with it off, a meeting may still
choose a stricter mode or rule), whether evidence screenshots are kept, the model
that checks them, what happens to streaming clients that show no consent step,
whether a Virtual Participant leaves after an objection, and whether it reads
replies said aloud. Their defaults are listed in
[The Meeting Consent settings](meeting-consent.md#the-meeting-consent-settings).
A saved change applies to meetings whose consent starts after the save.

### How each capture method runs it

The consent record and its rules are shared by every way a meeting reaches LMA;
each capture method decides how the notice is shown and how replies are read
(see [Flows by capture method](meeting-consent.md#flows-by-capture-method)).

**Virtual Participant.** The Virtual Participant posts the notice in the meeting
chat once it is admitted, and again when somebody joins. It reads replies typed in
the chat by the sender's display name and, with **Read spoken replies** on (the
shipped default), replies said aloud and addressed to LMA ("LMA, I consent", "LMA,
I object") under the name of the participant shown speaking. In an opt-in meeting
it writes no transcript, recording, chat lines, key frames or screen recording
until consent is given.

While consent is awaited and **Read spoken replies** is on, the Virtual
Participant still sends the meeting audio to the meeting's transcription engine,
on a session of its own, only to hear spoken replies. What it hears is discarded
inside the Virtual Participant: it does not reach the transcript, the recording,
the summary or the logs, and only the reply events are kept on the consent
record. With Amazon Transcribe that audio is processed by the Amazon Transcribe
streaming service in the stack's Region, and billed. Turn **Read spoken replies**
off where no meeting audio may leave the Virtual Participant before consent; the
Virtual Participant then reads the chat only, and an objection made out loud takes
effect when the LMA user records it in LMA. No preset changes this setting. See
[Spoken replies](meeting-consent.md#spoken-replies).

What a Virtual Participant can read differs by meeting platform, which matters
for the each-participant rule:

| Join | Each participant consents | Notes |
|---|---|---|
| Chime, Zoom (browser and Meeting SDK), Google Meet | Supported | The platform shows chat senders and an attendee count. Google Meet's sender and speaker reading has not been confirmed in a live meeting |
| Teams (browser) | Supported **aloud only** | Teams shows no chat sender, so a typed consent cannot be attributed. With **Read spoken replies** off the rule is never met |
| Teams (Azure Communication Services) | Supported | The platform gives the participants' names |
| Webex (browser) | **Not supported** | The page shows no attendee count, so one unidentified participant is always listed. Use the owner-attests rule on Webex |

Notify, owner-attests and objections work on every join. The full table, with how
the participant list is screenshotted on each platform, is in
[What each platform supports](meeting-consent.md#what-each-platform-supports).

**Stream Audio and the Desktop Capture App.** Neither can see the meeting's
participants or chat, so the consent step is the LMA user's own. They show the
notice with a copy button for the user to share in the meeting, and ask the user
to confirm that it was shared or, in opt-in, that everyone agreed (the
owner-attests rule). The WebSocket transcriber records that confirmation on the
meeting and decides whether audio is transcribed and recorded: in opt-in nothing
is captured until the record allows it, and audio sent before then is discarded.
**Participant objected** stops capture and ends the meeting. Because neither can
collect each participant's consent, an opt-in deployment whose rule is **Each
participant consents** and that does not allow a different choice per meeting
refuses Start from both. See
[Stream Audio and the Desktop Capture App](meeting-consent.md#stream-audio-and-the-desktop-capture-app).

**Clients without a consent step.** The browser extension, the Stream Audio embed,
Desktop Capture App builds from before the consent step, and scripts written
against the streaming API send no consent step. In Notify or Opt-in the setting
**Streaming clients without a consent step** decides what happens to their
meetings: **Accept with a warning** (the shipped default) captures them as before,
with no consent record and a warning in the transcriber's log, so in an opt-in
deployment such a meeting is captured without anybody having confirmed consent;
**Refuse** does not create, record or transcribe the meeting. Choose Refuse only
once every client in use shows the consent step. See
[Clients without a consent step](meeting-consent.md#clients-without-a-consent-step).

**Upload Audio** has no consent step: the recording already exists, and should
only be uploaded with the consent its participants gave when it was made.

### Objections

In every mode an objection, typed in the chat, said aloud to LMA, recorded by a
signed-in user with **Record an objection** on the meeting page, or reported with
**Participant objected** in Stream Audio or the Desktop Capture App, stops
recording and transcription at once. What was captured before the objection is
kept: an objection stops capture, it does not delete what was captured while
capture was allowed. Delete the meeting if that content must go too.

With **Virtual Participant leaves after an objection** off (the default) the
Virtual Participant stays without recording, and resumes if the objector later
consents; the screen recording does not resume. With it on, the Virtual
Participant leaves the meeting. See [Objections](meeting-consent.md#objections).

### Evidence kept

What participants were told and replied is kept on the meeting as its **consent
record**, an attribute of the meeting's row, so it is shared, kept and deleted
with the meeting. It holds the mode and rule, the status and whether capture was
allowed, the notice text and each time it was posted, the participants listed as
present, every reply and attestation with the participant's display name, where
it was read and when, the status history with reasons, and the evidence images
with the result of their vision-model check. See
[What is stored](meeting-consent.md#what-is-stored).

Evidence images are screenshots of the meeting chat and participant list taken by
the Virtual Participant when consent is granted and on every objection, or one
frame captured by the user in Stream Audio or the Desktop Capture App. An Amazon
Bedrock vision model can check each image (the **Evidence check model**, Amazon
Nova Lite by default); its verdict is supporting evidence and never changes the
meeting's consent status. With **Keep evidence screenshots** off, an image is
deleted once its check is stored. The consent record is included in the
summary's downloads.

Display names are not verified identities, and a model's reading of a screenshot
can be wrong; see [Limitations](meeting-consent.md#limitations).

### Choosing a mode

The following describes how the modes fit common situations. It is not a
statement of what any law requires.

- **Off** suits deployments where participants are told about recording by other
  means, such as an organisation-wide policy or the meeting platform's own
  notice, and for testing.
- **Notify** suits internal meetings where a notice and the chance to object are
  what is expected. It works on every capture method and every Virtual
  Participant join. The **Internal meetings – notify participants** preset sets
  it.
- **Opt-in with each participant consenting** suits meetings in which nothing may
  be captured until every participant has agreed. Only a Virtual Participant can
  collect it, and not on the Webex join; on the Teams browser join consent must be
  said aloud. The **Regulated – consent required, transcript not retained**
  preset sets it.
- **Opt-in with the owner attesting** suits meetings whose consent is gathered
  outside LMA (in the invitation's terms, or out loud at the start), meetings
  streamed from Stream Audio or the Desktop Capture App, and Virtual Participant
  joins that cannot list the participants. The record then rests on the owner's
  attestation, plus any evidence image.

Whatever the mode, set **Streaming clients without a consent step** to **Refuse**
in a deployment where every meeting must go through the consent step, and check
first that no one relies on the browser extension or the Stream Audio embed.

## What LMA stores, and for how long

### Retention controls

Five stack parameters set how long meeting data is kept, each defaulting to 90
days except where noted. Two of them can be shortened, but not lengthened, from
the **Retention** section of the Configuration page (see
[Retention: settings the stack limits](configuration-guide.md#retention-settings-the-stack-limits)):

| Control | Kind | Default | What it governs |
|---|---|---|---|
| `MeetingRecordExpirationInDays` | Stack parameter, and the limit of **Retention › Days to keep meeting records** | 90 | The DynamoDB time to live of the meeting record (with its summary, agenda and consent record) and of its usage records. Also, from the parameter only, the time to live of each Virtual Participant record, counted from when it was created or, if later, from the time it is scheduled for |
| `TranscriptionExpirationInDays` | Stack parameter, and the limit of **Retention › Days to keep transcript segments** | 90 | The DynamoDB time to live of transcript segments, chat lines, meeting assistant answers and key-frame items. Also, from the parameter only, the S3 lifecycle rules on the transcript knowledge base's files (the transcript and summary files), key-frame images and consent evidence images |
| `AudioRecordingExpirationInDays` | Stack parameter | 90 | The S3 lifecycle rules on audio and video recordings |
| `CloudWatchLogsExpirationInDays` | Stack parameter | 90 | The retention of the stack's CloudWatch log groups |
| `VPSchedulerStateMachineLogsExpirationInDays` | Stack parameter | 7 | The retention of the scheduled Virtual Participant state machine's log group |
| **Retention › Delete the transcript after the summary** | Setting | Off | Deletes each meeting's transcript at a fixed time after its summary; see [Deleting data](#deleting-data) |

A value saved in the Retention section applies to records written about five
minutes after the save or later; a record already written keeps its expiry. The
S3 lifecycle rules and the Virtual Participant records' expiry follow the stack
parameters only, so shortening a retention on the Configuration page does not
remove files from the recordings bucket, or Virtual Participant records, sooner.

DynamoDB removes an expired item in the background, typically within a few days
of its expiry rather than at it. The S3 lifecycle rules count whole days from when
an object was written. The recordings bucket is versioned: when a lifecycle rule
expires a file, Amazon S3 keeps its last version as a noncurrent version, and a
further rule removes every noncurrent version one day after it stopped being
current (see
[Earlier versions in the recordings bucket](#earlier-versions-in-the-recordings-bucket)).
A file is therefore gone from the bucket within about two days of the end of its
retention period, rather than at it.

### Data inventory

Every item below is in the AWS account and Region of the stack. "The meetings
table" is the AI stack's event-sourcing DynamoDB table; "the recordings bucket" is
the S3 bucket the main stack creates for recordings and transcript files. Both are
encrypted with the stack's customer-managed AWS KMS key, as are the other tables,
the CloudWatch log groups and the Kinesis call data stream (see
[Encryption](infrastructure-and-security.md#encryption)). Objects written to the
recordings bucket by a version of LMA before it used the customer-managed key
keep the encryption they were written with, Amazon S3 managed keys, until they
are rewritten or expire (see
[Recordings bucket and call data stream](infrastructure-and-security.md#recordings-bucket-and-call-data-stream)).

| Data | Where it is stored | Kept by default | Controlled by | Deleting the meeting |
|---|---|---|---|---|
| **Meeting record**: title, owner, sharing list, times, participants, speaker names, sentiment, transcript-deletion stamps | The meetings table, the meeting's row and its meeting-list rows | 90 days | `MeetingRecordExpirationInDays`; **Days to keep meeting records** | Deleted |
| **Transcript segments**: every turn, partial or final, on every channel | The meetings table, the meeting's transcript items | 90 days | `TranscriptionExpirationInDays`; **Days to keep transcript segments**; **Delete the transcript after the summary** | Deleted |
| **Meeting chat lines** a Virtual Participant adds to the transcript | Stored with the transcript segments | As the transcript | As the transcript; **Virtual Participant › Add the meeting chat to the transcript** (default On) decides whether they are captured. See [Meeting Chat in the Transcript](virtual-participant.md#meeting-chat-in-the-transcript) | Deleted |
| **Summary** | On the meeting record; with the transcript knowledge base deployed, also as a file in the recordings bucket (see the next rows) | As the meeting record; the file as the transcript files | As the meeting record. Kept when the transcript is deleted after the summary | Deleted |
| **Meeting assistant answers** | Stored with the transcript segments | As the transcript | As the transcript | Deleted |
| **Other model outputs**: the derived agenda, speaker names found from the transcript, key-frame text | On the meeting record (agenda, speaker names) or on the key-frame items (text) | As the record they are on | As that record | Deleted |
| **Transcript and summary files**: the transcript as text (speech only, without chat lines, followed by the shared screen's text), the formatted summary, a metadata file for each, and for an uploaded meeting the Amazon Transcribe output | The recordings bucket, `lma-transcripts/`. The summary step writes the transcript file for every meeting (the MCP server's transcript tool reads it), and the summary file only when the transcript knowledge base is deployed, because nothing else reads it | 90 days | The `lma-transcripts/` lifecycle rule, from `TranscriptionExpirationInDays`, for the summary file as for the transcript files: see [The knowledge base's copy of the summary](#the-knowledge-bases-copy-of-the-summary). **Delete the transcript after the summary** deletes the transcript files (every version) and keeps the summary file | Deleted, every version |
| **Transcript knowledge base entries** | The transcript knowledge base the stack creates (`TranscriptKnowledgeBase`, on by default): an Amazon Bedrock knowledge base over `lma-transcripts/`, with its vectors in an Amazon S3 Vectors index, which uses the service's default encryption (the template names no customer-managed key). Synced every 15 minutes | As long as the files they were built from | Set `TranscriptKnowledgeBase` to `DISABLED` to deploy no transcript knowledge base | Removed at the next sync, within about 15 minutes, once the files are gone |
| **Audio recording** | The recordings bucket, `lma-audio-recordings/` | 90 days | `AudioRecordingExpirationInDays`; **Meeting Recording › Record meeting audio** (default On) decides whether it is recorded | Deleted, every version |
| **Meeting video** (screen recording) from a Virtual Participant or the Desktop Capture App | The recordings bucket, `lma-video-recordings/`; a Virtual Participant's in-progress segments under `lma-video-chunks/` | 90 days; segments are deleted when the video is assembled, or after 1 day | `AudioRecordingExpirationInDays`; `EnableVideoRecording` (default `true`), the **Record meeting video** toggle per Virtual Participant, and the Desktop Capture App's own setting per machine. See [Meeting Video Recording](virtual-participant.md#meeting-video-recording) | Deleted, every version |
| **Key frames** of the shared screen, and the text read from them | Images in the recordings bucket, `lma-video-keyframes/<meeting>/`; items in the meetings table. Taken only when the meeting's video is recorded | 90 days | Items: as the transcript, including the page setting. Images: the lifecycle rule, from `TranscriptionExpirationInDays` only. Not removed by **Delete the transcript after the summary**. See [Retention, access and deletion](meeting-video.md#retention-access-and-deletion) | Deleted; the images, every version |
| **Agenda** | On the meeting record; an agenda given when a Virtual Participant is started or scheduled is also on that Virtual Participant's record | As the record it is on | As the meeting record; see the Virtual Participant records row | Deleted, from the meeting and from its Virtual Participant records |
| **Consent record** | On the meeting record (`Consent`) | As the meeting record | As the meeting record. Kept when the transcript is deleted after the summary | Deleted |
| **Consent evidence images** | The recordings bucket, `lma-consent-evidence/<meeting>/` | 90 days | The lifecycle rule, from `TranscriptionExpirationInDays`; **Keep evidence screenshots** off deletes each image once checked | Deleted, every version |
| **Usage records** behind the meeting's cost estimate | The meetings table | 90 days | `MeetingRecordExpirationInDays`; **Days to keep meeting records** | Deleted |
| **Virtual Participant records**: meeting name, platform, meeting ID and password, schedule, agenda, per-meeting consent and video choices, status | The Virtual Participant DynamoDB table | 90 days from when the record was created, or from the time it is scheduled for if that is later. A record written before these records were given an expiry has none, and is kept until it is deleted | `MeetingRecordExpirationInDays`, the stack parameter only; deleted from the **Virtual Participants** page | Every record that names the meeting is deleted |
| **Pending uploads**: a file uploaded for batch transcription, before it is processed | The recordings bucket, `lma-uploads-pending/` | 7 days | Fixed lifecycle rule | Not applicable |
| **Voice enrollment** voiceprint (only with `EnableVoiceEnrollment=true`) | The voice enrollment DynamoDB table. The sample audio is discarded once the voiceprint is computed | Until the person withdraws, or until their consent is older than `VoiceEnrollmentRetentionDays` (default 365), when a nightly pass deletes it | The enrollee, from **Enroll My Voice**; an Admin can revoke on their behalf. See [Voice Enrollment](voice-enrollment.md#retention) | Not affected: a voiceprint belongs to a person, not a meeting |
| **Voice consent log** (only with `EnableVoiceEnrollment=true`) | The voice consent log DynamoDB table, append-only, with no biometric data | No expiry | None; it is the audit trail of consent given and withdrawn | Not affected |
| **CloudWatch logs** of the Lambda functions, containers, MicroVMs and the GraphQL API | CloudWatch Logs log groups | 90 days (7 for the scheduler state machine) | `CloudWatchLogsExpirationInDays`; `LogLevel` and `AppSyncFieldLogLevel` decide whether meeting content is logged (see [Logging](#logging)) | Not affected |
| **S3 server access logs** of the stack's buckets, which name the objects read and written, and so the file names derived from meeting IDs | The stack's logging bucket, encrypted with Amazon S3 managed keys | 180 days | Fixed lifecycle rule | Not affected |
| **Virtual Participant browser profiles** (meeting-platform cookies, per user) | The Virtual Participant profiles bucket | 365 days after the profile was last saved | Removed when the user removes their Zoom credentials. See [Persistent Chromium profile per user](zoom-credentials-and-join-reliability.md#persistent-chromium-profile-per-user) | Not affected |
| **Downloads**: transcript, summary and slide exports | Produced in the user's browser and saved on the user's device | Outside LMA | Outside LMA. See [Transcript Downloads](web-ui-guide.md#transcript-downloads) | Not affected |

Who can read each item is described in
[User-Based Access Control](user-based-access-control.md): the meeting's owner,
the people it is shared with, and Admins read the meeting, its transcript, its key
frames and its consent record. Key-frame and evidence images are reached through
links signed for one hour after the same check, and recordings through
short-lived links the web UI signs when playback starts (see
[Recording Playback](user-based-access-control.md#recording-playback)).

### Copies that follow their own lifetime

Meeting content also passes through places that keep it for their own period and
that neither the retention settings nor deleting a meeting change:

| Where | How long |
|---|---|
| The Kinesis call data stream | 24 hours |
| The meetings table's DynamoDB stream | 24 hours |
| The point-in-time recovery of the stack's DynamoDB tables | 35 days; a deleted item can be restored from it until then. Point-in-time recovery is a safety protection and stays on |
| Earlier versions of objects in the recordings bucket | Up to about two days after they stop being current; see [Earlier versions in the recordings bucket](#earlier-versions-in-the-recordings-bucket). Deleting a meeting, and **Delete the transcript after the summary**, delete every version of the files they remove at once |
| CloudWatch logs | `CloudWatchLogsExpirationInDays` |
| Amazon Bedrock model invocation logging | As configured for the account. Off by default; if the account has it on, every prompt is logged, including summary prompts that contain the whole transcript |
| Optional integrations outside AWS | Under each provider's own terms; see [Optional integrations outside AWS](infrastructure-and-security.md#optional-integrations-outside-aws) |

[Copies outside the transcript store](transcript-deletion.md#copies-outside-the-transcript-store)
lists the same places for a transcript deleted after its summary.

### Earlier versions in the recordings bucket

The recordings bucket has S3 Versioning turned on. When a lifecycle rule expires a
file, Amazon S3 does not erase it: it adds a delete marker, and the file's last
version becomes a noncurrent version. An overwritten file keeps its earlier
version in the same way. The bucket's `ExpireNoncurrentVersions` lifecycle rule
removes every noncurrent version one day after it stopped being current, and
removes a delete marker once no version is left behind it, for every object in
the bucket. Amazon S3 applies lifecycle rules once a day and rounds to the next
midnight UTC, so an expired file can take up to about two days to leave the
bucket entirely. In that time it is not served to anyone, because it is no longer
the current version, but it still exists in the bucket.

The window is one day, where the configuration bucket keeps its replaced objects
for 35: the configuration bucket's earlier objects are what a configuration table
restored from point-in-time recovery refers to, while nothing in LMA restores an
earlier version of a recording or transcript file, and for meeting content the
retention period is the point.

Deleting a meeting, and **Delete the transcript after the summary**, do not wait
for the rule: they delete every version and delete marker of the files they
remove at once. LMA's other deletions in the bucket remove the current version
and leave the rest to the rule: a consent evidence image deleted once checked,
with **Keep evidence screenshots** off; an uploaded file once it is processed;
and a Virtual Participant's video segments once its video is assembled.

### The knowledge base's copy of the summary

With the transcript knowledge base deployed, the summary step also writes the
summary as a file in `lma-transcripts/`, next to the transcript file, so that
meeting-history search can find a meeting by its summary. Both files are the
knowledge base's documents and follow the same rule: the `lma-transcripts/`
lifecycle rule, from `TranscriptionExpirationInDays`. The summary on the meeting
record follows `MeetingRecordExpirationInDays`. A lifecycle rule takes whole days
and the meeting record's retention may be a fraction of a day, so the file cannot
follow the meeting record's parameter. With both parameters at their default of
90 days the two copies are kept equally long; to keep the file no longer than the
meeting record, set `TranscriptionExpirationInDays` no higher than
`MeetingRecordExpirationInDays`. Deleting the meeting deletes both copies.

Without the transcript knowledge base (`TranscriptKnowledgeBase` set to
`DISABLED`), the summary file is not written, and the summary is kept on the
meeting record only. Summary files written while a knowledge base was deployed
stay until their lifecycle rule removes them.

## Deleting data

### Transcript deletion after the summary

**Retention › Delete the transcript after the summary** deletes each meeting's
transcript permanently once the meeting is summarized: **Immediately**, or **15**,
**30** or **60 minutes** after the summary is stored. It ships set to Off, applies
to the whole deployment, and an Admin can change it at any time; a change applies
to the next meeting that ends. Unlike a retention period, it does not rely on
DynamoDB's background expiry: an EventBridge Scheduler schedule runs the deletion
within the minute it is due.

- **When.** The delay counts from when the summary is stored. If no summary is
  stored within 60 minutes of the meeting's end, the delay counts from then, and
  the transcript is deleted whether or not a summary was written. A deletion made
  immediately is checked again ten minutes later, to remove anything written for
  the meeting after it. A scheduled deletion runs even if the setting has since
  been turned off.
- **What is deleted.** Every transcript segment, the chat lines a Virtual
  Participant added, the meeting assistant answers, the transcript files in the
  recordings bucket (every version), and their entries in the transcript
  knowledge base.
- **What is kept.** The summary (on the meeting and in the knowledge base), the
  meeting record, the audio and video recordings, key frames and their text, and
  the consent record. Turn **Record meeting audio** off and set
  `EnableVideoRecording` to `false` if the recordings must not be kept either.
- **What users see.** The meeting page says when the transcript will be, or was,
  deleted; after it, **Regenerate** on the summary is refused.

As a backstop, while the setting is on, each transcript segment and assistant
answer is written with a DynamoDB time to live of at most two days. A deletion
that keeps failing is retried for up to 24 hours and then raises the
`transcript-deletion-dead-letter-queue-depth` alarm (see
[Monitoring & Alerting](monitoring.md)). See
[Transcript Deletion After the Summary](transcript-deletion.md).

### Deleting a meeting

A meeting's owner can delete it, and an Admin can delete any meeting, from the
meetings list or from the meeting page's **⋮** menu. People the meeting is shared
with cannot delete it. The meeting disappears from their meeting lists, without a
notification. See [Meeting Deletion](user-based-access-control.md#meeting-deletion).

Deleting a meeting removes, for that meeting:

- the meeting record and its meeting-list rows, and with them the summary, the
  agenda, the speaker names and the consent record;
- every transcript item, partial or final, including chat lines and meeting
  assistant answers;
- the usage records behind its cost estimate;
- its key-frame items and images, and its consent evidence images;
- its audio recording and its video recording, with any video segments still in
  the bucket;
- the transcript and summary files in `lma-transcripts/` whose names begin with
  the meeting's ID; the transcript knowledge base drops their entries at its next
  sync;
- every Virtual Participant record that names the meeting, after ending a Virtual
  Participant still in it.

Every file it removes from the recordings bucket is removed with all its versions
and delete markers, so no earlier version is left behind. If a step fails, for
example a file that Amazon S3 does not delete or a Virtual Participant record that
cannot be removed, the meeting record is not deleted: the page reports that the
deletion failed, the meeting stays in the list, and deleting it again repeats
every step.

It does not change the copies listed in
[Copies that follow their own lifetime](#copies-that-follow-their-own-lifetime):
the Kinesis and DynamoDB streams, point-in-time recovery, CloudWatch logs, and
anything already downloaded or sent to an integration. In particular, the deleted
meeting's items can still be restored from the DynamoDB tables' point-in-time
recovery for 35 days. Deleting a user from [User Management](user-management.md)
does not delete the meetings they own.

### Deleting a stack

The `EnableDataRetentionOnDelete` stack parameter, `true` by default, decides
what deleting the stack leaves behind:

- **`true`**: the DynamoDB tables, the S3 buckets (recordings, logging,
  configuration history and Virtual Participant profiles), the transcript
  knowledge base's vector bucket and the customer-managed KMS key are kept, with
  their contents.
- **`false`**: the S3 buckets are emptied of every object version and deleted,
  and the vector bucket, the tables and the key are deleted with the stack (AWS
  KMS deletes a key only after its waiting period).

Most of the stack's CloudWatch log groups are deleted with the stack, whatever
the parameter says. The resources that do not follow these rules are listed in
[Retained Resources](cleanup.md#retained-resources):

- Kept whatever the parameter says: the voice enrollment table and the voice
  consent log (with `EnableVoiceEnrollment=true`); the log groups of the WebSocket
  transcriber's and the Virtual Participant's containers and of the scheduled
  Virtual Participant state machine; and the GraphQL API's log group, which
  AppSync creates and the stack does not own. The log groups go on expiring their
  entries under their retention periods.
- Kept only with `true`: the MicroVM log groups of the Virtual Participant and of
  the on-demand speech engine.
- A deployment upgraded from a version before 0.4.0 may also have older log groups
  that the stack does not own.

See [Cleanup](cleanup.md) for the steps to remove what remains.

## Where content is processed

[Where meeting content is processed](infrastructure-and-security.md#where-meeting-content-is-processed)
lists every place LMA sends meeting content. In summary:

- **The stack's AWS Region.** LMA's own resources, the tables and buckets that
  store meeting content, and the services that carry it all run in the Region the
  stack is deployed to. CloudFront caches only the web UI's static files.
- **Amazon Bedrock.** The **Model inference geography** setting, in the **Amazon
  Bedrock Models** section, decides where every Bedrock model LMA calls may run:
  **Global** (the default; the shipped `global.` profiles may be served from any
  commercial Region), **Same geography as this deployment**, or **In-region only**.
  When the chosen geography offers no profile for a model, the feature reports an
  error rather than calling Bedrock outside it. The Nova Sonic voice assistant
  runs in the Region `AmazonNovaSonicRegion` names (this stack's Region when
  blank), and knowledge-base embeddings run in the knowledge base's Region. See
  [Amazon Bedrock](infrastructure-and-security.md#amazon-bedrock).
- **The transcription engine.** Amazon Transcribe, in the stack's Region, is the
  default for every meeting. Where the experimental on-demand engine is deployed
  (`EnableMicrovmAsr=true`), an Admin can move streaming meetings or Virtual
  Participants onto it in the **Transcription Engine** section; it runs on AWS
  Lambda MicroVMs in the stack's Region, and the audio is not sent to Amazon
  Transcribe. A meeting whose MicroVM cannot start falls back to Amazon
  Transcribe. See [Choosing the engine](microvm-asr.md#choosing-the-engine).
  Live translation on the meeting page calls Amazon Translate in the stack's
  Region.
- **Redaction.** **Amazon Transcribe › Redact personal information** (off by
  default) has Amazon Transcribe redact the entity types chosen in **Entity types
  to redact** from the transcript. It applies only to meetings transcribed by
  Amazon Transcribe in `en-US`, `en-AU`, `en-GB` or `es-US`; it does not apply to
  meetings using language identification or the on-demand engine, which has no
  redaction. It redacts the transcript text only: the audio recording, the video,
  key frames and their text, and the meeting chat are not redacted. See
  [Content Redaction (PII)](transcription-and-translation.md#content-redaction-pii).

### Logging

The `LogLevel` stack parameter sets the Lambda functions' and containers' logging.
At `INFO` (the default) and above, log lines record identifiers, sizes and
outcomes rather than meeting content. At `DEBUG` they also record meeting content:
the summary prompt with its transcript, meeting assistant requests and answers,
and the voice assistants' heard and spoken text. The `AppSyncFieldLogLevel`
parameter sets what the GraphQL API logs: `ERROR` (the default) and `NONE` log no
request content, while `DEBUG` and `ALL` log each request's content, transcript
segments included. Use either `DEBUG` setting only while diagnosing a problem. The
audio a Virtual Participant hears while waiting for consent is not logged at any
level.

## Configuration presets and deploy-time controls

### What each preset sets

A **configuration preset** sets the consent, retention and privacy settings
together. An Admin applies one from **Configuration ▸ Configuration ▸ Actions ▸
Apply a preset…**, or chooses one at deploy time with the `ConfigurationPreset`
stack parameter; each preset is also available as a read-only **Managed**
profile. All four set the same settings, so applying one replaces whatever another
preset set, and leaves every other setting as it is. See
[Configuration presets](configuration-guide.md#configuration-presets).

| Setting | Standard | Internal meetings – notify participants | Regulated – consent required, transcript not retained | Regulated – notify with opt-out, transcript not retained |
|---|---|---|---|---|
| Consent mode | Off | Notify | Opt-in | Notify |
| Consent rule (opt-in) | Each participant | Each participant | Each participant | Each participant |
| Allow a different choice per meeting | On | On | Off (stricter only) | Off (stricter only) |
| Keep evidence screenshots | On | On | On | On |
| Streaming clients without a consent step | Accept with a warning | Accept with a warning | Refuse | Refuse |
| Virtual Participant leaves after an objection | Off | Off | On | On |
| Delete the transcript after the summary | Off | Off | 15 minutes after | 15 minutes after |
| Record meeting audio | On | On | Off | Off |
| Add the meeting chat to the transcript | On | On | Off | Off |
| Redact personal information | Off | Off | On | On |
| Model inference geography | Global | Global | Same geography | Same geography |

No preset sets **Read spoken replies**. Under the consent-required preset, Stream
Audio and the Desktop Capture App cannot be used to start a meeting, the Webex
join cannot be granted, and the Teams browser join is granted only by consent
said aloud. Under the notify preset, every capture method that shows a consent
step works, and clients without one are refused.

### Controls no preset can set

Some controls are stack parameters, not settings, so a deployment that needs a
regulated posture sets them when it deploys or updates the stack:

| Parameter | Default | For a regulated deployment |
|---|---|---|
| `LogLevel` | `INFO` | Keep `INFO` or `WARN`; `DEBUG` logs meeting content |
| `AppSyncFieldLogLevel` | `ERROR` | Keep `ERROR` or `NONE` |
| `MeetingRecordExpirationInDays` | 90 | The longest meeting records, summaries and consent records are kept, and how long Virtual Participant records are kept |
| `TranscriptionExpirationInDays` | 90 | The longest transcript segments are kept, and how long transcript and summary files, key-frame images and consent evidence images stay in the recordings bucket |
| `AudioRecordingExpirationInDays` | 90 | How long audio and video recordings are kept |
| `CloudWatchLogsExpirationInDays` | 90 | How long logs are kept |
| `EnableVideoRecording` | `true` | Video is not part of any preset. Set `false` if no meeting video, and so no key frames, may be kept; otherwise a Virtual Participant records video unless its **Record meeting video** toggle is turned off |
| `TranscriptKnowledgeBase` | `BEDROCK_KNOWLEDGE_BASE (Create)` | `DISABLED` if transcripts and summaries must not be indexed for search across meetings |
| `EnableDataRetentionOnDelete` | `true` | Decide whether data must outlive the stack |

`TranscriptionExpirationInDays` is shared by the transcript files and the consent
evidence images, while the consent record follows
`MeetingRecordExpirationInDays`. A short transcript retention therefore also
shortens how long the evidence images are kept; keep the two parameters equal if
the images are needed for as long as the record, or copy them elsewhere before
they expire.

### Checklist for a regulated deployment

1. Apply **Regulated – consent required, transcript not retained** or
   **Regulated – notify with opt-out, transcript not retained**, at deploy time
   with `ConfigurationPreset` or from the Configuration page, and activate it.
2. Decide **Read spoken replies**: off if no meeting audio may reach the
   transcription engine before consent.
3. Under the consent-required preset, use Virtual Participants on joins that
   support the each-participant rule, and tell users that Stream Audio, the
   Desktop Capture App and the Webex join will not capture meetings.
4. Set `EnableVideoRecording` to `false`, unless meeting video is wanted and its
   retention is acceptable.
5. Keep `LogLevel` at `INFO` or `WARN` and `AppSyncFieldLogLevel` at `ERROR` or
   `NONE`.
6. Set `MeetingRecordExpirationInDays`, `TranscriptionExpirationInDays`,
   `AudioRecordingExpirationInDays` and `CloudWatchLogsExpirationInDays` to the
   periods your policy allows, bearing in mind that evidence images and the
   knowledge base's summary file follow `TranscriptionExpirationInDays`.
7. Decide whether the transcript knowledge base (`TranscriptKnowledgeBase`) and
   any optional integration outside AWS may receive meeting content.
8. Check that the chosen **Model inference geography** offers a profile for every
   model in use; the Amazon Bedrock Models section shows which features have
   none.
9. Decide how the day or two for which the recordings bucket keeps noncurrent
   object versions, the 35-day DynamoDB point-in-time recovery window, and Amazon
   Bedrock model invocation logging in the account fit your policy.
10. Set `EnableDataRetentionOnDelete` to match what must happen to the data when
    the deployment is removed.

## Related

- [Meeting Consent](meeting-consent.md)
- [Transcript Deletion After the Summary](transcript-deletion.md)
- [Configuration Guide](configuration-guide.md) and
  [Unified Configuration Store](configuration-store.md)
- [Infrastructure & Security](infrastructure-and-security.md)
- [Meeting Video and Key Frames](meeting-video.md)
- [Voice Enrollment (Speaker Recognition)](voice-enrollment.md)
- [User-Based Access Control](user-based-access-control.md)
- [CloudFormation Parameters Reference](cloudformation-parameters.md)
- [Cleanup](cleanup.md)
