---
title: "Scheduled Integration Tests"
---

<!--
Copyright (c) 2025 Amazon.com
This file is licensed under the MIT License.
See the LICENSE file in the project root for full license information.
-->

# Scheduled Integration Tests

The suite in `integ-tests/` runs against a **live deployed LMA stack**. It is the
only thing in this repository that exercises the WebSocket transcriber, Amazon
Transcribe, Kinesis, the Call Event Processor and AppSync together in one pass —
and it has historically run only when someone remembered to run it.

The `nightly_integ_tests` job in `.gitlab-ci.yml` runs it on a schedule instead.
This page is the one-time setup that job depends on. Until it is done the job
does not run at all, and if it is half-done the job fails rather than passing
quietly.

It is GitLab-only. The GitHub repository is public and the stack lives in an
internal AWS account, whose id cannot be committed here; the GitLab mirror's
runners and masked variables are where that belongs.

## What the nightly run does

It runs `make integ-tests-nightly`, which is `make integ-tests` plus two things
that matter when nobody is watching the run:

| | `make integ-tests` | `make integ-tests-nightly` |
|---|---|---|
| Real-meeting tests (`-m "not live"`) | excluded | excluded |
| A skipped test | skips | **fails the run** (`--no-skips`) |
| Report | terminal output | JUnit XML, attached to the pipeline |
| Cognito test user | `LMA_TEST_USERNAME` / `LMA_TEST_PASSWORD` | Secrets Manager |

The `--no-skips` behaviour is the important one. The strongest test in the suite,
`test_ws_stream_transcribes_to_meeting`, skips itself when its credentials or its
optional dependencies are missing — correct for a local run, and wrong for a
scheduled one, where it would mean a green pipeline that streamed no audio and
checked no transcript. Under `--no-skips` a skip is a failure, so a lapsed secret
or a dropped requirement reports red. Tests excluded by a marker (`live`) are
still allowed to skip; see `integ-tests/conftest.py`.

The nightly **tests an existing stack and does not deploy one.** A deploy takes
35–40 minutes and is far likelier to fail on its own account than to tell you
anything about the code, which would make the nightly's signal useless. Update
the stack deliberately instead, with `make integ-deploy-and-test`, when you want
the deployed code refreshed.

## How a failure reaches you

Four independent paths, so a red nightly cannot go unnoticed:

1. **Slack.** The job's `after_script` posts the result — pass or fail — to a
   channel, with the stack name, the pass/fail/skip counts, the names and
   assertion messages of any failures, and a link to the job. Needs the
   `SLACK_WEBHOOK_URL` variable from [step 4](#4-cicd-variables); absent, the job
   logs one line and skips the post. Setup is in
   [step 6](#6-optional-the-slack-notification).
2. **Email.** GitLab's **scheduled-pipeline failure notification** goes to the
   owner of the schedule. This is on by default for scheduled pipelines; confirm
   the schedule's owner is a person who reads that mailbox, because the schedule
   is owned by whoever created it. For a distribution list instead of one
   person, add the *Pipeline status emails* integration under **Settings >
   Integrations**, with "Notify only broken pipelines" on. ⚠️ That integration
   cannot filter by pipeline **source**, so it will also mail every failed
   `develop` push pipeline, not just the nightly.
3. **The JUnit report** is attached to the pipeline, so the failing test and its
   assertion message are visible on the pipeline page without opening a log.
4. **`--no-skips`**, as above: the failure modes that would otherwise be silent
   become ordinary test failures.

⚠️ **None of these is a heartbeat.** All four are event-driven, so a schedule
that silently stops firing produces exactly the same quiet as a month of passing
runs. **Build > Pipeline schedules** shows the last run and its status per
schedule, and is the only view that answers "did it run at all".

### Why the notification is posted by the job

GitLab's own Slack integration, and its pipeline **badge**, both key off
`pipeline_events` for a branch and cannot filter by pipeline **source**. Either
one would therefore report every push pipeline on `develop` as well, which is
the majority of them and is not what anybody subscribed for. The job's
`after_script` is the only place that knows the run was the scheduled one, and
the only place that has the JUnit report to summarise.

## One-time setup

### 1. A long-lived target stack

The convention is `lma-integtest1`, which already exists **in `us-west-2`** —
note that this is not the region a default profile resolves to. It has to be
updated in place rather than deleted and recreated, because `us-west-2` is at its
VPC quota and this stack's VPC is one of the five.

```bash
# Refresh the deployed code when you want to (NOT what the nightly does):
AWS_PROFILE=default make integ-deploy-and-test STACK=lma-integtest1
```

### 2. A Cognito test user, in Secrets Manager

Create a user in the stack's user pool for the audio streaming test, then store
its credentials as a JSON secret. The tests read the secret directly, so the
password never becomes a CI variable or reaches a job log.

```bash
aws secretsmanager create-secret \
    --region us-west-2 \
    --name lma/integ-tests/cognito-user \
    --description "Cognito user for the scheduled LMA integration tests" \
    --secret-string '{"username":"integ-tests@example.com","password":"REPLACE_ME"}'
```

The accepted keys are `username` (or `email`) and `password`. A secret that is
missing a key, or is not JSON, fails the run with a message naming the problem —
it does not fall back to skipping. See `integ-tests/cognito_test_user.py`.

### 3. An IAM role the pipeline can assume

The job authenticates with the OIDC token GitLab mints for it and assumes a role;
no long-lived access key is stored anywhere. Register the GitLab instance as an
OIDC identity provider in the account, then create a role trusting it, restricted
to this project and to protected-branch pipelines.

The role needs read access to the stack and its resources (CloudFormation,
ELBv2, ECS, AppSync, Kinesis, Cognito), read/write on the EventSourcing DynamoDB
table (the suite creates rows and deletes them), and
`secretsmanager:GetSecretValue` on the one secret above. Scope it to the test
stack's resources — it does not need administrator access, and should not have it.

A trust policy of this shape. Replace `123456789012` with the account id,
`gitlab.example.com` with your GitLab instance's host, and the `sub` claim with
your project path:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::123456789012:oidc-provider/gitlab.example.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "gitlab.example.com:aud": "https://gitlab.example.com"
      },
      "StringLike": {
        "gitlab.example.com:sub": "project_path:YOUR-GROUP/LMA:ref_type:branch:ref:develop"
      }
    }
  }]
}
```

The audience must be the GitLab instance's own URL. The job asks for the token
with `aud: $CI_SERVER_URL`, a predefined variable that already holds exactly
that, so there is nothing to keep in step — but if you override the audience,
change it in both places.

Narrowing `sub` to a branch is what keeps a pipeline on an arbitrary branch from
assuming this role. Restrict the OIDC provider to protected branches as well, so
the claim cannot be produced from an unreviewed ref.

### 4. CI/CD variables

Under **Settings > CI/CD > Variables**, or on the schedule itself:

| Variable | Required | Notes |
|---|---|---|
| `LMA_INTEG_ROLE_ARN` | yes | The role from step 3. **Mask it** — it contains the account id, which must not appear in this public repository. |
| `LMA_TEST_USER_SECRET_ID` | yes | The secret from step 2, e.g. `lma/integ-tests/cognito-user`. |
| `NIGHTLY_INTEG` | yes | `true`. Set on the *schedule*, so only that schedule runs the job. |
| `LMA_INTEG_STACK` | no | Defaults to `lma-integtest1`. |
| `AWS_DEFAULT_REGION` | no | Defaults to `us-west-2`. |
| `SLACK_WEBHOOK_URL` | no | The webhook from step 6. **Mask it, and do not protect it** — see step 6. Absent, the Slack post is skipped. |

### 5. The schedule

**Build > Pipeline schedules > New schedule**:

- **Interval**: a nightly cron outside working hours, e.g. `0 7 * * *` (UTC).
- **Target branch**: `develop`.
- **Variables**: `NIGHTLY_INTEG` = `true`.

The job is capped at 50 minutes against an expected ~40, holds a
`resource_group` so two runs cannot race on the same stack, and is
`interruptible: false` so a cancellation cannot skip the cleanup the mutating
tests do in a `finally`.

### 6. Optional: the Slack notification

The job builds its message with `integ-tests/junit_summary.py` and posts it with
`curl`. Everything below is about the Slack side of that.

The target is a **Workflow Builder** webhook rather than an incoming webhook,
because installing a Slack *app* needs workspace-admin approval that creating a
workflow does not. In Slack: **Tools > Workflow Builder > New workflow**, start
it **From a webhook**, and declare four variables on the trigger, all of type
**Text**:

| Variable | Carries |
|---|---|
| `status` | `✅ PASSED`, `❌ FAILED`, or `⚠️ CANCELED` |
| `commit` | the short commit SHA the run used |
| `job_url` | a link to the job, as a bare URL |
| `summary` | the stack, the counts, and any failing tests with their assertions |

Then add a **Send a message to a channel** step whose template uses those
variables, pick the channel, publish the workflow, and copy the webhook URL into
`SLACK_WEBHOOK_URL`.

Four things about this are easy to get wrong and fail quietly:

- **The four names are a contract.** A Workflow Builder trigger takes a flat
  JSON object whose keys are the declared variables and **silently ignores
  anything it does not recognise**. Renaming a key on either side posts a
  message with a blank variable rather than an error. The keys are built in
  `integ-tests/junit_summary.py`, whose module docstring says the same thing from
  the other direction.
- **Do not type Slack's `<url|label>` link syntax, backticks or asterisks into
  the message template.** Those are API mrkdwn: they render only in a body sent
  by an app, and a message composed in Workflow Builder shows them literally.
  Put `{{job_url}}` on its own line and Slack auto-links it, and use the
  editor's own formatting buttons for bold and code.
- **The CI variable must be masked and _not_ protected.** A protected variable
  is absent from pipelines on unprotected branches, so unless `develop` is a
  protected branch here the job would log "SLACK_WEBHOOK_URL not set" on every
  run and look like it was working.
- **The URL is a bearer credential**: anyone holding it can post to the channel.
  It belongs in a masked CI variable and nowhere in this repository, which is
  public.

The post only happens on a scheduled run (`$CI_PIPELINE_SOURCE == "schedule"`),
so clicking the job by hand does not interrupt a channel — the person who
clicked it is watching the pipeline. A failed post never changes the job's
verdict: the message is a report of the test result, not part of it. The payload
is built and printed to the log on every run regardless, and attached to the
pipeline as `slack-payload.json`, so "what did Slack get told" is answerable
after the fact.

### 7. Optional: a pipeline badge

**Settings > General > Badges > Add badge**, with the link
`https://<gitlab-host>/<group>/<project>/-/pipelines?ref=develop` and the image
`https://<gitlab-host>/<group>/<project>/badges/develop/pipeline.svg`. It then
appears on the project overview page.

⚠️ **The badge is source-blind.** It shows the latest `develop` pipeline of any
kind, so an ordinary push pipeline's result overwrites the nightly's — a green
badge does not mean the last nightly passed. Narrowing it to this job
(`…/badges/develop/pipeline.svg?job=nightly_integ_tests`) helps but does not fix
it, because a push pipeline that does not contain the job at all reports as
unknown. Treat the badge as a convenience and the Slack post, the email and the
pipeline-schedules page as the real signals.

## Verifying the setup

Run the schedule once by hand from the pipeline-schedules page ("Play") — which
counts as a scheduled run, so the Slack post happens too — and check four things
on the resulting pipeline:

1. `nightly_integ_tests` ran at all. If the pipeline is empty, `NIGHTLY_INTEG` is
   not set to `true` on the schedule.
2. The job's log shows the assumed-role ARN it is operating as, printed before any
   test runs. A failure here is credentials, not code.
3. The **Tests** tab lists the individual tests, including
   `test_ws_stream_transcribes_to_meeting` as **passed** rather than skipped. A
   skip is reported as a failure, so this cannot be missed — but check it is
   present, because it is the reason the schedule exists.
4. The end of the job log prints the summary and then `💬 Nightly result posted
   to Slack`, and the message arrived in the channel with every variable filled
   in. A blank variable in the message means the names in the workflow and in
   `junit_summary.py` disagree; `ℹ️ SLACK_WEBHOOK_URL not set` on a run that
   should have posted usually means the variable is **protected**.

## Running the same thing locally

```bash
# Exactly what the nightly runs, against the same stack:
AWS_PROFILE=default AWS_DEFAULT_REGION=us-west-2 \
    LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \
    make integ-tests-nightly STACK=lma-integtest1
```

To see the notification a report would produce, without posting it anywhere:

```bash
python3 integ-tests/junit_summary.py --report integ-tests-report.xml \
    --status success --stack lma-integtest1 --region us-west-2
```

The machinery this depends on — credential resolution, the `--no-skips` hook and
the notification payload — has its own unit tests, which need no AWS and run in
the fast pipeline:

```bash
make test-integ-plumbing
```

## See also

- [`integ-tests/README.md`](../integ-tests/README.md) — what each test covers
- [Developer Guide](developer-guide.md#continuous-integration) — the rest of CI
- [Security Scanning (SRT)](security-scanning.md) — the other GitLab-only job
