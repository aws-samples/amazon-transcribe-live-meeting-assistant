# LMA Native macOS Audio Client — Prototype & Plan

Streams **microphone + system audio** from a Mac directly to the LMA WebSocket
transcriber, so a user can transcribe a meeting they joined from a **native
desktop app** (Zoom / Teams / Meet / Slack huddle / phone bridge) — no Chrome
tab sharing, no Virtual Participant bot.

> ✅ **Status: builds, runs, and streams on macOS.** First brought up on macOS
> 26 / Swift 6.3 (2026-07). The SCK/AVFoundation code compiled unchanged; the
> one real bug was **auth** (see below). Audio pipeline verified end-to-end at
> the byte level (per-channel WAV tee + RMS): ch0=system, ch1=mic, correctly
> separated, aligned, and not swapped. Still **pending: a live meeting** through
> a native Zoom/Teams client to confirm transcript segments in the LMA UI.
>
> **⚠️ AUTH — do NOT switch back to header auth.** The original spike sent the
> Cognito token in an `Authorization: Bearer` header ("native clients can set
> headers"). That yields **401** here: the transcriber sits behind a CloudFront
> distribution whose `OriginRequestPolicy` header whitelist does **not** include
> `Authorization`, so CloudFront strips it before the origin. `QueryStringBehavior`
> is `all`, so we authenticate the **browser's way — query params**
> (`?authorization=Bearer%20...&id_token=...`). The Node CLI at
> `utilities/websocket-client` gets away with headers only because it hits the
> origin/ALB directly, bypassing CloudFront. See the AUTH NOTE in
> `TranscriberSocket.swift`.

---

## Why this is the right approach

The browser "Stream Audio" tab gets incoming meeting audio from
`getDisplayMedia({video:true, audio:true})` — Chrome/Edge-only, and it can only
hear audio playing **inside a browser tab**. It cannot hear a native Zoom/Teams
client. macOS **system-audio loopback** (ScreenCaptureKit) captures whatever is
playing on the machine regardless of the source app — exactly what screen
recorders use. That removes the browser dependency entirely.

The server side needs **no changes**: the transcriber is a plain WebSocket that
takes a JSON `START` frame then raw interleaved 16-bit PCM binary frames. We're
just a third client (alongside the browser and the existing Node reference
client at `utilities/websocket-client/`).

## The wire protocol (what this client implements)

1. `wss://<cloudfront-domain>/api/v1/ws`, Cognito **access token** in the
   `Authorization: Bearer` header (native clients can set headers; browsers use
   a query param instead — the server accepts both).
2. One JSON text frame: `{callId, agentId, fromNumber, toNumber, samplingRate, callEvent:"START"}`.
3. Raw **16-bit LE PCM, 2-channel interleaved**, ~100 ms binary frames.
   **ch0 = meeting/system audio (→ CALLER), ch1 = mic (→ AGENT).**
4. JSON `{..., callEvent:"END"}`, then close.

Source layout:
- `Config.swift`         — CLI/env config parsing.
- `TranscriberSocket.swift` — WebSocket + START/PCM/END framing (URLSession, no deps).
- `AudioCapture.swift`   — ScreenCaptureKit (system audio) + AVAudioEngine (mic).
- `StereoMixer.swift`    — align two mono streams → interleaved int16 chunks.
- `main.swift`           — wire-up + Ctrl-C shutdown.

## Build & run (on a Mac)

Requires macOS 13+ and Xcode command-line tools (`xcode-select --install`).

> **Downloaded this from the LMA web UI?** Run `./install-macos.sh` — it clears
> the macOS download quarantine, checks prerequisites, builds, and bundles the
> app in one step. If you double-click something first and hit *"Apple could not
> verify … is free of malware"*, that's Gatekeeper's download-quarantine flag;
> clear it from the unzipped folder with `xattr -dr com.apple.quarantine .` and
> re-run. (Building locally means nothing is downloaded pre-built, so once
> quarantine is cleared the freshly built binary is ad-hoc signed and just runs.)

### Recommended: run as a `.app` bundle (own TCC identity)

`swift run` has no bundle, so macOS attributes Mic/Screen-Recording to the
**parent process (Terminal)**. `make-app.sh` wraps the release binary in a
minimal `.app` with an `Info.plist` (mic usage string) so permissions are
attributed to **"LMA Audio Client"** — the way the production build will behave.

```bash
cd lma-audio-capture-app-stack/source/macos
./make-app.sh                       # swift build -c release + assemble build/LMAAudioClient.app

# Pass config via env vars (keeps tokens out of shell history & `ps` output).
export LMA_WS_ENDPOINT="wss://<your-cloudfront-domain>/api/v1/ws"
export LMA_ACCESS_TOKEN="<cognito-access-token>"
export LMA_ID_TOKEN="<cognito-id-token>"
export LMA_CALL_ID="Native Mac test $(date +%H:%M)"
export LMA_DEBUG_WAV="/tmp/lma-debug.wav"   # optional: tee streamed PCM for offline verify

./build/LMAAudioClient.app/Contents/MacOS/LMAAudioClient
```

Plain `swift run LMAAudioClient --endpoint ... --token ... --id-token ...` also
works for a quick spike, but grant permissions to *Terminal* in that case.

### Menu-bar (tray) app

Launched with **no CLI flags**, the app runs as a **menu-bar app** (an
`LSUIElement` agent — no Dock icon). Any `--flag` runs the headless CLI instead,
so the two modes coexist in one binary.

```bash
open build/LMAAudioClient.app          # or double-click it in Finder
```

An **"LMA"** item appears in the menu bar (top-right). **Left-click** it for the
popover: sign in, start/stop/pause, mute mic, mute system audio, live level
meters, and **Open in LMA**. While recording, the icon and label turn **red
(`●REC`)**. **Right-click** the item for **Quit** (kept out of the popover so it
can't be confused with *Stop*).

Options in the popover:
- **Remember my email** — prefills your login next launch (email only; never the
  password).
- **Start automatically at login** — registers the app as a macOS login item
  (`SMAppService`). For this to work the app must live in a stable location, so
  **move it to `/Applications` first**:
  ```bash
  cp -R build/LMAAudioClient.app /Applications/
  open /Applications/LMAAudioClient.app
  ```
  Toggle it off in the popover, or in **System Settings ▸ General ▸ Login Items**.

**Run it in the background.** It uses no audio/CPU when idle, so the intended
usage is: sign in once, enable *Start at login*, and leave it in the menu bar —
click **Start** when a meeting begins. **To relaunch after quitting**, press
**⌘-Space** (Spotlight), type **"LMA Audio Client"**, and hit Return — or
`open -a LMAAudioClient`.

### Live controls & diagnostics
- **Per-channel VU meters** print ~1×/sec: `ch0 meeting [##--] rms .. | ch1 mic
  [###-] rms ..` — confirms both channels are live and not swapped. A
  `⚠️ no audio drained` line means one source is dead.
- **Mic mute:** `kill -USR1 <pid>` toggles mute (or press `m`+Enter on a TTY).
  Muting streams true digital silence on ch1 while keeping channels aligned.
- **Reconnect:** on any WS drop the client reopens with capped backoff and sends
  a **fresh START** (the server has no session resume).
- **Device changes:** switching the default input/output mid-meeting rebuilds
  the mic tap automatically.
- **`--debug-wav` / `LMA_DEBUG_WAV`:** writes the exact streamed stereo PCM to a
  WAV so you can verify channels offline (`ch0`=Left=system, `ch1`=Right=mic).

### Getting a token for the spike

Fastest path: log into the LMA web UI in Chrome, open DevTools → Application →
Session/Local Storage, and copy the Cognito **access token** and **id token**
(keys under `CognitoIdentityServiceProvider.<clientId>.<user>.accessToken` /
`.idToken`). Paste into `--token` / `--id-token`. Tokens expire in ~1 hour —
fine for a spike; production needs the OAuth flow below.

### Permission prompts during the spike (important)

A bare `swift run` executable has **no app bundle / Info.plist**, so macOS
attributes its Microphone and Screen Recording access to the **parent process —
i.e. Terminal.app (or iTerm)** — not to "LMAAudioClient". Practical consequence:
grant **Terminal** both Microphone and Screen Recording in
System Settings › Privacy & Security, relaunch Terminal, then re-run. If SCK
still fails, wrap the binary in a minimal `.app` bundle (with the usage-string
`Info.plist`) so it gets its own TCC identity — that's what the production build
does anyway. This is a spike-only wrinkle, not a design problem.

### Verifying it works

Start a meeting in the **native Zoom/Teams app**, run the client, and watch the
LMA web UI meeting list — a new meeting (your `--call-id`) should appear with
live transcript segments, with your speech on the AGENT channel and the remote
participants on the CALLER channel. Also check the transcriber's CloudWatch logs
for the `START` event and Transcribe session.

---

## Production plan

Rough phasing; each builds on the prior.

### Phase 1 — Core capture app (the spike, hardened)
- Fix any first-build SCK/AVFoundation API issues; handle device changes
  (default output/input switching mid-meeting), sample-rate mismatches, and
  under/overflow when one source stalls.
- Robust reconnect: on WS drop, re-open and send a **fresh START** (server does
  not support client session resume). Buffer briefly during reconnect.
- Mic-mute toggle; VU meters so users can confirm both channels are live.

### Phase 2 — Auth (replaces pasted tokens)
- **OAuth Authorization Code + PKCE** against the Cognito Hosted UI: open the
  system browser, catch the redirect on `http://127.0.0.1:<port>` (or a custom
  URL scheme), exchange the code for tokens.
- **Refresh-token handling**: access tokens expire (~1 h); refresh before
  reconnect. Store the refresh token in the **macOS Keychain**.
- Fetch `WSEndpoint` + Cognito App Client ID / Hosted-UI domain from a small
  bootstrap config (today those live in SSM; expose a tiny unauthenticated
  config endpoint or ship a downloadable `.lmaconfig` file from the LMA site).

### Phase 3 — App shell & UX
- Menu-bar app (tray) with: sign-in, endpoint/config, meeting name, start/stop,
  mute, live status + VU meters, "recording in progress" indicator.
- Recommended stack: **native Swift + SwiftUI** (keeps the SCK/Core Audio path
  first-class and the bundle small). Avoid Electron — it re-imports the exact
  Chromium `getDisplayMedia` limitation we're escaping.
- First-run permission wizard (Microphone + Screen Recording) with deep links
  into System Settings and clear "why we need this" copy.

### Phase 4 — Distribution
- **Developer ID signing + Apple notarization** (see below) — mandatory for a
  downloadable app.
- Auto-update (Sparkle) or "check for updates" against the LMA site.
- Offer both a signed `.dmg`/`.pkg` download **and** buildable source.
- Windows counterpart later: WASAPI loopback (built into the OS — easier than
  macOS), same protocol client; Authenticode signing.

### Cross-platform note
If a shared codebase becomes a priority, a **Rust core** (`cpal` for WASAPI
loopback on Windows; a thin ScreenCaptureKit shim on macOS) with per-OS UI is
the natural consolidation. For a macOS-first launch, native Swift is faster.

---

## macOS code signing & notarization — what you're signing up for

This is the biggest non-code cost of shipping a downloadable Mac app. Summary:
**an unsigned/un-notarized app is effectively undistributable** — Gatekeeper
will block it with "cannot be opened because the developer cannot be verified"
and, for quarantined downloads, users can't even right-click-open it cleanly on
recent macOS. To distribute outside the Mac App Store you need the full chain.

### The chain

1. **Apple Developer Program membership** — $99/year. Gives you the ability to
   create signing certificates and notarize.

2. **Developer ID Application certificate** — this is the identity you sign the
   app with for **outside-the-App-Store** distribution. (The "Apple
   Development" / "Mac App Store" certs are different and won't work for a
   website download.) Created in the Apple Developer portal or via Xcode;
   the private key lives in your login Keychain / CI keychain.

3. **Sign the app bundle** with `codesign`, using the **hardened runtime**
   (`--options runtime`) — notarization *requires* hardened runtime:
   ```bash
   codesign --deep --force --options runtime \
     --entitlements LMAAudioClient.entitlements \
     --sign "Developer ID Application: Your Org (TEAMID)" \
     LMAAudioClient.app
   ```

4. **Entitlements** — the hardened runtime blocks capabilities unless you
   declare them. For this app you need at least:
   - `com.apple.security.device.audio-input` (microphone)
   - Screen Recording is **not** an entitlement — it's a **TCC user consent**
     prompt driven by an `Info.plist` usage string (see below).
   - If you later sandbox for other reasons, more entitlements apply; a plain
     Developer ID app does **not** have to be sandboxed.

5. **Info.plist usage-description strings** — required or the app crashes when
   it hits the API instead of prompting:
   - `NSMicrophoneUsageDescription` — "LMA needs the microphone to transcribe
     your side of the meeting."
   - **Screen Recording**: there is no dedicated plist key; the prompt is
     triggered by ScreenCaptureKit at runtime. You should still explain it in
     onboarding because audio-only capture confusingly requires the *Screen
     Recording* permission on macOS.

6. **Notarization** — upload the signed app to Apple's notary service; Apple
   scans it for malware and returns a ticket, usually within minutes:
   ```bash
   # Zip or wrap in a signed .dmg/.pkg first, then:
   xcrun notarytool submit LMAAudioClient.zip \
     --apple-id you@org.com --team-id TEAMID --password <app-specific-pw> \
     --wait
   ```
   Use an **app-specific password** (or an API key) — not your Apple ID password.

7. **Staple the ticket** so the app validates offline (users may run it with no
   network on first launch):
   ```bash
   xcrun stapler staple LMAAudioClient.app     # or the .dmg / .pkg
   ```

8. **Package for download** — ship a `.dmg` or `.pkg`. Sign **and** notarize the
   container too (staple the outer artifact), not just the inner `.app`.

### Ad-hoc signing vs notarization — and "can users sign it themselves?"

There's a local/self-signing command — **ad-hoc signing** — but it solves a
*different* problem than notarization, so it does **not** let you skip the chain
above for a polished download.

```bash
codesign -s - /path/to/LMAAudioClient    # "-s -" = ad-hoc: no cert, no Apple account
```

Two separate problems, and ad-hoc only fixes the first:

| | Ad-hoc `codesign -s -` | Developer ID + notarization |
|---|:---:|:---:|
| Makes an **arm64** binary executable at all | ✅ | ✅ |
| Passes **Gatekeeper** on a downloaded file | ❌ | ✅ |
| Suppresses "developer cannot be verified" | ❌ | ✅ |

Key facts:
- On **Apple Silicon, a signature is mandatory** — arm64 code won't run unsigned.
  `swift build` applies an ad-hoc signature automatically, which is why a
  locally-built binary "just runs".
- What actually blocks a *downloaded* file is the **`com.apple.quarantine`**
  extended attribute Gatekeeper adds to browser downloads. A user's ad-hoc
  signature does **not** remove it and is **not** a substitute for Apple
  notarization.

So the "ship unsigned, user signs after download" idea works only as this
two-command dance the user runs post-download:

```bash
xattr -dr com.apple.quarantine ~/Downloads/LMAAudioClient  # strip download quarantine
codesign -s - ~/Downloads/LMAAudioClient                   # ad-hoc sign (if shipped unsigned on arm64)
```

That's acceptable for a **CLI aimed at technical users** (many OSS Mac CLIs ship
this way). It's poor, slightly alarming UX for a **GUI app aimed at
non-technical users** — pasting `xattr`/`codesign` from a website is exactly
what notarization exists to avoid.

### ⚠️ The existing LMA CodeBuild pipeline cannot build this

`publish.sh` / the LMA CodeBuild pipeline is **Linux** (SAM, Docker, npm). A
native macOS Swift app using ScreenCaptureKit **cannot be cross-compiled from
Linux**, and `codesign` / `notarytool` are **macOS-only** tools. Therefore:
- The current Linux CodeBuild **cannot produce this artifact at all** — signed
  or unsigned. "Build unsigned in CodeBuild during deploy" is not a small tweak.
- Building it requires a **macOS build lane**: EC2 Mac, a CodeBuild macOS fleet,
  a GitHub Actions macOS runner, or a developer's Mac.

### Distribution options, ranked

1. **Build-from-source on the user's Mac (recommended for prototype / v1).**
   User runs `swift build`. The toolchain ad-hoc-signs automatically and there's
   no quarantine attribute (nothing was downloaded), so it *just runs* — no
   `xattr`, no Apple account, no CI changes, $0. Only cost: users need Xcode
   command-line tools. Fits "buildable/downloadable from the LMA site" directly.

2. **Prebuilt in a macOS lane, shipped unsigned/ad-hoc; user strips quarantine.**
   Add a macOS runner, build, upload to the site/S3 bucket. User runs the two
   `xattr`/`codesign` commands above. Fine for a CLI + technical users; weak for
   a GUI.

3. **Prebuilt + Developer ID + notarization (best UX, real cost).**
   Same macOS lane plus the $99/yr Apple account, Developer ID cert, and notary
   credentials in CI secrets (`codesign --options runtime` → `notarytool submit`
   → `stapler staple`). Users double-click, no warnings. The only pleasant path
   for non-technical users — but requires standing up macOS build infra separate
   from the existing Linux pipeline.

**Recommendation:** option 1 for the prototype and first internal release
(zero infra, zero cost, sidesteps signing entirely); optionally add option 2 as
a convenience prebuilt. Move to option 3 only for a public, non-technical-user
release.

### Gotchas
- **Sign inside-out**: sign nested frameworks/helpers/dylibs before the outer
  bundle, then the `.dmg`/`.pkg` last.
- **CI signing** needs the Developer ID cert imported into a temporary keychain
  and unlocked; store the cert (`.p12`) and notary credentials as CI secrets.
- **Certificates expire** (Developer ID Application: ~5 years) and depend on
  active membership — a lapsed $99 renewal breaks your ability to notarize new
  builds (already-notarized builds keep working).
- **Timestamping**: `codesign` uses Apple's secure timestamp by default; keep it
  (needed so signatures stay valid after the cert expires).
- **First-launch permission dance**: Screen Recording usually requires the user
  to toggle the app in System Settings and **relaunch** — build that into
  onboarding; you can't fully script it away.
- **`spctl -a -vvv LMAAudioClient.app`** and `codesign -dv --verbose=4` are your
  verification tools before publishing.

### Windows (for later)
Analogous but simpler audio (WASAPI loopback is built in). Signing uses an
**Authenticode** code-signing certificate; modern practice is an **OV/EV cert
on a hardware token or cloud HSM** (CAs stopped issuing file-based certs).
EV certs clear SmartScreen reputation faster. Budget ~$100–400/yr for the cert.
