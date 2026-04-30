---
title: "Virtual Participant"
---

# Virtual Participant

## Table of Contents

- [Overview](#overview)
- [When to use Virtual Participant](#when-to-use-virtual-participant)
- [Supported Platforms](#supported-platforms)
- [Joining a Meeting](#joining-a-meeting)
- [Meeting Scheduling](#meeting-scheduling)
- [Meeting Invitation Parsing](#meeting-invitation-parsing)
- [VNC Preview](#vnc-preview)
- [Launch Types](#launch-types)
- [EC2 Instance Types](#ec2-instance-types)
- [Auto-Scaling](#auto-scaling)
- [Chat Introduction Message](#chat-introduction-message)
- [Troubleshooting](#troubleshooting)
- [Developer Testing](#developer-testing)
- [See Also](#see-also)

## Overview

> **Not sure which capture option to use?** See [Meeting Sources](meeting-sources.md) for a side-by-side comparison of the Chrome Extension, Stream Audio, and Virtual Participant.

The Virtual Participant (VP) is a headless Chrome browser running on ECS (Fargate or EC2) that joins meetings as a separate participant via Puppeteer. It captures audio and metadata, sending them to the LMA Kinesis Data Stream for transcription and processing.

## When to use Virtual Participant

- **Attendees on native desktop or mobile apps**: Participants are free to use native meeting apps (Zoom, Teams, etc.) instead of being limited to the browser — the Chrome Extension and Stream Audio require joining from the web client.
- **Independent attendance**: The VP can join before you arrive, stay after you leave, or attend meetings you do not join at all — including scheduling it in advance.
- **Voice Assistant**: The VP is the only capture option that supports the Voice Assistant (wake phrase, push-to-talk, always-on modes).
- **Open VP live view**: Use the Meeting Assistant's "Open VP live view" feature to see the bot's browser view of the meeting in real time.

See [Meeting Sources](meeting-sources.md) for the full comparison.

## Supported Platforms

- Zoom
- Microsoft Teams
- Amazon Chime
- Google Meet
- WebEx

## Joining a Meeting

1. Navigate to **Virtual Participant** in the LMA UI.
2. Enter meeting details: URL, platform, meeting ID/password, and meeting name.
3. Click **Join Now**.
4. The VP starts in approximately 30-60 seconds (EC2) or 1-2 minutes (Fargate).
5. Once joined, the VP posts an introduction message in the meeting chat.
6. View VP status in the UI as it progresses through its lifecycle:
   - **INITIALIZING** -- ECS task is starting and the browser is launching
   - **JOINING** -- VP is navigating to the meeting and attempting to join
   - **JOINED** -- VP has successfully joined the meeting and is capturing audio
   - **ENDED** -- The meeting has ended or the VP has been disconnected

## Meeting Scheduling

Enter a future meeting time to schedule the VP to join a meeting later. The scheduling interface supports:

- Setting a specific date and time for the VP to join
- Starting a scheduled meeting immediately
- Stopping an in-progress scheduled meeting
- Deleting a scheduled meeting before it starts

## Meeting Invitation Parsing

Paste a full meeting invitation into the input field and Bedrock AI automatically parses it to extract and auto-fill:

- Meeting platform
- Meeting URL
- Meeting ID
- Meeting password

This eliminates the need to manually copy individual fields from calendar invitations.

## VNC Preview

The VNC preview provides real-time browser viewing and remote control of the VP's Chrome window at 1920x1120 resolution. This feature is available on the meeting detail page and allows you to:

- See exactly what the VP sees in the meeting
- Interact with the VP's browser session remotely
- Troubleshoot joining issues in real time

## Launch Types

### EC2 (Default, Recommended)

EC2 launch type uses warm instances with cached Docker images. This provides 85-90% faster startup compared to cold Fargate launches, with the VP ready in approximately 30-60 seconds. The estimated cost is approximately $33/month for always-on instances.

EC2 is the recommended launch type for most deployments due to its significantly faster startup time.

### Fargate

Fargate launch type is serverless and uses SOCI (Seekable OCI) for faster container image pulls, providing 40-60% faster startup than standard Fargate. The base cost is approximately $2/month, making it more economical for infrequent use. However, startup time is longer at 1-2 minutes.

## EC2 Instance Types

Choose an instance type based on your workload requirements:

**General Purpose:**
- `t3.medium` (default) -- Suitable for basic transcription without voice assistant
- `t3.large` -- Additional headroom for busier meetings
- `t3.xlarge` -- High-throughput scenarios

**Compute-Optimized (recommended for voice + avatar):**
- `c5.large` -- Good balance for voice assistant workloads
- `c5.xlarge` -- Recommended for voice assistant with avatar
- `c5.2xlarge` -- Heavy voice and avatar processing

**Memory-Optimized (recommended for voice + avatar):**
- `m5.large` -- Voice assistant workloads with higher memory needs
- `m5.xlarge` -- Recommended for voice assistant with avatar and large meeting context

## Auto-Scaling

Configure auto-scaling for EC2 launch type:

- **Minimum instances**: 0 to 10 (set to 0 to scale down completely when idle)
- **Maximum instances**: 1 to 100 (set based on expected concurrent meeting load)

The auto-scaler adjusts the number of warm EC2 instances based on demand, ensuring fast VP startup while controlling costs.

## Chat Introduction Message

The VP posts a customizable introduction message in the meeting chat when it joins. This message informs meeting participants that the VP is present and recording. You can configure the message content to suit your organization's requirements and compliance policies.

## Troubleshooting

- **Scheduling issues**: Check the Step Functions execution logs in the AWS console for errors related to meeting scheduling and trigger timing.
- **Joining or streaming issues**: Check ECS Fargate or EC2 task logs in CloudWatch for errors during the browser launch, meeting join, or audio streaming phases.
- **VP stuck "in progress"**: This may indicate that the ECS task crashed unexpectedly. This issue was identified and fixed in v0.3.0. Ensure you are running the latest version.

## Developer Testing

### Manual Step Function Execution

You can manually invoke the VP Step Function with a JSON payload for testing. The payload supports the following methods:

- **POST** -- Start a new VP session with specified meeting details
- **GET** -- Retrieve the status of an existing VP session
- **DELETE** -- Stop and clean up a running VP session

### Local Docker Testing

For local development and debugging, run the VP Docker container against a deployed LMA stack using the `make vp-start` target:

```bash
make vp-start STACK_NAME=<your-stack> PLATFORM=WEBEX MEETING_ID=<id>
```

This invokes `lma-virtual-participant-stack/backend/local-test.sh`, which reads configuration from CloudFormation, generates a `.env.local`, builds the Docker image, and runs the container locally with VNC exposed on ports 5900 / 5901.

For the recommended EC2 + VSCode Remote-SSH + VNC workflow — including how to manage secrets (`--reuse-env`), enable dev-mode auto-reload, and fix stale VSCode port forwarding — see [Virtual Participant Local Development](virtual-participant-local-dev.md).

## See Also

- [Virtual Participant Local Development](virtual-participant-local-dev.md) -- EC2 + VSCode + VNC workflow for running the VP locally
- [Stream Audio](stream-audio.md) -- Browser-based audio capture alternative
- [Voice Assistant](voice-assistant.md) -- Add a voice assistant to the Virtual Participant
- [Simli Avatar Setup](simli-avatar-setup.md) -- Configure a visual avatar for the VP

