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

## Supported meeting types

ACS Teams interop only joins meetings **organized by a Microsoft 365 work or school account**. The invite URL's domain tells you which kind of meeting you have:

| Invite URL | Meeting type | ACS SDK join |
|------------|--------------|--------------|
| `teams.microsoft.com/meet/<id>?p=...` or `teams.microsoft.com/l/meetup-join/...` | Microsoft 365 work/school tenant | ✅ Supported, if the tenant allows anonymous join |
| `teams.live.com/meet/<id>?p=...` | Personal / Teams Free (consumer Microsoft account) | ❌ Never supported — no tenant setting can enable it (rejected with subCode 5222) |

Also not supported by ACS Teams interop: webinars/town halls/live events, end-to-end-encrypted meetings, breakout rooms, channel-meeting chat, and GCC (government cloud) tenants.

Two tenant-side requirements for the supported case:

1. **Anonymous join must be allowed.** Teams admin center → **Meetings → Meeting settings → "Anonymous users can join a meeting"** (on by default for new tenants; commonly disabled in enterprises — rejected with subCode 5723 when off). Admins can also scope this per meeting policy instead of org-wide.
2. **Lobby rules apply as normal** — with default meeting options an external participant waits in the lobby until admitted, the same as the web-client method. Set "Who can bypass the lobby?" to **Everyone** to skip it.

No paid add-ons are needed on the Microsoft side — any M365 business/enterprise subscription that includes Teams works (e.g. Microsoft 365 Business Basic). A Microsoft 365 **Personal/Family** subscription does **not** work: it uses consumer accounts, whose meetings are the unsupported `teams.live.com` kind.

## Behavior and limitations

- The VP joins as an **anonymous external user**. Teams clients show its display name with an **"(External)"** suffix, e.g. `LMA (Kirk) (External)`.
- Meeting chat works both ways (intro/exit messages and `LMA leave` commands), but only from admission onward — no chat history from before the VP joined.
- Meetings can be joined by **numeric meeting ID + passcode** or by the invite **URL slug** (`/meet/ht45...` style links) — the VP reconstructs the meeting link automatically when the ID isn't numeric.

## Local testing

`local-test.sh` reads the connection string from your shell environment. Export it, then run with the `TEAMS` platform:

```bash
export ACS_CONNECTION_STRING="endpoint=https://your-resource.unitedstates.communication.azure.com/;accesskey=your-key"  # pragma: allowlist secret

./local-test.sh --dev LMA-dev-stack TEAMS 1234567890123 mypasscode
```

When set, the generated `.env.local` will contain it and `MEETING_TEAMS_METHOD=auto`, so the VP joins via the SDK. Watch the container log for `[teams-sdk] joined meeting` and confirm the meeting renders in the noVNC viewer (`http://localhost:5901/vnc.html`).

> Pass the **numeric meeting ID** (the `Meeting ID:` line in the invite, spaces optional) or the **URL slug** from a `/meet/<slug>` link, plus the passcode, as two separate arguments. Don't paste the full URL — the `?p=...` part gets mangled into the meeting-ID slot and fails with `subCode 5751 InvalidMeetingIdentifier`.
>
> If you previously generated `.env.local` and are re-running with `--reuse-env`, regenerate it once (run without `--reuse-env`) so the new variable is written.

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `Teams SDK method selected but ACS_CONNECTION_STRING is not set` | `MEETING_TEAMS_METHOD=sdk` was forced without the connection string. Set it, or use `auto`/`dom`. |
| `Teams SDK setup failed` right after page load | Malformed connection string (must contain both `endpoint=` and `accesskey=`), or the ACS resource was deleted/regenerated its keys. Re-copy the full connection string from **Keys** in the Azure portal. |
| `Anonymous join is disabled for this Teams tenant by policy (subCode 5723)` | The organizer's tenant blocks anonymous users. A Teams admin must enable **"Anonymous users can join a meeting"** (org-wide or scoped to the organizer's meeting policy). |
| `This meeting is a personal (Teams Free / teams.live.com) meeting (subCode 5222)` | The meeting was created by a personal Microsoft account. Not supported by ACS interop — use a meeting organized by an M365 work/school account (see [Supported meeting types](#supported-meeting-types)). |
| `subCode 5751` / `InvalidMeetingIdentifier` in the logs | The meeting ID reached the VP mangled — usually a pasted join URL whose `?p=...` stuck to the ID. Pass the numeric ID or bare `/meet/` slug and the passcode as separate arguments. |
| `access denied (tenant may block anonymous joins, ...)` | The meeting's tenant disables anonymous join, or the meeting ID/passcode is wrong. Verify the invite values; ask the tenant admin about the anonymous-join meeting policy. |
| Join times out (`never-joined`, `acs-not-admitted`) | Held in the lobby past the timeout. Admit the participant, or relax the meeting's lobby setting. |
| Joins but chat messages never appear | Chat requires admission to the meeting (not lobby) and doesn't work for channel meetings. The intro message is queued and sent automatically once chat initializes — check the log for `chat initialized`. |
| Voice assistant / avatar speech is labeled with a human's name in the transcript | Attribution combines the meeting's active-speaker signal with the `AgentSpeakingDetector` (RMS on `agent_output.monitor`). Confirm the voice assistant is enabled, and check the log for `🔊 Agent speaking ON` followed by `Speaker changed to: <LMA identity>` when the agent starts talking. Detector sensitivity is tunable with `AGENT_DETECTOR_ON_RMS` (default 500) and `AGENT_DETECTOR_OFF_MS` (default 2000); the latter is a hangover, so attribution returns to the human ~2s after the agent stops. |
| VP joins but is silent / no avatar | The handler unmutes (when the voice assistant is enabled) and starts the avatar camera after join; check the log for `microphone unmuted` and `camera started via raw media stream`. The avatar reaches the meeting through the patched `getUserMedia`, the bot's voice through the `agent_mic` default source. |

## Related

- [Virtual Participant](virtual-participant.md) — VP overview and lifecycle.
- [Zoom Meeting SDK Join](zoom-meeting-sdk.md) — the equivalent SDK join path for Zoom.
- [Simli Avatar Setup](simli-avatar-setup.md) — avatar configuration shared by both join methods.
