---
title: "Blog Update Proposal — Native Desktop Capture Apps"
---

# Blog Update Proposal — Adding the Native Desktop Capture Apps

**Target post:** [Live Meeting Assistant with Amazon Transcribe, Amazon Bedrock, and Strands Agents](https://aws.amazon.com/blogs/machine-learning/live-meeting-assistant-with-amazon-transcribe-amazon-bedrock-and-strands-agents/)

**Purpose:** the published post is built around **four** capture paths — Virtual
Participant, Stream Audio, Browser Extension, and Upload Audio. The new native
**Desktop Capture App** (macOS and Windows) is not mentioned anywhere. This
document proposes the minimum set of edits to make readers aware of it, with
drop-in prose for each location.

**Editorial principle:** keep the blog prose short. Every section below defers
detail to the docs — [Desktop Capture App](desktop-capture-app.md) and
[Meeting Sources](meeting-sources.md).

---

## 1. Update note at the top — one clause

The `Update — May 2026 (v0.3.4)` line should name the feature, since that's what
readers skim.

> …and adds the new **Desktop Capture App** — a native macOS and Windows app that
> streams your system audio and microphone to LMA, so you can transcribe
> meetings you join from a native desktop client.

## 2. "What's New in LMA v0.3" — add one bullet

Insert after the Virtual Participant / avatar bullets:

> - **Desktop Capture App (native macOS and Windows)** — a menu-bar/system-tray
>   app that captures your computer's system audio plus your microphone and
>   streams them to LMA. Transcribe meetings held in native Zoom, Teams, Webex,
>   or Slack clients (or a phone bridge) with no browser tab and no bot in the
>   meeting.

## 3. Solution Overview — "four ways" becomes five

The overview enumerates four ways to connect LMA to a meeting. Change the count
and add this item between *Browser Extension* and *Upload Audio*:

> 4. **Desktop Capture App (native)** — a local macOS or Windows app that captures
>    OS-level system audio and your mic, for meetings you attend from a native
>    desktop client.

## 4. Key Features — new subsection right after **Virtual Participant**

Placing it beside Virtual Participant is deliberate: they are the two "no browser
tab" options, and readers need the contrast immediately.

> ### Desktop Capture App
>
> A lightweight native app for **macOS and Windows** that runs in the menu bar or
> system tray. It captures your computer's **system (meeting) audio** on one
> channel and your **microphone** on the other, interleaves them, and streams to
> the same secure WebSocket transcriber the browser options use — so everything
> downstream (live transcription, translation, Meeting Assistant, summaries) is
> identical. Because it captures at the OS level rather than from a browser tab,
> it works with any meeting you can hear on your computer: native Zoom, Teams,
> Webex, or Slack clients, a softphone, or a dial-in bridge. Controls include
> start/stop, pause, mute mic, mute system audio, live per-channel level meters,
> and start-at-login.
>
> **Choose it when** you're attending the meeting yourself, don't want a visible
> bot or extra attendee, and join from a desktop app. **Choose the Virtual
> Participant instead** when you need per-speaker names, the in-meeting voice
> assistant or avatar, screen/video recording, or unattended and scheduled
> attendance. On macOS the app needs the Screen Recording permission (that's how
> macOS grants system-audio capture, even audio-only); on Windows loopback
> capture needs no permission at all. See
> [Desktop Capture App](https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant/desktop-capture-app/)
> for install steps and the full comparison.

## 5. Getting Started ▸ Other Ways to Capture Meeting Audio — new subsection

This is the most important edit — it's where readers go to pick a method. Add
**after** "Use the Browser Extension" and **before** "Upload Audio", matching the
existing ~250-word, numbered-steps format.

> ### Use the Desktop Capture App (macOS or Windows)
>
> If you join meetings from a native desktop client instead of a browser tab, the
> Desktop Capture App streams your system audio and microphone straight from your
> computer — no tab sharing and no bot.
>
> 1. In the LMA web app, open **Meeting Assistant ▸ Sources ▸ Desktop Capture App
>    (Native)** and download the build for your platform. The download is
>    preconfigured for your deployment, so you sign in with your normal LMA
>    username and password.
> 2. Unzip it and run the one-step installer: `bash install-macos.sh` on macOS
>    (requires Xcode command-line tools), or
>    `./build-windows.ps1 -SelfContained -Install` in PowerShell on Windows
>    (requires the .NET 8 SDK). The app is distributed as source you build
>    locally — a native app using ScreenCaptureKit or WASAPI can't be produced by
>    LMA's Linux build pipeline, and code-signing tooling is OS-specific.
> 3. Launch it, approve the microphone prompt, and on macOS also enable **LMA
>    Audio Client** under **System Settings ▸ Privacy & Security ▸ Screen
>    Recording**, then relaunch.
> 4. Start your meeting in your desktop client, click the **LMA** menu-bar or
>    tray icon, sign in, and choose **Start**. The meeting appears in the
>    meetings list with a live transcript: your speech on one channel, remote
>    participants on the other.
>
> The trade-off versus the Virtual Participant: the app is invisible to other
> attendees and works with any platform your computer can play audio from, but
> you must be present with it running, remote participants arrive as a single
> "Meeting Audio" channel rather than named speakers, and there's no in-meeting
> voice assistant or video recording. Because a locally built app is unsigned,
> macOS Gatekeeper or Windows SmartScreen may warn on first launch. Headphones
> are recommended so speaker audio doesn't bleed into your mic. Full details:
> [Desktop Capture App](https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant/desktop-capture-app/)
> and
> [Meeting Sources](https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant/meeting-sources/).

Also update the section's intro sentence if it says "three other ways".

## 6. Architecture section — one sentence

The post says the WebSocket server relays audio for Stream Audio and the browser
extension. Extend that list rather than adding a paragraph:

> The Desktop Capture App uses the same path: it authenticates with Cognito, then
> streams 2-channel 16-bit PCM (system audio on one channel, microphone on the
> other) to the same WebSocket server, so no server-side changes are needed.

## 7. Cost Assessment — one clause (optional but worth it)

Readers comparing options will care that this path skips the VP container:

> The Desktop Capture App runs on your own computer, so it incurs no LMA compute
> cost beyond the WebSocket transcriber, Transcribe, and Bedrock usage that all
> capture methods share — unlike the Virtual Participant, which runs a Fargate
> task per meeting.

## 8. Conclusion — half a sentence

Add "native macOS and Windows audio capture" to the list of v0.3 capabilities the
conclusion recaps.

---

## Pros and cons summary (for the editor's reference)

Not intended for the post verbatim — the prose above already carries the
trade-offs — but useful when reviewing the edits.

**Pros**

- Works with **native desktop meeting clients** (Zoom, Teams, Webex, Slack
  huddles, phone bridges) that the Chrome Extension and Stream Audio cannot
  reach.
- **No bot, no extra attendee** — invisible to other participants.
- Captures **any** audio playing on the machine, not just a browser tab.
- **No server-side changes** — same WebSocket transcriber, same downstream
  pipeline.
- **No per-meeting cloud compute** (unlike the Virtual Participant's Fargate
  task).
- On **Windows**, loopback capture needs **no OS permission** at all.

**Cons**

- **No per-speaker names** — remote participants land on a single "Meeting Audio"
  channel.
- **You must be present** with the app running; no unattended or scheduled
  attendance.
- **No in-meeting Voice Assistant or avatar**, and **no screen/video recording**.
- **Build-from-source install** (Xcode command-line tools on macOS, .NET 8 SDK on
  Windows) and an **unsigned binary**, so Gatekeeper/SmartScreen warn on first
  launch.
- macOS requires the **Screen Recording** permission and a relaunch, which is
  confusing for audio-only capture.
- Username/password sign-in, so **SSO-only organizations** may need a different
  option.

## Also worth doing

- **Add a screenshot.** The post is image-heavy per feature; the tray/menu-bar
  panel with the two live VU meters is the single most explanatory image for this
  feature, and its absence would make the section read as an afterthought.
- **`docs/INDEX.md`** still describes "the three capture options" in the Meeting
  Sources entry — stale now that there are four. Worth fixing in the same pass,
  since the blog links into that docs site.

## See also

- [Desktop Capture App](desktop-capture-app.md)
- [Meeting Sources](meeting-sources.md)
- [Virtual Participant](virtual-participant.md)
