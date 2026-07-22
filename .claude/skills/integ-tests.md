# Integration Testing — LMA (live-stack end-to-end)

Use this skill when asked to **run integration tests**, **verify a deployment**,
or **prove no regressions against a live stack** (e.g. after a dependency bump,
a stack change, or before a release). These tests hit a **real deployed LMA
stack**, unlike the mocked unit tests in `lib/lma_sdk/tests`.

The suite lives in `integ-tests/` and is driven by `make integ-tests`.

## What it validates (and why it exists)

The tests target runtime surfaces that unit tests and `tsc`/lint can't reach —
originally added to validate the PR #400 dependency batch:

- **Fastify 5 transcriber** — `/health/check` answers, and the transcriber ECS
  service has running (non-crash-looping) tasks.
- **AppSync GraphQL** — an IAM-signed query succeeds (the UI's data plane).
- **Virtual Participant** — registry CRUD lifecycle always; and an **opt-in**
  real meeting join that exercises the `@zoom/meetingsdk` path end to end.

## Pre-deploy guard (run this BEFORE deploying)

The transcriber and VP images are built by in-stack CodeBuild during deploy, so a
Dockerfile/build-context regression (e.g. a `COPY` of a renamed file) only shows
up ~15 min into a deploy and then rolls the whole stack back. Catch it locally in
~1-2 min first:

```bash
make docker-build-check        # transcriber image (fast; runs tsc + eslint 10)
make docker-build-check-all    # + Virtual Participant image (heavy)
```

Run this whenever you change a Dockerfile, delete/rename a file a Dockerfile
COPYs (eslint config, tsconfig, package.json), or bump build-time deps.

## Prerequisites

1. A deployed stack. To create a throwaway one from local code:
   ```bash
   AWS_PROFILE=default lma deploy --stack-name lma-integtest1 \
       --from-code . --admin-email <you>@amazon.com --wait
   ```
   (~35–40 min. Add Zoom params `-p ZoomMeetingSdkClientId=... -p ZoomMeetingSdkClientSecret=...`
   only if you intend to run the live Zoom join test.)
2. SDK + test deps in the venv: `make setup-cli-dev`
3. `AWS_PROFILE=default` (per repo convention — other profiles hit wrong accounts).

## Running

```bash
# Default: read-only checks + self-cleaning VP registry lifecycle. Repeatable.
make integ-tests STACK=lma-integtest1

# Opt-in: also place a REAL Virtual Participant into a live meeting.
make integ-tests-live STACK=lma-integtest1 PLATFORM=ZOOM \
    MEETING_ID=1234567890 MEETING_PASSWORD=secret
```

Stack resolution: `STACK=` → `$LMA_STACK_NAME` → `LMA`.

Direct pytest (finer control):
```bash
AWS_PROFILE=default .venv/bin/python -m pytest integ-tests/ \
    --stack-name lma-integtest1 -m "not live" -v
```

## Interpreting results

- **All pass** → the deployed stack's transcriber, AppSync, and VP surfaces are
  healthy. Safe to report no regression on those paths.
- **`test_transcriber_health_check` / `_ecs_service_running` fail** → the Fastify
  server likely failed to boot or is crash-looping. Check its logs:
  `lma logs --stack-name <stack>` or the transcriber ECS task in CloudWatch.
- **`test_appsync_reachable` fails** → AppSync URL resolution or IAM auth broke;
  confirm your creds and that the stack finished deploying.
- **`test_vp_live_join` fails** → the VP never left `INITIALIZING`; inspect the
  VP ECS task logs. For Zoom, cross-reference the per-signal checklist in
  `docs/dependency-upgrade-validation-runbook.md` (SDK load, join/admit, audio,
  active-speaker, chat, participant count, avatar).

## Cleanup

Delete a throwaway stack when done:
```bash
AWS_PROFILE=default lma delete --stack-name lma-integtest1 --wait
```

## Extending

Add tests to `integ-tests/test_lma_integration.py`. Use the session-scoped
`client` (a real `LMAClient`) and `outputs` fixtures from `conftest.py`. Keep
mutating tests self-cleaning (end/delete what you create in a `finally`). Mark
anything that touches a real meeting with `@pytest.mark.live` so the default run
stays side-effect-free.
