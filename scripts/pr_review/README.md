# Advisory AI pull request review

An automated, **advisory** code review. For every non-draft pull request against
`develop`, a GitHub Actions job runs Claude Code (via Amazon Bedrock) over the
diff with this repository's own review criteria and posts the result as a pull
request comment.

| File | What it is |
|---|---|
| `ai_pr_review.py` | The harness. Fetches metadata, builds a merge-base diff in a throwaway worktree, runs `claude -p`, enforces the disclosure contract, posts the comment. Stdlib only. |
| `cfn/github-oidc-review-role.yaml` | The GitHub OIDC provider and the Bedrock-only role the workflow assumes. |
| `test_ai_pr_review.py` | Unit tests for the disclosure guard and for the four workflow properties most likely to be deleted by accident. |
| `../../.github/workflows/ai-pr-review.yml` | The GitHub job. Its header documents every design constraint. |
| `../../.claude/skills/pr-review.md` | **The review criteria** — the six questions and the red-flag list. Shared with the interactive `review <url>` workflow. |
| `../../.claude/skills/pr-review-ci.md` | What an unattended run does differently: output format, posting rules, the disclosure contract, which CI checks not to re-derive. |

Read the header of `.github/workflows/ai-pr-review.yml` before changing anything.
It states why each control exists, and most of them were learned the hard way.

## Activating it — two commands

**It is inert until both are run.** Without the repository variable the workflow
checks out, installs the CLI, prints a skip and exits 0 — no credential, no model
call, no comment.

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

The GitLab mirror's `ai_mr_review` job needs a `GITLAB_REVIEW_TOKEN` CI variable
(`api` scope, masked) and its own Bedrock-only credentials; until then it prints a
skip. See the job's header in `.gitlab-ci.yml`.

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

**The output contract** is `.claude/skills/pr-review-ci.md` §2: a published
security finding names the file, the line, and the control that is needed — never
the attack, the precondition, the flaw class, or what the control's absence would
permit.

**It is enforced, not trusted.** `scan_for_disclosure` scans the composed comment
before anything is posted. On a match the whole review is **withheld** (not
edited, not trimmed) and a short neutral notice is posted in its place. The guard
is a set of attack-narrative *shapes* plus flaw-class terms each paired with
clearance patterns — not a word list — because CLAUDE.md explicitly permits the
ordinary senses (a cache bypass, a dependency CVE bump, prompt injection as a
product concern, Teams' own lobby-bypass setting, a test named
`test_unauthenticated_user_rejected`). A guard that withheld reviews over those
would be deleted within a week, so each of them is a test case.

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

Runs in `code_checks` on both platforms. Two groups:

- **The disclosure guard** — that each rule fires on the shape it is for, that
  every ordinary sense CLAUDE.md permits is cleared, and that the harness's own
  footer and withheld notice are themselves publishable (if they were not, every
  comment would be withheld).
- **The four workflow properties most likely to be deleted by accident** —
  `cancel-in-progress`, the draft exclusion, the harness-pinning step, and
  `persist-credentials: false` — plus that the job is claimed as required nowhere.
