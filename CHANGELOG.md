# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **"Meeting URL" field for URL-based joins** — In the Create Virtual Participant modal, entering a full `http(s)://` join URL relabels the field to **Meeting URL** and hides the separate **Meeting Password** field (the URL carries its own password). A numeric ID keeps the **Meeting ID** + password layout.
- **Per-platform stored browser profiles** — The VP's saved Chromium profile is now keyed per user *and* meeting platform, so a Zoom-authenticated session is never reused for Webex/Teams/Chime. The modal's **Stored browser profile** card shows/removes the profile for the selected platform. Existing pre-upgrade profiles aren't migrated (first join per platform starts fresh).
- **VP container build uses a larger CodeBuild compute type** (MEDIUM) for faster image builds.

### Fixed

- **Zoom "We detected you may be a bot" prejoin block now self-recovers** — Zoom occasionally renders a bot-detection modal in a closed Shadow DOM after the VP clicks Join; Puppeteer can't interact with it and the AI dialog watchdog can't reliably target it, so the VP previously sat on the prejoin screen until the 5-minute waiting timeout and exited "not admitted." The Zoom handler now waits 15s for quick admission and, **only if still stuck on the prejoin screen**, reloads the page (which clears the modal — it doesn't reappear), re-enters password + display name, and re-clicks Join before handing off to the standard admission poll. Meetings with a waiting room are unaffected: once the VP has left prejoin and is queued for host admission it skips the reload, so it's never bounced out of the waiting room.
- **Webex meetings joined via `j.php` launch links now work** — Pasting/using a Webex `https://SITE.webex.com/.../j.php?MTID=…` invite now joins reliably. The invite parser keeps the full launch URL as the Meeting ID (the `MTID` token has no extractable numeric ID), the VP navigates directly to it, clicks "Join from this browser", and dismisses Chromium's native "Open Webex.app?" app-launch prompt (via a real X11 keystroke — it's browser chrome that JS/CDP can't reach). Also handles the newer build's name/email pre-join form and avoids the "No camera found" popup that previously blocked the join.
- **VP startup status copy matches the hosting mode** — `WAITING_FOR_CAPACITY` / `INITIALIZING` no longer show EC2-only wording (host slots, auto-scaler) on Fargate deployments; copy is now launch-type-aware.
- **Pin `lma-sdk` to its in-repo path and mark internal npm packages private** — `uv` resolves `lma-sdk` from the repo rather than public PyPI, and internal packages (`docs-site`, VP backend, `pca_integration`) are marked private. See `lib/README.md` for the editable-install flow.

## [0.3.4] - 2026-06-03

### Added

- **Browser desktop notifications + audio chime for `MANUAL_ACTION_REQUIRED`** — When a Virtual Participant escalates to `MANUAL_ACTION_REQUIRED` (CAPTCHA, 2FA, SSO, unknown consent dialog) the LMA UI now fires a desktop notification (with `requireInteraction: true` so it stays visible until clicked) plus a short audio chime in addition to the existing in-page Flashbar. A 15-second debounce suppresses notifications for transient escalations that auto-clear within the window (e.g. a consent dialog the VP itself dismisses). The chime is generated via WebAudio to avoid shipping an audio asset. Browser notification permission is requested once at first visit; if the user denies, the in-page Flashbar still works. See [Web UI Guide → Manual Action Alerts](docs/web-ui-guide.md#manual-action-alerts).

- **Per-user Zoom sign-in for the Virtual Participant** — Each LMA user can now store their own Zoom credentials and have the VP sign in to Zoom before joining the meeting. Credentials live in AWS Secrets Manager keyed by Cognito sub (KMS-encrypted, plaintext password never returned to the UI), set via a new **Zoom account** card in the Create Virtual Participant modal and a new AppSync mutation surface (`getMyZoomCredentialsStatus` / `setMyZoomCredentials` / `deleteMyZoomCredentials`) backed by the new `zoom_credentials_manager` Lambda. The state machine resolves the secret name server-side and the VP container reads it at runtime — credentials are never put on the task definition or in the Step Functions execution input. A signed-in session joins far more reliably (the *"We detected you may be a bot"* guest block becomes rare) and lets the VP join meetings that disallow guests. See [Zoom Sign-in & Join Reliability](docs/zoom-credentials-and-join-reliability.md).

- **AI-driven Zoom sign-in loop** — Zoom's sign-in flow is no longer a single deterministic page-by-page script. After the username is submitted, the VP delegates each subsequent step to Claude (Bedrock, vision-capable) which decides whether to fill the password, skip a passkey/phone-binding upsell, click through an OTP/CAPTCHA challenge, wait for a loading state, escalate to `MANUAL_ACTION_REQUIRED`, or recognize that the dashboard is already showing. Same AI navigator handles the post-login interstitial sequence on the way to the meeting URL. This tolerates Zoom's frequent sign-in flow changes without code/deploy iterations.

- **AI DOM resolver fallback for all platform handlers** — Every platform handler (Zoom, Teams, Webex, Chime) now wraps its hard-coded CSS selectors in a fallback that asks Claude (Bedrock Haiku 4.5, vision-capable) to find the right element when a primary selector misses. Successful resolutions are cached in a shared `DomSelectorCache` DynamoDB table (30-day TTL) so the cost is paid only once per platform UI change. The same resolver classifies unknown popup dialogs: `CONSENT` / `RECORDING_NOTICE` are auto-dismissed; `CAPTCHA` / `LOGIN_REQUIRED` / `SSO_REDIRECT` / `BLOCKED` escalate to `MANUAL_ACTION_REQUIRED`. Disable by setting `BEDROCK_DOM_RESOLVER_MODEL_ID=""` in the task definition. Result: the VP self-heals when meeting platforms ship UI changes instead of silently failing.

- **Persistent per-user Chromium profile in S3** — The VP container now hydrates a per-user `userDataDir` from a new `VPProfilesBucket` S3 bucket at launch and uploads the modified profile back to S3 at meeting end. The whole `userDataDir` round-trips as a single `profile.tar.gz` keyed by `sha256(cognitoSub)` (one profile per user — all meeting platforms share state, so an SSO sign-in to office.com benefits a later Teams join). Once a user has signed in once and cleared any CAPTCHA / 2FA via the live VNC viewer, Zoom plants a "trusted device" cookie in the profile that persists across meetings — subsequent VP launches reuse the session and sign in cleanly without re-prompting. Last-write-wins on concurrent VPs for the same user (the loser just costs themselves session cookies and re-prompts next launch). Bucket is KMS-encrypted, public access blocked, versioned.

- **ECS capacity provider auto-scaling for the VP cluster** — The VP cluster now uses an `AWS::ECS::CapacityProvider` with `ManagedScaling` (TargetCapacity=100, step 1-2, instance warmup 90s) wired as the cluster's default strategy via a new `ClusterCapacityProviderAssociations` resource. RunTask drives `CapacityProviderStrategy` (instead of `LaunchType=EC2`), so when the existing hosts are full ECS automatically launches new hosts up to `VPMaxInstances` to absorb the queued task. New `WAITING_FOR_CAPACITY` VP status is written when a task is queued waiting for a host to come up — the UI shows a friendly "Waiting for compute capacity… auto-scaler is launching a new host (typically 60-90s)" message instead of a silent `INITIALIZING`. The EC2 container memory cap is 3500 MB (observed peak ~1650 MB with Chromium + Simli + Nova Sonic). A full voice + avatar meeting draws ~1.35 vCPU steady-state and runs reliably on the default `t3.medium`; size up only for more concurrent VPs per host.

- **AI sign-in hardening: existing-session fast-path, stuck-loop detector, cheap manual-action poll** — The AI-driven Zoom sign-in loop now has three new behaviors that materially reduce both Bedrock cost and failed sign-ins. **Existing-session fast-path**: before calling the full sign-in, probe `zoom.us/myhome` and check for the auth cookie — if the persistent S3 profile already has a valid session, the entire AI loop is skipped. **Stuck-loop detector**: if Claude returns the same action signature (kind + selector) twice in a row AND the page hasn't changed (URL + visible-element fingerprint), bail to `MANUAL_ACTION_REQUIRED` instead of burning the iteration budget on a dead-end (the previous behaviour was 17+ no-progress retries when a verification step was silently rejecting the form). **Cheap manual-action poll**: after escalating to `needs_human` the loop switches from "screenshot + Bedrock every 3 sec" to a free DOM-only auth-cookie poll every 5 sec for the rest of the timeout — no Bedrock spam while waiting for the human. Together these reduce typical sign-in cost from 4-20 Bedrock calls to 0 (returning user) or 4-8 (first sign-in) and prevent the runaway-loop pattern that previously degraded sign-in reliability.

- **Granular Virtual Participant startup statuses** — The VP now reports a fine-grained status as it boots: `BOOTING` (pushed from `entrypoint.sh` before Node even starts), `REGISTERING_NETWORK`, `HYDRATING_PROFILE`, `LAUNCHING_BROWSER`, `WARMING_PROFILE`, `VNC_READY`, `CONNECTING`, `JOINING`, `MANUAL_ACTION_REQUIRED`, `ACTIVE`. The UI exposes each as a distinct status badge with a human-readable summary, replacing the previous behaviour where the VP would sit at `INITIALIZING` for 60-90 seconds with no indication of progress. The live-view button only activates once `VNC_READY` is published, which is deferred until *after* fresh-profile warmup completes — so when a user clicks "Open live view" the meeting page is up rather than the warmup pass. The `get_virtual_participant_status` MCP tool verbalizes each state for AI agents. See [Virtual Participant → Status Lifecycle](docs/virtual-participant.md#status-lifecycle).

- **`MANUAL_ACTION_REQUIRED` UI alerts** — When a VP hits a CAPTCHA, 2FA prompt, SSO redirect, unknown consent dialog, or other Zoom verification step that needs human input, the LMA UI now surfaces it three ways: a persistent **Flashbar alert** at the top of every page (with a direct link to the affected meeting), an **action banner** above the live VNC viewer on the VP detail page, and (with permission) a **browser notification + audio chime** so users can be tabbed-away or in another window. Alerts are dismissible per-VP with `localStorage`-backed memory so they don't reappear on every refresh once handled. Backed by a one-time backfill query on page load so anything fired while the tab was closed still shows up. See [Web UI Guide → Manual Action Alerts](docs/web-ui-guide.md#manual-action-alerts).

- **Real-time Virtual Participant list updates** — The Virtual Participants list now subscribes to a new `onCreateVirtualParticipant` AppSync subscription, so newly-launched VPs (including ones launched from the MCP tools or another browser tab) appear in the list immediately without a page refresh.

- **`get_virtual_participant_status` MCP tool** — New built-in MCP tool lets the meeting assistant poll a VP it launched via `start_meeting_now`. Returns the granular status, a human-readable summary, the live VNC viewer URL, the meeting URL, and any `errorMessage` or `manualActionMessage` fields — so the agent can verbalize *"the VP is in the meeting"*, surface a CAPTCHA/2FA challenge to the user with a direct link to solve it, or report a clean failure reason without further prompting. `start_meeting_now` also now defaults to using the launching user's stored Zoom credentials when present (`useStoredZoomCredentials` defaults to `true`). See [MCP Servers → Built-in LMA MCP Tools](docs/mcp-servers.md#built-in-lma-mcp-tools).

- **MCP server returns LMA Web UI deep-links for every meeting** — All MCP tools that return meetings (`list_meetings`, `search_meetings`, `get_meeting_transcript`, `get_meeting_summary`, `start_meeting_now`, `schedule_meeting`) now include `meetingUrl` and `virtualParticipantUrl` fields pointing at the LMA Web UI's `#/calls/<id>` and `#/virtual-participant/<id>` routes, with the meeting ID URL-encoded so IDs containing spaces, `+`, or `:` resolve correctly. Powered by a new shared `tools/url_helper.py` that derives the base URL from the `LMA_WEB_APP_URL` env var (wired in via `lma-ai-stack.yaml`). Lets MCP clients (Claude/Quick Desktop/etc.) hand users one-click links straight into the Web UI instead of bare IDs.

- **`readOnlyHint` annotations on read-only MCP tools** — `search_lma_meetings`, `get_meeting_transcript`, `get_meeting_summary`, and `list_meetings` are now advertised with `annotations.readOnlyHint: true` per the MCP spec, so MCP clients can confidently invoke them without user confirmation prompts and surface them as safe/read-only in their UIs.

- **Amazon Quick Desktop Skills Pack** — New top-level `amazon-quick-desktop-skills-pack/` directory ships an LMA-for-Quick-Desktop integration bundle: two scheduled agents (`pre-meeting-brief` — runs every 5 min, builds AI briefs 15 min before calendar meetings using prior LMA meeting context and action items; `action-items` — hourly extraction of commitments from completed meetings) and two skills (`lma-meeting-catchup` — "catch me up on [meeting]" pulls the LMA summary + transcript and cross-references Slack; `lma-live-coach` — "coach me on this call" launches the LMA Virtual Participant and a background agent that polls the live transcript every 60s and posts MEDDPICC/SPIN/Challenger coaching cards). Includes a README with the bearer-token MCP setup flow and customization tips. Companion docs `docs/amazon-quick-mcp-setup.md` (consolidated setup for both Quick Suite OAuth and Quick Desktop API key) and `docs/amazon-quick-desktop-integration.md` (agent recipes) cover end-to-end install/troubleshooting and the agent prompts.

### Changed

- **Virtual Participant browser stack: Puppeteer → Playwright + CloakBrowser** — The VP container migrated from `puppeteer` to `playwright-core` driving [CloakBrowser](https://github.com/CloakHQ/cloakbrowser) (a patched Chromium build, pinned at 0.3.31) via `launchPersistentContext`. **Why Playwright:** the platform handlers were rewritten against Playwright's `Page`/`ElementHandle` API for a more robust join flow (time-bounded CDP calls, per-step verification, AI DOM-resolver fallback). **Why CloakBrowser:** its newer patched Chromium handles Zoom's web-client video encoder reliably even on a small 2-vCPU host (`t3.medium`) — the older stock Debian Chromium threw Zoom's "Something went wrong" error and turned the VP camera off under the same load. (A stock-Chromium build was trialed and reverted after a side-by-side on t3.medium confirmed the difference.) Two launch flags (`--force-webrtc-ip-handling-policy=default` + `--webrtc-ip-handling-policy=default`) restore local ICE candidate gathering so the Simli avatar's same-browser loopback video bridge connects; `--disable-gpu --disable-software-rasterizer` sheds the SwiftShader CPU cost (no hardware GPU in the container, and we inject our own camera / consume audio rather than rendering Zoom's video). CloakBrowser is `pip install`-ed with the binary pre-downloaded at build time (`python -m cloakbrowser install`). Fresh profiles run a short warmup (a few ordinary sites + the meeting-platform home pages) before the meeting URL so a first-time profile arrives with normal browsing history rather than zero state. Together with the 3p-cookie pre-write fix (Chrome v123+ default-blocks 3p cookies, breaking Zoom's cross-domain auth on a fresh profile) and the persistent S3 profile, fresh-profile Zoom joins are reliable.

- **Bigger image, smaller surface: `simli-client` v3 vendored into the image via esbuild** — The Simli avatar page now imports the `simli-client` ES module bundle from a fetch-intercepted in-image path (`http://local.simli/simli-client.bundle.mjs`) instead of loading it from a CDN at meeting time. The Dockerfile runs `esbuild` at build time to produce `dist/simli-client.bundle.mjs`, and `simli-avatar.ts` registers a Playwright route handler (`page.route`) that serves it from a synthetic `http://local.simli/` origin. Pinned local version is safer than the previous CDN dependency.

- **Robust clicks for the Sign-In / Join / Skip / chat controls** — the `humanClick()` helper drives meaningful UI clicks (Zoom Join, Sign-in, "Skip for now" interstitials, in-meeting chat-panel button) with `force:true` to skip Playwright's "covered by another element" actionability check (Zoom often floats a transient overlay over these buttons), and falls back to a direct DOM `element.click()` when a positional click can't land (e.g. the control is pushed off-viewport by a transient Zoom window). Audio/video SVG toggles use the `clickClickableAncestor()` helper since the SVG icon isn't itself the clickable target. Together these make the join clicks reliable across Zoom's shifting layouts.

### Fixed

- **Virtual Participant stuck in `INITIALIZING` on ECS RunTask soft-failure** — When ECS RunTask returned HTTP 200 but with a non-empty `failures` array (typically the container instance agent disconnected briefly during scheduling), the Step Functions state machine treated it as a successful launch and the VP record sat at `INITIALIZING` indefinitely with no error feedback to the user. The state machine now inspects `runTaskResult.failures[]` explicitly via a new `CheckRunTaskFailures` Choice state and routes to a `MarkVPFailed` Pass + Lambda invoke that writes `FAILED` plus the original failure reason to the VP DynamoDB record. The detail page surfaces the `errorMessage` in the troubleshooting card. Same `errorMessage` plumbing also surfaces clean failure reasons for Zoom-login bad-credentials, meeting-not-found, and Bedrock guardrail rejections.

- **Virtual Participant silently broke when Zoom shipped UI changes** — Hard-coded CSS selectors in the Zoom platform handler (`#input-for-pwd`, `svg.SvgAudioMute`, `.video-avatar__avatar-footer span`, `.audio-level-indicator`, `.zm-modal`, etc.) would silently miss when Zoom updated its meeting client, causing the VP to hang or fail with cryptic browser-automation errors and requiring a code+container release to fix. The new AI DOM resolver fallback (above) means the first meeting after a Zoom UI change pays the cost of a Bedrock call to rediscover the selector; every subsequent meeting hits the cache. The same self-healing applies to Teams, Webex, and Chime handlers.

- **VP host churn on newer ECS-optimized AMIs (May 2026+)** — The Amazon Linux 2 ECS-optimized AMI dropped its pre-installed `aws` CLI in mid-May 2026. The VP launch template's userdata had `aws ecr get-login-password ...` near the top with `set -e`, so on every new instance the script aborted at `aws: command not found` *before* `systemctl enable --now ecs` ran. The host registered with ECS via the auto-start service but failed its ECS health check minutes later, the ASG marked it unhealthy, and the cycle repeated every ~25 minutes — preventing any VP from running. Fixed by dropping `set -e`, adding a best-effort `yum install -y aws-cli` if the binary isn't present, and falling through to `systemctl enable --now ecs` regardless. The image-warm-pull behaviour is now best-effort (ECS still pulls the image at task launch via the task execution role).

- **ASG host-churn during stack updates** — The VP `AWS::AutoScaling::AutoScalingGroup` had no `UpdatePolicy`, so a CFN deploy that touched the launch template would drain the existing host while the new one was still booting + a stale ECS health check could mark the new instance unhealthy mid-rollout, leaving the cluster without a usable host for several minutes. Added `UpdatePolicy.AutoScalingRollingUpdate` with `SuspendProcesses: [HealthCheck, ReplaceUnhealthy, AZRebalance, AlarmNotification, ScheduledActions]` so health-check / replace / rebalance signals are paused during a rolling update. The capacity provider still launches new hosts on demand at runtime; this only affects how the ASG behaves during a stack update.

- **`list_meetings` MCP tool returned no rows for current-day meetings** — The DynamoDB GSI sort-key upper bound in `query_by_date_range` (`mcp_analytics/tools/list_meetings.py`) was built as `"ts#<end_iso>#~"`. Real SKs have the form `ts#<ISO8601>#id#<CallId>` (e.g. `ts#2026-05-27T17:34:…`), and `T` (0x54) sorts after `#` (0x23), so the `between(...)` query silently excluded every meeting whose SK had a `T` immediately after the date — which is *every* meeting whenever the caller didn't pass an explicit `end_date` past midnight (the default code path used a date-only end of `today.strftime("%Y-%m-%d")`). The upper bound is now `"ts#<end_iso>~"`; `~` (0x7E) sorts after every byte that can legally follow the date in the SK. Added a regression unit test (`tests/test_list_meetings_date_range.py`) that pins the exact `between` bounds via `botocore.stub.Stubber` so the bug can't silently return.

- **`make srt-clean` pre-scan cleanup** — New `scripts/srt/clean.py` + Make targets (`srt-clean`, `srt-clean-preview`, `srt-clean-checksums`) physically remove the build artifacts SRT's Bandit invocation can't be told to ignore (vendored Lambda-layer `python/` trees, `.aws-sam/`, `out/`, `node_modules/`, `dist/`, `build/`, `__pycache__/`, `.ash/`, intermediate scan JSONs). Preserves the SRT binary, scanner venv, `.srt/issues.json`, root `.venv`, `.env.local`, and `.checksum` cache. Scan time ~13 min → ~78 sec; open HIGH/CRITICAL count 190 → 0 after suppressing 7 reviewed findings whose rationale already existed for sister resources. Also expanded `.semgrepignore` to mirror `.ash.yaml`, and replaced the AWS-docs example placeholder credentials in `scripts/srt/setup.py` (used to satisfy `srt config` in CI) with non-AWS-format dummies. See [Security Scanning](docs/security-scanning.md).

- **CloudFront security headers** — Added a `ResponseHeadersPolicy` to the CloudFront distribution with Content-Security-Policy (restricted `connect-src` to AWS service origins), Strict-Transport-Security, X-Content-Type-Options, X-Frame-Options (DENY), and Referrer-Policy (strict-origin-when-cross-origin).

- **Unified LOG_LEVEL propagation** — The `LogLevel` CloudFormation parameter now propagates from the main stack to all nested stacks (AI, WebSocket Transcriber, Virtual Participant), all Lambda functions (via SAM Globals), and all ECS task definitions. Replaces previously hardcoded or missing log level configuration across 27 Lambda functions and 2 ECS services.

## [0.3.3] - 2026-05-15

### Added

- **Translator Mode for Nova Sonic voice assistant** — New `meetingMode = translator` turns the LMA virtual participant into a real-time bidirectional AI interpreter. Configure a language pair on the **Nova Sonic Configuration** page, deploy with `VoiceAssistantActivationMode = always_active`, and the VP will join Zoom, Teams, Chime, or Webex calls and speak each utterance back in the other language using a single polyglot Nova Sonic voice. Mid-meeting mute and unmute are built in via strict voice triggers — say **"translator mute"** (or **"alex mute"**) to silence translations during a side conversation or narration, and **"translator unmute"** (or **"alex unmute"**) to resume. The mute is implemented as Nova Sonic tools the model calls when it hears the trigger phrase; the system prompt encodes the MUTED/UNMUTED state machine so Nova stays silent across subsequent turns until explicitly unmuted, and the tool-result messages re-assert the state contract for long-horizon stability. Also ships **Group Meeting Mode** (`meetingMode = group`) as the formal replacement for the legacy `groupMeetingMode` boolean, with backward compatibility. The Nova Sonic Configuration UI gets a Meeting Mode selector and Translator Language A/B inputs, and warns when the deployment isn't in `always_active` mode. ElevenLabs has equivalent mute-gate parity. In Translator Mode the scribe also keeps the VP's spoken translations out of the Kinesis transcript so the recorded transcript stays faithful to what the humans actually said. See [Voice Assistant](docs/voice-assistant.md) and [Nova Sonic Setup](docs/nova-sonic-setup.md).

- **AI Translator Mode demo page** — New self-contained [`docs/embeddable-translator-demo.html`](docs/embeddable-translator-demo.html) demonstrates Translator Mode end-to-end inside an AWS-branded demo page with a 31-language picker, meeting form, and a 2x2 grid of LMA iframes (live VNC view, meeting details, transcript, summary) driven via `postMessage`. See [Embeddable Components](docs/embeddable-components.md).

- **Custom date-range for the meeting list + scalable GSI-backed query** — The meeting list is now scoped by an explicit start/end datetime instead of a client-side shard fan-out. New `listCallsDateRange` and `getCallCount` AppSync queries run a single GSI query backed by a new `TypeDateIndex` (populated by a `createCall` VTL stamp on new rows and a Step Functions Distributed Map backfill for existing rows), enforce Owner/`SharedWith` RBAC server-side, and `BatchGetItem` the call detail rows so the client gets fully-hydrated `Call` objects in one round-trip. The UI adds a **Custom…** option in the Load dropdown with explicit Start/End date+time inputs (UTC, ±365-day window), preset shortcuts at 2h / 4h / 8h / 1d / 2d / 1w / 2w / 30d, and persists the chosen range to `localStorage`. The list header shows the RBAC-filtered server-side total. The MCP `list_meetings` tool also switched to the same GSI path. Result: every meeting-listing path avoids DynamoDB scans and scales to tens of thousands of meetings with 1000+ concurrent users. See [Web UI Guide](docs/web-ui-guide.md).

- **Virtual Participant embed demo page** — New self-contained [`docs/embeddable-vp-demo.html`](docs/embeddable-vp-demo.html) shows the Virtual Participant flow end-to-end: a `component=vp-loader` iframe creates the VP via `postMessage`, emits `LMA_VP_CREATED` and streaming `LMA_VP_STATUS_CHANGED` events to the parent, and a 2×2 grid of dependent `vnc` / `vp-details` / `transcript` / `summary` iframes auto-refresh as the VP progresses through `INITIALIZING → CONNECTING → JOINING → ACTIVE`. Adds a `simple=true` query param on `vp-loader` for a compact "Meeting name / Status / vpId + End button" card variant. See [Embeddable Components → Virtual Participant Demo](docs/embeddable-components.md#virtual-participant-demo-page).

- **`lma vp` CLI commands and `client.vp` / `client.appsync` SDK namespaces** — The `lma` CLI gains a Virtual Participant command tree (`lma vp create / get / end / list`) for launching and managing VPs directly from the terminal using SigV4 (no Cognito login required). Backed by two new SDK operation namespaces: `client.vp` (`create`, `get`, `end`, `list`, `wait_for_launch`) which mirrors the Web UI's `EmbedVpLoader` lifecycle (AppSync `createVirtualParticipant` → `StartSyncExecution` on the VP scheduler Step Function → poll until status leaves `INITIALIZING`, with best-effort cleanup of orphan rows on failure), and `client.appsync` (a SigV4-signed `graphql()` helper that auto-resolves the AppSync URL from the stack's CloudFormation outputs). New Pydantic models (`VpRow`, `VpLaunchResult`, `VpStatus`) and exceptions (`LMAAppSyncError`, `LMAVirtualParticipantError`) round out the public API. See [LMA CLI → `lma vp`](docs/lma-cli.md#lma-vp--virtual-participant-operations) and [LMA SDK → `client.vp`](docs/lma-sdk.md#clientvp--virtual-participant-operations).

- **`lma load` Load Simulator (CLI plugin) and `lma_cli.plugins` extension mechanism** — New [LMA Load Simulator](utilities/load-simulator/README.md) (`utilities/load-simulator/`) lets you stress-test a deployed LMA stack against four scenarios: `lma load concurrent` (N in-flight meetings via a selectable driver: `kinesis` for synthetic segments, `upload` for batch Transcribe, `websocket` for real streaming Transcribe, or `vp` for Virtual Participants joining real meetings); `lma load backfill` (fabricate N historical meetings spread over Y days, with optional real-Cognito-user provisioning for RBAC-at-scale testing of the meeting-list paginator and `listCallsDateRange` GSI); `lma load rbac` (provision N synthetic users + latency-sweep `listCalls`/`getCallCount` under RBAC); and `lma load cleanup` (deterministic teardown of every synthetic resource — meetings, VPs, Cognito users, S3 orphans — by `--run-id`). Production-stack guard (`prod` in stack name → refused without `--force`), CloudWatch dashboard, cost/quota awareness, and `<results-dir>/cognito-users.json` credential capture. The simulator is exposed as `lma load …` via a new **`lma_cli.plugins` entry-point group** that the main CLI auto-discovers — third parties can use the same mechanism to add their own subcommand trees (`[project.entry-points."lma_cli.plugins"]\nmything = "my_pkg.cli:cmd"`). The simulator also runs standalone as `lma-load <subcommand>`. See [LMA CLI → `lma load`](docs/lma-cli.md#lma-load--load-simulator-plugin) and [CLI Plugins](docs/lma-cli.md#cli-plugins).

### Fixed

- **Simli avatar reliability with Zoom mid-meeting page transitions** — Replaced the 1-second background reconnect poll, which fought against Zoom's own media-pipeline lifecycle and produced a self-sustaining reconnect storm and frequent "no camera" states, with on-demand reconnect via Playwright's `page.exposeFunction`. The meeting-page `getUserMedia` override now requests a fresh WebRTC bridge inline only when the avatar track is actually missing, ended, or muted, returns `track.clone()` so Zoom can call `stop()` on its handle without killing our source, and includes `track.muted === false` in the liveness check so a still-buffering track is never handed to Zoom. The override also no longer falls through to the real (empty) camera device when Zoom's size-constraint probes don't match the avatar's native dimensions.

- **Virtual Participant failed to join when `TranscribeLanguageCode` is set to `identify-language` or `identify-multiple-languages`** — The VP scribe was passing the sentinel string into Amazon Transcribe streaming's `LanguageCode` field, which only accepts real ISO codes, so every session was rejected with a validation error and the VP exited via emergency cleanup. The scribe now maps the two sentinels to the correct SDK shape (`IdentifyLanguage` / `IdentifyMultipleLanguages` plus `LanguageOptions` and optional `PreferredLanguage`, with whitespace stripped from the language list to match the service's regex constraint). The transcription retry loop now short-circuits on validation/`BadRequestException`/HTTP 400 errors instead of burning 5 retries, and `ERR_STREAM_PREMATURE_CLOSE` during a deliberate teardown is no longer treated as fatal.

- **MCP API Gateway stage deployment failure in brand-new AWS accounts (complete fix)** — The initial fix removed only `MethodSettings.LoggingLevel: INFO` from `MCPApiKeyStage`, but the same account/region-wide `AWS::ApiGateway::Account.CloudWatchRoleArn` singleton is *also* required by REST API Gateway whenever `AccessLogSetting` is configured on a stage. In untouched accounts/regions the stage still failed to create with `CloudWatch Logs role ARN must be set in account settings to enable logging`. `AccessLogSetting` has now been removed from `MCPApiKeyStage` and the unused `MCPApiKeyAccessLogGroup` log group has been deleted, so the stage (and its parent nested stack `AISTACK`) now deploy cleanly into fresh accounts. Customers who want per-request MCP access logs can enable them manually after deployment: (1) create an IAM role trusted by `apigateway.amazonaws.com` with the `AmazonAPIGatewayPushToCloudWatchLogs` managed policy; (2) call `apigateway:UpdateAccount --cloud-watch-role-arn <role-arn>` once per region; (3) re-add `AccessLogSetting` on the MCP stage. CloudWatch request metrics on the stage (`MetricsEnabled: true`) are unchanged and continue to work without any account-level prerequisite.

- **`vp-loader` embed reliability and rendering** — Several fixes so the `component=vp-loader` iframe works reliably alongside the other VP embeds. The `onUpdateVirtualParticipant` AppSync subscription auto-resubscribes with capped exponential backoff after Amplify closes the WebSocket for token refresh; subscription merges no longer overlay `null` over previously-populated fields; `endVirtualParticipant` mutations include `endReason` and `endedBy`; `createVirtualParticipant` is followed immediately by a `getVirtualParticipant(id)` so the shared `VPConnectionDetails` render has all server-populated fields from the first paint; and the custom 2×2 "Virtual Participant Created" confirmation grid was replaced with the shared `StatusDetails` + `ConnectionDetails` components from `VirtualParticipantDetails.jsx`.

- **Web chat assistant failed to retrieve the transcript for meetings whose CallId contains `+`** — the chat iframe `src` interpolated `callId` into the query string without URL-encoding, and `URLSearchParams.get("callId")` then decoded the literal `+` as a space. Both chat iframe `src` assignments (`CallPanel.jsx` and `EmbedCallDetails.jsx`) now wrap the CallId with `encodeURIComponent`, restoring transcript retrieval for meeting IDs with `+` and any other URL-reserved characters.

- **Transcript / summary embeds showed "Error Loading Meeting" when loaded before the `callId` exists in DynamoDB** — `EmbedCallDetails.loadCall` returns `[null]` for not-yet-created calls, which then crashed inside `mapCallsAttributes` and hit the hard-error branch, masking the intended "Waiting for meeting to start..." state and blocking the AppSync `onCreateCall` auto-refresh. Null entries are now filtered before `mapCallsAttributes`, and "not found / does not exist / null / unauthorized" errors are classified as expected races so the subscription auto-load path takes over.

- **Slow bulk meeting delete from the UI** — the `meeting_controls_resolver` Lambda previously deleted meetings one at a time, and within each meeting deleted transcript segments and individual S3 objects serially — so deleting N meetings scaled roughly `N × (M_segments + S_s3_objects)` sequential round-trips. The resolver now (1) parallelises the per-meeting loop at `DELETE_MEETING_CONCURRENCY` (default 8), (2) parallelises transcript-segment deletes at `DELETE_SEGMENT_CONCURRENCY` (default 16) and runs them alongside VP cleanup, `getCall`, and S3 cleanup as four concurrent preparation steps, (3) replaces the per-object S3 `DeleteObject` loop with `DeleteObjects` batches of up to 1000 keys, and (4) expands the underlying urllib3 pool so concurrent AppSync calls don't serialise on the default 10-connection cap. Both concurrency dials are env-var tuneable. Bulk-delete of a 20-meeting selection goes from tens of seconds to a few seconds in typical runs.

## [0.3.2] - 2026-04-29


### Added

- **Virtual Participant local development workflow** — First-class `make vp-*` targets wrap the existing `lma-virtual-participant-stack/backend/local-test.sh` so developers can run the VP container locally against a deployed LMA stack the same way `make ui-start` runs the UI dev server. New targets: `vp-start` (build + run), `vp-start-dev` (`DEV=1` — mounts `src/` with auto-reload on TypeScript changes), `vp-start-reuse` (`REUSE_ENV=1` — preserves manually-added secrets like `ELEVENLABS_API_KEY` / `SIMLI_API_KEY` in `.env.local` between runs), plus `vp-stop`, `vp-logs`, and `vp-shell`. New [Virtual Participant Local Development](docs/virtual-participant-local-dev.md) doc covers the recommended EC2 + VSCode Remote-SSH + TigerVNC/noVNC workflow (most production-like since the VP's Chromium/audio/Playwright stack is Linux-specific), the `--reuse-env` secret-handling flow, and the VSCode stale-port-forwarding gotcha where TigerVNC hangs on `localhost:5900` until stale 5900/5901 forwards are deleted and re-created. Cross-linked from `docs/virtual-participant.md`, `docs/developer-guide.md` (replacing an outdated manual `docker run --env …` snippet), and `docs/INDEX.md`.

- **Upload Audio (pre-recorded files)** — New **Sources → Upload Audio** page lets users transcribe existing audio/video files. The file is uploaded directly from the browser to S3 via a presigned URL, Amazon Transcribe (batch) runs on it, and the meeting appears in the Meetings List with the usual summary — same downstream pipeline as live streaming. Ships a 3-stage Lambda pipeline (`upload_meeting_initiator` mints the presigned URL, `upload_meeting_processor` triggers on S3 `ObjectCreated` to emit a Kinesis `START` and kick off the Transcribe job, `upload_meeting_finalizer` handles the Transcribe `COMPLETED`/`FAILED` EventBridge event to emit transcript segments + `END` and promote the media to the long-term recordings prefix), a new `createUploadMeeting` AppSync mutation, and a matching `EmbedUploadAudio` / `EmbedSelectAudio` variant so hosts can iframe the uploader. See [Upload Audio](docs/upload-audio.md).

- **Meeting Sources comparison doc** — New [Meeting Sources](docs/meeting-sources.md) parent doc compares the three capture options (Chrome Extension, Stream Audio from Mic+Browser, Virtual Participant) in one place with an at-a-glance capability table and concise "when to use each" guidance. Each source-specific doc now has a prominent pointer to this doc, and the right-side Info panels for all three Sources pages in the Web UI include a compact "Meeting Sources comparison" link for quick discovery.

- **User Management (admin only)** — New **Configuration → User Management** page lets Admin users list, create, and delete LMA users (Admin or User role) directly from the Web UI — no AWS console needed. New users receive a Cognito-sent invitation email with a temporary password and a clickable link to the LMA Web UI (CloudFront URL), injected at send-time by a new Cognito `CustomMessage` Lambda trigger that reads the URL from the LMA Settings SSM parameter. Secure by design with three-layer authorization: AppSync schema `@aws_cognito_user_pools(cognito_groups: ["Admin"])`, defense-in-depth `cognito:groups` re-check in the new `user_management` Lambda resolver, and UI route/nav guards. The Lambda's IAM role is scoped to the LMA Cognito User Pool ARN with only the required `cognito-idp` admin actions. Guard rails prevent self-delete and deletion of the last remaining Admin, and honor the existing `AllowedSignUpEmailDomain` parameter. See [User Management](docs/user-management.md).

- **Optional WAF for the MCP API Gateway** — New `WAFAllowedIPv4Ranges` CloudFormation parameter (default `0.0.0.0/0` = disabled, backwards compatible). When set to a comma-separated list of restricted CIDRs, LMA creates a **REGIONAL WAFv2 WebACL** and associates it with the MCP REST API Gateway stage (when `EnableMCPServer=true`), letting customers IP-restrict the headless/programmatic MCP endpoint.

- **Delete Virtual Participants from the UI** — New **Delete** action on the Virtual Participants list supports single and bulk deletion with a typed-confirm modal. Active or scheduled VPs (`SCHEDULED`, `INITIALIZING`, `CONNECTING`, `JOINING`, `JOINED`, `ACTIVE`) are ended server-side first — ECS task stopped, ALB target group/listener/rule cleanup, EventBridge schedule cancelled — before the DynamoDB record is removed, so deletes never leave orphaned infrastructure. Adds a new `deleteVirtualParticipants` AppSync mutation, AppSync JS resolver, and `deleteVirtualParticipants` operation in the `virtual_participant_manager` Lambda (with per-ID error handling and a human-readable result summary). Deletion actor is captured from Cognito claims for the end-VP audit trail.

- **Embeddable components demo page** — New self-contained `docs/embeddable-components-demo.html` demonstrates the LMA embed API (`stream-audio` with `postMessage` Start/Stop + live event log, and a multi-panel `summary`/`chat`/`transcript` dashboard) inside a mock parent app — useful for docs/blog screenshots and validating embed deployments. **[▶ Open the live demo](https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant/embeddable-components-demo.html)** (hosted on the LMA docs site). See [Embeddable Components](docs/embeddable-components.md#interactive-demo-page).

### Fixed

- **Meeting detail links broken for IDs with special characters** — Meeting IDs containing spaces, `#`, `?`, `/`, or other URL-reserved characters caused "Open meeting details", breadcrumbs, Meetings Query Tool result links, Stream Audio "Open recorded meeting", embed buttons, and Virtual Participant "View Call Details" links to 404 or truncate the ID. All meeting-detail hrefs now wrap `callId` in `encodeURIComponent(...)`. Affects `call-details/breadcrumbs.jsx`, `call-list/calls-table-config.js`, `embed/EmbedMeetingLoader.jsx`, `embed/EmbedStreamAudio.jsx`, `meetings-query-layout/MeetingsQueryLayout.jsx`, `stream-audio/StreamAudio.jsx`, and `virtual-participant-layout/VirtualParticipantDetails.jsx`.

### Changed

- **Chrome Extension nav link** — The side-nav "Download Chrome Extension" link no longer triggers an immediate zip download. It now opens a new **Chrome Extension** page with an overview, a version-stamped Download button, step-by-step install instructions, usage steps, and a Troubleshooting section. The right-side information panel is populated with features, requirements, and documentation links. A compact "Meeting Sources comparison" link in the info panel (also added to the Stream Audio and Virtual Participant info panels) takes users to the new [Meeting Sources](docs/meeting-sources.md) doc for a side-by-side of the three capture options. Also renamed the nav items "Virtual Participant (Preview)" → "Virtual Participant" and "Stream Audio (no extension)" → "Stream Audio (from Mic+Browser)" for clarity. See [Browser Extension](docs/browser-extension.md).

- **UI modernization** — Migrated web UI from Create React App to **Vite 7**, upgraded React Router from v5 to **v6**, completed Cloudscape rebrand (`@awsui/*` → `@cloudscape-design/*`), upgraded AWS Amplify to **v6**, and aligned all AWS SDK packages to `^3.637.0`.

## [0.3.1] - 2026-04-17

## Added

- **MCP Server API Key Authentication** — Users can now generate personal API keys from the LMA UI (MCP Servers Configuration page) for headless/programmatic MCP client access. Keys authenticate via a REST API Gateway with a Lambda REQUEST authorizer (SHA-256 hashed at rest in DynamoDB). The API key endpoint implements the full MCP JSON-RPC 2.0 streamable HTTP protocol (initialize, tools/list, tools/call, ping), so standard MCP clients — including Amazon Quick Suite via bearer token — can connect directly without OAuth. One key per user, revocable from the UI, with `lma_` prefix for leak detection. See [MCP API Key Authentication](docs/mcp-api-key-auth.md).

- **Browser Extension restored** — Re-added the Chrome browser extension for streaming meeting audio directly from the browser, restored by popular demand.

- **CloudFormation Service Role** — New deployable CloudFormation template (`iam-roles/cloudformation-management/`) that creates a delegated service role for non-admin LMA deployment. Administrators deploy the role once; developers then use `lma-cli deploy --role-arn` or the CloudFormation console to deploy LMA without needing admin permissions. See [CloudFormation Service Role guide](docs/cloudformation-service-role.md).

- **LMA CLI & SDK** (`lma-cli`, `lma-sdk`) — New Python CLI and SDK for building, deploying, and managing LMA stacks from the command line. Key commands: `lma deploy` (auto-selects public template by region, `--from-code` for build+deploy, `--wait` with real-time event streaming, `--admin-email` for new stacks), `lma publish` (build and upload artifacts to S3 with change detection), `lma status/outputs/delete/logs`. See [LMA CLI Reference](docs/lma-cli.md).

- **Documentation Overhaul** - Updated documents reflect new features and remove deprecated feature references. See ./docs.

- **Information panels populated across all UI pages** — The Cloudscape Information (help) panel on every page now includes a brief feature summary and links to relevant documentation on the GitHub docs-site. Previously most pages had sparse or missing content; now Meetings List, Meeting Details, Stream Audio, Virtual Participant, Meetings Query Tool, MCP Servers, Nova Sonic Config, and Transcript Summary Config all have enriched panels. Also fixed: Stream Audio had duplicate text, Virtual Participant incorrectly reused the Stream Audio panel.

- **Documentation Site** — New Starlight-based docs site deployed to GitHub Pages. Built with Astro and auto-synced sidebar from `docs/INDEX.md`. Key Makefile targets: `make docs-build`, `make docs-dev`, `make docs-deploy`. View at: https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant/

- **Root Makefile** — New developer-facing `Makefile` with `make help` for easy discovery. Key targets:
  - `make setup` — sets up dev environment (nvm Node v20, Python `.venv` with lint tools)
  - `make lint` / `make fastlint` — cfn-lint on all CloudFormation templates, pylint/flake8/black on Lambda functions, ESLint on UI
  - `make format` — auto-format Python with black
  - `make build` / `make build-ui` / `make build-websocket` / `make build-vp` — build all or individual stacks
  - `make test` — run React UI tests
  - `make ui-start STACK_NAME=<name>` — auto-generate `.env` from CloudFormation stack outputs and start local UI dev server
  - `make publish BUCKET=<b> PREFIX=<p> REGION=<r>` — build and upload all artifacts to S3
  - `make version V=x.y.z` — update VERSION file
  - `make commit` / `make fastcommit` — lint, test, commit, and push
  - `make clean` / `make clean-all` — clean build artifacts and node_modules

- **LocalUITestingEnv output** — added to `lma-main.yaml` as passthrough from AI stack, enabling `make ui-start` to auto-configure `.env` for local UI development
- **`.nvmrc`** — pins Node.js v20 for consistent development environments

## Fixed

- **Zoom VP: auto-dismiss recording consent and language popups** — Virtual Participant now automatically dismisses Zoom popup dialogs (recording consent, language interpretation) that appear on join or mid-meeting, preventing the VP from being disconnected after ~15-20 seconds. Uses a MutationObserver-based handler that only targets modal overlays with consent-related text.
- **Zoom VP: meeting-end detection no longer falsely triggered by popups** — Replaced `waitForSelector` with text-content–aware `waitForFunction` to distinguish the "meeting has been ended" dialog from recording consent popups, which share the same button selector.

## Changed

- **macOS Apple Silicon support for publish/deploy** — `lma-cli publish` and `lma-cli deploy --from-code` now work on macOS (including Apple Silicon). The `lma-ai-stack` Makefile skips the Linux-only QEMU multiarch setup on macOS since Docker Desktop handles x86_64 emulation natively via Rosetta.

- **Bedrock Model Updates** — Removed deprecated Claude Sonnet 4 (`us.anthropic.claude-sonnet-4-20250514-v1:0`, `global.anthropic.claude-sonnet-4-20250514-v1:0`) from AllowedValues in response to Anthropic's deprecation notice (Legacy July 14, 2026; EOL October 14, 2026). Added Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`, `global.anthropic.claude-sonnet-4-6`), Claude Opus 4.6 (`us.anthropic.claude-opus-4-6-v1`, `global.anthropic.claude-opus-4-6-v1`), and `us.amazon.nova-2-lite-v1:0` as new model options. Default model (`global.anthropic.claude-haiku-4-5-20251001-v1:0`) is unchanged.

## [0.3.0] - 2026-04-09

### Added
- **STRANDS_BEDROCK_WITH_KB (Use Existing)** — new MeetingAssistService option to use an existing Bedrock Knowledge Base with the Strands agent
- **Bedrock Guardrails for Strands agent** — `BedrockGuardrailId` and `BedrockGuardrailVersion` parameters to apply guardrails to the meeting assistant
- Shared navigation component (`navigation-items.js`) — all layout navigation files now use a single source of truth for consistent nav items, ordering, Configuration section, and Deployment Info
- Simli avatar integration for Virtual Participant animated lip-synced avatar as the VP's camera feed in meetings, driven by voice assistant audio output (Nova Sonic or ElevenLabs). Configure with Simli API Key and Face ID in CloudFormation parameters.
- Wake phrase pre-connect optimization for voice assistant — detects wake phrase in partial (streaming) transcripts and pre-warms the voice agent connection (WebSocket for ElevenLabs, Bedrock session for Nova Sonic) in the background while the user is still speaking, eliminating 1-2 seconds of connection latency. Activation now triggers immediately when the Transcribe segment completes instead of waiting a fixed 3-second capture delay.
- Compute-optimized EC2 instance types (c5, m5) for Virtual Participant — recommended for voice assistant + Simli avatar workloads requiring sustained CPU performance

### Changed
- **Consolidated on Strands Bedrock agent** — MeetingAssistService options simplified to `STRANDS_BEDROCK`, `STRANDS_BEDROCK_WITH_KB (Create)`, and `STRANDS_BEDROCK_WITH_KB (Use Existing)`
- **CFT form reorganized** — VP Startup Optimization moved to 2nd position, Voice Assistant split into own group, MCP Server renamed to "LMA Hosted MCP Server", removed deprecated parameter groups
- **Left nav Sources order** — Virtual Participant now listed first (VP → Stream Audio → Chrome Extension)
- **Deprecated old models** — removed Claude 3.x from model selectors; only Claude 4+ and Nova models remain
- Virtual Participant audio and avatar performance improvements — persistent audio playback stream (replaces per-chunk process spawning), WebSocket bridge for Simli avatar audio delivery (replaces CDP round-trips), and tuned PulseAudio buffering to eliminate audio glitches and lip-sync drift on smaller instances

### Removed
- **QnABot** — removed QNABOT nested stack, QnABot submodule (`.gitmodules`), all `qna_*` Lambda handlers, demo data files (`qna-ma-demo.jsonl`, `qna-ma-healthcare-demo.jsonl`), QnABot READMEs, QnABot build from `publish.sh`, and all QnABot-related CFT parameters/conditions/outputs
- **Amazon Lex** — removed all Lex code paths from async_agent_assist_orchestrator, `lex_utils` Lambda layer, Lex env vars (`LEX_BOT_ID`, `IS_LEX_AGENT_ASSIST_ENABLED`), `IsLexAgentAssistEnabled` parameter, `TranscribeToLexLocaleId` mapping, Lex Web UI scripts and assets, `lex:RecognizeText` IAM permissions, AgentAssistBot Cognito Identity Pool and IAM roles
- **Bedrock Agent stack** — removed BEDROCKAGENT nested stack and build
- **Q Business** — removed all Q Business parameters, Lambda/IAM/DynamoDB/KMS resources from meetingassist-setup stack
- **Healthcare domain** — removed `Domain` parameter and `IsHealthcareDomainSelected` condition
- **OpenSearch Serverless** — removed from vector store allowed values (S3 Vectors only)
- **S3 config parameters** — `S3BucketName`, `AudioFilePrefix`, `TranscriptFilePrefix` removed from CFT form (hardcoded to defaults)
- **Vector store parameters** — removed from CFT form (hardcoded to `S3_VECTORS`)

### Fixed
- Virtual Participant ECS task crash leaving meeting permanently stuck as "in progress" due to missing cleanup on uncaught transcription pipeline errors
- Audio glitches and Simli avatar sync issues on smaller EC2 instances caused by CPU contention from process spawning overhead and aggressive PulseAudio buffering

## [0.2.30] - 2026-03-27

### Fixed
- Strands Meeting Assist Lambda failing with `No module named 'pydantic_core._pydantic_core'` on new stack deployments due to cross-platform pip install in publish.sh
- MCP Layer CodeBuild matching Lambda functions from other stacks in the same account

### Added
- WebSocket Audio Streaming API specification (`docs/WEBSOCKET_STREAMING_API.md`)
- Automatic MCP layer rebuild on stack create/update via CloudFormation Custom Resource, ensuring correct native binaries and preserving installed MCP servers across updates

## [0.2.29] - 2026-03-20

### Added
- Admin UI pages for Nova Sonic voice assistant configuration (`/#/configuration/nova-sonic`) and Transcript Summary prompt templates (`/#/configuration/transcript-summary`) - admins can now view defaults and edit custom overrides directly from the web UI instead of navigating to the DynamoDB console
- Full-stack AppSync GraphQL API for Nova Sonic config and LLM prompt template CRUD operations with Lambda resolvers implementing input validation and security filtering (allowlisted fields only)
- Embeddable component page (`/#/embed`) for iframe integration - third-party apps can embed individual LMA components (stream audio, transcript, summary, chat, VNC, virtual participant) in their own UI via URL query parameters
- PostMessage API for cross-origin iframe auth token passing and meeting lifecycle control (start/stop/events)
- Embed integration documentation (`docs/EMBED_COMPONENTS_SETUP.md`) with examples, auth options, and API reference
- Configurable turn-taking sensitivity (endpointingSensitivity) for AWS Nova Sonic 2 voice assistant - supports HIGH (1.5s), MEDIUM (1.75s, default), and LOW (2.0s) pause detection for controlling response timing
- Chat shortcut buttons now re-appear inline after each assistant response completes, so users no longer need to scroll back to the top to access them
- Add and delete buttons in Edit Chat Buttons modal - admins can now add new shortcut buttons and remove existing ones, not just edit placeholders
- Group meeting mode (groupMeetingMode) for AWS Nova Sonic 2 - enables passive listening with mute/unmute tools, allowing Nova to only respond when directly addressed (mentions "Alex"), ideal for multi-participant meetings
- Barge-in support for AWS Nova Sonic 2 - separate audio routing prevents feedback loops and enables interrupting Nova mid-sentence
- Display scheduled execution time on Virtual Participant details page when status is SCHEDULED
- DeepWiki auto-indexing badge in README for automatic weekly documentation refresh and AI-powered repository Q&A

### Changed
- AWS Nova Sonic 2 audio routing architecture - separate sinks for meeting audio and agent output with combined monitoring for transcription, enabling barge-in without feedback loops
- Chat shortcut buttons made more compact with smaller padding and font size for better space efficiency

### Fixed
- AWS Nova Sonic 2 stream error recovery - session now automatically refreshes on unexpected stream errors instead of waiting for next scheduled refresh, preventing prolonged connection loss
- Scheduled Virtual Participants failing to start due to missing KMS permissions on VPScheduler Lambda role
- Edit Chat Buttons modal label input losing focus after each keystroke - fixed by using stable sequence number as React key instead of the changing button key

## [0.2.28] - 2026-03-13

### Added
- AWS Nova Sonic 2 session refresh for continuous conversation beyond 8-minute timeout in `always_active` mode
- Keep-alive mechanism (30-second silence chunks) to prevent 55-second inactivity timeout
- Conversation history capture from Nova's ASR transcripts (both USER and ASSISTANT turns)
- Conversation history passing during session refresh to maintain context across sessions
- Queued refresh execution that waits for agent to be idle (not speaking or processing tools)
- Customizable prompt support for Amazon Nova Sonic voice assistant via DynamoDB configuration with three modes (base, inject, replace) and voice ID selection

### Changed
- AWS Nova Sonic 2 now supports unlimited conversation duration in `always_active` mode with automatic session refresh every 5 minutes
- CloudFormation VoiceAssistantActivationMode parameter description updated to clarify 8-minute limitation for `wake_phrase` mode
- Enhanced security posture with comprehensive DSR review fixes including KMS permissions for custom resource Lambda functions, CloudWatch Logs encryption, and DynamoDB encryption
- Renamed "aws_nova" to "amazon_nova_sonic" throughout codebase for better clarity and AWS naming consistency
- Updated AWS Nova Sonic 2 documentation with customization guide including prompt modes, voice ID selection, and DynamoDB configuration examples

### Removed
- Amazon Nova 2 Pro model support

### Fixed
- Virtual Participant stack deployment failure when StrandsLambdaArn parameter is not provided - IAM policy now conditionally includes entire StrandsLambdaPolicy instead of creating empty Resource array
- AWS Nova Sonic 2 8-minute session timeout - sessions now automatically refresh before timeout
- AWS Nova Sonic 2 55-second inactivity timeout during long agent responses or silence periods
- AWS Nova Sonic 2 context loss after session refresh - conversation history now maintained
- AWS Nova Sonic 2 model confusion after session refresh - proper context prevents tool usage issues
- Fresh CloudFormation stack deployment failures for LLMStorePromptTemplates and ChatButtonStoreConfig custom resources - added missing KMS permissions (kms:Decrypt, kms:GenerateDataKey, kms:DescribeKey) to Lambda execution roles for accessing KMS-encrypted CloudWatch Logs and DynamoDB tables

## [0.2.27] - 03/03/26

### Fixed
- AWS Nova Sonic 2 voice assistant session management during tool use - session now stays open while tools process and audio plays
- AWS Nova Sonic 2 async tool processing - tools now execute in background without blocking response stream, allowing Nova to remain responsive during tool execution
- AWS Nova Sonic 2 pre-tool acknowledgment - implemented confirmation-based prompting strategy where Nova announces "Let me search for that information. This may take a moment." before calling tools, setting proper user expectations for sub-agent processing time

## [0.2.26] - 02/23/26

### Added
- Voice assistant integration for Virtual Participant with ElevenLabs Conversational AI and AWS Nova Sonic 2 support
- Multi-provider voice assistant architecture with factory pattern (elevenlabs, amazon_nova_sonic, none)
- Wake phrase activation mode with configurable phrases and duration for voice assistant
- Strands agent tool integration for both voice providers (meeting history, document search, web search)
- Voice assistant CloudFormation parameters (VoiceAssistantProvider, VoiceAssistantActivationMode, VoiceAssistantWakePhrases, VoiceAssistantActivationDuration)
- PulseAudio virtual microphone for agent audio routing in Virtual Participant
- Microphone activity monitoring for Zoom speaker detection when VP is speaking
- ElevenLabs Voice Assistant setup documentation

### Changed
- Virtual Participant microphone now stays unmuted when voice assistant is enabled (Zoom, Teams, Chime, WebEx)
- Zoom speaker detection enhanced with previous speaker tracking and microphone activity monitoring
- Local test script now supports --reuse-env flag for faster iteration

### Fixed
- CloudFormation stack deletion failure when EnableDataRetentionOnDelete=false - S3 buckets (LoggingBucket and RecordingsBucket) are now automatically emptied and deleted by custom resource with retry logic to handle async CloudFront log writes
- Zoom speaker detection not updating when VP voice assistant is speaking
- Local test script syntax error (missing proper indentation in SKIP_ENV_GENERATION block)

## [0.2.25] - 02/06/26

### Added
- Amazon Quick Suite MCP integration documentation with step-by-step setup guide
- MCP server configuration outputs exposed in main CloudFormation stack (MCPServerEndpoint, MCPServerClientId, etc.)
- Enterprise Webex Virtual Participant support with guest authentication, CAPTCHA handling, and speaker detection

### Changed
- Updated default Bedrock model from Claude 3 Haiku to Claude Haiku 4.5 (global.anthropic.claude-haiku-4-5-20251001-v1:0)
- Changed Virtual Participant default launch type from FARGATE to EC2

### Fixed
- Critical security vulnerability: JWT tokens now always verified with signature validation (prevents token forgery and user impersonation)
- Virtual Participant transcription failure due to function signature mismatch in get_owner_from_jwt() call - all transcript segments were failing to write to DynamoDB
- Enabled X-Ray tracing on GetEventApiDnsFunction, GetCloudFrontPrefixListFunction, and VirtualParticipantSchedulerFunction for improved observability
- Triaged and suppressed 54 security scan false positives and acceptable design decisions
- Webex Virtual Participant password-protected meeting support
- CloudFormation stack deletion failure when using EC2 launch type for Virtual Participant

## [0.2.24] - 01/07/26

### Added
- S3 Vectors integration for Knowledge Base storage (40-60% cost reduction vs OpenSearch)
- MCP (Model Context Protocol) server with OAuth 2.0 authentication
- Six MCP tools: list_meetings, search_lma_meetings, get_meeting_summary, get_meeting_transcript, start_meeting_now, schedule_meeting
- MCP server test suite with quickstart guide
- EnableDataRetentionOnDelete parameter support for S3 Vectors and OpenSearch resources
- Add Nova 2 Pro model

### Changed
- Replaced custom Lambda resources with native CloudFormation (AWS::S3Vectors::*, AWS::BedrockAgentCore::*)
- Optimized meeting queries with date-sharded DynamoDB access

### Fixed
- Transcript retrieval filename conversion handling

## [0.2.23] - 12/24/25

### Added
- MCP (Model Context Protocol) server support with public registry integration from modelcontextprotocol.io
- OAuth 2.1 authentication with PKCE, OAuth 2.0 fallback, for secure MCP server connections
- OAuth Client Credentials support for machine-to-machine authentication
- Custom header, env variables, and bearer token authenticaiton options
- Automatic OAuth token refresh before expiration
- Salesforce MCP server integration with full CRUD operations
- MCP Servers UI with dual installation modes (Custom and Registry tabs)
- Lambda SnapStart for faster cold starts on agent functions
- QuickSight S3 manifest CloudFormation output for analytics integration
- Salesforce MCP setup documentation

### Changed
- MCP servers now support multiple authentication methods (Bearer, OAuth 2.1, OAuth Client Credentials, Custom Headers)
- Build automation improved with automatic file change detection

## [0.2.22] - 12/08/25

### Added
- Virtual Participant browser control via Strands agent
- VNC preview control tool for programmatic show/hide
- Meeting controls shortcut buttons with edit UI
- Amazon Nova 2 Lite model support

### Changed
- VNC viewer resolution increased to 1920x1120 for full window visibility

### Fixed
- VNC viewer top cutoff - Chrome tabs and controls now fully visible
- VP browser tab management - opens new tabs correctly


## [0.2.21] - 11/17/25

### Added
- Strands-based Meeting Assistant Tools
- noVNC Support for Real-Time Virtual Participant Viewing and Interaction
- Claude Global Models
- Tool for meeting chronology with DynamoDB query for finding last meeting

### Changed
- Update chime meeting ending functionality
- Filters for virtual participant enhancements

### Fixed
- Fix zoom speaker detection
- Zoom bugfix VNC wording
- Fix: Virtual Participant VNC Routing for Multiple Concurrent Sessions
- Remove ARN being detected in history for code defender
- Teams captcha fix
- Fixing logging bucket issue
- Add detection and delay for Amazon Zoom to login with VNC viewer
- Increase VNC connect delay for healthy for Zoom connect failure
- Fix virtual participant issue
- ECR vulnerabilities in VP stack

## [0.2.20] - 2025-11-04
### Fixed
 - Github #199 issue: Fixed: Deployment issues due to a logging bucket name missing issue. 

## [0.2.20] - 2025-10-24
### Added
- Virtual Participant filtering and table
- User-based access control (UBAC) enhancements for Virtual Participants with improved security and permissions
- Terminate/end scheduled Virtual Participant tasks functionality with automated cleanup

## [0.2.19] - 2025-10-17
### Added
- Virtual Participant scheduling feature for future meetings
- Meeting invitation parsing through Bedrock AI for auto-filling forms
- Copy to clipboard feature for meeting summaries

## [0.2.18] - 2025-10-13
### Fixed
- Github #197 issue : Fixed: Virtual Participant functionality not working with native Chime/Zoom applications in version 0.2.17

## [0.2.17] - 2025-10-10
### Added
- Webex Virtual Participant support (joins Teams, Zoom, Chime, and Meet)
- Claude Sonnet 4.5 support for meeting summaries and meeting assistant
- AWS Strands agent as a lightweight alternative to QnABot for meeting assistance with new STRANDS_BEDROCK option

### Changed
- Enhanced security posture with comprehensive infrastructure and code improvements
- Added optional IAM permissions boundary support (similar to IDP project)
- Created centralized CustomerManagedEncryptionKey resource for consistent encryption across all services

## [0.2.16] - 2025-10-01
### Fixed
- Github #196 issue - Fixed QnABot deployment permissions issue.

## [0.2.15] - 2025-09-26
### Added
- Added support for Teams Meeting application

### Changed
- Migrated Virtual Participant from Python sdk to Node sdk.

### Fixed

## [0.2.14] - 2025-09-12
### Added
- Virtual Participant user enhancements including status tracking, notifications for tracking virtual participant, and option to end virtual participant

### Fixed
- Meeting deletion now properly removes Virtual Participant traces

## [0.2.14] - 2025-09-12
### Added
- Virtual Participant user enhancements including status tracking, notifications for tracking virtual participant, and option to end virtual participant

### Fixed
- Meeting deletion now properly removes Virtual Participant traces

## [0.2.13] - 2025-08-28
### Added
- Virtual Participant UI improvements including success modal and enhanced error handling

### Changed
- Updated license to MIT-0 for improved open source compatibility
- ESLint and Prettier code formatting improvements for UI components

### Fixed
- Zoom Virtual Participant issue - improved element waiting logic and removed unnecessary delays
- Deployment name handling fixes


## [0.2.12] - 2025-08-21
### Added
- Support for newest Amazon Bedrock models including inference profiles
- Auto-scroll behavior during meetings - now stops and switches to manual scroll when user scrolls up to read previous transcripts
- New 'Deployment Info' details added to the UI showing stack name, build date, and version.

### Changed
- Migrated from complex update-version.sh script approach to simpler token-based versioning system, eliminating version mismatches and reducing maintenance overhead
- Updated Lambda runtime to Node.js 22 across multiple stacks
- Favicon replaced for better branding

### Fixed
- Virtual Participant Dockerfile - fixed Chromium font dependencies issue that was causing CloudFormation deployment failures


## [0.2.11] - 2025-07-03
### Added
- Adding model initialization parameter for when Amazon Bedrock model access might not be setup e.g. AWS workshops. Enables deployment of LMA without Amazon Bedrock model access. #185


## [0.2.10] - 2025-06-20
### Added
- Add DOCX export functionality and increase LLM chat history limit. #183
### Fixed 
- When deploying LMA, the VirtualParticipantStack fails to create due to BuildCustomResourceFunction resource. #184

## [0.2.9] - 2025-02-09
### Fixed 
- Zoom browser extension, active speaker not identified when video share or screen share is on bug fix #174

## [0.2.8] - 2025-02-07
### Fixed 
- user identification has not working with chrome extension for teams - PR #170
- Changed browser extension icons to a "person in a meeting room" - PR #167

## [0.2.7] - 2024-11-23
### Added
- Added ability to remove 'sharedWith' users - PR #158
- Added delete meetings functionality - PR #158
- Integration for Google Meet in LMA browser extension - PR #159
### Fixed 
- Improvements to meetings sharing user experience - PR #158
- Refactored UI to use common modules to avoid code duplication - PR #158

## [0.2.6] - 2024-11-01
### Fixed
- Fixed a bug that allowed non-Admin users to see other user call details if they had (or could guess) the callId.
- Fixed a bug that prevented calls from being shared for live calls.
- Added share meeting button to call split panel and call details pages.

## [0.2.5] - 2024-10-25

### Added
- User Based Access Control is enhanced to allow users to share their meetings with other users. PR #145
- Add support for new Anthropic Claude 3.5 Sonnet v2 - PR #147

### Fixed
- Updated zoom code to address issue for leaving a meeting, continued effort on participant recognition - PR #148


## [0.2.4] - 2024-10-20

### Added
- Knowledge base of meeting transcripts #129
- Meetings Query Tool for running GenAI queries across the new meetings knowledge base [README](./README.md#meetings-query-tool---query-past-meetings-from-the-transcript-knowledge-base)

### Fixed
- Stops transcribing calls without error messaging - added exception catch and retry for Transcribe sessions - Issue #137
- Virtual Participant meeting won't open in UI if meeting name has &, /, or + symbols #142

## [0.2.3] - 2024-10-11

### Added
- Allow meeting assistant to perform custom actions using a Bedrock Agent #128

### Fixed
- Fixes for differences between Zoom and Zoom Enterprise - PR #132

## [0.2.2] - 2024-10-03

### Added
- Ability to apply optional Bedrock Guardrail when MeetingAssistant is BEDROCK_KNOWLEDGE_BASE or BEDROCK_LLM - Issue #53 

### Fixed
- When using Virtual Participant (preview) no audio recording is created. #126
- Use selected Transcribe language for virtual-participant - PR #118 
- Updated QnABot nested stack to QnABot v6.1.1 - PR #119
- Publish script fails to detect make failure in lca-ai-stack during build - Issue #111
- When publishing, AISTACK `make` fails to find bash with new version of gnu make (4.3) - Issue #110
- Muliple dependabot PRs
- Upgrade all python Lambda functions to python3.12 (latest)
- Publish script hangs in lma-ui-stack in sam build when using arm64 container image, on new EC2 and Cloud9 instances #124

## [0.2.1] - 2024-08-29

### Added
- Support for Anthropic Claude 3.5 Sonnet #94

### Fixed
- With Q Business as the Meeting Assistant, the ASK ASSISTANT button responses are missing meeting transcript context #97
- Non admin users are unable to see meetings started with Virtual Participant #98


## [0.2.0] - 2024-08-24

### Notes
- When you update an existing stack from v0.1.x to v0.2.x the existing admin user is recreated. You are emailed a temporary password and must set a new permanent password as you did when you first deployed the stack. See [User Based Access Control (Preview)](./lma-ai-stack/README_UBAC.md) for more information, and recommendations on how to differentiate the admin username/email from a non-admin username/email (e.g joe+admin@acme.com vs joe@acme.com) if you are configuring LMA for multiple users.

### Added
- User Based Access Control (UBAC) (Preview) adds multi-user access where each user can access only meetings that they initiated. #67
- Amazon Q Business can now be used as the meeting assistant knowledge base service. #68

### Fixed
- Virtual Participant is status "In Progress" after Chime meeting ended and no call summary generated #84
- Speaker attribution lacks fidelity when multiple users are talking #92
- Knowledge base citation source links occasionally blank #93
- Use version number in browsers extension download zip file name to avoid download of older version due to cache.
- Miscellaneous security patches from dependabot


## [0.1.10] - 2024-08-15

### Added

- Initial support for WebEx web client in the browser extension PR #81
- Download button for exporting call summary and call transcript PR #80
- Optional specialized prompts for Healthcare use cases (SOAP/BIRP notes, etc.) by selecting 'Healthcare' from new 'Business Domain' parameter - PR #20

## [0.1.9] - 2024-08-05

### Fixed

- #76 Support optional deployment using existing VPC/subnets
- #71 Introduce 'Virtual Participant' (Preview) [VP README](./lma-virtual-participant-stack/README.md)
- #155 Missing call transcript download button

## [0.1.8] - 2024-08-01

### Fixed

- Remove unused KMS keys #72

## [0.1.7] - 2024-08-01

### Fixed

- Use auth role for Meeting Assistant bot, and remove all permissions for unauthenticated Cognito identities #65
- Optimize costs by making Appsync API cache optional, with configurable size - default OFF. #66

## [0.1.6] - 2024-07-24

### Fixed

- Bedrock KB source links for S3 documents should be click to open #46
- Web URL missing from assistant response sources from Bedrock KB webcrawler #49
- When using Microsoft Teams, LMA browser extension closes chat window and always opens participants window #52
- Teams browser extension problem when logged in as guest account. #53
- Add note to Cognito email regarding the Chrome browser extension #55
- Meeting assistant bot voice output doesn't work. #39

## [0.1.5] - 2024-07-15

### Added

- Added initial support for Teams web client in the browser extension
- Added option to automatically create Bedrock Knowledge Base and associated S3 or Web Url datasource(s) during deployment

### Fixed

- Stack deployment now fails fast if required Bedrock models are not available or enabled in the account/region
- #44 - Stack deployment failure in AISTACK, due to node package checksum problem
- #43 - Assistant fails when Bedrock KB article is sourced from new KB web crawler data source connector

## [0.1.4] - 2024-06-08

### Added

- Improve the user experience by merging consecutive segments and render them in single line - see PR #28
- Stream Audio tab UX improvements (PR #30)
  - Added Mute/Unmute button for microphone - #29
  - Updated labels on fields and added validation
  - Defaulted meeting organiser field to logged in user's email rather than "Me"
  - Removed microphone source field (defaulted to meeting organiser)
  - Added links to open the meeting while/after recording
  - Added logic to disable fields while recording is in progress and show warning message
  - Added timestamp to meeting name to ensure id is unique
  - Updated READMEs with new field names and functionality
- Enable/disable call recording - useful if you don't want any audio recordings saved (PR #31)
- Enable configurable retention period for turn by turn transcription - useful if you want to keep the meeting summary, but not the line by line transcription (PR #31)
- Enable configurable retention for CloudWatch logs (PR #31)

### Fixed

- #33 - Fix/active speaker assignment not for mic channel (PR #34)
- Streamline Websocket server logs
- #25 - Fix Updating Participant Name on Stream Audio Page does not reflect in the meeting transcript
- #24 - Fix TEST ALL in QnABot is continuously putting file version into the S3 bucket (PR #26)
- #35 - Fix Browser extension intermittently silently fails to authenticate (PR #35)

## [0.1.3] - 2024-05-22

### Fixed

- #6 - Fixed multi languageID segment overwrite issue (PR #23)

## [0.1.2] - 2024-05-10

### Added

- Added option for Bedrock LLM (without knowledge bases) to be used as the meeting assistant service for 'OK Assistant' and 'Ask Assistant' responses.
- Added option for single language auto-detection - using Amazon Transcribe's 'Identify Language'.
- Added option for multiple language auto-detection - using Amazon Transcribe's 'Identify Multiple Languages'.

### Fixed

- Added `&` to the previous defense against Meeting Names / IDs with special characters that are not URL safe, by replacing with pipe character `|` in the browser extension when starting the streaming. PR #10
- Fix for #1 - "Stream Audio" tab stops working after a stack update when AssistantWakePhraseRegEx is modified. PR #11
- Fix for #2 - Incorrect Chime speaker name attribution when muting - PR #19
- Fix for #3 - Add a CloudFormation rule to require `BedrockKnowledgeBaseId` parameter to be provided when BEDROCK_KNOWLEDGE_BASE is chosen as the meeting assistant service.
- Fix for #4 - Chrome extension bug causing meeting topic to be continually overwritten
- Fix for #13 - Longer CloudFormation stack names cause errors in length of Lambda function names.

### Changed

- Downsize web socket server ecs-fargate task for improved cost efficiency. PR #12
- Browser extension now displays release version number
- Websocket server now sends audio in 100ms chunks to Transcribe (best practice)

## [0.1.1] - 2024-04-19

### Fixed

- Added defense against Meeting Names / IDs with special characters `/?#%+` that are not URL safe, by replacing with pipe character `|` in the browser extension when starting the streaming.

## [0.1.0] - 2024-04-17

### Added

- Initial release
