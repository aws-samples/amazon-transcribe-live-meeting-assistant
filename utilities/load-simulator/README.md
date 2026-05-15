# LMA Load Simulator

A load / scale simulator for a deployed **Amazon Transcribe Live Meeting
Assistant (LMA)** stack.

It provides four scenarios, all packaged as sub-commands of the main `lma`
CLI (or as a standalone `lma-load` binary):

| Scenario    | What it does | What it exercises |
|-------------|--------------|-------------------|
| `concurrent` | N simultaneous meetings for Y duration, via one of four drivers (`kinesis`, `upload`, `websocket`, `vp`) | Transcribe concurrency, WebSocket transcriber, upload pipeline, VP autoscaling, Bedrock quotas |
| `backfill`   | Fabricate X historical meetings spread over Y days, with **backdated timestamps** | Meeting-list paginator, `listCallsDateRange` GSI, date-range picker scalability |
| `rbac`       | Provision N synthetic Cognito users + latency-sweep `listCalls`/`getCallCount` under RBAC | Per-user ownership filter, subscription fanout, role-gated queries |
| `cleanup`    | Delete every synthetic resource tagged with a given run-id | Deterministic teardown of meetings, VPs, users, S3 orphans |

Every synthetic resource is stamped with a `LoadTestRunId` / Owner-prefix
so cleanup is deterministic. Running against stacks whose name contains
`prod` is refused unless `--force` is passed.

## Table of contents

1. [Installation](#installation)
2. [Quick start](#quick-start)
3. [Scenarios in detail](#scenarios-in-detail)
    - [Concurrent](#1-concurrent--n-in-flight-meetings)
    - [Backfill](#2-backfill--fabricate-historical-meetings)
    - [RBAC](#3-rbac--synthetic-user--latency-sweep)
    - [Cleanup](#4-cleanup--deterministic-teardown)
4. [Synthetic Cognito users](#synthetic-cognito-users)
5. [Cost and quota awareness](#cost-and-quota-awareness)
6. [CloudWatch observability](#cloudwatch-observability)
7. [Safety rails](#safety-rails)
8. [Architecture notes](#architecture-notes)
9. [Development](#development)

---

## Installation

From the LMA repo root:

```bash
# Baseline install — enough for `backfill` and `concurrent --driver kinesis`:
pip install -e utilities/load-simulator

# Add the extras for other drivers:
pip install -e 'utilities/load-simulator[websocket]'   # --driver websocket
pip install -e 'utilities/load-simulator[upload]'      # --driver upload
# `--driver vp` needs no extras — it talks to AppSync + Step Functions
# directly using your AWS credentials (SigV4) via `lma-sdk`.
pip install -e 'utilities/load-simulator[all]'         # everything
```

Generate the shipped fixture WAV once (we don't commit binary audio):

```bash
python -m lma_load.fixtures.generate
```

Verify install:

```bash
lma load --help                 # if you have lma-cli installed too
lma-load --help                 # always works
```

## Quick start

```bash
export LMA_STACK_NAME=LMA
export AWS_DEFAULT_REGION=us-east-1

# 1) Sanity check — print the stack resources we resolved
lma load stack-info

# 2) Dry-run — safest thing to try first
lma load backfill --meetings 100 --days 7 --dry-run

# 3) Seed 1,000 historical meetings spread over the last 30 days (~zero cost)
lma load backfill --meetings 1000 --days 30

# 4) Smoke-test 10 live-looking concurrent meetings via Kinesis (~zero cost)
lma load concurrent --driver kinesis --meetings 10 --duration 2m

# 5) Bigger: 50 real WebSocket streaming meetings for 5 minutes
lma load concurrent --driver websocket --meetings 50 --duration 5m \
    --email-prefix strahanr --email-domain amazon.com

# 6) After you're done, clean up
lma load cleanup --target-run-id lt-20260510T131456-abc123
```

## Scenarios in detail

### 1. `concurrent` — N in-flight meetings

```bash
lma load concurrent
  --driver {kinesis|upload|websocket|vp}
  --meetings N
  --duration 5m                 # per-meeting duration (e.g. 30s, 15m, 1h)
  [--concurrency M]             # max in-flight (default = --meetings)
  [--ramp 30s] [--jitter 2s]
  [--wav ./my-sample.wav]       # for upload/websocket drivers
  [--meeting-ids-file ids.yaml] # for vp driver
  [--user-pool-size K]
  [--email-prefix NAME] [--email-domain DOMAIN]
```

#### Driver matrix

| Driver | Real Transcribe? | Real audio? | Cognito users? | Meeting invites? | Cost |
|---|---|---|---|---|---|
| **`kinesis`**   | no (synthetic segments) | no | no | no | ~$0 |
| **`upload`**    | batch Transcribe | shipped WAV | yes | no | $$ |
| **`websocket`** | streaming Transcribe | shipped WAV, looped | yes | no | $$$ |
| **`vp`**        | streaming Transcribe (real meeting) | your meeting audio | yes (1) | **yes** | $$$ + VP compute |

#### `--driver kinesis`

No audio, no Transcribe. Emits START → ADD_TRANSCRIPT_SEGMENT* → END
events onto `CallDataStream`, with real-time pacing so the meeting
appears "live" in the UI. Used to smoke-test the event processor,
DynamoDB, AppSync subscription fanout and the Bedrock summary
orchestrator at volume.

```bash
lma load concurrent --driver kinesis --meetings 100 --duration 10m
```

#### `--driver upload`

Calls `createUploadMeeting`, S3-PUTs the WAV fixture, lets the backend
batch-Transcribe it. Requires synthetic users.

```bash
lma load concurrent --driver upload --meetings 20 --duration 1m \
    --email-prefix strahanr --email-domain amazon.com
```

#### `--driver websocket` (the real stressor)

Opens real WSS connections with per-user JWTs and streams the WAV
fixture in real-time, looping if `--duration` > clip length. This is
the only driver that will surface the Amazon Transcribe concurrent
streaming quota ceiling (default ~250/region, per channel).

```bash
# Start small and ramp up to find your stack's breaking point:
lma load concurrent --driver websocket --meetings 50  --duration 5m \
    --email-prefix strahanr --email-domain amazon.com

lma load concurrent --driver websocket --meetings 200 --duration 10m \
    --email-prefix strahanr --email-domain amazon.com \
    --ramp 60s --concurrency 100
```

#### `--driver vp` (Virtual Participant at scale)

Creates N Virtual Participants against the deployed LMA stack using
**IAM-signed AWS API calls** — `createVirtualParticipant` via AppSync
(SigV4) and `StartSyncExecution` on the VP scheduler Step Function.
No browser, no Cognito sign-in; your local AWS credentials are used
directly.

Round-robins across real meeting invitations you provision ahead of time:

```yaml
# meetings.yaml  (your file)
meetings:
  - name: "LoadSim Sandbox — Zoom"
    platform: ZOOM
    id: "1234567890"
    password: ""
  - name: "LoadSim Sandbox — Chime"
    platform: CHIME
    id: "9988776655"
```

```bash
lma load concurrent --driver vp --meetings 10 --duration 5m \
    --stack-name LMA \
    --meeting-ids-file meetings.yaml
```

Each VP is automatically ended (via `endVirtualParticipant`) after
`--duration` elapses, so scribe containers don't linger past the load
run. If the end call fails, the error is captured in
`concurrent-result.json` and you can clean up stragglers with
`lma vp end --id <vp-id>` or `lma load cleanup`.

**Required IAM for the caller** (in addition to stack-info read):

* `appsync:GraphQL` on the LMA AppSync API
* `states:StartSyncExecution` + `states:DescribeStateMachine` on the
  `<stack-name>-LMAVirtualParticipantScheduler` state machine
* `cloudformation:DescribeStacks` (to resolve AppSync URL)

**Single-VP smoke test** using the new `lma vp` sub-commands:

```bash
lma vp create --stack-name LMA --name "SDK smoke" --platform ZOOM \
    --id 1234567890
lma vp list    --stack-name LMA
lma vp end     --stack-name LMA --id <vp-id>
```

**Tip**: a single long-lived Zoom/Chime meeting with waiting-room
disabled can accept hundreds of VP attendees — the cheapest way to scale.

**Capacity gotcha**: the VP ECS cluster's EC2 Auto Scaling Group is
provisioned at `DesiredCapacity=1` by default and each scribe task needs
~3500 MiB — so only 1 VP will place at a time. For load-test runs > 1
concurrent VP, scale the ASG manually first:

```bash
aws autoscaling update-auto-scaling-group \
    --auto-scaling-group-name <stack-VP-ASG-name> \
    --desired-capacity <N>
```

### 2. `backfill` — fabricate historical meetings

```bash
lma load backfill
  --meetings N
  [--days 30]            # spread uniformly over last N days
  [--users 1]            # distribute ownership across N synthetic owner strings
  [--skip-summary]       # inject a deterministic synthetic summary instead of
                         # letting Bedrock run. Default is --with-summary so
                         # the scenario exercises real Bedrock quotas.
```

**Three-phase emission** — the injector serialises every meeting's
`START → segments → END` events into three separate Kinesis flushes with
small inter-phase pauses. This guarantees the `call_event_processor`
Lambda has finished `createCall` before any segment lands and has
finished every segment before `END` triggers the summary orchestrator.
Meetings therefore finish **"Done"** with a real duration and a real
summary — not stuck "In Progress" or "Done + An error occurred" from the
race that earlier versions suffered.

Default driver is Kinesis injection — no Transcribe. Each fabricated
meeting ends up as a proper DDB row with a backdated `CreatedAt`, visible
through the normal UI (meeting-list, date-range picker, detail page,
summary, transcript):


```bash
# 50,000 synthetic meetings spread over the last 6 months, ~zero cost
lma load backfill --meetings 50000 --days 180 --users 20 --force
```

Expected throughput: ~800 meetings/s per Kinesis shard, so 50k
meetings take about a minute on a typical 1-shard LMA deployment.

### 3. `rbac` — synthetic users + latency sweep

```bash
lma load rbac
  --users 100
  --email-prefix strahanr
  --email-domain amazon.com
  [--iterations 20]      # listCalls queries per user
  [--admin-fraction 0.2]
  [--window-days 30]
```

For each of N Cognito users (20% admin, 80% user), runs `--iterations`
parallel `listCallsDateRange` + `getCallCount` queries and emits p50 /
p95 / p99 histograms, plus a breakdown by admin vs. user role.

Tokens for all synthetic users are cached at `~/.lma-load/<runId>/users.json`
(mode 0600) so you can poke around after the scenario finishes.

### 4. `cleanup` — deterministic teardown

```bash
lma load cleanup --target-run-id lt-20260510T131456-abc123   # one run
lma load cleanup --target-run-id '*'                         # everything ever
lma load cleanup --target-run-id '*' --dry-run               # preview only
```

Deletes, in order:

1. Cognito users matching `*+loadtest-<runId>-*@*`
2. Virtual Participants whose `meetingName` contains the run-id
3. Meeting rows in the EventSourcing table whose `Owner` starts with `loadtest-<runId>-`
4. S3 orphans under `lma-uploads-pending/loadtest-<runId>-*`

## Synthetic Cognito users

Users are created using **+N subaddressing**:

```
prefix = strahanr
domain = amazon.com
runId  = lt-20260510T131456-abc123

→  strahanr+loadtest-lt-20260510T131456-abc123-0001@amazon.com
→  strahanr+loadtest-lt-20260510T131456-abc123-0002@amazon.com
→  ...
```

- Cognito treats each `+NNNN` as a distinct identity
- All welcome emails are **suppressed** (`MessageAction=SUPPRESS`)
- Random 24-char password is set with `AdminSetUserPassword(Permanent=true)`
  so there's no forced-reset on first login
- The first 20% of users are added to the `Admin` group
- Tokens fetched via `USER_PASSWORD_AUTH` — this must be enabled on the
  LMA app client (it is, by default)

## Cost and quota awareness

Every scenario runs a **pre-flight probe** that:

1. Calls Service Quotas for Transcribe streaming / batch, Bedrock,
   ECS tasks and Kinesis shards.
2. Computes a list-price cost estimate for the requested load.
3. Prints a red `EXCEEDS LIMIT` marker if the requested load exceeds a
   quota.

Example:

```
┌───────────┬────────────────────────────────┬───────┬───────────┬──────────────────────────┐
│ Service   │ Quota                          │ Limit │ Requested │ Status                   │
├───────────┼────────────────────────────────┼───────┼───────────┼──────────────────────────┤
│ transcribe│ transcribe_streaming_concurrent│   250 │       400 │ EXCEEDS LIMIT (250)      │
│ bedrock   │ bedrock_claude_rpm             │  1000 │           │ ok                       │
│ ecs       │ ecs_tasks_per_service          │  5000 │           │ ok                       │
│ kinesis   │ kinesis_shards_per_stream      │   500 │           │ ok                       │
└───────────┴────────────────────────────────┴───────┴───────────┴──────────────────────────┘
┌──────────────────────┬─────────┐
│ Item                 │   Value │
├──────────────────────┼─────────┤
│ driver               │ websocket│
│ meetings             │      200│
│ duration_min         │     10.0│
│ transcribe_usd       │    96.00│
│ bedrock_usd          │     4.00│
│ total_usd            │   100.00│
└──────────────────────┴─────────┘
```

## CloudWatch observability

A template dashboard JSON is shipped at
`lma_load/observability/cloudwatch_dashboard.json`. Deploy it per-run with:

```bash
# Fill in template vars then put-dashboard
sed "s/\${region}/$AWS_DEFAULT_REGION/g; \
     s/\${stack}/$LMA_STACK_NAME/g; \
     s/\${call_data_stream}/$(lma load stack-info | awk '/call_data_stream/{print $2}')/g" \
     utilities/load-simulator/lma_load/observability/cloudwatch_dashboard.json \
    > /tmp/dash.json

aws cloudwatch put-dashboard \
    --dashboard-name "LMA-LoadSim-$LMA_STACK_NAME" \
    --dashboard-body file:///tmp/dash.json
```

The dashboard covers:

* Transcribe active streaming sessions (Maximum)
* Kinesis CallDataStream throughput + throttles
* `CallEventProcessor` Lambda concurrency / errors / throttles
* Bedrock invocations / throttles / client errors
* ECS VP cluster `RunningTaskCount`
* AppSync 4xx/5xx + Latency

Each scenario also saves per-run artifacts to `./results/<runId>/`:

* `summary.md` — human-readable overview
* `<scenario>-result.json` — machine-readable raw results
* `rbac-latencies.json` / `rbac-users.json` (rbac only)
* `cleanup-counts.json` (cleanup only)

## Safety rails

1. **Prod-stack refusal**: stacks whose name contains `prod`, `production`
   or `prd` are refused unless you pass `--force`.
2. **Large-scale confirmation**: creating/deleting more than a driver-specific
   threshold of objects prompts interactively (bypass with `--force`).
3. **`--dry-run`**: every scenario supports it. Prints what would happen
   and exits without touching AWS.
4. **Run-id tagging**: every synthetic resource carries a
   `LoadTestRunId` / `loadtest-<runId>-*` marker.
5. **Cognito suppression**: user creation is always `MessageAction=SUPPRESS`
   so we don't accidentally email hundreds of strangers.

## Architecture notes

```
        ┌───────────────────┐
        │  lma load <cmd>   │  click sub-command plugin of lma_cli_pkg
        └──────┬─────┬──────┘
               │     │
       ┌───────┘     └─────────┐
       ▼                       ▼
  scenarios/               cleanup.py / stack_info.py / quota_probe.py
  ├─ concurrent.py ◄───┐
  ├─ backfill.py       │
  └─ rbac.py           │
                       │
       ┌───────────────┤───────────────┐
       ▼               ▼               ▼
  drivers/         auth/          observability/
  ├─ kinesis_injector.py          ├─ report.py
  ├─ ws_streaming_driver.py       └─ cloudwatch_dashboard.json
  ├─ upload_driver.py
  └─ vp_loader_driver.py
```

### Where drivers plug into LMA

| Driver | Direct LMA touch-point |
|---|---|
| `kinesis`   | `CallDataStream` (Kinesis) → `call_event_processor` Lambda → AppSync mutations |
| `upload`    | `createUploadMeeting` GraphQL → presigned S3 PUT → `upload_meeting_processor` → batch Transcribe → `upload_meeting_finalizer` |
| `websocket` | LMA WSS transcriber endpoint → real streaming Transcribe → event pipeline |
| `vp`        | `createVirtualParticipant` (AppSync, SigV4) → `StartSyncExecution` on the VP scheduler Step Function → ECS task places scribe → scribe joins the real meeting and writes status callbacks to AppSync |

### Why the Kinesis injector is the default for backfill

`CreatedAt` is caller-supplied at every step of the LMA event pipeline
— the `addTranscriptSegment` VTL preserves it, and
`call_event_processor.py` uses it verbatim when creating the DynamoDB
call row. So a single Kinesis `PutRecords` batch of 500 backdated
events produces 500 proper past meetings at near-zero cost.

## Troubleshooting

### `--driver vp` — `AccessDeniedException` on AppSync or Step Functions

The API-only VP driver calls two services with the caller's IAM
credentials (no Cognito). Make sure your role has:

* `appsync:GraphQL` on the LMA AppSync API
* `states:StartSyncExecution` + `states:DescribeStateMachine` on
  `arn:aws:states:<region>:<account>:stateMachine:<stack>-LMAVirtualParticipantScheduler`
* `cloudformation:DescribeStacks` on the LMA root stack

### `--driver vp` — VP stays `INITIALIZING` forever

The LMA VP scheduler Step Function uses a non-`.sync` `ecs:runTask`
integration, which **succeeds even when ECS fails to place the task**
(e.g. `RESOURCE:MEMORY` on a capacity-limited cluster). The driver
polls `getVirtualParticipant` for up to 120 s after launch to detect
this — when it sees the status stuck at `INITIALIZING`, it surfaces an
``LMATimeoutError`` pointing at the scheduler execution.

Two common fixes:

1. **Scale the VP ASG** — by default the cluster has 1 EC2 instance
   with room for 1 task. Bump `DesiredCapacity` before the load test.
2. **Check the VP scheduler execution output** in the Step Functions
   console for the failed run — the `failures[]` block usually says
   either `RESOURCE:MEMORY` (need more capacity) or
   `CannotPullContainerError` (ECR image missing / wrong region).

### Migrating from a pre-API driver

If you previously set `--email-prefix` / `--email-domain` for the vp
driver, those flags are now **no-ops** — the driver uses your local
AWS credentials for every call. They remain accepted so existing
command lines keep working; other drivers (`websocket`, `upload`) still
use them to mint synthetic Cognito users.

## Development

```bash
# Install with dev extras
pip install -e 'utilities/load-simulator[dev]'

# Run the unit tests
cd utilities/load-simulator
pytest -v

# Lint (ruff is installed as part of the LMA repo's root setup)
ruff check lma_load
ruff format lma_load
```

### Running against a local UI dev server

If you're running the React UI locally via `make ui-start STACK_NAME=LMA`,
the simulator will still target the deployed stack's AWS resources
directly — it doesn't go through CloudFront. Point your browser at the
local UI URL and watch the meetings appear in real time while the
simulator is running.

### Follow-ups on the roadmap

* **Fargate remote-workers**: a CDK stack that packages this CLI into an
  ECS service so `--remote-workers N` can fan out the `vp` and
  `websocket` drivers across N tasks, avoiding local NIC/CPU limits
  beyond ~50 concurrent streams.
* **`--direct-ddb` backfill** for 500k+ rows that bypass Kinesis entirely.
* **Share-matrix enrichment** for the `rbac` scenario — currently users
  own their meetings but we don't yet call `shareMeetings` randomly.
