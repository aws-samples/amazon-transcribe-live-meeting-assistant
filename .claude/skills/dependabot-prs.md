# Dependabot PR Review & Merge Skill — LMA

Use this skill when the user asks to **review, triage, or merge Dependabot PRs**
(e.g. "review the new dependabot PRs", "merge the dependabot bumps if safe").

This is distinct from `pr-review.md` (a written review of someone else's PR at a
URL) — here the end state is usually *merged or held, with a reason*.

Four invariants — never skip any of them:

1. **Green CI is necessary but not sufficient.** The `Code Checks` workflow
   (`.github/workflows/code-checks.yml`, job name "Lint and unit tests", ~4–5
   min) does run on every Dependabot PR and is a real signal. But it has
   specific blind spots listed under *CI coverage map* below — most importantly
   it never `pip install`s most `requirements.txt` files, so a pip bump can be
   green and still uninstallable.
2. **Never bulk-merge.** Assess each PR (Step 3), and handle majors separately
   from minor/patch.
3. **Something must be run locally** for any bump CI does not cover, before
   merging: `pip install` for pip dirs, `npm install`/build for npm dirs CI
   skips, `make docker-build-check*` for anything in a container image.
4. **Repo wording rules apply.** These PRs land on a public repository. When
   writing merge commits, CHANGELOG entries, or a new `ignore` note in
   `.github/dependabot.yml`, follow the Security Disclosure Hygiene section of
   `CLAUDE.md`. Naming a CVE/GHSA that an *upstream dependency* fixed is
   explicitly fine; describing a weakness in *this* repo is not.

Dependabot here already targets `develop` (`target-branch: develop` on every
entry in `.github/dependabot.yml`), so unlike some repos there is **no
retargeting step**. Still confirm `baseRefName` before merging — a rebase or a
config edit can move it.

## Step 1 — Inventory and classify

```bash
gh pr list --author "app/dependabot" --state open \
  --json number,title,baseRefName,headRefName,createdAt,url --limit 50
```

Sort the list into four kinds, because each is handled differently:

- **Grouped npm PRs** — titled `bump the <area>-minor-patch group …`. One per
  area per week (`ui`, `transcriber`, `vp`, `browser-ext`, `ai-stack-tooling`,
  `tooling`). These are the normal case: verify, squash-merge individually.
- **Grouped major PRs** — `<area>-major`. Never "merge-if-safe" territory.
  Read the upstream breaking changes, decide per dependency, and expect either
  a code change alongside it or a new `ignore` rule.
- **Grouped pip PR** — `bump the python-minor-patch group across N directories`.
- **Ungrouped pip singletons** — titled `update <dep> requirement from A to B
  in /<dir>`. Dependabot's pip constraint-updates are *not* folded into the
  `python-minor-patch` group, so a weekly run can produce 20 of these (e.g.
  #699–#718 on 2026-09-23). Two traps:
  - The `directories:` glob `/lma-ai-stack/source/lambda_layers/*` matches both
    a parent and its child, so **byte-identical pairs** appear (#600/#604,
    #601/#605, #602/#603). Diff them and close the duplicate.
  - A lone bump can be **uninstallable because its companion pin was left
    behind** — #612 bumped `pytest` to 9 while `pytest-asyncio==0.24.0` pinned
    `pytest<9`. Always `pip install -r` the changed file (Step 4).

## Step 2 — Redundancy check

Dependabot diffs against `develop`, which is the working branch, so stale-PR
redundancy is less common than in repos where it targets `main`. It still
happens when a bump landed manually or in an earlier batch. Check the version
actually on `develop` before reviewing:

```bash
git fetch origin develop --quiet
# npm: resolved version in the lockfile
git show origin/develop:lma-ai-stack/source/ui/package-lock.json | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(d['packages']['node_modules/<dep>']['version'])"
# pip: the constraint in the manifest
git show origin/develop:lma-ai-stack/source/lambda_layers/<layer>/requirements.txt
```

If `develop` already has that version or newer, **close the PR** with a one-line
comment saying it is already on `develop`. Do not merge it.

## Step 3 — Risk assessment (per PR)

Read `gh pr diff <NN>` and `gh pr view <NN> --json body` (Dependabot inlines
release notes and commit lists). Assess:

- **Semver distance** — patch < minor < major. Grouped minor/patch with green
  CI plus the local check from Step 4 is the merge bar; majors go to the user.
- **Advisory content** — a CVE/GHSA fix in the dependency raises urgency and
  usually justifies the merge; cite the advisory id in your summary.
- **Where it runs.** Blast radius in this repo is mostly a question of *which
  stack*:
  - `lma-websocket-transcriber-stack/source/app` — internet-facing runtime,
    handles live audio. Highest care; also built into a container image.
  - `lma-virtual-participant-stack/backend` — container image, Playwright /
    Zoom / Azure SDK constraints (see the `ignore` notes; `react` is pinned to
    exactly 18.2.0 by `@zoom/meetingsdk`'s exact peer).
  - `lma-ai-stack/source/lambda_functions/*`, `lambda_layers/*` — Lambda
    runtime deps, vendored at build time by `publish.sh`.
  - `lma-ai-stack/source/ui`, `/docs-site`, `/lma-ai-stack/deployment/manifest-generator`,
    `/utilities/websocket-client`, `/lma-ai-stack` (prettier/eslint), `/` —
    build/dev tooling, lower risk.
- **`dependencies` vs `devDependencies`** (`chore(deps)` vs `chore(deps-dev)`
  in the title).
- **New transitive packages** — scan the lockfile diff for newly added entries
  and name them in the summary.
- **Behavioural changes** — skim release notes for changed defaults, removals,
  renamed options; grep our source for the affected API when unsure.

If a bump is genuinely incompatible, the repo's established answer is **not** to
leave the PR open: add an `ignore` entry to `.github/dependabot.yml` with a
comment explaining the constraint in the same style as the existing ones (state
the upstream fact and the condition for un-ignoring), then close the PR
referencing it. That is what keeps the weekly run from re-raising it.

## Step 4 — Local verification (the part CI does not do)

### CI coverage map

What `code-checks.yml` actually installs and exercises:

| Area | Covered by CI? |
|---|---|
| `lma-ai-stack/source/ui` | Yes — `npm ci` + eslint + vitest (`make lint-ui-force`, `make test-ui-force`) |
| `lma-websocket-transcriber-stack/source/app` | Yes — `npm ci`, `npm test` (tsc + eslint + node --test), `npm run smoke` |
| `lma-virtual-participant-stack/backend` | Yes — `npm ci` + `npm test` + `tsc` (`make test-vp`, `make lint-typescript`) |
| `lma-browser-extension-stack` | Yes — `npm install` (deliberately not `npm ci`) + test + build |
| `lma-asr-microvm-stack/source` | Yes — its `requirements-dev.txt` **is** pip-installed by `make test-asr` |
| All other pip manifests (Lambda functions, layers, `lma-ai-stack/requirements/*`) | **No** — `make setup-python` installs a fixed lint-tool list, and `make test-lambdas` runs pytest in the repo venv. Nothing resolves these files. |
| `/`, `/docs-site`, `/lma-ai-stack` (prettier), `/lma-ai-stack/deployment/manifest-generator`, `/utilities/websocket-client` | **No** — never installed or built in CI |
| Container image builds (transcriber, VP) | **No** — `make docker-build-check*`, local only |
| Live runtime behaviour | **No** — `make integ-tests`, and `docs/dependency-upgrade-validation-runbook.md` |

### Recipes

Work in a throwaway worktree so `develop` stays clean:

```bash
git fetch origin pull/<NN>/head:pr-<NN> && git worktree add /tmp/wt-<NN> pr-<NN>
```

```bash
# pip manifest bump — the single most important local check:
python3 -m venv /tmp/v<NN> && /tmp/v<NN>/bin/pip install -r <changed requirements.txt>
# Resolution failure here is exactly the #612 class of problem. Then, if the dir
# has tests: cd <dir> && /tmp/v<NN>/bin/python -m pytest -q

# npm dir that CI does not build (docs-site, manifest-generator,
# websocket-client, root, lma-ai-stack tooling):
cd <dir> && npm ci && npm run build   # whichever scripts exist

# Browser extension — run `npm ci` FIRST as a lockfile check. It has repeatedly
# failed with `EUSAGE … Missing: <pkg> from lock file` because Dependabot's npm
# drops a nested lockfile entry that npm 11.x writes. If it fails that way, run
# `npm install` and commit the regenerated lockfile onto the PR branch — that is
# the fix. Then verify the way CodeBuild does, with no CI env var:
cd lma-browser-extension-stack && npm install && npm run build

# Anything in a container image (transcriber or VP deps, Dockerfile-adjacent):
make docker-build-check          # transcriber (fast)
make docker-build-check-all      # + Virtual Participant (heavy)
```

For a transcriber or VP **major** (Fastify, Zoom SDK, Playwright, React), static
checks are not enough — `docs/dependency-upgrade-validation-runbook.md` is the
live-meeting validation procedure written for exactly this case (deploy to a
test stack, stream real audio, join a real Zoom meeting).

## Step 5 — Merge

Only after: risk assessed, `baseRefName == develop`, the local check for that
area passed, and `Lint and unit tests` is green.

```bash
gh pr checks <NN>
gh pr view <NN> --json mergeable,mergeStateStatus,baseRefName
gh pr merge <NN> --squash
```

`--squash` is the convention (verified: merged Dependabot commits on `develop`
are single-parent). `mergeStateStatus: UNSTABLE` on its own just means a
non-required check has not finished — `CodeQL` reports `skipping` on dependency
PRs, which is expected and not a failure.

If the PR is `CONFLICTING`/`DIRTY`, comment `@dependabot rebase` and poll until
Dependabot force-pushes (~1–2 min, which also restarts CI). After any rebase,
re-check `baseRefName` and re-run the local check — the diff may have changed.

### When to batch instead

The repo's fallback for a pile-up is a single consolidated branch
`chore/dependabot-batch-N` with a PR titled `chore(deps): consolidate N
Dependabot bumps (tested batch)` (precedent: #400, #442, #618; `-6` exists, so
the next free number is `chore/dependabot-batch-7`). Now that CI runs per-PR,
prefer individual squash-merges and reserve a batch for cases where one
validation run should cover many bumps — a set of majors, or several bumps that
all need the same deploy-and-test cycle from the runbook.

## Step 6 — Post-merge validation (mandatory)

Merged ≠ done. Pull `develop` and run what the merged set touched:

```bash
git checkout develop && git pull
make lint test                  # cfn + python + UI lint, then all no-AWS suites
make test-vp test-vp-template   # if VP deps moved
make docker-build-check-all     # if transcriber or VP deps moved
```

One run at the end covers several merges. If validation fails, identify the
offending bump, `git revert -m 1 <sha>` (or plain revert for a squash),
push, and report the failure with the test output — then add an `ignore` entry
if the bump cannot be taken at all.

## Step 7 — Report

Per PR: dependency (or group and dep count), old→new versions, risk verdict
with advisories and new transitive packages noted, what was run locally and its
result, action taken (merged / closed as duplicate / closed + `ignore` added /
left open and why), and the post-merge validation outcome.

If a CHANGELOG entry is warranted — the convention here is one rolled-up
"Dependency updates across every stack" bullet per release rather than a line
per bump — see `.claude/skills/prepare-changelog.md`.
