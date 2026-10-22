# Advisory AI pull request review

An automated, **advisory** code review. For every non-draft pull request against
`develop`, a GitHub Actions job runs Claude Code (via Amazon Bedrock) over the
diff with this repository's own review criteria and posts the result as a pull
request comment.

| File | What it is |
|---|---|
| `ai_pr_review.py` | The harness. Fetches metadata, builds a merge-base diff in a throwaway worktree, runs `claude -p`, enforces the disclosure contract, posts the comment. Stdlib only. |
| `cfn/github-oidc-review-role.yaml` | The GitHub OIDC provider and the Bedrock-only role the workflow assumes. |
| `test_ai_pr_review.py` | Unit tests for the disclosure guard (both directions, plus a re-measured false-positive rate) and for the CI-job properties most likely to be deleted by accident. |
| `../../.github/workflows/ai-pr-review.yml` | The GitHub job. Its header documents every design constraint. |
| `../../.claude/skills/pr-review.md` | **The review criteria** — the six questions and the red-flag list. Shared with the interactive `review <url>` workflow. |
| `../../.claude/skills/pr-review-ci.md` | What an unattended run does differently: output format, posting rules, the disclosure contract, which CI checks not to re-derive. |

Read the header of `.github/workflows/ai-pr-review.yml` before changing anything.
It states why each control exists, and most of them were learned the hard way.

## Activating it — two commands

**It is inert until both are run, and it stays green while it is.** Two independent
things make the workflow do nothing, and neither fails the job:

- **No `AI_REVIEW_ROLE_ARN` repository variable.** The role check reports a skip and
  every later step is conditional on it, so no credential is minted, no model is
  called and no comment is posted.
- **The harness is not on the target branch yet.** The pinning step refuses to run
  the pull request's own copy of the reviewer, and when the files it needs are not on
  `develop` it says so and exits 0 with the review skipped. That is the state on the
  pull request that *adds* this directory — `ai_pr_review.py` and `pr-review-ci.md`
  genuinely do not exist on `develop` until it merges — so the first run a maintainer
  sees from this change is a green job that skipped. It starts reviewing on the next
  pull request after the merge.

  `exit 1` is kept for `pr-review.md` and `CLAUDE.md`, which always exist on
  `develop`: a failure to resolve *those* means a rename nobody updated here, which
  is a real defect and should be loud.

### 1. Deploy the role

In the AWS account that should pay for the reviews:

```bash
AWS_PROFILE=default aws cloudformation deploy \
  --template-file scripts/pr_review/cfn/github-oidc-review-role.yaml \
  --stack-name lma-sdlc-github-review-role \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

If the account already has a GitHub Actions OIDC provider — check with
`aws iam list-open-id-connect-providers` — pass it rather than creating a second
one, which fails with `EntityAlreadyExists`:

```bash
  --parameter-overrides ExistingOidcProviderArn=arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com
```

### 2. Point the workflow at it

```bash
gh variable set AI_REVIEW_ROLE_ARN \
  --repo aws-samples/amazon-transcribe-live-meeting-assistant \
  --body "$(AWS_PROFILE=default aws cloudformation describe-stacks \
    --stack-name lma-sdlc-github-review-role --region us-east-1 \
    --query 'Stacks[0].Outputs[?OutputKey==`GitHubReviewRoleArn`].OutputValue' \
    --output text)"
```

A repository **variable**, not a secret: a role ARN is not a credential. It is
kept out of the tree only because it embeds an AWS account id and this repository
is public.

If the account mandates an IAM permissions boundary, add
`PermissionsBoundaryArn=arn:aws:iam::123456789012:policy/<name>` to the
`--parameter-overrides`.

The GitLab mirror's `ai_mr_review` job needs a `GITLAB_REVIEW_TOKEN` CI variable
(`api` scope, masked) and its own Bedrock-only credentials; until then it prints a
skip. See the job's header in `.gitlab-ci.yml`.

### 3. Later, once reviews are landing: `--fail-on-skip`

**Not part of the initial setup, and deliberately not done yet.** Add
`--fail-on-skip` to the GitHub invocation in `.github/workflows/ai-pr-review.yml`
after the first few reviews have posted successfully.

The reason to add it: on that platform the token always exists in a non-fork run
and the role check already gates the step, so once the variable is set every
remaining `Skip` cause is real breakage — a missing `claude` binary, an unreachable
Bedrock, a 401/403/404 from the API. Each of those currently produces a **green job
with no review**, which is the failure mode the loud-skip machinery exists to
prevent and the one it cannot prevent by itself.

The reason to wait: during activation a skip is the *expected* outcome, so turning
it into a failure first would just produce a red job telling you what you already
know. Note that `--fail-on-skip` interacts with `continue-on-error: true` on the
Review step — the step still will not fail the check, but the failure appears in the
log and the step is marked as having errored, which is the visibility that is
wanted.

## What it costs, and what bounds it

A review is a real Bedrock spend. **Measured on this repository**, from a local
dry run of this harness: **$0.83** for pull request #800 — 24 files, +1704/-131,
a 100 KB diff reviewed untruncated — on `us.anthropic.claude-opus-5-5`. LMA's
open pull requests run 600–2,900 changed lines, so **$0.30–1.50 per review** is
the expectation. No CI run has been metered yet; it should land near the same
figure, and the harness prints `total model cost` in its summary, so a real
number is one job log away.

Reviews are idempotent per head SHA, so a new push means a new paid review. Three
controls bound the total and **all three have to stay**:

1. `concurrency: cancel-in-progress` in the workflow — a push while a review is
   running cancels it, so a burst of pushes costs about one review rather than one
   per push. This is the main protection and the easiest to delete by accident.
2. The per-head-SHA marker (`<!-- ai-review: sha=… rev=… -->`) the harness reads
   back off the pull request's comments. A re-run over an unchanged head posts
   nothing and pays nothing — which is also what makes the hourly scheduled sweep
   affordable.
3. Drafts are excluded, so the work-in-progress phase pays nothing.

`test_ai_pr_review.py` asserts 1 and 3 directly.

## It is advisory, and must stay that way

A model's opinion must not decide whether code merges. The `code_checks` job (on
both GitHub Actions and GitLab) and `srt_security_review` are the gates.

Keep this job's status-check context out of every required-check set and out of
every branch ruleset. Requiring it would be worse than it looks: **GitHub reports
a conditionally skipped job as succeeding**, and this job skips on drafts and on
fork pull requests — so a required context would pass on exactly the pull requests
no review ran for. `test_ai_pr_review.py` fails if any workflow or ruleset in the
tree claims it as required.

## Disclosure safety — what gets published and what does not

This repository is public, and this job publishes model-written text on it.
CLAUDE.md's "Security Disclosure Hygiene" section applies to every artifact this
repository publishes, and a review comment is one of them.

**The control is the output contract** in `.claude/skills/pr-review-ci.md` §2: a
published security finding names the file, the line, and the control that is
needed — never the attack, the precondition, the flaw class, or what the control's
absence would permit. That file is pinned to the target branch before the reviewer
runs, so a pull request cannot edit its own contract.

**`scan_for_disclosure` is a backstop, not the control.** It scans the composed
comment before anything is posted, and on a match the whole review is **withheld**
(not edited, not trimmed) with a short neutral notice posted in its place. It
catches the common phrasings. It will not catch everything, and the gap is not
small — **measured: of fifteen realistically-phrased disclosure sentences put to an
earlier version, all fifteen would have published**, among them "the JWT is
accepted without checking its signature" (the rule wanted the literal "is not
verified") and "without the Admin group *test*" (the control-noun list had `check`
and `guard` but not `test`). Five of those phrasings are covered now and more will
always exist, because it is regular expressions over English.

So: do not read a clean run as evidence that a comment is safe, and do not
loosen the contract on the grounds that the guard will catch it.

**The calibration is measured in the other direction too, and that is where the
first version went badly wrong.** A trip withholds the *entire* review and the text
goes nowhere, so each false positive destroys a complete paid review and every
finding in it, and shows the author a notice naming an attack-narrative rule for a
sentence about a button. An earlier version keyed `grants-actor` on a grant verb
near any actor noun and `actor-can` on a determiner, an actor noun and a capability
verb — and LMA is a product about users, participants, clients and guests, so it
withheld **15 of 49** of this repository's own documentation pages, on lines like
"a compact widget that lets users start a meeting recording with one click".

Both of those rules now additionally *require* an action verb and a cross-principal
object ("another tenant's", "somebody else's"), which is what separates "a user can
see their own uploads" from "a non-admin can delete another person's meeting". That
took the figure to **1 of 49**, and the one page that still trips names threat
categories as categories — arguably correct rather than a false positive.
`test_the_guard_does_not_fire_on_this_repositorys_own_prose` re-measures this on
every CI run so it cannot drift back, and every sentence above is a pinned test
case.

**There is no private channel to defer detail to, and that was measured rather
than assumed.** On 2026-10-22, from an account holding only public read on a
public repository (`pull: true`, `push: false`, `triage: false`), both

- `GET /repos/{owner}/{repo}/actions/artifacts/{id}/zip`, and
- `GET /repos/{owner}/{repo}/actions/runs/{id}/logs`

returned **200 OK with real content**; artifact *metadata* reads with no
credential at all. So the obvious design — full review to the run artifact, safe
version to the comment — would publish the full review to anyone with a GitHub
account. It is not used here. The artifact carries **exactly the text that was
published**, the job log records only the *names* of the guard rules that fired,
and the withheld text is written **nowhere**.

What to do when a review is withheld:

- A maintainer can re-run it locally, where nothing is published:
  `make ai-pr-review PR=<n> ARGS=--no-redact-artifact`. That writes the full text
  to `ai-reviews/` on their own disk and posts nothing. Neither CI job passes that
  flag.
- Anything genuinely security-relevant goes to AWS/Amazon Security through the
  channel in [CONTRIBUTING.md](../../CONTRIBUTING.md#security-issue-notifications).
  That is the one non-public channel that exists.

## Keeping the reviewer out of the reviewer

The job hands a model a checkout of author-controlled code plus a token that can
comment publicly on a public repository. The controls, in rough order of how much
they carry:

1. **`--restricted` on the CLI** (requires Claude Code ≥ 2.1.280-ish; verified in
   the pinned 2.1.308). It ignores user, project and local settings files, confines
   the file tools to the working directory, and refuses `bypassPermissions`. This
   replaces three arguments with one mechanism — and the confinement was **measured**
   under this exact argv on 2.1.308: a probe asked for `/etc/hostname` and an in-tree
   file; without the flag the out-of-tree read was **served**, with it the read was
   **refused** with an explicit permission denial while the in-tree read still
   worked.
2. **The harness and the instruction files are checked out of the target branch**
   before the reviewer runs, so a pull request cannot supply its own reviewer,
   permission mode, tool lists, prompt or disclosure contract.
3. **The whole `.claude/` directory is removed from the worktree** and only the
   pinned files are written back. Overwriting a list of known paths was not enough:
   a skill can be a *directory* whose `SKILL.md` frontmatter is injected into the
   system prompt, which `.claude/skills/*.md` missed. Removing the tree needs no list
   and so cannot be incomplete. An instruction file a pull request *adds* anywhere —
   including `AGENTS.md`, which Claude Code also reads — is deleted rather than
   applied, and the review is told it was.
4. **`persist-credentials: false`** on checkout, so the job's token is not left in
   `.git/config` in a tree the model can read.
5. `Read`, `Grep`, `Glob` and nothing else, with `--permission-mode manual` and
   `--strict-mcp-config` named in the argv rather than left to a settings file.
6. **Every forge and Actions credential stripped from the child environment** before
   it starts — including `ACTIONS_ID_TOKEN_REQUEST_TOKEN`/`_URL`, which are a second
   route to the AWS credential whenever the job has `id-token: write`.
7. **Nothing in the job builds or installs from the checkout.** On the scheduled path
   the diff can come from a fork and the job holds a Bedrock credential, so a
   `pip install -r` or `make` target there would hand a fork code execution — before
   the model runs, so the model's sandbox would be irrelevant.
8. Diagnostics carrying raw CLI or model output are scanned by the disclosure guard
   before they reach the job log or `summary.json`.

Fork pull requests get no OIDC token, which is the protection rather than a bug;
they are covered by the scheduled sweep instead.

## Running it by hand

```bash
# one pull request, printed rather than posted
make ai-pr-review PR=800

# same thing directly
python3 scripts/pr_review/ai_pr_review.py --mr 800 --dry-run --remote github

# what the workflow runs (needs GITHUB_TOKEN with pull-requests: write)
python3 scripts/pr_review/ai_pr_review.py --forge github --remote origin --mr 800

# the GitLab mirror
python3 scripts/pr_review/ai_pr_review.py --forge gitlab --remote origin --mr 77 --dry-run
```

`--remote` matters locally and does not in CI: a developer checkout of this
repository has **two** remotes (`github` for GitHub, `origin` for the GitLab
mirror), and asking the wrong one for a `refs/pull/` ref fails at the fetch before
anything is reviewed. In CI there is one remote and it is the forge running the
job, which is why both CI jobs pass `--remote origin`.

Local runs need a `claude` CLI on `PATH`, Node ≥ 22, and AWS credentials with
`bedrock:InvokeModel` on the model in `DEFAULT_MODEL`. A missing precondition
produces a loud `SKIPPED:` line naming what is absent, never a clean-looking exit.

Two things that only bite locally:

- **`~/.claude/settings.json` can redirect credential resolution.** On an Amazon
  developer machine it often sets `awsCredentialExport` to an Isengard helper, and
  the `claude` CLI then uses that instead of the ambient `AWS_PROFILE` —
  failing with `failed to resolve Isengard credentials` even though
  `aws sts get-caller-identity` works. Run with
  `CLAUDE_CONFIG_DIR=$(mktemp -d)` to bypass the machine's settings. A CI runner
  has no user settings file, so this does not apply there.
- **`.claude/skills/pr-review-ci.md` has to exist on the target branch** for the
  harness to pin it. Until this change is merged it does not, so a dry run reviews
  with `pr-review.md` and `CLAUDE.md` alone and says so in its own output. That is
  the "first landing" case both CI jobs' pinning steps describe.

## Tests

```bash
make test-pr-review
```

Runs in `code_checks` on both platforms. Three groups:

- **The disclosure guard, in both directions.** That each rule fires on the shape it
  is for — including the vocabulary-free shapes that a word list misses and the
  backtick-evasion cases — and that every ordinary sense CLAUDE.md permits is
  cleared, with every measured false positive from `docs/` and `CHANGELOG.md`
  pinned as its own case. Plus that the harness's own footer and withheld notice are
  themselves publishable, since if they were not, every comment would be withheld
  including the one explaining why.
- **The false-positive rate, re-measured on every run.**
  `test_the_guard_does_not_fire_on_this_repositorys_own_prose` scans every `docs/*.md`
  page over 2 KB as a whole document and fails if more than a ceiling of them trip.
  This is the test the first version of the guard needed and did not have: it was at
  15 of 49.
- **The properties of the CI jobs most likely to be deleted by accident** —
  `cancel-in-progress` and a per-PR concurrency group, the draft exclusion, the
  harness-pinning step on *both* platforms (including that it exits 0 rather than
  reddening an advisory job, and that the Review step reads its outcome),
  `persist-credentials: false`, `continue-on-error`/`allow_failure`, the three-way
  arithmetic between `--max-mrs`, `--timeout` and `timeout-minutes`, the absence of
  `--no-redact-artifact` from both invocations, `--restricted` and the tool lists,
  the stripped credential list, the auto-discovered instruction shapes, and that
  nothing in the tree claims the job as a required check.
