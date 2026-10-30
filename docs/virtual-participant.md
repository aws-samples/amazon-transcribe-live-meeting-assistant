---
title: "Virtual Participant"
---

# Virtual Participant

## Table of Contents

- [Overview](#overview)
- [When to use Virtual Participant](#when-to-use-virtual-participant)
- [Several people behind one attendee](#several-people-behind-one-attendee)
- [Supported Platforms](#supported-platforms)
- [Joining a Meeting](#joining-a-meeting)
- [Status Lifecycle](#status-lifecycle)
  - [The status detail line](#the-status-detail-line)
  - [How the VP decides a meeting has ended](#how-the-vp-decides-a-meeting-has-ended)
- [Manual Action Required (CAPTCHA, 2FA, SSO)](#manual-action-required-captcha-2fa-sso)
- [AI-driven self-healing](#ai-driven-self-healing)
- [Zoom Sign-in](#zoom-sign-in)
- [Meeting Scheduling](#meeting-scheduling)
- [Meeting Invitation Parsing](#meeting-invitation-parsing)
- [VNC Preview](#vnc-preview)
  - [How a viewer is authorized](#how-a-viewer-is-authorized)
    - [Known limitations](#known-limitations)
- [Launch Types](#launch-types)
  - [MicroVM launch type (default)](#microvm-launch-type-default)
- [EC2 Instance Types](#ec2-instance-types)
- [Auto-Scaling](#auto-scaling)
- [Transcription Engine](#transcription-engine)
- [Chat Introduction Message](#chat-introduction-message)
- [In-meeting Chat Commands](#in-meeting-chat-commands)
- [Troubleshooting](#troubleshooting)
- [Developer Testing](#developer-testing)
- [See Also](#see-also)

## Overview

> **Not sure which capture option to use?** See [Meeting Sources](meeting-sources.md) for a side-by-side comparison of the Chrome Extension, Stream Audio, and Virtual Participant.

The Virtual Participant (VP) is a headless Chrome browser that joins meetings as a separate participant, driven by Playwright. It runs on ECS (EC2 or Fargate) or, where available, in an AWS Lambda MicroVM — see [Launch Types](#launch-types). It captures audio and metadata, sending them to the LMA Kinesis Data Stream for transcription and processing.

## When to use Virtual Participant

- **Attendees on native desktop or mobile apps**: Participants are free to use native meeting apps (Zoom, Teams, etc.) instead of being limited to the browser — the Chrome Extension and Stream Audio require joining from the web client.
- **Independent attendance**: The VP can join before you arrive, stay after you leave, or attend meetings you do not join at all — including scheduling it in advance.
- **Voice Assistant**: The VP is the only capture option that supports the Voice Assistant (wake phrase, push-to-talk, always-on modes).
- **Open VP live view**: Use the Meeting Assistant's "Open VP live view" feature to see the bot's browser view of the meeting in real time.

See [Meeting Sources](meeting-sources.md) for the full comparison.

## Several people behind one attendee

A Virtual Participant names speakers from the meeting roster, which is exact for
an ordinary remote attendee and useless for a conference room: everybody sharing
one speakerphone shares one roster entry, so a whole room arrives as a single
speaker.

**Virtual Participant voice separation** on
**Configuration ▸ Transcription Engine** fixes that. With it on, the VP tells
apart the distinct voices behind one attendee and labels them
`Conference Room (spk_0)`, `Conference Room (spk_1)` and so on — the roster name
for who the platform says is talking, the voice id for which voice within it.

Name them on the
[Speakers panel](transcription-and-translation.md#naming-the-voices), which
lists one row per voice and relabels every segment that voice produced.

The other two naming routes do **not** apply to a Virtual Participant today, and
it is worth knowing which:
[naming from what people say](transcription-and-translation.md#finding-the-names-automatically)
only considers voices whose base name is a placeholder such as
`Other Participant`, and a VP's base name is always a real roster name; and
[voiceprints](voice-enrollment.md) are seeded only into streaming sessions, not
into a VP's.

It works on either transcription engine. On Amazon Transcribe the turns are
recovered from Transcribe's own per-item speaker labels; on the
[on-demand engine](microvm-asr.md) they come from that engine's own separation.
Either way the switch is read once at the start of each meeting, so it takes
effect on the next meeting with no redeployment.

Three limits are worth knowing.

Labels arrive only when a segment finalises. Amazon Transcribe caps a result at
about thirty seconds, so the live transcript can show one roster name, and no
voice ids, for that long before the row splits into voices. The words themselves
are not lost; a watcher simply sees the separation late.

Accuracy depends on the audio, and on which engine. A room heard through a
speakerphone, compressed by the meeting platform and mixed with everyone else,
is the hardest input separation gets.

Measured on a deployed stack with four synthesised voices mixed onto one
channel, 33 seconds, identical audio to both engines:

| Engine | Distinct labels for 4 voices | Time-weighted purity |
|---|---|---|
| Amazon Transcribe | 2 | 57% |
| [On-demand engine](microvm-asr.md) | 3 | 79% |

Purity here is the share of labelled time whose label maps to a single real
person; 100% would mean every label belongs to exactly one speaker. **Prefer the
on-demand engine for rooms.** On Amazon Transcribe each label straddled more
than one person, so naming those rows attaches a real name to a substantial
share of somebody else's words — which is worse than the unseparated
`Conference Room`, because that at least claims nothing it cannot support.

Both engines merged the speaker whose turns were under about a second
("Agreed.", "Fine by me."): too little audio to characterise a voice. Expect
short interjections to be attributed to whoever spoke around them.

Synthesised voices may be more alike than real ones and the test had no room
reverb or background noise, so treat the figures as directional rather than a
guarantee for your room.

To see how a particular room separated, set the stack's **LogLevel** parameter
to `DEBUG`. The Virtual Participant then logs one `[DIARIZATION]` line per
Transcribe result, giving the number of segments it produced and the speaker it
assigned to each — which is what tells you whether the labels or the platform's
active-speaker signal is the thing going wrong.

Note what that writes: the speaker names on those lines are **participant
names**, so at `DEBUG` they reach CloudWatch Logs. No transcript text is
written by this line. Turn the level back down once you have what you need.

Where the two signals disagree, the finer boundary wins: a voice label running
across a change of platform speaker is split at the change. A turn split in two
can be put right with **Only this segment** on the meeting page, which is the
affordance for a single turn attributed to the wrong voice; a turn merged under
one name cannot be separated at all. (Renaming on the Speakers panel would not
help here — it is per voice, so it would rename every turn that voice produced,
not the one that was misattributed.)

The voice assistant's own speech is transcribed on a channel of its own on both
engines, so it never acquires a voice id.

## Supported Platforms

- Zoom
- Microsoft Teams
- Amazon Chime
- WebEx

**Google Meet is not supported by the Virtual Participant.** It is supported by the [Chrome extension](browser-extension.md), which captures a meeting you are in yourself — so Meet meetings can be transcribed, but not attended unattended or scheduled in advance. Adding Virtual Participant support is tracked in [#661](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/issues/661).

## Joining a Meeting

1. Navigate to **Virtual Participant** in the LMA UI.
2. Enter meeting details: URL, platform, meeting ID/password, and meeting name.
3. Click **Join Now**.
4. The VP starts in approximately 20-25 seconds (MicroVM, the default), 30-60 seconds (EC2), or 1-2 minutes (Fargate).
5. Once joined, the VP posts an introduction message in the meeting chat.
6. View VP status in the UI as it progresses through its lifecycle (see [Status Lifecycle](#status-lifecycle)).

## Status Lifecycle

The VP reports a granular status as it boots, joins, and runs. The UI uses these to give a precise picture of where in the startup sequence the VP is:

| Status | What's happening |
| --- | --- |
| `INITIALIZING` | Step Functions has submitted the ECS RunTask request (or `RunMicrovm` under `MICROVM`); the VP is being scheduled |
| `WAITING_FOR_CAPACITY` | The task is queued waiting for compute placement (typically 60-90 seconds). On **EC2**, the capacity-provider auto-scaler may be launching a new host (see [Auto-Scaling](#auto-scaling)); on **Fargate**, this is the brief serverless provisioning / networking step. The UI tailors this message to your deployment's `VPLaunchType`. |
| `BOOTING` | Container started; pulling Chrome image, starting Xvfb / VNC / PulseAudio |
| `REGISTERING_NETWORK` | Registering the task with the live-view ALB (typically 30-60 seconds). Skipped under `MICROVM`, which has no ALB — each MicroVM exposes its own HTTPS endpoint. |
| `HYDRATING_PROFILE` | Restoring the per-user Chromium profile (cookies, "trusted device" markers) from S3 |
| `LAUNCHING_BROWSER` | Launching Chromium via Playwright (`chromium.launchPersistentContext`) |
| `VNC_READY` | Browser is up; live-view viewer can connect |
| `CONNECTING` | Initializing audio/video pipelines (Nova Sonic, Simli avatar, agent mic) |
| `JOINING` | Navigating to the meeting URL; signing in to Zoom if credentials are stored |
| `MANUAL_ACTION_REQUIRED` | A CAPTCHA, 2FA, SSO, consent dialog, or other Zoom verification step needs human input — see [Manual Action Required](#manual-action-required-captcha-2fa-sso) |
| `ACTIVE` / `JOINED` | VP is in the meeting and capturing audio |
| `COMPLETED` / `ENDED` | Meeting ended cleanly (host removed the VP, all attendees left, or the VP got "lonely") |
| `FAILED` | The VP could not join. The DDB record carries an `errorMessage` describing what went wrong (e.g. *"Meeting join failed: …"*, *"Zoom login failed: invalid credentials"*, *"ECS RunTask soft-failure: agent not connected"*) |

When ECS reports a "soft failure" on RunTask (returns HTTP 200 with a non-empty `failures` array — typically the container instance agent is briefly disconnected), the state machine catches it explicitly and writes `FAILED` plus the original failure reason to the VP record, instead of leaving the VP stuck in `INITIALIZING` indefinitely. That write is conditional on the row not having reached a terminal status, so a task that did start and is reporting its own outcome keeps it.

### The status detail line

Alongside `status`, the VP record carries two fields describing the current status in more detail:

| Field | Contents |
| --- | --- |
| `errorMessage` | The prose the UI shows under the status. It is the detail line for every status, not only failures: the reason a meeting ended on `COMPLETED` (*"Asked to leave by Jeremy."*), the current sub-step while the VP is still `JOINING` (*"Waiting to be admitted…"*), and the error on `FAILED`. |
| `endTrigger` | The machine-readable counterpart: which branch the VP exited through, from the vocabulary below. Written on `COMPLETED` and `FAILED`, so join outcomes across many meetings can be counted with a query over the VP table instead of a search through container logs. |

Reaching `JOINED`, `ACTIVE` or `COMPLETED` without a new detail to show clears `errorMessage`, so a message from earlier in the same run — a join sub-step, or an attempt that did not work out before a later one did — is not still displayed next to a VP that is in the meeting.

Most values are one of these fixed strings:

| Group | Values |
| --- | --- |
| Never got in | `pre-join:not-admitted`, `pre-join:no-join-button`, `pre-join:no-name-input`, `pre-join:goto-failed`, `pre-join:ai-error-screen`, `acs-not-admitted`, `sdk-not-admitted`, `enterprise-login-timeout` |
| Asked to leave in chat | `chat` |
| The meeting ended | `post-meeting-screen`, `meeting-ended-text`, `HANGUP_BUTTON_HIDDEN`, `REJOIN_BUTTON_APPEARED`, `ZOOM_END_DIALOG`, `SDK_HOST_ENDED`, `SDK_REMOVED`, `SDK_DISCONNECTED` |
| Everyone else left, or the VP was removed | `attendees-left`, `acs-participants`, `sdk-participants`, `participants-watchdog`, `participants-ui:counter-gone`, `participants-ui:page-dead`, `attendee-badge-missing` |
| Ran out of time, or lost the page | `meetingTimeout`, `renderer-crash`, `navigated-away`, `URL_CHANGE_BLANK`, `ZOOM_END_DIALOG_ABORTED` |
| The join threw, and the message matched one of the container's six classes | `error:renderer-crashed`, `error:password`, `error:meeting-not-found`, `error:meeting-ended`, `error:permission-denied`, `error:unclassified` |

The rest are a fixed prefix plus a detail that came from the platform, so the prefix is what groups and what follows the colon varies:

| Form | Written when | Detail after the colon |
| --- | --- | --- |
| `ACS_REMOVED:…`, `ACS_DISCONNECTED:…` | The Teams SDK reported the call disconnected, or the VP removed from it | The SDK's own `code` and `subCode`, or `no-endreason` |
| `webex-text:…` | A Webex meeting-end phrase appeared on the page | Which of five phrases matched |
| `error:…` | The Webex watcher threw while waiting for the meeting to end — so, after the VP was admitted | The start of the underlying browser message |
| `poll-error:…` | The Chime meeting-end poll threw | The start of the underlying browser message |

Note that `error:` appears in both tables and means different things in each. The six values in the first are written by the container's own classifier when a *join* throws, and are fixed. An `error:` value that is not one of those six came from the Webex watcher *after* admission, and its detail is whatever the browser said — so **the value space is open**, and this page cannot list it. Group those by prefix and read `errorMessage` for the detail, which is where prose belongs.

Whatever follows the first colon is reduced to a lower-case, hyphenated slug before the value is stored, and the whole value is capped at 100 characters. That is what keeps the second table countable: two reports of the same failure become the same string whatever the wording or punctuation around them. A trigger with no colon is stored exactly as it is.

`COMPLETED` means the VP was in the meeting. A VP that never got in — not admitted, or stuck on the pre-join screen — ends as `FAILED` with *"Could not join the meeting (was not admitted, or stuck on the join screen)."*, on every platform.

### How the VP decides a meeting has ended

The VP leaves on its own so an abandoned meeting is not recorded (or billed) indefinitely. It uses several signals, in order of confidence:

1. **An in-meeting chat command** — `LMA leave` and its synonyms; see [In-meeting Chat Commands](#in-meeting-chat-commands).
2. **A positive end-of-meeting signal from the platform** — on Teams, the post-meeting screen ("You left the meeting", "The meeting has ended") or a post-meeting URL, checked before anything else on every poll, so a meeting Teams announces as over is detected on the first poll. The hang-up button disappearing is a second such signal, watched continuously rather than on the poll cycle.
3. **A sustained empty roster** — the participant count is present and reads one or fewer for several consecutive polls.
4. **A sustained absence of the whole meeting UI** — neither a readable participant count nor any in-meeting controls. This is the backstop for the case where none of the above fires.

The distinction between the third and fourth signals matters, and getting it wrong is what caused [#660](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/issues/660). Teams removes the roster button, and with it the participant-count badge, whenever the meeting toolbar collapses — most commonly during a content share or in full-screen layouts. An unreadable badge therefore says nothing on its own: it is equally the signature of a meeting that ended and of a meeting where someone is presenting. So before an unreadable count counts towards leaving at all, the VP checks whether the rest of the in-meeting interface (the stage, the calling screen, the call controls) is still present, and while it is, the VP stays. That matters because nothing in the VP moves the pointer, so a toolbar that auto-hides during a share can stay hidden for the whole presentation.

That check is itself bounded, deliberately, because Teams is not reliable in the other direction either: it sometimes leaves in-meeting controls on screen after a meeting has ended, which is how a Virtual Participant once kept recording long after everyone had gone. So the in-meeting interface can hold the VP in a meeting with an unreadable roster for about 30 consecutive minutes, after which the unreadable count starts counting again and the VP leaves about five minutes later. A readable count at any point restores the full allowance, so a meeting containing several long shares is fine; it is one unbroken half-hour with no readable roster at all that is treated as suspicious. The worst case in either direction is therefore bounded: a share is safe for half an hour at a stretch, and a meeting whose end Teams announces to nobody is left after about 35 minutes at the default cadence — and never more than an hour, whatever the settings below.

Three CloudFormation parameters control the timing. They apply to the **Teams browser join path** only — Zoom has its own equivalent, and the Teams ACS SDK join path (used when `AcsConnectionString` is configured) gets deterministic roster events instead and needs none of this. Both the ECS and MicroVM launch types honour them. The defaults suit Teams as it behaves today; change them only if a deployment is consistently leaving meetings early, or lingering after they end:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `VPAttendeePollMs` | `20000` | How often the VP samples the meeting UI, in milliseconds. Range 1000–120000. |
| `VPPollsBeforeEnd` | `3` | Consecutive polls reading one or fewer attendees before the VP leaves — about 60 seconds at the default cadence. Range 1–90. |
| `VPPollsBeforeEndMissing` | `15` | Consecutive polls with neither a readable attendee count nor any in-meeting controls before the VP leaves — about 5 minutes at the default cadence. Range 1–90. |

Editing the VP task definition by hand does not work: both launch types reference a specific task-definition revision, so a revision registered outside CloudFormation is never used. Change the parameters and update the stack instead. That does not rebuild the VP container image, so it takes effect on the next meeting.

The values reach the container as the `VP_ATTENDEE_POLL_MS`, `VP_POLLS_BEFORE_END` and `VP_POLLS_BEFORE_END_MISSING` environment variables, which is also how they can be set when [running the container locally](virtual-participant-local-dev.md). The container also bounds them on its own, in both directions: a value that is not a whole number in range is ignored in favour of the built-in default, and each tolerance is held inside a wall-clock window however the cadence and the count combine: 4 to 30 minutes for an unreadable meeting, and 30 seconds to 10 minutes for an empty one. So lowering the cadence for more frequent logging cannot accidentally narrow a tolerance to seconds, and raising a count cannot leave a VP running without a backstop. A count outside its window is adjusted rather than honoured, with a line in the log saying so — at the default cadence, for instance, `VPPollsBeforeEndMissing` below 12 is raised, despite the parameter accepting 1. Note that a fractional value such as `15.5` passes CloudFormation's own validation for a `Number` parameter and is then ignored by the container in favour of the default, with a line in the log saying so.

Every decision the watchdog makes is logged. The resolved values appear once per meeting next to `Listening for attendee changes`; each poll logs its running counter against the bound that applies to it; and a poll held back by the in-meeting interface reports which element it matched, along with how much of the 30-minute allowance is left. If a Virtual Participant ever leaves a meeting that was still running, or lingers after one ended, those lines are what to look at first.

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

### How a viewer is authorized

Opening the viewer takes two steps, both of which happen automatically in the UI, and rests
on a third control at the load balancer.

**1. A token for the connection.** Before it opens the WebSocket, the viewer calls a
GraphQL mutation to mint a token for the specific Virtual Participant it is about to
watch. The resolver looks the VP up in DynamoDB and mints only for the VP's owner, a
user it has been shared with, or a member of the `Admin` group. Which mutation it
calls depends on the launch type:

| Launch type | Mutation | Transport |
|---|---|---|
| `MICROVM` | `createMicrovmVncToken(vpId)` | Port-scoped auth token passed as a WebSocket subprotocol to the MicroVM's own endpoint |
| `EC2` / `FARGATE` | `createVncEdgeToken(vpId)` | Token passed as the `token` query parameter on `wss://<cloudfront>/vnc/<vpId>` |

For the ECS launch types the token is `base64url(payload).base64url(HMAC-SHA256(payload))`.
The payload names the `vpId`, the `/vnc/<vpId>` path, the target port, and an expiry a few
minutes out. A Lambda@Edge viewer-request function on the CloudFront `/vnc/*` behavior
recomputes the MAC and forwards the request only when the signature matches, the expiry is
inside the permitted window, and both the `vpId` and the path in the payload equal the ones
in the request. The signing key is generated per deployment into AWS Secrets Manager and
read only by the minting resolver and the edge function; the edge function fetches it once
per cold start from the stack's home region (Lambda@Edge takes no environment variables and
supports no layers, so the secret ARN and region are substituted into its source at deploy
time) and refuses the request if it cannot read it. Both mutations mint on demand per viewer
session, so the viewer's existing auto-reconnect simply mints again.

This is one of three controls, not the whole of it. The edge function sees traffic that
arrives through this deployment's CloudFront distribution, which is what step 3 below
establishes for every route into a task; the credential in step 2 applies at the VNC server
itself.

**2. A credential for the VNC server.** On the ECS launch types the VP generates a
random VNC credential when it boots and publishes it on its own record; the
`createVncEdgeToken` response carries it back to the already-authorized viewer, which
supplies it to the server. The credential belongs to one task and is replaced on every
boot. Under `MICROVM` the VP runs as a pre-snapshot stack at image-build time, where
there is no per-meeting record to publish a credential to and any credential would be
captured in the snapshot and shared by every launch; there the framebuffer is bound to the
VM's loopback interface and the port-scoped auth token is the only route in.

**3. An origin-verify header on the load balancer.** The CloudFront distribution attaches a
per-deployment header value to every request it sends to the VNC load balancer, and the load
balancer forwards only requests carrying it — anything else gets a 403. This applies to both
routes into a task: the rule the AI stack creates for the shared target group, and the rule
each Virtual Participant creates for its own path at run time. The participant is given the
secret's ARN, not its value, and reads the value from Secrets Manager once when it starts, so
the value never appears in its task definition; if it cannot read the value it does not
create a routing rule at all, and the participant fails to start rather than being reachable
without the header.

The load balancer's security group additionally admits only CloudFront's
`com.amazonaws.global.cloudfront.origin-facing` managed prefix list. That is a useful coarse
filter, and it establishes that a request came from CloudFront. The header establishes the
narrower property: that it came through this deployment's own distribution.

> **If you are upgrading, these take effect only after the Virtual Participant container
> image is rebuilt** — both are applied in code that runs inside the container:
>
> - The framebuffer's loopback binding (`entrypoint.sh`). Under `MICROVM` this runs at
>   image-build time, so it lands when the MicroVM snapshot is rebuilt; existing snapshots
>   keep the previous binding until then.
> - The origin-verify condition on the per-participant routing rule (`status-manager.ts`).
>
> Everything else in this section — the token check at the edge, the mutations, and the
> load balancer's own rule and default response — is CloudFormation and applies as soon as
> the stack update completes.
>
> Virtual Participants that are already running when the stack updates keep the routing rule
> their task created; those rules are deleted when each participant's meeting ends, by the
> same cleanup that removes its target group, so they clear on their own as those meetings
> finish. A rule can outlive its task if that task ended without the cleanup running; a
> participant started afterwards on the rebuilt image replaces any rule it finds on its own
> path before creating its own, and a leftover rule for a participant that no longer exists
> can be deleted from the load balancer's listener by hand (its `VirtualParticipantId` tag
> names the participant it belonged to).

#### Known limitations

These are current trade-offs rather than oversights, recorded so they are visible when
planning changes to this path.

**The ECS token is a bearer credential in a URL.** Browsers cannot set headers on a
WebSocket handshake, so the token travels as the `token` query parameter, and the `/vnc/*`
behavior sets `ForwardedValues: QueryString: true`. It therefore appears in CloudFront
access logs and in browser history, and it is a bearer credential — valid for whoever
presents it, with nothing binding it to the browser it was minted for. Its few-minute
lifetime and the VNC server credential are what bound that. The MicroVM path avoids
carrying the token in the URL at all, by passing it as a WebSocket
subprotocol, which is the same trick applied to a different endpoint. The same approach
could in principle be used here, but websockify would have to be configured to accept and
ignore the extra subprotocol, and the edge function would have to read it from the
`Sec-WebSocket-Protocol` header instead of the query string; that has not been done.

**The edge function calls Secrets Manager cross-region on a cold start.** A viewer-request
trigger is capped at 5 seconds and 128 MB, and the signing key lives in one region while
the function runs replicated in whichever edge region is nearest the viewer. The call is
made once per container and cached for its lifetime, and the function refuses the request
if it cannot complete — so the failure mode is a viewer in a distant region intermittently
being unable to open the live view, not an unchecked request. Lambda@Edge cannot be
pre-warmed. Worth watching after a first deployment.

**Rotating the signing key requires a deliberate rollover.** The key is cached for the life
of each edge container with no TTL, and nothing rotates it on a schedule. Replacing the
secret's value therefore leaves already-warm replicas verifying against the previous key and
rejecting newly minted tokens, for as long as those containers live — an interval that is
not bounded by anything in the design. A rotation needs either a tolerated window of
failures or a change that lets the function hold more than one key.

**Rotating the origin-verify value is harder still, and needs a stack update.** Five places
hold it: the CloudFront origin's header and the load balancer's own rule, both resolved at
stack create/update; the in-memory copy each running task read when it started; the literal
baked into each per-participant rule already on the listener; and Secrets Manager itself,
which newly started tasks read live. Replacing the secret's value **without** a stack update
leaves running participants working while every newly started one becomes unviewable — its
rule carries the new value, the distribution still sends the old one, so requests fall
through to the load balancer's own rule and return a 503 from the shared target group, which
holds no targets. Doing it **with** a stack update inverts that: new participants work and
every already-running one becomes unviewable for the remainder of its meeting. Either way a
rotation requires a stack update and interrupts live viewing for an interval bounded only by
how long the longest running meeting lasts. There is no rotation schedule on this secret, or
on any secret in this solution. Live viewing is a diagnostic aid rather than part of the
capture path, so a rotation does not affect transcription or recording.

**The hop from CloudFront to the load balancer is not encrypted.** The `vnc-alb` origin is
configured `OriginProtocolPolicy: http-only`, so the origin-verify header, the access token
in the query string, the VNC server credential in the RFB handshake and the framebuffer
itself all travel in clear text between the CloudFront edge and an internet-facing load
balancer. HTTPS to this origin is not available: CloudFront requires a publicly trusted
certificate matching the origin's domain name, and one cannot be issued for an
`*.elb.amazonaws.com` name. Moving the load balancer behind a **CloudFront VPC origin** is
the change that removes this along with the public DNS name, and is the recommended
direction for this path.

## Launch Types

### MicroVM launch type (default)

`VPLaunchType=MICROVM` is the **default**. It runs each Virtual Participant inside an [AWS Lambda MicroVM](https://docs.aws.amazon.com/lambda/latest/dg/microvms.html) — a Firecracker VM resumed from a memory+disk snapshot — instead of an ECS task. It runs **the same container image** as the ECS launch types; only the surrounding infrastructure differs.

**Requirements and limits** (all imposed by the service, not by LMA):

| | |
|---|---|
| Architecture | **ARM64 only** |
| Regions | us-east-1, us-east-2, us-west-2, ap-northeast-1, eu-west-1 |
| Max meeting duration | **8 hours** (hard ceiling) |
| Connections per MicroVM | 8 (not adjustable) |

**What it changes:**

- **No ALB.** Each MicroVM returns its own HTTPS endpoint, so the VNC ALB, its target group, listener, security groups, CloudFront `/vnc/*` behavior and the Lambda@Edge auth function are not created at all. The viewer authenticates with a short-lived, port-scoped MicroVM auth token instead. This is why `MICROVM` also removes those resources from the AI stack.
- **No hosts to scale.** No EC2 ASG, no capacity provider, no SSM host patching. Billing is per meeting rather than per warm instance, and there is no `WAITING_FOR_CAPACITY` wait for a host to launch.
- **Faster startup.** The Firecracker snapshot is taken with Xvfb, x11vnc, websockify and PulseAudio already running, and the image-build `/validate` hook exercises a real Chromium so Lambda prefetches its snapshot pages. Measured on a live deployment: VNC ready in ~20-25s.
- **Config delivery.** ECS passes per-meeting values as `ContainerOverrides` environment variables. MicroVM image environment variables are baked into the image and shared by every MicroVM launched from it (and capped at 50), so the launcher stages the full configuration in the VP task registry and the `/run` hook payload carries only the VP id. The service caps `runHookPayload` at 4096 bytes, which three Cognito JWTs alone exceed.
- **Egress through your VPC.** A `AWS::Lambda::NetworkConnector` routes MicroVM outbound traffic through the private subnets, so the VP reaches meeting platforms from the same NAT gateway Elastic IP as an ECS task. This costs no hourly charge (NAT data processing applies, which the ECS launch types already pay) and keeps meeting-platform-visible behavior identical across launch types.

**Still requires a VPC.** The WebSocket transcriber mandates the VPC and its NAT gateways regardless of how the VP is hosted, so `MICROVM` does not remove the VPC stack — it removes the VP's ALB.

### EC2

EC2 launch type uses warm instances with cached Docker images. This provides 85-90% faster startup compared to cold Fargate launches, with the VP ready in approximately 30-60 seconds. The estimated cost is approximately $33/month for always-on instances.

EC2 is the recommended choice in regions where Lambda MicroVMs are not available and startup latency matters.

### Fargate

Fargate launch type is serverless and uses SOCI (Seekable OCI) for faster container image pulls, providing 40-60% faster startup than standard Fargate. The base cost is approximately $2/month, making it more economical for infrequent use. However, startup time is longer at 1-2 minutes.

## EC2 Instance Types

> Applies to `VPLaunchType=EC2` only. Fargate and MicroVM have no instances to size.

Each VP container is capped at 3500 MB (observed peak memory is ~1650 MB with Chromium + Simli + Nova Sonic; the cap leaves headroom). A full voice-assistant + Simli-avatar meeting draws ~1.35 vCPU steady-state and runs reliably on the default **`t3.medium`** (the CloakBrowser stack downscales the avatar to 256×256@15fps and disables the emulated GPU, which keeps the encode cheap). Pick a larger instance mainly for **more concurrent VPs per host** rather than for a single meeting's headroom — and ensure host memory accommodates your expected concurrent VPs plus ~600 MB for the OS / ECS agent:

**General Purpose:**
- `t3.medium` (default) -- 3867 MB host → 1 concurrent VP. The capacity-provider auto-scaler launches additional hosts when concurrent demand exceeds capacity, so users running one meeting at a time pay the baseline (~$30/month per host) and only scale up while multiple VPs are active.
- `t3.large` -- 7857 MB host → 3 concurrent VPs. Pick this if you regularly run 2-3 concurrent meetings and want fewer scale-outs.
- `t3.xlarge` -- 15.7 GB host → 6 concurrent VPs.

**Compute-Optimized (non-burstable; for higher concurrency or guaranteed sustained CPU):**
- `c5.large` (2 vCPU) -- a dedicated-CPU alternative to `t3.medium` for a single voice + avatar VP (no burstable credit limits)
- `c5.xlarge` (4 vCPU) -- multiple concurrent voice + avatar VPs per host
- `c5.2xlarge` -- heavier concurrency

**Memory-Optimized (for large meeting context):**
- `m5.large` / `m5.xlarge` -- when meeting context / model memory needs are higher

`t3` instances are burstable: a single voice + avatar VP fits the `t3.medium` baseline, but if you run **multiple concurrent** VPs per host (or very long meetings), prefer `c5.*` / `m5.*` for guaranteed sustained CPU.

## Auto-Scaling

> Applies to `VPLaunchType=EC2` only. Fargate scales per task, and MicroVM launches one Firecracker VM per meeting with no cluster to scale.

The VP cluster uses an **ECS capacity provider with managed scaling**:

- **`VPMinInstances`** (default `1`) -- Minimum warm hosts always running. Set to `0` to fully scale down when idle and pay only when a VP is requested (cold-start adds ~60-90s to the first VP).
- **`VPMaxInstances`** (default `10`) -- Hard ceiling on concurrent hosts. With the `t3.large` default each host fits 3 VPs, so 10 hosts × 3 VPs = 30 concurrent meetings.

When concurrent demand exceeds the current cluster's capacity, ECS automatically launches new hosts (`TargetCapacity=100`, step size 1-2, instance warmup 90s). The launching VP shows status `WAITING_FOR_CAPACITY` while the auto-scaler provisions a new host — typically 60-90 seconds — then transitions to `BOOTING`. Scale-down to `VPMinInstances` happens automatically when reservation drops.

`ManagedTerminationProtection` is enabled, so a host running an active VP will never be killed by scale-in until the VP exits.

## Meeting Video Recording

In addition to the audio recording, the Virtual Participant can capture the meeting **screen** (screen shares, slides, participant video, the platform UI) as an MP4. The VP records its own X11 display — the same browser view you see in the live VNC viewer — with FFmpeg, uploads 60-second segments to S3 as the meeting runs (so a container crash loses at most the last segment), and assembles a single seekable `.mp4` when the meeting ends.

**Enabling / disabling**

- **Per meeting**: the **Create Virtual Participant** modal has a **Record meeting video** toggle (default **on**). Turn it off to capture audio + transcript only for that meeting.
- **Per deployment**: the `EnableVideoRecording` CloudFormation parameter (default `true`) sets the default the toggle starts from and applies to launches that don't specify a value (e.g. the `start_meeting_now` MCP tool).

**Playback**: once the meeting ends and the video finishes uploading, a **Recording Video** player appears in the meeting detail view alongside the **Recording Audio** player. The URL is stored on the call record (`Call.VideoRecordingUrl`) and presigned with your Cognito credentials at playback time — the same mechanism as the audio recording.

**Storage & retention**: final videos are written to the recordings bucket under `lma-video-recordings/` and follow the same retention as audio (`AudioRecordingExpirationInDays`). In-progress segments under `lma-video-chunks/` are deleted as soon as the final `.mp4` is assembled; a 1-day S3 lifecycle rule reaps any chunks orphaned by a crash. Expect roughly **30-50 MB per meeting** at the default 5 fps (vs. ~5 MB for audio-only).

**Resource cost**: capture runs at 5 fps with `libx264 -preset ultrafast` to keep CPU overhead low (~5-10%). This fits comfortably on the default `t3.medium` host for a typical VP; if you run many concurrent video-recording VPs per host, size the instance up. Tunable via the `VIDEO_FRAMERATE`, `VIDEO_RESOLUTION`, and `VIDEO_SEGMENT_DURATION` task-definition environment variables.

## Transcription Engine

A Virtual Participant is transcribed by Amazon Transcribe. With a voice assistant
configured, the VP sends Amazon Transcribe one stream with two channels, with channel
identification on: the meeting's participants on the first channel and the assistant's
voice on the second, the same layout as Stream Audio's tab and microphone. Participant
rows are on the `CALLER` channel, named from the meeting roster, and only they are
checked for wake phrases and stop requests; the assistant's rows are on the `AGENT`
channel under the VP's own name. A participant who talks over the assistant therefore
gets a line of their own, and the assistant can never wake or stop itself. A two-channel
stream is billed for its duration, the same as one mono stream. Without a voice
assistant the VP sends one mono stream of the meeting audio. The S3 audio recording is
always the mix of both, as is the video recording's audio track.

A deployment with
`EnableMicrovmAsr=true` can move Virtual Participants onto the on-demand MicroVM
ASR engine instead, from **Configuration ▸ Transcription Engine** (admin only):

- **Virtual Participants: engine** chooses the engine for every VP that starts after the
  change. The VP acquires and releases its own MicroVM, and falls back to Amazon
  Transcribe when none can be acquired.
- **Virtual Participant voice separation** asks the on-demand engine for per-voice
  labels, so several people behind one attendee tile (a conference room, a shared screen
  playing a recording) come out as `Name (spk_0)`, `Name (spk_1)`. Off by default:
  speaker names come from the meeting roster, which is the better label for a normal
  attendee.
- **The voice assistant is its own channel.** With the assistant enabled, its speech is
  transcribed from a second engine session on the same MicroVM and appears on the `AGENT`
  channel under the VP's own name, never diarized, exactly as Stream Audio separates the
  microphone from the tab. Meeting rows are attributed to the roster by when they start.
- **Pausing keeps the engine session.** The chat pause command stops feeding meeting audio
  but leaves both sessions up, so the resume command continues the same transcript. If the
  engine dies mid-meeting, Amazon Transcribe takes over for the rest of it.

See [MicroVM ASR](microvm-asr.md) for the engine itself.

## Chat Introduction Message

The VP posts a customizable introduction message in the meeting chat when it joins. This message informs meeting participants that the VP is present and recording. You can configure the message content to suit your organization's requirements and compliance policies.

## In-meeting Chat Commands

Anyone in the meeting can control the VP by typing one of these commands in the meeting chat. Commands work across Zoom, Microsoft Teams, Webex, and Chime. Every command is addressed to `LMA` by name.

| Command (typed in chat) | What it does |
| --- | --- |
| `LMA leave`, `LMA end`, `LMA stop`, `LMA quit`, `LMA exit` | The VP says goodbye and leaves the meeting. |
| `Goodbye LMA`, `bye LMA` | Same as above. |
| `LMA pause` | Stops transcription and recording without leaving the meeting. |
| `LMA start` | Resumes a paused session. |
| `LMA help` | The VP posts the list of chat commands in the meeting chat. |
| `LMA ask <question>`, `@LMA <question>` | The meeting assistant answers the question in the meeting chat, using the meeting's transcript. See [Asking the Meeting Assistant from Chat](#asking-the-meeting-assistant-from-chat). |

When a Nova Sonic voice assistant is configured, the assistant-mode commands (`LMA translate`, `LMA translate <language>`, `LMA assist`, `LMA listen`, `LMA always`, `LMA wake`) also work, and `LMA help` lists them; see [Changing Mode During a Meeting](voice-assistant.md#changing-mode-during-a-meeting). With no voice assistant, or with ElevenLabs, which cannot change mode during a meeting, `LMA help` leaves them out.

Notes:

- Every command requires the literal token `LMA` in the message. For the dismissal commands that means bare words like `END` or `LEAVE` are ignored so that prose like *"the meeting will end at 3pm"* or *"I'll leave at 3"* never trips a false dismissal. The `LMA` and verb tokens are matched case-insensitively and word-bounded (so `endless` or `ENDPOINT` don't match either).
- When the chat platform exposes the sender name (Zoom, Chime, Webex), the goodbye message acknowledges who asked the VP to leave: *"Thanks Alice — I'll head out now."*
- The default introduction message tells participants to type `LMA leave` to dismiss the bot and `LMA help` to see the other commands. Customize via the `IntroMessage` CloudFormation parameter of the Virtual Participant stack or the `INTRO_MESSAGE` env var; a customized message is used as it is.
- `LMA pause`, `LMA start` and `LMA help` must be the whole chat message: the `LMA` addressee (or `@LMA`, before or after the command) plus the command word, matched case-insensitively with punctuation ignored, so *"@LMA, pause!"* and *"start LMA"* count as the command. A bare `PAUSE` or `START` without the addressee is ordinary chat, as is a message that merely contains the command — *"LMA PAUSED"*, *"please LMA pause now"*, or an introduction message that explains the commands.
- The help reply is posted as one or two single-line chat messages, listing each command in one canonical form. It is answered whether or not transcription is paused.

### Asking the Meeting Assistant from Chat

`LMA ask <question>` (also `@LMA ask <question>`, `LMA, ask: <question>`) sends the question to the deployment's Strands meeting assistant — the same agent the LMA meeting page's assistant and the voice assistant's lookups use — with the meeting's transcript as context, and posts the answer in the meeting chat. `@LMA <question>` works too when the text after `@LMA` is not one of the other commands, so `@LMA help` is still help and `@LMA translate` still changes mode. A message that starts with `LMA` but has neither the `@` nor the word `ask` is not treated as a question.

- **Availability.** It needs a Strands meeting assistant (**MeetingAssistService** set to one of the `STRANDS_BEDROCK` options); it does not need a voice assistant. `LMA help` lists `LMA ask` only when one is configured. Without one, an explicit `LMA ask` is answered with a note saying so and `@LMA` asides are ignored. **For operators:** on a Strands deployment any participant, including a guest, can ask, and each question is a meeting-assistant (Bedrock) request billed like any other. There is no separate switch to turn chat questions off; open an issue if your deployment needs one.
- **What the agent can use.** A chat sender is known to the VP only by the display name the meeting shows, so a chat question is treated as any participant's question. Because the answer is read by everyone in the meeting, it is answered from the current meeting's transcript and web search (where configured) only. The document knowledge base, the owner's past meetings, MCP servers, and the VNC and browser-control tools are not offered for chat questions; ask through the LMA meeting page for answers that use them.
- **How answers are posted.** The answer is reduced to plain text (markdown removed, links reduced to their text) on a single line, starting `Live Meeting Assistant:`. Long answers are split at sentence ends into at most three messages, numbered `(1/3)`, `(2/3)`, ..., and the rest is cut with an ellipsis. LMA keeps each message to 1,000 characters on Zoom and Teams and 500 on Webex and Chime, which post by typing; these are LMA's own conservative limits, not the platforms' maximums.
- **Limits.** Each participant may ask once every 20 seconds (told once if they ask sooner), questions over 500 characters are declined, and one question is answered at a time. On the Teams web client the sender is not visible, so everyone there shares one 20-second limit. Answers usually take a few seconds and can take longer when the agent searches; other commands are not held up meanwhile. If no answer arrives within 90 seconds the VP says so and takes the next question. While `LMA pause` is in effect questions are not sent to the meeting assistant; an explicit `LMA ask` is told to wait for `LMA start`. An answer that becomes ready while the VP is posting another message (a help or mode reply, say) can interleave with it on any platform; it is most likely on Webex and Chime, which post by typing.
- **The VP never answers itself.** Its own messages all begin `Live Meeting Assistant`, which cannot be read as a question, and messages from the VP's own display name are ignored.
- The Virtual Participant logs each question and answer as `[chat-ask] question ...` and `[chat-ask] answered platform=<platform> chars=<n> messages=<n> ms=<n>`, without the question text. The meeting-assistant function's own log records the request it receives, question included, as it does for every meeting-assistant request.

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
- [MicroVM ASR](microvm-asr.md) -- The on-demand transcription engine and its Virtual Participant switches

