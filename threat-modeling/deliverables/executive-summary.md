# Threat Model — Executive Summary

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 1.0 |
| **Last Updated** | 2025-05-18 |
| **Classification** | Internal |
| **System** | Live Meeting Assistant (LMA) |

## 1. Purpose

This document provides an executive-level summary of the threat model for the Live Meeting Assistant (LMA), an AWS-based solution for real-time meeting transcription, AI-powered meeting assistance, and virtual meeting participation. The threat model identifies security risks across the system's architecture, features, and integrations, and documents the controls in place to mitigate them.

## 2. System Summary

The Live Meeting Assistant captures and processes live meeting audio through multiple channels (browser extension, Stream Audio tab, Virtual Participant bots), transcribes speech in real-time, and provides AI-powered meeting assistance including summaries, Q&A, action items, semantic search, and voice interaction.

### Key Metrics

| Metric | Value |
|--------|-------|
| **AWS Services Used** | 15+ (Transcribe, Bedrock, ECS Fargate, Lambda, DynamoDB, AppSync, Cognito, S3, Kinesis, OpenSearch, CloudFront, KMS, Translate, API Gateway, CloudWatch) |
| **CloudFormation Stacks** | 11 nested stacks |
| **Lambda Functions** | 19 |
| **AppSync Resolvers** | 39 |
| **ECS Fargate Services** | 2 (WebSocket server, Virtual Participant) |
| **AI/ML Models** | Claude 4.x, Nova, Nova Sonic, Transcribe, Translate |
| **Third-Party APIs** | 3 (ElevenLabs, Simli, Tavily) |
| **Meeting Platforms** | 5 (Zoom, Teams, Chime, Meet, WebEx) |
| **Auth Model** | Cognito (User Pool + Identity Pool, admin group) |

## 3. Threat Model Results

### 3.1 Threats Identified

| Category | Count |
|----------|-------|
| **Total threats identified** | **54** |
| Very High risk (score 8-9) | 6 |
| High risk (score 6) | 15 |
| Medium risk (score 3-4) | 26 |
| Low risk (score 1-2) | 7 |

### 3.2 STRIDE Distribution

| STRIDE Category | Count | Key Concern |
|----------------|-------|-------------|
| **Information Disclosure** | 19 | Cross-meeting data leakage, audio/transcript exfiltration, vocalized secrets |
| **Tampering** | 16 | Prompt injection via transcripts, KB poisoning, template manipulation |
| **Spoofing** | 9 | Wake phrase abuse, voice synthesis impersonation, credential theft |
| **Elevation of Privilege** | 9 | Agent tool abuse, meeting access bypass, hook privilege escalation |
| **Denial of Service** | 8 | Resource exhaustion, platform API abuse, processing overload |
| **Repudiation** | 5 | Recording without consent, speaker attribution manipulation |

### 3.3 Mitigation Status

| Status | Count | Percentage |
|--------|-------|------------|
| **Mitigated** | 33 | 61% |
| **Partially Mitigated** | 14 | 26% |
| **Accepted** | 7 | 13% |

## 4. Key Risk Areas

### 4.1 Prompt Injection via Meeting Transcripts (Highest Technical Risk)

Meeting participants can verbally speak prompt injection payloads that, once transcribed, manipulate the AI agent's behavior (ASST.T01). This is the #1 technical risk because the attack surface is the meeting conversation itself — an inherently untrusted input that must be processed by the LLM.

**Mitigations**: Bedrock Guardrails, system prompt hardening with clear input/output boundaries, transcript content marked as untrusted data, multi-layer injection detection.

### 4.2 Cross-Meeting Data Leakage (Highest Privacy Risk)

All meeting transcripts are indexed into a single Bedrock Knowledge Base without per-meeting access control at the retrieval layer (KB.T01). Any authenticated user can potentially search and retrieve content from meetings they weren't authorized to attend.

**Mitigations**: Meeting-level access control in DynamoDB, single-tenant deployment. **Gap**: KB query-time access filtering not yet implemented — metadata-based filtering recommended.

### 4.3 Recording Consent (Highest Legal/Compliance Risk)

LMA records meeting audio and generates transcripts by default (REC.T03, VP.T04). In two-party consent jurisdictions, this creates significant legal risk if participants are not properly notified and consent is not obtained.

**Mitigations**: VP bot identification in meeting lists, configurable recording enable/disable. **Gap**: No mandatory consent notification workflow — recommended for production deployments.

### 4.4 Data Exfiltration via Extensibility Points

MCP integrations (MCP.T01) and Lambda hooks (HOOK.T01) can send meeting transcript data to external systems. While designed for legitimate integrations, these channels create data exfiltration risk.

**Mitigations**: Admin-only configuration, audit logging, Bedrock Guardrails. **Gap**: VPC egress controls and endpoint allowlists not enforced by default.

### 4.5 Voice Assistant Information Disclosure

The voice assistant responds audibly in meetings (VOICE.T05). If it retrieves sensitive information from the KB or MCP tools and vocalizes it, that information is disclosed to ALL meeting participants — a unique risk not present in text-only systems.

**Mitigations**: Bedrock Guardrails, scope defaults to current meeting. **Gap**: Sensitivity classification for voice responses not yet implemented.

## 5. Unique Risk Factors for LMA

Compared to typical SaaS applications, LMA has several unique risk characteristics:

| Factor | Risk Impact | Unique To LMA |
|--------|-------------|---------------|
| **Live audio processing** | Real-time sensitive data flow, no retry opportunity | Audio capture and streaming |
| **Verbal prompt injection** | Attack vector is human speech — cannot be blocked at input | Transcript-based AI processing |
| **Multi-platform bot access** | VP has credentials to 5+ meeting platforms | Virtual Participant |
| **Audible AI responses** | Information disclosed to all meeting participants, not just requester | Voice Assistant |
| **Recording consent complexity** | Legal requirements vary by jurisdiction and participant count | Audio recording |
| **Cross-meeting search** | Single KB indexes all meetings without per-meeting ACL | Bedrock Knowledge Base |
| **Third-party voice synthesis** | Audio data sent to external APIs (ElevenLabs, Simli) | Voice/Avatar features |

## 6. Recommendations

### Immediate (Very High Risk, Partially Mitigated)

1. **Implement KB access filtering** — Add meeting-level metadata to indexed documents and enforce access control at query time
2. **Deploy consent workflow** — Mandatory notification before recording/transcription begins
3. **Add MCP egress controls** — VPC endpoint restrictions and data classification filtering
4. **Scope agent tools** — Per-meeting tool authorization and confirmation for high-impact calls

### Short-Term

5. **Migrate secrets to Secrets Manager** — ElevenLabs, Simli, Tavily API keys with rotation
6. **Voice response sensitivity filter** — Classify and filter KB/MCP results before vocalization
7. **VP credential hardening** — Secrets Manager integration, log scrubbing, credential rotation
8. **Unified data deletion** — API that purges meeting from all stores (S3 + DDB + KB)

### Ongoing

9. **Prompt injection monitoring** — Detect unusual agent behavior and tool invocation patterns
10. **Recording consent audit** — Verify consent mechanisms are active for all sessions
11. **KB access pattern monitoring** — Alert on cross-meeting data access anomalies
12. **MCP tool invocation review** — Regular audit of external tool calls and data volumes

## 7. Compliance

The threat model has been developed using:
- **STRIDE methodology** for systematic threat identification
- **Risk scoring** (Likelihood × Severity) for prioritization
- **AWS Well-Architected Framework** security pillar alignment
- **Privacy by Design** principles for audio/recording features
- **Multi-jurisdictional consent** consideration for recording features

## 8. Document References

| Document | Description |
|----------|-------------|
| [System Overview](../architecture/system-overview.md) | Architecture, components, trust boundaries |
| [Data Flows](../architecture/data-flows.md) | All data flow diagrams with security analysis |
| [STRIDE Analysis](../threat-analysis/stride-analysis.md) | Full STRIDE analysis across all components |
| [Risk Matrix](../risk-assessment/risk-matrix.md) | Complete risk register with scoring |
| [Implementation Guide](implementation-guide.md) | Security controls implementation details |
| [Threat ID Glossary](../threat-id-glossary.md) | All 54 threat IDs with cross-references |
