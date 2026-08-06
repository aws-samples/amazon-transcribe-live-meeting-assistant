# Integration Testing — LMA (live-stack end-to-end)

Use this skill when asked to **run integration tests**, **verify a deployment**,
or **prove no regressions against a live stack** (e.g. after a dependency bump,
a stack change, or before a release). These tests hit a **real deployed LMA
stack**, unlike the mocked unit tests in `lib/lma_sdk/tests`.

The suite lives in `integ-tests/`.

## How to run it (the main flow)

**One-shot: deploy-or-update a stack, then test.** This is the default entry
point — it updates the stack if it already exists, or creates it if it doesn't:

```bash
# Existing stack (or default 'lma-integtest1'): builds local code, UPDATES it, tests.
AWS_PROFILE=default make integ-deploy-and-test STACK=lma-integtest1

# New stack (does not exist yet): ADMIN_EMAIL is required to create it.
AWS_PROFILE=default make integ-deploy-and-test STACK=lma-newtest ADMIN_EMAIL=you@amazon.com
```

Decision rule for the skill when asked to "run integration tests":
- If the user names a stack → `make integ-deploy-and-test STACK=<that>` (updates it).
- If they don't → default to `lma-integtest1`. If it exists, update+test; if not,
  create it (needs `ADMIN_EMAIL` — ask the user for one, or reuse the repo's
  known admin email if they've given it before).
- Deploy takes ~35–40 min for a create, less for an update. Run it in the
  background and report when done.

**Test-only (stack already deployed and current):**
```bash
AWS_PROFILE=default make integ-tests STACK=lma-integtest1
```

**Include the opt-in tests** (real audio + real meeting join):
```bash
# Real WebSocket audio streaming (needs a Cognito user's creds):
LMA_TEST_USERNAME=you@example.com LMA_TEST_PASSWORD=... \
    AWS_PROFILE=default make integ-tests STACK=lma-integtest1

# Real Virtual Participant join into a live meeting:
make integ-tests-live STACK=lma-integtest1 PLATFORM=ZOOM \
    MEETING_ID=1234567890 MEETING_PASSWORD=secret
```

Stack resolution: `STACK=` → `$LMA_STACK_NAME` → `lma-integtest1` (deploy) / `LMA` (test-only).

## Prerequisites

1. `make setup-cli-dev` — LMA SDK + CLI + test deps in `.venv`.
2. For the opt-in WebSocket audio test: `.venv/bin/pip install -r integ-tests/requirements.txt`
   (pycognito + websockets) and `LMA_TEST_USERNAME` / `LMA_TEST_PASSWORD`.
3. `AWS_PROFILE=default` (per repo convention — other profiles hit wrong accounts).

## Pre-deploy guard (run BEFORE any deploy)

The transcriber and VP images are built by in-stack CodeBuild during deploy, so a
Dockerfile/build-context regression only shows up ~15 min into a deploy and then
rolls the whole stack back. Catch it locally in ~1–2 min first:
```bash
make docker-build-check        # transcriber image (fast; runs tsc + eslint)
make docker-build-check-all    # + Virtual Participant image (heavy)
```

## What it validates (and the regressions each guards)

| Test | Validates | Guards |
|------|-----------|--------|
| `test_stack_status_is_complete` | stack in a `*_COMPLETE` state | deploy sanity |
| `test_required_output_present[*]` | key CFN outputs present | UI/extension wiring |
| `test_transcriber_alb_targets_healthy` | ALB target HEALTHY (Fastify 5 serving) | fastify 3→5 boot |
| `test_transcriber_ecs_service_running` | ECS tasks running, not crash-looping | fastify 5 stability |
| `test_appsync_reachable` | IAM-signed GraphQL query works | AppSync data plane |
| `test_kds_pipeline_creates_meeting` | synthetic START → meeting in DynamoDB | **gql 4→AppSync** |
| `test_ws_stream_transcribes_to_meeting` *(opt-in)* | stream a real WAV → meeting **+ transcript segments** | **@fastify/websocket 11 upgrade + Transcribe + gql** |
| `test_vp_registry_lifecycle` | VP create→get→list→end (no meeting) | VP CRUD surface |
| `test_vp_live_join` *(opt-in)* | VP joins a real meeting | `@zoom/meetingsdk` |

`test_ws_stream_transcribes_to_meeting` is the strongest single check — it streams
`utilities/load-simulator/lma_load/fixtures/stereo-demo-call.wav` (real speech; the
sibling `stereo-16k-30s.wav` is a synthetic tone that won't transcribe) over the
live `wss://.../api/v1/ws` and asserts the socket stays OPEN, a meeting row is
written, and transcript segments are produced.

## Interpreting failures

- **`test_transcriber_alb_targets_healthy` / `_ecs_service_running`** → Fastify
  server failed to boot or crash-loops. `lma logs --stack-name <stack>` / ECS task logs.
- **`test_appsync_reachable`** → AppSync URL/IAM auth broke; check creds + deploy state.
- **`test_kds_pipeline_creates_meeting`** → Kinesis→CallEventProcessor→AppSync broken
  (usual cause: a gql/AppSync schema-introspection failure). Check CallEventProcessor logs.
- **`test_ws_stream_transcribes_to_meeting`** → if it connects but 0 segments,
  Transcribe streaming or `processTranscriptionResults` is broken; if it 500s on
  connect, check the `@fastify/websocket` route registration ordering (must be in
  `server.after()`). Cross-reference `docs/dependency-upgrade-validation-runbook.md`.
- **`test_vp_live_join`** → VP never left `INITIALIZING`; inspect VP ECS task logs
  and the Zoom per-signal checklist in the runbook.

## Cleanup

Delete a throwaway stack when done:
```bash
AWS_PROFILE=default lma delete --stack-name lma-integtest1 --yes
```
(Meeting/transcript rows created by the tests are self-cleaned by the tests.)

## Extending

Add tests to `integ-tests/test_lma_integration.py`. Use the session-scoped
`client` (a real `LMAClient`) and `outputs` fixtures from `conftest.py`. Keep
mutating tests self-cleaning (delete what you create in a `finally`). Mark
anything that needs a real meeting or user creds `@pytest.mark.live` or gate it on
env vars + `pytest.skip` so the default `make integ-tests` run stays green and
side-effect-free. The reusable probes are `kds_pipeline_probe.py` (Kinesis→DDB)
and `ws_stream_probe.py` (real WAV over the WebSocket).
