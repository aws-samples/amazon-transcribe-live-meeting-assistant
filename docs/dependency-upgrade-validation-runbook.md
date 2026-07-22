# Dependency Upgrade Validation Runbook (PR #400)

This runbook validates the consolidated Dependabot batch (PR #400) beyond the
automated build/test/lint and the local smoke test. It covers the two areas that
static analysis **cannot** reach:

1. **WebSocket transcriber** — Fastify 3→5 + `@fastify/websocket` 5→11 under real
   browser audio streaming to Amazon Transcribe (internet-facing runtime).
2. **Virtual Participant** — `@zoom/meetingsdk` 4→6 joining a live Zoom meeting.

> **Why manual?** Neither stack has unit tests, and the Zoom `ZoomMtg.*` calls
> live inside a browser-side HTML string (untyped, so `tsc` can't check them).
> The automated coverage already in place: per-stack build/test/lint from the
> merged branch, plus `npm run smoke` in the transcriber app (proves the
> Fastify 5 boot + v11 websocket handler round-trips a binary frame).

---

## Prerequisites

- `AWS_PROFILE=default` (per repo convention).
- A **test** deployment target — do **not** validate against production.
- For the Zoom test: a **Zoom Meeting SDK app** (Client ID + Secret) that Zoom
  has approved/allowlisted for meetings. Set these as the CloudFormation params
  `ZoomMeetingSdkClientId` / `ZoomMeetingSdkClientSecret` (both optional; when
  empty the VP falls back to automating the public Zoom web client and the
  SDK-6 code path is **not** exercised).

---

## Step 0 — Local smoke test (no deploy, ~30s)

```bash
cd lma-websocket-transcriber-stack/source/app
npm install --legacy-peer-deps
npm run smoke
```
**Pass:** prints `PASS: fastify 5 booted ... echoed the audio frame.` and exits 0.
The `FSTDEP023 disableRequestLogging` deprecation line is expected (still
functional; slated for a fastify-6 follow-up) — not a failure.

---

## Step 1 — Deploy the batch to a test stack (~35–40 min)

Deploying **is the highest-value validation** — it's the only way to exercise
the live Fastify-5 server under real audio and the Zoom SDK-6 join flow.

```bash
# From the PR #400 branch (chore/dependabot-batch):
AWS_PROFILE=default ./publish.sh <cfn_bucket_basename> <cfn_prefix> <region>
```
Then deploy the generated `lma-main.yaml` template URL via CloudFormation into a
**test** account/region. Provide the Zoom SDK params if testing Zoom.

**Pass:** all nested stacks reach `CREATE_COMPLETE` / `UPDATE_COMPLETE`. Watch
especially the WebSocket transcriber (ECS Fargate) and Virtual Participant
(Fargate) services reach a steady RUNNING state — a Fastify boot failure would
crash-loop the transcriber task.

---

## Step 2 — WebSocket transcriber live test (Fastify 5)

1. Open the LMA web UI, sign in, and start a **live meeting** using the browser
   extension or the built-in stream-audio page (stereo mic + tab audio).
2. Speak / play audio for ~60s.

**Pass criteria:**
- [ ] Transcript segments appear in the UI in near-real-time (proves the WS
      route accepts audio and streams to Transcribe under the v11 handler).
- [ ] Both channels transcribe (agent + caller / mic + tab).
- [ ] The ECS transcriber task does **not** restart during the session
      (CloudWatch logs show no `Error starting websocket server` / crash loop).
- [ ] `GET /health/check` on the transcriber ALB target returns 200.
- [ ] If `SHOULD_RECORD_CALL=true`: the WAV recording lands in the recordings
      S3 bucket.

**If it fails:** check the transcriber task's CloudWatch logs for Fastify boot
errors (logger/transport config) or `preHandler`/JWT auth rejections.

---

## Step 3 — Virtual Participant Zoom join test (Zoom SDK 6)

Invite the Virtual Participant to a **real Zoom meeting** (via the UI's
"Invite to meeting" with a Zoom URL). Tail the VP Fargate task's CloudWatch logs
and watch for the `[zoom-sdk]` / `ZoomSDK page:` lines.

The DOM selectors and `ZoomMtg.*` event payloads are the **most fragile surface**
across Zoom SDK majors — check each row:

| # | What to verify | Console signal on success | Symptom if SDK-6 broke it |
|---|----------------|---------------------------|---------------------------|
| 1 | **SDK loads** | `__lmaSdkReady === true` within 60s | hangs 60s then "Loading Zoom SDK…" timeout — `setZoomJSLib`/`preLoadWasm`/`prepareWebSDK` or vendored asset path changed |
| 2 | **VP joins & is admitted** | `[zoom-sdk] clicked preview Join` → `[zoom-sdk] joined meeting` | `Zoom SDK join failed (code=...)` — `ZoomMtg.init`/`join` args or the JWT signature payload changed |
| 3 | **Audio captured / transcribed** | `[zoom-sdk] joined computer audio`; transcript appears in UI | no "joined computer audio" — the `join audio` toolbar selector changed |
| 4 | **Active-speaker labels** | speaker names attributed in transcript | all text unattributed — `onActiveSpeaker` payload shape changed |
| 5 | **Chat commands** (pause/resume/end via meeting chat) | intro message posts; typing the end command makes the VP leave | commands ignored / no intro — `onReceiveChatMsg` payload or `sendChat` changed |
| 6 | **Participant count / lonely-exit** | VP stays while others present; leaves when alone | VP leaves immediately ("alone in meeting") or never leaves — `onUserJoin`/`onUserLeave` or the DOM counter selector changed |
| 7 | **Voice assistant + Simli avatar** (only if enabled) | `[zoom-sdk] unmuted microphone`, `[zoom-sdk] started video` | avatar camera never turns on — `svg.SvgAudioUnmute` / `svg.SvgVideoOff` selectors changed |

**Selectors to grep if something breaks** (`src/zoom-sdk.ts` / `src/zoom-sdk-server.ts`):
`button.preview-join-button`, `svg.SvgAudioUnmute`, `svg.SvgAudioMute`,
`svg.SvgVideoOff`, `svg.SvgVideoOffDisallowed`, the participant-count DOM node,
and the `.min.js` / `/av` asset paths served from
`node_modules/@zoom/meetingsdk/dist/`.

> **Already verified statically (so these are low-risk):** SDK 6.2.0 still ships
> `zoom-meeting-6.2.0.min.js` (filename resolved dynamically from the package
> version), and all `dist/lib/vendor/*.min.js` + `/av/*.wasm` assets the embed
> page loads still exist at the expected paths.

---

## Step 4 — Python formatting/type follow-up (separate change, not in PR #400)

`black` 22→26 and `mypy` 0.95→2.3 were upgraded but **not applied**. Before/after
merging #400, run these and land the churn as its own commit:

```bash
cd lma-ai-stack
make lint-pylint lint-mypy   # mypy 2.3 may surface new type errors
# and a formatting pass (black 26 reformats differently than 22):
#   black <python dirs>   # review the diff before committing
```

---

## Sign-off

- [ ] Step 0 smoke test passes
- [ ] Step 1 test stack deploys clean
- [ ] Step 2 transcriber transcribes live audio, no task restarts
- [ ] Step 3 Zoom rows 1–6 pass (7 if voice/avatar enabled) **or** Zoom SDK not in scope
- [ ] Step 4 black/mypy follow-up scheduled
