---
title: "Audio Capture App (Native)"
---

# Audio Capture App (Native)

The **Audio Capture App** is a lightweight native application (macOS and Windows)
that streams your **microphone** and your computer's **system (meeting) audio**
directly to LMA. Because it captures the operating system's audio — not a browser
tab — it can transcribe meetings you join from a **native desktop app** (Zoom,
Microsoft Teams, Cisco Webex, Slack huddles, phone bridges, …), which the
[Chrome Extension](browser-extension.md) and [Stream Audio](stream-audio.md)
options cannot. It adds **no bot** or extra attendee to the meeting.

> **Status:** macOS and Windows are available today; iOS/Android are under
> consideration (see [Roadmap](#roadmap)). The app is distributed as source that
> you build locally with a one-step script — a native app using ScreenCaptureKit
> (macOS) or WASAPI/WPF (Windows) cannot be cross-compiled by LMA's Linux build
> pipeline, and code-signing tools are OS-specific, so building on your own
> machine is both required and the most trustworthy option.

## How it works

- Your **microphone** is transcribed as the meeting owner — the **"My Mic"**
  channel (the AGENT channel downstream).
- Your computer's **system audio** — the remote participants — is the
  **"Meeting Audio"** channel (the CALLER channel downstream).
- Audio streams to the **same** secure WebSocket transcriber endpoint the
  browser options use; the server is unchanged. From there it flows through the
  identical pipeline (real-time transcription, Meeting Assistant, summaries,
  knowledge base).
- **macOS** captures system audio via **ScreenCaptureKit** (loopback) and the
  mic via **AVAudioEngine**. **Windows** captures system audio via **WASAPI
  loopback** and the mic via **WASAPI capture**. On both, the two mono sources
  are interleaved into 2-channel 16-bit PCM.

> On **Windows**, system-audio (loopback) capture is built into the OS and needs
> **no special permission** — there is no equivalent of the macOS Screen
> Recording prompt. The only OS gate is the microphone privacy setting.

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
4. In Terminal, `cd` into the unzipped folder and run the installer **with
   `bash`** (not `./`):
   ```bash
   bash install-macos.sh
   ```
   It clears the macOS download quarantine, checks prerequisites, builds the
   app, and installs it to `/Applications/LMAAudioClient.app`. (Terminal is only
   used to build and install — you won't run the app from Terminal.)

   > ⚠️ **Use `bash install-macos.sh`, not `./install-macos.sh`.** On recent
   > macOS, running a freshly downloaded script directly makes the kernel
   > `execve()` a quarantined file, which Gatekeeper blocks with *"Apple could
   > not verify … is free of malware"* **before the script runs** — so its own
   > quarantine-clearing never happens. Running it via `bash` reads the script as
   > data (no `execve` on a quarantined file), so it runs and clears the flag
   > from the rest of the folder itself.
5. **Launch it like a normal app.** Press **⌘-Space** (Spotlight), type **LMA
   Audio Client** and press Return — or double-click it in Finder. An **LMA**
   item appears in the menu bar (top-right).

   > ⚠️ **Don't launch it from Terminal.** Always launch via Spotlight, Finder,
   > or `open -a "LMA Audio Client"` — never the binary inside `Contents/MacOS`.
   > Only launching through macOS gives the app its own privacy identity; running
   > it from Terminal makes macOS attribute Microphone / Screen Recording to
   > **Terminal**, and system-audio capture silently won't work.
6. Approve the **Microphone** prompt. Then open **System Settings ▸ Privacy &
   Security ▸ Screen Recording**, enable **LMA Audio Client**, and **quit and
   relaunch** it (Screen Recording requires a restart to take effect). Screen
   Recording is what lets macOS capture system/meeting audio, even for audio-only
   capture.
7. Left-click the **LMA** menu-bar item, sign in with your LMA username and
   password, and click **Start**. Your meeting appears in the
   [Meetings List](web-ui-guide.md) with a live transcript.

> **Tip: use headphones.** Otherwise your speakers' meeting audio can bleed into
> your microphone and appear faintly on both transcript channels.

### "Apple could not verify… is free of malware" (Gatekeeper)

macOS flags files downloaded from a browser, and on recent versions it **blocks
running a downloaded script directly** (`./install-macos.sh`) — the kernel
`execve()`s a quarantined file, Gatekeeper kills it, and the script's own
quarantine-clearing never gets to run. Run it via `bash` instead, which isn't
blocked:

```bash
bash install-macos.sh
```

To clear the whole folder up front instead (then either form works):

```bash
xattr -dr com.apple.quarantine .
./install-macos.sh
```

## Download and install (Windows)

Windows is **simpler than macOS**: loopback (system) audio capture is built into
the OS, so there's no Screen-Recording permission or download-quarantine dance —
only the microphone privacy toggle.

1. In the LMA web app, open **Meeting Assistant ▸ Sources ▸ Audio Capture App
   (Native)**, choose **Windows**, and click **Download for Windows**. The zip is
   **preconfigured for your deployment** (endpoint + Cognito settings baked in),
   so you only sign in with your normal LMA username and password.
2. Unzip it (right-click ▸ **Extract All**).
3. Install the **.NET 8 SDK** once from
   [dotnet.microsoft.com](https://dotnet.microsoft.com/download/dotnet/8.0). No
   admin is required — it can install into your user folder.
4. In **PowerShell**, `cd` into the unzipped folder and run the
   build-and-install script:
   ```powershell
   ./build-windows.ps1 -SelfContained -Install
   ```
   It builds a standalone app, runs a built-in self-test, installs it to
   `%LOCALAPPDATA%\Programs\LMA Audio Capture` (no admin needed), and adds a
   **Start Menu** shortcut — so you don't have to dig into the build output.
   - Omit `-Install` to just build (the exe lands in
     `bin\Release\net8.0-windows\win-x64\publish\`).
   - Add `-DesktopShortcut` for a desktop icon too.
   - Add `-ProgramFiles` to install machine-wide under `%ProgramFiles%` instead
     (prompts for admin to do the copy).
5. **Launch it from the Start Menu:** press the **Windows key**, type **LMA Audio
   Capture**, and press Enter. An **LMA** icon appears in the **system tray**
   (bottom-right notification area). If SmartScreen warns about an unrecognized
   app, choose **More info ▸ Run anyway** — expected for a locally built,
   unsigned app.
6. If Windows blocks microphone access, enable it in **Settings ▸ Privacy &
   security ▸ Microphone** (turn on *Microphone access* and *Let desktop apps
   access your microphone*), then restart the app. **System/meeting audio needs
   no permission.**
7. Left-click the **LMA** tray icon, sign in with your LMA username and password,
   and click **Start**. Your meeting appears in the
   [Meetings List](web-ui-guide.md) with a live transcript.

> **Tip: use headphones.** Otherwise your speakers' meeting audio can bleed into
> your microphone and appear faintly on both transcript channels.

### "Windows protected your PC" (SmartScreen)

Expected for a locally built, unsigned app. Click **More info**, then **Run
anyway**. (A future release may ship an Authenticode-signed build to avoid this.)

## Using the menu-bar app (macOS)

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
  The installer already placed the app in `/Applications`, so this works out of
  the box. You can also manage it in **System Settings ▸ General ▸ Login Items**.

### Running it in the background

The app uses no audio or CPU when idle, so the intended usage is to leave it
running in the menu bar and click **Start** when a meeting begins. **To relaunch
after quitting**, press **⌘-Space** (Spotlight), type **LMA Audio Client**, and
press Return — or run `open -a "LMA Audio Client"`.

## Using the system-tray app (Windows)

Launched with no arguments (the normal case), the app runs as a **system-tray
app** (no taskbar button when idle). An **LMA** icon appears in the notification
area (bottom-right).

- **Left-click** the icon for the controls panel: sign in / out,
  **Start** / **Stop** / **Pause**, **mute mic**, **mute system audio**, live
  per-channel **level meters**, and **Open in LMA** (jumps to the live meeting in
  your browser).
- While recording, the icon turns **red** so it's obvious at a glance.
- **Right-click** the icon for **Quit** (kept out of the panel so it isn't
  confused with *Stop*).

Panel options:

- **Remember my email** — prefills your login next launch (email only; the
  password is never stored).
- **Start automatically at login** — adds a per-user startup entry that launches
  the tray app when you sign in; the toggle reflects the real system state.

The app uses no audio or CPU when idle, so the intended usage is to leave it in
the tray and click **Start** when a meeting begins.

> **Developer / headless mode.** Any command-line flag runs a headless CLI that
> streams to stdout with a live VU meter (`--cli` forces it). `--selftest`
> validates the login crypto offline, and `--capture-test <seconds> <out.wav>`
> records the exact streamed stereo PCM with no server — useful for confirming
> **ch0/Left = system, ch1/Right = mic** by measuring per-channel RMS.

## Audio Capture App vs Virtual Participant

Both transcribe meetings without a browser tab, but make different trade-offs.
See the [Meeting Sources overview](meeting-sources.md) for the full comparison
of all capture options.

| Dimension                   | Audio Capture App                                        | [Virtual Participant](virtual-participant.md)          |
| --------------------------- | -------------------------------------------------------- | ------------------------------------------------------ |
| How it captures             | Runs on your computer; captures OS system audio + mic locally | Headless bot joins the meeting in the cloud       |
| Meeting platforms           | Any native or web app that plays audio on your computer  | Only platforms it can automate (Zoom, Teams, Chime, Webex, Meet) |
| Speaker identification      | ❌ No per-speaker names (one "Meeting Audio" channel)    | ✅ Active-speaker names from the meeting platform      |
| In-meeting voice assistant  | ❌ Web-UI chat assistant only                            | ✅ Optional Nova Sonic voice assistant in the meeting  |
| Visible to others           | ✅ No bot / extra attendee                               | ❌ Visible bot joins the meeting                       |
| Who must be present         | You, with the app running on your computer               | ✅ Runs unattended in the cloud                        |
| Video / screen recording    | ❌ Audio only                                            | ✅ Can also record the meeting screen/video            |

**In short:** choose the **Audio Capture App** when you're attending yourself,
want no visible bot, or need a platform the Virtual Participant doesn't support.
Choose the **Virtual Participant** when you need per-speaker names, an in-meeting
voice assistant, screen/video capture, or hands-off unattended recording.

## Roadmap

| Platform        | Status              | Capture technology                     |
| --------------- | ------------------- | -------------------------------------- |
| macOS 13+       | **Available**       | ScreenCaptureKit loopback + AVAudioEngine mic |
| Windows 10/11   | **Available**       | WASAPI loopback + WASAPI mic capture   |
| iPhone / iPad   | Under consideration | ReplayKit / broadcast upload           |
| Android         | Under consideration | AudioPlaybackCapture API               |

## Troubleshooting

### macOS

- **No remote-participant audio in the transcript.** Grant **Screen Recording**
  to "LMA Audio Client" in System Settings and relaunch. Audio-only capture
  still requires the Screen Recording permission on macOS.
- **Build errors / `xcode-select: command not found`.** Install Apple's
  command-line tools (`xcode-select --install`), complete the popup, and re-run
  `bash install-macos.sh`.

### Windows

- **"Windows protected your PC" (SmartScreen).** Expected for a locally built,
  unsigned app. Click **More info**, then **Run anyway**.
- **No microphone / "access denied".** Enable **Settings ▸ Privacy & security ▸
  Microphone** (both *Microphone access* and *Let desktop apps access your
  microphone*), then restart the app. System audio is unaffected.
- **`dotnet` is not recognized / build errors.** Install the
  [.NET 8 SDK](https://dotnet.microsoft.com/download/dotnet/8.0), open a fresh
  PowerShell window, and re-run `./build-windows.ps1 -SelfContained -Install`.
- **No remote-participant audio.** Make sure meeting audio is playing through
  your default playback device (the app captures the default render endpoint).
  Switching the default device mid-meeting is handled automatically.
- **Uninstall.** From the unzipped folder, run `./build-windows.ps1 -Uninstall`.
  It removes the installed app and its Start Menu / Desktop shortcuts and clears
  the app's per-user settings (remembered email, start-at-login). If you
  installed machine-wide with `-ProgramFiles`, run the uninstall from an elevated
  (admin) PowerShell.

### Both platforms

- **Sign-in fails.** Use the same email and password you use for the LMA web
  app. If your organization uses SSO, this app's username/password sign-in may
  not apply — use the [Chrome Extension](browser-extension.md) instead.

## See also

- [Meeting Sources overview](meeting-sources.md) — compare all capture options
- [Chrome Extension](browser-extension.md)
- [Virtual Participant](virtual-participant.md)
