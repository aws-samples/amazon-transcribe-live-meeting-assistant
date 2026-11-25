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
- [Quick Start Guide](quick-start-guide.md) — Your first meeting in 5 minutes using Stream Audio/Video or Virtual Participant
- [Demo Videos](demo-videos.md) — Recorded walkthroughs of LMA features, and how to record and publish a new one
- [LMA in Action](lma-in-action.md) — An illustrated one-page tour of what LMA does and what sets it apart

### Core Features

- [Transcription & Translation](transcription-and-translation.md) — Real-time transcription, speaker attribution, multi-language support, live translation, PII redaction, custom vocabulary, audio recording
- [On-demand ASR & Speaker Diarization (MicroVM)](microvm-asr.md) — An alternative to Amazon Transcribe (the default engine), selected per meeting source: per-voice speaker labels from an ASR + diarization engine on AWS Lambda MicroVMs, with a swappable model
- [Voice Enrollment (Speaker Recognition)](voice-enrollment.md) — Beta, optional and off by default: people enroll their own voice, with first-party consent and an append-only consent log, so they are labelled by name from their first word
- [Meeting Assistant](meeting-assistant.md) — Strands agent chat interface, built-in tools, Knowledge Base, Guardrails, model selection, wake phrase, custom prompts
- [Live Guidance](live-guidance.md) — Guidance offered while a meeting is in progress, without being asked: the checklist progress it records, the guidance types, who sees it, turning it on for a meeting or for every new meeting, how often it runs, the digest, its settings, cost per meeting-hour, and its limits
- [Meeting Whiteboards](meeting-whiteboards.md) — Named panels (Markdown, or Mermaid diagrams) on the meeting page that the people who can see a meeting edit and the assistant can keep up to date from the transcript: templates, user edits win, versions and restore, who can see them, retention with the transcript, settings, cost, the MCP tools and the export
- [Meeting Assistant Skills](meeting-assistant-skills.md) — Guidance an administrator writes for the assistant, read only when a request needs it: the Skills page, importing SKILL.md, Agent Skills folders and Amazon Quick skill files, the skill library, choosing skills per profile, and how the assistant loads a skill
- [Transcript Summarization](transcript-summarization.md) — Automatic and on-demand summaries, regenerating and editing a stored summary, custom prompt templates, Lambda-based summarization
- [Meeting Context, Labels, Audience and Checklist](meeting-context-and-checklist.md) — The four inputs a meeting can carry beside its agenda: what each is for, its limits, the Meeting preparation section of the Virtual Participant form, the panels on the meeting page, who can see and change them, the assistant's rule for requests made in the meeting, the summary placeholders, and retention
- [Meeting Agenda](meeting-agenda.md) — An agenda on each meeting: given when a Virtual Participant is started or scheduled (on the form or by an MCP client), the Agenda panel on the meeting page, who can change it, deriving one from the transcript, the summary's agenda coverage section, and asking the assistant how the meeting is going against it
- [Meeting Consent](meeting-consent.md) — Telling participants a meeting is recorded and transcribed, and waiting for their consent: the off, notify and opt-in modes and the two opt-in rules, the Meeting Consent settings, the consent record kept on each meeting, who can change it, the capture gate and the API capture clients call, and the limitations of what it records
- [Transcript and Recording Deletion After the Summary](transcript-deletion.md) — Deleting each meeting's transcript, and by a setting of its own its audio and video recordings, permanently once it is summarized, immediately or 15, 30 or 60 minutes later: when it happens, what is deleted and what is kept, what users see, how it is carried out, and the copies outside the transcript store that follow their own lifetimes
- [Meeting Video and Key Frames](meeting-video.md) — Key frames of the shared screen: when they are taken, the screen-share signal per platform, measured cost, storage, retention and access, and the API to read them
- [Meetings Query Tool](meetings-query-tool.md) — Semantic search across past meetings via transcript knowledge base

### Meeting Sources

- [Meeting Sources Overview](meeting-sources.md) — Side-by-side comparison of the three capture options and guidance on when to use each
- [Stream Audio/Video](stream-audio.md) — Browser-based audio capture from your microphone and any Chrome tab, with optional screen video
- [Upload Audio](upload-audio.md) — Upload a pre-recorded audio/video file for batch transcription and summary
- [Browser Extension](browser-extension.md) — Chrome extension for capturing browser tab audio from any meeting platform
- [Desktop Capture App](desktop-capture-app.md) — Native macOS / Windows app (menu bar / system tray) that captures system + mic audio for meetings joined from native desktop apps (no browser, no bot)
- [Virtual Participant](virtual-participant.md) — Headless Chrome bot that joins Zoom, Teams, Chime, WebEx and Google Meet meetings; runs on AWS Lambda MicroVMs (default), EC2, or Fargate
- [Zoom Sign-in & Join Reliability](zoom-credentials-and-join-reliability.md) — Per-user Zoom credentials, AI-driven sign-in loop, persistent Chromium profiles, AI DOM resolver fallback
- [Zoom Meeting SDK Join](zoom-meeting-sdk.md) — Join Zoom via the Zoom Meeting Web SDK (alternative to web-client automation), credentials, and approval requirements
- [Teams ACS SDK Join](teams-acs-sdk.md) — Join Microsoft Teams via Azure Communication Services interop (alternative to web-client automation), ACS resource setup and connection string

### Voice Assistant & Avatar

- [Voice Assistant Overview](voice-assistant.md) — Activation modes, wake phrases, session management, provider comparison
- [Nova Sonic 2 Setup](nova-sonic-setup.md) — AWS Nova Sonic 2 voice assistant configuration
- [ElevenLabs Setup](elevenlabs-setup.md) — ElevenLabs Conversational AI voice assistant configuration
- [Simli Avatar Setup](simli-avatar-setup.md) — Animated lip-synced avatar for Virtual Participant

### MCP Server Integration

- [MCP Servers Overview](mcp-servers.md) — Model Context Protocol, authentication methods, admin UI, built-in LMA tools
- [Meeting Export](meeting-export.md) — One structured JSON record of a meeting (schema version 1): what it holds, `include`, participants and where their names come from, retention, large exports, and the Download menu, Everything archive and MCP routes to it
- [Meeting Notifications](meeting-notifications.md) — Items shown live in a meeting's Notifications panel to the people who can see the meeting (guidance is one category): who sees them, dismissing and muting, buttons, retention, reading them over MCP, and publishing one from a backend function
- [MCP API Key Authentication](mcp-api-key-auth.md) — Per-user API key auth for programmatic MCP clients, key scopes (read only, read and write), JSON-RPC 2.0 protocol support, and every hosted MCP tool: the transcript read page by page, meeting preparation, meeting guidance and notifications, the meeting record, consent, the meeting assistant and meeting management
- [MCP Tools Reference](mcp-tools-reference.md) — Every tool the hosted MCP server offers, with an example request and answer for those an AI assistant uses to prepare, follow and close a meeting: scheduling with meeting preparation, transcript paging, agenda, context, checklist, labels, sharing, consent and ending
- [Salesforce MCP Setup](salesforce-mcp-setup.md) — Salesforce Connected App with OAuth 2.1
- [Amazon Quick MCP Setup](amazon-quick-mcp-setup.md) — Connect LMA to Amazon Quick Suite (web, OAuth) or Quick Desktop (native, API key)
- [Amazon Quick Desktop: LMA Workflows](amazon-quick-desktop-integration.md) — Conversational workflows, scheduled agent recipes, and the skills pack for Quick Desktop
- [DeepWiki MCP Setup](deepwiki-mcp-setup.md) — DeepWiki repository documentation search

### Web UI

- [Web UI Guide](web-ui-guide.md) — Dashboard, meeting details, chat, sentiment analysis, transcript downloads, sharing, admin configuration pages

### Access Control & Security

- [User Management](user-management.md) — Admin-only UI to create and delete LMA users (Admin or User roles)
- [User-Based Access Control](user-based-access-control.md) — Admin vs non-admin users, meeting sharing, meeting deletion
- [Infrastructure & Security](infrastructure-and-security.md) — Architecture overview, VPC, Cognito, KMS encryption, CloudFront, IAM, data retention, and [where meeting content is processed](infrastructure-and-security.md#where-meeting-content-is-processed) (AWS Region, Amazon Bedrock inference geography, optional integrations)
- [Privacy and Data Handling Controls](privacy-and-data-handling.md) — For administrators and reviewers: the consent options, every kind of meeting data with where it is stored, how long it is kept and what controls that, deleting transcripts, meetings and stacks, where content is processed, and the presets and deploy-time controls for a regulated deployment
- [Hosting Models](hosting-models.md) — Running LMA in a team's own account for meetings with external organizations, or in the account of the organization whose meetings it records: the two presets, the deploy-time settings, isolating the account, what meeting platforms need to allow, and moving meetings between the two with `lma meetings delete`
- [Security Scanning (SRT)](security-scanning.md) — Automated security scanning via the AWS Sample Security Review Tool, suppression workflow, GitLab CI integration, DSR → SRT migration
- [Security Testing](security-testing.md) — The live access tests (`make stacktest-rbac`: roles, tokens, meeting access and every other entry point, as throwaway users), the OWASP ZAP scan (`make stacktest-zap`), `make security-results`, how to read their reports, what they create and change, and when they run; and the [release snapshots](security-testing.md#release-snapshots), the counts-only record of each release's security test results in `security/test-results/<version>/`
- [CloudFormation Service Role](cloudformation-service-role.md) — Delegated IAM role for non-admin LMA deployment and management

### Integration & API

- [WebSocket Streaming API](websocket-streaming-api.md) — Full protocol specification for building custom streaming clients
- [Embeddable Components](embeddable-components.md) — iframe embedding, postMessage API, authentication options
- [Lambda Hook Functions](lambda-hook-functions.md) — Custom transcript processing, FetchTranscript utility, summarization hooks
- [Meeting Lifecycle Events](meeting-events.md) — The stack's EventBridge event bus: Meeting Started, Meeting Ended, Summary Ready and Virtual Participant Status Changed, what each event carries (identifiers and states only), reopened meetings, deduplication, an example rule, and polling instead
- [Solution Profile](solution-profile.md) — The machine-readable description of what LMA does, where it can be extended, where it stops and what to ask to judge fit, published with each GitHub Release: its files, the checks that keep it in step with the code, the deterministic release asset, and the review each release needs

### Administration

- [CloudFormation Parameters Reference](cloudformation-parameters.md) — Complete reference of all stack parameters by category
- [Configuration Guide](configuration-guide.md) — For administrators: which settings can be changed from the web UI without redeploying, where, when a saved change applies, and what to do when a save is refused
- [Unified Configuration Store](configuration-store.md) — The shared runtime-configuration table: its key layout, the compressed record body, the three caching tiers a section can declare, conditional saves, the section schemas every save is checked against, the revision history every save records, and deploy-time seeding
- [Stack Updates & Upgrades](stack-updates-and-upgrades.md) — Updating existing stacks, template URLs, version migration notes
- [Well-Architected Framework Assessment](well-architected.md) — Assessment of LMA against the six pillars of the AWS Well-Architected Framework
- [Meeting Cost Estimate](meeting-cost-estimate.md) — The itemised estimate of what each meeting cost: what is metered and how, the price table and where its prices come from, changing a price, and what the estimate leaves out
- [Monitoring & Alerting](monitoring.md) — The operational alerts SNS topic and its alarms, the CloudWatch dashboard, how to subscribe a team rather than one person, and why an unconfirmed email subscription delivers nothing
- [Troubleshooting](troubleshooting.md) — Monitoring, CloudWatch logs, common issues, cost assessment
- [AI Troubleshooting Agent](troubleshooting-agent.md) — The read-only diagnostic agent: the Admin Troubleshooting page and Diagnose button, how to invoke it directly, the four log and record tools it uses, where its system prompt lives and how to tune it without redeploying, and the spend limits that bound a run
- [Cleanup](cleanup.md) — Deleting stacks and retained resources

### Development

- [Developer Guide](developer-guide.md) — Building from source, LMA CLI, local UI development, contributing
- [Scheduled Integration Tests](scheduled-integration-tests.md) — The nightly end-to-end run against a live stack: what it covers, how failures surface, and the one-time GitLab schedule / CI role / test-user setup it needs
- [Test Coverage](test-coverage.md) — `make test-coverage`: line coverage per component, how unimported files are accounted for, and the current baseline
- [Virtual Participant Local Development](virtual-participant-local-dev.md) — Run the VP locally on EC2 via `make vp-start` with VSCode Remote-SSH + VNC
- [LMA CLI Reference](lma-cli.md) — Command-line interface for deploy, publish, status, logs, Virtual Participant, and the `lma load` plugin
- [LMA SDK Reference](lma-sdk.md) — Python SDK for programmatic LMA operations (stack, publish, AppSync, Virtual Participant)
- [LMA Load Simulator](../utilities/load-simulator/README.md) — Stress-test a deployed LMA stack (concurrent meetings, historical backfill, RBAC-at-scale, deterministic cleanup) — exposed as `lma load …` via the CLI plugin mechanism


