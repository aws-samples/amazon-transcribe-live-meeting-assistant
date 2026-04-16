---
title: "LMA CLI — Command-Line Interface for AWS Live Meeting Assistant"
---

# LMA CLI — Command-Line Interface for AWS Live Meeting Assistant

## Overview

The **LMA CLI** (`lma-cli`) provides a rich command-line interface for building, deploying, and managing AWS Live Meeting Assistant. It uses the [LMA SDK](lma-sdk.md) under the hood and provides beautiful terminal output via [Rich](https://rich.readthedocs.io/).

## Installation

```bash
# From the project root — install both SDK and CLI
pip install -e lib/lma_sdk
pip install -e lib/lma_cli_pkg

# Or use make
make setup-cli
```

After installation, the `lma` command (and alias `lma-cli`) is available:

```bash
lma --help
```

## Commands

### `lma publish` — Build & Upload Artifacts

Packages all LMA sub-stacks, uploads artifacts to S3, and generates a deployable CloudFormation template URL.

```bash
# Standard build and publish (bucket auto-generated from AWS account ID)
lma publish

# With custom bucket and prefix, and region
lma publish --bucket-basename my-artifacts --prefix lma --region us-east-1

# Force full rebuild (skip change detection)
lma publish --force

# Public artifacts (for shared deployments)
lma publish --public
```

**Options:**

| Option | Description |
|--------|-------------|
| `--source-dir PATH` | Path to LMA project root (default: `.`) |
| `--bucket-basename TEXT` | S3 bucket basename (auto-generated from account ID if omitted) |
| `--prefix TEXT` | S3 key prefix (default: `lma`) |
| `--region TEXT` | AWS region (default: from AWS CLI profile) |
| `--public` | Make artifacts publicly readable |
| `--force` | Force full rebuild (delete checksums) |
| `--version TEXT` | Override version string |
| `--no-validate` | Skip CloudFormation template validation |
| `-v, --verbose` | Enable verbose build output |

**Features:**
- Rich progress output with per-stack status
- SHA256-based change detection (skips unchanged stacks)
- Colourized summary panel with template URL and console URL

### `lma deploy` — Deploy/Update Stack

Deploy or update the LMA CloudFormation stack. If no template source is specified, deploys from the public published LMA template for the current region.

```bash
# Deploy from public template (simplest — new stack)
lma deploy --stack-name MyLMA --admin-email user@example.com --wait

# Deploy from public template (create new stack or update existing stack)
lma deploy --stack-name MyLMA --wait

# Deploy from local code (build, publish, then deploy)
lma deploy --stack-name MyLMA --from-code . --admin-email user@example.com --wait

# Deploy from local code with custom bucket
lma deploy --stack-name MyLMA --from-code . --bucket-basename my-artifacts --wait

# Deploy from specific S3 template URL
lma deploy --stack-name MyLMA --template-url https://s3.us-east-1.amazonaws.com/bucket/lma-main.yaml

# Deploy with parameter overrides
lma deploy --stack-name MyLMA -p AdminEmail=admin@example.com -p InstallDemoMode=true --wait

# Deploy from local template file
lma deploy --stack-name MyLMA --template-file /tmp/lma-main.yaml --wait
```

**Options:**

| Option | Description |
|--------|-------------|
| `--stack-name TEXT` | CloudFormation stack name (default: LMA) |
| `--admin-email TEXT` | Admin user email (required for new stacks) |
| `--from-code DIRECTORY` | Build and publish from local source before deploying |
| `--template-url TEXT` | S3 URL for CloudFormation template |
| `--template-file FILE` | Path to local CloudFormation template file |
| `-p, --parameter KEY=VALUE` | Parameter override (can be repeated) |
| `--wait` | Wait for stack operation to complete with event streaming |
| `--no-rollback` | Disable rollback on stack creation failure |
| `--role-arn TEXT` | CloudFormation service role ARN |
| `--timeout INTEGER` | Max wait time in minutes (default: 120) |
| `--bucket-basename TEXT` | S3 bucket basename (used with `--from-code`) |
| `--prefix TEXT` | S3 key prefix (default: lma, used with `--from-code`) |
| `--public` | Make S3 artifacts publicly readable (used with `--from-code`) |
| `--clean-build` | Force full rebuild (used with `--from-code`) |
| `--no-validate-template` | Skip CloudFormation template validation (used with `--from-code`) |

**Template Resolution:**
- If `--from-code` is specified, builds and publishes artifacts first, then deploys the resulting template
- If `--template-url` is specified, uses that URL directly
- If `--template-file` is specified, uses the local file
- If none specified, auto-selects the public LMA template for the current region:
  - `us-east-1`, `us-west-2`, `ap-southeast-2` are supported

**Smart Features:**
- Auto-detects if the stack has an operation in progress and switches to monitoring mode
- Streams CloudFormation events in real-time when `--wait` is used
- Validates that `--admin-email` is provided for new stack creation
- Shows next steps and important outputs upon completion

### `lma status` — Stack Status

Show current CloudFormation stack status, parameters, and outputs.

```bash
lma status
lma status --stack-name MyLMA
```

### `lma outputs` — Stack Outputs

Show stack outputs (CloudFront URL, AppSync endpoint, Cognito pool, etc.).

```bash
lma outputs
lma outputs --json
lma outputs --stack-name MyLMA
```

### `lma delete` — Delete Stack

Delete the LMA stack and all its resources.

```bash
lma delete                    # Interactive confirmation
lma delete --yes              # Skip confirmation
lma delete --stack-name MyLMA --yes
```

### `lma logs` — CloudWatch Logs

View CloudWatch logs for Lambda functions and services.

```bash
# List available log groups
lma logs --list

# View logs from a specific group
lma logs /LMA/lambda/FetchTranscript

# Partial name matching
lma logs FetchTranscript --since 60 --limit 200
```

### `lma check-prereqs` — Prerequisite Check

Verify all publish prerequisites are installed (Docker, SAM CLI, Node.js, etc.).

```bash
lma check-prereqs
```

### `lma list-stacks` — List Sub-Stacks

List all publishable LMA sub-stacks with their package types.

```bash
lma list-stacks
```

## Global Options

```bash
lma --region us-west-2 status                  # Override AWS region
lma --profile prod outputs                     # Use specific AWS profile
lma --stack-name MyLMA status                  # Override stack name
lma -v publish --source-dir . --region us-east-1  # Verbose logging
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `LMA_STACK_NAME` | CloudFormation stack name | `LMA` |
| `AWS_DEFAULT_REGION` | AWS region | `us-east-1` |
| `AWS_PROFILE` | AWS CLI profile | (default chain) |

## Typical Workflows

### Quick Deploy (from public template)

```bash
# Deploy the latest published LMA — auto-selects template for your region
lma deploy --stack-name MyLMA --admin-email user@example.com --wait
```

### Developer Workflow (from local code)

```bash
# 1. Check prerequisites
lma check-prereqs

# 2. Build and deploy from local source code in one step
lma deploy --stack-name MyLMA --from-code . --admin-email user@example.com --wait

# 3. Get the CloudFront URL
lma outputs

# 4. View logs
lma logs --list
lma logs FetchTranscript --since 30
```

### Manual Publish + Deploy

```bash
# 1. Publish artifacts to S3
lma publish --source-dir . --region us-east-1

# 2. Deploy using the published template URL
lma deploy --stack-name MyLMA --template-url <template-url> --wait

# 3. Monitor status
lma status
```

### Monitor an In-Progress Deployment

```bash
# If a deploy is already running, this auto-detects and monitors it
lma deploy --stack-name MyLMA --wait
```

## Future Commands (Phase 2+)

| Command | Description | Phase |
|---------|-------------|-------|
| `lma meetings list` | List meetings | 2 |
| `lma meetings get <id>` | Get meeting details | 2 |
| `lma transcript get <id>` | Get transcript | 2 |
| `lma join <meeting-url>` | Join meeting via Virtual Participant | 3 |
| `lma leave <meeting-id>` | Leave meeting | 3 |
| `lma ask "<question>"` | Ask meeting assistant | 4 |
| `lma stream start` | Stream audio | 4 |

## Architecture

The CLI is built with [Click](https://click.palletsprojects.com/) and [Rich](https://rich.readthedocs.io/), following the same patterns as the IDP CLI:

```
lma_cli/
├── cli.py           # Main entry point, Click group
├── formatters.py    # Rich output helpers (tables, panels, colours)
└── commands/
    ├── publish.py   # publish, check-prereqs, list-stacks
    └── stack.py     # status, outputs, deploy, delete, logs
```

All AWS operations go through the LMA SDK — the CLI only handles UX/formatting.
