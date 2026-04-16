---
title: "Developer Guide"
---

# Developer Guide

This guide describes how to build the LMA project from source code, run local development, and contribute.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Repository Structure](#repository-structure)
- [Building and Publishing](#building-and-publishing)
- [Local UI Development](#local-ui-development)
- [WebSocket Server](#websocket-server)
- [Virtual Participant](#virtual-participant)
- [WebSocket Test Client](#websocket-test-client)
- [Linting](#linting)
- [Customization Entry Points](#customization-entry-points)
- [Contributing](#contributing)

## Prerequisites

You need the following installed on your machine:

| Dependency | Version |
|------------|---------|
| bash | Linux, macOS, or Windows WSL |
| Node.js | v18, v20, or v22 |
| npm | Bundled with Node.js |
| Docker | Running (required for SAM builds) |
| zip | Any version |
| Python 3 | With pip3 |
| virtualenv | `pip3 install virtualenv` |
| AWS CLI | Configured with credentials |
| AWS SAM CLI | >= 1.118.0 |

## Repository Structure

```
lma-main.yaml                    # Main CloudFormation orchestration template
publish.sh                        # Build and publish script
VERSION                           # Current version (0.3.0)
lma-ai-stack/                     # Core: Lambdas, AppSync, DynamoDB, UI
  ├── deployment/                 # CloudFormation templates
  ├── source/
  │   ├── lambda_functions/       # 19 Python Lambda functions
  │   ├── lambda_layers/          # Shared Python/Node layers
  │   ├── appsync/                # GraphQL schema and 39 resolvers
  │   └── ui/                     # React web application
  ├── Makefile                    # Build orchestration
  └── config.mk                  # Build configuration
lma-websocket-transcriber-stack/  # WebSocket server (TypeScript/Fastify on Fargate)
lma-virtual-participant-stack/    # VP (TypeScript/Puppeteer on ECS)
lma-vpc-stack/                    # VPC networking
lma-cognito-stack/                # Cognito auth
lma-meetingassist-setup-stack/    # Strands agent config
lma-bedrockkb-stack/              # Bedrock Knowledge Base
lma-llm-template-setup-stack/     # LLM prompt templates
lma-chat-button-config-stack/     # Chat button config
lma-nova-sonic-config-stack/      # Nova Sonic config
docs/                             # Documentation (you are here)
```

## Building and Publishing

### Using LMA CLI (Recommended)

Set up your development environment (installs Node.js, Python venv, SDK, and CLI):

```bash
make setup
```

#### Check Prerequisites

```bash
lma-cli check-prereqs
```

#### Build and Deploy in One Step

The simplest way to build from source and deploy:

```bash
lma-cli deploy --stack-name LMA --from-code . --admin-email user@example.com --wait
```

This builds all stacks, publishes artifacts to S3, and deploys the CloudFormation stack in one command. Use `--wait` to monitor progress with real-time event streaming.

#### Publish Only (without deploying)

```bash
lma-cli publish --source-dir . --region us-east-1
```

This packages all sub-stacks, uploads to S3, and outputs a CloudFormation template URL you can use later.

#### Deploy from Published Template

```bash
lma-cli deploy --stack-name LMA --template-url <template-url> --admin-email user@example.com --wait
```

See the [LMA CLI Reference](lma-cli.md) for the full list of options.

Both `lma-cli publish` and `lma-cli deploy --from-code` use content-hash-based checksums to skip rebuilding unchanged stacks on subsequent runs.

## Local UI Development

The React UI is in `lma-ai-stack/source/ui/`. The simplest way to start the UI dev server is:

```bash
make ui-start STACK_NAME=<your-stack-name>
```

This automatically retrieves the `.env` configuration from your deployed stack's CloudFormation outputs, installs dependencies, and starts the development server at [http://localhost:3000](http://localhost:3000). The page reloads on edits.

Other npm scripts (run from `lma-ai-stack/source/ui/`):
- `npm test` — Run Jest tests in watch mode
- `npm run build` — Production build to `build/`

## WebSocket Server

The WebSocket transcription server is in `lma-websocket-transcriber-stack/source/app/`.

```bash
cd lma-websocket-transcriber-stack/source/app
npm install
npm run build     # TypeScript compilation
npm test          # Jest tests
```

The server is a TypeScript/Fastify application deployed as a Docker container on ECS Fargate behind an Application Load Balancer.

## Virtual Participant

The VP backend is in `lma-virtual-participant-stack/backend/`.

```bash
cd lma-virtual-participant-stack/backend
npm install
npm run build     # TypeScript compilation
```

### Local Docker Testing

Build and run the VP container locally:

```bash
cd lma-virtual-participant-stack
docker build -t lma-vp .
```

Run with required environment variables:

```bash
docker run \
  --env MEETING_ID=123456789 \
  --env MEETING_PASSWORD=abc123 \
  --env MEETING_NAME=TestMeeting \
  --env AWS_DEFAULT_REGION=us-east-1 \
  --env KINESIS_STREAM_NAME=<CallDataStreamName> \
  --env SHOULD_RECORD_CALL=true \
  --env RECORDINGS_BUCKET_NAME=<RecordingsS3Bucket> \
  --env RECORDINGS_KEY_PREFIX=lca-audio-recordings/ \
  --env MEETING_PLATFORM=Zoom \
  --env USER_NAME=TestUser \
  lma-vp
```

### Manual Step Function Testing

Execute the VP scheduler Step Function directly:

```bash
aws stepfunctions start-execution \
  --state-machine-arn arn:aws:states:us-east-1:123456789012:stateMachine:SchedulerStateMachine-XXXX \
  --input '{"apiInfo":{"httpMethod":"POST"},"data":{"meetingPlatform":"Zoom","meetingID":"12345678","meetingPassword":"a1b2c3","meetingName":"Test","meetingTime":"","userName":"Bob"}}'
```

Supported `httpMethod` values: `POST` (join/schedule), `GET` (list scheduled), `DELETE` (cancel scheduled).

## WebSocket Test Client

A test utility in `utilities/websocket-client/` streams WAV file audio to the WebSocket server:

```bash
cd utilities/websocket-client
npm run setup           # Install dependencies (first time)
npm run build           # Build TypeScript
```

Configure environment variables (export or `.env` file):

```
SAMPLE_RATE=8000
BYTES_PER_SAMPLE=2
CHUNK_SIZE_IN_MS=200
CALL_FROM_NUMBER='LCA-Client'
CALL_TO_NUMBER='+8001112222'
AGENT_ID='TestAgent'
LMA_ACCESS_JWT_TOKEN=<access_token>
LMA_ID_JWT_TOKEN=<id_token>
LMA_REFRESH_JWT_TOKEN=<refresh_token>
```

Get the JWT tokens from an authenticated LMA user session. Then run:

```bash
npm run start -- --uri <WebSocket_Server_Endpoint> --wavfile <file.wav>
```

The WebSocket server endpoint is in the CloudFormation stack Outputs.

## Linting

From `lma-ai-stack/`, the Makefile provides linting targets (requires `CONFIG_ENV` environment variable):

| Target | Tool | What it checks |
|--------|------|----------------|
| `make lint-cfn-lint` | cfn-lint | CloudFormation templates |
| `make lint-yamllint` | yamllint | YAML syntax |
| `make lint-pylint` | pylint | Python code (100 char lines) |
| `make lint-mypy` | mypy | Python type annotations |
| `make lint-bandit` | bandit | Python security |
| `make lint-validate` | SAM CLI | Template validation |

Code style conventions:
- **Python**: Black formatter, Flake8, Pylint. 100-character line limit. Config in `.pylintrc`, `.flake8`.
- **JavaScript/TypeScript**: ESLint (airbnb-base) + Prettier. 120-character line limit, single quotes, trailing commas. Config in `.eslintrc.json`, `.prettierrc`.

## Customization Entry Points

| What to customize | How | Docs |
|-------------------|-----|------|
| LLM summary prompts | DynamoDB or admin UI | [Transcript Summarization](transcript-summarization.md) |
| Chat shortcut buttons | Admin UI | [Meeting Assistant](meeting-assistant.md) |
| MCP server integrations | Admin UI | [MCP Servers](mcp-servers.md) |
| Knowledge Base documents | S3 bucket or web crawling | [Meeting Assistant](meeting-assistant.md) |
| Bedrock Guardrails | CloudFormation parameter | [Meeting Assistant](meeting-assistant.md) |
| Transcript processing | Custom Lambda function | [Lambda Hook Functions](lambda-hook-functions.md) |
| Voice assistant prompts | DynamoDB or admin UI | [Nova Sonic 2 Setup](nova-sonic-setup.md) |

## Contributing

See [CONTRIBUTING.md](../CONTRIBUTING.md) for guidelines on reporting bugs, requesting features, and submitting pull requests.
