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
An introduction to LMA for new viewers, and why it is much more than a note taker. LMA is invited to a Google Meet from a pasted invitation (the same in Zoom, Teams, Webex and Chime), posts its consent notice in the meeting chat, and transcribes live with its own speech engine, telling apart two people sharing a conference-room microphone and translating on the fly. During the meeting the assistant answers what is still open, searches the web, runs a one-click CLARITY CHECK, and replies to a question typed in the meeting chat. It extends to other systems through MCP connectors, and is itself an MCP server for agents such as Amazon Quick. Afterwards come the summary, speakers named from their introductions, key frames, questions across all meetings, the consent record, retention and cost controls, and administration.

**Duration**: ~7 minutes

<!-- VIDEO_URL_LMA_COMPLETE_DEMO: replace this comment and the line below with the GitHub asset URL of the lma-complete-demo recording. -->
*Video coming soon.*

**Related Documentation**: [Quick Start Guide](./quick-start-guide.md), [MCP Servers](./mcp-servers.md), [Amazon Quick MCP Setup](./amazon-quick-mcp-setup.md), [Meeting Consent](./meeting-consent.md), [On-demand ASR](./microvm-asr.md)

### Meeting consent and compliance
For compliance, privacy and legal teams. The three consent modes (off, notify, opt-in by each participant or by the owner's attestation), the notice and reply phrases, and the settings around them; which options work with the Virtual Participant, Stream Audio and the Desktop Capture App, the browser extension and uploads. Then a live Google Meet run under opt-in: LMA posts the request and records nothing, a participant consents, the owner attests, a participant objects and recording stops, and consents again. It closes on the auditable consent record (notice, replies, attestation, evidence screenshots checked by a vision model, timeline), Stream Audio consent records, and configuration profiles kept for different jurisdictions and policies.

**Duration**: ~7 minutes

<!-- VIDEO_URL_CONSENT_AND_COMPLIANCE: replace this comment and the line below with the GitHub asset URL of the consent-and-compliance recording. -->
*Video coming soon.*

**Related Documentation**: [Meeting Consent](./meeting-consent.md), [Privacy and Data Handling Controls](./privacy-and-data-handling.md), [Configuration Guide](./configuration-guide.md#configuration-presets)

### An AI coach for a requirements workshop
A software vendor gathers requirements from a customer in a Google Meet, three people sharing the customer's board-room microphone, with LMA coaching the vendor's analyst as the meeting happens. After the consent notice, FACT CHECK corrects a figure that was just quoted, CLARITY CHECK lists requirements that are still too vague to build from, the assistant suggests the questions not yet asked, AGENDA CHECK tracks the five agenda items and MISSING ACTION OWNER or ETA finds follow-ups with no owner or date. After the meeting, the assistant drafts a software requirements specification from what was said.

**Duration**: ~5 minutes

<!-- VIDEO_URL_REQUIREMENTS_WORKSHOP_COACH: replace this comment and the line below with the GitHub asset URL of the requirements-workshop-coach recording. -->
*Video coming soon.*

**Related Documentation**: [Meeting Assistant](./meeting-assistant.md), [Virtual Participant](./virtual-participant.md)

### Live Meeting Assistant overview
The project's demo of many of LMA's features, also shown at the top of the [README](../README.md).

https://github.com/user-attachments/assets/7642f659-ed9e-4abf-8baf-2f6fb27b08cb

**Related Documentation**: [Quick Start Guide](./quick-start-guide.md)

---

## What's new in LMA 0.4.0

### What's new in LMA 0.4.0: a meeting's life
A tour of the 0.4.0 release following one meeting from its invitation to its shared summary: the launch settings read from a pasted invitation, LMA's own speech engine and the detected language, the consent record, speakers named automatically when the meeting ends, the agenda and labelled chapters on the Transcript tab, summary sections, key frames exported as a PDF, the itemised cost estimate and the Meeting Cost Prices page, retention controls, the Virtual Participant's display name setting, the Download menu, configuration profiles and the troubleshooting agent.

**Duration**: ~4 minutes

<!-- VIDEO_URL_WHATS_NEW_0_4_0_MEETING_LIFE: replace this comment and the line below with the GitHub asset URL of the whats-new-0-4-0-meeting-life recording. -->
*Video coming soon.*

**Related Documentation**: [Meeting Consent](./meeting-consent.md), [Transcript Deletion](./transcript-deletion.md), [Virtual Participant](./virtual-participant.md), [Meeting Cost Estimate](./meeting-cost-estimate.md)

### The Virtual Participant in depth
Scheduling a Virtual Participant for later and editing it before it starts, then directing it from the meeting chat: asking for help, pausing and resuming the transcript, and, with two LMAs in one Google Meet, asking one of them by name to leave while the other stays.

**Duration**: ~2 minutes

<!-- VIDEO_URL_VP_IN_DEPTH: replace this comment and the line below with the GitHub asset URL of the vp-in-depth recording. -->
*Video coming soon.*

**Related Documentation**: [Virtual Participant](./virtual-participant.md)

### The meeting page in depth
A tour of the meeting page: the compact header and tabs, the itemised cost estimate, the agenda and chapter marks on the Transcript tab, playing the recording from any transcript line, search, speaker filters and live translation, editing the summary by hand with protection against regenerating over the edit, AGENDA CHECK, the Download menu and sharing with a colleague.

**Duration**: ~3 minutes

<!-- VIDEO_URL_MEETING_PAGE_IN_DEPTH: replace this comment and the line below with the GitHub asset URL of the meeting-page-in-depth recording. -->
*Video coming soon.*

**Related Documentation**: [Web UI Guide](./web-ui-guide.md#meeting-detail-page)

### Speakers and the shared screen
How LMA tells apart and names several people sharing one microphone, with the words each name came from; naming a voice by hand; showing a room as one person or several people; voice enrollment; and searchable key frames of shared slides with their own summary section.

**Duration**: ~2 minutes

<!-- VIDEO_URL_SPEAKERS_AND_SCREEN_SHARE: replace this comment and the line below with the GitHub asset URL of the speakers-and-screen-share recording. -->
*Video coming soon.*

**Related Documentation**: [Web UI Guide](./web-ui-guide.md#meeting-detail-page), [Voice Enrollment](./voice-enrollment.md), [Meeting Video and Key Frames](./meeting-video.md)

### The assistant, consent and privacy
The MISSING ACTION OWNER or ETA quick action, the Claude Haiku 5.5 default with prompt caching and the scope of in-meeting requests, model inference geography, meeting consent modes and the per-meeting choice, deleting transcripts after the summary, and the consent record kept with each meeting.

**Duration**: ~2 minutes

<!-- VIDEO_URL_ASSISTANT_CONSENT_PRIVACY: replace this comment and the line below with the GitHub asset URL of the assistant-consent-privacy recording. -->
*Video coming soon.*

**Related Documentation**: [Meeting Consent](./meeting-consent.md), [Transcript Deletion](./transcript-deletion.md)

### Configuration and administration
The View / Edit Configuration page: named profiles and Managed presets, Form, YAML or JSON editing, revision history, comparing profiles, saving edits as a new profile, the Transcription Engine section, Meeting Cost Prices, the model lists, and the troubleshooting agent explaining why a Virtual Participant could not join.

**Duration**: ~3 minutes

<!-- VIDEO_URL_CONFIGURATION_AND_ADMIN: replace this comment and the line below with the GitHub asset URL of the configuration-and-admin recording. -->
*Video coming soon.*

**Related Documentation**: [Configuration Guide](./configuration-guide.md#configuration-profiles), [Meeting Cost Estimate](./meeting-cost-estimate.md)

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
