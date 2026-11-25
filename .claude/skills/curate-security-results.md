# Curate Security Results — LMA (the per-release, public-safe snapshot)

Use this skill when asked to **capture the security results for a release**,
**update the security snapshot**, **publish the security test results**, or
**curate the SRT / access test / ZAP output**. It produces
`security/test-results/<version>/summary.md` and `summary.json`, which are
committed to this **public** repository. Running the tests themselves is
`.claude/skills/run-security-tests.md`; what each test checks is
`docs/security-testing.md`.

## What the snapshot may hold

Counts only, by construction (`scripts/security/curate_snapshot.py`):

- SRT findings by priority and status, and how many are suppressed with a
  recorded decision;
- the static role checks' passed / failed / errors / skipped;
- the live access tests' passed / failed / not run per suite, and the reason
  each not-run check gives;
- ZAP's rule tally (PASS, WARN, FAIL, IGNORE, INFO) and alert counts by risk,
  per target, and the scan mode;
- the release version, the date, the stack's version, the commit each part's
  tests ran from (or `not recorded`), what was not run, and what the tests do not
  exercise.

Never an alert name, a finding's text, a check's subject or what it observed, a
URL, a hostname, an ARN, an account id, a Region or a stack name. The curator
refuses (exit 1, nothing written) when its output matches one of those shapes or
contains a string taken from the raw reports. Do not work around a refusal by
editing the output: find which input carried the string and fix the curator or
the report that produced it.

## For a release: one command

On the release candidate commit, against a stack deployed from that commit or
from the release's published template (never production; the user names it):

```bash
AWS_PROFILE=default make security-results STACK=<name> REGION=<region> SNAPSHOT=1
```

This runs SRT, the static role checks, `make stacktest-rbac` and a passive
`make stacktest-zap`, then writes the snapshot of **this run's** reports only: a
step that did not run, or wrote no report, is recorded as not run, never read
from an earlier run. The version is `VERSION`; the stack's version comes from its
template description; each part's commit is `git rev-parse HEAD`.

- SRT's first setup on a host asks for an AWS profile interactively: run
  `make srt-setup` once in a terminal and pick the profile. Never set
  `GITLAB_CI` or `GITHUB_ACTIONS` on a developer host or a shared box.
  `scripts/srt/setup.py` writes SRT's placeholder AWS credentials only on a CI
  runner with no other credential source, and removes them when setup ends.
- On a stack others are testing, add `SHARED=1` (make passes it on to
  `stacktest-rbac`): the web UI app client is left alone, and the token expiry
  check and its control are recorded as *not run*, with that reason.
- A version folder that exists is not overwritten; `REPLACE=1` overwrites it.
  Do not replace the snapshot of a version that has shipped.

## Curating reports already on disk

```bash
make security-snapshot CURATE_ARGS="--stack-version <v> --commit live-rbac=<sha> --commit zap=<sha>"
```

It reads `.srt/issues.json`, `scratch/security-results/static-roles.xml`,
`scratch/api-test-results/report.json`, `scratch/security-results/live-rbac.xml`
and `scratch/zap-reports/latest/summary.json` (each overridable: `--srt-issues`,
`--static-junit`, `--access-report`, `--access-junit`, `--zap-summary`), and
`--skip <part>` records a part as not run. The live tests' JUnit file is used
only when it was written within an hour of `report.json`; otherwise the skipped
tests read *not recorded*. Each part
carries its own *measured* date, so parts from different days are visible as
such. Pass a part's commit only when you know which commit its tests ran from;
otherwise it reads `not recorded`.

## Before committing the snapshot

1. **Read `summary.md` and `summary.json` line by line** against CLAUDE.md's
   *Security Disclosure Hygiene*. The guard catches identifier shapes; it cannot
   judge prose. Look in particular at each *not run* reason (it is the one free
   text that comes from the tests) and at every count that is not zero.
2. **Check the provenance.** The version, the stack's version, each part's
   measured date and commit must be what you ran. A part from an older run, or
   from a stack built from another commit, is either labelled as such or left
   out with `--skip`.
3. **A failure is handled before it is published.** The curator refuses a
   snapshot with a failed static or live check or test, or a ZAP `FAIL` rule,
   until `ALLOW_FAILURES=1` (`--allow-failures`) is given. Report the failure
   privately first (CONTRIBUTING.md, *Security issue notifications*), and pass
   the flag only once it is fixed or assessed. Counts carry no detail, but a
   non-zero failure count next to a release is itself a statement. SRT findings
   still open at High or Critical are not refused, but ask the user whether to
   triage them before the snapshot is committed.
4. **Stage by path**: `git add security/test-results/<version>/`. Never add
   anything from `scratch/` or `.srt/issues.json`.
5. Commit with a neutral subject, e.g. `chore(security): add the <version>
   security test snapshot`, and say in the PR which parts ran and which did not.

The folder is classified for CI as the security tooling's area
(`scripts/ci/changed_areas.py`), so a snapshot-only PR runs
`scripts/security/tests`, whose `test_every_committed_snapshot_is_public_safe_and_counts_only`
holds every committed snapshot to the guard, and gets a full AI review.

## Changing the curator

- Publish a new field only if it is a count or a fixed vocabulary; add its
  identifier shape to `UNSAFE_PATTERNS`, or its raw strings to `forbidden`, in
  the same change.
- Keep the refusal message free of the matched text: it is printed in logs.
- Run `scripts/security/tests/test_curate_snapshot.py` locally after a change;
  CI runs the rest.
