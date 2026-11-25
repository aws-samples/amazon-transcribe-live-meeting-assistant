# Threat Model — Executive Summary

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 1.1 |
| **Last Updated** | 2026-11-25 |
| **Classification** | Internal |
| **System** | Live Meeting Assistant (LMA) |

## 1. Purpose

This document provides an executive-level summary of the threat model for the Live Meeting Assistant (LMA), an AWS-based solution for real-time meeting transcription, AI-powered meeting assistance, and virtual meeting participation. The threat model identifies security risks across the system's architecture, features, and integrations, and documents the controls in place to mitigate them.

## 2. System Summary

The Live Meeting Assistant captures and processes live meeting audio through multiple channels (browser extension, Stream Audio tab, Virtual Participant bots), transcribes speech in real-time, and provides AI-powered meeting assistance including summaries, Q&A, action items, semantic search, and voice interaction.

### Key Metrics

| Metric | Value |
|--------|-------|
| **AWS Services Used** | 15+ (Transcribe, Bedrock, ECS Fargate, Lambda, DynamoDB, AppSync, Cognito, S3, S3 Vectors, Kinesis, CloudFront, KMS, Translate, API Gateway, CloudWatch) |
| **CloudFormation Stacks** | 11 nested stacks |
| **Lambda Functions** | 19 |
| **AppSync Resolvers** | 39 |
| **ECS Fargate Services** | 2 (WebSocket server, Virtual Participant) |
| **AI/ML Models** | Claude 4.x, Nova, Nova Sonic, Transcribe, Translate |
| **Third-Party APIs** | 3 (ElevenLabs, Simli, Tavily) |
| **Meeting Platforms** | 5 via Virtual Participant (Zoom, Teams, Chime, WebEx, Google Meet); all five via the browser extension |
| **Auth Model** | Cognito (User Pool + Identity Pool), three roles (Admin, User, Participant) named on every operation |

## 3. Threat Model Results

### 3.1 Threats Identified

| Category | Count |
|----------|-------|
| **Total threats identified** | **65** |
| Very High risk (score 8-9) | 6 |
| High risk (score 6) | 15 |
| Medium risk (score 3-4) | 34 |
| Low risk (score 1-2) | 10 |

### 3.2 STRIDE Distribution

| STRIDE Category | Count | Key Concern |
|----------------|-------|-------------|
| **Information Disclosure** | 28 | Cross-meeting data leakage, audio/transcript exfiltration, vocalized secrets, sharing with listed participants |
| **Tampering** | 18 | Prompt injection via transcripts, KB poisoning, template manipulation |
| **Spoofing** | 13 | Wake phrase abuse, voice synthesis impersonation, credential theft, display-name consent match |
| **Elevation of Privilege** | 12 | Agent tool abuse, meeting access bypass, hook privilege escalation, role boundaries |
| **Denial of Service** | 8 | Resource exhaustion, platform API abuse, processing overload |
| **Repudiation** | 7 | Recording without consent, speaker attribution manipulation, consent records, access reviews |

### 3.3 Mitigation Status

| Status | Count | Percentage |
|--------|-------|------------|
| **Mitigated** | 42 | 64% |
| **Partially Mitigated** | 16 | 25% |
| **Accepted** | 7 | 11% |

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

### 4.6 Participant Access, Sharing and Consent Given in Advance

**Threats**: AUTH.T07, AUTH.T08, UI.T06, REC.T06, REC.T07, REC.T09, VP.T07

People outside the deploying organization can now hold a Participant account, be shared a meeting automatically from its participant list, and consent before a meeting. **Mitigations**: one role per token and roles named on every operation, tested against the role matrix; Participant-only operations returning allowlisted types, with the live transcript filtered on each line's sharing list and recording links signed by the server for an hour; automatic shares recorded on the meeting and removed when a person leaves the list; consent given in advance only from a verified address on the list, with the exact text kept, a longest period, and withdrawal at any time. **Documented design limit**: a Virtual Participant matches an attendee to a consent given in advance by display name; the notice is always posted, the people counted are named in the meeting, and an objection always wins.

### 4.7 Retention and Access Review Controls

**Threats**: REC.T02, REC.T08, AUTH.T09, UI.T07

**Mitigations**: deletion of a meeting's transcript and recordings after the summary; the `MeetingDataPointInTimeRecoveryDays` stack parameter for how long deleted meeting content stays restorable; last sign-in, last activity and MCP API key last use recorded for access reviews and exported by `lma users list`; a Content-Security-Policy and cross-origin and transport headers on the web UI.

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
| [Threat ID Glossary](../threat-id-glossary.md) | All 65 threat IDs with cross-references |
