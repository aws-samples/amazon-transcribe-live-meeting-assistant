---
title: "Demo Videos"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Demo Videos

This page collects the demonstration videos for the Live Meeting Assistant (LMA), organized by feature area. For an illustrated one-page tour with the same videos, see [LMA in Action](lma-in-action.md).

## Table of Contents

- [Overview](#overview)
- [What's new in LMA 0.4.0](#whats-new-in-lma-040)
- [Recording a new demo](#recording-a-new-demo)

---

## Overview

### Live Meeting Assistant: an AI teammate in your meetings
An introduction to LMA for new viewers, and why it is much more than a note taker. LMA joins a meeting as a participant (Google Meet here, and the same in Zoom, Teams, Webex and Chime), posts a consent notice, and transcribes live, telling apart two people sharing a conference-room microphone and translating on the fly. During the meeting the assistant answers questions about what is still open, searches the web, and replies to questions typed in the meeting chat, or asked out loud through the voice assistant. It extends to other systems through MCP connectors such as Salesforce, and is itself an MCP server that agents such as Amazon Quick can use. Afterwards come the summary, agenda coverage, key frames and questions across all meetings, with consent records and retention under your control.

**Duration**: ~5 minutes

https://github.com/user-attachments/assets/32ad7709-6866-4051-95b0-c5a35fb9fb87

**Related Documentation**: [Quick Start Guide](./quick-start-guide.md), [MCP Servers](./mcp-servers.md), [Amazon Quick MCP Setup](./amazon-quick-mcp-setup.md), [Meeting Consent](./meeting-consent.md), [Voice Assistant](./voice-assistant.md)

### Live Meeting Assistant overview
The project's demo of many of LMA's features, also shown at the top of the [README](../README.md).

https://github.com/user-attachments/assets/7642f659-ed9e-4abf-8baf-2f6fb27b08cb

**Related Documentation**: [Quick Start Guide](./quick-start-guide.md)

---

## What's new in LMA 0.4.0

### What's new in LMA 0.4.0: a meeting's life
A tour of the 0.4.0 release following one meeting from its invitation to its shared summary. LMA reads the agenda from a pasted invitation and joins a Google Meet (the same in Zoom, Teams, Webex and Chime), posts its informed-consent notice in the meeting chat, separates two people speaking from one conference room, and answers a question typed in the chat. The finished meeting shows the new header and tabs, summary sections with agenda coverage, speakers named from their introductions, key frames of shared slides, how the meeting was captured with its consent record and itemised cost, transcript retention and deletion settings, the Download menu and CLARITY CHECK. It ends with configuration profiles and the troubleshooting agent.

**Duration**: ~4 minutes

https://github.com/user-attachments/assets/ab0245f3-b9e9-4cf3-859d-a80b1b95eafd

**Related Documentation**: [Meeting Consent](./meeting-consent.md), [Transcript Deletion](./transcript-deletion.md), [Virtual Participant](./virtual-participant.md)

### The Virtual Participant in depth
Scheduling a Virtual Participant for later and editing it before it starts, then directing it from the meeting chat: asking for help, pausing and resuming the transcript, and, with two LMAs in one Google Meet, asking one of them by name to leave while the other stays.

**Duration**: ~2 minutes

https://github.com/user-attachments/assets/0b452f88-2cbf-4291-a121-ad303a754b37

**Related Documentation**: [Virtual Participant](./virtual-participant.md)

### The meeting page in depth
A tour of the meeting page: the compact header and tabs, playing the recording from any transcript line, transcript search and speaker filters, regenerating or hand-editing the summary, the agenda with AGENDA CHECK, and the Download menu.

**Duration**: ~3 minutes

https://github.com/user-attachments/assets/c907513e-7266-4528-9288-287779dd8b0e

**Related Documentation**: [Web UI Guide](./web-ui-guide.md#meeting-detail-page)

### Speakers and the shared screen
How LMA tells apart and names several people sharing one microphone, merges voices, and lets each name be shown as one person or several; voice enrollment; and searchable key frames of shared slides with their own summary section.

**Duration**: ~2 minutes

https://github.com/user-attachments/assets/00ea3e11-3876-4e36-8061-5a8dcd948fb4

**Related Documentation**: [Web UI Guide](./web-ui-guide.md#meeting-detail-page), [Voice Enrollment](./voice-enrollment.md), [Meeting Video and Key Frames](./meeting-video.md)

### The assistant, consent and privacy
The new MISSING ACTION OWNER or ETA quick action, the Claude Haiku 5.5 default with prompt caching, model inference geography, meeting consent modes and the per-meeting choice, deleting transcripts after the summary, and the consent record kept with each meeting.

**Duration**: ~2 minutes

https://github.com/user-attachments/assets/74899379-056e-4d7a-a7fc-4d23f9132033

**Related Documentation**: [Meeting Consent](./meeting-consent.md), [Transcript Deletion](./transcript-deletion.md)

### Configuration and administration
The View / Edit Configuration page: named profiles and Managed presets, Form, JSON or YAML editing, revision history with a comparison of two revisions, saving edits as a new profile, and the Claude 5.5 models.

**Duration**: ~2 minutes

https://github.com/user-attachments/assets/3b3bfd8e-2c22-412b-8bf5-421221bb770c

**Related Documentation**: [Configuration Guide](./configuration-guide.md#configuration-profiles)

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
