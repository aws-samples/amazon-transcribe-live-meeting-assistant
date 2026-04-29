---
title: "Virtual Participant Local Development"
---

# Virtual Participant Local Development

This guide describes how to run and iterate on the LMA Virtual Participant (VP) locally against a deployed LMA CloudFormation stack. The VP is a headless-Chrome-on-Linux Puppeteer app that normally runs in ECS (Fargate or EC2), so the most realistic local environment is a Linux EC2 instance that mirrors the ECS task's runtime — edited via VSCode Remote-SSH and previewed via VNC.

## Table of Contents

- [Why not just run it on your laptop?](#why-not-just-run-it-on-your-laptop)
- [Recommended Setup](#recommended-setup)
- [Running the VP Locally](#running-the-vp-locally)
- [Managing Secrets (`--reuse-env`)](#managing-secrets---reuse-env)
- [Dev Mode (auto-reload)](#dev-mode-auto-reload)
- [VNC Preview and VSCode Port Forwarding](#vnc-preview-and-vscode-port-forwarding)
- [Useful `make` Targets](#useful-make-targets)
- [Troubleshooting](#troubleshooting)
- [See Also](#see-also)

## Why not just run it on your laptop?

The VP container bundles a specific Linux + Chromium + audio-stack combination that closely matches what ECS runs in production. Running Docker on macOS or Windows (even via WSL or Docker Desktop) introduces subtle differences in:

- Chromium/Puppeteer behavior and fonts
- Audio device enumeration and virtual sinks
- CPU architecture (Apple Silicon vs. x86_64)
- Networking and DNS resolution inside the container

Reproducing ECS-specific bugs locally on a laptop is unreliable. Running on a Linux EC2 that uses the same base image/arch as the ECS task is the closest match to production.

## Recommended Setup

### 1. EC2 instance

Launch an EC2 instance that mirrors your VP ECS task configuration:

- Same architecture (x86_64 is the default)
- Instance size large enough to run the container comfortably (e.g. `c5.xlarge` or `m5.xlarge` if you are testing Voice Assistant + Simli avatar)
- Docker installed and running
- AWS CLI installed and configured with credentials that can `describe-stacks`, `list-stack-resources`, `get-graphql-api` on the target LMA stack
- Open SSH inbound (nothing else — VNC will be tunneled through SSH)

### 2. VSCode Remote-SSH

From your laptop:

1. Install the **Remote - SSH** VSCode extension.
2. Add an SSH host entry for your EC2 instance.
3. Connect — VSCode opens a remote workspace running on the EC2.
4. Clone this repo onto the EC2 and open it in the remote VSCode window.

### 3. TigerVNC (or any VNC viewer) on your laptop

Install TigerVNC Viewer (or RealVNC / macOS Screen Sharing with `vnc://localhost:5900`). You will not connect it to the EC2 directly — VSCode forwards the container's VNC ports back to your laptop automatically.

## Running the VP Locally

From the repository root on the EC2:

```bash
make vp-start STACK_NAME=LMA-dev-stack PLATFORM=WEBEX MEETING_ID=25523622514
```

With a password:

```bash
make vp-start STACK_NAME=LMA-dev-stack PLATFORM=ZOOM MEETING_ID=123456789 MEETING_PASSWORD=abc123  # pragma: allowlist secret
```

The `vp-start` target wraps `lma-virtual-participant-stack/backend/local-test.sh`, which:

1. Reads your deployed LMA stack via CloudFormation (Kinesis stream, S3 bucket, AppSync endpoints, VP Task Registry table, Nova Sonic config, Simli config, etc.)
2. Writes everything to `lma-virtual-participant-stack/backend/.env.local`
3. Builds the VP Docker image (`lma-vp-local`)
4. Runs the container with your AWS credentials mounted read-only and ports `5900` (VNC) and `5901` (noVNC) exposed

Accepted `PLATFORM` values: `WEBEX`, `ZOOM`, `TEAMS`, `CHIME`.

## Managing Secrets (`--reuse-env`)

`local-test.sh` discovers most configuration from CloudFormation, but **some values are not stored in the stack** and must be set manually in `.env.local`. These currently include:

- `ELEVENLABS_API_KEY` — ElevenLabs Conversational AI API key
- `SIMLI_API_KEY` — Simli avatar API key
- Any other workflow-specific overrides you want to test (e.g. a custom `MEETING_NAME` to target an existing real meeting so Nova Sonic / ElevenLabs tool calls work end-to-end)

### Workflow

1. **First run** — let the script generate a fresh `.env.local` from CloudFormation:

   ```bash
   make vp-start STACK_NAME=LMA-dev-stack PLATFORM=WEBEX MEETING_ID=25523622514
   ```

2. **Stop the container** (`Ctrl+C`, or `make vp-stop`).

3. **Edit `lma-virtual-participant-stack/backend/.env.local`** — fill in the blank secret values:

   ```bash
   ELEVENLABS_API_KEY=sk-...         # pragma: allowlist secret
   SIMLI_API_KEY=simli_...           # pragma: allowlist secret
   # Optionally pin MEETING_NAME to an existing meeting so tool calls can resolve it:
   MEETING_NAME=MyRealMeeting-LMA
   ```

4. **Subsequent runs** — use `REUSE_ENV=1` so your edits are preserved. The script only updates `MEETING_PLATFORM`, `MEETING_ID`, `MEETING_PASSWORD`, `MEETING_TIME`, and `DEV_MODE`:

   ```bash
   make vp-start-reuse STACK_NAME=LMA-dev-stack PLATFORM=WEBEX MEETING_ID=25523622514
   ```

   Equivalent long form:

   ```bash
   make vp-start REUSE_ENV=1 STACK_NAME=LMA-dev-stack PLATFORM=WEBEX MEETING_ID=25523622514
   ```

> **Tip:** If the stack's CloudFormation values change (e.g. you redeployed and table names or endpoints rotated), drop `REUSE_ENV=1` once to regenerate `.env.local`, then re-add your secrets.

> **Note on `MEETING_NAME`:** The `local-test.sh` `--reuse-env` path intentionally does **not** overwrite `MEETING_NAME` by default so that if you have pointed it at a real meeting ID (to exercise Nova Sonic / ElevenLabs tool calls), your pin is preserved across runs. See the comment in `local-test.sh` near the `MEETING_NAME` sed line.

## Dev Mode (auto-reload)

Dev mode mounts `lma-virtual-participant-stack/backend/src` into the container and auto-rebuilds / restarts when TypeScript files change.

```bash
make vp-start-dev STACK_NAME=LMA-dev-stack PLATFORM=WEBEX MEETING_ID=25523622514
```

Or combined with reusing your `.env.local`:

```bash
make vp-start-dev REUSE_ENV=1 STACK_NAME=LMA-dev-stack PLATFORM=WEBEX MEETING_ID=25523622514
```

Dev mode runs the container as `lma-vp-local-test` (named, persistent). Once it's running:

```bash
make vp-logs      # tail container logs
make vp-shell     # exec into the container
make vp-stop      # stop and remove the container
```

## VNC Preview and VSCode Port Forwarding

When the container starts, VNC is available on:

- **VNC Client** (TigerVNC, etc.): `localhost:5900`
- **Web browser (noVNC)**: <http://localhost:5901/vnc.html>

VSCode's Remote-SSH session automatically forwards these ports from the EC2 back to your laptop, so `localhost:5900` on your laptop reaches the container on the EC2.

### ⚠️ Stale port forwarding gotcha

VSCode's auto-forwarded ports can become **stale from day to day** (especially after disconnects, EC2 reboots, or long idle sessions). Symptom:

> TigerVNC Viewer connects to `localhost:5900` and hangs indefinitely. No error, no timeout, just a spinner.

**Fix:** delete the stale forwarded ports and let VSCode recreate them.

1. In VSCode, open the **Ports** panel (View → Terminal, then the **PORTS** tab next to TERMINAL / OUTPUT).
2. Right-click ports `5900` and `5901` and choose **Stop Forwarding Port**.
3. Stop the VP container (`make vp-stop`) and start it again (`make vp-start-reuse ...`). VSCode will detect the listening ports and auto-forward them fresh.
4. Reconnect TigerVNC to `localhost:5900` — it should connect immediately.

If auto-forward doesn't kick in, click **Forward a Port** in the Ports panel and manually add `5900` and `5901`.

## Useful `make` Targets

| Target | Purpose |
|--------|---------|
| `make vp-start STACK_NAME=… PLATFORM=… MEETING_ID=…` | Build and run the VP container locally |
| `make vp-start-dev STACK_NAME=… PLATFORM=… MEETING_ID=…` | Same, but dev mode (source mounted, auto-reload) |
| `make vp-start-reuse STACK_NAME=… PLATFORM=… MEETING_ID=…` | Reuse existing `.env.local` (preserves manually-set secrets) |
| `make vp-stop` | Stop and remove the `lma-vp-local-test` container |
| `make vp-logs` | Tail container logs |
| `make vp-shell` | Open a bash shell inside the running container |
| `make build-vp` | TypeScript build of the VP backend (no Docker) |

All `vp-start*` targets accept:

| Variable | Required | Description |
|----------|----------|-------------|
| `STACK_NAME` | yes | LMA CloudFormation stack name |
| `PLATFORM` | yes | `WEBEX`, `ZOOM`, `TEAMS`, or `CHIME` |
| `MEETING_ID` | yes | Meeting ID to join |
| `MEETING_PASSWORD` | no | Meeting password, if the platform requires one |
| `DEV=1` | no | Enable dev mode (implied by `vp-start-dev`) |
| `REUSE_ENV=1` | no | Reuse `.env.local` (implied by `vp-start-reuse`) |

## Troubleshooting

- **"Could not fetch all required CloudFormation resources"** — verify `STACK_NAME` is correct and the stack is fully deployed. Check your AWS credentials on the EC2: `aws sts get-caller-identity`.
- **"Container 'lma-vp-local-test' already exists"** — run `make vp-stop` and retry. Happens in non-dev mode when a previous dev-mode container was left running.
- **TigerVNC hangs on connect** — delete stale forwarded ports in the VSCode Ports panel (see [above](#vnc-preview-and-vscode-port-forwarding)).
- **Changes to `.ts` not picked up** — make sure you're running in dev mode (`DEV=1` / `make vp-start-dev`). Production mode does not mount `src`.
- **Voice Assistant not working** — confirm `ELEVENLABS_API_KEY` or relevant Nova Sonic config is present in `.env.local` and that you used `REUSE_ENV=1` on your follow-up run so the key isn't wiped.
- **AWS creds not found inside the container** — the script mounts `~/.aws` read-only. Ensure `~/.aws/credentials` or `~/.aws/config` exists on the EC2, or use an instance profile (the container inherits the EC2's role via IMDS only if you remove the `-v ~/.aws:/root/.aws:ro` mount — the simplest is to write credentials into `~/.aws` on the EC2).

## See Also

- [Virtual Participant](virtual-participant.md) — Overview of the VP feature
- [Developer Guide](developer-guide.md) — Building LMA from source
- [Voice Assistant](voice-assistant.md) — Voice Assistant providers used by the VP
- [Simli Avatar Setup](simli-avatar-setup.md) — Avatar configuration
