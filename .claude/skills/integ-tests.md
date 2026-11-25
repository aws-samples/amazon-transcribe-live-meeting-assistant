# Integration Testing — LMA (live-stack end-to-end)

Use this skill when asked to **run integration tests**, **verify a deployment**,
or **prove no regressions against a live stack** (e.g. after a dependency bump,
a stack change, or before a release). These tests hit a **real deployed LMA
stack**, unlike the mocked unit tests in `lib/lma_sdk/tests`.

The suite lives in `integ-tests/`.

## When an integration deploy is needed

A deploy-and-test run takes 20–40 minutes and a stack update, so run one only
when the change alters what a stack deploys or runs: Lambda, layer, AppSync,
UI, transcriber, Virtual Participant, ASR or browser-extension code, a
CloudFormation/SAM template, a Dockerfile or build context, a dependency or
lockfile of deployed code, `publish.sh`, or the SDK/CLI code that `lma deploy`
uses. It is **not** needed for:

- documentation, `CHANGELOG.md`, READMEs, images or the docs site;
- changes only to unit tests (`test_*.py`, `*.test.ts`/`.js`/`.jsx`,
  `lma-ai-stack/test/`, `lma-virtual-participant-stack/test/`) — CI runs those;
- `integ-tests/` plumbing alone (probes, conftest, JUnit summary): run the
  affected tests against the existing stack with `make integ-tests` if they
  need a live check, without redeploying;
- CI configuration, `.claude/` skills, `CLAUDE.md`, the review harness
  (`scripts/pr_review/`) or the demo recorder.

How to tell: list the change's files (`git diff --name-only origin/develop...HEAD`,
or `gh pr view <n> --json files`) and drop the categories above; if nothing is
left, there is nothing to deploy. `python3 scripts/ci/changed_areas.py
--files-from <list>` prints the areas, which is a quick first pass: only `docs`,
`agent_instructions`, `pr_review`, `demo_recorder`, `integ_plumbing`,
`ai_stack_tests` or `no_ci_coverage` means no deploy is needed, while a code area
such as `ui` or `vp` needs one unless every file it lists is a unit test.
(`no_ci_coverage` includes the desktop capture app's code, which is deployable but
not exercised by this suite either.) Re-deploying for a
CHANGELOG conflict resolution or a docs fix after an earlier head already
passed its integration run repeats that run for nothing; the nightly run
(`nightly_integ_tests`) re-tests `develop` after the merge anyway.

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

# Desktop capture app video: a narrated slideshow -> one key frame per slide,
# images in S3, and the assistant reading the last slide (~2 min; Polly+Bedrock).
# Needs also: pip install -r integ-tests/requirements-video.txt
LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \
    AWS_PROFILE=default make integ-tests STACK=lma-integtest1 LMA_TEST_DESKTOP_VIDEO=1

# Meeting consent on the Stream Audio path (~3 min; changes the deployment's
# Meeting Consent setting for the run and puts it back in teardown). Needs Pillow,
# from requirements-video.txt, for the evidence image:
LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \
    AWS_PROFILE=default make integ-tests STACK=lma-integtest1 LMA_TEST_CONSENT=1

# Real Virtual Participant join into a live meeting:
make integ-tests-live STACK=lma-integtest1 PLATFORM=ZOOM \
    MEETING_ID=1234567890 MEETING_PASSWORD=secret

# A VP in a real meeting nobody else attends (~5 min): join, ACTIVE, meeting
# record, then its own exit when alone and an end from LMA. The meeting comes
# from the Secrets Manager secret lma/integ-tests/live-meeting
# (or $LMA_TEST_LIVE_MEETING_SECRET_ID); see docs/scheduled-integration-tests.md.
LMA_TEST_VP_LIVE_MEETING=1 AWS_PROFILE=default make integ-tests STACK=lma-integtest1
```

Stack resolution: `STACK=` → `$LMA_STACK_NAME` → `lma-integtest1` (deploy) / `LMA` (test-only).

**On a schedule.** A nightly GitLab pipeline (`nightly_integ_tests`) runs
`make integ-tests-nightly` against `lma-integtest1`, which is in **us-west-2** —
not the region a default profile resolves to. That target tests an existing stack
and never deploys one, and a *skipped* test fails it (`--no-skips`), so a lapsed
secret cannot report green. It takes the Cognito user from Secrets Manager via
`LMA_TEST_USER_SECRET_ID` rather than from the environment. See
`docs/scheduled-integration-tests.md`. When asked to debug a failing nightly,
start from the JUnit report on the pipeline, not the job log.

The job's `after_script` summarises that report with `integ-tests/junit_summary.py`
and posts it to Slack via a masked `SLACK_WEBHOOK_URL`, on a pass as well as a
failure; the payload is also printed in the log and kept as `slack-payload.json`.
Two things to know before changing it: the four payload keys (`status`, `commit`,
`job_url`, `summary`) are a contract with a Slack **Workflow Builder** trigger,
which silently ignores keys it does not declare, so a rename posts a blank
variable rather than failing; and the summary must stay free of Slack's
`<url|label>` syntax, backticks and asterisks, which Workflow Builder renders
literally. `junit_summary.py` must never raise — it runs after a job that may
have died anywhere — and its cases are covered in `test_ci_plumbing.py`.

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
| `test_vp_live_meeting.py` *(opt-in, `LMA_TEST_VP_LIVE_MEETING=1`)* | VP joins a real meeting nobody else attends, reaches `ACTIVE`, gets a meeting record; left alone it leaves as `COMPLETED`/`attendees-left`; ended from LMA it shows the end reason (`COMPLETED` required under FARGATE/EC2); the meeting record ends | the VP's join, empty-meeting exit and end path on a real platform |
| `test_meeting_preparation.py` | VP scheduled a day ahead stores labels/audience/context/checklist and an edit before start changes them (VP cancelled after); synthetic START with them creates a meeting that has them; a user's status survives a replace and refuses the assistant; transcript deletion strips quotes, keeps `SegmentId`/`StartTime`. Live part (`LMA_TEST_VP_LIVE_MEETING=1`): VP scheduled 6 min ahead and edited creates its meeting with the edits | meeting preparation resolver, VP create/scheduled-edit pipelines, START path, transcript deletion |
| `test_vp_video_setting.py` *(launch case opt-in, `LMA_TEST_VP_VIDEO_LAUNCH=1`)* | with Meeting Recording › Record Virtual Participant meeting video off (active profile, restored in teardown): `getVirtualParticipantVideoRecording` answers false; a VP scheduled a day ahead asking for video gets `videoRecordingOffReason` `configuration` from the create (cancelled after); a VP created with the setting on, then launched at a stale Zoom meeting ID after it is turned off, writes `configuration` itself at start (opt-in: few minutes, places a VP task; ended after) | createVirtualParticipant setting check, the form's read, the VP's check at start (`video-recording-rule.ts`) |
| `test_desktop_video.py` *(opt-in, `LMA_TEST_DESKTOP_VIDEO=1`)* | synthetic slideshow over the desktop app's video socket → 5 key frames at the slide times, both JPEGs in S3, assistant quotes the unspoken last slide | desktop key frames (`videokeyframes.ts`); the assistant's shared-screen tool once #891 is deployed |
| `test_meeting_export.py` *(Cognito test user; ~12 model calls)* | START with `SummaryProfile: discovery-session` + checklist, conversation and chat, END → summary with that profile's sections and `ActionItems` (owners named in the meeting); `get_meeting_export` validates against the schema; `list_summary_profiles`, `update_meeting_metadata summaryProfile` (unknown refused), `regenerate_meeting_summary` with `healthcare` (meeting's profile kept, action items cleared); `prepareMeetingArchive(EXPORT)` → one JSON file, 600 s link; >6 MB transcript → `exportUrl` ≤ 10 min; after transcript deletion the export leaves it out; a spoken reply recorded with its `SpeakerLink` (backend role, assumed-role only) → `speakerId` `remote-spk_0` + `granted` in `get_meeting_consent` and the export, other replies unlinked, kept through a rename, link kept without a speaker after transcript deletion; lines carrying a `PlatformParticipantId` and a chat reply with a `platform_identity` link → that speaker's `speakerId` + `granted`, a non-matching id unlinked, the raw id in neither answer and removed from the consent record by transcript deletion; another user's meeting refused. `LMA_TEST_MEETING_EXPORT_OUT` keeps the export | summary profiles, structured action items, `meeting_archive` export, MCP export/consent/profile tools, consent speaker links |
| `test_meeting_archive.py` *(opt-in, `LMA_TEST_MEETING_ARCHIVE=1`)* | a synthetic meeting's Everything archive: `prepareMeetingArchive` → READY → the signed link downloads a ZIP whose README lists every file, with the summary, transcript and key frame in it and no consent record for a backend caller; the meeting and archive are deleted afterwards | the meeting archive function (`meeting_archive/`), its resolvers, IAM and the archive prefix |
| `test_meeting_consent.py` *(opt-in, `LMA_TEST_CONSENT=1`)* | sets `meeting_consent` to opt-in / owner attests, then notify, through the `lma_config` store with IAM (the test user is not Admin), restoring it in teardown: unconfirmed opt-in START refused (`CONSENT_NOT_CONFIRMED`, no meeting, no segments); attested opt-in transcribed with attestation, notice, evidence JPEG in `lma-consent-evidence/<meeting>/` and a stored evidence check; notify objection closes the socket, stops segments, record `objected`, meeting `ENDED` | the transcriber's consent gate (`consent/` in the transcriber) and the consent resolver and verifier behind it |
| `test_hosting_presets.py` | the `team-hosted-external` and `customer-account` Managed profiles hold every value their presets set; synthetic meetings with a unique label, owned by a temporary user and by another owner, one with another label value and one still in progress, are found by `lma meetings delete` run through the CLI as the temporary user (their own) and as a temporary Admin (every user's); the dry run deletes nothing, the meeting in progress is skipped, and `--confirm` removes each matching meeting's record, list entry, transcript items and files in the recordings bucket and nothing else (the two temporary Cognito users, created with no password and no invitation message, in the default run, and the meetings are removed afterwards). With `LMA_TEST_HOSTING_ASSISTANT=1` it also sets **In-meeting requests: who may ask** to Off for the run (put back afterwards) and checks a meeting-chat request is refused while the meeting page's is answered | the configuration seeder's Managed profiles; the SDK's meeting search and the per-meeting delete path; the meeting assistant's requester check |
| `test_meeting_data_point_in_time_recovery.py` | read-only: each meeting content table of the AI stack (meetings, Virtual Participant, Virtual Participant task registry, troubleshooting jobs, and voice enrollment where deployed) reports point-in-time recovery off when `MeetingDataPointInTimeRecoveryDays` is 0 and on with that recovery period otherwise, and the configuration table, the voice consent log and the skills table keep 35 days (skips on a stack that predates the parameter) | the `MeetingDataPointInTimeRecoveryDays` wiring from the main stack to the tables |
| `test_minimised_content_preset.py` *(about five minutes, three model calls)* | the `minimised-content` Managed profile holds every value its preset sets, **Meeting Assistant › Leave out sensitive personal information** on among them; the default and Managed profiles hold the `minimised-content` summary profile, every section carrying the rule; *model-dependent* (marker `model_dependent`): a synthetic meeting started with that profile, in which a participant names a health condition and its treatment as the reason for an action item, is summarized with its sections and the summary names neither (a failure prints the sections). Changes no deployment-wide setting; removes its meeting, transcript items and files | the configuration seeder's Managed profiles and shipped summary profiles; the summarizer's profile selection; the model following the profile's instruction |
| `test_assistant_profile.py` *(opt-in, `LMA_TEST_ASSISTANT_PROFILE=1`)* | with the Meeting Assistant section changed for the run and put back afterwards: the profile's assistant instructions shape an answer; under Selected tools, an installed MCP server flagged off for in-meeting requests is not offered to a meeting-chat request and is to the owner's (read from the assistant's log); the MCP server library export holds no credential, re-importing it changes no server, and an imported server is created waiting for its credentials (its probe row removed afterwards). Needs an installed, active MCP server for the per-server check | the profile's assistant instructions and tool selection; the MCP server library export and import |
| `test_assistant_skills.py` *(opt-in, `LMA_TEST_ASSISTANT_SKILLS=1`)* | creates a probe skill with `client.skills` (deleted afterwards) and, with the Meeting Assistant section changed for the run and put back afterwards: an answer to a question its description matches follows its instructions, which the assistant has to load; under Selected tools with the skills switch on, the skill flagged off for in-meeting requests is not offered to a meeting-chat request and is to the owner's (read from the assistant's `Skills: offered=` log line); the probe skill's library entry, imported back, is found unchanged and not written | the meeting assistant's skills: progressive disclosure, the per-skill selection, and the skill library export and import |

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
- **`test_desktop_video_keeps_one_key_frame_per_slide`** → zero frames: the
  transcriber image predates desktop key frames, or `EnableVideoRecording=false`;
  look for `[KEYFRAMES]` lines in the transcriber's logs. Wrong count or times:
  the detector thresholds in `videokeyframes.ts`. Slide times follow Polly's
  narration lengths; `python integ-tests/desktop_video_probe.py --media-only DIR`
  prints the generated video's key frame times to compare against.
- **`test_assistant_reads_the_last_slide`** → skipped means the deployed
  assistant has no `meeting_frames.py` (the tool comes with #891, not yet merged); a wrong answer means the tool did not
  run or the model could not read the image (Nova Micro reads no images).
- **`test_meeting_consent.py`** → failing at setup with "did not answer
  CONSENT_SETTINGS" means the transcriber image predates the Stream Audio
  consent step, and nothing was changed. A missing evidence check means the
  evidence check model is not enabled in Bedrock (see the
  `ConsentEvidenceVerifierFunction` and `MeetingConsentResolverFunction` logs).
  If a run was killed before its teardown, check the Meeting Consent section on
  the Configuration page: the test's saves are in the configuration history.
- **`test_vp_live_join`** → VP never left `INITIALIZING`; inspect VP ECS task logs
  and the Zoom per-signal checklist in the runbook.
- **`test_vp_live_meeting.py`** → each failure prints the statuses the VP went
  through and its `endTrigger` / `errorMessage`, with the meeting redacted. An error
  at setup naming the secret means the flag is on but the secret is missing or
  malformed. "Did not join" with a `pre-join:` trigger means the meeting is not open
  to guests without a host. "Did not start transcribing" on a stack whose Meeting
  Consent mode is opt-in is expected, because nobody consents in an empty meeting.
  "Still ACTIVE in a meeting with nobody else in it" means the attendee watchdog did
  not fire, unless a person or a leftover participant is actually in the meeting.

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
