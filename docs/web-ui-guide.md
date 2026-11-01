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

Clicking a meeting from the list opens the meeting detail page, which contains several panels and sections.

### Transcript Panel

Real-time transcription displayed with:
- Speaker names and timestamps for each segment
- Sentiment indicators per segment
- Color-coded entries by channel for easy visual distinction between speakers

### Chat Panel

Interact with the meeting assistant by typing questions or requests:
- Real-time token streaming responses from the Strands agent
- **Chat shortcut buttons** for common actions: Summary, Action Items, Topic, and more
- Shortcut buttons re-appear after each response for quick follow-up actions

### Summary Section

- On-demand summaries generated during the meeting
- Post-meeting summaries generated automatically when the meeting ends
- Multiple sections based on configured prompt templates
- Copy to clipboard support for easy sharing

### Shared Screen

For a meeting whose video is recorded, a strip of thumbnails of the shared
screen: one for each slide, page or view, with the time it went up, growing
while the meeting runs. Selecting one shows it large and moves the transcript
to that time; after the meeting it also plays the video recording from there.
See [Meeting Video and Key Frames](meeting-video.md#on-the-meeting-page).

### Agenda

The meeting's agenda as a numbered list, with where it came from and who last
changed it. The owner, anyone the meeting is shared with, and an Admin can edit
it while the meeting runs and after it ends, and **Derive agenda** reads the
transcript for an agenda somebody stated. See
[Meeting Agenda](meeting-agenda.md).

### Estimated Cost

A collapsed section showing an itemised estimate of what the meeting cost: Transcribe
seconds, Bedrock tokens per feature, Nova Sonic, and the Virtual Participant's
compute, priced at dated list prices. It is labelled as an estimate and lists the
lines it could not price and what it leaves out. See
[Meeting Cost Estimate](meeting-cost-estimate.md).

### Audio Playback

When recording is enabled, a WAV recording player is available for audio playback of the meeting.

### VNC Viewer

When a Virtual Participant (VP) is active, a real-time VNC viewer displays the VP's browser view. This allows you to see and control what the VP sees in the meeting.

### Meeting Metadata

Displays detailed meeting information including:
- Call ID, status, and duration
- Participants list
- Owner and shared users
- TTL (time-to-live) expiration

## Sentiment Analysis

LMA provides sentiment analysis at multiple levels:

- **Per-segment indicators** — Each transcript segment displays a sentiment indicator
- **Overall sentiment per channel** — Aggregated sentiment for each speaker/channel
- **Sentiment trend visualization** — Track how sentiment changes over the course of the meeting

## Transcript Downloads

Export meeting transcripts in multiple formats:

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

Only meeting owners can share meetings. Recipients receive read-only access to the shared meetings. For full details, see [User-Based Access Control](user-based-access-control.md).

## Meeting Deletion

Delete meetings you own:

1. Select one or more owned meetings from the meeting list
2. Click the delete icon
3. Type "confirm" in the confirmation dialog
4. Click Delete

Shared users automatically lose access to deleted meetings. For full details, see [User-Based Access Control](user-based-access-control.md).

## Live Translation

Translate meeting transcripts in real-time:

- Select a target language from the language dropdown
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
| `/#/configuration/asr` | The **Transcription Engine** section, shown only when the on-demand speech engine is deployed (`EnableMicrovmAsr=true`). See [MicroVM ASR](microvm-asr.md) |

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
- Open the **Edit Chat Buttons** modal from the meeting detail page
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
