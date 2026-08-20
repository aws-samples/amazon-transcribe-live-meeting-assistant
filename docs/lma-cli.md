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
  - `us-east-1`, `us-west-2`, `ap-northeast-1`, `eu-west-1` are supported

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

### `lma vp` — Virtual Participant Operations

Create, fetch, end, and list Virtual Participants directly from the CLI. These commands talk to the LMA AppSync API and the VP scheduler Step Function via SigV4-signed AWS calls (no Cognito login required — your AWS credentials are used).

```bash
# Create + launch a VP, waiting until it leaves INITIALIZING
lma vp create --name "Weekly sync" --platform ZOOM --id 1234567890

# Create with a password and a custom display name; emit JSON
lma vp create --name "Standup" --platform TEAMS --id 99999 \
              --password "abc" --user-name "lma-bot" --json

# Get a single VP row
lma vp get --id <vp-id>

# End a running VP
lma vp end --id <vp-id> --reason "Manual cleanup"

# List all VPs visible to the calling IAM principal
lma vp list
lma vp list --json
```

**Common options for every `lma vp` subcommand:**

| Option | Description |
|--------|-------------|
| `--stack-name TEXT` | CloudFormation stack name (env: `LMA_STACK_NAME`) |
| `--region TEXT` | AWS region (env: `AWS_DEFAULT_REGION`) |
| `--json` | Emit JSON instead of formatted text |

**`lma vp create` extra options:**

| Option | Description |
|--------|-------------|
| `--name TEXT` | Meeting name (required) |
| `--platform [ZOOM\|TEAMS\|CHIME\|WEBEX]` | Meeting platform (required, case-insensitive) |
| `--id TEXT` | Meeting ID (required) |
| `--password TEXT` | Meeting password (default: empty) |
| `--user-name TEXT` | Display name the scribe reports (default: `lma-cli@lma`) |
| `--wait / --no-wait` | Poll until the VP leaves `INITIALIZING` (default: `--wait`) |
| `--timeout FLOAT` | Max seconds to wait for launch (default: 120) |

> The underlying SDK methods are documented under [`client.vp`](lma-sdk.md#clientvp--virtual-participant-operations).

### `lma load` — Load Simulator (plugin)

The `lma load` subcommand tree is provided by the **LMA Load Simulator** package (`utilities/load-simulator/`). It registers itself with the `lma` CLI through the `lma_cli.plugins` entry-point group, so once both packages are installed alongside each other it appears as a first-class subcommand of `lma`.

```bash
# Install the load simulator (in addition to lma-sdk + lma-cli)
pip install -e utilities/load-simulator

# Or with optional driver extras (websocket audio resampling, upload pipeline)
pip install -e 'utilities/load-simulator[all]'

# Verify
lma load --help
```

Available scenarios (each tagged with a `--run-id` for deterministic teardown):

| Subcommand | Purpose |
|------------|---------|
| `lma load concurrent` | Drive N simultaneous meetings via `--driver kinesis\|upload\|websocket\|vp` |
| `lma load backfill` | Fabricate N historical meetings spread over the last Y days (synthetic or real Cognito users for RBAC-at-scale testing) |
| `lma load rbac` | Provision N synthetic Cognito users + latency-sweep `listCalls` / `getCallCount` |
| `lma load cleanup` | Delete every synthetic resource (meetings, VPs, users, S3 orphans) for a given `--target-run-id` (or `*`) |
| `lma load stack-info` | Print the CloudFormation resources the simulator resolved — handy for debugging |

The simulator can also be invoked standalone as `lma-load <subcommand>` if you have not installed `lma-cli`.

For the full reference (driver matrix, cost/quota guardrails, observability dashboard, synthetic-user provisioning, safety rails, and example fixtures), see the [LMA Load Simulator README](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/blob/main/utilities/load-simulator/README.md).

## CLI Plugins

The `lma` CLI auto-discovers any installed Python package that declares a Click command under the `lma_cli.plugins` entry-point group. This is how `lma load ...` is contributed by the Load Simulator package; the same mechanism can be used for any third-party command tree.

```toml
# pyproject.toml of your plugin package
[project.entry-points."lma_cli.plugins"]
mything = "my_pkg.cli:my_command"   # registers `lma mything ...`
```

Plugin failures are logged but never crash the CLI — if a plugin fails to import, its commands simply don't appear under `lma --help`.

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
| `lma ask "<question>"` | Ask meeting assistant | 4 |
| `lma stream start` | Stream audio | 4 |

> Virtual Participant join/leave/list is already shipped — see [`lma vp`](#lma-vp--virtual-participant-operations). Load testing is provided by the [`lma load`](#lma-load--load-simulator-plugin) plugin.

## Architecture

The CLI is built with [Click](https://click.palletsprojects.com/) and [Rich](https://rich.readthedocs.io/), following the same patterns as the IDP CLI:

```
lma_cli/
├── cli.py           # Main entry point, Click group + plugin loader
├── formatters.py    # Rich output helpers (tables, panels, colours)
└── commands/
    ├── publish.py   # publish, check-prereqs, list-stacks
    ├── stack.py     # status, outputs, deploy, delete, logs
    └── vp.py        # vp create / get / end / list
```

External plugins (e.g. `utilities/load-simulator/lma_load/cli.py`, which contributes `lma load ...`) are auto-registered via the `lma_cli.plugins` entry-point group — see [CLI Plugins](#cli-plugins).

All AWS operations go through the LMA SDK — the CLI only handles UX/formatting.

