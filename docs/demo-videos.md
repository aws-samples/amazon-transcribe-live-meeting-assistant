---
title: "Demo Videos"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Demo Videos

This page collects the demonstration videos for the Live Meeting Assistant (LMA), organized by feature area. For an illustrated one-page tour with the same videos, see [LMA in Action](lma-in-action.md).

## Table of Contents

- [Overview](#overview)
- [What's new in this release](#whats-new-in-this-release)
- [Earlier releases](#earlier-releases)
- [Recording a new demo](#recording-a-new-demo)

---

## Overview

### Live Meeting Assistant: an AI teammate in your meetings
An introduction to LMA for new viewers, and why it is much more than a note taker. LMA is invited to a Google Meet from a pasted invitation (the same in Zoom, Teams, Webex and Chime), posts its consent notice in the meeting chat, and transcribes live with either of its two speech engines, Amazon Transcribe or its own on-demand engine, telling apart two people sharing a conference-room microphone and translating on the fly. During the meeting the assistant answers what is still open, searches the web, runs a one-button CLARITY CHECK, and replies to a question typed in the meeting chat; the voice assistant can also answer out loud or act as a live interpreter. It extends to other systems through MCP connectors, and is itself an MCP server for agents such as Amazon Quick. Afterwards come the summary, including the meeting chat and every link shared in it, the audio or video recording, speakers named from their introductions, shared slides captured with titles and descriptions, downloadable as a PDF, translated by the assistant and shown as thumbnails in the summary, questions across all meetings, the consent record, retention and cost controls, and administration.

**Duration**: ~8 minutes

https://github.com/user-attachments/assets/ccb1e079-3a9e-4e94-990a-c39043b659dd

**Related Documentation**: [Quick Start Guide](./quick-start-guide.md), [MCP Servers](./mcp-servers.md), [Amazon Quick MCP Setup](./amazon-quick-mcp-setup.md), [Meeting Consent](./meeting-consent.md), [On-demand ASR](./microvm-asr.md)

### Meeting consent and compliance
For compliance, privacy and legal teams. The three consent modes (off, notify, opt-in by each participant or by the owner's attestation), the notice, the video sentence added whenever video is recorded, and the reply phrases; which options work with the Virtual Participant, Stream Audio/Video, the browser extension and the Desktop Capture App, and why the Virtual Participant reads only chat messages sent to everyone. Then consent given in advance: the owner invites the meeting's listed participants, and a Participant signs in to My consents and agrees for that meeting or for the organizer's meetings, reading the exact text first. In a live Google Meet under opt-in, LMA names an attendee whose consent given in advance is on record, asks everyone else, starts recording once everyone present is covered, stops at once when a participant objects, and stops again when a participant withdraws their consent on My consents during the meeting. Afterwards the Participant reads the shared meeting and asks the assistant about it; the consent record keeps the notice, every reply, the consents given in advance, objections, withdrawals, evidence screenshots and a timeline; the recordings are deleted once the summary is written; and configuration presets keep different policies side by side.

**Duration**: ~7 minutes

https://github.com/user-attachments/assets/999ede25-a0ba-4aba-b4ee-1bc30a4c9402

**Related Documentation**: [Meeting Consent](./meeting-consent.md), [Consent given in advance](./meeting-consent.md#consent-given-in-advance), [Privacy and Data Handling Controls](./privacy-and-data-handling.md), [User Management](./user-management.md), [Configuration Guide](./configuration-guide.md#configuration-presets)

### An AI coach for a requirements workshop
A software vendor gathers requirements from a customer in a Google Meet, three people sharing the customer's board-room microphone, with LMA coaching the vendor's analyst as the meeting happens. After the consent notice, FACT CHECK corrects a figure that was just quoted, CLARITY CHECK lists requirements that are still too vague to build from, the assistant suggests the questions not yet asked, AGENDA CHECK tracks the five agenda items and MISSING ACTION OWNER or ETA finds follow-ups with no owner or date. After the meeting, the assistant drafts a software requirements specification from what was said.

**Duration**: ~5 minutes

https://github.com/user-attachments/assets/dc00b633-0ddd-4039-be4b-abef1555d797

**Related Documentation**: [Meeting Assistant](./meeting-assistant.md), [Virtual Participant](./virtual-participant.md)

### Live Meeting Assistant overview
The project's demo of many of LMA's features, also shown at the top of the [README](../README.md).

https://github.com/user-attachments/assets/7642f659-ed9e-4abf-8baf-2f6fb27b08cb

**Related Documentation**: [Quick Start Guide](./quick-start-guide.md)

---

## What's new in this release

### What's new in this release: a meeting's life
A tour of this release following one meeting from its invitation to its shared summary: the participant list in the Create Virtual Participant form; meeting preparation with a summary profile, a label, a briefing for the assistant and a checklist; consent given in advance on the My consents page; the live transcript and a whiteboard the assistant keeps updated; live guidance in the Notifications drawer, raising what the meeting has not yet covered; a question asked in the meeting chat and answered there; the summary in the chosen profile's sections with structured action items, and the Download menu with every format, the meeting export and one archive; the meeting shared with its participants, read on a Participant's Shared meetings page; and, for administrators, the new configuration presets, the Participant role and Last active on User Management, and meeting notifications.

**Duration**: ~3 minutes

https://github.com/user-attachments/assets/61aa2f96-9ae1-48dc-8f24-335f5ebb88d5

**Related Documentation**: [Meeting Context and Checklist](./meeting-context-and-checklist.md), [Live Guidance](./live-guidance.md), [Meeting Whiteboards](./meeting-whiteboards.md), [Meeting Consent](./meeting-consent.md#consent-given-in-advance), [Meeting Export](./meeting-export.md), [Meeting Notifications](./meeting-notifications.md), [Hosting Models](./hosting-models.md)

### The meeting page in depth
A tour of the meeting page: the compact header with its actions in one row and the speakers named from their introductions; the agenda derived from the conversation and the recording player on the Transcript tab; search, speaker filters and live translation; editing the summary by hand, with protection against regenerating over the edit; the Participants panel, with each listed person's LMA account, the invitation to consent in advance, and sharing with everyone listed; whiteboards made from a template, with the switch that lets the assistant keep them updated; the notice that says when and why the recordings were deleted after the summary; and the Download menu's summary formats and Email summary, addressed to the participant list.

**Duration**: ~3 minutes

https://github.com/user-attachments/assets/02165bb4-340b-4add-b564-8943bf6c7821

**Related Documentation**: [Web UI Guide](./web-ui-guide.md#meeting-detail-page), [Meeting Whiteboards](./meeting-whiteboards.md), [Meeting Export](./meeting-export.md), [Transcript Deletion](./transcript-deletion.md)

### The assistant, consent and privacy
The MISSING ACTION OWNER or ETA quick action finding follow-ups with no owner or date; Participant use, which decides what the assistant may use to answer anyone other than the meeting's owner and the people it is shared with, and who in the meeting may ask; the switches that turn web search and MCP servers off for every request; a Participant asking the assistant about a meeting shared with them, answered from that meeting only; Leave out sensitive personal information; model inference geography; and the Minimised content preset compared with Standard.

**Duration**: ~3 minutes

https://github.com/user-attachments/assets/ad4783be-7d9d-4d0c-a2f3-bc1112e4b0ce

**Related Documentation**: [Meeting Assistant](./meeting-assistant.md#who-may-ask), [Meeting Consent](./meeting-consent.md), [Configuration Guide](./configuration-guide.md#configuration-presets), [Privacy and Data Handling Controls](./privacy-and-data-handling.md)

### Configuration and administration
The View / Edit Configuration page: named profiles and the Managed presets, including the new Team-hosted (external meetings), Customer account and Minimised content presets, each of which now also sets the meeting assistant and summary settings; who activated the active profile and when, with the activation and deletion history; editing a profile as a form or YAML, with every save a revision; and new settings in this release (Participant use: who may ask, the video sentence, deleting recordings after the summary, recording Virtual Participant meeting video). Then User Management, with the Admin, User and Participant roles and each account's Last sign-in and Last active; creating a Participant who is invited to a meeting; and an MCP API key's Last used time.

**Duration**: ~3 minutes

https://github.com/user-attachments/assets/28f88e4a-396f-4520-bbde-909e97a1a6dc

**Related Documentation**: [Configuration Guide](./configuration-guide.md#configuration-profiles), [User Management](./user-management.md), [Hosting Models](./hosting-models.md), [MCP API Key Authentication](./mcp-api-key-auth.md)

## Earlier releases

### The Virtual Participant in depth
Scheduling a Virtual Participant for later and editing it before it starts, then directing it from the meeting chat: asking for help, pausing and resuming the transcript, and, with two LMAs in one Google Meet, asking one of them by name to leave while the other stays.

**Duration**: ~2 minutes

https://github.com/user-attachments/assets/3086d7ac-2de2-47ee-9c7d-a3f0394b5b4d

**Related Documentation**: [Virtual Participant](./virtual-participant.md)

### Speakers and the shared screen
How LMA tells apart and names several people sharing one microphone, with the words each name came from; naming a voice by hand; showing a room as one person or several people; voice enrollment; and searchable key frames of shared slides with their own summary section.

**Duration**: ~2 minutes

https://github.com/user-attachments/assets/5dde83b5-482d-49bd-9774-5465b78565d1

**Related Documentation**: [Web UI Guide](./web-ui-guide.md#meeting-detail-page), [Voice Enrollment](./voice-enrollment.md), [Meeting Video and Key Frames](./meeting-video.md)

---

## Recording a new demo

The repository includes a recorder that turns a browser session against a deployed stack into a narrated, captioned mp4 with title, chapter and takeaway cards. The procedure, from proposing a storyboard to drafting the entry for this page, is the [product demo skill](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/blob/develop/.claude/skills/product-demo.md) (`.claude/skills/product-demo.md`), and the recorder is `scripts/ux_recorder.py`.

Videos on this page are attached as GitHub assets: drag the mp4 into a comment on a pull request or issue in this repository, then paste the resulting `https://github.com/user-attachments/assets/...` URL into the entry. Record against a disposable stack with synthetic or shipped audio; a recording shows whatever is on screen, unredacted. Upload only the mp4: the rest of its session directory, which logs clicks with text from the page, is never committed or uploaded.

A new entry goes under the heading of the feature area it shows, in this shape:

```markdown
### <Title>
<One paragraph: what it shows and why it matters.>

**Duration**: ~<m> minutes

<the https://github.com/user-attachments/assets/... URL>

**Related Documentation**: [<Doc title>](./<doc>.md#<anchor>)
```

Add a heading for a feature area that has no video yet, and list it in the table of contents.
