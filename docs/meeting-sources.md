---
title: "Meeting Sources"
---

# Meeting Sources — Choosing a Capture Option

LMA can capture meeting audio for transcription and analysis in three ways. This
page compares the options and helps you pick the one that best fits your
scenario. All three feed into the same downstream pipeline (real-time
transcription, speaker attribution, Meeting Assistant, summaries, knowledge
base, etc.).

- **[Chrome Extension](browser-extension.md)** — browser extension that runs
  inside the tab where your meeting is loaded.
- **[Stream Audio (from Mic+Browser)](stream-audio.md)** — built-in page in the
  LMA web app that streams audio from your microphone and any Chrome tab
  (meetings, softphones, YouTube, audio/video playback).
- **[Virtual Participant](virtual-participant.md)** — headless Chrome bot that
  joins the meeting as a separate participant.

## At-a-glance comparison

| Capability                                              | Chrome Extension       | Stream Audio (from Mic+Browser) | Virtual Participant |
| ------------------------------------------------------- | ---------------------- | ------------------------------- | ------------------- |
| One-time install required                               | Yes (Chromium only)    | No                              | No                  |
| Runs inside meeting tab                                 | ✅                     | ❌ (separate LMA tab)           | ❌ (joins as a bot) |
| Captures both sides of audio                            | ✅                     | ✅                              | ✅                  |
| Speaker attribution from meeting platform               | ✅                     | ❌ (no meeting-app metadata)    | ✅                  |
| Invisible to other attendees                            | ✅                     | ✅                              | ❌ (visible bot)    |
| Works with native desktop/mobile meeting apps           | ❌ (must join from Chrome) | ❌ (must join from Chrome)   | ✅                  |
| Captures any Chrome tab audio (softphone, YouTube…)     | ❌                     | ✅                              | ❌                  |
| Can attend meetings without you (unattended / overnight)| ❌                     | ❌                              | ✅                  |
| Supports scheduling for future meetings                 | ❌                     | ❌                              | ✅                  |
| Supports the Voice Assistant                            | ❌                     | ❌                              | ✅                  |
| Supports "Open VP live view" in the Meeting Assistant   | ❌                     | ❌                              | ✅                  |
| Works on any modern browser                             | ❌ (Chromium only)     | ❌ (Chrome only)                | N/A (server-side)   |

## When to use each

### Use the **Chrome Extension** when…

- You join meetings from the meeting platform's web client in Chrome.
- You want to start/stop transcription with a single click from inside the
  meeting tab — no separate LMA tab to switch to.
- You want speaker attribution derived from the meeting platform's own
  attendee metadata.
- You don't want a bot visible to other attendees.

### Use **Stream Audio (from Mic+Browser)** when…

- You don't want to install anything.
- You want to capture audio from something other than a conventional meeting —
  a softphone, a YouTube video, a recorded audio/video file playing in a tab,
  etc.
- You're fine with no speaker attribution (Stream Audio has no access to
  meeting-app metadata; it only distinguishes your microphone channel from the
  tab-audio channel).

### Use **Virtual Participant** when…

- Attendees will be on native desktop or mobile meeting apps (not the web
  client) — which rules out both the Chrome Extension and Stream Audio.
- You can't (or don't want to) attend in person — e.g. schedule the bot to
  join a meeting overnight, or let it stay after you leave.
- You want the Voice Assistant to participate in the meeting (wake phrase,
  push-to-talk, continuous mode).
- You want to use **Open VP live view** in the Meeting Assistant to see what
  the bot is seeing in the meeting.

## See also

- [Chrome Extension](browser-extension.md) — install and usage guide
- [Stream Audio](stream-audio.md) — browser-based capture
- [Virtual Participant](virtual-participant.md) — server-side bot
