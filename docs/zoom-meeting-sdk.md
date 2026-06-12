---
title: "Zoom Meeting SDK Join"
sidebar_label: Zoom Meeting SDK
---

# Zoom Meeting SDK Join

## Overview

The Virtual Participant (VP) can join Zoom meetings two ways:

| Method | How it joins | When it's used |
|--------|--------------|----------------|
| **Web-client automation** (`dom`, default) | Drives the public Zoom web client (`zoom.us/wc/...`) in CloakBrowser, filling the prejoin form and scraping the DOM. | Used by default. No Zoom developer app required. |
| **Meeting Web SDK** (`sdk`) | Embeds the [Zoom Meeting Web SDK](https://developers.zoom.us/docs/meeting-sdk/web/) (Client View) in a local page and joins programmatically with a signed signature. | Used automatically when Meeting SDK credentials are configured. |

The SDK method renders the real Zoom client into the same browser tab, so the **live (VNC) view**, **meeting audio capture/transcription**, **voice assistant**, and **Simli avatar** all work exactly as they do with the web-client method — only the join mechanism differs.

> **Why the SDK method exists.** Zoom's Terms of Service do not permit automated/bot joins through the Web, Desktop, or Mobile *client*, and Zoom's bot detection actively blocks automated web-client joins (no allowlist is available for that path). The supported path for an automated participant is the **Meeting SDK**. See [Approval & usage policy](#approval--usage-policy).

## How it's enabled

Enablement follows the same "set the keys and it turns on" pattern as the Simli avatar:

- When **both** `ZoomMeetingSdkClientId` and `ZoomMeetingSdkClientSecret` are set, Zoom meetings join via the SDK.
- When either is blank, Zoom meetings use the default web-client automation.

An optional task-definition env var `MEETING_ZOOM_METHOD` can force a method regardless of credentials:

| `MEETING_ZOOM_METHOD` | Behavior |
|-----------------------|----------|
| `auto` (default) | SDK when both credentials are present, otherwise `dom`. |
| `sdk` | Force the SDK (errors at join if credentials are missing). |
| `dom` | Force web-client automation even if credentials are present. |

This setting only affects Zoom; Teams, Chime, and Webex are unaffected.

## CloudFormation parameters

Set these on the main stack (group **Meeting Assist Voice Assistant**):

| Parameter | Description |
|-----------|-------------|
| `ZoomMeetingSdkClientId` | Zoom Meeting SDK app **Client ID**. |
| `ZoomMeetingSdkClientSecret` | Zoom Meeting SDK app **Client Secret** (`NoEcho`). Used to sign the SDK join signature. |

Both are optional and default to empty. They are passed to the VP task definition as the env vars `ZOOM_MEETING_SDK_CLIENT_ID` and `ZOOM_MEETING_SDK_CLIENT_SECRET`. The secret is never logged and is only used inside the container to sign a short-lived (2-hour) JWT.

## Getting Meeting SDK credentials

1. Sign in to the [Zoom App Marketplace](https://marketplace.zoom.us/) with the account that will own the app.
2. **Develop → Build App → Meeting SDK**.
3. Copy the **Client ID** and **Client Secret** from the app's credentials page.
4. The app does **not** need to be published to the Marketplace for internal use — an unpublished app works for users on the app owner's own Zoom account. (Your Zoom admin may need to approve the app at the account level.)

## Approval & usage policy

- The Meeting SDK is reserved for human-style participant use cases; using it for an AI notetaker requires Zoom to **approve / allowlist your SDK app** for that use case. This is per-app (per Client ID) and is handled through Zoom Developer Support.
- Without that approval, an unpublished app can still join meetings **on the app owner's own account** — sufficient for development and internal testing.
- To join meetings hosted by **other** organizations (or other accounts), the SDK app must be approved by Zoom for the use case. Each deploying organization needs its own SDK app and its own approval.
- **RTMS is not a substitute.** Zoom steers notetakers to Real-Time Media Streams (RTMS), but RTMS is egress-only — it cannot send the VP's avatar video, voice, or chat messages back into the meeting, which the VP requires.

## Cross-origin isolation

The Meeting SDK's gallery view uses `SharedArrayBuffer`, which the browser only enables when the page is cross-origin isolated. The local server that hosts the embed page sets `Cross-Origin-Opener-Policy: same-origin` and `Cross-Origin-Embedder-Policy: require-corp` (plus `Cross-Origin-Resource-Policy` on assets) on every response. If isolation can't be established in a given environment, the SDK falls back to a single-stream view; this is logged as a warning and does not fail the join.

## Local testing

`local-test.sh` reads the SDK credentials from your shell environment. Export both, then run with the `ZOOM` platform:

```bash
export ZOOM_MEETING_SDK_CLIENT_ID="your-client-id"        # pragma: allowlist secret
export ZOOM_MEETING_SDK_CLIENT_SECRET="your-client-secret" # pragma: allowlist secret

./local-test.sh --dev LMA-dev-stack ZOOM 98765432101 mypasscode
```

When both are set, the generated `.env.local` will contain them and `MEETING_ZOOM_METHOD=auto`, so the VP joins via the SDK. Watch the container log for `[zoom-sdk] joined meeting` and confirm the meeting renders in the noVNC viewer (`http://localhost:5901/vnc.html`).

> If you previously generated `.env.local` and are re-running with `--reuse-env`, regenerate it once (run without `--reuse-env`) so the new SDK variables are written.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `Zoom SDK method selected but ... CLIENT_ID / ... CLIENT_SECRET are not set` | `MEETING_ZOOM_METHOD=sdk` was forced without credentials. Set both, or use `auto`/`dom`. |
| `Zoom SDK join failed (code=...)` with a signature/`invalid` reason | Wrong Client ID/Secret, or the app isn't authorized to join this meeting. Verify credentials and that the meeting is on the app owner's account (or that the app is approved). |
| Join times out (`never-joined`, `sdk-not-admitted`) | Held in the waiting room past the timeout, or the meeting requires registration. Admit the participant, or check the passcode. |
| `crossOriginIsolated=false` warning | Gallery view degraded to single-stream; the join still works. |
| Avatar not visible to other participants | Validate that the SDK's media path uses the patched `getUserMedia` — see [Simli Avatar Setup](simli-avatar-setup.md). The bot's voice (via `agent_mic`) is independent of this. |

## Related

- [Zoom Sign-in & Join Reliability](zoom-credentials-and-join-reliability.md) — the web-client (`dom`) method and per-user Zoom login credentials.
- [Virtual Participant](virtual-participant.md) — VP overview and lifecycle.
- [Simli Avatar Setup](simli-avatar-setup.md) — avatar configuration shared by both join methods.
