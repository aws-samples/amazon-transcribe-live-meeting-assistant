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
| **ACS SDK** (`sdk`) | Joins through [Azure Communication Services Teams interoperability](https://learn.microsoft.com/azure/communication-services/concepts/teams-interop) — the officially supported way for an external application to participate in a Teams meeting. The VP embeds the ACS calling + chat SDKs (via the [ACS UI Library](https://azure.github.io/communication-ui-library/) `CallWithChatComposite`) in a local page and joins programmatically with meeting ID + passcode. | Used automatically when an ACS connection string is configured. When the meeting's tenant or meeting type refuses it, the VP joins the same meeting with the web client instead (see [Falling back to the browser join](#falling-back-to-the-browser-join)). |

The SDK method renders a full meeting UI (video gallery, active-speaker highlighting, screen-share view, chat panel) into the same browser tab, so the **live (VNC) view**, **meeting audio capture/transcription**, **voice assistant**, and **Simli avatar** all work exactly as they do with the web-client method — only the join mechanism differs. The two methods are at feature parity: intro message, chat commands (`LMA leave`/`stop`/`start`), speaker labels, transcript capture, avatar camera + watchdog, and the alone/host-ended/removed exit reasons are all present. The difference is that the SDK method receives these as deterministic SDK events and API calls instead of scraping the Teams DOM — no fragile selectors, no CAPTCHA risk, no sign-in.

> **Why the SDK method exists.** Teams' web client is not built for automated participants; DOM automation is fragile across Teams UI updates and can trigger CAPTCHA challenges. ACS Teams interoperability is Microsoft's sanctioned path for custom applications (including bots) to join Teams meetings as external participants. On your side it needs no Teams app registration and no approval process — only an Azure Communication Services resource on your own Azure subscription. Whether a given meeting accepts it is decided by the settings of the tenant that organised the meeting, which are not yours to change for meetings other organisations host (see [Supported meeting types and tenant requirements](#supported-meeting-types-and-tenant-requirements)).

## How it's enabled

Enablement follows the same "set the key and it turns on" pattern as the Zoom Meeting SDK:

- When the connection string is set — as either `AcsConnectionString` or `AcsConnectionStringSecretArn` — Teams meetings join via the ACS SDK.
- When it is blank, Teams meetings use the default web-client automation.

An optional task-definition env var `MEETING_TEAMS_METHOD` can force a method regardless of credentials:

| `MEETING_TEAMS_METHOD` | Behavior |
|------------------------|----------|
| `auto` (default) | SDK when the connection string is present, otherwise `dom`. When Teams refuses the SDK join for a reason the meeting's tenant or meeting type decides, the VP joins with `dom` instead, in the same run. |
| `sdk` | Force the SDK (errors at join if the connection string is missing). A refused join ends the VP **FAILED**; there is no fallback. |
| `dom` | Force web-client automation even if the connection string is present. |

This setting only affects Teams; Zoom, Chime, and Webex are unaffected.

## CloudFormation parameters

Set this on the main stack (group **Meeting Assist Voice Assistant**):

| Parameter | Description |
|-----------|-------------|
| `AcsConnectionString` | Azure Communication Services **connection string** (`endpoint=https://...;accesskey=...`, `NoEcho`). |
| `AcsConnectionStringSecretArn` | ARN of a Secrets Manager secret you have already provisioned holding the full connection string. Use instead of `AcsConnectionString`; supply one form or the other, not both. |

Both are optional and default to empty.

The connection string is held in AWS Secrets Manager, in a secret named `<stack-name>/integrations/acs-connection-string` encrypted with the deployment's customer-managed KMS key. The VP task definition names the secret's ARN, and the ECS agent sets `ACS_CONNECTION_STRING` in the container when each meeting's task starts, so the value is not part of the task definition. Under the default `VPLaunchType=MICROVM` the MicroVM launcher does the equivalent, by a different route — see [Integration Credentials](infrastructure-and-security.md#integration-credentials). The access key is never logged and is only used inside the container to create a new ACS identity and a short-lived user token for each meeting. The token lasts the meeting's maximum length plus 30 minutes (at least 1 hour, at most 24), and the identity is deleted when the VP leaves.

**To rotate the connection string:** regenerate the key in the Azure portal under **Settings → Keys** (the resource has a primary and a secondary key, so you can swap to the secondary, update LMA, then regenerate the primary without an outage), put the new full connection string in the `AcsConnectionString` parameter, and apply the stack update. Virtual Participant tasks start one per meeting, so the next meeting to begin uses the new value — no forced ECS deployment is needed, and a meeting already in progress finishes on the value it started with. You can also store the new version directly in Secrets Manager for an immediate change; if you do, set the parameter to match, or the next stack update writes the old value back. See [Integration Credentials](infrastructure-and-security.md#integration-credentials).

## Getting an ACS connection string

You need an Azure subscription (any pay-as-you-go or free-trial account works — this is entirely on the Azure side; nothing is configured in Microsoft Teams or Microsoft 365):

1. Sign in to the [Azure portal](https://portal.azure.com/).
2. **Create a resource → search "Communication Services" → Create**. Pick any resource group, a resource name (e.g. `lma-virtual-participant`), and a data location (e.g. United States). Full walkthrough: [Create a Communication Services resource](https://learn.microsoft.com/azure/communication-services/quickstarts/create-communication-resource).
3. Once deployed, open the resource and go to **Settings → Keys**.
4. Copy the **Connection string** for the **Primary key** (the value that starts with `endpoint=https://...;accesskey=...`). This is the value for `AcsConnectionString`. Do not copy the bare endpoint or the bare key alone — the full connection string is required.
5. There is nothing else to configure on the Azure side: no app registration, no OAuth, no scopes, no publishing. The VP creates a fresh anonymous ACS identity and access token per meeting from this connection string.

### Cost

ACS calling is billed per participant-minute on the Azure resource (currently ~$0.004/participant/min — the VP counts as one participant, so a 60-minute meeting costs roughly $0.24). Teams-side attendees are covered by their own Teams licenses and are not billed to you. Chat messages sent by the VP are ~$0.0008/message. See [ACS pricing](https://learn.microsoft.com/azure/communication-services/concepts/pricing).

## Supported meeting types and tenant requirements

ACS Teams interop only joins meetings **organized by a Microsoft 365 work or school account**. The invite URL's domain tells you which kind of meeting you have:

| Invite URL | Meeting type | ACS SDK join |
|------------|--------------|--------------|
| `teams.microsoft.com/meet/<id>?p=...` or `teams.microsoft.com/l/meetup-join/...` | Microsoft 365 work/school tenant | Supported, if the meeting's tenant lets ACS users join (below) |
| `teams.live.com/meet/<id>?p=...` | Personal / Teams Free (consumer Microsoft account) | Never supported — no tenant setting can enable it (refused with subCode 5222; with the default `auto` method the VP then joins with the browser, see [Falling back to the browser join](#falling-back-to-the-browser-join)) |

Also not supported by ACS Teams interop: webinars/town halls/live events, end-to-end-encrypted meetings, breakout rooms, channel-meeting chat, and GCC (government cloud) tenants.

**ACS users are not the same kind of guest as the browser join.** To Teams, a participant that joins through ACS is an external *application* user (an identity on your Azure Communication Services resource), while the browser join is an anonymous guest of the Teams web client. Teams tenants control the two separately, so a tenant can admit the browser join to a meeting and refuse the ACS join to the same meeting. That has been measured: a meeting whose browser join was admitted refused the ACS join with `403` / subCode `5723` (`AnonymousJoinDisabledDueToPolicy`, *"Anonymous Join is disabled for tenant by policy"*) about 2 seconds after the join started.

Which tenant setting produces 5723 in that case has not been established. These are the settings of the meeting's tenant that its Teams administrator can check, as likely candidates rather than confirmed causes:

1. **The meeting policy "Anonymous users can join a meeting"** (Teams admin center → **Meetings → Meeting policies**, or the org-wide **Meeting settings**; see [Manage anonymous participant access to meetings](https://learn.microsoft.com/microsoftteams/anonymous-users-in-meetings)). This is the setting the refusal's own wording points at. It is not, on its own, a full explanation of the measured case, because the browser guest that was admitted is an anonymous participant as well.
2. **The tenant's ACS federation setting.** A tenant decides whether ACS users may join its meetings, and from which ACS resources, with [`Set-CsTeamsAcsFederationConfiguration`](https://learn.microsoft.com/powershell/module/teams/set-csteamsacsfederationconfiguration) (`EnableAcsUsers`, and the list of allowed ACS resources). If the tenant allows ACS users only from listed resources, your resource has to be on the list.
3. **Identity verification for anonymous users**, where the tenant requires anonymous participants to verify themselves (for example with a one-time passcode sent by email) before they can join. An ACS user cannot complete that step. This has **not** been confirmed as a cause of 5723: it may refuse the ACS join with a different code, or not apply to ACS users at all.

When a meeting's tenant does let ACS users in, **lobby rules apply as normal**: with default meeting options an external participant waits in the lobby until admitted, the same as with the web-client method. Set "Who can bypass the lobby?" to **Everyone** to skip it.

No paid add-ons are needed on the Microsoft side — any Microsoft 365 business/enterprise subscription that includes Teams works (for example Microsoft 365 Business Basic). A Microsoft 365 **Personal/Family** subscription does **not** work: it uses consumer accounts, whose meetings are the unsupported `teams.live.com` kind.

### Hosting your own test meeting

To try the ACS join against a meeting you control, the meeting has to be organised by a Microsoft 365 **work or school** account whose licence includes Teams, for example [Microsoft 365 Business Basic](https://www.microsoft.com/microsoft-365/business/microsoft-365-business-basic). At the time of writing Microsoft offers a trial of that plan; check that page for current availability and pricing rather than relying on this one. An Azure subscription on its own does not include Teams: it gives you the ACS resource the VP joins *from*, not a meeting to join. The Microsoft Entra tenant that your Azure account belongs to can be the same tenant that receives the Microsoft 365 licence, so one organisation can hold both the ACS resource and the test meeting. Meetings created from a personal Microsoft account (`teams.live.com`) never accept an ACS join, whatever plan the account has.

## Falling back to the browser join

With the join method chosen automatically (`MEETING_TEAMS_METHOD` unset or `auto`, the default), the VP tries ACS first and, when Teams refuses the ACS join for one of these reasons, joins the same meeting with the browser (the Teams web client, the `dom` method) in the same run instead of ending **FAILED**:

| Refusal | Why it falls back |
|---------|-------------------|
| subCode **5723** (`AnonymousJoinDisabledDueToPolicy`) | The meeting's tenant turns ACS users away; it can still admit a browser guest (see above). |
| subCode **5222** | A personal `teams.live.com` meeting, which ACS never joins. |

The VP recognises these from Teams' refusal of the join request, or from the call ending with that subCode before admission. It then leaves the ACS call, stops its local ACS page, deletes the ACS identity it created, and starts the browser join from the beginning on the same browser tab. The VP log has one line saying so:

```
[teams-sdk] Teams refused the ACS join (subCode 5723: the meeting's organisation does not let Azure Communication Services users join); joining with the browser instead
```

and the VP's status detail in LMA, still **JOINING**, reads *"Teams did not accept the Azure Communication Services join (…); joining with the browser instead…"* until the browser join's own steps replace it.

Everything else stays a failure, because the browser join would meet it too or because an administrator has to see it: not being admitted from the lobby before the timeout, being declined from the lobby (the ACS page `accessDeniedTeamsMeeting`, which the ACS UI Library shows for subCode 5854 only and describes as not being granted entry to the call), the meeting having ended, subCode 5751 (invalid meeting identifier), a malformed connection string, and any ACS setup or initialisation error.

**`MEETING_TEAMS_METHOD=sdk` turns the fallback off.** Forcing the SDK means ACS and nothing else, so a refused join ends the VP **FAILED** with Teams' reason.

A browser join after a fallback differs in two ways from one chosen from the start: the browser was launched with the WebRTC settings the ACS join needs (the same ones the Simli avatar uses on every platform), and a fresh browser profile is not warmed up first, because the warmup runs before the join and is skipped for the ACS join, which does not visit the Teams website. The fallback is covered by unit tests only; it has not yet been run against a live meeting that refuses ACS.

## Which method joined

Once Teams has admitted the VP, the VP log has one line saying which join got it in:

| Log line | Meaning |
|----------|---------|
| `[teams] joined with Azure Communication Services` | The ACS join. |
| `[teams] joined with the browser` | The browser join, chosen from the start. |
| `[teams] joined with the browser after Teams refused the ACS join (subCode 5723)` | The browser join, after a fallback. |

The Virtual Participant's page in LMA shows the same under **Connection Details → Join method** (*Azure Communication Services*, *Browser*, or *Browser, after Teams refused the Azure Communication Services join (subCode 5723)*). It is stored in the VP record's `joinMethod` field, which is written once after admission and, unlike the status detail line, is not cleared when the status changes. It is absent until the VP is admitted, for platforms other than Teams, and for VPs that ran on a release that did not record it.

## Is ACS more reliable?

For the meetings it can join, it avoids most of what makes the browser join fragile: there is no Teams web page to automate, so a Teams UI change cannot break it and there is no CAPTCHA, and chat messages arrive with their sender's name. But it depends more on the meeting's tenant: an organisation that admits browser guests can still refuse ACS users (above), and personal meetings never accept them. It does not replace the browser join, which remains the default when no ACS connection string is configured and is what the VP falls back to.

## Trying ACS on a deployed stack

1. Create an ACS resource and copy its connection string ([Getting an ACS connection string](#getting-an-acs-connection-string)).
2. Set the main stack's `AcsConnectionString` parameter to it (or `AcsConnectionStringSecretArn` to a secret holding it), keep every other parameter's current value, and apply the stack update. The next VP to start reads it.
3. Start a Teams VP from the LMA UI, or from the CLI with `lma vp create --name "ACS test" --platform TEAMS --id <numeric meeting ID> --password <passcode>` (see [LMA CLI](lma-cli.md)).
4. In the VP's log, look for `Teams join method: sdk` when the join starts, then `[teams-sdk] joined meeting` and `[teams] joined with Azure Communication Services` once admitted, or the fallback line above followed by `[teams] joined with the browser after Teams refused the ACS join (subCode …)`. The Virtual Participant's page shows the outcome under **Join method**.

To go back to the browser join for every Teams meeting, clear the parameter and apply the stack update again.

## Behavior and limitations

- The VP joins as an **external user**, under its display name (`LMA - <user>` by default; see [Virtual Participant › More than one LMA in a meeting](virtual-participant.md#more-than-one-lma-in-a-meeting)). Teams clients mark it as external, for example `LMA - Kirk (External)`.
- Meeting chat works both ways (intro/exit messages and `LMA leave` commands), but only from admission onward — no chat history from before the VP joined.
- **Chat turned off for guests.** On the browser join, Teams turned the meeting chat off for the VP 20 to 27 seconds after admission in live meetings on 2026-11-14, and kept it open in every live run on 2026-11-15 (see [Meeting consent › When Teams turns the chat off](meeting-consent.md#when-teams-turns-the-chat-off)). Whether a meeting does the same to an ACS participant has not been measured. Both joins post the introduction with its chat commands, as on other platforms.
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
| `Teams refused the ACS join (subCode 5723 …); joining with the browser instead` | The meeting's tenant does not let ACS users join. The VP carries on with the browser join; nothing to fix unless you want ACS for that tenant's meetings, in which case see the settings in [Supported meeting types and tenant requirements](#supported-meeting-types-and-tenant-requirements). |
| `Teams SDK join failed: Teams refused the Azure Communication Services join … (subCode 5723 …)`, VP **FAILED** | The same refusal with `MEETING_TEAMS_METHOD=sdk` forced, so there is no fallback. Use `auto`, or ask the meeting's tenant administrator about the settings listed above. |
| `This meeting is a personal (Teams Free / teams.live.com) meeting (subCode 5222)` | The meeting was created by a personal Microsoft account. Not supported by ACS interop; with `auto` the VP joins with the browser instead, and with `sdk` forced it fails (see [Supported meeting types and tenant requirements](#supported-meeting-types-and-tenant-requirements)). |
| `subCode 5751` / `InvalidMeetingIdentifier` in the logs | The meeting ID reached the VP mangled — usually a pasted join URL whose `?p=...` stuck to the ID. Pass the numeric ID or bare `/meet/` slug and the passcode as separate arguments. |
| `access denied (Teams did not grant the participant entry to the call …; subCode 5854)` | The VP was not let in, for example declined from the lobby. Not a fallback case: asking again from the browser would go to the same people. Admit the VP, or check with the organiser. |
| Join times out (`never-joined`, `acs-not-admitted`) | Held in the lobby past the timeout. Admit the participant, or relax the meeting's lobby setting. |
| Joins but chat messages never appear | Chat requires admission to the meeting (not lobby) and doesn't work for channel meetings. The intro message is queued and sent automatically once chat initializes — check the log for `chat initialized`. |
| Voice assistant / avatar speech is labeled with a human's name in the transcript | Attribution combines the meeting's active-speaker signal with the `AgentSpeakingDetector` (RMS on `agent_output.monitor`). Confirm the voice assistant is enabled, and check the log for `🔊 Agent speaking ON` followed by `Speaker changed to: <LMA identity>` when the agent starts talking. Detector sensitivity is tunable with `AGENT_DETECTOR_ON_RMS` (default 500) and `AGENT_DETECTOR_OFF_MS` (default 2000); the latter is a hangover, so attribution returns to the human ~2s after the agent stops. |
| VP joins but is silent / no avatar | The handler unmutes (when the voice assistant is enabled) and starts the avatar camera after join; check the log for `microphone unmuted` and `camera started via raw media stream`. The avatar reaches the meeting through the patched `getUserMedia`, the bot's voice through the `agent_mic` default source. |

## Related

- [Virtual Participant](virtual-participant.md) — VP overview and lifecycle.
- [Zoom Meeting SDK Join](zoom-meeting-sdk.md) — the equivalent SDK join path for Zoom.
- [Simli Avatar Setup](simli-avatar-setup.md) — avatar configuration shared by both join methods.
