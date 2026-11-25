# Run Security Tests — LMA (live access tests, ZAP DAST, the security set)

Use this skill when asked to **run the security tests**, **check access control
on a stack**, **run the RBAC tests**, **run a ZAP / DAST scan**, or **produce the
security results**. The user-facing description of every test, its safety notes
and its schedule is `docs/security-testing.md`; read it before changing a test.

| Ask | Command |
|---|---|
| Live access tests on a stack | `AWS_PROFILE=default make stacktest-rbac STACK=<name> REGION=<region>` |
| ...on a stack others are testing at the same time | add `SHARED=1`: the web UI app client is not changed (the expiry check is not run) |
| ZAP scan, passive | `AWS_PROFILE=default make stacktest-zap STACK=<name> REGION=<region>` |
| ZAP scan, one target | `... make stacktest-zap STACK=<name> TARGETS=graphql` (`graphql`, `web`, `mcp`) |
| ZAP scan, active | `... make stacktest-zap STACK=<name> ZAP_ACTIVE=1`: **only on a throwaway stack the user names for it** |
| Everything, offline | `make security-results` (SRT + the static role checks) |
| Everything, with a stack | `AWS_PROFILE=default make security-results STACK=<name> REGION=<region>` |

Prerequisites: `make setup-cli-dev`, then `.venv/bin/pip install -r
integ-tests/requirements.txt`; Docker running for ZAP; `AWS_PROFILE=default`
(CLAUDE.md). Which stack: the one the user names. Never default to a stack that
looks like production; the nightly stack `lma-integtest1` is in **us-west-2**.

## Before running against a stack

- `make stacktest-rbac` creates throwaway users (`integ-rbac-*`), one meeting, a
  temporary app client and MCP API keys, and deletes them all; it shortens the
  web UI app client's ID token lifetime for one sign-in and restores it. If the
  restore check fails, the report's detail holds the settings to put back with
  `aws cognito-idp update-user-pool-client`: do that before anything else, and
  tell the user. A run killed during that sign-in leaves them in
  `scratch/api-test-results/web-client-settings-before-change.json`.
- It takes about ten minutes; run it in the background with its output in a
  file, and read the report when it ends.
- An active ZAP scan sends attack payloads and mutations as an Admin and can
  change or delete data. Run it only when the user asked for an active scan on
  a stack they named as throwaway. Never on `lma-integtest1` or a shared stack.

## Reading results

- Live tests: `scratch/api-test-results/report.md` (counts per suite, then every
  check that failed or was not run) and `report.json`. A test's assertion lists
  its failed checks. Treat a failure as real until shown otherwise: compare it
  with the role matrix (`lma-ai-stack/source/appsync/role-matrix.json`) and
  `docs/user-based-access-control.md` before deciding the test is wrong.
- ZAP: `scratch/zap-reports/latest/summary.md`, then the HTML report of a
  target. Exit `2` means a scan wrote no report: read `<target>-zap.log` in the
  run folder. Findings never fail the run (WARN-only).
- `make security-results`: `scratch/security-results/summary.md`, each step's
  log beside it.

## Where results go — this repository is public

CLAUDE.md, *Security Disclosure Hygiene*, applies to every result:

- Report to the user **counts only** (passed / failed / not run, alerts by risk)
  and where the local report is. Write what a failed check or a ZAP alert means
  only where the user keeps private notes outside the repository (ask if you
  do not know where), never into a commit, PR, issue, CHANGELOG, code comment or
  `docs/`.
- Do not attach a report from `scratch/` to anything; it names the stack and
  what it answered. In CI (`CI` set) a failed test's message carries counts
  only and the report it writes withholds what failed checks observed, so the
  nightly log, JUnit report, triage and Slack post carry no details.
- A fix is written as the control now in place, in neutral words. A
  `zap-rules.conf` reason states why the rule does not apply to the code as it
  stands.

## Changing the tests

- Add a check by recording it: `REPORT.record(suite, subject, caller, expected,
  observed, detail)` in `integ-tests/test_security_access.py`; every test takes
  the `recorded` fixture, which fails it on its own failed checks. Name the test
  for the invariant it holds.
- A mutation a caller is allowed to make goes only to the throwaway meeting;
  every other mutation names an id that does not exist. Anything the run creates
  is deleted in a `finally`, and any stack setting it changes is restored there
  and the restore checked.
- A new GraphQL operation needs nothing here: `test_role_access.py` and the
  no-credential sweep read the role matrix and the schema. A new mutation that
  starts paid work goes into `NEVER_SCANNED` in `scripts/security/zap_scan.py`
  (a test fails if a name there leaves the schema).
- A new entry point (an API, a function URL, a CloudFront path) gets a check in
  the *Other entry points* suite, a row in `docs/security-testing.md`, and an
  entry in the role matrix's `surfaces`.
- The pure helpers are unit-tested offline: `integ-tests/test_security_helpers.py`
  and `scripts/security/tests/` (`make test-integ-plumbing`). Run those files
  locally after a change; CI runs them on the pull request, and the live module
  runs in the nightly integration run.
