# Backend Lambda Development — LMA

Conventions for writing or modifying Python Lambda functions in the
Live Meeting Assistant project. Read alongside `infrastructure.md` (for the
SAM resource pattern) and `code-review.md` (for the pre-commit checklist).

## Stack & Runtime
- **Python**: 3.12
- **Framework**: AWS SAM (`AWS::Serverless-2016-10-31`)
- **Packaging**: Zip (most functions). Code lives at
  `lma-ai-stack/source/lambda_functions/<name>/` and is referenced from
  templates via `CodeUri: ../source/lambda_functions/<name>`
- **Logging library**: `aws_lambda_powertools` (NOT stdlib `logging`)
- **Linting**: Pylint + Flake8 + Bandit + mypy (line-length **100**)
- **Formatting**: Ruff (`pyproject.toml` — double quotes, py312, line-length
  100). Black is also wired through `make lint-black` for backwards-compat,
  but new code should be Ruff-formatted.

## Lambda File Structure

Every function lives in its own directory under
`lma-ai-stack/source/lambda_functions/`:

```
lma-ai-stack/source/lambda_functions/<function_name>/
├── lambda_function.py    # PREFERRED handler module name (most LMA functions)
├── index.py              # Acceptable alternative — match the SAM Handler property
├── requirements.txt      # Function-specific deps (layer-provided deps omitted)
├── __init__.py           # Empty marker
└── <subdirs>/            # Optional: business logic split out (e.g. event_processor/)
```

Representative examples (read these first when writing similar code):
- `lma-ai-stack/source/lambda_functions/async_agent_assist_orchestrator/lambda_function.py`
  — minimal handler, Powertools logger, Kinesis + Lambda invoke clients
- `lma-ai-stack/source/lambda_functions/call_event_processor/lambda_function.py`
  — async Lambda using `appsync_utils.AppsyncAioGqlClient` from the layer
- `lma-ai-stack/source/lambda_functions/bedrock_summary_lambda/`
  — Bedrock invocation pattern with aggressive retry config
- `lma-meetingassist-setup-stack/src/strands_meeting_assist_function.py`
  — Strands Agents SDK + MCP server loading, AppSync client caching

## Handler Template

```python
#!/usr/bin/env python3.12
# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""Brief description of what this Lambda does."""

import json
from os import getenv
from typing import TYPE_CHECKING, Any, Dict

import boto3

# third-party imports from Lambda layer
from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.config import Config as BotoCoreConfig

LOGGER = Logger(location="%(filename)s:%(lineno)d - %(funcName)s()")

if TYPE_CHECKING:
    from boto3 import Session as Boto3Session
    from mypy_boto3_kinesis.client import KinesisClient
    from mypy_boto3_lambda.client import LambdaClient
else:
    Boto3Session = object
    LambdaClient = object
    KinesisClient = object

# Module-level AWS clients (reused across invocations)
BOTO3_SESSION: Boto3Session = boto3.Session()
CLIENT_CONFIG = BotoCoreConfig(
    retries={"mode": "adaptive", "max_attempts": 3},
)

LAMBDA_CLIENT: LambdaClient = BOTO3_SESSION.client("lambda", config=CLIENT_CONFIG)
KINESIS_CLIENT: KinesisClient = BOTO3_SESSION.client("kinesis")

# Required environment variables (raise on missing)
APPSYNC_GRAPHQL_URL = getenv("APPSYNC_GRAPHQL_URL", "")
# Optional with default
LOG_LEVEL = getenv("LOG_LEVEL", "INFO")


@LOGGER.inject_lambda_context
def handler(event: Dict[str, Any], context: LambdaContext) -> Dict[str, Any]:
    """Lambda entry point."""
    LOGGER.info("Processing event", extra={"event": event})
    # ... business logic ...
    return {"statusCode": 200, "body": json.dumps({"ok": True})}
```

## Key Patterns (MUST follow)

1. **Handler name**: match the SAM template's `Handler:` property exactly.
   Most LMA functions use `Handler: lambda_function.handler` or
   `lambda_function.lambda_handler`. A few utility functions use
   `index.handler`. Don't rename without updating the template.
2. **Logging**: `aws_lambda_powertools.Logger` at module level with the
   location format string above. Use `LOGGER.info(msg, extra={...})` for
   structured fields. `LOGGER.exception(msg)` to log a stack trace from
   inside an `except`.
3. **Powertools context injection**: decorate the handler with
   `@LOGGER.inject_lambda_context` so cold-start, request id, and function
   metadata are added to log records automatically.
4. **AWS clients**: initialize at module level, **outside** the handler, so
   they are reused across invocations. Always pass a `BotoCoreConfig` with
   adaptive retry — `max_attempts=3` is the project default.
5. **Bedrock retry config**: Bedrock clients (especially when using
   cross-region inference profiles) need a much higher retry budget:
   ```python
   BEDROCK_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 50})
   bedrock_runtime = BOTO3_SESSION.client("bedrock-runtime", config=BEDROCK_CONFIG)
   ```
6. **X-Ray**: enabled at the SAM level via `Tracing: Active` /
   `TracingConfig: Mode: Active`. Don't call `aws_xray_sdk.core.patch_all()`
   in code — it isn't the LMA convention.
7. **Type stubs**: use `TYPE_CHECKING` guards for `mypy_boto3_*` types so
   they're available to mypy but don't fail at runtime if the stubs aren't
   installed in the layer.
8. **Environment variables**: `os.environ["REQUIRED"]` (raises `KeyError`
   on missing) for required vars; `os.environ.get("OPTIONAL", "default")` or
   `os.getenv(...)` for optional. Cast booleans explicitly:
   `getenv("FLAG", "false").lower() == "true"`.
9. **Error handling**: explicit `botocore.exceptions.ClientError` catches.
   Use `LOGGER.exception(...)` from inside `except` (not bare `except:`).
10. **Copyright header**: first 4 lines of every Python file:
    ```python
    #!/usr/bin/env python3.12
    # Copyright (c) 2025 Amazon.com
    # This file is licensed under the MIT License.
    # See the LICENSE file in the project root for full license information.
    ```

## Return Patterns
- **Direct invocation / API-style**: `{"statusCode": 200|400|500, "body": json.dumps(...)}`
- **Kinesis / SQS / EventBridge sources**: return values are typically ignored;
  raise on failure so the source applies its own retry/DLQ semantics
- **AppSync resolver Lambdas**: return the GraphQL field shape directly

## Lambda Layer (`lma-ai-stack/source/lambda_layers/transcript_enrichment_layer/`)

Reuse — don't reimplement — utilities from the shared layer:

| Module | Purpose |
|---|---|
| `appsync_utils` | `AppsyncAioGqlClient`, `AppsyncGqlClient` — AppSync GraphQL clients with SigV4 auth |
| `eventprocessor_utils` | `get_transcription_ttl`, sentiment analysis helpers, JWT parsing |
| `graphql_helpers` | Query field builders for AppSync mutations |

Import from the layer with the `# pylint: disable=import-error` /
`# pylint: enable=import-error` pattern used in existing code (the layer
isn't on the path during local lint runs).

## Strands Agents SDK

The Meeting Assistant uses Strands Agents (`strands-agents` package) +
Amazon Bedrock + MCP servers. Canonical example:
`lma-meetingassist-setup-stack/src/strands_meeting_assist_function.py`.

Key conventions:
- Cache the AppSync client at module scope to avoid re-authenticating per
  invocation.
- Load MCP servers dynamically from configuration in DynamoDB.
- Wrap tools so the agent emits "thinking" steps to the AppSync subscription
  for the UI to render.
- Bedrock Guardrails are configured per agent.

## Lint Configuration Highlights

`lma-ai-stack/.pylintrc`:
- `max-line-length=100`
- `max-args=5`, `max-locals=15`
- `fail-under=5.0` (minimum score to pass)
- Min similarity: 9 lines

`lma-ai-stack/.flake8`:
- `max-line-length = 100`
- `extend-ignore = E203,E501,W293,F401,F811,F841,F541,E711,E712,E722`

`lma-ai-stack/pyproject.toml` (Ruff):
- `line-length = 100`, `target-version = "py312"`
- `select = ["E", "F", "I"]`, `ignore = ["E501"]`
- `quote-style = "double"`

## Adding a New Lambda

1. Create directory `lma-ai-stack/source/lambda_functions/<your_function>/`
2. Add `lambda_function.py` (handler), `requirements.txt`, `__init__.py`
3. Add the resource to the relevant template
   (`lma-ai-stack/deployment/lma-ai-stack.yaml` or a nested template),
   following the canonical pattern:
   - Dedicated `AWS::Logs::LogGroup` with KMS encryption
   - `LoggingConfig.LogGroup` referencing it
   - cfn-nag `Metadata` block with `reason:` for every suppressed rule
   - `PermissionsBoundary` `!If` conditional on the IAM role
   - All ARNs use `!Sub "arn:${AWS::Partition}:..."`
   - All AWS endpoints use `${AWS::URLSuffix}`
4. See `infrastructure.md` for the full template snippet.

## Commands

```bash
cd lma-ai-stack
make lint-pylint           # Pylint
make lint-mypy             # mypy type checking
make lint-bandit           # security scan
make lint-python           # pylint + flake8 + mypy + black + bandit
make test                  # SAM unit tests + local invokes

# AWS profile MUST be default for these
AWS_PROFILE=default make build
AWS_PROFILE=default make test-local-invoke-default
```
