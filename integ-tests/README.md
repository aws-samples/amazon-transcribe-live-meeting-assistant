# LMA Integration Tests

End-to-end tests that run against a **live deployed LMA stack** (not mocks).
They validate the runtime surfaces that unit tests and static analysis can't
reach — in particular the areas touched by the dependency-upgrade batch
(PR #400): the Fastify 5 WebSocket transcriber, the AppSync GraphQL data plane,
and the Virtual Participant launch path (Zoom SDK 6).

## Prerequisites

1. A deployed LMA stack. To create a throwaway one from local code:
   ```bash
   AWS_PROFILE=default lma deploy --stack-name lma-integtest1 \
       --from-code . --admin-email you@example.com --wait
   ```
2. The LMA SDK + test deps installed in the repo venv:
   ```bash
   make setup-cli-dev
   # for the opt-in WebSocket streaming test only:
   .venv/bin/pip install -r integ-tests/requirements.txt
   ```
3. AWS credentials for the account the stack lives in (`AWS_PROFILE=default`).

## Running

```bash
# Default (read-only + self-cleaning VP registry lifecycle) — safe to repeat:
make integ-tests STACK=lma-integtest1

# Include a REAL Virtual Participant join into a live meeting (Zoom SDK 6 path):
make integ-tests-live STACK=lma-integtest1 PLATFORM=ZOOM \
    MEETING_ID=1234567890 MEETING_PASSWORD=secret

# Include the live WebSocket streaming test (needs a Cognito user's creds):
LMA_TEST_USERNAME=you@example.com LMA_TEST_PASSWORD=... \
    make integ-tests STACK=lma-integtest1
```

Or invoke pytest directly:
```bash
AWS_PROFILE=default .venv/bin/python -m pytest integ-tests/ \
    --stack-name lma-integtest1 -m "not live"
```

Stack resolution order: `--stack-name` / `STACK=` → `$LMA_STACK_NAME` → `LMA`.

## What each test covers

| Test | Validates | Batch risk it guards |
|------|-----------|----------------------|
| `test_stack_status_is_complete` | stack is in a `*_COMPLETE` (non-rollback) state | deploy sanity |
| `test_required_output_present[*]` | key CFN outputs exist & non-empty | UI/extension wiring |
| `test_transcriber_alb_targets_healthy` | transcriber ALB target group has a HEALTHY target (server passing `/health/check`) | **Fastify 3→5 boot** |
| `test_transcriber_ecs_service_running` | transcriber ECS service has running tasks, not crash-looping | **Fastify 5 runtime stability** |
| `test_appsync_reachable` | IAM-signed GraphQL query succeeds | AppSync data plane |
| `test_kds_pipeline_creates_meeting` | synthetic START on Kinesis → CallEventProcessor → meeting row in DynamoDB | **gql/AppSync introspection (the gql 4 regression)** |
| `test_ws_stream_connects` *(opt-in)* | stream audio over the live `wss://.../api/v1/ws` and stay OPEN | **@fastify/websocket 11 upgrade ordering (the WS 500 regression)** |
| `test_vp_registry_lifecycle` | VP create → get → list → end via AppSync (no meeting) | VP CRUD surface |
| `test_vp_live_join` *(opt-in)* | VP actually joins a real meeting and leaves `INITIALIZING` | **@zoom/meetingsdk 4→6** |

`test_vp_live_join` is marked `live` and **skipped** unless `--vp-meeting-id`
(`MEETING_ID=`) is provided.

`test_kds_pipeline_creates_meeting` puts a synthetic `START` event on the
CallDataStream (empty AccessToken → the processor uses `AgentId` as Owner, the
documented Virtual-Participant service-call path) and polls the EventSourcing
DynamoDB table for the resulting call record, then deletes it. It is the
regression guard for the gql 4 → AppSync incompatibility: the CallEventProcessor
introspects the AppSync schema to build its DSL mutations, gql 4's introspection
query is rejected by AppSync, so `createCall` never ran and meetings never
appeared in the UI. The probe is reusable standalone:
`AWS_PROFILE=default python integ-tests/kds_pipeline_probe.py <stack-name>`.

## Notes

- `/health/check` is the **ALB target-group** health path and is deliberately
  NOT routed through the public CloudFront distribution (which only forwards
  `/api/v1/ws`), so it can't be probed over the internet. The transcriber health
  test therefore asserts on ALB **target health** (via `elbv2`), not an HTTP GET.
- The default run is **idempotent and self-cleaning**: the VP registry test ends
  every row it creates in a `finally` block.
- These are intentionally dependency-light (stdlib `urllib` + `boto3`/`lma_sdk`),
  so they add no new packages to the repo.
- For the manual/observational Zoom checklist (active-speaker, chat commands,
  DOM selectors), see `docs/dependency-upgrade-validation-runbook.md`.
