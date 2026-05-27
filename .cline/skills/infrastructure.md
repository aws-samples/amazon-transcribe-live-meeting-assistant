# Infrastructure — CloudFormation / SAM / IaC — LMA

Conventions for CloudFormation and SAM templates in the Live Meeting
Assistant project. Read alongside `backend-lambda.md` (function-side
conventions) and `code-review.md` (pre-commit checklist).

## Architecture Overview

LMA is a set of **nested CloudFormation stacks** orchestrated by
`lma-main.yaml` at the repo root (`Transform: AWS::Serverless-2016-10-31`).

| Nested stack template (`*/deployment/*.yaml`) | Purpose |
|---|---|
| `lma-ai-stack/deployment/lma-ai-stack.yaml` | Lambdas, AppSync, Cognito, DynamoDB, UI (CloudFront) |
| `lma-websocket-transcriber-stack/` | ECS Fargate WebSocket → Transcribe → Kinesis |
| `lma-virtual-participant-stack/` | ECS Fargate headless Chrome (Puppeteer) |
| `lma-vpc-stack/` | VPC, security groups, NAT |
| `lma-meetingassist-setup-stack/` | Meeting assistant config + Strands agent |
| `lma-bedrockkb-stack/` | Bedrock Knowledge Base |
| `lma-cognito-stack/` | User / identity pools |
| `lma-llm-template-setup-stack/` | LLM prompt templates in DynamoDB |
| `lma-chat-button-config-stack/` | Chat UI button config |
| `lma-nova-sonic-config-stack/` | Nova Sonic voice assistant config |

## Key Parameters (in `lma-main.yaml`)

- `AdminEmail`, `AllowedSignUpEmailDomain`
- `PermissionsBoundaryArn` — applies to all IAM roles when non-empty
- `CustomerManagedEncryptionKeyArn` — KMS key for CloudWatch Logs / DynamoDB / SQS
- `EnableDataRetentionOnDelete` — toggles `Retain` vs `Delete` deletion policies
- `BedrockModelId`, knowledge-base parameters, etc.

## CRITICAL: GovCloud Compatibility Rules

Every template change MUST follow these rules:

1. **ARN partition**: Use
   `!Sub "arn:${AWS::Partition}:service:${AWS::Region}:${AWS::AccountId}:resource"`
   - Never hardcode `arn:aws:` — it breaks in GovCloud (`arn:aws-us-gov:`)
   - **Exception**: `AllowedPattern` regexes in parameters can include
     `arn:aws:` literals (e.g.
     `"^(|arn:aws:lambda:.*)$"` in `lma-main.yaml:360`) — these are
     existing string-validation patterns and are not flagged

2. **Service endpoints**: Use `!Sub "service.${AWS::URLSuffix}"`
   - Never hardcode `amazonaws.com` — China uses `amazonaws.com.cn`
   - Common LMA usage: `!Sub "states.${AWS::URLSuffix}"`,
     `!Sub "lambda.${AWS::URLSuffix}"`

3. **Permissions boundary**: Use `!If [HasPermissionsBoundary, ...]` on
   every IAM role:
   ```yaml
   Conditions:
     HasPermissionsBoundary: !Not [!Equals [!Ref PermissionsBoundaryArn, ""]]
   # ...
   PermissionsBoundary: !If
     - HasPermissionsBoundary
     - !Ref PermissionsBoundaryArn
     - !Ref AWS::NoValue
   ```

4. **Verify**: `make lint-cfn-lint`, `make lint-yamllint`,
   `make lint-validate` (in `lma-ai-stack/`).

## Canonical Lambda Resource Pattern

Reference: `lma-ai-stack/deployment/lma-ai-stack.yaml:849-892`
(`BackfillWorkerFunction`).

```yaml
MyFunctionLogGroup:
  Type: AWS::Logs::LogGroup
  Properties:
    LogGroupName: !Sub "/${AWS::StackName}/lambda/MyFunction"
    RetentionInDays: !Ref CloudWatchLogsExpirationInDays
    KmsKeyId: !Ref CustomerManagedEncryptionKeyArn

MyFunction:
  Type: AWS::Serverless::Function
  Metadata:
    cfn_nag:
      rules_to_suppress:
        - id: W89
          reason: "Function does not require VPC access — only interacts with DynamoDB"
        - id: W92
          reason: "Function does not require reserved concurrency"
  # checkov:skip=CKV_AWS_116: "DLQ not required - Step Functions handles retries"
  # checkov:skip=CKV_AWS_117: "Function does not require VPC access"
  # checkov:skip=CKV_AWS_115: "Function does not require reserved concurrency"
  # checkov:skip=CKV_AWS_173: "Environment variables do not contain sensitive data"
  Properties:
    FunctionName: !Sub "${AWS::StackName}-MyFunction"
    Handler: lambda_function.handler
    Runtime: python3.12
    CodeUri: ../source/lambda_functions/my_function
    Description: One-line description of what this function does
    Timeout: 60
    MemorySize: 256
    Tracing: Active                       # X-Ray tracing
    LoggingConfig:
      LogGroup: !Ref MyFunctionLogGroup
    Environment:
      Variables:
        LOG_LEVEL: !Ref LogLevel
        STACK_NAME: !Ref AWS::StackName
    Policies:
      - DynamoDBCrudPolicy:
          TableName: !Ref EventSourcingTable
      - Statement:
          - Effect: Allow
            Action:
              - kms:Encrypt
              - kms:Decrypt
              - kms:ReEncrypt*
              - kms:GenerateDataKey*
              - kms:DescribeKey
            Resource: !Ref CustomerManagedEncryptionKeyArn
  DependsOn:
    - MyFunctionLogGroup     # prevents Lambda from auto-creating a conflicting log group
```

If the function defines its own IAM role (instead of letting SAM generate
one), the role MUST include the `PermissionsBoundary` `!If` conditional —
see `lma-ai-stack/deployment/lma-ai-stack.yaml:894-907` for the
`BackfillStateMachineRole` example.

## cfn-nag / checkov Suppression Rules

- **Every** suppressed rule needs a `reason:` (cfn-nag) or rationale comment
  (checkov) — see canonical `reason:` strings in
  `lma-ai-stack/deployment/lma-ai-stack.yaml:858-868`
- Common LMA reasons:
  - `W89: Function does not require VPC access — only interacts with <service>`
  - `W92: Function does not require reserved concurrency`
  - `W76: Complexity necessary for this orchestration`
  - `CKV_AWS_116: DLQ not required - Step Functions handles retries`
  - `CKV_AWS_117: Function does not require VPC access`
  - `CKV_AWS_115: Function does not require reserved concurrency`
  - `CKV_AWS_173: Environment variables do not contain sensitive data`

## Encryption & Retention

- DynamoDB tables, CloudWatch Log Groups, S3 buckets, and SQS queues all use
  `CustomerManagedEncryptionKeyArn` (KMS) where supported
- Stateful resources alternate `DeletionPolicy` based on
  `EnableDataRetentionOnDelete`:
  ```yaml
  DeletionPolicy: !If [HasDataRetention, Retain, Delete]
  UpdateReplacePolicy: !If [HasDataRetention, Retain, Delete]
  ```

## Build & Deploy

**AWS profile:** Always export `AWS_PROFILE=default` before any build / deploy
/ test command. Other profiles (e.g. `bedrock`) point at unrelated accounts
and will fail with `AccessDenied` on S3/CloudFormation.

**Full publish (recommended for releases):**
```bash
AWS_PROFILE=default ./publish.sh <cfn_bucket_basename> <cfn_prefix> <region> [public]
```
Validates dependencies, builds all stacks (SAM + npm), uploads artifacts to
S3, prints CloudFormation deploy URLs. End-to-end deployment via the
emitted URL takes 35–40 minutes.

**Per-stack (in `lma-ai-stack/`):**
```bash
# CONFIG_ENV is required (set in config.mk or config-$USER.mk)
AWS_PROFILE=default make install
AWS_PROFILE=default make build
AWS_PROFILE=default make package
AWS_PROFILE=default make deploy
AWS_PROFILE=default make test-local-invoke-default
```

**Deploying from local code with the LMA CLI:**
`lma deploy --from-code` exits 0 but **no-ops if there are untracked files
in the bundle** — make sure new files are committed (or at least tracked)
before running it. (See `[[feedback_lma_deploy_untracked_files]]`.)

## Lint Commands (in `lma-ai-stack/`)

```bash
make lint-cfn-lint              # cfn-lint
make lint-yamllint              # YAML lint
make lint-validate              # SAM template validation
make lint-cfn-policy-validator  # IAM policy validator
make lint-cfn                   # all of the above + cfn_nag
```

## Common Red Flags

- New hardcoded `arn:aws:` outside of an `AllowedPattern` regex
- New hardcoded `amazonaws.com`
- New IAM role missing the `PermissionsBoundary` `!If` block
- New Lambda without a dedicated `LogGroup` + `LoggingConfig.LogGroup`
- New `LogGroup` missing `KmsKeyId: !Ref CustomerManagedEncryptionKeyArn`
- cfn-nag suppression without `reason:`
- checkov skip without an inline rationale comment
- New `Resource: "*"` in an IAM policy without a justification suppression
- Missing `DependsOn: [<FunctionName>LogGroup]` (causes log-group conflicts
  on first deploy)

## Helpful File References

- Umbrella stack: `lma-main.yaml` (parameters at lines ~474–640;
  `HasPermissionsBoundary` condition at 999)
- Canonical Lambda example:
  `lma-ai-stack/deployment/lma-ai-stack.yaml:849-892`
- Canonical IAM role with PermissionsBoundary:
  `lma-ai-stack/deployment/lma-ai-stack.yaml:894-907`
- Build/deploy entry: `publish.sh` at repo root; per-stack `Makefile`
  targets in `lma-ai-stack/Makefile`
