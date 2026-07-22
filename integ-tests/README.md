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
   ```
3. AWS credentials for the account the stack lives in (`AWS_PROFILE=default`).

## Running

```bash
# Default (read-only + self-cleaning VP registry lifecycle) — safe to repeat:
make integ-tests STACK=lma-integtest1

# Include a REAL Virtual Participant join into a live meeting (Zoom SDK 6 path):
make integ-tests-live STACK=lma-integtest1 PLATFORM=ZOOM \
    MEETING_ID=1234567890 MEETING_PASSWORD=secret
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
| `test_transcriber_health_check` | `GET /health/check` returns 200/503 with expected JSON | **Fastify 3→5 boot** |
| `test_transcriber_ecs_service_running` | transcriber ECS service has running tasks, not crash-looping | **Fastify 5 runtime stability** |
| `test_appsync_reachable` | IAM-signed GraphQL query succeeds | AppSync data plane |
| `test_vp_registry_lifecycle` | VP create → get → list → end via AppSync (no meeting) | VP CRUD surface |
| `test_vp_live_join` *(opt-in)* | VP actually joins a real meeting and leaves `INITIALIZING` | **@zoom/meetingsdk 4→6** |

`test_vp_live_join` is marked `live` and **skipped** unless `--vp-meeting-id`
(`MEETING_ID=`) is provided.

## Notes

- The default run is **idempotent and self-cleaning**: the VP registry test ends
  every row it creates in a `finally` block.
- These are intentionally dependency-light (stdlib `urllib` + `boto3`/`lma_sdk`),
  so they add no new packages to the repo.
- For the manual/observational Zoom checklist (active-speaker, chat commands,
  DOM selectors), see `docs/dependency-upgrade-validation-runbook.md`.
