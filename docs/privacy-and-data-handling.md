---
title: "Privacy and Data Handling Controls"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
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
- [Transcript and Recording Deletion After the Summary](transcript-deletion.md) — deleting a
  meeting's transcript, and its recordings, once it is summarized.
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
- [Minimised content](#minimised-content)
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
| Teams (browser) | Supported (**aloud only** off the light-meetings client) | On Teams' anonymous light-meetings client the VP reads each chat message's sender from the heading Teams shows with it, and the participant list from the People pane; both were measured in live meetings, but the VP's own reading of them has not yet run in one. When the People pane cannot be read the VP falls back to the attendee count and the names it has seen |
| Teams (Azure Communication Services) | Supported | The platform gives the participants' names |
| Webex (browser) | **Not supported** | The page shows no attendee count, so one unidentified participant is always listed. Use the owner-attests rule on Webex |

Notify, owner-attests and objections work on every join. The full table, with how
the participant list is screenshotted on each platform, is in
[What each platform supports](meeting-consent.md#what-each-platform-supports).

**Stream Audio/Video and the Desktop Capture App.** Neither can see the meeting's
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
[Stream Audio/Video and the Desktop Capture App](meeting-consent.md#stream-audiovideo-and-the-desktop-capture-app).

**Clients without a consent step.** The Stream Audio embed, browser extension and
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
**Participant objected** in Stream Audio/Video or the Desktop Capture App, stops
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
it was read and when, the transcript speaker a reply was linked to when it was
recorded, the status history with reasons, and the evidence images with the
result of their vision-model check. See
[What is stored](meeting-consent.md#what-is-stored).

On Teams through Azure Communication Services and the Zoom Meeting SDK, the
Virtual Participant also stores the meeting platform's participant id, a
personal identifier, on each transcript segment of speech it names after that
participant (`PlatformParticipantId`), and on a chat consent reply linked to its
speaker by that id. It is kept with the transcript segments (**Days to keep
transcript segments**), removed from the consent record when the transcript is
deleted after the summary, and deleted with the meeting. It is not written to
the logs at the default log level and is not sent to a Transcript Lambda hook
(debug-level logging of whole incoming records, for troubleshooting, includes
it with the rest of the segment). It is not returned by the GraphQL API, and is
not in the meeting export or any MCP tool's answer, which give LMA's own speaker
id instead. See
[Which speaker gave a reply](meeting-consent.md#which-speaker-gave-a-reply).

Evidence images are screenshots of the meeting chat and participant list taken by
the Virtual Participant when consent is granted and on every objection, or one
frame captured by the user in Stream Audio/Video or the Desktop Capture App. An Amazon
Bedrock vision model can check each image (the **Evidence check model**, Amazon
Nova Lite by default); its verdict is supporting evidence and never changes the
meeting's consent status. With **Keep evidence screenshots** off, an image is
deleted once its check is stored. The consent record is included in the
summary's downloads.

Display names are not verified identities, and a model's reading of a screenshot
can be wrong; see [Limitations](meeting-consent.md#limitations).

### Consent given in advance

A person on a meeting's participant list can consent before the meeting,
signed in to LMA, from the meeting's consent link, for that meeting or for the
organizer's meetings until a date at most a year ahead, and withdraw it on
**My consents** at any time. Each consent keeps the person's email address,
account id, the scope, when it was given and until when, and the exact text
they agreed to with its hash. It keeps nothing about their device unless
**Meeting Consent › Keep sign-in device details with consents given in
advance** is on (off by default and in every preset), and then the request's
IP address and the browser's user agent. A consent is kept for
`MeetingRecordExpirationInDays` days after it stops counting, and is not
deleted with a meeting. A meeting a Virtual Participant joins counts it for an
attendee whose display name matches the person on the participant list; the
meeting's consent record then keeps, on that attendee's event, the consent's
scope, times and text hash and the match's basis, never the address. See
[Consent given in advance](meeting-consent.md#consent-given-in-advance).

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
  streamed from Stream Audio/Video or the Desktop Capture App, and Virtual Participant
  joins that cannot list the participants. The record then rests on the owner's
  attestation, plus any evidence image.

Whatever the mode, set **Streaming clients without a consent step** to **Refuse**
in a deployment where every meeting must go through the consent step, and check
first that no one relies on the Stream Audio embed or an older browser extension build.

## What LMA stores, and for how long

### Retention controls

Six stack parameters set how long meeting data is kept, each defaulting to 90
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
| `MeetingDataPointInTimeRecoveryDays` | Stack parameter | 35 | The DynamoDB point-in-time recovery window of the tables that hold meeting content: for how many days a deleted item can still be restored. 0 turns point-in-time recovery off on them. See [Point-in-time recovery of the DynamoDB tables](#point-in-time-recovery-of-the-dynamodb-tables) |
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
| **Meeting record**: title, owner, sharing list, times, participants, speaker names, sentiment, transcript and recording deletion stamps | The meetings table, the meeting's row and its meeting-list rows | 90 days | `MeetingRecordExpirationInDays`; **Days to keep meeting records** | Deleted |
| **Transcript segments**: every turn, partial or final, on every channel | The meetings table, the meeting's transcript items | 90 days | `TranscriptionExpirationInDays`; **Days to keep transcript segments**; **Delete the transcript after the summary** | Deleted |
| **Meeting chat lines** a Virtual Participant adds to the transcript | Stored with the transcript segments | As the transcript | As the transcript; **Virtual Participant › Add the meeting chat to the transcript** (default On) decides whether they are captured. See [Meeting Chat in the Transcript](virtual-participant.md#meeting-chat-in-the-transcript) | Deleted |
| **Chat attachments**: images and files shared in the meeting chat, and each one's AI title and summary | Files in the recordings bucket, `lma-chat-attachments/<meeting>/`, encrypted with the stack's key; one item each in the meetings table (file name, kind, type, size, title, summary). Captured only from a chat line that is added to the transcript, and only while **Virtual Participant › Capture chat attachments** (default Off) is on; an attachment that is not stored keeps its file name only | 90 days | Items: as the transcript, including **Delete the transcript after the summary**. Files: the `lma-chat-attachments/` lifecycle rule, from `TranscriptionExpirationInDays`, and deleted with the transcript. **AI titles and summaries for chat attachments** (default On) decides whether the model reads them. See [Chat Attachments](virtual-participant.md#chat-attachments) | Deleted; the files, every version |
| **Summary** | On the meeting record; with the transcript knowledge base deployed, also as a file in the recordings bucket (see the next rows) | As the meeting record; the file as the transcript files | As the meeting record. Kept when the transcript is deleted after the summary | Deleted |
| **Meeting assistant answers** | Stored with the transcript segments | As the transcript | As the transcript | Deleted |
| **Other model outputs**: the derived agenda, speaker names found from the transcript, key-frame text | On the meeting record (agenda, speaker names) or on the key-frame items (text) | As the record they are on | As that record | Deleted |
| **Transcript and summary files**: the transcript as text (speech only, without chat lines, followed by the shared screen's text), the formatted summary, a metadata file for each, and for an uploaded meeting the Amazon Transcribe output | The recordings bucket, `lma-transcripts/`. The summary step writes the transcript file for every meeting (the MCP server's transcript tool reads it), and the summary file only when the transcript knowledge base is deployed, because nothing else reads it | 90 days | The `lma-transcripts/` lifecycle rule, from `TranscriptionExpirationInDays`, for the summary file as for the transcript files: see [The knowledge base's copy of the summary](#the-knowledge-bases-copy-of-the-summary). **Delete the transcript after the summary** deletes the transcript files (every version) and keeps the summary file | Deleted, every version |
| **Transcript knowledge base entries** | The transcript knowledge base the stack creates (`TranscriptKnowledgeBase`, on by default): an Amazon Bedrock knowledge base over `lma-transcripts/`, with its vectors in an Amazon S3 Vectors index, which uses the service's default encryption (the template names no customer-managed key). Synced every 15 minutes | As long as the files they were built from | Set `TranscriptKnowledgeBase` to `DISABLED` to deploy no transcript knowledge base | Removed at the next sync, within about 15 minutes, once the files are gone |
| **Audio recording** | The recordings bucket, `lma-audio-recordings/` | 90 days | `AudioRecordingExpirationInDays`; **Meeting Recording › Record meeting audio** (default On) decides whether it is recorded; **Delete the recordings after the summary** deletes it, every version, once the meeting is summarized | Deleted, every version |
| **Meeting video** (screen recording) from a Virtual Participant or a streaming client (the Stream Audio/Video page, the browser extension or the Desktop Capture App) | The recordings bucket, `lma-video-recordings/`; a Virtual Participant's in-progress segments under `lma-video-chunks/` | 90 days; segments are deleted when the video is assembled, or after 1 day | `AudioRecordingExpirationInDays`; `EnableVideoRecording` (default `true`); for a Virtual Participant, **Meeting Recording › Record Virtual Participant meeting video** (default On), which overrides the **Record meeting video** toggle per Virtual Participant when it is off; for the streaming clients, **Meeting Recording › Record screen video from streaming clients** and the Desktop Capture App's own setting per machine; **Delete the recordings after the summary** deletes it, and any segments left, once the meeting is summarized. See [Meeting Video Recording](virtual-participant.md#meeting-video-recording) | Deleted, every version |
| **Key frames** of the shared screen, and the text read from them | Images in the recordings bucket, `lma-video-keyframes/<meeting>/`; items in the meetings table. Taken only when the meeting's video is recorded | 90 days | Items: as the transcript, including the page setting. Images: the lifecycle rule, from `TranscriptionExpirationInDays` only. Not removed by **Delete the transcript after the summary** or **Delete the recordings after the summary**. See [Retention, access and deletion](meeting-video.md#retention-access-and-deletion) | Deleted; the images, every version |
| **Agenda** | On the meeting record; an agenda given when a Virtual Participant is started or scheduled is also on that Virtual Participant's record | As the record it is on | As the meeting record; see the Virtual Participant records row | Deleted, from the meeting and from its Virtual Participant records |
| **Participant list**: the name and email address of each person expected at the meeting, as the organizer, the Virtual Participant form or an MCP client gave them. These are personal data of people who may have no LMA account | On the meeting record (`Participants`); a list given when a Virtual Participant is started or scheduled is also on that Virtual Participant's record | As the record it is on | As the meeting record; see the Virtual Participant records row. The owner or an Admin can change or clear it on the meeting page. With **Meeting Sharing › Share meetings with their listed participants** on (the default), listing someone shares the meeting with their address, read-only, from the time the meeting record exists, and the addresses it shared with are kept on the meeting record (`AutoSharedWith`) so that taking someone off the list removes that share; a listed person who signs in sees the names on the list, not the addresses (see [Sharing with the participant list](user-based-access-control.md#sharing-with-the-participant-list)). Listing someone does not create an account; an account the owner creates for them with **Create accounts for participants without one** is a user pool account, kept until it is deleted on the User Management page. See [Participant list](virtual-participant.md#participant-list) | Deleted, from the meeting and from its Virtual Participant records |
| **Consent record** | On the meeting record (`Consent`) | As the meeting record | As the meeting record. Kept when the transcript is deleted after the summary | Deleted |
| **Consents given in advance**: each person's own consent to a meeting, or to an organizer's meetings until a date: their address, account sub and username, the scope, when it was given, until when it counts, when it was withdrawn, and the exact text agreed to with its hash and version; the request's IP address and user agent only with **Meeting Consent › Keep sign-in device details with consents given in advance** on (default Off). Also a request to be added to a meeting's participant list (the person's address and name), and its daily count | The meetings table, under the person's address (`pmc#`); a request under the meeting (`pmr#`) for 30 days, or on the Virtual Participant's row until that row expires | Until `MeetingRecordExpirationInDays` days after the consent stops counting, at its end date (at most a year after it was given) or when it is withdrawn | The person withdraws it on **My consents**; deleting their account withdraws it; **Meeting Consent › Allow consent given in advance** (default On) decides whether new ones can be given. See [Consent given in advance](meeting-consent.md#consent-given-in-advance) | Kept: it is the person's, not the meeting's |
| **Consent evidence images** | The recordings bucket, `lma-consent-evidence/<meeting>/` | 90 days | The lifecycle rule, from `TranscriptionExpirationInDays`; **Keep evidence screenshots** off deletes each image once checked. Kept when the transcript or the recordings are deleted after the summary | Deleted, every version |
| **Meeting notifications**: guidance for the people running the meeting, and other categories a feature publishes; each user's dismissals and categories muted on that meeting | The meetings table, under the meeting. A user's categories muted on every meeting are one item per user | Guidance: as the transcript. Other categories and users' views: as the meeting record | Guidance follows the transcript, including **Delete the transcript after the summary**; the others follow **Days to keep meeting records**. A user's every-meeting mutes are deleted with the user. See [Meeting Notifications](meeting-notifications.md#retention) | Deleted |
| **Meeting whiteboards**: named panels made by hand or kept by the assistant from the transcript, every version of them, and the assistant's notes | The meetings table, under the meeting | As the transcript | Follow the transcript, including **Delete the transcript after the summary**, whoever wrote them. See [Meeting Whiteboards](meeting-whiteboards.md#retention) | Deleted |
| **Usage records** behind the meeting's cost estimate | The meetings table | 90 days | `MeetingRecordExpirationInDays`; **Days to keep meeting records** | Deleted |
| **Virtual Participant records**: meeting name, platform, meeting ID and password, schedule, agenda, participant list, per-meeting consent and video choices, status | The Virtual Participant DynamoDB table | 90 days from when the record was created, or from the time it is scheduled for if that is later. A record written before these records were given an expiry has none, and is kept until it is deleted | `MeetingRecordExpirationInDays`, the stack parameter only; deleted from the **Virtual Participants** page | Every record that names the meeting is deleted |
| **Pending uploads**: a file uploaded for batch transcription, before it is processed | The recordings bucket, `lma-uploads-pending/` | 7 days | Fixed lifecycle rule | Not applicable |
| **Voice enrollment** voiceprint (only with `EnableVoiceEnrollment=true`) | The voice enrollment DynamoDB table. The sample audio is discarded once the voiceprint is computed | Until the person withdraws, or until their consent is older than `VoiceEnrollmentRetentionDays` (default 365), when a nightly pass deletes it | The enrollee, from **Enroll My Voice**; an Admin can revoke on their behalf. See [Voice Enrollment](voice-enrollment.md#retention) | Not affected: a voiceprint belongs to a person, not a meeting |
| **Voice consent log** (only with `EnableVoiceEnrollment=true`) | The voice consent log DynamoDB table, append-only, with no biometric data | No expiry | None; it is the audit trail of consent given and withdrawn | Not affected |
| **Participant account and lookup counts**: for each User, per UTC day, how many Participant accounts they created from their meetings' participant lists, and a hash of each address whose account status they looked up (never the address itself), so that the daily limits can be applied | The meetings table, under the user (`usr#<username>`), one item per kind per day | 2 days, by the table's time to live | None; Admins are not counted. See [Participant accounts for a meeting](user-management.md#participant-accounts-for-a-meeting) | Not affected |
| **Last sign-in times**: when each user last signed in with their password, for user access reviews | The Cognito stack's sign-in table, one item per user keyed by Cognito sub | Until the user is deleted on the User Management page; each sign-in replaces the time | None. Deleting a user on the User Management page deletes their item; a user deleted directly in Cognito (the console or `admin-delete-user`) leaves it until the table is deleted. The table is kept with the user pool when `EnableDataRetentionOnDelete` is `true`. See [Last sign-in](user-management.md#last-sign-in) | Not affected |
| **Last active times**: when each user was last active (their latest sign-in, token renewal or MCP API key use, to within 15 minutes), for user access reviews, and when each MCP API key was last used | The Cognito stack's sign-in table (the user's time, in the same item as the last sign-in) and the MCP API key table (the key's time, on the key's item) | The user's time as for the last sign-in; the key's time until the key is revoked. Each time is overwritten, at most once every 15 minutes, by the next activity; no history is kept | None. Deleting a user on the User Management page deletes their sign-in item; revoking a key deletes its item. See [Last active](user-management.md#last-active) | Not affected |
| **CloudWatch logs** of the Lambda functions, containers, MicroVMs and the GraphQL API | CloudWatch Logs log groups | 90 days (7 for the scheduler state machine) | `CloudWatchLogsExpirationInDays`; `LogLevel` and `AppSyncFieldLogLevel` decide whether meeting content is logged (see [Logging](#logging)) | Not affected |
| **S3 server access logs** of the stack's buckets, which name the objects read and written, and so the file names derived from meeting IDs | The stack's logging bucket, encrypted with Amazon S3 managed keys | 180 days | Fixed lifecycle rule | Not affected |
| **Virtual Participant browser profiles** (meeting-platform cookies, per user) | The Virtual Participant profiles bucket | 365 days after the profile was last saved | Removed when the user removes their Zoom credentials. See [Persistent Chromium profile per user](zoom-credentials-and-join-reliability.md#persistent-chromium-profile-per-user) | Not affected |
| **Downloads**: transcript, summary and slide exports | Produced in the user's browser and saved on the user's device | Outside LMA | Outside LMA. See [Transcript Downloads](web-ui-guide.md#transcript-downloads) | Not affected |
| **Meeting archives**: the ZIP files **Download › Everything (.zip)**, **Key frames (.zip)** and **All attachments (.zip)** prepare, the [meeting export](meeting-export.md) files **Download › Meeting export (.json)** and the MCP `get_meeting_export` tool prepare, and a status file for each | The recordings bucket, `lma-meeting-archives/<meeting>/`, encrypted with the stack's key. Each is a copy of the meeting's files as they were when it was prepared; the consent record and evidence are in it only for the meeting's owner or an Admin | 1 day | Fixed lifecycle rule (`MeetingArchiveCleanup`), which also removes an unfinished upload after a day. See [Everything as one archive](web-ui-guide.md#everything-as-one-archive) | Deleted, every version. An archive whose build was still running when the meeting was deleted is written after the deletion, and the lifecycle rule removes it, with its status file, a day after it was written (up to about two) |

Who can read each item is described in
[User-Based Access Control](user-based-access-control.md): the meeting's owner,
the Users and Admins it is shared with, and every Admin read the meeting, its
transcript, its key frames and its consent record. A
[Participant](user-based-access-control.md#participant) it is shared with reads
a read-only subset: the transcript, the summary, the meeting chat, the
recordings, the consent record without its evidence, and the names (not the
addresses) on the participant list; not the key frames, chat attachment files,
meeting context, checklist or the assistant's answers (see
[What a Participant sees of a shared meeting](user-based-access-control.md#what-a-participant-sees-of-a-shared-meeting)).
Key-frame and evidence images are reached through links signed for one hour
after the same check. Recordings are reached through short-lived links the web
UI signs when playback starts, and for a Participant through links the server
signs for an hour after checking that the meeting is shared with them (see
[Recording Playback](user-based-access-control.md#recording-playback)).

### Copies that follow their own lifetime

Meeting content also passes through places that keep it for their own period and
that neither the retention settings nor deleting a meeting change:

| Where | How long |
|---|---|
| The Kinesis call data stream | 24 hours |
| The meetings table's DynamoDB stream | 24 hours |
| The point-in-time recovery of the DynamoDB tables that hold meeting content | `MeetingDataPointInTimeRecoveryDays` days, 35 by default; a deleted item can be restored from it until then. With 0 there is no point-in-time recovery on these tables and nothing to restore from. See [Point-in-time recovery of the DynamoDB tables](#point-in-time-recovery-of-the-dynamodb-tables) |
| AWS Backup plans, on-demand DynamoDB backups and table exports, if anyone has set them up in the account | As each was configured; LMA creates none of them and the stack parameters do not change them |
| Earlier versions of objects in the recordings bucket | Up to about two days after they stop being current; see [Earlier versions in the recordings bucket](#earlier-versions-in-the-recordings-bucket). Deleting a meeting, **Delete the transcript after the summary** and **Delete the recordings after the summary** delete every version of the files they remove at once |
| A meeting archive prepared for download | One day after it was written (up to about two, with its earlier version), whatever the retention settings say of the files it copied. **Delete the transcript after the summary**, **Delete the recordings after the summary** and the other retention rules do not reach into an archive already prepared; deleting the meeting deletes its archives. A person who downloaded an archive keeps their copy, as with any download |
| CloudWatch logs | `CloudWatchLogsExpirationInDays` |
| Amazon Bedrock model invocation logging | As configured for the account. Off by default; if the account has it on, every prompt is logged, including summary prompts that contain the whole transcript |
| Optional integrations outside AWS | Under each provider's own terms; see [Optional integrations outside AWS](infrastructure-and-security.md#optional-integrations-outside-aws) |

[Copies outside LMA's stores](transcript-deletion.md#copies-outside-lmas-stores)
lists the same places for a transcript, or recordings, deleted after the summary.

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

Deleting a meeting, **Delete the transcript after the summary** and **Delete the
recordings after the summary** do not wait for the rule: they delete every
version and delete marker of the files they remove at once. LMA's other deletions in the bucket remove the current version
and leave the rest to the rule: a consent evidence image deleted once checked,
with **Keep evidence screenshots** off; an uploaded file once it is processed;
and a Virtual Participant's video segments once its video is assembled.

### Point-in-time recovery of the DynamoDB tables

DynamoDB point-in-time recovery keeps a continuous backup of a table, from which
someone with access to the AWS account can restore the table, as a new table, to
any second within its recovery period. The restored table holds the items as they
were at that moment, including items that have since been deleted by a meeting
deletion, by **Delete the transcript after the summary**, by a time to live, or by
hand. Point-in-time recovery is a safety protection: it is how a table is brought
back after an accidental write, a faulty update or a mistaken deletion. It also
means that, for as long as the recovery period lasts, a deletion from the table is
not final.

The `MeetingDataPointInTimeRecoveryDays` stack parameter sets the recovery period
of the tables that hold meeting content. It is a whole number from 0 to 35, and 35,
the longest period DynamoDB offers, is the default, so a deployment that does not
set it keeps the 35-day window earlier versions had.

| Table | What it holds |
|---|---|
| The meetings table | Meeting records (with their participant lists), transcript segments, summaries, consent records, consents given in advance and requests to be added to a participant list, chat lines and chat attachment items, key-frame items, meeting assistant answers, notifications, whiteboards and usage records |
| The Virtual Participant table | Meeting names, platforms, meeting IDs and passwords, schedules, agendas and participant lists |
| The Virtual Participant task registry | Each launched Virtual Participant's launch configuration, with the meeting's ID, name and time; each item expires about nine hours after the launch |
| The troubleshooting jobs table | The troubleshooting agent's answers, which can name a meeting and quote its logs; each job expires after seven days |
| The voice enrollment table (only with `EnableVoiceEnrollment=true`) | Voiceprints |

- **1 to 35**: point-in-time recovery is on, with that recovery period. An item
  deleted from one of these tables can still be restored until that many days
  after it was deleted. A short window keeps the protection while limiting the
  restorable copy: with 1, a deleted meeting or transcript can be restored for a
  day after its deletion and not after, and an accidental change can be undone
  only if it is noticed within that day. Lowering the period takes effect at once:
  DynamoDB stops offering restore points older than the new period.
- **0**: point-in-time recovery is off on these tables. Nothing can be restored
  from it, so a deletion is final as far as these tables are concerned, and an
  accidental write or deletion cannot be undone either. Turning it off ends the
  table's existing recovery history; turning it on again starts a new one from
  that moment. Set 0 when a deletion must leave no restorable copy.

Changing the parameter is a stack update that changes the tables' backup setting
in place; the tables are not replaced. DynamoDB charges for point-in-time recovery
by table size, not by the length of the period, so only 0 changes its cost.

The other tables keep point-in-time recovery on for 35 days, because none of them
holds meeting content and each is worth restoring after a mistake: the
configuration table and its profiles (whose earlier objects in the configuration
bucket are also kept for 35 days), the prompt template, chat button, Nova Sonic
and speech engine configuration tables, the MCP server, skill and MCP API key
tables, the OAuth state table, the selector cache, and the Cognito stack's sign-in
table (each user's last sign-in and last active times). The voice consent log also
keeps 35 days: it is the append-only audit trail of consent given and withdrawn,
LMA never deletes from it, and it holds no voiceprint or meeting content.

Three kinds of copy are outside this parameter:

- **AWS Backup and on-demand backups.** An AWS Backup plan that selects the
  account's DynamoDB tables, an on-demand backup, or an export of a table to
  Amazon S3 is a separate copy, kept for as long as it was configured. LMA creates
  none of them. A deployment that must make deletion final should check that no
  backup plan in the account covers these tables.
- **The system backup of a deleted table.** When a table that has point-in-time
  recovery on is deleted, DynamoDB keeps a backup of it, a system backup named
  `<table>$DeletedTableBackup`, for 35 days, whatever the recovery period was. This
  applies when the stack is deleted with `EnableDataRetentionOnDelete` set to
  `false` and when a retained table is deleted afterwards. A table with
  point-in-time recovery off gets no system backup: set
  `MeetingDataPointInTimeRecoveryDays` to `0` in a stack update before deleting
  the stack, and turn point-in-time recovery off on any other table, or on a
  table kept after the stack is gone, before deleting it; see
  [Cleanup](cleanup.md#manual-cleanup).
- **The DynamoDB stream** of the meetings table, which keeps each change for 24
  hours.

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
  Participant added and the chat attachments stored with them (their records,
  and every version of their files), the meeting assistant answers, the
  transcript files in the recordings bucket (every version), and their entries
  in the transcript knowledge base.
- **What is kept.** The summary (on the meeting and in the knowledge base), the
  meeting record, key frames and their text, and the consent record. The audio
  and video recordings follow their own setting, below. Turn **Record meeting
  audio** off and set `EnableVideoRecording` to `false` if no recording may be
  made at all.
- **What users see.** The meeting page says when the transcript will be, or was,
  deleted; after it, **Regenerate** on the summary is refused.

As a backstop, while the setting is on, each transcript segment and assistant
answer is written with a DynamoDB time to live of at most two days. A deletion
that keeps failing is retried for up to 24 hours and then raises the
`transcript-deletion-dead-letter-queue-depth` alarm (see
[Monitoring & Alerting](monitoring.md)). See
[Transcript and Recording Deletion After the Summary](transcript-deletion.md).

### Recording deletion after the summary

**Retention › Delete the recordings after the summary** deletes each meeting's
audio and video recordings permanently once the meeting is summarized, with the
same choices, the same timing rules and the same retries and alarm as the
transcript's setting, and independently of it: either can be on without the
other. It ships set to Off.

- **What is deleted.** Every audio and video recording the meeting record links
  to, in every capture session, every version and delete marker of each file;
  the WebSocket transcriber's raw audio beside each recording; a Virtual
  Participant's leftover
  video segments; and the meeting record's links to them.
- **What is kept.** The summary, the transcript (which follows its own setting),
  key frames and their text, chat attachments, the consent record and its
  evidence images, and the meeting record.
- **Late recordings.** A recording stored after the deletion, which happens
  because the transcriber stores a session's recording after the session ends,
  or one from a later session of a rejoined or continued meeting, is deleted as
  soon as it reaches the meeting record.
- **What users see.** The meeting page says when the recordings will be, or
  were, deleted; once they are, it offers no player and no recording download.

The two Regulated presets and the Minimised content preset set it to
**Immediately**; the other presets leave it Off. See
[Transcript and Recording Deletion After the Summary](transcript-deletion.md).

### Deleting a meeting

A meeting's owner can delete it, and an Admin can delete any meeting, from the
meetings list or from the meeting page's **⋮** menu. People the meeting is shared
with cannot delete it. The meeting disappears from their meeting lists, without a
notification. See [Meeting Deletion](user-based-access-control.md#meeting-deletion).
Every meeting that carries given labels can be deleted at once with
[`lma meetings delete`](lma-cli.md#lma-meetings-delete--delete-meetings-by-label),
which goes through the same per-meeting deletion and so removes the same parts.

Deleting a meeting removes, for that meeting:

- the meeting record and its meeting-list rows, and with them the summary, the
  agenda, the speaker names and the consent record;
- every transcript item, partial or final, including chat lines and meeting
  assistant answers;
- the usage records behind its cost estimate;
- its key-frame items and images, and its consent evidence images;
- its chat attachments: their items, and every version of their files;
- its notifications, and every user's dismissals and mutes on it;
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
recovery for `MeetingDataPointInTimeRecoveryDays` days, 35 by default, and not
at all when it is 0 (see
[Point-in-time recovery of the DynamoDB tables](#point-in-time-recovery-of-the-dynamodb-tables)).
Deleting a user from [User Management](user-management.md)
does not delete the meetings they own. It withdraws the consents they gave in
advance, which are then kept until they expire (see
[Withdrawing](meeting-consent.md#withdrawing)), and deletes their last sign-in
and last active times.

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
  the ASR MicroVM speech engine.
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
  key frames and their text, and the meeting chat are not redacted. Chat
  attachments, and the titles and summaries a model writes about them, are not
  redacted either; since they are captured only with the meeting chat, which is
  not added while redaction is on, none are captured in a meeting that redacts. See
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
profile. All seven set the same settings, so applying one replaces whatever another
preset set, and leaves every other setting as it is. See
[Configuration presets](configuration-guide.md#configuration-presets). The two
hosting presets, Team-hosted and Customer account, are explained in
[Hosting Models](hosting-models.md), and Minimised content in
[Minimised content](#minimised-content).

| Setting | Standard | Internal meetings – notify participants | Regulated – consent required, transcript not retained | Regulated – notify with opt-out, transcript not retained | Team-hosted (external meetings) | Customer account | Minimised content – summary only |
|---|---|---|---|---|---|---|---|
| Consent mode | Off | Notify | Opt-in | Notify | Opt-in | Opt-in | Opt-in |
| Consent rule (opt-in) | Each participant | Each participant | Each participant | Each participant | Each participant | Each participant | Each participant |
| Allow a different choice per meeting | On | On | Off (stricter only) | Off (stricter only) | Off (stricter only) | On | Off (stricter only) |
| Keep evidence screenshots | On | On | On | On | On | On | On |
| Video sentence | Shipped sentence | Shipped sentence | Shipped sentence | Shipped sentence | Shipped sentence | Shipped sentence | Shipped sentence |
| Streaming clients without a consent step | Accept with a warning | Accept with a warning | Refuse | Refuse | Refuse | Refuse | Refuse |
| Virtual Participant leaves after an objection | Off | Off | On | On | On | Off | On |
| Allow consent given in advance | On | On | On | On | On | On | On |
| Longest consent given in advance | 365 days | 365 days | 180 days | 180 days | 180 days | 365 days | 365 days |
| Keep sign-in device details with consents given in advance | Off | Off | Off | Off | Off | Off | Off |
| Delete the transcript after the summary | Off | Off | 15 minutes after | 15 minutes after | Off | Off | Immediately |
| Delete the recordings after the summary | Off | Off | Immediately | Immediately | Off | Off | Immediately |
| Record meeting audio | On | On | Off | Off | Off | Off | Off |
| Record screen video from streaming clients | On | On | Off | Off | Off | On | Off |
| Record Virtual Participant meeting video | On | On | Off | Off | Off | On | Off |
| Add the meeting chat to the transcript | On | On | Off | Off | On | On | Off |
| Redact personal information | Off | Off | On | On | Off | Off | On |
| Model inference geography | Global | Global | Same geography | Same geography | Global | Global | Same geography |
| Meeting assistant: web search | On | On | On | On | Off | Off | On |
| Meeting assistant: MCP servers | On | On | On | On | Off | On | On |
| Participant use | Current meeting only | Current meeting only | Current meeting only | Current meeting only | Current meeting only | Current meeting only | Current meeting only |
| Participant use: meeting context and checklist | None | None | None | None | None | None | None |
| Participant use: who may ask | Anyone | Anyone | Anyone | Anyone | Off | Anyone | Anyone |
| Meeting assistant: leave out sensitive personal information | Off | Off | Off | Off | Off | Off | On |
| Default summary profile for new meetings | `default` | `default` | `default` | `default` | `discovery-session` | `default` | `minimised-content` |
| Share meetings with their listed participants | On | On | On | On | On | On | On |

No preset sets **Read spoken replies** or **Capture chat attachments**, which
is off by default. Under the consent-required preset, Stream
Audio/Video and the Desktop Capture App cannot be used to start a meeting, the Webex
join cannot be granted, and the Teams browser join is granted only by consent
said aloud; the Team-hosted and Minimised content presets use the same consent
settings, with the same consequences. Under the notify preset, every capture method that shows a consent
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
| `AudioRecordingExpirationInDays` | 90 | How long audio and video recordings are kept, unless **Delete the recordings after the summary** deletes them sooner |
| `CloudWatchLogsExpirationInDays` | 90 | How long logs are kept |
| `EnableVideoRecording` | `true` | Set `false` if no meeting video, and so no key frames, may be kept from any source; no setting can turn it back on. With `true`, the two Regulated presets, the Team-hosted preset and the Minimised content preset turn off both video settings (**Record screen video from streaming clients** and **Record Virtual Participant meeting video**), and an Admin may turn them on again |
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
   support the each-participant rule, and tell users that Stream Audio/Video, the
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
9. Decide **Meeting Sharing › Share meetings with their listed participants**
   (on in every preset): on, each person on a meeting's participant list who
   has, or is given, an LMA account reads that meeting read-only.
10. Decide how the day or two for which the recordings bucket keeps noncurrent
   object versions and Amazon Bedrock model invocation logging in the account fit
   your policy, and set `MeetingDataPointInTimeRecoveryDays`: 0 if a deletion must
   leave no restorable copy, or the shortest window your recovery needs allow.
   Check that no AWS Backup plan in the account covers the stack's tables (see
   [Point-in-time recovery of the DynamoDB tables](#point-in-time-recovery-of-the-dynamodb-tables)).
11. Set `EnableDataRetentionOnDelete` to match what must happen to the data when
    the deployment is removed.

## Minimised content

The **Minimised content – summary only** preset (`minimised-content`) is for a
deployment that must keep as little personal content as possible. Under it the
summary is the only record of what was said that LMA keeps once the meeting is
summarized, and the model that writes the summary, and the meeting assistant,
are told to leave sensitive personal information out of what they write. Some
other content is still kept; see
[What can still hold sensitive personal information](#what-can-still-hold-sensitive-personal-information).

### What the preset sets

- **Consent first.** Opt-in with the each-participant rule; a meeting may only
  use this mode or a stricter one; streaming clients without a consent step are
  refused; the Virtual Participant leaves after an objection; evidence screenshots
  are kept. These are the settings of the Regulated – consent required preset,
  with the same consequences: Stream Audio/Video and the Desktop Capture App
  cannot start a meeting, and the Webex join is never granted.
- **No transcript kept.** **Delete the transcript after the summary** is
  Immediately, so the transcript, the meeting assistant's answers stored with it,
  and the transcript file in the recordings bucket are deleted as soon as the
  summary is written (see
  [Transcript and Recording Deletion After the Summary](transcript-deletion.md)). The meeting
  chat is not added to the transcript, and personal information is redacted from
  it while it exists (in en-US, en-AU, en-GB and es-US).
- **No recordings.** **Record meeting audio**, **Record screen video from
  streaming clients** and **Record Virtual Participant meeting video** are off,
  so no client records the meeting's audio or video and no key frames are kept,
  whatever a Virtual Participant's **Record meeting video** toggle says. **Delete
  the recordings after the summary** is Immediately, so a recording that is
  still stored, such as an uploaded meeting's audio, is deleted as soon as the
  summary is written. The
  `EnableVideoRecording` stack parameter is the deployment-wide limit: set to
  `false`, no setting, profile or launch records video, so a profile copied from
  this one that turns video back on still records none.
- **Same geography.** Amazon Bedrock models run within this deployment's
  geography.
- **Consent given in advance.** Allowed, for at most 365 days, and kept without
  the sign-in device's details (see
  [Consent given in advance](#consent-given-in-advance)).
- **Shared with the participant list.** **Meeting Sharing › Share meetings with
  their listed participants** is on, as in every preset, so each person on a
  meeting's participant list who signs in to LMA reads the meeting read-only,
  and sees the names of the others on the list (see
  [Sharing with the participant list](user-based-access-control.md#sharing-with-the-participant-list)).
- **Summary and assistant rule.** New meetings are summarized with the
  shipped `minimised-content` summary profile, and **Meeting Assistant › Leave
  out sensitive personal information** is on, which appends a rule to the
  assistant's system prompt after its fixed part, so whatever **Assistant
  instructions** say, the rule follows them. Both tell the model to leave out sensitive personal information about
  anyone: health and medical information (conditions, symptoms, diagnoses,
  treatments, medication, disabilities, pregnancy and mental health), racial or
  ethnic origin, religious or philosophical beliefs, political opinions, trade
  union membership, sex life and sexual orientation, gender identity, genetic and
  biometric data, and criminal convictions or allegations. The summary profile's
  sections (a summary, the action items and, for a meeting with an agenda, its
  coverage) are the default Transcript Summary sections of the same names with
  the rule added to each, and where such information bears on a decision or an
  action item the summary says only that a personal matter was raised. Asked for
  such information, the assistant says that it leaves it out and answers the
  rest of the request.

The other meeting assistant settings keep their shipped defaults. The summary
profile is shipped in every deployment's Summary Profiles section, whichever
preset is active, so it can also be chosen for a single meeting.

### The omission is an instruction to the model

The rule is part of the prompt. It is not a filter applied to what the model
writes, and nothing checks the output against it. A model usually follows it, but
it can miss something, particularly information that is implied rather than
named, so review summaries where that matters. Measured by calling the default
summary and meeting assistant models directly, outside LMA and without the
assistant's tools, on one short scripted meeting in which a participant names a
health condition and its treatment as the reason for an action item: with the
shipped default prompts, 4 of 6 summary sections named a medical need, and with
the `minimised-content` sections 0 of 10 did; asked about it, the meeting
assistant named the condition in 5 of 5 answers with the shipped instructions and
in 0 of 5 with the same rule added after them. That is a small sample of one conversation, not a
measured rate, and a different model or meeting can behave differently.

The rule applies only where its prompts are used:

- a meeting whose owner chooses another summary profile when it starts or on the
  meeting page, or whose summary is regenerated with another profile, is
  summarized without it;
- an Admin who edits the summary profile, or turns **Leave out sensitive
  personal information** off, in a Custom profile copied from this one removes
  it; editing **Assistant instructions** does not;
- a Virtual Participant's voice assistant (Amazon Nova Sonic, or an ElevenLabs
  agent) has its own instructions, which the preset does not change; a question
  it passes to the meeting assistant is answered with the rule, but what the
  voice assistant says in its own words is not covered.

### What can still hold sensitive personal information

With the preset active, these can still hold such information:

- **The summary itself**, if the model fails to leave something out. It is kept
  with the meeting for `MeetingRecordExpirationInDays`, and indexed in the
  transcript knowledge base unless `TranscriptKnowledgeBase` is `DISABLED`.
- **Answers the meeting assistant gives during the meeting.** They are shown to
  whoever asked, and a Virtual Participant posts the answer to a question asked in
  the meeting chat into that chat, where the meeting platform keeps it under its
  own rules. LMA's copy is deleted with the transcript.
- **Text read from shared-screen key frames**, if a profile copied from this one
  turns video back on where `EnableVideoRecording` is `true`: the key frames and the text read from them
  are kept for **Days to keep transcript segments**, not deleted with the
  transcript.
- **Chat attachments, if captured.** The preset does not add the meeting chat to
  the transcript, and redaction is on, so no attachment is captured under it. A
  profile copied from it that turns the chat, and **Capture chat attachments**,
  back on stores the images and files shared in the chat, and their AI titles
  and summaries, which the instruction does not cover, until the transcript
  deletion removes them with the transcript.
- **Consent evidence images.** **Keep evidence screenshots** is on, so a
  screenshot taken as consent evidence stays with the meeting; it can show
  participants' names, faces and anything else on screen. Turn the setting off to
  keep only each check's result.
- **The meeting record**: its title, participant list, speaker names, agenda,
  meeting context and checklist, which are kept with the meeting.
- **Consents given in advance**, which name the person and the organizer and
  are kept under the person's address, not with the meeting, until
  `MeetingRecordExpirationInDays` days after they stop counting.
- **Copies outside the meeting**: logs at `LogLevel` `DEBUG` or
  `AppSyncFieldLogLevel` `DEBUG`/`ALL`, Amazon Bedrock model invocation logging
  if the account has it on, the meetings table's point-in-time recovery window,
  and the recordings bucket's noncurrent object versions (see
  [Copies that follow their own lifetime](#copies-that-follow-their-own-lifetime)).

### Deploy-time steps

As for a regulated deployment, also set: `EnableVideoRecording` to `false`;
`TranscriptKnowledgeBase` to `DISABLED` if summaries must not be indexed for
search across meetings; `LogLevel` to `INFO` or `WARN`; and
`MeetingRecordExpirationInDays`, `TranscriptionExpirationInDays`,
`AudioRecordingExpirationInDays` and `CloudWatchLogsExpirationInDays` to the
periods your policy allows. See
[Checklist for a regulated deployment](#checklist-for-a-regulated-deployment).

## Related

- [Meeting Consent](meeting-consent.md)
- [Transcript and Recording Deletion After the Summary](transcript-deletion.md)
- [Configuration Guide](configuration-guide.md) and
  [Unified Configuration Store](configuration-store.md)
- [Infrastructure & Security](infrastructure-and-security.md)
- [Meeting Video and Key Frames](meeting-video.md)
- [Voice Enrollment (Speaker Recognition)](voice-enrollment.md)
- [User-Based Access Control](user-based-access-control.md)
- [CloudFormation Parameters Reference](cloudformation-parameters.md)
- [Cleanup](cleanup.md)
