# Prompt: Build the LMA Audio Capture App — Windows (Native)

> **How to use this file:** Open Claude Code (or your agent) in the LMA repo
> **on a Windows machine** and paste the entire "PROMPT" section below. It is
> self-contained: it specifies the wire protocol, the auth handshake, and the
> exact feature/behavior parity required so the Windows app matches the existing
> macOS app. The macOS reference implementation lives at
> `lma-audio-capture-app-stack/source/macos/` — read it for ground truth.

---

## PROMPT

You are building the **Windows-native** version of the **LMA Audio Capture App**.
A working **macOS** version already exists in this repo at
`lma-audio-capture-app-stack/source/macos/` (Swift + ScreenCaptureKit + SwiftUI).
Your job is to produce a Windows app that is **functionally identical** — same
wire protocol, same auth, same controls, same behavior — using a native Windows
stack. Build it, run it, and test it on this Windows machine.

### What the app does (one sentence)

It captures the machine's **system/loopback audio** (ch0) and the **microphone**
(ch1), interleaves them into 2-channel 16-bit PCM, and streams that to the LMA
WebSocket transcriber — letting a user transcribe a meeting they joined from a
**native desktop client** (Zoom/Teams/Meet/Slack/phone bridge) with **no browser
tab sharing and no bot**. The server is unchanged; this is just a third client
alongside the browser and the Node CLI.

### Ground-truth reference — READ THESE FIRST

Read the macOS sources; they are small (~2,350 LOC) and heavily commented. Your
Windows implementation must match their **behavior** file-for-file in spirit:

| macOS file | Responsibility | Port to Windows as |
|---|---|---|
| `Sources/LMAAudioClient/AudioCapture.swift` | System audio (ScreenCaptureKit) + mic (AVAudioEngine), resample to target rate, mono downmix, device-change handling | **WASAPI**: loopback capture on default render endpoint (system) + capture on default capture endpoint (mic); resample via Media Foundation / built-in; handle device changes via `IMMNotificationClient` |
| `Sources/LMAAudioClient/StereoMixer.swift` | Buffer 2 mono float streams, drain `min(count)` frames on a 100 ms cadence → interleaved int16 LE, mute/pause, VU meters, debug tee | Port the **logic verbatim** — it is pure and platform-agnostic |
| `Sources/LMAAudioClient/TranscriberSocket.swift` | WebSocket: START/PCM/END framing, reconnect w/ capped backoff + fresh START, brief PCM buffering during reconnect, handshake-failure detection | Port logic; use `System.Net.WebSockets.ClientWebSocket` (or equivalent) |
| `Sources/LMAAudioClient/SRP.swift` + `BigUInt.swift` | Cognito `USER_SRP_AUTH` login, dependency-free, validated against pycognito | Port the algorithm exactly (see "Auth" below); .NET has `BigInteger` built in |
| `Sources/LMAAudioClient/Config.swift` | Config layering: CLI flag → env var → `lma-config.json` → default | Port verbatim; read `lma-config.json` from next to the executable |
| `Sources/LMAAudioClient/CaptureController.swift` | UI-agnostic engine controller: login/start/stop/pause/mute + state machine + callbacks | Port verbatim |
| `Sources/LMAAudioClient/MenuBarApp.swift` | macOS menu-bar (tray) UI | **System-tray app** (see UI parity below) |
| `Sources/LMAAudioClient/WavTee.swift` | Debug: tee exact streamed PCM to a local WAV | Port verbatim |
| `Sources/LMAAudioClient/SelfTest.swift` | Offline known-answer test for the SRP crypto | Port; keep the same known-answer vectors |
| `Sources/LMAAudioClient/main.swift` | Entry point; GUI with no args, headless CLI with any `--flag` | Same dual-mode dispatch |

### Recommended stack

**C# / .NET 8** (native, self-contained). Rationale: first-class WASAPI access,
`BigInteger` + `HMACSHA256`/`SHA256` in the BCL (no crypto deps for SRP),
`ClientWebSocket` in the BCL, and a mature tray-app story.

- **Audio:** [`NAudio`](https://github.com/naudio/NAudio) — `WasapiLoopbackCapture`
  for system audio and `WasapiCapture` for the mic. (CSCore is an acceptable
  alternative.) Resample with NAudio's `MediaFoundationResampler` or
  `WdlResamplingSampleProvider`.
- **UI:** A tray app. **WPF** or **WinUI 3** for the popup panel + `NotifyIcon`
  (WPF: `System.Windows.Forms.NotifyIcon` from a hidden window, or the
  `Hardcodet.NotifyIcon.Wpf` package). Keep the app **tray-only** (no taskbar
  window when idle), mirroring the macOS `LSUIElement` menu-bar behavior.
- **WebSocket / HTTP / crypto / JSON:** BCL only (`ClientWebSocket`,
  `HttpClient`, `System.Numerics.BigInteger`, `System.Security.Cryptography`,
  `System.Text.Json`).

Keep third-party dependencies minimal (NAudio + optionally the WPF tray-icon
helper). Do **not** use Electron — it re-imports the exact browser
`getDisplayMedia` limitation this app exists to escape.

Put everything under `lma-audio-capture-app-stack/source/windows/`.

---

### THE WIRE PROTOCOL (must match exactly — server is unchanged)

1. Connect `wss://<cloudfront-domain>/api/v1/ws`.
   **AUTH — critical, do NOT use an `Authorization` header.** The transcriber
   sits behind CloudFront whose `OriginRequestPolicy` header whitelist does
   **not** include `Authorization`, so the header is stripped → 401. Authenticate
   the **browser's way, via query params** on the WS URL:
   - `authorization=Bearer%20<ACCESS_TOKEN>`
   - `id_token=<ID_TOKEN>`
   - `refresh_token=` (empty)
   (You may also set the same values as headers — harmless, and lets the client
   work if pointed straight at the origin/ALB in future — but query params are
   what actually authenticate through CloudFront.)
2. Send **one JSON text frame** (the START handshake), then audio:
   ```json
   { "callId": "<string>", "agentId": "<mic label>", "fromNumber": "<meeting label>",
     "toNumber": "<string>", "samplingRate": 48000, "callEvent": "START" }
   ```
3. Stream raw **interleaved 16-bit LE PCM, 2-channel**, in ~100 ms binary frames.
   **ch0 = meeting/system audio (→ CALLER), ch1 = mic (→ AGENT).** Do not swap.
   Interleave starting at index 0 (do NOT replicate the browser worklet's
   off-by-one that drops the first mic sample).
4. On stop, send a JSON text frame with `"callEvent": "END"`, then close.

**float→int16 clamp** (match `StereoMixer.floatToInt16`): clamp to [-1, 1], then
`s < 0 ? s * 32768 : s * 32767`.

**Reconnect semantics (match `TranscriberSocket`):** the server has **no session
resume**. On any unexpected drop, reopen a new socket and send a **fresh START**
(same callId) before audio. Use capped exponential backoff (0.5s → cap 10s).
Briefly buffer produced PCM while the socket is down (bound it to ~3s of stereo
audio: `48000 * 2 bytes * 2 ch * 3`), then flush on reconnect; drop oldest beyond
the bound and count it. If the socket **never opens** and the handshake keeps
failing (~4 tries), treat it as an expired/invalid token: warn clearly and stop
(don't hammer the server with a dead token).

**Mixer drain (match `StereoMixer.drain`):** on a 100 ms timer, drain
`n = min(meetingBuffered, micBuffered)` frames so the two channels stay aligned;
if one source stalls, hold back rather than skew. Mute = zero that channel's
samples but still consume them (keeps alignment). Pause = consume+discard, send
nothing, keep the socket open. Emit per-second VU levels (RMS + peak per channel)
for the UI and a one-line meter; a "no audio drained this second" warning when
both channels can't supply frames.

---

### AUTH — Cognito `USER_SRP_AUTH` (match `SRP.swift` / `BigUInt.swift` exactly)

Dependency-free SRP-6a, the same flow the LMA web UI (Amplify) uses. Two
unauthenticated calls to `https://cognito-idp.<region>.amazonaws.com/` with
header `X-Amz-Target: AWSCognitoIdentityProviderService.<Op>` and
`Content-Type: application/x-amz-json-1.1`:

1. `InitiateAuth` `{AuthFlow: USER_SRP_AUTH, ClientId, AuthParameters: {USERNAME, SRP_A}}`
   → returns `PASSWORD_VERIFIER` challenge with `SALT`, `SRP_B`, `SECRET_BLOCK`,
   `USER_ID_FOR_SRP`.
2. `RespondToAuthChallenge` `{ChallengeName: PASSWORD_VERIFIER, ClientId,
   ChallengeResponses: {USERNAME, PASSWORD_CLAIM_SECRET_BLOCK,
   PASSWORD_CLAIM_SIGNATURE, TIMESTAMP}}` → `AuthenticationResult` with
   `AccessToken`, `IdToken`, `RefreshToken`.

The password is never sent. Signature math (Cognito SRP variant, SHA-256), copy
**exactly** from `SRP.swift` — these details are load-bearing and are validated
byte-for-byte in `SelfTest.swift`:

- N = RFC 3526 **3072-bit** MODP prime (the `nHex` constant in `SRP.swift`), g = 2.
- `a` = random 128 bytes mod N; `A = g^a mod N`; reject if `A % N == 0`.
- `k = hexHash("00" + N_HEX + "0" + "2")` (note the literal `"0"` before g's hex).
- `u = hexHash(padHex(A) + padHex(B))`; reject if 0.
- `x = hexHash(padHex(salt) + sha256hex(poolName + userId + ":" + password))`
  where `poolName` is the pool id **after** the `_` (e.g. `us-west-2_ABC` → `ABC`).
- `S = (B - k*g^x)^(a + u*x) mod N`, computed underflow-safe as `(B + N - k*g^x mod N)`.
- HKDF "Caldera Derived Key", 16-byte key: `prk = HMAC(salt=padHex(u) bytes, ikm=padHex(S) bytes)`;
  `okm = HMAC(prk, "Caldera Derived Key" || 0x01)`; take first 16 bytes.
- signature = `base64( HMAC-SHA256(key, poolNameBytes || userIdBytes || secretBlockBytes || timestampBytes) )`.
- `padHex`: if odd length prepend `"0"`; else if top nibble is 8..F prepend `"00"`
  (sign byte). NOT full-width padding.
- `sha256hex`: lowercase hex, zero-padded to 64 chars.
- `hexHash(hexStr)` = `sha256hex(bytesOf(hexStr))`.
- **timestamp** format (en-US, UTC, **no leading zero** on day):
  `ddd MMM d HH:mm:ss 'UTC' yyyy` — in .NET:
  `DateTime.UtcNow.ToString("ddd MMM ", enUS) + day + DateTime.UtcNow.ToString(" HH:mm:ss 'UTC' yyyy", enUS)`
  (or format and strip the leading zero on the day). Must match pycognito.

Port `SelfTest.swift`'s known-answer vectors and run them as a `--selftest`
subcommand; the SRP signature must reproduce the baked-in expected value offline
before you trust a live login. `.NET`: use `System.Numerics.BigInteger` (mind
sign/endianness — Cognito hex is unsigned big-endian), `SHA256`, `HMACSHA256`.

---

### CONFIG (match `Config.swift`)

Resolve each value with precedence: **CLI flag → env var → `lma-config.json` →
default**. `lma-config.json` is baked into the download package and sits next to
the executable (also check a `Resources/` sibling). Keys and their flags/env:

| JSON key | CLI flag | Env var | Meaning |
|---|---|---|---|
| `wssEndpoint` | `--endpoint` | `LMA_WS_ENDPOINT` | WebSocket transcriber URL |
| `webEndpoint` | `--web-endpoint` | `LMA_WEB_ENDPOINT` | Web UI base (for "Open in LMA" deep link — a **different** CloudFront distro than the WS host) |
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
| — | `--password` | `LMA_PASSWORD` | (prompt without echo if omitted) |

"Open in LMA" URL: `"<webEndpoint without trailing slash>/#/calls"` plus
`"/" + encodeURIComponent(callId)` when a call is active. Fall back to the WS host
only if `webEndpoint` is empty.

Ship a sample `lma-config.json` in `source/windows/` with placeholder values
(the real one is baked by the packaging pipeline — see below).

---

### UI / BEHAVIOR PARITY (match `MenuBarApp.swift`)

A **system-tray app**, tray-only (no taskbar button when idle). Left-click the
tray icon → popup panel; right-click → context menu with **Quit** (kept out of
the panel so it can't be confused with Stop). While recording, the tray icon
turns **red / shows a recording indicator**.

Panel contents and states (state machine: `idle → signingIn → authenticated →
starting → streaming ⇄ paused → stopping → idle`, plus `error`):
- **Signed out:** Email + Password fields, **"Remember my email"** toggle
  (persist **email only**, never the password — use the registry or a settings
  file under `%APPDATA%`), **Sign In** button, inline error text.
- **Authenticated, not streaming:** optional **Meeting name** field, **Start
  Recording** button, **"Start automatically at login"** toggle (Windows: HKCU
  `...\Run` registry key or a Startup-folder shortcut or Task Scheduler entry —
  reflect the real system state), **Sign out**, **"Open LMA meetings"**.
- **Streaming:** live **per-channel VU meters** (System = ch0, Mic = ch1),
  **● Live / ○ Reconnecting…** status, **Pause/Resume**, **Stop**, **Mute mic**,
  **Mute system**, **"Open this meeting in LMA"**. Meeting name gets a timestamp
  suffix `"<name> - yyyy-MM-dd HH:mm"` when starting, matching macOS.

Meters use ×3 gain for visibility, same as macOS. Reflect live WS connection
state so "sent" isn't misleading while reconnecting.

**Dual mode (match `main.swift`):** launched with **no CLI flags** → GUI (tray);
with any `--flag` → **headless CLI** streaming to stdout with the same VU meter
line. `--cli` forces CLI, `--gui` forces GUI, `--selftest` runs the SRP
known-answer test and exits, `--login-only` does SRP then prints token metadata
(not the token) and exits.

---

### PERMISSIONS / OS NOTES (Windows is simpler than macOS here)

- **System (loopback) audio needs no special permission** on Windows — WASAPI
  loopback on the default render endpoint is built in. This removes the entire
  macOS "Screen Recording TCC + relaunch" dance. Call this out in the README as a
  Windows advantage.
- **Microphone:** Windows 10/11 has a Settings ▸ Privacy ▸ Microphone gate.
  Handle the access-denied case gracefully and point the user to that page.
- **Device changes mid-meeting:** register an `IMMNotificationClient` (NAudio
  exposes device enumeration/notifications) and rebuild the affected capture
  when the default render/capture device changes — mirror
  `AudioCapture.observeDeviceChanges()`. Keep channel alignment (the mixer only
  drains frames both channels can supply).
- **Sample rate:** capture at the device rate, resample both sources to 48000 Hz
  mono before feeding the mixer.

---

### PACKAGING / DISTRIBUTION

Look at `lma-audio-capture-app-stack/template.yaml` — the CloudFormation stack
that packages the downloadable app. It is written generically ("macOS today;
Windows / iOS / Android later") and its CodeBuild job does **not compile** the
app — it only bakes the deployment's endpoint + Cognito ids into
`lma-config.json` and zips the source. Mirror that model for Windows:

- Provide a build script (`build-windows.ps1` or a `dotnet publish` invocation)
  and a first-run/README so a user can build on their own Windows machine
  (`dotnet publish -c Release -r win-x64 --self-contained`), OR
- Add a **Windows build lane** (GitHub Actions `windows-latest`) to produce a
  signed `.msi`/`.exe`. Signing is **Authenticode** (OV/EV cert on hardware
  token / cloud HSM; EV clears SmartScreen faster; ~$100–400/yr) — analogous to
  Apple notarization but not required for a build-from-source v1.
- For a v1, prefer **build-from-source** (zero signing infra) exactly like the
  macOS prototype. Add an unsigned-binary convenience build only if easy.

Do **not** change the existing macOS template behavior; if you extend the CFN
template to also emit a Windows zip, make it purely additive and keep the
`<VERSION_TOKEN>` substitution pattern.

---

### DELIVERABLES

Under `lma-audio-capture-app-stack/source/windows/`:
1. A .NET solution/project (`LMAAudioClient.Windows` or similar) implementing all
   layers above with **behavior parity** to the macOS app.
2. `--selftest` passing (SRP known-answer vectors reproduced offline).
3. `README.md` covering build, run, permissions, tray usage, start-at-login,
   `--debug-wav` verification, and the "loopback needs no permission" note.
4. A build script and a sample `lma-config.json`.
5. **Verification on this machine:**
   - `--selftest` passes.
   - `--debug-wav out.wav` while a Zoom/Teams/YouTube plays and you speak, then
     confirm the WAV has **ch0/Left = system audio, ch1/Right = mic**, aligned
     and not swapped (measure per-channel RMS).
   - A live meeting: start the tray app, sign in, Start, and confirm the meeting
     appears in the LMA web UI with transcript segments — your speech on the
     **AGENT** channel, remote participants on the **CALLER** channel.
   - Reconnect: kill the network briefly and confirm it reopens with a fresh
     START and flushes buffered audio.

### CONSTRAINTS

- Match the macOS **behavior and wire protocol exactly**; when in doubt, read the
  corresponding Swift file and replicate its logic and comments.
- Minimal dependencies (NAudio + optional WPF tray helper). BCL for WS/HTTP/
  crypto/JSON.
- Keep the engine (`CaptureController`, `StereoMixer`, `TranscriberSocket`,
  `Config`, SRP) **UI-agnostic** so the CLI and tray share one engine — same
  separation the macOS app has.
- Do not modify the server or any other stack. This is a new client only.

Start by reading the macOS sources, then scaffold the .NET project, port the
platform-agnostic layers (Config, StereoMixer, TranscriberSocket, SRP/BigUInt via
`BigInteger`, WavTee, CaptureController), then implement the WASAPI capture and
the tray UI, then build and run the verification steps above.
