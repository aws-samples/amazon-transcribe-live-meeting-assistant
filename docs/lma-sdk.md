---
title: "LMA SDK — Python SDK for AWS Live Meeting Assistant"
---

# LMA SDK — Python SDK for AWS Live Meeting Assistant

## Overview

The **LMA SDK** (`lma-sdk`) is a Python SDK that provides programmatic access to AWS Live Meeting Assistant operations. It is the shared foundation used by both the [LMA CLI](lma-cli.md) and (eventually) the LMA MCP server.

The SDK follows the same architecture as the [IDP SDK](https://github.com/aws-samples/generative-ai-idp-on-aws), with a client → operations → core layered design.

## Installation

```bash
# From the project root
pip install -e lib/lma_sdk

# Or with dev dependencies
pip install -e "lib/lma_sdk[dev]"
```

## Quick Start

```python
from lma_sdk import LMAClient

# Initialize client (uses env vars or defaults)
client = LMAClient(stack_name="LMA", region="us-east-1")

# Stack operations
status = client.stack.status()
if status.exists:
    print(f"Stack: {status.stack.stack_name}")
    print(f"Status: {status.stack.status}")

# Get stack outputs
outputs = client.stack.outputs()
for key, output in outputs.items():
    print(f"{key}: {output.value}")

# Publish artifacts to S3
result = client.publish.publish(
    bucket_basename="my-lma-artifacts",
    prefix="lma",
    region="us-east-1",
)
print(f"Template URL: {result.template_url}")
print(f"Console URL: {result.console_url}")
```

## Configuration

The client reads configuration from multiple sources (in priority order):

1. **Explicit parameters**: `LMAClient(stack_name="...", region="...")`
2. **Environment variables**: `LMA_STACK_NAME`, `AWS_DEFAULT_REGION`, `AWS_PROFILE`
3. **Defaults**: stack_name=`"LMA"`, region=`"us-east-1"`

```python
# Using environment variables
import os
os.environ["LMA_STACK_NAME"] = "MyLMA"
os.environ["AWS_DEFAULT_REGION"] = "eu-west-1"
client = LMAClient()  # Uses env vars

# Using a specific AWS profile
client = LMAClient(profile="my-profile")

# Using a pre-configured boto3 session
import boto3
session = boto3.Session(profile_name="prod", region_name="us-west-2")
client = LMAClient(session=session)
```

## API Reference

### `LMAClient`

The main entry point. Provides access to operation namespaces.

```python
client = LMAClient(
    stack_name="LMA",           # CloudFormation stack name
    region="us-east-1",         # AWS region
    profile="default",          # AWS CLI profile (optional)
    session=None,               # Pre-configured boto3.Session (optional)
)
```

### `client.stack` — Stack Operations

| Method | Description | Returns |
|--------|-------------|---------|
| `status(stack_name=None)` | Get stack status | `StackStatusResult` |
| `outputs(stack_name=None)` | Get stack outputs | `dict[str, StackOutput]` |
| `deploy(template_url=..., parameters=...)` | Deploy/update stack | `StackDeployResult` |
| `delete(stack_name=None, wait=True)` | Delete stack | `StackDeleteResult` |
| `get_log_groups(stack_name=None)` | List CloudWatch log groups | `list[str]` |
| `tail_logs(log_group, since_minutes=15)` | Get recent log entries | `list[LogEntry]` |

### `client.publish` — Publish Operations

| Method | Description | Returns |
|--------|-------------|---------|
| `publish(bucket_basename, prefix, region, ...)` | Publish all artifacts to S3 | `PublishResult` |
| `available_stacks()` | List publishable stack names | `list[str]` |
| `check_prerequisites()` | Check publish prerequisites | `list[str]` (errors) |

### `client.appsync` — AppSync GraphQL Helper

A SigV4-signed GraphQL helper for the LMA AppSync API. Uses the calling
IAM principal (your AWS credentials) — no Cognito login is required, but
the principal must be authorized for the LMA AppSync API.

| Method / property | Description | Returns |
|-------------------|-------------|---------|
| `graphql_url` (property) | Auto-resolved AppSync URL from CloudFormation outputs (`GraphqlApiUrl`, `AppSyncGraphqlUrl`, or the `LocalUITestingEnv` blob) | `str` |
| `graphql(query, variables=None, operation_name=None, timeout_s=30.0)` | Execute a GraphQL query/mutation against the LMA AppSync API. Returns the `data` portion. | `dict` |

```python
data = client.appsync.graphql(
    query="query Q { listCalls { CallId } }",
)
print(data["listCalls"])
```

Raises `LMAAppSyncError` on GraphQL errors / non-2xx, and `LMAConfigurationError` if the URL or AWS credentials cannot be resolved.

### `client.vp` — Virtual Participant Operations

Programmatic CRUD + launch flow for LMA Virtual Participants. Mirrors what
the LMA Web UI's `EmbedVpLoader` component does, but using SigV4 and the
Python SDK (no Cognito login required). The lifecycle is:

1. `createVirtualParticipant` AppSync mutation writes a row in
   `VirtualParticipantTable` with status `INITIALIZING`.
2. `stepfunctions:StartSyncExecution` is invoked on the LMA VP scheduler
   Express state machine to place an ECS task that joins the meeting.
3. (Optionally) `getVirtualParticipant` is polled until the scribe
   reports the first non-`INITIALIZING` status update — the most reliable
   way to detect ECS placement failures.

| Method | Description | Returns |
|--------|-------------|---------|
| `create(meeting_name, platform, meeting_id, meeting_password="", user_name="loadtest@lma", wait=True, timeout_s=120.0, poll_interval_s=2.0)` | Create + launch a VP and (by default) wait for it to leave `INITIALIZING` | `VpLaunchResult` |
| `get(vp_id)` | Fetch a VP row by id | `VpRow` |
| `end(vp_id, reason="SDK requested termination", ended_by="SDK")` | End (stop) a running VP | `VpRow` |
| `list()` | List VPs visible to the calling IAM principal | `list[VpRow]` |
| `wait_for_launch(vp_id, timeout_s=120.0, poll_interval_s=2.0)` | Poll until status leaves `INITIALIZING` | `VpRow` |
| `scheduler_state_machine_arn` (property) | Resolved ARN of the VP scheduler state machine (auto-derived from the stack) | `str` |

```python
result = client.vp.create(
    meeting_name="Weekly sync",
    platform="ZOOM",
    meeting_id="1234567890",
    meeting_password="",
    user_name="lma-bot",
    wait=True,
)
print(result.id, result.status, result.call_id, result.elapsed_ms)

# Later — list, fetch, end
for row in client.vp.list():
    print(row.id, row.status)

client.vp.end(result.id, reason="Test complete")
```

Raises `LMAVirtualParticipantError` on any failure (AppSync error, SFN
`FAILED`, placement failure, or — when `wait=True` — failure to launch
within `timeout_s`). Failed launches are best-effort cleaned up to avoid
orphan `INITIALIZING` rows in the UI.

### Data Models

All models are Pydantic `BaseModel` subclasses defined in `lma_sdk.models`:

- **`StackInfo`** — Stack name, status, outputs, parameters, tags
- **`StackOutput`** — Output key, value, description
- **`StackStatusResult`** — success, exists, stack info
- **`StackDeployResult`** — success, status, console URL, outputs
- **`StackDeleteResult`** — success, message
- **`LogEntry`** — timestamp, message, log stream
- **`PublishResult`** — success, per-stack results, template URL, console URL
- **`StackPublishResult`** — per-stack success, skipped, duration
- **`PublishConfig`** — bucket, prefix, region, stacks, force
- **`VpRow`** — Virtual Participant row from AppSync (id, meeting fields, status, CallId, owner, VNC info)
- **`VpLaunchResult`** — Result of `client.vp.create(...)` (id, status, call_id, elapsed_ms, sfn_execution_arn, …)
- **`VpStatus`** — Enum of VP lifecycle states (`INITIALIZING`, `JOINING`, `JOINED`, `ACTIVE`, `COMPLETED`, `ENDED`, `FAILED`, …)


### Exceptions

All exceptions inherit from `LMAError`:

```python
from lma_sdk.exceptions import (
    LMAError,                     # Base exception
    LMAConfigurationError,        # Invalid SDK configuration
    LMAStackError,                # CloudFormation operation failure
    LMAPublishError,              # Artifact publish failure
    LMAResourceNotFoundError,     # AWS resource not found
    LMAValidationError,           # Input validation failure
    LMAAuthError,                 # Authentication failure
    LMATimeoutError,              # Operation timeout
    LMAAppSyncError,              # AppSync GraphQL call failure
    LMAVirtualParticipantError,   # Virtual Participant operation failure
)
```

## Architecture

```
lma_sdk/
├── __init__.py                  # Public API: LMAClient, exceptions
├── client.py                    # LMAClient — main entry point
├── exceptions.py                # Exception hierarchy
├── models/                      # Pydantic data models
│   ├── stack.py                 # StackInfo, StackOutput, etc.
│   ├── publish.py               # PublishResult, PublishConfig, etc.
│   └── virtual_participant.py   # VpRow, VpLaunchResult, VpStatus
├── operations/                  # Thin namespace wrappers (public API)
│   ├── stack.py                 # StackOperations
│   ├── publish.py               # PublishOperations
│   ├── appsync.py               # AppSyncOperations (SigV4 GraphQL helper)
│   └── virtual_participant.py   # VirtualParticipantOperations
└── _core/                       # Core implementations (internal)
    ├── stack.py                 # StackManager
    └── publish.py               # Publisher, prerequisites, change detection
```

**Design principles** (same as IDP SDK):
- **Operations** are thin wrappers that delegate to `_core` implementations
- **Models** use Pydantic for validation and serialization
- **Client** lazy-loads operation namespaces to avoid circular imports
- **Core** modules handle all AWS API calls via the client's boto3 session

## Future Phases

- **Phase 2**: Meeting operations (`client.meeting.list()`, `client.transcript.get()`)
- **Phase 4**: Audio streaming, assistant/chat, MCP server integration

> Virtual Participant (`client.vp.create / get / end / list`) is shipped — see [`client.vp`](#clientvp--virtual-participant-operations). The companion [LMA Load Simulator](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/blob/main/utilities/load-simulator/README.md) is built on top of the SDK and is exposed as `lma load …` via the [CLI plugin mechanism](lma-cli.md#cli-plugins).

