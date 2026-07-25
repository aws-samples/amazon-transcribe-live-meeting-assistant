---
title: "Meeting Sources"
---

# Meeting Sources — Choosing a Capture Option

LMA can capture meeting audio for transcription and analysis in several ways.
This page compares the options and helps you pick the one that best fits your
scenario. All feed into the same downstream pipeline (real-time transcription,
speaker attribution, Meeting Assistant, summaries, knowledge base, etc.).

- **[Chrome Extension](browser-extension.md)** — browser extension that runs
  inside the tab where your meeting is loaded.
- **[Stream Audio (from Mic+Browser)](stream-audio.md)** — built-in page in the
  LMA web app that streams audio from your microphone and any Chrome tab
  (meetings, softphones, YouTube, audio/video playback).
- **[Audio Capture App (Native)](audio-capture-app.md)** — native macOS and
  Windows app (menu bar / system tray) that captures your system (meeting) audio
  + microphone, for meetings you join from a **native desktop app** — no browser
  tab and no bot.
- **[Virtual Participant](virtual-participant.md)** — headless Chrome bot that
  joins the meeting as a separate participant.

## At-a-glance comparison

| Capability                                              | Chrome Extension       | Stream Audio (from Mic+Browser) | Audio Capture App (Native) | Virtual Participant |
| ------------------------------------------------------- | ---------------------- | ------------------------------- | -------------------------- | ------------------- |
| One-time install required                               | Yes (Chromium only)    | No                              | Yes (build on your computer) | No                |
| Runs inside meeting tab                                 | ✅                     | ❌ (separate LMA tab)           | ❌ (menu-bar / tray app)   | ❌ (joins as a bot) |
| Captures both sides of audio                            | ✅                     | ✅                              | ✅                         | ✅                  |
| Speaker attribution from meeting platform               | ✅                     | ❌ (no meeting-app metadata)    | ❌ (no meeting-app metadata) | ✅                |
| Invisible to other attendees                            | ✅                     | ✅                              | ✅                         | ❌ (visible bot)    |
| Works with native desktop/mobile meeting apps           | ❌ (must join from Chrome) | ❌ (must join from Chrome)   | ✅ (macOS/Windows desktop apps) | ✅             |
| Captures any Chrome tab audio (softphone, YouTube…)     | ❌                     | ✅                              | ✅ (any system audio)      | ❌                  |
| Can attend meetings without you (unattended / overnight)| ❌                     | ❌                              | ❌                         | ✅                  |
| Supports scheduling for future meetings                 | ❌                     | ❌                              | ❌                         | ✅                  |
| Supports the Voice Assistant                            | ❌                     | ❌                              | ❌ (web-UI chat only)      | ✅                  |
| Supports "Open VP live view" in the Meeting Assistant   | ❌                     | ❌                              | ❌                         | ✅                  |
| Works on any modern browser                             | ❌ (Chromium only)     | ❌ (Chrome only)                | N/A (native app; macOS/Windows) | N/A (server-side) |

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

### Use the **Audio Capture App (Native)** when…

- You join meetings from a **native desktop app** on macOS or Windows (Zoom,
  Teams, Webex, Slack, a phone bridge, …) rather than a Chrome tab — which rules
  out both the Chrome Extension and Stream Audio, and you don't want a visible bot.
- You're attending the meeting yourself and want an unobtrusive menu-bar / tray
  app (start/stop, pause, mute mic/system, live meters) with no extra attendee.
- You're fine with no per-speaker names (like Stream Audio, it only distinguishes
  your microphone from the system/meeting-audio channel) and don't need the
  in-meeting Voice Assistant.
- macOS and Windows are available today; mobile is on the roadmap. See
  [Audio Capture App](audio-capture-app.md).

### Use **Virtual Participant** when…

- Attendees will be on native desktop or mobile meeting apps (not the web
  client), and you want it to run **unattended** (you can't or don't want to
  attend) — schedule it overnight, or let it stay after you leave.
- You want per-speaker attribution from the meeting platform.
- You want the Voice Assistant to participate in the meeting (wake phrase,
  push-to-talk, continuous mode).
- You want to use **Open VP live view** in the Meeting Assistant to see what
  the bot is seeing in the meeting.

## See also

- [Chrome Extension](browser-extension.md) — install and usage guide
- [Stream Audio](stream-audio.md) — browser-based capture
- [Audio Capture App (Native)](audio-capture-app.md) — native macOS / Windows app
- [Virtual Participant](virtual-participant.md) — server-side bot
