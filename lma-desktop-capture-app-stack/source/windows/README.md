# LMA Capture Client (Windows)

Streams **microphone + system (loopback) audio** — and, optionally, **screen
video** — from a Windows PC directly to the LMA WebSocket transcriber, so a user
can transcribe a meeting they joined from a **native desktop app** (Zoom / Teams
/ Meet / Slack huddle / phone bridge) — no Chrome tab sharing, no Virtual
Participant bot.

This is the Windows counterpart to the macOS client in
`../macos/`. It speaks the **same wire protocol**, uses the **same Cognito SRP
login**, and shares the same UI-agnostic engine split between a **system-tray
app** and a **headless CLI**. The server is unchanged; this is a third client
alongside the browser and the Node CLI.

> ✅ **Verified on Windows 11** (audio client):
> - `--selftest` reproduces the pycognito SRP known-answer byte-for-byte (incl.
>   the 3072-bit `g^x mod N` modpow).
> - `--capture-test` confirmed **ch0/Left = system audio, ch1/Right = mic**,
>   aligned and not swapped, by measuring per-channel RMS on the streamed PCM.
> - Tray GUI and headless CLI both launch cleanly.
> - The recording-time taskbar button was verified against the shell's own UI
>   Automation tree: absent when idle, present while recording/paused, gone after
>   Stop; closing its window leaves the recording running.
>
> ✅ **Type-checked, including the WPF UI:** `./tools/compile-check.sh` compiles
> every file under `Engine/` and `App/` against the real NuGet packages
> (ScreenRecorderLib 6.2.0, NAudio, WPF) using the .NET SDK in Docker — so the
> ScreenRecorderLib API surface and all the newer code are confirmed to build.
> Run it after any change to this client.
>
> ⚠️ **Not yet RUN on Windows:** optional screen-video capture (ScreenRecorderLib
> + the video WebSocket), the rename to LMACaptureClient with per-stack
> identifiers, the window/layout fixes, and the newer UX (elapsed timer,
> notifications, recent meeting names, separate Settings window, capture-inputs
> summary, source picker with resolutions). These were written on macOS, so while
> they compile, their runtime behaviour still needs a smoke test on a Windows
> machine.

---

## Windows platform notes

- **System (loopback) audio needs no special permission.** WASAPI loopback on
  the default render endpoint is built into Windows — no equivalent of macOS's
  "Screen Recording" TCC prompt and relaunch dance, so the app can capture
  whatever is playing (a native Zoom/Teams client) with zero permission
  friction. Screen-video capture likewise needs no permission grant.
- **Microphone** is the only OS gate: Windows 10/11 has a
  **Settings ▸ Privacy & security ▸ Microphone** toggle (and a separate "Let
  desktop apps access your microphone"). If mic capture is denied, the app logs
  a clear pointer to that page and keeps streaming system audio.

## The wire protocol (unchanged from the browser / macOS client)

1. `wss://<cloudfront-domain>/api/v1/ws` — **auth is via query params**, not an
   `Authorization` header. The transcriber sits behind CloudFront whose
   `OriginRequestPolicy` header whitelist does **not** include `Authorization`,
   so a header is stripped → 401. We authenticate the browser's way:
   `?authorization=Bearer%20<ACCESS_TOKEN>&id_token=<ID_TOKEN>`. (`refresh_token`
   is deliberately **not** sent: the server has no consumer for it, and a real
   one in the query string would persist a long-lived credential into
   CloudFront/ALB access logs. The param is optional server-side.)
   (The same values are also set as headers — harmless, and lets the client work
   against the origin/ALB directly in future. **Do not "fix" back to header-only
   auth.**)
2. One JSON text frame (START): `{callId, agentId, fromNumber, toNumber,
   samplingRate, callEvent:"START"}`.
3. Raw **16-bit LE PCM, 2-channel interleaved**, ~100 ms binary frames.
   **ch0 = meeting/system audio (→ CALLER), ch1 = mic (→ AGENT).** Interleave
   starts at index 0 (we do **not** replicate the browser worklet's off-by-one
   that drops the first mic sample).
4. JSON `{..., callEvent:"END"}`, then close.

`float→int16` clamp matches the browser/macOS: clamp to [-1,1], then
`s < 0 ? s*32768 : s*32767`.

**Reconnect:** the server has no session resume. On any drop we reopen a new
socket and send a **fresh START** (same callId), with capped exponential backoff
(0.5 s → 10 s). PCM produced while down is buffered (bounded to ~3 s of stereo)
and flushed on reconnect; overflow drops oldest and is counted. If the handshake
keeps failing with an auth-looking rejection (~4 tries) and the token cannot be
refreshed, we warn and stop.

**Token refresh (issue #535):** Cognito access tokens last ~1 h; the server
401s an expired one. `TokenStore` keeps the token alive for the life of the
session — a proactive timer renews it ~5 min before `exp` (re-checked on
`SystemEvents.PowerModeChanged` Resume, so a sleep or hibernate that outlasts
the deadline is caught at wake), `Connect()` renews on demand when a token is
already past that window (covers a Start an hour later), and a rejected
handshake triggers one refresh-and-retry before it counts toward the fatal
threshold. Only when the refresh token itself is rejected is auth fatal: the
**tray app stays alive**, clears the session, and re-shows the sign-in form
with the reason; the headless CLI exits non-zero. A single-flight connect gate
ensures a reconnect timer and a refresh-then-retry can never open two sockets
(and send two STARTs) for the same call. The pure scheduling/classification
logic is pinned by `--selftest`.

## Source layout

| File | Responsibility |
|---|---|
| `Engine/Config.cs` | Config layering: CLI flag → env var → `lma-config.json` → default |
| `Engine/Srp.cs` | Cognito `USER_SRP_AUTH` login + `REFRESH_TOKEN_AUTH` renewal, dependency-free (`System.Numerics.BigInteger` + BCL crypto) |
| `Engine/TokenStore.cs` | Keeps the access token fresh: proactive refresh before `exp`, on-demand at connect, reactive after a 401; single-flight, resume-from-sleep aware (issue #535) |
| `Engine/SelfTest.cs` | Offline known-answer tests: SRP crypto + token-refresh scheduling/classification (`--selftest`) |
| `Engine/StereoMixer.cs` | Buffer 2 mono streams, drain `min(count)` on a 100 ms cadence → interleaved int16, mute/pause, VU meters, debug tee |
| `Engine/TranscriberSocket.cs` | WebSocket START/PCM/END framing, reconnect + fresh START, PCM buffering, handshake-failure detection (`ClientWebSocket`) |
| `Engine/WavTee.cs` | Debug: tee exact streamed PCM to a local WAV |
| `Engine/LinearResampler.cs` | Continuous-phase mono resampler (device rate → 48 kHz) |
| `Engine/AudioCapture.cs` | **WASAPI**: loopback on default render (system/ch0) + capture on default capture (mic/ch1), resample, mono downmix, device-change handling via `IMMNotificationClient` |
| `Engine/CaptureController.cs` | UI-agnostic engine controller: login/start/stop/pause/mute + state machine + callbacks |
| `App/Program.cs` | Entry point; dual-mode dispatch (GUI vs headless CLI) + `--selftest` / `--login-only` / `--capture-test` + taskbar `--lma-*` verb relay |
| `App/TrayApp.cs`, `App/PanelView.cs` | System-tray UI (WPF + Hardcodet.NotifyIcon) and the recording-time taskbar button |
| `App/TaskbarStatus.cs` | Taskbar button images (icon / overlay badges / thumb-button glyphs) + the named-pipe channel that carries JumpList verbs to the running instance |
| `App/AppSettings.cs` | Remember-email (HKCU) + Start-at-login (HKCU `...\Run`) |
| `tools/make-icon-images.ps1` | Regenerates the tray-icon image used in `docs/` (same geometry/colors as `IconFactory.Make`) |

The engine (`Config`, `Srp`, `StereoMixer`, `TranscriberSocket`,
`CaptureController`, `WavTee`) is UI-agnostic, so the CLI and tray share one
engine — the same separation as the macOS app.

---

## Build & run

**Prerequisites:** the [.NET 8 SDK](https://dot.net/download). No admin needed —
the SDK can install into a user directory (e.g. via `dotnet-install.ps1
-InstallDir $HOME\dotnet`).

```powershell
cd lma-desktop-capture-app-stack/source/windows

# Build a standalone app, then INSTALL it (per-user %LOCALAPPDATA%\Programs) and
# add a Start Menu shortcut — recommended, no admin needed:
./build-windows.ps1 -SelfContained -Install

# Just build, no install (exe lands in the publish folder):
./build-windows.ps1 -SelfContained

# Framework-dependent build (smaller; target needs the .NET 8 Desktop Runtime):
./build-windows.ps1
```

`build-windows.ps1` publishes a `win-x64` executable and runs `--selftest` (so a
build whose crypto can't reproduce the known-answer never ships). The executable
and `lma-config.json` land in `bin/<Config>/net8.0-windows/win-x64/publish/`.

Install flags:
- `-Install` — copy to `%LOCALAPPDATA%\Programs\LMA Audio Capture`, add a
  **Start Menu** shortcut (launch via the Windows key → "LMA Audio Capture"), and
  register an "Apps & features" entry. Closes a running instance first so an
  upgrade can't fail on locked files. No admin required.
- `-ProgramFiles` — install machine-wide under `%ProgramFiles%` instead (the copy
  step re-launches elevated to get admin).
- `-DesktopShortcut` — also drop a Desktop shortcut.

> **No taskbar pin — deliberately.** Earlier versions tried a best-effort pin.
> Don't add it back. Two reasons:
> 1. Windows 10+ removed the supported pin API, and the shell verb is **absent on
>    current Win11 builds**, so the attempt essentially always failed. Advertising
>    a pin that doesn't happen just sets false expectations.
> 2. Probing for the verb requires `New-Object -ComObject Shell.Application`,
>    which loads **every in-process shell extension on the machine**. Third-party
>    ones (corporate sync/backup/DLP tools) dump their own diagnostics straight to
>    the console — e.g. repeated `log4net:ERROR ... Failed to create object to set
>    param: lockingModel`. That noise isn't ours (this app has no log4net
>    dependency) and can't be suppressed by ordinary in-process redirection, but
>    it makes a **successful install look broken**.
>
> The app lives in the tray; the docs tell users to pin it themselves from the
> Start Menu if they want it on the taskbar. (Note this is about *pinning* — the
> app does take a taskbar button of its own **while recording**; see
> [Recording-time taskbar button](#recording-time-taskbar-button).)
>
> **Always-visible tray icon.** The app asks Windows to keep its tray icon out of
> the overflow (▲) flyout so the **red recording icon stays visible** while
> recording. If Windows still hides it, drag it onto the taskbar once, or toggle
> it in **Settings ▸ Personalization ▸ Taskbar ▸ Other system tray icons**.

Uninstall — two equivalent ways:

- **Settings ▸ Apps ▸ Installed apps** — the app registers an "Apps & features"
  entry ("LMA Audio Capture"); find it and choose **Uninstall**. (`-Install`
  writes this entry; per-user installs register under HKCU, `-ProgramFiles`
  installs under HKLM.)
- **Script:**
  ```powershell
  ./build-windows.ps1 -Uninstall
  ```

Either way removes the installed app (per-user and, if present, machine-wide)
plus its Start Menu / Desktop shortcuts, the "Apps & features" entry, and the
app's per-user settings (remembered email, start-at-login `Run` entry). If you
installed machine-wide with `-ProgramFiles`, run the uninstall from an elevated
(admin) PowerShell so it can delete the `%ProgramFiles%` copy and the HKLM entry.

You can also build by hand:

```powershell
dotnet publish -c Release -r win-x64 --self-contained true
```

### Run the tray app

Double-click `LMACaptureClient.exe` (or run it with **no arguments**, or `--gui`).
It appears as a **system-tray icon** with no taskbar window when idle (mirrors
the macOS menu-bar `LSUIElement` behavior).

- **Left-click** the tray icon → popup panel (sign in, start/stop/pause, mute
  mic, mute system, live per-channel VU meters, "Open in LMA").
- **Right-click** → context menu with **Quit** (kept out of the panel so it
  can't be confused with *Stop*).
- While recording, the tray icon turns **red**.

**Single instance.** Launching the app again — Start Menu, a pinned taskbar
shortcut, double-clicking the exe — does **not** add a second tray icon; it
opens the running app's UI. The instance is claimed with a per-user named mutex
(`Local\LMACaptureClient.<stack-slug>.instance.<user>`), and the losing process relays
`--lma-panel` over the pipe and exits, so a relaunch behaves like clicking the
tray icon. Two things this must get right, both of which have bitten:

- The check has to be the **mutex**, not "can I connect to the pipe?" — the pipe
  server only starts listening once the UI is built, so a relaunch during startup
  would otherwise conclude nobody was home and open a duplicate.
- Raising the window needs `AllowSetForegroundWindow` in the *relaying* process
  plus `SetForegroundWindow` in the owner (`ForceForeground`). `Window.Activate()`
  alone is a no-op when another process owns the foreground, which is exactly the
  case here — the symptom is a taskbar flash and no visible panel.

The mutex is released by the OS on exit (including a kill or crash), so a
relaunch after a crash correctly starts fresh.

### Recording-time taskbar button

While recording — and **only** while recording — the app also takes a **taskbar
button**. This is the Windows counterpart to the macOS Dock tile, and it exists
for the same reason: the tray icon can end up in the ▲ overflow, and a live
recording needs a way to see and stop it that Windows can't hide.

- The button's icon carries a **red dot overlay** (`TaskbarItemInfo.Overlay`),
  and the progress wash (`ProgressState`) is **green while recording / yellow
  when paused**, so the state reads even at 16×16.
- **Hover** it for the thumbnail toolbar: **Pause/Resume** and **Stop**
  (`ThumbButtonInfos`) — control the recording without opening a window.
- **Right-click** it for the **JumpList**: Start / Pause / Resume / Stop
  Recording and Open Control Panel, matching the macOS Dock menu.
- **Click** it to open the control panel as a regular window (the same
  `PanelView` the tray flyout uses).
- **Closing or minimizing that window never stops the recording** — it
  minimizes to keep the button (and its overlay) on screen. Stopping is always
  an explicit *Stop*.

Implementation notes for anyone touching this:

- The taskbar button belongs to a second `Window` that is only ever *shown*
  while streaming; hiding it is what makes the button disappear. It must be a
  real `Window` (not a `Popup`) for the same reason the flyout is — a `Popup`
  gets no activated top-level HWND, so its text fields can't take keyboard
  focus.
- `PanelView` is a **single instance shared by both windows**. WPF allows an
  element exactly one logical parent, so it is *moved* between them
  (`MovePanelTo`) — never held by both, or you get "already the logical child of
  another element".
- A `JumpTask` can only relaunch the exe with arguments, so each task passes an
  `--lma-*` verb. The new process forwards that verb to the running instance
  over a per-user named pipe and exits (`TrayIpc`); it only starts a GUI itself
  if nobody answers. Without that relay, "Stop Recording" from the taskbar would
  spawn a second process that knows nothing about the live capture.

Panel options:
- **Remember my email** — prefills your login next launch (email only; the
  password is never stored — it stays in memory for the session).
- **Settings (⚙ gear)** — transcript speaker labels for the two channels plus a
  microphone picker, persisted under `HKCU\...\LMACaptureClient`. A blank label
  field means "use the default", and that default is drawn **in grey inside the
  field** (`PlaceholderBox` + `UpdateLabelHints`) so it's visible without
  hovering: the mic label defaults to the signed-in email, the system label to
  "Other participants". WPF has no built-in placeholder, and writing a watermark
  into `Text` would get persisted as a real value, so the hint is a separate
  non-hit-testable `TextBlock` layered over the `TextBox`.
- **Start automatically at login** — toggles an `HKCU\...\Run` entry pointing at
  this exe (`--gui`); the checkbox reflects the real registry state, so it stays
  correct even if changed elsewhere.
- Meeting name gets a `" - yyyy-MM-dd HH:mm"` timestamp suffix when you Start,
  matching macOS.
- **Recording-consent gate** — the first Start on a machine shows the
  deployment's `recordingDisclaimer` (from `lma-config.json`; same text as the
  browser extension's popup) with Agree/Cancel, rendered in place of the Start
  controls. Agree persists (`HKCU\...\DisclaimerAgreed`) so it's one-time;
  Cancel doesn't start. The JumpList "Start Recording" path routes through the
  same gate (`NeedsDisclaimer` / `ShowDisclaimerGate`), surfacing the panel so
  the dialog is visible.

### Headless CLI mode (dev + debugging)

Any `--flag` runs the headless CLI (`--cli` forces it). It streams to stdout
with the same one-line VU meter as macOS.

```powershell
$env:LMA_WS_ENDPOINT="wss://<your-cloudfront-domain>/api/v1/ws"
$env:LMA_ACCESS_TOKEN="<cognito-access-token>"
$env:LMA_ID_TOKEN="<cognito-id-token>"
$env:LMA_CALL_ID="Native Windows test $(Get-Date -f HH:mm)"
LMACaptureClient.exe --cli
```

Access tokens expire in ~1 hour; to let a pasted-token session renew itself,
also paste the Cognito **refresh token** into `LMA_REFRESH_TOKEN` (preferred —
a refresh token is a long-lived credential, ~30 days, and anything on the
command line is visible to other local users via Task Manager or
`Get-CimInstance Win32_Process`; `--refresh-token` exists for parity) — with it
the client refreshes proactively before `exp` and retries once after a 401 (see
"Token refresh" below). The in-app `--username` login captures the refresh
token automatically, so it never needs this variable.

Interactive controls on a TTY: press **m** to toggle mic mute, **q** (or
Ctrl-C) to stop.

Or sign in with SRP instead of pasting tokens:

```powershell
LMACaptureClient.exe --username you@example.com          # prompts for password
LMACaptureClient.exe --login-only --username you@example.com   # login, print token metadata, exit
```

### Subcommands

| Command | What it does |
|---|---|
| `--selftest` | Run the SRP known-answer vectors offline and exit (0 = pass). |
| `--login-only` | Do the SRP login, print token metadata (not the token), exit. |
| `--capture-test <seconds> <out.wav>` | Run the **real** WASAPI capture + mixer + WavTee with **no socket**, so you can verify channel mapping offline — no token/server needed. |
| `--debug-wav <path>` | Tee the exact streamed stereo PCM to a WAV during a live run. |
| `--lma-start` / `--lma-pause` / `--lma-stop` / `--lma-panel` | Internal: taskbar JumpList verbs. Forwarded to the running tray app over a named pipe (see `App/TaskbarStatus.cs`); not meant to be typed by hand. |

---

## Verifying it works

### 1. SRP crypto (offline)

```powershell
LMACaptureClient.exe --selftest
# → "All self-tests PASSED"
```

### 2. Channel mapping (offline, no server)

Play some audio (YouTube / a media file) and speak into the mic while running:

```powershell
LMACaptureClient.exe --capture-test 6 out.wav
```

Then measure per-channel RMS on `out.wav` — **ch0/Left should carry the system
audio, ch1/Right the mic**, time-aligned and not swapped. (`--debug-wav` on a
live streaming run produces the identical file — it tees the exact bytes sent to
the server.) A quick PowerShell RMS check:

```powershell
$b=[IO.File]::ReadAllBytes("out.wav"); $o=44; $n=($b.Length-$o)/4
$sl=0.0;$sr=0.0; for($i=0;$i -lt $n;$i++){
  $l=[BitConverter]::ToInt16($b,$o+$i*4)/32768; $r=[BitConverter]::ToInt16($b,$o+$i*4+2)/32768
  $sl+=$l*$l;$sr+=$r*$r }
"ch0/Left(system) rms={0:F4}  ch1/Right(mic) rms={1:F4}" -f [math]::Sqrt($sl/$n),[math]::Sqrt($sr/$n)
```

### 3. Live meeting

Start a meeting in the **native Zoom/Teams app**, run the tray app, sign in,
click **Start**, and watch the LMA web UI meeting list — a new meeting (your
meeting name / callId) should appear with live transcript segments: your speech
on the **AGENT** channel, remote participants on the **CALLER** channel.

### 4. Reconnect

Briefly disconnect the network mid-stream. The client logs a capped-backoff
reconnect, reopens with a **fresh START**, and flushes the audio buffered during
the outage.

### Device changes mid-meeting

Switching the default playback or recording device is handled automatically: an
`IMMNotificationClient` rebuilds the affected capture against the new device.
Channel alignment holds because the mixer only drains frames both channels can
supply.

---

## Configuration

Each value resolves with precedence **CLI flag → env var → `lma-config.json` →
default**. `lma-config.json` sits next to the executable (the packaging pipeline
bakes the deployment's real values into it; the checked-in copy is a placeholder).

| JSON key | CLI flag | Env var | Meaning |
|---|---|---|---|
| `wssEndpoint` | `--endpoint` | `LMA_WS_ENDPOINT` | WebSocket transcriber URL |
| `webEndpoint` | `--web-endpoint` | `LMA_WEB_ENDPOINT` | Web UI base (for "Open in LMA"; a **different** CloudFront distro than the WS host) |
| `userPoolId` | `--user-pool-id` | `LMA_USER_POOL_ID` | Cognito pool id |
| `clientId` | `--client-id` | `LMA_CLIENT_ID` | Cognito app client id |
| `region` | `--region` | `LMA_REGION` | Region (else inferred from pool id prefix) |
| `samplingRate` | `--sample-rate` | `LMA_SAMPLE_RATE` | default **48000** |
| — | `--token` | `LMA_ACCESS_TOKEN` | pasted access token (alt to login) |
| — | `--id-token` | `LMA_ID_TOKEN` | pasted id token |
| — | `--refresh-token` | `LMA_REFRESH_TOKEN` | pasted refresh token, lets the session renew itself (prefer the env var — argv is visible to other local users) |
| — | `--call-id` | `LMA_CALL_ID` | default `"LMA native prototype - <ISO8601 now>"` |
| — | `--agent-id` | `LMA_AGENT_ID` | mic-channel label, default `"Me"` |
| — | `--from` | `LMA_FROM` | meeting-channel label, default `"Other participants"` |
| — | `--to` | `LMA_TO` | default `"System"` |
| — | `--debug-wav` | `LMA_DEBUG_WAV` | tee streamed PCM to a WAV |
| — | `--username` | `LMA_USERNAME` | for in-app SRP login |
| — | `--password` | `LMA_PASSWORD` | (prompted without echo if omitted) |

---

## Distribution & signing

For a **v1, prefer build-from-source** on the user's machine (zero signing
infra, $0), exactly like the macOS prototype. A self-contained
`dotnet publish` produces a standalone folder you can zip.

For a polished download you'd add **Authenticode** signing (OV/EV cert on a
hardware token or cloud HSM; EV clears SmartScreen reputation faster;
~$100–400/yr) — analogous to Apple notarization but **not required** for a
build-from-source release. A GitHub Actions `windows-latest` lane can produce a
signed `.exe`/`.msi` when that becomes a priority.

The parent CloudFormation stack (`../../template.yaml`) bakes the deployment's
endpoint + Cognito ids into `lma-config.json` and zips the source per platform;
the Windows lane there is purely additive and does **not** compile the app
(mirroring the macOS model — the user builds on their own machine).
