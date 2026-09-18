# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Live Meeting Assistant (LMA) -- an AWS-based solution for real-time meeting transcription, AI-powered meeting assistance, and virtual meeting participation. Built on Amazon Transcribe, Amazon Bedrock, and the Strands Agents SDK. Current version is tracked in `./VERSION`.

## Build & Publish

**Prerequisites:** bash, node v22 (>=22.22.2; required by jsdom 30 in the UI test stack), npm, docker (running), zip, python3, pip3, virtualenv, aws cli, sam cli (>=1.118.0).

**AWS profile:** Always use `AWS_PROFILE=default` for build/deploy/test commands in this repo unless the user explicitly tells you otherwise. Other profiles (e.g. `bedrock`) point at unrelated accounts and will fail with `AccessDenied` on S3/CloudFormation.

**Full build and publish to S3:**
```bash
./publish.sh <cfn_bucket_basename> <cfn_prefix> <region> [public]
```
This validates dependencies, builds all stacks (SAM + npm), uploads artifacts to S3, and outputs CloudFormation deploy URLs. Deployment takes 35-40 minutes via CloudFormation.

**AI stack Makefile** (in `lma-ai-stack/`):
- Requires `CONFIG_ENV` env var (maps to SAM `--config-env`). Set in `config.mk` or `config-$(USER).mk`.
- `make install` -- set up Python venvs and npm deps
- `make build` -- build SAM application
- `make package` -- package artifacts
- `make deploy` -- deploy CloudFormation stack
- `make test-local-invoke-default` -- local SAM Lambda invocation

**UI** (in `lma-ai-stack/source/ui/`):
```bash
npm install && npm start    # local dev server
npm run build               # production build
npm test                    # vitest tests
```

**WebSocket server** (in `lma-websocket-transcriber-stack/source/app/`):
```bash
npm install && npm run build   # TypeScript build
npm test                       # build + node --test on dist/**/*.test.js
```

**ASR MicroVM runtime** (in `lma-asr-microvm-stack/source/`):
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q   # no model weights needed (backends are injected)
.venv/bin/ruff check .
```

**Virtual Participant** (in `lma-virtual-participant-stack/backend/`):
```bash
npm install && npm run build   # TypeScript build
npm test                       # node:test unit tests
```
Its CloudFormation/entrypoint tests are Python:
```bash
python3 -m pytest lma-virtual-participant-stack/test/
```

## Linting

Makefile targets in `lma-ai-stack/`:
- `make lint-cfn-lint` -- CloudFormation template lint
- `make lint-yamllint` -- YAML lint
- `make lint-pylint` -- Python lint (100 char lines, see `.pylintrc`)
- `make lint-mypy` -- Python type checking
- `make lint-bandit` -- Python security scanning
- `make lint-validate` -- SAM template validation

JavaScript/TypeScript uses ESLint (airbnb-base) + Prettier (120 char lines, single quotes, trailing commas). Config in `lma-ai-stack/.eslintrc.json` and `.prettierrc`.

Python uses Black (formatter), Flake8, Pylint (100 char lines). Config in `lma-ai-stack/.pylintrc` and `.flake8`.

## Architecture

**Nested CloudFormation stacks** orchestrated by `lma-main.yaml`:

| Stack | Purpose | Language |
|-------|---------|----------|
| `lma-ai-stack/` | Core stack: Lambda functions, AppSync GraphQL API, Cognito auth, DynamoDB, UI (React/CloudFront) | Python (Lambdas), React (UI) |
| `lma-websocket-transcriber-stack/` | WebSocket server on ECS Fargate ingesting stereo audio, streaming to Amazon Transcribe, writing to Kinesis | TypeScript/Fastify |
| `lma-virtual-participant-stack/` | Headless CloakBrowser/Chromium (Playwright) on ECS Fargate joining meetings, optional voice assistant + avatar | TypeScript |
| `lma-asr-microvm-stack/` | Optional on-demand streaming ASR + speaker diarization on Lambda MicroVMs (alternative to Amazon Transcribe); model selectable by CFN parameter | Python (sherpa-onnx) |
| `lma-vpc-stack/` | VPC networking, security groups, NAT gateways | CloudFormation |
| `lma-meetingassist-setup-stack/` | Meeting assistant configuration | CloudFormation |
| `lma-bedrockkb-stack/` | Bedrock Knowledge Base setup | CloudFormation |
| `lma-cognito-stack/` | Cognito user pool and identity pool | CloudFormation |
| `lma-llm-template-setup-stack/` | LLM prompt templates stored in DynamoDB | CloudFormation |
| `lma-chat-button-config-stack/` | Chat UI button configuration | CloudFormation |
| `lma-nova-sonic-config-stack/` | Nova Sonic voice assistant config | CloudFormation |

**Data flow:** Browser audio -> WebSocket server (Fargate) -> Amazon Transcribe -> Kinesis Data Stream -> Call Event Processor Lambda (Strands Agents SDK) -> DynamoDB + AppSync (real-time GraphQL subscriptions) -> React UI.

The Amazon Transcribe step is pluggable: when `EnableMicrovmAsr=true` and an admin moves a meeting source onto it on the Transcription Engine page, meetings are instead transcribed by an ASR MicroVM (one per meeting, one WebSocket session per audio channel), which returns text and speaker labels together. Both engines emit identical `ADD_TRANSCRIPT_SEGMENT` events, so nothing downstream of Kinesis is engine-aware. See `docs/microvm-asr.md`.

**Key source locations:**
- Lambda functions: `lma-ai-stack/source/lambda_functions/` (19 functions)
- AppSync resolvers: `lma-ai-stack/source/appsync/` (39 resolvers)
- React UI: `lma-ai-stack/source/ui/`
- Lambda layers: `lma-ai-stack/source/lambda_layers/`
- CloudFormation templates: `lma-ai-stack/deployment/`

**Meeting Assistant** uses the Strands Agents SDK with Amazon Bedrock. It supports built-in tools (transcript search, web search, document search, meeting history), MCP server integration for external tools, and Bedrock Guardrails. Customization is done via DynamoDB-stored LLM prompt templates and chat button configs.

## Documentation

Full documentation lives in `./docs/` with the master entry point at `docs/INDEX.md`. Scattered .md files in stack subdirectories are redirect stubs pointing to the consolidated docs.

## Git Workflow

- `main` branch: releases
- `develop` branch: active development (default PR target)
- Feature branches: `feature/` prefix
- Release branches: `release/` prefix

## Security Disclosure Hygiene

<EXTREMELY_IMPORTANT>
> **Mandatory for every change in this repository.**

This repository is **public**: `aws-samples` on GitHub is the primary, and the
GitLab remote is an internal mirror of it. Commit messages, PR/MR titles and
bodies, branch names, CHANGELOG entries, release notes and code comments are
all public and permanent.

**Never describe a security weakness in any of them.** This applies to the
weakness being fixed, to one being hardened against, and to anything removed
or restricted. Specifically, never write:

- What an attacker could do, or how ("any authenticated user can…", "allows
  forged tokens", "unsanitized input reaches…", "privilege escalation via…").
- The precondition or entry point that makes something reachable.
- Which versions or deployment configurations are affected.
- Severity or flaw-class vocabulary (`CRITICAL`, `RCE`, `XSS`, `CVE`,
  `vulnerability`, `exploit`, `bypass`, `injection`, `unauthenticated`) **used
  to characterise a defect in this repository**. The same words in their
  ordinary senses are fine — a cache bypass, a dependency `CVE` bump, prompt
  injection as a product concern, Teams' own lobby-bypass setting.
- Links to internal findings, threat-model IDs, scanner issue IDs, or tickets
  whose contents describe the weakness.

The same rule applies to every other artifact this repository publishes: files
under `docs/` (built into the public documentation site), `threat-modeling/`,
GitHub issue titles, comments and closing references, GitHub release notes and
tag messages, `.github/workflows/` step and job names, and suppression
rationale wherever it lives — `reason:` fields in cfn-nag/checkov metadata,
`# nosec`, `# noqa` and `# nosemgrep` comments, and `suppressionReason` entries
in `.srt/suppressions.json`. Suppression rationale is expected to explain why a
finding does not apply to the code as it stands; write it as a statement about
the code, not as an analysis of what would otherwise be reachable.

**Write the change, not the weakness.** Describe the new behaviour in neutral,
forward-looking engineering terms — name the control that is now in place,
never the gap it closes. Well-formed subjects look like:

- `feat(<scope>): require signed capability tokens for access`
- `feat(<scope>): sanitize rendered output before insertion`
- `feat(<scope>): restrict management operations to the Admin group`
- `chore(<scope>): narrow Content-Security-Policy directives`

A subject is malformed if, after the change has shipped, a reader of it still
knows what used to be possible. Anything of the shape *"fix: \<flaw class\> —
\<what it allowed\>"*, or that names a control together with the consequence of
its absence ("…, current one allows …", "…, prevents …"), fails the rule
however it is worded. Do not illustrate the rule with a specific bad example
drawn from this repository: a paired "don't write / write instead" example is
itself a disclosure.

Tests follow the same rule: name them for the invariant they assert —
`test_rejects_token_with_invalid_signature` — not for the attack that motivated
them (`test_auth_bypass`). Keep the `test_` prefix pytest requires. A name that
states what the code now guarantees is fine even if it contains a word from the
list above (`test_unauthenticated_user_rejected` is a good name).

Detailed findings, exploitability analysis and remediation write-ups belong in
an internal Taskei task — **not** in this repo. That includes `.srt/` and
`.dsr/`: their tracked files are committed to this public repository, not
internal scratch space.

Where a **security-motivated** behaviour change needs a CHANGELOG entry,
describe the new behaviour and any migration step rather than the prior
shortcoming. This does not change how ordinary bug fixes are written —
`### Fixed` entries still lead with the user-visible symptom and a sentence of
cause, as `.claude/skills/prepare-changelog.md` requires.

**This is about wording, not about withholding.** If users must act — rotate a
credential, redeploy, change a stack parameter, or stop relying on a setting —
say so plainly in the CHANGELOG and the release notes, in terms of the action
required and the behaviour that changed. Where the impact genuinely cannot be
conveyed without describing the weakness, do not water the entry down: raise it
through the channel in
[CONTRIBUTING.md](CONTRIBUTING.md#security-issue-notifications) (AWS/Amazon
Security) and publish a GitHub Security Advisory or AWS security bulletin, then
link that advisory from the CHANGELOG. An advisory published through those
channels is the one place where the weakness is described on purpose; silence
is never an acceptable substitute for one.
</EXTREMELY_IMPORTANT>

## Skill Files

Project-specific coding patterns, checklists, and review workflows live in
`.claude/skills/`. Consult the relevant skill file whenever a task touches
the corresponding domain — these conventions take precedence over generic
patterns.

| Skill File | When to Use |
|------------|-------------|
| `.claude/skills/backend-lambda.md` | Writing Python Lambda handlers in `lma-ai-stack/source/lambda_functions/` |
| `.claude/skills/frontend-ui.md` | React / Cloudscape UI changes in `lma-ai-stack/source/ui/` |
| `.claude/skills/infrastructure.md` | CloudFormation / SAM templates, nested stacks, GovCloud rules |
| `.claude/skills/code-review.md` | Pre-commit self-review checklist for your own changes |
| `.claude/skills/pr-review.md` | Reviewing a GitHub PR or GitLab MR at a URL (e.g. `review <url>`) |
| `.claude/skills/integ-tests.md` | Running end-to-end integration tests against a live deployed stack (`make integ-tests`) |

When asked to `review <PR/MR URL>`, follow `.claude/skills/pr-review.md` and
produce a structured review answering the six questions (good PR / safe /
good UX / no security issues / well documented / safe to merge).
