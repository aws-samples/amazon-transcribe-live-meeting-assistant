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

### Exceptions

All exceptions inherit from `LMAError`:

```python
from lma_sdk.exceptions import (
    LMAError,                  # Base exception
    LMAConfigurationError,     # Invalid SDK configuration
    LMAStackError,             # CloudFormation operation failure
    LMAPublishError,           # Artifact publish failure
    LMAResourceNotFoundError,  # AWS resource not found
    LMAValidationError,        # Input validation failure
    LMAAuthError,              # Authentication failure
    LMATimeoutError,           # Operation timeout
)
```

## Architecture

```
lma_sdk/
├── __init__.py          # Public API: LMAClient, exceptions
├── client.py            # LMAClient — main entry point
├── exceptions.py        # Exception hierarchy
├── models/              # Pydantic data models
│   ├── stack.py         # StackInfo, StackOutput, etc.
│   └── publish.py       # PublishResult, PublishConfig, etc.
├── operations/          # Thin namespace wrappers (public API)
│   ├── stack.py         # StackOperations
│   └── publish.py       # PublishOperations
└── _core/               # Core implementations (internal)
    ├── stack.py          # StackManager
    └── publish.py        # Publisher, prerequisites, change detection
```

**Design principles** (same as IDP SDK):
- **Operations** are thin wrappers that delegate to `_core` implementations
- **Models** use Pydantic for validation and serialization
- **Client** lazy-loads operation namespaces to avoid circular imports
- **Core** modules handle all AWS API calls via the client's boto3 session

## Future Phases

- **Phase 2**: Meeting operations (`client.meeting.list()`, `client.transcript.get()`)
- **Phase 3**: Virtual Participant (`client.vp.join()`, `client.vp.leave()`)
- **Phase 4**: Audio streaming, assistant/chat, MCP server integration
