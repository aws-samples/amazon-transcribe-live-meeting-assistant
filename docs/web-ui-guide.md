---
title: "Web UI Guide"
---

# Web UI Guide

## Table of Contents

- [Overview](#overview)
- [Navigation](#navigation)
- [Meeting List / Dashboard](#meeting-list--dashboard)
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
- **Stream Audio** — Stream audio directly to LMA for transcription

### Meetings
- **Meeting List** — View and manage all accessible meetings
- **Meetings Query Tool** — Advanced search and filtering across meetings

### Configuration (admin only)
- **MCP Servers** — Install and manage MCP server integrations
- **Nova Sonic** — Configure the Nova Sonic voice assistant
- **Transcript Summary** — Manage summary prompt templates

### Deployment Info
- Stack name, build date, and version information

## Meeting List / Dashboard

The meeting list provides a searchable dashboard of all meetings you have access to.

- **Columns**: Meeting Topic, Date/Time, Duration, Status, Owner, Shared With
- **Status indicators**: In Progress, Ended
- **Multi-select**: Check multiple meetings for batch operations (share, delete)
- **Configurable time period**: Adjust the time range for loading meetings to manage performance with large meeting histories

Use the search bar to filter meetings by topic or other attributes.

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

### MCP Servers

**Route**: `/#/configuration/mcp-servers`

Install and manage MCP (Model Context Protocol) servers to extend the meeting assistant's capabilities with external tools and services.

See [MCP Servers](mcp-servers.md) for full documentation.

### Nova Sonic

**Route**: `/#/configuration/nova-sonic`

Configure the Nova Sonic voice assistant settings:
- Voice assistant prompt customization
- Voice ID selection
- Endpointing sensitivity adjustment
- Group meeting mode toggle

See [Nova Sonic 2 Setup](nova-sonic-setup.md) for full documentation.

### Transcript Summary

**Route**: `/#/configuration/transcript-summary`

Manage the prompt templates used for generating meeting summaries:
- View default prompts provided by LMA
- Create custom prompt templates
- Edit and delete existing custom templates

### Chat Buttons

Customize the chat shortcut buttons that appear in the meeting chat panel:
- Open the **Edit Chat Buttons** modal from the meeting detail page
- Add new shortcut buttons with custom labels and prompts
- Edit existing button labels and prompts
- Delete buttons you no longer need

## Deployment Info

The deployment info section in the sidebar displays:

- **Stack name** — The CloudFormation stack name for your LMA deployment
- **Build timestamp** — When the current version was built
- **Version** — The LMA version number, sourced from CloudFormation outputs
