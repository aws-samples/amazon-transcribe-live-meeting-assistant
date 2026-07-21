---
title: "Audio Capture App (Native)"
---

# Audio Capture App (Native macOS)

The **Audio Capture App** is a lightweight native macOS application that streams
your **microphone** and your computer's **system (meeting) audio** directly to
LMA. Because it captures the operating system's audio — not a browser tab — it
can transcribe meetings you join from a **native desktop app** (Zoom, Microsoft
Teams, Cisco Webex, Slack huddles, phone bridges, …), which the
[Chrome Extension](browser-extension.md) and [Stream Audio](stream-audio.md)
options cannot. It adds **no bot** or extra attendee to the meeting.

> **Status:** macOS is available today. Windows and iOS/Android are on the
> roadmap (see [Roadmap](#roadmap)). The app is distributed as source that you
> build locally with a one-step installer — a native macOS app using
> ScreenCaptureKit cannot be cross-compiled by LMA's Linux build pipeline, and
> Apple's signing tools are macOS-only, so building on your own Mac is both
> required and the most trustworthy option.

## How it works

- Your **microphone** is transcribed as the meeting owner — the **"My Mic"**
  channel (the AGENT channel downstream).
- Your computer's **system audio** — the remote participants — is the
  **"Meeting Audio"** channel (the CALLER channel downstream).
- Audio streams to the **same** secure WebSocket transcriber endpoint the
  browser options use; the server is unchanged. From there it flows through the
  identical pipeline (real-time transcription, Meeting Assistant, summaries,
  knowledge base).
- macOS captures system audio via **ScreenCaptureKit** (loopback) and the mic
  via **AVAudioEngine**; the two are interleaved into 2-channel 16-bit PCM.

## Download and install (macOS)

1. In the LMA web app, open **Meeting Assistant ▸ Sources ▸ Audio Capture App
   (Native)** and click **Download for macOS**. The zip is **preconfigured for
   your deployment** (endpoint + Cognito settings baked in), so you only sign in
   with your normal LMA username and password.
2. Unzip it.
3. If you don't already have Apple's command-line tools, install them once:
   ```bash
   xcode-select --install
   ```
4. In Terminal, `cd` into the unzipped folder and run the installer:
   ```bash
   ./install-macos.sh
   ```
   It clears the macOS download quarantine, checks prerequisites, builds the
   app, and bundles it as `LMAAudioClient.app`.
5. Launch it. macOS prompts for **Microphone** access — approve it. Then open
   **System Settings ▸ Privacy & Security ▸ Screen Recording**, enable **LMA
   Audio Client**, and **relaunch** (Screen Recording requires a restart to take
   effect). Screen Recording is what lets macOS capture system/meeting audio,
   even for audio-only capture.
6. Sign in with your LMA username and password. Click **Start**, and your
   meeting appears in the [Meetings List](web-ui-guide.md) with a live
   transcript.

> **Tip: use headphones.** Otherwise your speakers' meeting audio can bleed into
> your microphone and appear faintly on both transcript channels.

### "Apple could not verify… is free of malware" (Gatekeeper)

macOS flags files downloaded from a browser. The installer clears this
automatically, but if you hit the warning before running it, clear the quarantine
flag from the unzipped folder and re-run:

```bash
xattr -dr com.apple.quarantine .
./install-macos.sh
```

## Using the menu-bar app

Launched with no arguments (the normal case), the app runs as a **menu-bar app**
(no Dock icon). An **LMA** item appears at the top-right of the menu bar.

- **Left-click** the item for the controls popover: sign in / out,
  **Start** / **Stop** / **Pause**, **mute mic**, **mute system audio**, live
  per-channel **level meters**, and **Open in LMA** (jumps to the live meeting in
  your browser).
- While recording, the icon turns **red** so it's obvious at a glance.
- **Right-click** the item for **Quit** (kept out of the popover so it isn't
  confused with *Stop*).

Popover options:

- **Remember my email** — prefills your login next launch (email only; the
  password is never stored).
- **Start automatically at login** — registers the app as a macOS login item.
  For this to work the app must live in a stable location, so move it to
  `/Applications` first:
  ```bash
  cp -R build/LMAAudioClient.app /Applications/
  open /Applications/LMAAudioClient.app
  ```
  You can also manage it in **System Settings ▸ General ▸ Login Items**.

### Running it in the background

The app uses no audio or CPU when idle, so the intended usage is to leave it
running in the menu bar and click **Start** when a meeting begins. **To relaunch
after quitting**, press **⌘-Space** (Spotlight), type **LMA Audio Client**, and
press Return — or run `open -a LMAAudioClient`.

## Audio Capture App vs Virtual Participant

Both transcribe meetings without a browser tab, but make different trade-offs.
See the [Meeting Sources overview](meeting-sources.md) for the full comparison
of all capture options.

| Dimension                   | Audio Capture App                                        | [Virtual Participant](virtual-participant.md)          |
| --------------------------- | -------------------------------------------------------- | ------------------------------------------------------ |
| How it captures             | Runs on your Mac; captures OS system audio + mic locally | Headless bot joins the meeting in the cloud            |
| Meeting platforms           | Any native or web app that plays audio on your Mac       | Only platforms it can automate (Zoom, Teams, Chime, Webex, Meet) |
| Speaker identification      | ❌ No per-speaker names (one "Meeting Audio" channel)    | ✅ Active-speaker names from the meeting platform      |
| In-meeting voice assistant  | ❌ Web-UI chat assistant only                            | ✅ Optional Nova Sonic voice assistant in the meeting  |
| Visible to others           | ✅ No bot / extra attendee                               | ❌ Visible bot joins the meeting                       |
| Who must be present         | You, with the app running on your Mac                    | ✅ Runs unattended in the cloud                        |
| Video / screen recording    | ❌ Audio only                                            | ✅ Can also record the meeting screen/video            |

**In short:** choose the **Audio Capture App** when you're attending yourself,
want no visible bot, or need a platform the Virtual Participant doesn't support.
Choose the **Virtual Participant** when you need per-speaker names, an in-meeting
voice assistant, screen/video capture, or hands-off unattended recording.

## Roadmap

| Platform        | Status              | Capture technology                     |
| --------------- | ------------------- | -------------------------------------- |
| macOS 13+       | **Available**       | ScreenCaptureKit loopback + AVAudioEngine mic |
| Windows         | Planned             | WASAPI loopback                        |
| iPhone / iPad   | Under consideration | ReplayKit / broadcast upload           |
| Android         | Under consideration | AudioPlaybackCapture API               |

## Troubleshooting

- **No remote-participant audio in the transcript.** Grant **Screen Recording**
  to "LMA Audio Client" in System Settings and relaunch. Audio-only capture
  still requires the Screen Recording permission on macOS.
- **Sign-in fails.** Use the same email and password you use for the LMA web
  app. If your organization uses SSO, this app's username/password sign-in may
  not apply — use the [Chrome Extension](browser-extension.md) instead.
- **Build errors / `xcode-select: command not found`.** Install Apple's
  command-line tools (`xcode-select --install`), complete the popup, and re-run
  `./install-macos.sh`.

## See also

- [Meeting Sources overview](meeting-sources.md) — compare all capture options
- [Chrome Extension](browser-extension.md)
- [Virtual Participant](virtual-participant.md)
