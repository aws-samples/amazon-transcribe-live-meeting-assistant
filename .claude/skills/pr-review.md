# PR / MR Review Skill — LMA

Use this skill when the user asks to **review a pull request or merge request** —
typically with a prompt like:

> `review <PR/MR URL>`  (target develop branch)
>
> Is this a good PR?
> Safe? No regressions?
> Good UX?
> No security issues?
> Well documented?
> Safe to merge?

This skill is for reviewing **someone else's** PR/MR at a URL. It is distinct
from `code-review.md`, which is the pre-commit self-review checklist applied to
my own changes before returning them to the user.

## Ground Rules

1. **Never push, merge, approve, or comment on the PR/MR** from this skill
   unless the user explicitly asks. Produce a written review for the user to
   act on.
2. **Do not modify files** in the working copy as part of the review. Reviewing
   is read-only.
3. Reference specific **files and line numbers** whenever citing an issue.
4. If the PR/MR targets a branch **other than `develop`**, call this out as the
   first finding. The expected target for this repo is `develop`.
5. Keep findings factual and specific — quote the diff where useful.

## Step 1 — Detect the host from the URL and fetch metadata

LMA lives on **two remotes**:
- **GitHub** (public): `github.com/aws-samples/amazon-transcribe-live-meeting-assistant` — use `gh` CLI.
- **GitLab** (AWS internal mirror): `gitlab.aws.dev/strahanr/LMA` — use
  **git over SSH** for the diff (the `glab` CLI is not reliably available
  in this environment), and `curl` against the GitLab HTTPS API for
  metadata.

Detect which host from the URL the user provided. Both are first-class.

### GitHub PR (`github.com/<owner>/<repo>/pull/<NN>`)

```bash
gh pr view <NN> --repo <owner>/<repo> --json \
  title,body,author,state,mergeable,mergeStateStatus,baseRefName,headRefName,\
additions,deletions,changedFiles,files,statusCheckRollup,reviews,comments,url

gh pr diff   <NN> --repo <owner>/<repo>
gh pr checks <NN> --repo <owner>/<repo>
```

### GitLab MR (`gitlab.aws.dev/<group>/<project>/-/merge_requests/<NN>`)

The `origin` remote is already the SSH-backed GitLab mirror
(`git@ssh.gitlab.aws.dev:<group>/<project>.git`), so SSH auth is wired up
— no extra setup.

**Step A — fetch the MR ref over SSH and inspect the diff with `git`:**

```bash
NN=<MR number from URL>

# Fetch the MR head into a local ref via SSH
git fetch origin "refs/merge-requests/$NN/head:mr-$NN"

# Files changed
git diff --name-status origin/develop...mr-$NN

# Size (insertions / deletions / file count)
git diff --shortstat origin/develop...mr-$NN

# Commits on the MR
git log --oneline origin/develop..mr-$NN

# Full diff
git diff origin/develop...mr-$NN

# When done, clean up the local ref
git branch -D mr-$NN   # optional
```

Use `A...B` (three dots) for `git diff` so it diffs from the merge base
— that matches what GitLab shows in the MR. Use `A..B` (two dots) for
`git log` to list commits on the MR not on `develop`.

**Step B — pull metadata via the GitLab HTTPS API:**

The GitLab API is HTTP-only (no SSH transport), so `curl` is the path for
title, description, author, reviews, comments, and pipeline/CI status.

```bash
PROJECT=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote_plus(sys.argv[1]))" \
  "<group>/<project>")
TOKEN="${GITLAB_TOKEN:?Set GITLAB_TOKEN to a personal access token (read_api scope) in your shell}"

# MR metadata (title, description, author, source/target branch, draft, ...)
curl -sH "PRIVATE-TOKEN: $TOKEN" \
  "https://gitlab.aws.dev/api/v4/projects/$PROJECT/merge_requests/$NN" | jq

# MR comments / discussion notes
curl -sH "PRIVATE-TOKEN: $TOKEN" \
  "https://gitlab.aws.dev/api/v4/projects/$PROJECT/merge_requests/$NN/notes" | jq

# Pipeline / CI status
curl -sH "PRIVATE-TOKEN: $TOKEN" \
  "https://gitlab.aws.dev/api/v4/projects/$PROJECT/merge_requests/$NN/pipelines" | jq
```

If `$TOKEN` is empty, proceed with the SSH-only path: you can still review
the diff and commits, but note in the report that CI status, comments,
and the MR description couldn't be checked.

### Collect at minimum

- PR/MR title, description, author
- Source branch → target branch (confirm target is `develop`)
- CI / pipeline status
- List of changed files with insertions / deletions
- The full diff
- Existing reviews and comments
- Linked issues, if any

For larger PRs (> ~500 lines changed or touching unfamiliar modules), read key
changed files in full — not just the diff — using the `Read` tool on paths at
the head ref so surrounding context is captured.

## Step 2 — Evaluate the six review questions

Work through each question. Cross-reference the other LMA skills
(`backend-lambda.md`, `frontend-ui.md`, `infrastructure.md`, `code-review.md`)
for stack-specific conventions.

### 1. Is this a good PR?
- Scope is focused (single feature / fix / refactor; not a grab-bag)
- Title is descriptive and follows convention (e.g. `feat:`, `fix:`, `docs:`,
  `chore:`)
- Description explains **what** and **why**, and links to an issue when
  applicable
- Targets `develop` (flag otherwise)
- CI is green (or failures are understood)
- Size is reviewable — very large PRs should be called out

### 2. Safe? No regressions?
- New/changed logic has tests where practical
- Existing tests are not deleted or weakened without justification
- Backward compatibility maintained for shared interfaces:
  - AppSync GraphQL schema (`lma-ai-stack/source/appsync/schema.graphql`) and
    the generated UI clients in `lma-ai-stack/source/ui/src/graphql/`
  - Lambda handler event/response contracts (Kinesis, SQS, AppSync resolvers)
  - DynamoDB item shapes (the EventSourcing table is append-only — schema
    changes need migration thought)
  - WebSocket transcriber message protocol
    (`lma-websocket-transcriber-stack/source/app/src/`)
- No obvious race conditions, unbounded loops, or missing pagination
- Error handling uses explicit exceptions (no bare `except:`); use
  `LOGGER.exception(...)` or `LOGGER.error(..., exc_info=True)` when re-raising
- No removal of observability — Powertools `Logger`, X-Ray `Tracing: Active`,
  CloudWatch metrics

### 3. Good UX?
- **Frontend** (`lma-ai-stack/source/ui/`):
  - Cloudscape Design System components (not MUI / AntD / Bootstrap)
  - Loading, empty, and error states handled
  - Arrow-function components (enforced by ESLint
    `react/function-component-definition`)
  - New custom hooks in kebab-case (`use-thing.js`); legacy `useThing.js`
    files are kept as-is
  - Amplify `ConsoleLogger` used, not stray `console.log` in production code
  - Auth via `@aws-amplify/ui-react` `useAuthenticator`
  - GraphQL: hand-edits to `mutations.js`/`queries/*.js` are acceptable in
    this repo (the files are tracked and edited, not auto-regenerated by a
    Make target) — but if the AppSync schema changed, verify the client-side
    operations still align
  - Accessibility: labels on inputs, keyboard nav, focus management
  - `DOMPurify` used for any `dangerouslySetInnerHTML` / markdown rendering
- **Backend / API**:
  - Actionable error messages; failures surface to the UI (e.g. status updates
    via AppSync subscriptions)
  - Sensible defaults; new required parameters have migration guidance in the
    PR description

### 4. No security issues?
- No hardcoded credentials, API keys, or tokens
- No full JWTs or session tokens logged in plaintext
- **GovCloud-friendly ARNs**: new code uses
  `!Sub "arn:${AWS::Partition}:..."` — flag any new hardcoded `arn:aws:`
  literal in CloudFormation/SAM templates (existing ones in `AllowedPattern`
  regexes are fine)
- **GovCloud-friendly endpoints**: new code uses `${AWS::URLSuffix}` — flag
  any new hardcoded `amazonaws.com` literal
- IAM policies scoped — flag new `Resource: "*"` or wildcard ARNs without
  written justification (existing ones with `Resource: "*"` for services that
  require it, like `bedrock:InvokeModel*`, are fine)
- `PermissionsBoundary` conditional present on new IAM roles
  (`!If [HasPermissionsBoundary, !Ref PermissionsBoundaryArn, !Ref AWS::NoValue]`)
- New Lambdas have a dedicated `AWS::Logs::LogGroup` with KMS encryption
  (`KmsKeyId: !Ref CustomerManagedEncryptionKeyArn`) and the function uses
  `LoggingConfig.LogGroup: !Ref ...LogGroup`
- Bedrock invocation clients have retry config
  (`max_attempts: 50, mode: adaptive`) for inference-profile reliability
- Input validation on new API surfaces; `DOMPurify` on HTML render paths
- New Python/npm dependencies are pinned and from trusted sources
- `cfn-nag` / `checkov` suppressions include a `reason:` field — never
  silently suppress
- No new public S3 buckets, public SNS topics, or open security groups

### 5. Well documented?
- User-facing changes update `CHANGELOG.md` (Keep-a-Changelog format)
- Feature docs added/updated under `docs/` with:
  - YAML frontmatter (`title:` required, used by Astro/Starlight site)
  - License/copyright line after frontmatter
- New entries linked from `docs/INDEX.md` where appropriate
- Public functions/classes have docstrings
- Complex logic has inline comments explaining *why*
- If the AppSync schema changed
  (`lma-ai-stack/source/appsync/schema.graphql`), the corresponding resolvers
  under `lma-ai-stack/source/appsync/` and any UI consumers in
  `lma-ai-stack/source/ui/src/graphql/` are updated together

### 6. Safe to merge?
- Targets `develop` ✅
- CI / pipeline green ✅
- No unresolved review comments on the latest commit
- No merge conflicts
- `VERSION` bumped if required by change scope
- No leftover `TODO` / `FIXME` / debug prints / commented-out code blocks
- Migration notes provided for any breaking change

## Step 3 — Produce the review

Output the review in this exact structure so the user can paste it or act on
it:

```markdown
## PR/MR Review: <title> (#<NN>)
**Repo:** <owner/repo>
**Author:** @<author>
**Source → Target:** `<source>` → `<target>`   <!-- ⚠️ if not develop -->
**CI status:** ✅ passing | ❌ failing | ⏳ pending
**Size:** +<insertions> / -<deletions> across <N> files

### Summary
<1–3 sentence plain-English description of what this PR does.>

### Verdict

| Question | Verdict | Notes |
|---|---|---|
| Good PR? | ✅ / ⚠️ / ❌ | … |
| Safe / no regressions? | … | … |
| Good UX? | … / N/A | … |
| No security issues? | … | … |
| Well documented? | … | … |
| **Safe to merge?** | ✅ Approve / ⚠️ Request changes / ❌ Block | … |

### Findings
- 🔴 **Blocking** — <file>:<line> — description + suggested fix
- 🟡 **Should fix** — <file>:<line> — description
- 🟢 **Nice to have** — <file>:<line> — description

### Recommendation
**Approve** / **Request changes** / **Comment**

<one-paragraph rationale>
```

Legend:
- ✅ pass / 🟢 nit — non-blocking
- ⚠️ / 🟡 — should fix before merge
- ❌ / 🔴 — blocking; must fix before merge

## Step 4 — After delivering the review

- Do **not** post the review as a PR/MR comment unless the user explicitly
  said "post this as a review comment" or similar.
- If the user asks follow-up questions, re-use the already-fetched diff /
  metadata rather than re-fetching.
- If the user asks you to implement the requested changes, switch context:
  treat it as a new implementation task following the relevant skill files
  and the `code-review.md` pre-commit checklist.

## Quick Reference — Common Red Flags

Flag these immediately when seen in any diff:

- New hardcoded `arn:aws:` in a CloudFormation / SAM template
  (should be `!Sub "arn:${AWS::Partition}:..."`)
- New hardcoded `amazonaws.com` (should be `${AWS::URLSuffix}`)
- New IAM policy with `Resource: "*"` and no `cfn-nag` / `checkov`
  suppression with `reason:`
- New Lambda without a dedicated `AWS::Logs::LogGroup` referenced from
  `LoggingConfig.LogGroup`
- New Lambda LogGroup without `KmsKeyId: !Ref CustomerManagedEncryptionKeyArn`
- New IAM role without the `PermissionsBoundary` `!If` conditional
- `print(` used for logging in Lambda code (should be
  `aws_lambda_powertools.Logger`)
- `console.log(` in production UI code (should be Amplify `ConsoleLogger`)
- Bedrock client without retry config — at minimum
  `BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 50})` for
  inference profiles
- Missing copyright header (`Copyright (c) 2025 Amazon.com` + MIT license
  line) at the top of new Python / JS / TS / YAML files
- AppSync `schema.graphql` change without matching updates to resolvers in
  `lma-ai-stack/source/appsync/` or the UI clients in
  `lma-ai-stack/source/ui/src/graphql/`
- Secrets / access keys / JWTs in code, logs, or tests
- Deleted tests without a replacement
- `cfn-nag` / `checkov` suppressions added without a `reason:` field
