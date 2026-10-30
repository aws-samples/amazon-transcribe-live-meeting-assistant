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

Three jobs, in order, every night:

| Job | Does | Posts to Slack |
|---|---|---|
| `nightly_publish` | Builds the scheduled commit and uploads its artifacts | only if it fails |
| `nightly_deploy` | Deploys those artifacts to `lma-integtest1` | only if it fails |
| `nightly_integ_tests` | `make integ-tests-nightly` against that stack | always |

So the stack is rebuilt from the scheduled ref before the suite runs, and a
green nightly means **this branch** works — not that some older build still
does. Exactly one Slack message arrives per run, from whichever stage decided
the outcome.

### Why three jobs and not one

Two reasons, and the second one is a hard constraint rather than a preference.

**The run has to fit inside its AWS credentials.** The runner's credential
vendor is a one-shot `pre_clone` step: it assumes the role once, writes the
default profile, and never refreshes. The session is role-chained, so STS caps
it at **one hour** whatever the role's `MaxSessionDuration` says. One job doing
all three stages would run past that and die on an expired token wherever the
next AWS call happened to fall — a failure that points at the call rather than
at the cause. Each job re-enters the pipeline and gets its own fresh hour, and
each carries a `timeout` **below** that hour, so credentials cannot expire
mid-job: an overrun is killed by its timeout and says so. Anything added to
these jobs has to preserve that. Don't merge them.

**A build failure and a test failure are different news.** Keeping them in
separate jobs is what lets the notification say which stage failed and what it
means for the stack — a failed publish leaves the stack untouched, while a
failed deploy can leave it rolled back, or with a rollback that could not
finish. A stack in that second state (`UPDATE_ROLLBACK_FAILED`) refuses every
update, so `nightly_deploy` first resumes such a rollback, once and skipping no
resource, before deploying (`integ-tests/resume_rollback.py`); if the resumed
rollback fails as well, the deploy job fails there and says why. See `--stage`
in `integ-tests/junit_summary.py`.

### The suite itself

`make integ-tests-nightly` is `make integ-tests` plus two things that matter
when nobody is watching the run:

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

### The build, without a Docker daemon

`nightly_publish` sets **`SAM_BUILD_IN_CONTAINER=false`**, which builds the
Lambda bundles with the runner's own interpreter instead of inside SAM's build
container. These shared runners have no Docker daemon and cannot start one —
`services:` containers don't come up and `/var/run/docker.sock` doesn't exist —
so `--use-container` fails before it builds anything.

That substitution is only sound because the runner **is** the Lambda platform:
linux/amd64 on python3.12, so the wheels it resolves are the wheels Lambda
wants. The flag is explicit rather than inferred from `uname` for exactly that
reason — on a developer's machine, or on a runner with a different Python, it
would produce bundles Lambda cannot import, and the failure would surface at
runtime as an import error rather than at build time. **Leave the default
(`true`) alone locally**, where Docker is a prerequisite and
`lma publish` checks for it.

## How a failure reaches you

Four independent paths, so a red nightly cannot go unnoticed:

1. **Slack.** Each job's `after_script` can post to a channel, and exactly one
   of them does per run: `nightly_publish` and `nightly_deploy` post only when
   they fail, and `nightly_integ_tests` posts pass or fail. The message carries
   the stack name, the counts, the template that was deployed, **every check
   that ran and how it ended**, the assertion messages of any failures, and a
   link to the job — or, when an earlier stage failed, which stage it was,
   why, and what that leaves the stack in. For a failed publish the reason is
   the decisive lines of the job's output; for a failed deploy it is read from
   the stack's events, through every nested stack to the resources that failed
   on their own account, and for a failed in-stack CodeBuild image build, the
   lines of that build's log just before it gave up
   (`integ-tests/failure_cause.py`). Needs the `SLACK_WEBHOOK_URL` variable from
   [step 4](#4-cicd-variables); absent, the job logs one line and skips the
   post. Setup is in [step 6](#6-optional-the-slack-notification).

   The per-check list is grouped by test module, and a module is listed in full
   only when it has few enough distinct checks to read — the live suite always
   is; the pipeline's own plumbing tests are numerous and uniform, so they
   collapse to a tally. Parametrised checks collapse to one line with a count.
   The point of the list is to make a *missing* check visible: counts alone
   cannot show that the test you care about never ran.
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

### Triage of a failed run

A test's assertion often names a symptom rather than a cause.
`test_ws_stream_transcribes_to_meeting` reports "no transcript segments were
produced within 90s" whether the WebSocket server rejected the upgrade,
Transcribe refused the stream, the Kinesis consumer errored, the Call Event
Processor threw, or the Cognito test user's password has lapsed — and the
evidence that tells those apart is in several CloudWatch log groups, not in the
pytest output.

So when the suite fails, `integ-tests/failure_agent.py` runs Claude Code
headless against Bedrock, hands it the JUnit report and the run's full output
**on disk**, and lets it grep those, read the repository, and query the live
stack. Its report is appended to the Slack message under a `🤖 Triage:`
heading, and kept as the `triage-report.txt` artifact. On disk rather than in
the prompt because a nightly's output is tens of thousands of lines, most of
them pytest progress: a file it greps costs almost nothing, where the same text
as prompt tokens would not.

Enable it with **`LMA_FAILURE_AGENT=1`**. It is off by default so that a slow
or flaky agent cannot destabilise the nightly.

⚠️ **It needs its own read-only role**, named in
`LMA_FAILURE_AGENT_ROLE_ARN`, and **refuses to run without one** rather than
falling back to the job's own credentials — which are administrative in the
test account, so an agent inheriting them could delete the stack it was asked
to diagnose. The trust policy needs `sts:TagSession` as well as
`sts:AssumeRole`: the credential vendor's session carries transitive tags, so a
chained assume that cannot pass them on is denied. Three further points, in
decreasing order of how much they protect you:

- **IAM is the boundary.** `ReadOnlyAccess` plus `bedrock:InvokeModel` on the
  Claude models is all the role needs.
- **The harness allowlist is a speed bump, not a boundary.** `--allowedTools`
  lists read-only verbs, but Bash allowlist rules are prefix globs, so
  `Bash(aws logs *)` does not stop a chained
  `aws logs … ; aws cloudformation delete-stack …`. Treat it as noise reduction.
- **The agent never decides pass/fail.** Python owns the verdict; the agent is
  advisory, and returns nothing rather than guessing. A notification that
  called a run green because an agent said so would be worse than no agent.

It is capped three ways — turns, dollars, and a wall clock — all inside the
job's timeout and so inside the one-hour credential ceiling. Observed: about
1–3 minutes and $0.20–$0.50 per failure.

### Why the notification is posted by the job

GitLab's own Slack integration, and its pipeline **badge**, both key off
`pipeline_events` for a branch and cannot filter by pipeline **source**. Either
one would therefore report every push pipeline on `develop` as well, which is
the majority of them and is not what anybody subscribed for. The job's
`after_script` is the only place that knows the run was the scheduled one, and
the only place that has the JUnit report to summarise.

## One-time setup

### 1. A long-lived target stack

The convention is `lma-integtest1`, in **`us-west-2`** — note that this is not
the region a default profile resolves to, and a wrong region here reports the
stack as non-existent rather than as misconfigured.

It lives in an account of its own, used for nothing else, so that a run that
leaves something behind cannot disturb anybody's work and the test user below
is the only human-shaped identity in it. Deploy it from the published template
once, which needs no local build:

```bash
lma deploy --stack-name lma-integtest1 --region us-west-2 \
    --admin-email you@example.com --wait
```

Thereafter refresh the deployed code when you want to — deliberately, and **not**
what the nightly does:

```bash
AWS_PROFILE=<the test account> make integ-deploy-and-test STACK=lma-integtest1
```

⚠️ **Check the account's VPC Block Public Access setting before concluding the
stack is broken.** A newly created account may have it set to `block-ingress`,
which drops inbound internet-gateway traffic for the whole account **without
changing security groups or NACLs** — so every per-resource check passes while
CloudFront cannot reach the transcriber's load balancer, and
`test_ws_stream_transcribes_to_meeting` fails on a WebSocket upgrade returning
HTTP 502. The symptom to recognise is a load balancer whose target is healthy
and whose `RequestCount` is nevertheless zero.

```bash
aws ec2 describe-vpc-block-public-access-options --region us-west-2
```

Rather than turning it off account-wide, exclude the stack's VPC and leave the
account-wide setting as it is, so a VPC added later is still blocked by default:

```bash
aws ec2 create-vpc-block-public-access-exclusion --region us-west-2 \
    --vpc-id <the stack's VPC> --internet-gateway-exclusion-mode allow-bidirectional
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

The job gets short-lived, job-scoped credentials from the GitLab instance's
**AWS Credential Vendor**: name a role in `AWS_CREDS_TARGET_ROLE` and the runner
assumes it and writes the result to the default profile in the credential search
order. No long-lived access key is stored anywhere, and the job's own
`before_script` does nothing but check the variable is set and print the
resulting identity. See
[Vending AWS credentials to CI/CD jobs](https://docs.hub.amazon.dev/docs/gitlab/user-guide/howto/vending-aws-credentials/).

**The role deploys the stack, so it needs the permissions to do that.** The
nightly publishes artifacts to S3 and then updates a stack of a dozen nested
stacks that create IAM roles, Lambda functions, ECS services, CloudFront
distributions, Cognito pools, Kinesis streams and DynamoDB tables. Enumerating
that as a least-privilege policy is not practical to keep correct: every miss
surfaces as a nightly that fails partway through a deploy, which is precisely
the unreliable signal this schedule exists to replace.

So the containment here is **the account, not the role**. Give the test account
nothing but this stack, let nobody else deploy into it, and grant the role
broad permissions **within it** — `AdministratorAccess` is a reasonable choice
for a dedicated test account and a bad one anywhere else. The trust policy
below is then what limits who can use it: one GitLab group and one project.

If you would rather scope the role down, the suite itself — as opposed to the
deploy — needs only read access to the stack and its resources (CloudFormation,
ELBv2, ECS, Kinesis), `appsync:GraphQL` on the stack's API, read/write on the
EventSourcing DynamoDB table (it creates rows and deletes them),
`states:StartSyncExecution` on the Virtual Participant scheduler, use of the
stack's KMS key through those services, and `secretsmanager:GetSecretValue` on
the one secret above. The Cognito side needs no IAM at all: the test user
authenticates over the public SRP API with the password from the secret.

The trust policy is a template with two values to fill in — your GitLab group
and project, as they appear in the project URL. Do **not** change the principal:
`arn:aws:iam::979517299116:role/gitlab-runners-prod` is the runner role the
credential vendor jumps through, and it is the same for every project.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "AWS": "arn:aws:iam::979517299116:role/gitlab-runners-prod"
    },
    "Action": ["sts:AssumeRole", "sts:TagSession"],
    "Condition": {
      "StringEquals": {
        "aws:PrincipalTag/GitLab:Group": "your-group",
        "aws:PrincipalTag/GitLab:Project": "YOUR-PROJECT"
      }
    }
  }]
}
```

`sts:TagSession` is required, not optional — the vendor tags the session with
the group and project, which is what these conditions read. Always condition on
**both**: a project-only condition would let a same-named project in another
group assume the role. The values are case-sensitive and must match the project
URL's path segments.

> **Why not OIDC.** An `id_tokens:` block and an IAM OIDC provider are the
> obvious shape for this and do not work on `gitlab.aws.dev`: its OpenID
> discovery and JWKS endpoints sit behind the same authenticating edge as the
> rest of the instance, so IAM cannot fetch the keys to validate a job's token.
> The credential vendor is the supported path on this instance.

### 4. CI/CD variables

Under **Settings > CI/CD > Variables**, or on the schedule itself:

| Variable | Required | Notes |
|---|---|---|
| `AWS_CREDS_TARGET_ROLE` | yes | The role from step 3. **Mask it** — it contains the account id, which must not appear in this public repository. Read by the runner's credential vendor, not by anything in this repository, so the name is fixed. |
| `LMA_TEST_USER_SECRET_ID` | yes | The secret from step 2, e.g. `lma/integ-tests/cognito-user`. |
| `NIGHTLY_INTEG` | yes | `true`. Set on the *schedule*, so only that schedule runs the job. |
| `LMA_INTEG_STACK` | no | Defaults to `lma-integtest1`. |
| `AWS_DEFAULT_REGION` | no | Defaults to `us-west-2`. |
| `SLACK_WEBHOOK_URL` | no | The webhook from step 6. **Mask it, and do not protect it** — see step 6. Absent, the Slack post is skipped. |
| `LMA_FAILURE_AGENT` | no | `1` enables the failure-triage agent. Off by default. |
| `LMA_FAILURE_AGENT_ROLE_ARN` | if the agent is on | The **read-only** role it runs as. **Mask it.** Without it the agent declines to run; see [Triage of a failed run](#triage-of-a-failed-run). |

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
