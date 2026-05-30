---
title: "Virtual Participant"
---

# Virtual Participant

## Table of Contents

- [Overview](#overview)
- [When to use Virtual Participant](#when-to-use-virtual-participant)
- [Supported Platforms](#supported-platforms)
- [Joining a Meeting](#joining-a-meeting)
- [Status Lifecycle](#status-lifecycle)
- [Manual Action Required (CAPTCHA, 2FA, SSO)](#manual-action-required-captcha-2fa-sso)
- [AI-driven self-healing](#ai-driven-self-healing)
- [Zoom Sign-in](#zoom-sign-in)
- [Meeting Scheduling](#meeting-scheduling)
- [Meeting Invitation Parsing](#meeting-invitation-parsing)
- [VNC Preview](#vnc-preview)
- [Launch Types](#launch-types)
- [EC2 Instance Types](#ec2-instance-types)
- [Auto-Scaling](#auto-scaling)
- [Chat Introduction Message](#chat-introduction-message)
- [In-meeting Chat Commands](#in-meeting-chat-commands)
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
6. View VP status in the UI as it progresses through its lifecycle (see [Status Lifecycle](#status-lifecycle)).

## Status Lifecycle

The VP reports a granular status as it boots, joins, and runs. The UI uses these to give a precise picture of where in the startup sequence the VP is:

| Status | What's happening |
| --- | --- |
| `INITIALIZING` | Step Functions has submitted the ECS RunTask request; container is being scheduled |
| `WAITING_FOR_CAPACITY` | All current EC2 hosts are full; the capacity-provider auto-scaler is launching a new t3.large host (typically 60-90 seconds). See [Auto-Scaling](#auto-scaling) |
| `BOOTING` | Container started; pulling Chrome image, starting Xvfb / VNC / PulseAudio |
| `REGISTERING_NETWORK` | Registering the task with the live-view ALB (typically 30-60 seconds) |
| `HYDRATING_PROFILE` | Restoring the per-user Chromium profile (cookies, "trusted device" markers) from S3 |
| `LAUNCHING_BROWSER` | Launching CloakBrowser's patched Chromium via `cloakbrowser/playwright` |
| `VNC_READY` | Browser is up; live-view viewer can connect |
| `CONNECTING` | Initializing audio/video pipelines (Nova Sonic, Simli avatar, agent mic) |
| `JOINING` | Navigating to the meeting URL; signing in to Zoom if credentials are stored |
| `MANUAL_ACTION_REQUIRED` | A CAPTCHA, 2FA, SSO, consent dialog, or other Zoom verification step needs human input — see [Manual Action Required](#manual-action-required-captcha-2fa-sso) |
| `ACTIVE` / `JOINED` | VP is in the meeting and capturing audio |
| `COMPLETED` / `ENDED` | Meeting ended cleanly (host removed the VP, all attendees left, or the VP got "lonely") |
| `FAILED` | The VP could not join. The DDB record carries an `errorMessage` describing what went wrong (e.g. *"Meeting join failed: …"*, *"Zoom login failed: invalid credentials"*, *"ECS RunTask soft-failure: agent not connected"*) |

When ECS reports a "soft failure" on RunTask (returns HTTP 200 with a non-empty `failures` array — typically the container instance agent is briefly disconnected), the state machine catches it explicitly and writes `FAILED` plus the original failure reason to the VP record, instead of leaving the VP stuck in `INITIALIZING` indefinitely.

## Manual Action Required (CAPTCHA, 2FA, SSO)

When the VP can't proceed without a human (Zoom 2FA passcode, CAPTCHA, SSO redirect, an unknown consent dialog the auto-dismiss handler can't classify), it sets status to `MANUAL_ACTION_REQUIRED` and surfaces:

- A persistent **Flashbar alert** at the top of the LMA UI with the action type and a link to the meeting detail page.
- An **action banner** on the Virtual Participant detail page above the live VNC viewer.
- A **browser notification + audio chime** if you've granted notification permission, so you can be tabbed-away or in another window.

Open the live VNC viewer for the affected VP, complete the challenge (type the passcode, solve the CAPTCHA, sign in via SSO, click the consent button — whatever the banner says), and the VP picks up automatically. The default timeout is 3 minutes; if no human response arrives, the VP fails the meeting cleanly.

The banner is dismissible — dismissed alerts are remembered in `localStorage` (per VP id) so the banner doesn't reappear on every page refresh after you've dealt with it.

## AI-driven self-healing

Every platform handler (Zoom, Teams, Webex, Chime) wraps its hard-coded CSS selectors in an AI fallback resolver. When a primary selector misses (because the meeting platform shipped a UI change), the resolver asks Claude (Bedrock, vision-capable) to find the right element by reading a compact DOM summary plus a screenshot of the current page. Successful resolutions are cached in a shared `DomSelectorCache` DynamoDB table (30-day TTL on `lastUsedAt`) so the cost is paid only once per platform UI change.

The same resolver classifies unknown popup dialogs:
- `CONSENT` / `RECORDING_NOTICE` → auto-clicked by the VP.
- `CAPTCHA` / `LOGIN_REQUIRED` / `SSO_REDIRECT` / `BLOCKED` → escalated to `MANUAL_ACTION_REQUIRED` (see above) so a human can solve it via the live VNC viewer.

Disable the AI fallback by setting `BedrockDomResolverModelId` to an empty string at deploy time — the VP reverts to hardcoded-selector-only behavior.

For full details on the AI resolver and its interaction with the Zoom sign-in flow, see [Zoom Sign-in & Join Reliability](zoom-credentials-and-join-reliability.md).

## Zoom Sign-in

LMA can sign the VP in to Zoom using **per-user stored credentials** before navigating to the meeting URL. A signed-in session joins far more reliably ("We detected you may be a bot…" guest blocks become rare) and allows the VP to join meetings that disallow guests.

- Each user adds their own credentials via the **Zoom account** card in the Create Virtual Participant modal. Credentials live in AWS Secrets Manager keyed by Cognito sub, and the plaintext password is never returned to the React UI.
- The VP container reads the secret at runtime; it is never put on the task definition or in the Step Functions execution input.
- The sign-in flow is **AI-driven** end-to-end. After submitting the username, Claude inspects each subsequent page (password entry, OTP, passkey-binding upsell, phone-binding upsell, dashboard, etc.) and decides whether to fill, skip, click-through, wait, or escalate — so it tolerates Zoom's frequent post-login interstitials without code changes.
- **2FA / CAPTCHA / SSO challenges** that need human input fall through to `MANUAL_ACTION_REQUIRED` (see above).
- **Per-user persistent Chromium profile** in S3 means cookies and "trusted device" markers survive across meetings — after the first manual sign-in, subsequent VPs reuse the session and sign in cleanly without re-prompting.

For setup, caveats, and the full join-reliability stack, see [Zoom Sign-in & Join Reliability](zoom-credentials-and-join-reliability.md).

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

Each VP container is capped at 2500 MB (~1.85× observed peak memory of ~1348 MB during concurrent Chrome + Simli + Nova Sonic startup). Pick an instance whose host memory accommodates your expected concurrent VPs per host plus ~600 MB for the OS / ECS agent:

**General Purpose:**
- `t3.medium` (default) -- 3867 MB host → 1 concurrent VP. The capacity-provider auto-scaler launches additional hosts when concurrent demand exceeds capacity, so users running one meeting at a time pay the baseline (~$30/month per host) and only scale up while multiple VPs are active.
- `t3.large` -- 7857 MB host → 3 concurrent VPs. Pick this if you regularly run 2-3 concurrent meetings and want fewer scale-outs.
- `t3.xlarge` -- 15.7 GB host → 6 concurrent VPs.

**Compute-Optimized (recommended for voice + avatar):**
- `c5.large` -- Voice assistant workloads, 1 concurrent VP
- `c5.xlarge` -- Voice assistant with avatar, 3 concurrent VPs (no burstable throttling)
- `c5.2xlarge` -- Heavy voice and avatar processing, 6 concurrent VPs

**Memory-Optimized (recommended for voice + avatar with large meeting context):**
- `m5.large` -- Voice assistant workloads with higher memory needs, 1 concurrent VP
- `m5.xlarge` -- Recommended for voice assistant with avatar and large meeting context, 3-4 concurrent VPs

`t3` instances are burstable and may cause audio glitches under sustained load — prefer `c5.*` or `m5.*` for the voice-assistant + avatar combination.

## Auto-Scaling

The VP cluster uses an **ECS capacity provider with managed scaling**:

- **`VPMinInstances`** (default `1`) -- Minimum warm hosts always running. Set to `0` to fully scale down when idle and pay only when a VP is requested (cold-start adds ~60-90s to the first VP).
- **`VPMaxInstances`** (default `10`) -- Hard ceiling on concurrent hosts. With the `t3.large` default each host fits 3 VPs, so 10 hosts × 3 VPs = 30 concurrent meetings.

When concurrent demand exceeds the current cluster's capacity, ECS automatically launches new hosts (`TargetCapacity=100`, step size 1-2, instance warmup 90s). The launching VP shows status `WAITING_FOR_CAPACITY` while the auto-scaler provisions a new host — typically 60-90 seconds — then transitions to `BOOTING`. Scale-down to `VPMinInstances` happens automatically when reservation drops.

`ManagedTerminationProtection` is enabled, so a host running an active VP will never be killed by scale-in until the VP exits.

## Chat Introduction Message

The VP posts a customizable introduction message in the meeting chat when it joins. This message informs meeting participants that the VP is present and recording. You can configure the message content to suit your organization's requirements and compliance policies.

## In-meeting Chat Commands

Anyone in the meeting can control the VP by typing one of these commands in the meeting chat. Commands work across Zoom, Microsoft Teams, Webex, and Chime.

| Command (typed in chat) | What it does |
| --- | --- |
| `LMA end`, `LMA leave`, `LMA stop`, `LMA quit`, `LMA exit` | The VP says goodbye and leaves the meeting. |
| `Goodbye LMA`, `bye LMA` | Same as above. |
| `PAUSE` | Stops transcription and recording without leaving the meeting. |
| `START` | Resumes a paused session. |

Notes:

- Dismissal commands always require the literal token `LMA` somewhere in the message — bare words like `END` or `LEAVE` are ignored so that prose like *"the meeting will end at 3pm"* or *"I'll leave at 3"* never trips a false dismissal. The `LMA` and verb tokens are matched case-insensitively and word-bounded (so `endless` or `ENDPOINT` don't match either).
- When the chat platform exposes the sender name (Zoom, Chime, Webex), the goodbye message acknowledges who asked the VP to leave: *"Thanks Alice — I'll head out now."*
- The default introduction message advertises the `LMA leave` / `LMA end` commands so participants always have a way to dismiss the bot. Customize via the `IntroMessage` CloudFormation parameter or the `INTRO_MESSAGE` env var.
- `PAUSE` and `START` are still keyword-matched on the literal token (case-sensitive) for backwards compatibility.

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

