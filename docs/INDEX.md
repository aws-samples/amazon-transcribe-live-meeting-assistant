---
title: "LMA Documentation"
---

# LMA Documentation

**Live Meeting Assistant (LMA) v0.3.0** — Real-time meeting transcription, AI-powered meeting assistance, and virtual meeting participation built on Amazon Transcribe, Amazon Bedrock, and the Strands Agents SDK.

**📖 Browse the docs site: [aws-samples.github.io/amazon-transcribe-live-meeting-assistant](https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant/)**

> For the changelog, see [CHANGELOG.md](../CHANGELOG.md). For contributing, see [CONTRIBUTING.md](../CONTRIBUTING.md).

---

## Table of Contents

### Getting Started

- [Prerequisites & Deployment](prerequisites-and-deployment.md) — AWS account setup, Bedrock model access, CloudFormation deployment, initial login
- [Quick Start Guide](quick-start-guide.md) — Your first meeting in 5 minutes using Stream Audio or Virtual Participant

### Core Features

- [Transcription & Translation](transcription-and-translation.md) — Real-time transcription, speaker attribution, multi-language support, live translation, PII redaction, custom vocabulary, audio recording
- [Meeting Assistant](meeting-assistant.md) — Strands agent chat interface, built-in tools, Knowledge Base, Guardrails, model selection, wake phrase, custom prompts
- [Transcript Summarization](transcript-summarization.md) — Automatic and on-demand summaries, custom prompt templates, Lambda-based summarization
- [Meetings Query Tool](meetings-query-tool.md) — Semantic search across past meetings via transcript knowledge base

### Meeting Sources

- [Stream Audio](stream-audio.md) — Browser-based audio capture from your microphone and any Chrome tab
- [Browser Extension](browser-extension.md) — Chrome extension for capturing browser tab audio from any meeting platform
- [Virtual Participant](virtual-participant.md) — Headless Chrome bot that joins Zoom, Teams, Chime, Google Meet, and WebEx meetings

### Voice Assistant & Avatar

- [Voice Assistant Overview](voice-assistant.md) — Activation modes, wake phrases, session management, provider comparison
- [Nova Sonic 2 Setup](nova-sonic-setup.md) — AWS Nova Sonic 2 voice assistant configuration
- [ElevenLabs Setup](elevenlabs-setup.md) — ElevenLabs Conversational AI voice assistant configuration
- [Simli Avatar Setup](simli-avatar-setup.md) — Animated lip-synced avatar for Virtual Participant

### MCP Server Integration

- [MCP Servers Overview](mcp-servers.md) — Model Context Protocol, authentication methods, admin UI, built-in LMA tools
- [MCP API Key Authentication](mcp-api-key-auth.md) — Per-user API key auth for programmatic MCP clients, JSON-RPC 2.0 protocol support
- [Salesforce MCP Setup](salesforce-mcp-setup.md) — Salesforce Connected App with OAuth 2.1
- [Amazon Quick Suite MCP Setup](quicksuite-mcp-setup.md) — Amazon Quick Suite integration
- [DeepWiki MCP Setup](deepwiki-mcp-setup.md) — DeepWiki repository documentation search

### Web UI

- [Web UI Guide](web-ui-guide.md) — Dashboard, meeting details, chat, sentiment analysis, transcript downloads, sharing, admin configuration pages

### Access Control & Security

- [User Management](user-management.md) — Admin-only UI to create and delete LMA users (Admin or User roles)
- [User-Based Access Control](user-based-access-control.md) — Admin vs non-admin users, meeting sharing, meeting deletion
- [Infrastructure & Security](infrastructure-and-security.md) — Architecture overview, VPC, Cognito, KMS encryption, CloudFront, IAM, data retention
- [CloudFormation Service Role](cloudformation-service-role.md) — Delegated IAM role for non-admin LMA deployment and management

### Integration & API

- [WebSocket Streaming API](websocket-streaming-api.md) — Full protocol specification for building custom streaming clients
- [Embeddable Components](embeddable-components.md) — iframe embedding, postMessage API, authentication options
- [Lambda Hook Functions](lambda-hook-functions.md) — Custom transcript processing, FetchTranscript utility, summarization hooks

### Administration

- [CloudFormation Parameters Reference](cloudformation-parameters.md) — Complete reference of all stack parameters by category
- [Stack Updates & Upgrades](stack-updates-and-upgrades.md) — Updating existing stacks, template URLs, version migration notes
- [Troubleshooting](troubleshooting.md) — Monitoring, CloudWatch logs, common issues, cost assessment
- [Cleanup](cleanup.md) — Deleting stacks and retained resources

### Development

- [Developer Guide](developer-guide.md) — Building from source, LMA CLI, local UI development, contributing
- [LMA CLI Reference](lma-cli.md) — Command-line interface for deploy, publish, status, logs, and more
- [LMA SDK Reference](lma-sdk.md) — Python SDK for programmatic LMA operations
