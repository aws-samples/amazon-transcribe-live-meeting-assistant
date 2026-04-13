# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Live Meeting Assistant (LMA) -- an AWS-based solution for real-time meeting transcription, AI-powered meeting assistance, and virtual meeting participation. Built on Amazon Transcribe, Amazon Bedrock, and the Strands Agents SDK. Current version is tracked in `./VERSION`.

## Build & Publish

**Prerequisites:** bash, node v18/v20/v22, npm, docker (running), zip, python3, pip3, virtualenv, aws cli, sam cli (>=1.118.0).

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
npm test                    # jest/react-scripts tests
```

**WebSocket server** (in `lma-websocket-transcriber-stack/source/app/`):
```bash
npm install && npm run build   # TypeScript build
npm test                       # jest tests
```

**Virtual Participant** (in `lma-virtual-participant-stack/backend/`):
```bash
npm install && npm run build   # TypeScript build
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
| `lma-virtual-participant-stack/` | Headless Chrome (Puppeteer) on ECS Fargate joining meetings, optional voice assistant + avatar | TypeScript |
| `lma-vpc-stack/` | VPC networking, security groups, NAT gateways | CloudFormation |
| `lma-meetingassist-setup-stack/` | Meeting assistant configuration | CloudFormation |
| `lma-bedrockkb-stack/` | Bedrock Knowledge Base setup | CloudFormation |
| `lma-cognito-stack/` | Cognito user pool and identity pool | CloudFormation |
| `lma-llm-template-setup-stack/` | LLM prompt templates stored in DynamoDB | CloudFormation |
| `lma-chat-button-config-stack/` | Chat UI button configuration | CloudFormation |
| `lma-nova-sonic-config-stack/` | Nova Sonic voice assistant config | CloudFormation |

**Data flow:** Browser audio -> WebSocket server (Fargate) -> Amazon Transcribe -> Kinesis Data Stream -> Call Event Processor Lambda (Strands Agents SDK) -> DynamoDB + AppSync (real-time GraphQL subscriptions) -> React UI.

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
