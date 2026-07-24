# LMA Native Windows Audio Client

Streams **microphone + system (loopback) audio** from a Windows PC directly to
the LMA WebSocket transcriber, so a user can transcribe a meeting they joined
from a **native desktop app** (Zoom / Teams / Meet / Slack huddle / phone
bridge) — no Chrome tab sharing, no Virtual Participant bot.

This is the Windows counterpart to the macOS client in
`../macos/`. It speaks the **same wire protocol**, uses the **same Cognito SRP
login**, and shares the same UI-agnostic engine split between a **system-tray
app** and a **headless CLI**. The server is unchanged; this is a third client
alongside the browser and the Node CLI.

> ✅ **Status: builds, runs, and verified on Windows 11.**
> - `--selftest` reproduces the pycognito SRP known-answer byte-for-byte (incl.
>   the 3072-bit `g^x mod N` modpow).
> - `--capture-test` confirmed **ch0/Left = system audio, ch1/Right = mic**,
>   aligned and not swapped, by measuring per-channel RMS on the streamed PCM.
> - Tray GUI and headless CLI both launch cleanly.
> - Pending, like the macOS client: a full live meeting through a native
>   Zoom/Teams client to confirm transcript segments in the LMA web UI (needs a
>   deployed stack + credentials).

---

## Why Windows is simpler than macOS here

- **System (loopback) audio needs no special permission.** WASAPI loopback on
  the default render endpoint is built into Windows — no equivalent of macOS's
  "Screen Recording" TCC prompt and relaunch dance. This is a genuine Windows
  advantage: the app can capture whatever is playing (a native Zoom/Teams
  client) with zero permission friction.
- **Microphone** is the only OS gate: Windows 10/11 has a
  **Settings ▸ Privacy & security ▸ Microphone** toggle (and a separate "Let
  desktop apps access your microphone"). If mic capture is denied, the app logs
  a clear pointer to that page and keeps streaming system audio.

## The wire protocol (unchanged from the browser / macOS client)

1. `wss://<cloudfront-domain>/api/v1/ws` — **auth is via query params**, not an
   `Authorization` header. The transcriber sits behind CloudFront whose
   `OriginRequestPolicy` header whitelist does **not** include `Authorization`,
   so a header is stripped → 401. We authenticate the browser's way:
   `?authorization=Bearer%20<ACCESS_TOKEN>&id_token=<ID_TOKEN>&refresh_token=`.
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
and flushed on reconnect; overflow drops oldest and is counted. If the socket
never opens and the handshake keeps failing (~4 tries), we treat it as an
expired/invalid token, warn, and stop.

## Source layout

| File | Responsibility |
|---|---|
| `Engine/Config.cs` | Config layering: CLI flag → env var → `lma-config.json` → default |
| `Engine/Srp.cs` | Cognito `USER_SRP_AUTH` login, dependency-free (`System.Numerics.BigInteger` + BCL crypto) |
| `Engine/SelfTest.cs` | Offline known-answer test for the SRP crypto (`--selftest`) |
| `Engine/StereoMixer.cs` | Buffer 2 mono streams, drain `min(count)` on a 100 ms cadence → interleaved int16, mute/pause, VU meters, debug tee |
| `Engine/TranscriberSocket.cs` | WebSocket START/PCM/END framing, reconnect + fresh START, PCM buffering, handshake-failure detection (`ClientWebSocket`) |
| `Engine/WavTee.cs` | Debug: tee exact streamed PCM to a local WAV |
| `Engine/LinearResampler.cs` | Continuous-phase mono resampler (device rate → 48 kHz) |
| `Engine/AudioCapture.cs` | **WASAPI**: loopback on default render (system/ch0) + capture on default capture (mic/ch1), resample, mono downmix, device-change handling via `IMMNotificationClient` |
| `Engine/CaptureController.cs` | UI-agnostic engine controller: login/start/stop/pause/mute + state machine + callbacks |
| `App/Program.cs` | Entry point; dual-mode dispatch (GUI vs headless CLI) + `--selftest` / `--login-only` / `--capture-test` |
| `App/TrayApp.cs`, `App/PanelView.cs` | System-tray UI (WPF + Hardcodet.NotifyIcon) |
| `App/AppSettings.cs` | Remember-email (HKCU) + Start-at-login (HKCU `...\Run`) |

The engine (`Config`, `Srp`, `StereoMixer`, `TranscriberSocket`,
`CaptureController`, `WavTee`) is UI-agnostic, so the CLI and tray share one
engine — the same separation as the macOS app.

---

## Build & run

**Prerequisites:** the [.NET 8 SDK](https://dot.net/download). No admin needed —
the SDK can install into a user directory (e.g. via `dotnet-install.ps1
-InstallDir $HOME\dotnet`).

```powershell
cd lma-audio-capture-app-stack/source/windows

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
- `-Install` — copy to `%LOCALAPPDATA%\Programs\LMA Audio Capture` and add a
  **Start Menu** shortcut (launch via the Windows key → "LMA Audio Capture"). No
  admin required.
- `-ProgramFiles` — install machine-wide under `%ProgramFiles%` instead (the copy
  step re-launches elevated to get admin).
- `-DesktopShortcut` — also drop a Desktop shortcut.

Uninstall:

```powershell
./build-windows.ps1 -Uninstall
```

Removes the installed app (per-user and, if present, machine-wide) plus its Start
Menu / Desktop shortcuts, and clears the app's per-user settings (remembered
email, start-at-login `Run` entry). If you installed machine-wide with
`-ProgramFiles`, run the uninstall from an elevated (admin) PowerShell so it can
delete the `%ProgramFiles%` copy.

You can also build by hand:

```powershell
dotnet publish -c Release -r win-x64 --self-contained true
```

### Run the tray app

Double-click `LMAAudioClient.exe` (or run it with **no arguments**, or `--gui`).
It appears as a **system-tray icon** with no taskbar window when idle (mirrors
the macOS menu-bar `LSUIElement` behavior).

- **Left-click** the tray icon → popup panel (sign in, start/stop/pause, mute
  mic, mute system, live per-channel VU meters, "Open in LMA").
- **Right-click** → context menu with **Quit** (kept out of the panel so it
  can't be confused with *Stop*).
- While recording, the tray icon turns **red**.

Panel options:
- **Remember my email** — prefills your login next launch (email only; the
  password is never stored — it stays in memory for the session).
- **Start automatically at login** — toggles an `HKCU\...\Run` entry pointing at
  this exe (`--gui`); the checkbox reflects the real registry state, so it stays
  correct even if changed elsewhere.
- Meeting name gets a `" - yyyy-MM-dd HH:mm"` timestamp suffix when you Start,
  matching macOS.

### Headless CLI mode (dev + debugging)

Any `--flag` runs the headless CLI (`--cli` forces it). It streams to stdout
with the same one-line VU meter as macOS.

```powershell
$env:LMA_WS_ENDPOINT="wss://<your-cloudfront-domain>/api/v1/ws"
$env:LMA_ACCESS_TOKEN="<cognito-access-token>"
$env:LMA_ID_TOKEN="<cognito-id-token>"
$env:LMA_CALL_ID="Native Windows test $(Get-Date -f HH:mm)"
LMAAudioClient.exe --cli
```

Interactive controls on a TTY: press **m** to toggle mic mute, **q** (or
Ctrl-C) to stop.

Or sign in with SRP instead of pasting tokens:

```powershell
LMAAudioClient.exe --username you@example.com          # prompts for password
LMAAudioClient.exe --login-only --username you@example.com   # login, print token metadata, exit
```

### Subcommands

| Command | What it does |
|---|---|
| `--selftest` | Run the SRP known-answer vectors offline and exit (0 = pass). |
| `--login-only` | Do the SRP login, print token metadata (not the token), exit. |
| `--capture-test <seconds> <out.wav>` | Run the **real** WASAPI capture + mixer + WavTee with **no socket**, so you can verify channel mapping offline — no token/server needed. |
| `--debug-wav <path>` | Tee the exact streamed stereo PCM to a WAV during a live run. |

---

## Verifying it works

### 1. SRP crypto (offline)

```powershell
LMAAudioClient.exe --selftest
# → "All self-tests PASSED"
```

### 2. Channel mapping (offline, no server)

Play some audio (YouTube / a media file) and speak into the mic while running:

```powershell
LMAAudioClient.exe --capture-test 6 out.wav
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
