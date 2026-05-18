# Live Meeting Assistant (LMA) — Threat Model

## Document Information

| Field | Value |
|-------|-------|
| **Version** | 1.0 |
| **Last Updated** | 2025-05-18 |
| **System** | Live Meeting Assistant (LMA) |
| **Architecture** | Real-time audio transcription + AI meeting assistance |
| **Methodology** | STRIDE |
| **Total Threats** | 54 |
| **Classification** | Internal |

## Overview

This directory contains the comprehensive threat model for the Live Meeting Assistant (LMA) — an AWS-based solution for real-time meeting transcription, AI-powered meeting assistance, and virtual meeting participation. The threat model covers the full architecture: audio streaming, AI processing, virtual participant bots, voice assistant, MCP integrations, knowledge base, and all extensibility points.

### Key Statistics

| Metric | Value |
|--------|-------|
| Threats identified | **54** |
| Very High risk (8-9) | 6 |
| High risk (6) | 15 |
| Medium risk (3-4) | 26 |
| Low risk (1-2) | 7 |
| Mitigated | 33 (61%) |
| Partially mitigated | 14 (26%) |
| Accepted | 7 (13%) |

## Directory Structure

```
threat-modeling/
├── README.md                                    ← You are here
├── threat-id-glossary.md                        ← All 54 threat IDs with cross-references
│
├── architecture/                                ← System architecture & data flows
│   ├── system-overview.md                       ← Architecture, components, trust boundaries
│   └── data-flows.md                            ← All data flow diagrams with security analysis
│
├── feature-threats/                             ← Per-feature threat analysis
│   ├── transcription-audio.md                   ← Audio streaming, Transcribe, Kinesis threats (AUDIO)
│   ├── meeting-assistant.md                     ← Strands Agents, Bedrock, prompt injection (ASST)
│   ├── virtual-participant.md                   ← Headless Chrome bot, platform interaction (VP)
│   ├── voice-assistant.md                       ← Nova Sonic, ElevenLabs, Simli, avatar (VOICE)
│   ├── mcp-integration.md                       ← MCP server, API Gateway, external tools (MCP)
│   ├── knowledge-base.md                        ← Bedrock KB, RAG, semantic search (KB)
│   ├── authentication-access.md                 ← Cognito, JWT, RBAC, meeting access (AUTH)
│   ├── web-ui.md                                ← React UI, CloudFront, AppSync, iframe (UI)
│   ├── recording-storage.md                     ← S3 audio, data retention, consent (REC)
│   └── lambda-hooks.md                          ← Customer extensibility threats (HOOK)
│
├── threat-analysis/                             ← Cross-cutting analysis
│   └── stride-analysis.md                       ← Full STRIDE analysis across all components
│
├── risk-assessment/
│   └── risk-matrix.md                           ← Complete risk register with scoring
│
└── deliverables/                                ← Executive deliverables
    ├── executive-summary.md                     ← Executive-level summary
    └── implementation-guide.md                  ← Security controls implementation details
```

## Quick Navigation

### Start Here
- **[Executive Summary](deliverables/executive-summary.md)** — High-level overview for stakeholders
- **[System Overview](architecture/system-overview.md)** — Architecture, components, and trust boundaries

### Architecture & Data Flows
- **[System Overview](architecture/system-overview.md)** — 11 CloudFormation stacks, trust boundaries, data classification
- **[Data Flows](architecture/data-flows.md)** — All data flow diagrams with security analysis

### Feature-Specific Threats
- **[Audio Streaming](feature-threats/transcription-audio.md)** — WebSocket hijacking, stream interception, Kinesis injection
- **[Meeting Assistant](feature-threats/meeting-assistant.md)** — Prompt injection via transcripts, template tampering, tool abuse
- **[Virtual Participant](feature-threats/virtual-participant.md)** — Credential exposure, Chrome escape, unauthorized recording
- **[Voice Assistant](feature-threats/voice-assistant.md)** — Wake phrase spoofing, deepfake, vocalized secrets
- **[MCP Integration](feature-threats/mcp-integration.md)** — Data exfiltration, API key compromise, response injection
- **[Knowledge Base](feature-threats/knowledge-base.md)** — Cross-meeting leakage, KB poisoning, RAG injection
- **[Authentication](feature-threats/authentication-access.md)** — Self-registration abuse, JWT theft, access bypass
- **[Web UI](feature-threats/web-ui.md)** — XSS, subscription eavesdropping, iframe security
- **[Recording & Storage](feature-threats/recording-storage.md)** — Unauthorized access, consent, retention compliance
- **[Lambda Hooks](feature-threats/lambda-hooks.md)** — Hook exfiltration, tampering, IAM escalation

### Cross-Cutting Analysis
- **[STRIDE Analysis](threat-analysis/stride-analysis.md)** — Full STRIDE across all components
- **[Risk Matrix](risk-assessment/risk-matrix.md)** — Complete risk register with scoring and recommendations
- **[Threat ID Glossary](threat-id-glossary.md)** — All 54 threat IDs with quick reference

### Implementation
- **[Implementation Guide](deliverables/implementation-guide.md)** — Security controls, configuration, and checklists

## Threat Categories

| Prefix | Category | Count | Highest Risk | Document |
|--------|----------|-------|-------------|----------|
| AUDIO | Audio Streaming | 6 | High (6) | [transcription-audio.md](feature-threats/transcription-audio.md) |
| ASST | Meeting Assistant | 5 | Very High (9) | [meeting-assistant.md](feature-threats/meeting-assistant.md) |
| VP | Virtual Participant | 6 | Very High (9) | [virtual-participant.md](feature-threats/virtual-participant.md) |
| VOICE | Voice Assistant | 5 | High (6) | [voice-assistant.md](feature-threats/voice-assistant.md) |
| MCP | MCP Integration | 6 | Critical (8) | [mcp-integration.md](feature-threats/mcp-integration.md) |
| KB | Knowledge Base | 5 | Very High (9) | [knowledge-base.md](feature-threats/knowledge-base.md) |
| AUTH | Authentication | 6 | High (6) | [authentication-access.md](feature-threats/authentication-access.md) |
| UI | Web UI | 5 | High (6) | [web-ui.md](feature-threats/web-ui.md) |
| REC | Recording/Storage | 5 | Very High (9) | [recording-storage.md](feature-threats/recording-storage.md) |
| HOOK | Lambda Hooks | 5 | Critical (8) | [lambda-hooks.md](feature-threats/lambda-hooks.md) |

## Top 5 Priority Threats

| # | ID | Threat | Risk | Status |
|---|-----|--------|------|--------|
| 1 | ASST.T01 | Prompt injection via meeting transcript content | 9 | Mitigated |
| 2 | KB.T01 | Cross-meeting data leakage via Knowledge Base | 9 | Partially Mitigated |
| 3 | REC.T03 | Recording without consent (legal/privacy) | 9 | Partially Mitigated |
| 4 | VP.T04 | Unauthorized meeting recording via VP | 9 | Partially Mitigated |
| 5 | MCP.T01 | Data exfiltration via MCP tools | 8 | Partially Mitigated |

## Unique Risk Factors

LMA has several risk characteristics not common in typical SaaS applications:

| Factor | Why It Matters |
|--------|---------------|
| 🎙️ **Live audio as attack surface** | Verbal prompt injection cannot be blocked at input — it's the meeting conversation itself |
| 🤖 **Multi-platform bot credentials** | VP holds OAuth/password credentials for 5+ meeting platforms simultaneously |
| 🔊 **Audible AI responses** | Voice assistant discloses information to ALL meeting participants, not just requester |
| ⚖️ **Recording consent complexity** | Legal requirements vary by jurisdiction, participant count, and meeting type |
| 🔍 **Cross-meeting semantic search** | Single KB indexes all meetings without native per-meeting access control |
| 🌐 **Third-party voice APIs** | Meeting audio/text sent to ElevenLabs, Simli for synthesis and avatar |

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-05-18 | Initial threat model: 10 feature areas, 54 threats, STRIDE analysis, risk matrix |
