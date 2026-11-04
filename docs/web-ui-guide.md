---
title: "Web UI Guide"
---

# Web UI Guide

## Table of Contents

- [Overview](#overview)
- [Navigation](#navigation)
- [Meeting List / Dashboard](#meeting-list--dashboard)
- [Manual Action Alerts](#manual-action-alerts)
- [Virtual Participant Pages](#virtual-participant-pages)
- [Meeting Detail Page](#meeting-detail-page)
- [Sentiment Analysis](#sentiment-analysis)
- [Transcript Downloads](#transcript-downloads)
- [Meeting Sharing](#meeting-sharing)
- [Meeting Deletion](#meeting-deletion)
- [Live Translation](#live-translation)
- [Admin Configuration Pages](#admin-configuration-pages)
- [Deployment Info](#deployment-info)

## Overview

The LMA web UI is a React application hosted on Amazon S3 and served via Amazon CloudFront. Authentication is handled by Amazon Cognito, which manages user sign-up, sign-in, and access control. The UI provides real-time meeting transcription, an AI-powered chat assistant, meeting management, and administrative configuration.

## Navigation

The left sidebar organizes the application into the following sections:

### Sources
- **Virtual Participant** — Join meetings via a virtual participant bot
- **Stream Audio (from Mic+Browser)** — Stream audio directly to LMA from any Chrome tab (meetings, softphones, YouTube, audio/video playback)
- **Chrome Extension** — Information, download, and install instructions for the LMA Chrome browser extension (see [Browser Extension](browser-extension.md))

### Meetings
- **Meeting List** — View and manage all accessible meetings
- **Meetings Query Tool** — Advanced search and filtering across meetings

### Configuration (admin only)
- **Configuration** — Every runtime setting in one place: summary prompts, chat buttons, Nova Sonic, transcription engine and meeting cost prices (see [Configuration Guide](configuration-guide.md))
- **MCP Servers** — Install and manage MCP server integrations
- **Configuration History** — Every saved revision of the configuration: view, compare, label and restore
- **User Management** — Create and delete LMA users (see [User Management](user-management.md))

### Deployment Info
- Stack name, build date, and version information

## Meeting List / Dashboard

The meeting list provides a searchable dashboard of all meetings you have access to.

- **Columns**: Meeting Topic, Date/Time, Duration, Status, Owner, Shared With
- **Status indicators**: In Progress, Ended
- **Multi-select**: Check multiple meetings for batch operations (share, delete)
- **Configurable time range**: Use the **Load** dropdown to choose a preset window (2 hrs, 4 hrs, 8 hrs, 1 day, 2 days, 1 week, 2 weeks, 30 days) or pick **Custom…** to open a modal with explicit Start date + Start time (UTC) + End date + End time (UTC) inputs (±365-day window). The selected range is persisted in `localStorage` and the list header shows both the number of loaded meetings and the RBAC-filtered server-side total (e.g. `42 loaded of 127` or `500+` when the count is truncated). The meeting list is served by a DynamoDB GSI (`TypeDateIndex`) and a Lambda resolver that paginates a single date-range query, so performance scales to thousands of meetings without scans.

Use the search bar to filter meetings by topic or other attributes.

## Manual Action Alerts

A **persistent Flashbar** at the top of the LMA UI surfaces any of your Virtual Participants currently in the `MANUAL_ACTION_REQUIRED` state — a CAPTCHA, 2FA prompt, SSO redirect, unknown consent dialog, or other Zoom verification step that needs human input. Each alert links directly to the affected meeting detail page where you can open the live VNC viewer and complete the challenge.

- Alerts arrive in real time via an AppSync GraphQL subscription, with a one-time backfill on page load so anything fired while the tab was closed still shows up.
- Each alert is **dismissible** — dismissed alerts are remembered in `localStorage` (per VP id) so they don't reappear on every refresh after you've dealt with them.
- If you grant the **browser notification permission** in the LMA UI, you also get a desktop notification + audio chime when a new MANUAL_ACTION fires — useful when you're tabbed-away or in another window. Most browsers prompt for this permission on first sign-in; deny and it falls back to in-page alerts only.

See [Troubleshooting › VP Stuck at MANUAL_ACTION_REQUIRED](troubleshooting.md#vp-stuck-at-manual_action_required) for how to resolve specific challenge types.

## Virtual Participant Pages

The Virtual Participant section of the UI has two views.

### Virtual Participant List

The **Virtual Participants** sidebar entry opens a list of your in-flight and recent VPs, with status, owner, meeting platform, and meeting name. New VPs appear in the list **without a page refresh** — the list subscribes to `onCreateVirtualParticipant` GraphQL events. Those events, like the list itself, show a VP to its owner, the users it is shared with, and Admins.

### Create Virtual Participant modal

The **Create Virtual Participant** button opens a modal where you enter meeting details and launch the VP. When **Zoom** is selected as the platform, the modal includes a **Zoom account** card:

- ✅ **Signed in as user@example.com — [Update] [Remove]** — your stored Zoom credentials are present. Tick **"Sign in with my stored Zoom account when joining this meeting"** to use them; the VP signs in to Zoom before navigating to the meeting URL.
- ⚠ **Not configured. The VP will join as a guest. [Add Zoom credentials]** — no credentials stored. Click to open a sub-modal with username + password fields. Credentials are stored in AWS Secrets Manager scoped to your Cognito sub; the plaintext password is never returned to the UI.

For the full sign-in flow, join-reliability notes, and operational caveats, see [Zoom Sign-in & Join Reliability](zoom-credentials-and-join-reliability.md).

The modal also has an optional **Agenda** field, one item per line. **Paste Meeting Invitation** fills it from the invitation when the invitation lists an agenda. The agenda is stored with the VP and placed on the meeting record when the meeting starts, for meetings started now and scheduled ones alike. See [Meeting Agenda](meeting-agenda.md).

### Virtual Participant detail page

Click a VP from the list to open its detail page. The page shows:

- **Connection details** — platform, meeting ID, duration, status (with the granular status lifecycle described in [Virtual Participant › Status Lifecycle](virtual-participant.md#status-lifecycle)).
- **Action banner** above the VNC viewer when the VP is in `MANUAL_ACTION_REQUIRED` — describes what challenge needs solving (CAPTCHA, 2FA, SSO, etc.).
- **Live Virtual Participant View** — embedded noVNC viewer of the VP's Chrome window. Toggle **View Only** off to interact with the VP's browser (type a 2FA code, solve a CAPTCHA, click through SSO).
- **Troubleshooting** card — when a VP fails, the human-readable `errorMessage` written by the VP backend is displayed here, along with common solutions for that error class.
- **Status history** with timestamps for each state transition.

## Meeting Detail Page

Clicking a meeting from the list opens the meeting detail page: a compact
header with what the meeting was, and four tabs under it. The selected tab is
part of the page's address (`#/calls/<Meeting ID>?tab=overview`), so a link or
a reload opens the same tab; a link without one opens the **Transcript** tab.
Switching tabs keeps each tab as it was, including a summary or agenda being
edited and the transcript's translation and scroll position. The meeting
assistant is a panel beside the tabs, on every tab; see
[Meeting assistant panel](#meeting-assistant-panel).

### Meeting header

- **Title** — the meeting's name, without the date and time its source added to
  the Meeting ID (for example `Weekly sync` for
  `Weekly sync-2026-11-03-14-05-09-123`). The full Meeting ID is in the title's
  tooltip, the copy button beside it copies it, and the **Details** tab shows it.
  A Meeting ID without a recognised date and time is shown as it is.
- **Status, start time and duration** — the start time is shown in your own time
  zone, with the stored UTC value in its tooltip. The duration is the longer of
  the meeting's stored duration and the time the loaded transcript reaches (see
  [Duration](#duration)).
- **Platform** — when a Virtual Participant joined the meeting, the platform it
  joined (Zoom, Microsoft Teams, Amazon Chime, Webex).
- **Owner** and **Speakers** — the people heard in the meeting, by the names
  given to their voices. **Name speakers** opens the Speakers panel on the
  **Details** tab.
- **Est. cost** — the meeting's estimated cost. Select it to see the itemised
  estimate; see [Meeting Cost Estimate](meeting-cost-estimate.md).
- **Assistant** — opens and closes the [meeting assistant panel](#meeting-assistant-panel),
  when the deployment has the meeting assistant enabled. A dot on the button
  means an answer finished while the panel was closed.
- **Share**, **Download** and the **⋮** menu — **Download** offers the summary
  (copy, text file, Word, email) and, once the meeting has ended, the transcript
  (Excel, text, Word); see [Transcript Downloads](#transcript-downloads). **Delete
  meeting** is in the **⋮** menu. Share and Delete open the same dialogs as on the
  meeting list.

#### Duration

The stored duration (`TotalConversationDurationMillis`) is written as transcript
segments are processed, and holds the end time of whichever final segment was
written last rather than the latest one, so it can be shorter than the meeting.
The page therefore shows the longer of that value and the end of the last final
transcript segment it has loaded; when the two differ, the **Details** tab gives
the stored value as well. Both are measured on the transcript's clock, from when
transcription started, so a recording that began before transcription can be
longer than either.
The meeting list and the MCP tools still read the stored value.

### Overview tab

- **Meeting Summary** — the summary from the configured prompt templates, with
  **Regenerate**, **Edit** and the summary's own download menu. The slides
  named in its **SLIDES PRESENTED** section are links rather than pictures:
  selecting one opens the **Screen share** tab at that frame. The **CHAPTERS**
  section is shown once, as the **Chapters** list beside the summary, and the
  **AGENDA COVERAGE** section under the agenda it describes. Downloads and copies
  of the summary still include every section. See
  [Transcript Summarization](transcript-summarization.md).
- **Chapters** — the meeting's chapters; selecting one opens the **Transcript**
  tab at its time and, after the meeting, plays the recording from there in the
  [player](#the-recording-player). See
  [Meeting Video and Key Frames](meeting-video.md#chapters).
- **Agenda** — the meeting's agenda, who last changed it, and its coverage. A
  meeting without an agenda shows one line with **Derive agenda** and **Add
  agenda** for those who can change it. See [Meeting Agenda](meeting-agenda.md).
- **Sentiment and voice tone** charts, when those features are enabled; see
  [Sentiment Analysis](#sentiment-analysis).

### Transcript tab

The tab a meeting opens on. Real-time transcription displayed with:
- Speaker names and timestamps for each segment; select a name to rename that
  voice (see [Naming the voices](transcription-and-translation.md#naming-the-voices))
- Sentiment indicators per segment
- Color-coded entries by channel for easy visual distinction between speakers
- **Auto-scroll**: while the meeting runs it keeps the newest segment in view;
  after the meeting, with a recording, it keeps the segment being played in
  view. Scrolling away pauses it, and **Resume** returns to the newest segment
  or to the one being played
- **Translate**, with the language chosen beside it; see
  [Live Translation](#live-translation)
- **Search the transcript** and the speaker filter; see
  [Searching and filtering](#searching-and-filtering)

The transcript keeps its own scroll area, sized to the window, so following a
live meeting keeps the newest segment in view while you can still scroll back.

#### The recording player

Once a meeting has ended and its recording has been stored, a player sits above
the transcript. It plays the video recording when the meeting has one (the video
file carries the meeting's audio as well) and otherwise the audio recording; a
meeting with both has a **Video** / **Audio only** switch, which keeps the
place. During a live meeting there is no player, because neither recording
exists until the meeting ends. The player stays at the top of the window when
the page is scrolled.

- **Controls** — play/pause, the time, the seek bar, the speed (0.75x to 2x),
  mute and, for video, full screen. Each has an accessible name, and the seek
  bar is a slider that the keyboard moves.
- **Chapters on the seek bar** — each of the meeting's
  [chapters](meeting-video.md#chapters) is a mark on the seek bar, with its
  title shown on hover or focus; selecting a mark plays from that chapter. The
  chapter being played is named under the bar.
- **The transcript follows the recording** — the segment being heard is
  highlighted, and kept in view while **Auto-scroll** is on. Scrolling the
  transcript while it plays stops it following (a notice says so, with
  **Resume**); playing from a segment, a chapter or the seek bar follows again.
- **Playing from a segment** — after the meeting, each segment's time is a link:
  selecting it plays the recording from the start of that segment. Only the
  time is a link, so the segment's text can still be selected and copied and its
  speaker renamed.
- **Keyboard** — with the Transcript tab open and the focus not in a text field,
  **Space** plays or pauses and **Left** / **Right** move 5 seconds. Space is
  left to a focused button or link, which it presses, and the arrows to a
  focused slider.
- **Only one thing plays** — opening another tab pauses the player. The
  **Details** tab has no player of its own: its recordings have **Play**, which
  opens this tab and plays, and **Open file**.

From the other tabs, a time selected moves the player as well as the
transcript:

| Selected | Tab shown | Player |
|---|---|---|
| A chapter in the Overview's **Chapters** list | Transcript | plays from the chapter |
| A thumbnail on **Screen share** | stays on Screen share | moves to the frame, paused |
| **Show in transcript** on Screen share | Transcript | moves to the frame, paused |
| **Play recording from here** on Screen share | Transcript | plays from the frame |
| A slide link in the summary's **SLIDES PRESENTED** | Screen share, at the frame | moves to the frame, paused |

The ones that only move the player are for looking at something rather than
listening to it; the player is then ready at that point when the Transcript tab
is opened.

##### Lining the recording up with the transcript

Transcript times are on the transcript's clock, which starts when transcription
of the meeting starts; a recording has its own, from the start of its file. The
player converts between them:

| Source | Audio recording | Video recording |
|---|---|---|
| Virtual Participant | Starts with transcription: the same clock | Starts before the Virtual Participant is admitted, so it is ahead of the transcript by the time spent joining and in the lobby, and by more after each pause or reconnection (the video records through them; the transcript skips them). Lined up from the key frames, as below |
| Stream Audio, the browser extension | Starts with transcription | None |
| Desktop Capture App | Starts with transcription | Its file is built with the audio recording as its sound and the video placed against it, so it starts on the same clock |
| Upload Audio | The uploaded file itself, which is what was transcribed | None |

Each [key frame](meeting-video.md) records both its time in the transcript and
its place in the video recording, so the player lines the video up using the key
frame nearest before each time; this follows a Virtual Participant's video
across its pauses as well. A Virtual Participant meeting whose video has no key
frames therefore starts on the audio recording, which lines up with the
transcript: the player says why, and so does the video recording on the
**Details** tab. **Video** still plays the video, as if it started with
transcription, which plays each point earlier in the meeting than the
transcript line it was started from, by the time the Virtual Participant took
to join. The choice is made once the key frames have loaded, so the player can
change to the audio a moment after the page opens. The video of any other
source has no such offset and plays without key frames. A video stored without sound (when a
Virtual Participant's audio was not ready as its recording started, or when the
desktop app's video could not be combined with the audio) plays silently, and
**Audio only** plays the sound. With more than a hundred key frames, only the
ones loaded so far (the first hundred, more as the Screen share strip is
scrolled) are used. Small differences are expected: the transcript's times can
drift from the recording after Amazon Transcribe reconnects during a long
meeting.

#### Searching and filtering

- **Search the transcript** marks every segment that holds all the words typed,
  in any order, ignoring case and accents (the same rule as the search of the
  shared screen's text), and highlights the words. The first match is shown at
  once; **Enter** or **Next match** moves to the next, **Shift+Enter** or
  **Previous match** to the previous, and the count shows which match is shown.
  Moving between matches while the recording plays stops the transcript
  following it, as scrolling does. Matches are
  marked rather than the other segments hidden, so each is read with what was
  said around it. The search reads the segments as transcribed, not their
  translations.
- **Speaker chips** — **All** and one chip for each speaker, by the names shown
  in the transcript. Choosing speakers shows only their segments (and hides the
  assistant's answers); choosing **All** shows everything again. The search
  works within the speakers chosen.

During a live meeting with a Virtual Participant, the VP's **live view** (a
real-time VNC viewer of the VP's browser) appears at the top of this tab when it
is opened from the assistant panel's settings menu (**Open VP Live View**) or by
the assistant. Opening it from another tab switches to this one. You can see and
control what the VP sees in the meeting.

### Meeting assistant panel

The meeting assistant's chat, in a panel on the right of the page, available on
every tab. **Assistant** in the header opens and closes it, and the panel's
**✕** closes it too, as does **Escape** while the keyboard focus is in the
panel. Opening it puts the focus in the box where you type a question.

- **While it is open** the tabs are narrower; when it is closed they have the
  full width. Where the page is narrow (a small window, the meeting list's
  split panel at the side, a narrow embed) the panel lies over the right of
  the tabs instead of beside them. It stays in view while the page scrolls.
- **Open or closed is remembered** for you in this browser, for every meeting.
  A live meeting you have not opened before starts with the panel open; after
  that it is as you last left it.
- **The conversation is kept** when you switch tabs or close and reopen the
  panel. Opening another meeting starts a conversation about that meeting.
- **What it offers:** answers streamed as the Strands agent writes them, and
  [chat shortcut buttons](#chat-buttons) for common requests (Summary, Action
  Items, Topic and more), shown again after each answer.
- **Frames of the shared screen** in an answer open full size when selected.
  Under the full-size frame, **Show in transcript** opens the **Transcript**
  tab at that moment and moves the [player](#the-recording-player) there,
  paused. Where the panel lies over the page, it closes to show the transcript.
- **Settings menu** (administrators, in the Lambda assistant mode): **Edit Chat
  Buttons**, **Open VP Live View** / **Close VP Live View**, and **MCP
  Servers**.

The panel is shown only when the deployment has the meeting assistant enabled;
otherwise there is no **Assistant** button. In the embeddable components, a
`call-details` embed and a `vp-details` embed whose `show` includes `chat` start
with the panel open; see [Embeddable Components](embeddable-components.md).

### Screen share tab

Shown for a meeting whose shared screen was captured, with the number of key
frames in its label. A strip of thumbnails of the shared screen: one for each
slide, page or view, with the time it went up, growing while the meeting runs.
Selecting one shows it large and moves the transcript and the
[player](#the-recording-player) to that time; **Show in transcript** opens the
**Transcript** tab there, and after the meeting **Play recording from here**
opens it and plays the recording from there. **Find text on the
shared screen** searches the slides' text and **Export slides (PDF)** prints them
as a deck. See [Meeting Video and Key Frames](meeting-video.md#on-the-meeting-page).

### Details tab

- **Meeting attributes** — the full Meeting ID, status, duration, the start and
  last-update times in UTC (with your local time under each), platform, owner,
  who the meeting is shared with, and, once the meeting has ended, its audio
  and video recordings: **Play** opens the **Transcript** tab and plays it in the
  [player](#the-recording-player), and **Open file** opens the file in a new
  browser tab, to watch or save.
- **Speakers** — name each voice once and the whole transcript updates; see
  [Naming the voices](transcription-and-translation.md#naming-the-voices).
- **Consent** — for a meeting whose consent was started: its mode and status,
  whether it may be recorded and transcribed now, the notice posted, each
  participant's latest reply, the owner's attestation, the evidence screenshots
  with a vision model's check of each, and a timeline. The owner, anyone the
  meeting is shared with and an Admin can record an objection; the owner and
  Admins can check evidence; the owner can attest that everyone agreed, in an
  opt-in meeting whose rule is that the owner attests. The summary download
  includes the record. See [Meeting Consent](meeting-consent.md#on-the-meeting-page).
- **Estimated cost** — the itemised estimate the header's cost chip opens:
  Transcribe seconds, Bedrock tokens per feature, Nova Sonic, and the Virtual
  Participant's compute, priced at dated list prices. It is labelled as an
  estimate and lists the lines it could not price and what it leaves out. See
  [Meeting Cost Estimate](meeting-cost-estimate.md).

## Sentiment Analysis

LMA provides sentiment analysis at multiple levels:

- **Per-segment indicators** — Each transcript segment displays a sentiment indicator
- **Overall sentiment per channel** — Aggregated sentiment for each speaker/channel
- **Sentiment trend visualization** — Track how sentiment changes over the course of the meeting

## Transcript Downloads

Export meeting transcripts in multiple formats, from **Download** in the meeting
page's header once the meeting has ended:

- **XLSX** — Spreadsheet format with structured columns
- **DOCX** — Word document format
- **TXT** — Plain text format

All export formats include speaker attribution and timestamps for each segment.

## Meeting Sharing

Share meetings with other LMA users:

1. Select one or more meetings from the meeting list
2. Click the share icon
3. Enter recipient email addresses
4. Click Submit

On a meeting's own page, **Share** in its header opens the same dialog for that meeting.

Only meeting owners can share meetings. Recipients receive read-only access to the shared meetings. For full details, see [User-Based Access Control](user-based-access-control.md).

## Meeting Deletion

Delete meetings you own:

1. Select one or more owned meetings from the meeting list
2. Click the delete icon
3. Type "confirm" in the confirmation dialog
4. Click Delete

On a meeting's own page, **Delete meeting** is in the **⋮** menu of its header.

Shared users automatically lose access to deleted meetings. For full details, see [User-Based Access Control](user-based-access-control.md).

## Live Translation

Translate meeting transcripts in real-time:

- On the **Transcript** tab, turn on **Translate** and choose the target language beside it
- Choose from **75+ languages** powered by Amazon Translate
- Translation is performed client-side for low latency

Live translation applies to the transcript panel and updates as new segments arrive.

## Admin Configuration Pages

The following configuration pages are available to admin users only.

The **Configuration** page and the **Edit Chat Buttons** dialog edit settings
that apply to every meeting in the deployment, so saving a change requires
membership of the **Admin** Cognito group. That is enforced by the API, not just
by the menu: the AppSync field pins the operation to the group and the resolver
behind it checks the caller's group membership again, so the two are
independent. Add someone to the group on [User Management](user-management.md)
if they need to change these settings. A user outside the group who opens the
Configuration page sees a short message in place of the form; the Chat Buttons
dialog is reached from a menu that only an Admin is shown.

All of these settings, and the meeting cost prices, are saved into one
configuration profile, which records who saved it — the saver's email address,
or their Cognito username where the token carries none — and when, taken from
the signed-in identity rather than from the request. A save is refused if
someone else saved after the page was loaded, rather than replacing their
change, and every save is recorded as a revision on the **Configuration
History** page. See the [Configuration Guide](configuration-guide.md).

The other two pages in this section work differently and none of the above
applies to them: **MCP Servers** manages one row per installed server, and
**User Management** acts on the Cognito user pool directly.

### Configuration

**Route**: `/#/configuration/settings`

One page for every runtime setting, one section per part of the configuration,
with the form for each generated from that section's schema:

- Values that differ from the shipped defaults are marked, with **Reset to
  default** beside each and **Reset section to defaults** for a whole section
- Values are checked as you type, against the same rules a save is checked
  against
- **Save changes** asks for an optional comment and label for the revision the
  save records, and saves every changed section together
- A save refused because someone else saved first can be reloaded with your
  changes kept over theirs
- `?section=<name>` opens one section directly, for example
  `/#/configuration/settings?section=call_pricing`

See the [Configuration Guide](configuration-guide.md) for full documentation.

### MCP Servers

**Route**: `/#/configuration/mcp-servers`

Install and manage MCP (Model Context Protocol) servers to extend the meeting assistant's capabilities with external tools and services.

See [MCP Servers](mcp-servers.md) for full documentation.

### Pages that moved into Configuration

The **Nova Sonic**, **Transcript Summary** and **Transcription Engine** pages
each edited one section of the configuration. They are now sections of the
**Configuration** page, and their addresses open those sections:

| Earlier address | Opens |
|---|---|
| `/#/configuration/nova-sonic` | The **Nova Sonic Voice Assistant** section. See [Nova Sonic 2 Setup](nova-sonic-setup.md) |
| `/#/configuration/transcript-summary` | The **Transcript Summary** section. See [Transcript Summarization](transcript-summarization.md) |
| `/#/configuration/asr` | The **Transcription Engine** section. Its engine choices are shown only when the on-demand speech engine is deployed (`EnableMicrovmAsr=true`); Virtual Participant voice separation is shown on every deployment. See [MicroVM ASR](microvm-asr.md) |

### User Management

**Route**: `/#/configuration/user-management`

Create and delete LMA users (Admin and non-Admin), directly from the Web UI without needing the AWS console.

- View all users in the Cognito user pool, with role, status, and creation date
- Create a new user and choose their role (Admin or User) — Cognito emails them a temporary password
- Select and delete one or more users
- Guard rails prevent self-delete and deletion of the last remaining Admin

See [User Management](user-management.md) for full documentation.

### Chat Buttons

Customize the chat shortcut buttons that appear in the meeting chat panel:
- Open the **Edit Chat Buttons** modal from the settings menu of the meeting
  detail page's [assistant panel](#meeting-assistant-panel)
- Add new shortcut buttons with custom labels and prompts
- Edit existing button labels and prompts
- Delete buttons you no longer need
- **Reset to Defaults** brings back the shipped buttons

Once any custom button is saved, the custom list replaces the shipped one
entirely; it does not add to it. The buttons are stored in the shared
[configuration store](configuration-store.md), and a save is refused, with a
message to reopen the dialog, if someone else saved the configuration while it
was open.

## Deployment Info

The deployment info section in the sidebar displays:

- **Stack name** — The CloudFormation stack name for your LMA deployment
- **Build timestamp** — When the current version was built
- **Version** — The LMA version number, sourced from CloudFormation outputs
