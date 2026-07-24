---
title: "Teams ACS SDK Join"
sidebar_label: Teams ACS SDK
---

# Microsoft Teams ACS SDK Join

## Overview

The Virtual Participant (VP) can join Microsoft Teams meetings two ways:

| Method | How it joins | When it's used |
|--------|--------------|----------------|
| **Web-client automation** (`dom`, default) | Drives the anonymous Teams web client (`teams.microsoft.com`) in CloakBrowser, filling the prejoin form and scraping the DOM. | Used by default. No Azure resources required. |
| **ACS SDK** (`sdk`) | Joins through [Azure Communication Services Teams interoperability](https://learn.microsoft.com/azure/communication-services/concepts/teams-interop) — the officially supported way for an external application to participate in a Teams meeting. The VP embeds the ACS calling + chat SDKs (via the [ACS UI Library](https://azure.github.io/communication-ui-library/) `CallWithChatComposite`) in a local page and joins programmatically with meeting ID + passcode. | Used automatically when an ACS connection string is configured. |

The SDK method renders a full meeting UI (video gallery, active-speaker highlighting, screen-share view, chat panel) into the same browser tab, so the **live (VNC) view**, **meeting audio capture/transcription**, **voice assistant**, and **Simli avatar** all work exactly as they do with the web-client method — only the join mechanism differs. The two methods are at feature parity: intro message, chat commands (`LMA leave`/`stop`/`start`), speaker labels, transcript capture, avatar camera + watchdog, and the alone/host-ended/removed exit reasons are all present. The difference is that the SDK method receives these as deterministic SDK events and API calls instead of scraping the Teams DOM — no fragile selectors, no CAPTCHA risk, no sign-in.

> **Why the SDK method exists.** Teams' web client is not built for automated participants; DOM automation is fragile across Teams UI updates and can trigger CAPTCHA challenges. ACS Teams interoperability is Microsoft's sanctioned path for custom applications (including bots) to join Teams meetings as external participants. It requires no Teams-side app registration, no Microsoft 365 tenant changes, and no approval process — only an Azure Communication Services resource on your own Azure subscription.

## How it's enabled

Enablement follows the same "set the key and it turns on" pattern as the Zoom Meeting SDK:

- When `AcsConnectionString` is set, Teams meetings join via the ACS SDK.
- When it is blank, Teams meetings use the default web-client automation.

An optional task-definition env var `MEETING_TEAMS_METHOD` can force a method regardless of credentials:

| `MEETING_TEAMS_METHOD` | Behavior |
|------------------------|----------|
| `auto` (default) | SDK when the connection string is present, otherwise `dom`. |
| `sdk` | Force the SDK (errors at join if the connection string is missing). |
| `dom` | Force web-client automation even if the connection string is present. |

This setting only affects Teams; Zoom, Chime, and Webex are unaffected.

## CloudFormation parameters

Set this on the main stack (group **Meeting Assist Voice Assistant**):

| Parameter | Description |
|-----------|-------------|
| `AcsConnectionString` | Azure Communication Services **connection string** (`endpoint=https://...;accesskey=...`, `NoEcho`). |

Optional; defaults to empty. It is passed to the VP task definition as the env var `ACS_CONNECTION_STRING`. The access key is never logged and is only used inside the container to mint a short-lived (24-hour) ACS user token for each meeting.

## Getting an ACS connection string

You need an Azure subscription (any pay-as-you-go or free-trial account works — this is entirely on the Azure side; nothing is configured in Microsoft Teams or Microsoft 365):

1. Sign in to the [Azure portal](https://portal.azure.com/).
2. **Create a resource → search "Communication Services" → Create**. Pick any resource group, a resource name (e.g. `lma-virtual-participant`), and a data location (e.g. United States). Full walkthrough: [Create a Communication Services resource](https://learn.microsoft.com/azure/communication-services/quickstarts/create-communication-resource).
3. Once deployed, open the resource and go to **Settings → Keys**.
4. Copy the **Connection string** for the **Primary key** (the value that starts with `endpoint=https://...;accesskey=...`). This is the value for `AcsConnectionString`. Do not copy the bare endpoint or the bare key alone — the full connection string is required.
5. There is nothing else to configure: no app registration, no OAuth, no scopes, no publishing. The VP creates a fresh anonymous ACS identity and access token per meeting from this connection string.

### Cost

ACS calling is billed per participant-minute on the Azure resource (currently ~$0.004/participant/min — the VP counts as one participant, so a 60-minute meeting costs roughly $0.24). Teams-side attendees are covered by their own Teams licenses and are not billed to you. Chat messages sent by the VP are ~$0.0008/message. See [ACS pricing](https://learn.microsoft.com/azure/communication-services/concepts/pricing).

## Behavior and limitations

- The VP joins as an **anonymous external user**. Teams clients show its display name with an **"(External)"** suffix, e.g. `LMA (Kirk) (External)`.
- The meeting's tenant must allow anonymous joins (**"Let anonymous people join a meeting"**, on by default in Teams admin center). If the tenant blocks anonymous users, the join fails cleanly as `never-joined`.
- The VP is subject to the normal Teams **lobby** rules — with default meeting options an external participant waits in the lobby until admitted, the same as the web-client method. Set "Who can bypass the lobby?" to **Everyone** to skip it.
- Not supported by ACS Teams interop: personal **teams.live.com** meetings (Teams for Home), webinars/town halls/live events, end-to-end-encrypted meetings, breakout rooms, and GCC (government cloud) tenants.
- Meeting chat works both ways (intro/exit messages and `LMA leave` commands), but only from admission onward — no chat history from before the VP joined.

## Local testing

`local-test.sh` reads the connection string from your shell environment. Export it, then run with the `TEAMS` platform:

```bash
export ACS_CONNECTION_STRING="endpoint=https://your-resource.unitedstates.communication.azure.com/;accesskey=your-key"  # pragma: allowlist secret

./local-test.sh --dev LMA-dev-stack TEAMS 1234567890123 mypasscode
```

When set, the generated `.env.local` will contain it and `MEETING_TEAMS_METHOD=auto`, so the VP joins via the SDK. Watch the container log for `[teams-sdk] joined meeting` and confirm the meeting renders in the noVNC viewer (`http://localhost:5901/vnc.html`).

> Pass the **numeric meeting ID** and passcode from the Teams invite (the `Meeting ID:`/`Passcode:` lines, or the `.../meet/<digits>?p=<passcode>` join URL fields) — the same values the web-client method uses.
>
> If you previously generated `.env.local` and are re-running with `--reuse-env`, regenerate it once (run without `--reuse-env`) so the new variable is written.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `Teams SDK method selected but ACS_CONNECTION_STRING is not set` | `MEETING_TEAMS_METHOD=sdk` was forced without the connection string. Set it, or use `auto`/`dom`. |
| `Teams SDK setup failed` right after page load | Malformed connection string (must contain both `endpoint=` and `accesskey=`), or the ACS resource was deleted/regenerated its keys. Re-copy the full connection string from **Keys** in the Azure portal. |
| `access denied (tenant may block anonymous joins, ...)` | The meeting's tenant disables anonymous join, or the meeting ID/passcode is wrong. Verify the invite values; ask the tenant admin about the anonymous-join meeting policy. |
| Join times out (`never-joined`, `acs-not-admitted`) | Held in the lobby past the timeout. Admit the participant, or relax the meeting's lobby setting. |
| Joins but chat messages never appear | Chat requires admission to the meeting (not lobby) and doesn't work for channel meetings. The intro message is queued and sent automatically once chat initializes — check the log for `chat initialized`. |
| VP joins but is silent / no avatar | The handler unmutes (when the voice assistant is enabled) and starts the avatar camera after join; check the log for `microphone unmuted` and `camera started via raw media stream`. The avatar reaches the meeting through the patched `getUserMedia`, the bot's voice through the `agent_mic` default source. |

## Related

- [Virtual Participant](virtual-participant.md) — VP overview and lifecycle.
- [Zoom Meeting SDK Join](zoom-meeting-sdk.md) — the equivalent SDK join path for Zoom.
- [Simli Avatar Setup](simli-avatar-setup.md) — avatar configuration shared by both join methods.
