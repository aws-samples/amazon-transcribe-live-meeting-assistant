# Code Review Checklist — LMA (pre-commit self-review)

Use this skill before returning a change to the user. It is the pre-commit
companion to `pr-review.md` (which reviews someone else's PR/MR).

## Pre-Submit Commands

LMA does not have a single root `make lint` / `make test`. Run the targets in
the stack(s) you touched.

**AI stack (Python Lambdas + UI + main CFN templates) — `lma-ai-stack/`:**

```bash
cd lma-ai-stack
make lint-pylint        # Pylint (line-length 100, fail-under 5.0)
make lint-mypy          # mypy type checking
make lint-bandit        # Python security scanning
make lint-cfn-lint      # cfn-lint
make lint-yamllint      # YAML lint
make lint-validate      # SAM template validation
make lint-eslint        # JS/TS lint (UI + Lambda JS)
make lint-prettier      # JS/TS format check
make lint-state-machines# Step Functions linting
make lint               # All of the above (cfn + python + js + state machines)
make test               # SAM unit + local-invoke tests
```

**WebSocket transcriber — `lma-websocket-transcriber-stack/source/app/`:**

```bash
cd lma-websocket-transcriber-stack/source/app
npm install && npm run build   # TypeScript build
npm test                       # jest tests
```

**Virtual Participant — `lma-virtual-participant-stack/backend/`:**

```bash
cd lma-virtual-participant-stack/backend
npm install && npm run build   # TypeScript build
```

**UI dev/build — `lma-ai-stack/source/ui/`:**

```bash
cd lma-ai-stack/source/ui
npm install && npm start    # Vite dev server
npm run build               # Production build
npm test                    # vitest
```

**AWS profile:** Always export `AWS_PROFILE=default` for any build / deploy /
test command that touches AWS APIs. Other profiles (e.g. `bedrock`) point at
unrelated accounts and will fail with `AccessDenied`.

## Python Lambda Review (`lma-ai-stack/source/lambda_functions/`)

### Style & Formatting
- [ ] Pylint passes (`make lint-pylint`) — line length **100**, `fail-under=5.0`,
      `max-args=5`, `max-locals=15` (see `lma-ai-stack/.pylintrc`)
- [ ] Flake8 passes (`make lint-flake8`) — same 100-char limit, ignores
      `E203,E501,W293,F401,F811,F841,F541,E711,E712,E722` (see `.flake8`)
- [ ] Ruff format applied (double quotes, py312, line-length 100) — see
      `lma-ai-stack/pyproject.toml`
- [ ] mypy passes (`make lint-mypy`)
- [ ] Bandit passes (`make lint-bandit`)
- [ ] Copyright header present at top of every Python file:
  ```python
  #!/usr/bin/env python3.12
  # Copyright (c) 2025 Amazon.com
  # This file is licensed under the MIT License.
  # See the LICENSE file in the project root for full license information.
  ```

### Lambda Conventions
- [ ] Handler signature `handler(event, context: LambdaContext)` (or
      `lambda_handler(...)` when matched by the SAM `Handler:` property) in
      `lambda_function.py` (preferred) or `index.py`
- [ ] Logging via `aws_lambda_powertools.Logger`:
  ```python
  from aws_lambda_powertools import Logger
  from aws_lambda_powertools.utilities.typing import LambdaContext
  LOGGER = Logger(location="%(filename)s:%(lineno)d - %(funcName)s()")
  ```
- [ ] Handler decorated with `@LOGGER.inject_lambda_context` where useful
- [ ] AWS clients initialized at **module level** with retry config:
  ```python
  from botocore.config import Config as BotoCoreConfig
  CLIENT_CONFIG = BotoCoreConfig(retries={"mode": "adaptive", "max_attempts": 3})
  ```
- [ ] Bedrock clients use the more aggressive retry profile
      (`max_attempts=50, mode="adaptive"`) — required for cross-region
      inference profile reliability
- [ ] X-Ray tracing enabled via SAM template (`Tracing: Active` /
      `TracingConfig: Mode: Active`); manual `patch_all()` in code is **not**
      the LMA convention
- [ ] Type hints on function signatures; `TYPE_CHECKING` guard for
      `mypy_boto3_*` stubs that aren't available at runtime
- [ ] Explicit `botocore.exceptions.ClientError` catches; use
      `LOGGER.exception(...)` or `LOGGER.error(..., exc_info=True)` when
      re-raising
- [ ] No hardcoded AWS account IDs or regions
- [ ] Shared utilities reused from the Lambda layer at
      `lma-ai-stack/source/lambda_layers/transcript_enrichment_layer/` —
      `appsync_utils`, `eventprocessor_utils`, `graphql_helpers`. Don't
      duplicate code that already lives there.

## Frontend UI Review (`lma-ai-stack/source/ui/`)

### Style & Formatting
- [ ] ESLint passes (`make lint-eslint` or
      `cd lma-ai-stack/source/ui && npx eslint .`) — config:
      airbnb + plugin:react/recommended + plugin:prettier/recommended
- [ ] Prettier applied (printWidth **120**, single quotes, trailing
      commas) — see `lma-ai-stack/source/ui/.prettierrc`
- [ ] Max line length 120; unix linebreaks

### Component Standards
- [ ] **Arrow function components** (enforced by ESLint
      `react/function-component-definition: namedComponents = arrow-function`)
- [ ] Cloudscape Design System components (`@cloudscape-design/components`)
- [ ] `ConsoleLogger` from `aws-amplify/utils` for logging in production code
      (`no-console` is currently `off` in eslintrc, but new code should still
      prefer `ConsoleLogger`)
- [ ] React Context for state (`AppContext`, `CallsContext`, `SettingsContext`
      under `src/contexts/`); no Redux
- [ ] **New** custom hooks in kebab-case (`use-aws-config.js`,
      `use-current-session-creds.js`); existing hooks keep their current name

### GraphQL / AppSync
- [ ] If the AppSync schema
      (`lma-ai-stack/source/appsync/schema.graphql`) changed, the
      corresponding resolvers under `lma-ai-stack/source/appsync/` and the
      UI client operations in `lma-ai-stack/source/ui/src/graphql/` are
      updated together
- [ ] `mutations.js` is auto-generated and starts with `/* eslint-disable */`
      — don't reformat it; if you need a new mutation, regenerate
- [ ] Operation files under `src/graphql/queries/*.js` keep the copyright
      header

## Infrastructure Review (CloudFormation / SAM)

### GovCloud & Partition Compatibility
- [ ] No new hardcoded `arn:aws:` literals — use
      `!Sub "arn:${AWS::Partition}:..."` (existing `AllowedPattern` regex
      strings like `^(|arn:aws:lambda:.*)$` in parameter validation are
      acceptable and unchanged)
- [ ] No new hardcoded `amazonaws.com` — use `${AWS::URLSuffix}` (e.g.
      `!Sub "states.${AWS::URLSuffix}"`)
- [ ] `make lint-cfn-lint` and `make lint-yamllint` pass
- [ ] `make lint-validate` (SAM validate) passes

### Lambda Resource Pattern (canonical example:
`lma-ai-stack/deployment/lma-ai-stack.yaml:849-892`)
- [ ] Dedicated `AWS::Logs::LogGroup` named
      `!Sub "/${AWS::StackName}/lambda/<FunctionName>"` with KMS encryption
      (`KmsKeyId: !Ref CustomerManagedEncryptionKeyArn`)
- [ ] Function references that LogGroup via
      `LoggingConfig.LogGroup: !Ref <FunctionName>LogGroup`
- [ ] `Runtime: python3.12`
- [ ] cfn-nag suppressions in `Metadata` include a `reason:` field for every
      rule (e.g. `W89`, `W92`, `W76`)
- [ ] checkov skip comments include rationale (e.g.
      `# checkov:skip=CKV_AWS_116: "DLQ not required - Step Functions retries"`)
- [ ] If the function defines its own IAM role, the role has the
      `PermissionsBoundary` conditional:
      `!If [HasPermissionsBoundary, !Ref PermissionsBoundaryArn, !Ref AWS::NoValue]`
- [ ] `DependsOn: [ <FunctionName>LogGroup ]` so Lambda doesn't auto-create a
      conflicting log group

### Security
- [ ] No credentials, API keys, or secrets in code or templates
- [ ] No full JWT tokens or session tokens logged in plaintext
- [ ] S3 access scoped — flag new wildcard `Resource: "*"` on S3 actions
      without a written justification in a `reason:` suppression
- [ ] Input validation on new API surfaces; `DOMPurify` for HTML rendering in
      the UI

## Documentation
- [ ] User-facing changes update `CHANGELOG.md` (Keep-a-Changelog format)
- [ ] New feature docs added under `docs/` with YAML frontmatter:
  ```markdown
  ---
  title: "Feature Title"
  ---
  ```
- [ ] License/copyright line below the frontmatter
- [ ] New doc linked from `docs/INDEX.md` where appropriate
- [ ] Public Python functions/classes have docstrings
- [ ] Complex logic has inline `# why` comments (not `# what`)

## Testing
- [ ] Unit tests live alongside the Lambda
      (`lma-ai-stack/source/lambda_functions/<name>/test_*.py`); reuse the
      pattern in existing tests (e.g. `mcp_analytics/test_jsonrpc.py`)
- [ ] AWS calls mocked with `unittest.mock` / `MagicMock` (or `moto` if AWS
      service-level mocking is needed)
- [ ] `make test` (in the appropriate stack) passes locally with
      `AWS_PROFILE=default`
- [ ] UI tests via vitest (`cd lma-ai-stack/source/ui && npm test`)
- [ ] WebSocket transcriber: `cd lma-websocket-transcriber-stack/source/app
      && npm test`

## Git Workflow
- [ ] Branch from `develop` using prefix: `feature/`, `fix/`, `docs/`,
      `chore/`, or `release/`
- [ ] Focused, single-issue change — no unrelated reformatting
- [ ] PR targets `develop` (NOT `main`)
- [ ] `VERSION` bumped if change scope requires it
- [ ] Commit message style consistent with recent history
      (`feat(scope):`, `fix(scope):`, `chore:`, etc.)

## Memory Notes
See `[[feedback_lma_deploy_untracked_files]]` — `lma deploy --from-code`
exits 0 but no-ops if there are untracked files in the bundle. Make sure
new files are tracked before relying on a `--from-code` deploy.
