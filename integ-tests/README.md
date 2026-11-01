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

# Or take that user from a Secrets Manager secret instead of the environment
# ({"username": ..., "password": ...}) — how the scheduled run does it:
LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \
    make integ-tests STACK=lma-integtest1
```

### On a schedule

`make integ-tests-nightly` is what the `nightly_integ_tests` GitLab job runs. It
differs from `make integ-tests` in two ways that matter when nobody is watching:
a **skipped test fails the run** (`--no-skips`), so a lapsed secret or a dropped
dependency cannot report green having streamed no audio; and it writes a JUnit
report for the CI UI. It tests an existing stack and does not deploy one.

Afterwards the job summarises its JUnit report with `junit_summary.py` and posts
the result to Slack, on a pass as well as a failure.

See [Scheduled Integration Tests](../docs/scheduled-integration-tests.md) for the
one-time GitLab schedule, CI role, test-user and Slack-webhook setup it needs.

The machinery those behaviours rest on — `cognito_test_user.py`, the `--no-skips`
hook in `conftest.py`, and `junit_summary.py` — is unit-tested in
`test_ci_plumbing.py`, which needs no AWS and runs in the fast pipeline via
`make test-integ-plumbing`.

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
| `test_desktop_video.py` *(opt-in, `LMA_TEST_DESKTOP_VIDEO=1`)* | a narrated synthetic slideshow streamed as the desktop capture app streams it → one key frame per slide, at the right times, with both images in S3; the meeting assistant reads the last slide | desktop video key frames and the assistant's shared-screen tool |

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

## Voice enrollment consent lifecycle

`test_voice_enrollment_consent_lifecycle` grants consent, enrols a voice from
Polly-synthesised speech, and withdraws — then reads DynamoDB to check what was
actually stored, because the stored records are what the consent terms promise.
It exists as an integration test rather than more unit tests precisely because
no amount of review can establish that a consent row lands in the table, that a
MicroVM really computes a voiceprint from real audio, or that a withdrawal
really destroys it.

Requires `LMA_TEST_USERNAME` / `LMA_TEST_PASSWORD` like the streaming probe, and
a stack with `EnableVoiceEnrollment=true` **and** `EnableMicrovmAsr=true` —
enrolling needs the on-demand engine, because that is what computes speaker
embeddings. It skips cleanly on a deployment without the feature, which is the
default. Self-cleaning: it withdraws at the end, which deletes the voiceprint it
created.

## Desktop video key frames

`test_desktop_video.py` streams a short narrated slideshow into the stack the
way the desktop capture app does, and checks that the transcriber keeps one key
frame per slide. It is opt-in, because it takes about two minutes and calls
Polly and Bedrock:

```bash
.venv/bin/pip install -r integ-tests/requirements.txt -r integ-tests/requirements-video.txt
AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \
    make integ-tests STACK=lma-integtest1 LMA_TEST_DESKTOP_VIDEO=1

# Only this file:
AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user LMA_TEST_DESKTOP_VIDEO=1 \
    .venv/bin/python -m pytest integ-tests/test_desktop_video.py --stack-name lma-integtest1 -s
```

`desktop_video_probe.py` builds the media and does the streaming:

- **Slides.** Five 1920 × 1080 slides with large, distinct text, rendered with
  Pillow, each held eight seconds. Polly (Matthew, neural) narrates each one on
  the left (meeting) channel; the microphone channel is silent. The last slide,
  "Launch codename: BLUE HERON / Ship date: March 14", is never spoken, so an
  answer that quotes it can only come from the stored picture.
- **Encoding.** As `VideoCapture.swift` and `VideoCapture.cs` encode: H.264 Main
  at 5 fps and about 1.5 Mbit/s, a key frame every two seconds, as a fragmented
  MP4 (init segment, then one `moof`/`mdat` fragment per two seconds). The ffmpeg
  is `LMA_TEST_FFMPEG`, else `ffmpeg` on PATH, else the one `imageio-ffmpeg`
  bundles; it needs libx264.
- **Streaming.** The narration goes over the Stream Audio socket in real time,
  as `ws_stream_probe.py` sends it. Half a second in, a second socket to the
  same endpoint, with the same token, sends `START_VIDEO` with
  `videoTimeOffsetMs: 1000`, then each fragment when the app's encoder would
  have finished it (the init segment with the first), then `END_VIDEO`, as
  `VideoSocket.swift` and `VideoSocket.cs` do. The audio `END` follows.

What it asserts:

| Test | Asserts |
|------|---------|
| `test_desktop_video_streams_alongside_audio` | both sockets stayed open until their end events, every fragment was sent, and the meeting has transcript segments |
| `test_desktop_video_keeps_one_key_frame_per_slide` | exactly five `kf#<CallId>` items, all `Source=DESKTOP_CAPTURE`, timestamps strictly increasing and each within 2.5 s of its slide's start on the transcript's clock (1, 9, 17, 25 and 33 s with the current narration), each with a `VideoOffset` |
| `test_desktop_video_key_frame_images_are_stored` | both JPEGs of every frame exist in the recordings bucket, under `lma-video-keyframes/<meeting>/` |
| `test_assistant_reads_the_last_slide` | invokes `STRANDS-<stack>-MeetingAssist` with "What did the last slide say?" and expects "heron" in the answer. Skips, even with the flag, when the deployed function's package has no `meeting_frames.py`: the shared-screen tool comes with pull request #891, which is not merged yet |

With the flag set, a missing dependency, ffmpeg or test user fails the run
rather than skipping. Without it the four tests are reported as skipped,
including under `make integ-tests-nightly`, because they carry the
`optional_feature` marker. Zero key frames usually means the transcriber image
predates desktop key frames or the stack has `EnableVideoRecording=false`.
`--media-only` (below) prints the key frame times of the generated video, which
are where the slides should land, offset by one second.

The meeting (`desktop-video-probe-<UTC time>`) is not deleted: it expires with
the deployment's transcript retention, and its Shared screen strip can be
looked at on the meeting page. To build only the media, for a look at the slides
and the fragment sizes, without a stack:
`AWS_PROFILE=default python integ-tests/desktop_video_probe.py --region us-west-2 --media-only /tmp/slides`
(this calls Polly).
