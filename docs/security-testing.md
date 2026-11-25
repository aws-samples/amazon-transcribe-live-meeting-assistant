---
title: "Security Testing"
---

<!--
Copyright (c) 2025 Amazon.com
This file is licensed under the MIT-0 License.
See the LICENSE file in the project root for full license information.
-->

# Security Testing

LMA has four kinds of security test. Two read the repository and need no AWS
account; two call a deployed stack.

| Test | What it reads or calls | How to run it | When it runs |
|---|---|---|---|
| **SRT** (static analysis, dependencies, infrastructure as code) | the repository | `make srt-scan` | GitLab merge requests; see [Security Scanning (SRT)](security-scanning.md) |
| **Static role checks** | the role matrix, `schema.graphql`, the Cognito template, the docs | `make test-ai-stack`, `make test-integ-plumbing` | every pull request that touches them |
| **Live access tests** | a deployed stack, as throwaway users | `make stacktest-rbac STACK=<name>` | every night, in the [scheduled integration run](scheduled-integration-tests.md) |
| **ZAP DAST** (OWASP ZAP, dynamic scan) | a deployed stack's GraphQL API, web UI and MCP endpoint | `make stacktest-zap STACK=<name>` | weekly, or by hand, in GitLab |

`make security-results` runs them as a set and writes one summary; see
[Running everything](#running-everything). Each release's counts are published
in `security/test-results/<version>/`; see [Release snapshots](#release-snapshots).

The role matrix, `lma-ai-stack/source/appsync/role-matrix.json`, is the single
source of truth for who may call each GraphQL operation and use each other entry
point. [User-Based Access Control](user-based-access-control.md) describes the
roles and per-meeting rules the tests hold the stack to.

## Contents

- [Live access tests](#live-access-tests)
- [ZAP DAST](#zap-dast)
- [Running everything](#running-everything)
- [Release snapshots](#release-snapshots)
- [Schedule](#schedule)
- [What these tests do not exercise](#what-these-tests-do-not-exercise)
- [Reporting a result](#reporting-a-result)

## Live access tests

```bash
make setup-cli-dev
.venv/bin/pip install -r integ-tests/requirements.txt
AWS_PROFILE=default make stacktest-rbac STACK=<stack name> REGION=<region>
```

This runs two modules of `integ-tests/` against the stack:
`test_role_access.py` and `test_security_access.py`. It takes about ten
minutes, five of which are the wait for a token to expire (overlapped with the
other tests). A skipped test fails the run (`--no-skips`), except a test of a
feature the stack does not have (the MCP server when `EnableMCP` is `false`, the
Virtual Participant viewer path under the MicroVM launch type).

### What is checked

| Suite (in the report) | Module | What must hold |
|---|---|---|
| AppSync role matrix | `test_role_access.py` | Every query and mutation is refused at the field to each role the matrix does not list, and every query is answered to each role it lists. The same module also checks, as plain test assertions rather than report lines, that each token names one role (`cognito:groups`, `lma_role`) and that the transcriber, the identity pool and the MCP API key endpoint admit each role as the matrix says. |
| AppSync authentication | `test_security_access.py` | Every query and mutation is refused with no `Authorization` header. A User's ID token is refused when its claims were changed (a role group added, the expiry moved), its signature altered, its header set to `alg: none`, or its claims signed by another key, with this pool's issuer or another one's. A genuine ID token issued to another app client of the user pool is refused. A genuine ID token is answered while fresh and refused after it expires. |
| AppSync backend-only operations | `test_security_access.py` | Every operation only backend roles may call (the matrix's `backendOnly`), and every query that also accepts backend roles (`alsoIam`), is refused to a request signed with SigV4 by the web UI's identity-pool credentials, for an Admin, a User and a Participant. |
| Meeting access | `test_security_access.py` | One meeting, owned by one User and shared with a second, is read (`getCall`, the transcript, the transcript with sentiment, the key frames with their signed links, the meeting list) by its owner, the shared user and an Admin, and by no other User. The operations that name the meeting (chat attachments, consent, cost, whiteboards, notifications, the archive) answer its owner and refuse the other User. Labels are changed by the owner and the shared user and not by the other User; sharing and deleting are done by the owner only; a deleted meeting is no longer returned. The web UI's identity-pool credentials read the audio and video recording prefixes of the recordings bucket and no other prefix, and a Participant's read none. |
| Other entry points | `test_security_access.py` | The WebSocket transcriber admits a genuine access token only (no parameter, a non-bearer value, an ID token, an altered or self-signed token: `401`), and refuses a `START` naming a meeting the caller does not own. The MCP API key endpoint refuses no key, a key never issued and a revoked key, and answers a meeting's transcript to its owner and shared user only. The MCP OAuth gateway refuses no token, a non-token, web UI ID and access tokens, and a self-signed token. The Virtual Participant viewer path (`/vnc/*`) answers `401` with no token and `403` with one it did not sign. The identity pool gives no credentials without a login; the user pool refuses a self sign-up from outside the allowed domains. Every REST API method of the stack names an authorizer, and the stack declares no Lambda function URL. |

The role matrix and the schema are also held to each other offline, by
`lma-ai-stack/test/test_role_matrix.py` and `test_appsync_contract.py`, and the
helpers these modules use (token tampering, the report, the probe's pure
functions) by `integ-tests/test_security_helpers.py`.

### What the run creates and changes

The tests are written to leave the stack as they found it, also when a test
fails:

- **Users.** An Admin, three Users and a Participant for
  `test_security_access.py`, and an Admin, a User and a Participant for
  `test_role_access.py`, named `integ-rbac-<label>-<time>@<domain>` with a
  random password, created with no invitation email and deleted at the end. The
  domain is `example.invalid`, a reserved name no message can reach, unless the
  stack restricts sign-up to its `AllowedSignUpEmailDomain`, which refuses other
  domains even to an administrator; then it is the first allowed domain. Users
  and app clients with the run's prefixes that are more than two hours old,
  left by an interrupted run, are deleted when the next run starts.
- **One meeting.** Created through the meeting pipeline (a `START` on the call
  data stream) with one transcript line and one key frame, shared, changed and
  finally deleted by its owner; whatever remains is removed by its CallId. One
  small object is written under four prefixes of the recordings bucket and
  deleted.
- **App clients.** A temporary app client of the user pool is created and
  deleted. The web UI's app client has its ID token lifetime shortened to five
  minutes for one sign-in, a few seconds, and every setting is put back at once;
  the restore is itself a check, and if it fails the report gives the settings
  to restore by hand. Users signing in during those seconds get a five-minute
  ID token, which the web UI renews as usual. On a stack other runs are using
  at the same time, pass `SHARED=1` (`LMA_SECTEST_NO_CLIENT_CHANGE=1`): the
  client is left alone, and the expiry check and its control are reported as
  not run.
- **MCP API keys.** Generated by the throwaway users and revoked.
- **Mutations.** A mutation a caller is allowed to make is only ever made on the
  throwaway meeting. Every other mutation names an id that does not exist, so a
  caller that is admitted fails validation instead of changing data. The
  mutations that start work for a caller they admit (a Virtual Participant, a
  model call) are never called as such a caller.

Run it against a development or test stack. It does not need, and should not be
given, a production stack.

### Reading the report

Every check is recorded, and at the end of the run two files are written to
`scratch/api-test-results/` (ignored by git; `LMA_SECURITY_REPORT_DIR` moves
them):

- `report.md`: the counts per suite, then every check that failed or was not
  run, with what was expected, what was observed and the HTTP status and error
  the stack answered.
- `report.json`: every check, with `suite`, `subject` (the operation or entry
  point and how it was called), `caller`, `expected`, `observed`, `outcome`
  (`pass`, `fail` or `not-run`) and `detail`.

A test fails on any check it recorded that did not pass, and its assertion
message lists those checks. In CI (`CI` set, as in the scheduled run) the message
gives only the number of failed checks per suite, because the job log, the JUnit
report and the Slack summary are kept and shared, and the report written there
keeps only the suite and outcome of each check that did not pass; rerun
`make stacktest-rbac` locally to read the details. A `not-run` is not a pass: it means the condition
the check needs was absent on the stack.

Treat a failure as real until it is shown to be a test problem. The report holds
the stack's identifiers and what it answered, so keep it local: do not attach it
to an issue or a pull request (see [Reporting a result](#reporting-a-result)).

## ZAP DAST

```bash
make setup-cli-dev
.venv/bin/pip install -r integ-tests/requirements.txt
AWS_PROFILE=default make stacktest-zap STACK=<stack name> REGION=<region>
```

Needs Docker (the official ZAP image, pinned to a release in
`scripts/security/zap_scan.py`; set `ZAP_DOCKER_IMAGE` to use another). `scripts/security/zap_scan.py` runs three
ZAP scans, about ten to thirty minutes in all:

| Target | ZAP script | Seeded with | Signed in as |
|---|---|---|---|
| `graphql`, the AppSync GraphQL API | `zap-api-scan.py -f graphql` | a plain SDL made from `schema.graphql` | a throwaway Admin's ID token |
| `web`, the CloudFront web UI | `zap-baseline.py` | the spider | nobody (the static site) |
| `mcp`, the MCP server's API key endpoint | `zap-api-scan.py -f openapi` | a one-operation OpenAPI document (a JSON-RPC `tools/list`) | a read key the Admin generates and revokes |

`TARGETS=graphql,web` scans a subset. The `mcp` target is reported as not run on
a stack with `EnableMCP=false`.

**The GraphQL seed.** ZAP's GraphQL parser does not know AppSync's directives
and scalars, so `zap_sdl()` drops every `@aws_*` directive and its definition,
declares the `AWS*` scalars, removes the `Subscription` type (ZAP sends HTTP
requests only) and, in a passive scan, the `Mutation` type. ZAP then generates
one request per root field. The queries that call a model (the knowledge base
query and the meeting invitation parser) are never sent, and neither, even in
an active scan, are the mutations that start paid work or manage users
(`NEVER_SCANNED` in the script). The token is given to ZAP's replacer in a
config file in the run's folder, never on the command line, and that file is
deleted when the scans end.

**Passive by default.** ZAP's safe mode (`-S`) skips the active scan: ZAP sends
the seeded requests and the spider's, and applies its passive rules to what
comes back (security headers, Content-Security-Policy, cookies, caching,
information in responses). `ZAP_ACTIVE=1` runs the active scan and includes the
mutations: ZAP then sends attack payloads to every operation **as an Admin**, so
it can change and delete that stack's data. Use it on a throwaway stack only,
never on one somebody else uses.

**WARN-only.** A finding does not fail the run. `make stacktest-zap` exits `0`
when every scan wrote its report, `2` when one wrote none (the image could not
be pulled, ZAP did not start; its log is `<target>-zap.log`) and `1` when the
stack could not be read.

### Reading the reports

Each run writes to `scratch/zap-reports/<time>/` (ignored by git;
`LMA_ZAP_REPORT_DIR` moves it), and `scratch/zap-reports/latest` points to the
newest:

- `summary.md` and `summary.json`: per target, the alert counts by risk (High,
  Medium, Low, Informational), how many rules passed, and each alert's name and
  plugin id; with `--counts-only` (the CI job) the counts alone.
- `<target>-report.html`, `.json` and `.md`: ZAP's own reports, with the
  requests and responses of each alert.

### Triage: `zap-rules.conf`

`scripts/security/zap-rules.conf` sets each rule to `IGNORE`, `WARN` or `FAIL`
(tab-separated: plugin id, action, comment). An `IGNORE`d rule is left out of the
counts. To triage a run, read `summary.md` and the HTML report and, for each
recurring alert, change the code, mark the rule `IGNORE` with the reason it does
not apply to LMA's code, or leave it `WARN`. Write the reason as a statement
about the code as it stands. Once a rule's results are clean it can be set to
`FAIL`, and the run made to gate on it; today nothing gates.

`IGNORE` applies to every target. Where a rule does not apply to one host only,
use an `OUTOFSCOPE` line instead: plugin id, `OUTOFSCOPE`, and a URL regex
(matched from the start of the URL) in place of the comment, with the reason on
a `#` line above it. ZAP leaves that rule's alerts on matching URLs out of its
own tally, and `zap_scan.py` leaves them out of the counts; the rule still
reports on the other targets. The AppSync GraphQL host is out of scope for the
`Strict-Transport-Security` rule (10035), because AppSync is an AWS-managed
endpoint that sets its own response headers; the web UI and the MCP endpoint are
still checked for it.

## Running everything

```bash
AWS_PROFILE=default make security-results                                  # offline
AWS_PROFILE=default make security-results STACK=<stack name> REGION=<region>  # and live
```

Offline it runs SRT (`make srt-setup srt-scan`) and the static role checks.
With `STACK` it also runs `make stacktest-rbac` and a passive
`make stacktest-zap`. Every step runs even when an earlier one fails. The
summary, with each step's outcome, time and counts, is written to
`scratch/security-results/summary.md` and `summary.json`, and each step's output
to `<step>.log` beside them.

## Release snapshots

Each release commits a published record of its security test results, in
[`security/test-results/<version>/`](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/tree/develop/security/test-results):
`summary.md` to read and `summary.json` with the same content for tools. A
snapshot holds **counts only**:

| Section | What it gives |
|---|---|
| Provenance | the release version (`VERSION`), the date the snapshot was written, the tested stack's version (from its template description), and for each test the date it was measured and the commit its tests ran from, or `not recorded` |
| SRT | findings by priority (Critical, High, Medium, Low, Info) and status (Open, Reopened, Suppressed, Resolved); how many suppressed findings have a recorded decision in `.srt/suppressions.json`, and how many have none |
| Static role checks | tests passed, failed, errored and skipped |
| Live access tests | checks passed, failed and not run in each suite of [What is checked](#what-is-checked), and the reason given for each check not run |
| ZAP DAST | the scan mode, and per target (`graphql`, `web`, `mcp`) ZAP's rule outcomes (PASS, WARN, FAIL, IGNORE, INFO) and the alert counts by risk after the triage in `zap-rules.conf` |
| Not run in this snapshot | every test that did not run, with the reason, and the active ZAP scan when the scan was passive |
| What these tests do not exercise | the scope limits in [What these tests do not exercise](#what-these-tests-do-not-exercise) |

It never names an alert, a finding, an operation that was called or what it
answered, a URL, a host, an account, a Region or a stack. The detail stays in the
local reports, as [Reporting a result](#reporting-a-result) requires.

**Reading a snapshot.** A test that did not run is listed as not run rather than
left out, and a check not run is not a pass. Compare the measured dates: a
snapshot can be curated from runs made on different days, and each test shows its
own. A ZAP alert count is not a failure (the scan is WARN-only); a non-zero
`FAIL` rule count or a failed access check is. SRT's *Open* column is what
remained unreviewed when the scan ran; *Suppressed* findings each carry a reviewed
decision. The stack's version says which release the live tests and the ZAP scan
ran against; a development stack shows a development version such as
`0.4.1.dev1`.

**Producing one.** For a release, on the release commit and against a
non-production stack built from it:

```bash
AWS_PROFILE=default make security-results STACK=<stack name> REGION=<region> SNAPSHOT=1
```

This runs the whole set and writes the snapshot of that run's reports;
`make security-snapshot` curates reports already on disk instead.
`scripts/security/curate_snapshot.py` reads only the fields above from the raw
reports and refuses to write anything (exit 1) when its output matches the shape
of a URL, host name, ARN, account id, pool id, email address or local path, uses
ZAP alert wording, or contains any string from the raw reports, such as an alert
name, a finding's text or the stack name. It also refuses a snapshot that records
a failed check or test or a ZAP `FAIL` rule until `ALLOW_FAILURES=1` is given,
which is for after the failure has been reported and handled as
[Reporting a result](#reporting-a-result) describes. An existing version's
snapshot is not overwritten unless `REPLACE=1` is given. Read the snapshot before committing it;
the steps are in the
[`curate-security-results`](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/blob/develop/.claude/skills/curate-security-results.md)
skill.

## Schedule

| What | Where | When |
|---|---|---|
| Static role checks, and the unit tests of the live tests' helpers and of the ZAP tooling | the **Lint and unit tests** check (`.github/workflows/code-checks.yml`, `code_checks` in `.gitlab-ci.yml`) | every pull request that touches them; every push to `develop` and `main` |
| SRT | `srt_security_review` in `.gitlab-ci.yml` | GitLab merge requests |
| Live access tests | `nightly_integ_tests` (`make integ-tests-nightly`), which runs every module of `integ-tests/` | every night, against `lma-integtest1` after the nightly deploy |
| ZAP DAST, passive | `zap_dast` in `.gitlab-ci.yml` | a weekly pipeline schedule, or by hand |

The live tests are not run on pull requests: they need AWS credentials and a
deployed stack, which pull request CI does not have, and the nightly run tests
`develop` after every merge. ZAP is not run on pull requests or nightly either,
for the reasons the IDP project gives for its scan: it takes up to half an hour,
and what it reports is about the deployed stack rather than about a change, so
once a week is enough to see a regression in headers or responses, and a scan
can be run by hand after a change to them.

To set up the ZAP schedule in GitLab, add a pipeline schedule on `develop` with
the variable `ZAP_DAST` = `true`, weekly, and without `NIGHTLY_INTEG`, so the
nightly deploy does not run with it. It uses the same `AWS_CREDS_TARGET_ROLE`
as the nightly jobs ([Scheduled Integration Tests](scheduled-integration-tests.md)).
To run it by hand (on `develop` only), start a pipeline on `develop` from **Build › Pipelines › Run
pipeline** and play the `zap_dast` job. It runs the ZAP image as the job image
(these runners have no Docker daemon), scans passively, prints counts only, and
keeps only the counts (`summary.md` and `summary.json`, written with
`--counts-only`) as the job's artifacts for a month: job logs and artifacts are
treated as public, so ZAP's own reports stay on the runner. Run
`make stacktest-zap` locally to read the alerts. It never runs an active
scan: `lma-integtest1` is shared with the nightly run.

## What these tests do not exercise

So that a green run is not read as more than it is:

- **Subscriptions** are held to the role matrix by the schema contract test
  only; no live test opens one.
- **The ASR MicroVM sessions** the transcriber opens, the **Virtual Participant
  launch** state machine, and the **MicroVM viewer tokens** are not called
  directly; they are reached only through the operations above.
- **The browser extension and the Desktop Capture App** are clients of the
  entry points tested here; their own code is covered by their unit tests.
- **Token revocation after sign-out**, **TLS protocol versions** and **rate
  limits** are not tested.
- **ZAP sends HTTP requests only.** It scans the GraphQL API, the web UI and the
  MCP endpoint, not the WebSocket transcriber, which the live access tests call
  instead.
- **Recording links per meeting.** The web UI signs recording links itself,
  with credentials scoped to the recordings prefixes, as
  [Recording Playback](user-based-access-control.md#recording-playback)
  describes; the tests hold that scope, not a per-meeting decision.

## Reporting a result

This repository is public. If a run shows that a control does not behave as
documented, do not describe it in an issue, a pull request, a commit message or
the CHANGELOG. Report it through the channel in
[CONTRIBUTING.md](../CONTRIBUTING.md#security-issue-notifications), and keep the
reports, which name the stack and what it answered, local.
