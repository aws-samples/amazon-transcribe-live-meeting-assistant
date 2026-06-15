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

The SDK method renders the real Zoom client into the same browser tab, so the **live (VNC) view**, **meeting audio capture/transcription**, **voice assistant**, and **Simli avatar** all work exactly as they do with the web-client method — only the join mechanism differs. The two methods are at feature parity: intro message, chat commands (`LMA leave`/`stop`/`start`), speaker labels, transcript capture, avatar + camera watchdog, consent/AI-summary popup dismissal, and the alone/host-ended/removed exit reasons are all present. The difference is that the SDK method receives these as deterministic SDK events instead of scraping the DOM, and it has no Zoom sign-in step (it authenticates with a signed signature rather than a Zoom user login).

> The Meeting SDK is a feature of a Zoom **General App**, distinct from the **Video SDK** (a separate product, account, and billing model that builds custom video UIs and cannot join scheduled Zoom meetings). The VP uses the Meeting SDK.

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
2. **Develop → Build App → General App** (the Meeting SDK is a feature of a General App, not a separate app type). Do **not** create a Video SDK app — that is a different product with separate credentials and billing, and cannot join scheduled Zoom meetings.
3. Under **Features → Embed**, toggle **Meeting SDK** ON.
4. Still under the Meeting SDK feature, toggle **"Are you developing a programmatic join use case?"** ON. This is required for the VP — without it, joins fail with `errorCode 3712 Invalid signature` even when the Client ID/Secret are correct.
5. To unlock **Local Test / Activate**, the Marketplace form requires the **Basic Information** fields, an **OAuth Redirect URL**, an **OAuth Allow List** entry, and at least one **Scope** to be filled in. The VP's signature-based join does **not** use OAuth at runtime, so these can be placeholders — e.g. set the redirect URL and allow-list entry both to `https://localhost:4000`, and add a minimal scope such as `meeting:read:meeting`. (When you click "Add app", Zoom redirects to that URL with a `?code=...`; the resulting connection error is harmless — the app is now authorized on your account.)
6. Copy the **Client ID** and **Client Secret** from **App Credentials** (use the **Development** column for testing). These are the values for `ZoomMeetingSdkClientId` / `ZoomMeetingSdkClientSecret`. Do not confuse the Client Secret with the webhook **Secret Token** / **Verification Token**.
7. The app does **not** need to be published to the Marketplace for internal use — an unpublished app works for users on the app owner's own Zoom account. (Your Zoom admin may need to approve the app at the account level.)
8. The account hosting the test meeting must allow web-client joins — enable **Settings → Meeting → "Show a 'Join from your browser' link"**.

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
>
> Pass the **bare numeric meeting ID** (not a join URL) and quote the passcode — e.g. `./local-test.sh --dev LMA-dev-stack ZOOM 98765432101 'pAsScO.de1'`. A pasted URL or unquoted passcode gets mangled into the meeting-ID slot and fails with `code=3707`. Note that `--reuse-env` only rewrites the meeting fields, and nodemon only reloads on `src/**` changes — to pick up an edited `.env.local`, restart the container (`docker restart lma-vp-local-test`).

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `Zoom SDK method selected but ... CLIENT_ID / ... CLIENT_SECRET are not set` | `MEETING_ZOOM_METHOD=sdk` was forced without credentials. Set both, or use `auto`/`dom`. |
| `Zoom SDK join failed (code=3712): Invalid signature` | Despite the name, with a correctly-signed JWT this almost always means the **app config is incomplete**: the **Meeting SDK** feature and **"programmatic join use case"** toggle must both be ON (see [Getting Meeting SDK credentials](#getting-meeting-sdk-credentials)). Zoom config changes can take a few minutes to propagate. Also re-confirm you copied the **Client Secret** (not a webhook token) and the matching **Client ID**. |
| `Zoom SDK join failed (code=3707): The meeting number is not found` | Bad meeting number — usually a pasted join *URL* or mis-quoted CLI arg. Pass the bare numeric meeting ID and quote the passcode. |
| Joins, then `no rwc available` / WebSocket close `513`, never enters | The meeting's media node refused the connection. Two known causes: (1) the meeting is on a **different account** than the SDK app (an unpublished app can only join meetings on its own account); (2) the join was triggered by a non-trusted click. The VP clicks the preview **Join** via Playwright (a trusted gesture) for this reason — an in-page `el.click()` is rejected by the SDK's media layer. |
| Join times out (`never-joined`, `sdk-not-admitted`) | Held in the waiting room past the timeout, or the meeting requires registration. Admit the participant, or check the passcode. |
| `Failed to create WebGPU Context Provider` / `WebGL is not supported` / `init tf fail` in the browser console | **Harmless noise.** The container runs with `--disable-gpu`; the SDK's video subsystem logs these but still joins and renders fine. Do not enable a GPU/SwiftShader to "fix" it — it has a large CPU cost for no benefit. |
| `crossOriginIsolated=false` warning | Gallery view degraded to single-stream; the join still works. |
| VP joins but is silent / no avatar / no transcription | The SDK lands muted, video-off, and sometimes with audio not connected. The handler drives the in-meeting toolbar (join audio, unmute, start video) after join; confirm `media setup complete (audio=true mic=true video=on)` in the log. The avatar (via the patched `getUserMedia`) and the bot's voice (via the `agent_mic` default source) both reach the meeting once video/mic are on. |
| No consent / AI-summary / interpretation popup gets dismissed | The handler auto-dismisses these (`[zoom-sdk] dismissing popup: ...`). If a specific dialog isn't caught, capture its modal container class + button text so the matcher can be extended. |

## Related

- [Zoom Sign-in & Join Reliability](zoom-credentials-and-join-reliability.md) — the web-client (`dom`) method and per-user Zoom login credentials.
- [Virtual Participant](virtual-participant.md) — VP overview and lifecycle.
- [Simli Avatar Setup](simli-avatar-setup.md) — avatar configuration shared by both join methods.
