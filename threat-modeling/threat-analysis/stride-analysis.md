# STRIDE Threat Analysis

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 1.1 |
| **Last Updated** | 2026-11-25 |
| **Classification** | Internal |
| **Methodology** | STRIDE (Spoofing, Tampering, Repudiation, Information Disclosure, Denial of Service, Elevation of Privilege) |

## 1. Overview

This document provides a comprehensive STRIDE analysis across all components of the Live Meeting Assistant. Each STRIDE category is analyzed for the system's major components: audio streaming, transcription, AI meeting assistant, virtual participant, voice assistant, MCP integration, knowledge base, authentication, web UI, recording/storage, and Lambda hooks.

## 2. Spoofing

Spoofing threats involve an attacker pretending to be something or someone they are not.

### 2.1 Identity Spoofing

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **WebSocket connection hijacking** with stolen meeting tokens | Audio Streaming | High (6) | TLS/WSS, meeting tokens, ALB security groups, optional WAF |
| **Self-registration abuse** with spoofed email domain | Authentication | High (6) | Domain restrictions, email verification, optional pre-signup Lambda |
| **JWT token theft** via XSS enabling session hijacking | Authentication | High (6) | Short-lived tokens, React XSS protection, CSP headers |
| **MCP API key compromise** impersonating authorized clients | MCP Integration | High (6) | SHA-256 hashing, rate limiting, Lambda Authorizer |
| **Wake phrase spoofing** triggering unauthorized voice assistant activation | Voice Assistant | High (6) | Configurable wake phrase, admin-controlled activation |
| **WAF bypass** via direct endpoint access | Authentication | Low (2) | Defense-in-depth (WAF supplementary), Cognito auth required |
| **Consent given in advance for the wrong person** — a forwarded consent link or a shared sign-in | Recording/Storage | Medium (3) | Verified address on the participant list, text hash check, longest period, withdrawal, objection always wins |
| **Display-name match** — an attendee shows a listed person's name to be counted under their consent given in advance | Virtual Participant | Medium (3) | Documented design limit: unique match only, notice always posted, covered names posted, `MatchBasis` recorded, objection always wins |

### 2.2 Service Spoofing

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **MCP response injection** — external server returns manipulated data | MCP Integration | High (6) | Response validation, Guardrails, size limits |
| **Voice synthesis impersonation** via custom voice models | Voice Assistant | Medium (4) | Admin-only config, bot identification, approved voice list |
| **Meeting platform impersonation** tricking VP into connecting to fake meeting | Virtual Participant | Low (2) | Platform-specific URL validation, HTTPS verification |

## 3. Tampering

Tampering threats involve unauthorized modification of data or code.

### 3.1 Data Tampering

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Prompt injection via meeting transcript** — spoken content manipulates agent | Meeting Assistant | Very High (9) | Bedrock Guardrails, prompt hardening, input/output tagging |
| **LLM prompt template tampering** — malicious system prompts | Meeting Assistant | Medium (4) | Admin-only access, KMS encryption, audit logging |
| **Kinesis stream injection** — fabricated transcript events | Audio Streaming | Medium (3) | IAM least-privilege, KMS encryption, event source validation |
| **KB poisoning via transcript** — false info indexed for future retrieval | Knowledge Base | High (6) | Source attribution, meeting metadata, curation process |
| **RAG context injection** — prompt injection via retrieved KB content | Knowledge Base | High (6) | Context isolation, Guardrails, output validation |
| **Hook result tampering** — modified transcripts returned by hook | Lambda Hooks | Medium (3) | Original preservation, schema validation, diff logging |
| **Speaker attribution manipulation** — audio channel swapping | Audio Streaming | Low (2) | Two-channel separation, diarization confidence scores |
| **Avatar video manipulation** — tampered visual content | Voice Assistant | Low (2) | TLS to Simli, admin-only config |
| **Private chat instructions** — commands, questions or consent replies sent to the VP privately | Virtual Participant | Low (2) | VP acts only on messages sent to everyone, posts nothing in reply |
| **Browser-side script loading** — content loading scripts or connecting to origins the web UI does not use | Web UI | Medium (3) | Content-Security-Policy and response headers policy, header tests |

### 3.2 Configuration Tampering

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **MCP server registration** — adding malicious tool endpoints | MCP Integration | Medium (3) | IaC-only configuration, admin access, audit logging |
| **Voice assistant configuration** — unauthorized voice/behavior changes | Voice Assistant | Medium (3) | Admin-only settings, configuration audit trail |

## 4. Repudiation

Repudiation threats involve users denying they performed an action.

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Recording without consent** — no proof participants were notified | Recording/Storage | Very High (9) | VP bot identification, configurable consent workflow |
| **Speaker attribution manipulation** — "I never said that" | Audio Streaming | Low (2) | Two-channel audio, diarization, confidence scores |
| **Meeting data deletion** without audit trail | Recording/Storage | Medium (3) | CloudTrail logging, DynamoDB streams |
| **VP launch without justification** — who authorized the bot? | Virtual Participant | Medium (3) | Admin and User roles only, launching user recorded on the VP, CloudWatch logging, audit trail |
| **Hook modification untracked** — changes without logging | Lambda Hooks | Low (2) | Invocation logging, result comparison |
| **Dormant accounts and keys unnoticed** — no record of last use for access reviews | Authentication | Medium (4) | Last sign-in, last active and API key last use recorded; `lma users list` export |
| **Consent given in advance disputed** — which text was agreed to, and when it was withdrawn | Recording/Storage | Medium (3) | Exact text, hash and wording version kept; replaced and withdrawn consents kept with times |
| **Private consent replies** — a reply others in the meeting cannot see | Virtual Participant | Low (2) | Only replies sent to everyone are recorded |

## 5. Information Disclosure

Information disclosure threats involve exposure of sensitive data to unauthorized parties.

### 5.1 Data Exposure

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Cross-meeting data leakage via KB** — search returns other meetings' content | Knowledge Base | Very High (9) | Meeting access control, metadata filtering (partial) |
| **Data exfiltration via MCP tools** — meeting content sent externally | MCP Integration | Critical (8) | Admin-only config, audit logging, Guardrails |
| **Data exfiltration via Lambda hook** — transcript sent externally | Lambda Hooks | Critical (8) | Customer responsibility, VPC recommendation |
| **Audio recording unauthorized access** — S3 bucket exposure | Recording/Storage | Medium (4) | KMS encryption, block public access, IAM scoping |
| **Audio stream interception** — MITM on WebSocket | Audio Streaming | Medium (4) | TLS 1.2+, certificate validation |
| **Meeting sharing over-permission** — too broad access grants | Recording/Storage | Medium (4) | User-level sharing, owner revocation |
| **Sensitive info vocalized** — assistant speaks confidential data in meetings | Voice Assistant | High (6) | Guardrails, scope to current meeting, sensitivity filtering |
| **VP meeting credential exposure** — leaked in logs/env vars | Virtual Participant | High (6) | KMS encryption, ephemeral containers, log scrubbing |
| **Third-party API key exposure** — ElevenLabs/Simli/Tavily keys | Voice Assistant | Medium (4) | NoEcho params, env var encryption, Secrets Manager |
| **DynamoDB over-access** — Lambda reads all meeting data | Recording/Storage | Medium (3) | Per-table IAM, KMS encryption |
| **S3 Vectors data exposure** — direct vector store access | Knowledge Base | Medium (3) | IAM policies, encryption |
| **AppSync subscription eavesdropping** — listen to other meetings | Web UI | High (6) | Subscription authorization, user-scoped filters |
| **Automatic sharing over-reach** — a meeting shared with an unintended listed address | Recording/Storage | Medium (4) | Owner/Admin-only list, `AutoSharedWith` removal, manual shares respected, setting can be turned off |
| **Participant view over-exposure** — fields or meetings beyond the share | Web UI | Medium (3) | Participant-only operations, allowlisted types, per-message sharing filter on the live transcript |
| **Participant recording links** — links beyond the share | Recording/Storage | Low (2) | Server-signed, one hour, only for a shared meeting, only under the playback prefixes |
| **Restorable deleted content** — point-in-time recovery keeps deleted items | Recording/Storage | Medium (3) | `MeetingDataPointInTimeRecoveryDays` 0-35, restore needs account permissions |
| **Account lookup** — learning who has an LMA account from a participant list | Authentication | Low (2) | Owner/Admin only, status only, 1,000 addresses a day, hashed counters |

### 5.2 Session/Configuration Exposure

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **CloudFront config exposure** — endpoints visible in JS | Web UI | Low (2) | All endpoints require auth |
| **GraphQL schema introspection** — API structure disclosed | Web UI | Medium (4) | Auth required, consider disabling introspection |
| **Cross-meeting MCP data access** — tools return wrong meetings' data | MCP Integration | High (6) | User identity context, meeting-level scoping |

## 6. Denial of Service

Denial of service threats involve making the system unavailable.

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Transcribe session exhaustion** — too many concurrent streams | Audio Streaming | Medium (4) | Service quotas, ALB security groups, WAF rate limiting |
| **Automatic processing overload** — excessive Bedrock invocations | Meeting Assistant | Medium (4) | Reserved concurrency, batch sizing, backpressure |
| **VP container resource exhaustion** — Chrome OOM | Virtual Participant | Low (2) | Task sizing, health checks, auto-recovery |
| **Meeting platform API abuse** — rate limits / IP bans | Virtual Participant | Medium (4) | Per-platform rate limits, NAT IP management |
| **MCP API DDoS** — overwhelming API Gateway | MCP Integration | Low (2) | Rate limiting, Lambda concurrency, WAF |
| **KB query overload** — S3 Vectors throttling / cost spike | Knowledge Base | Low (1) | Query limits, circuit breaker, throttle alarms |
| **Hook failure cascade** — blocking real-time pipeline | Lambda Hooks | Medium (4) | Error handling, timeout, circuit breaker |
| **GraphQL API abuse** — expensive queries | Web UI | Medium (4) | Rate limiting, query depth limits, auth |

## 7. Elevation of Privilege

Elevation of privilege threats involve gaining capabilities beyond what was authorized.

| Threat | Component | Risk | Mitigations |
|--------|-----------|------|-------------|
| **Admin group self-promotion** — adding self to admin Cognito group | Authentication | Medium (4) | IAM-protected admin APIs, CloudTrail logging |
| **Meeting access control bypass** — accessing unauthorized meetings | Authentication | High (6) | Resolver auth, meeting access validation, UUID IDs |
| **Agent tool abuse** — manipulated context triggers unauthorized tools | Meeting Assistant | High (6) | Tool parameter validation, Guardrails, allowlists |
| **Hook IAM over-privilege** — hook accessing platform resources | Lambda Hooks | Medium (3) | Separate roles, resource policies |
| **MCP unauthorized tool registration** — adding malicious tools | MCP Integration | Medium (3) | IaC-managed, admin-only deployment |
| **Prompt injection → tool execution** — transcript manipulation triggers actions | Meeting Assistant | Very High (9) | Guardrails, prompt hardening, tool confirmation |
| **Participant role over-reach** — a Participant reaching member operations | Authentication | Medium (3) | Role in every token, roles named on every operation, role matrix tests, no-permission identity pool role |
| **Participant account creation over-reach** — accounts for unlisted addresses or other roles | Authentication | Low (2) | Listed addresses only, Participant role only, per-day limits |
| **VP unauthorized launch** — a role without launch rights starting virtual participants | Virtual Participant | Medium (3) | Roles named on the launch operations (Admin and User), Participant refused |

## 8. Cross-Cutting Threats

### 8.1 Supply Chain Threats

| Threat | Impact | Mitigations |
|--------|--------|-------------|
| **Hook Lambda dependency compromise** | Transcript data exfiltration | Customer responsibility, documentation |
| **Chromium browser vulnerability** | Container compromise in VP | Regular image updates; container + network isolation (browser runs --no-sandbox) |
| **React dependency compromise** | XSS in web UI | Lock files, dependency scanning, CSP |
| **Browser extension code tampering** | Audio interception | Extension signing, code review |

### 8.2 AI/ML-Specific Threats

| Threat | Impact | Mitigations |
|--------|--------|-------------|
| **Indirect prompt injection via transcripts** | Agent behavior manipulation | Guardrails, context isolation, output validation |
| **Model hallucination** | False meeting records | Citation requirements, confidence scoring, full transcript access |
| **Voice deepfake via TTS** | Participant impersonation | Approved voice list, watermarking, bot identification |
| **KB context injection** | Agent manipulation via stored content | RAG isolation, injection pattern detection |

### 8.3 Privacy & Compliance Threats

| Threat | Impact | Mitigations |
|--------|--------|-------------|
| **Recording without consent** | Legal liability, fines | Meeting Consent modes, notices, objections, consent given in advance with withdrawal, documentation |
| **Data retention non-compliance** | Regulatory penalties | TTL configuration, transcript and recordings deletion after the summary, recovery window parameter, lifecycle policies |
| **Cross-jurisdictional data flow** | GDPR/privacy violations | Geo restrictions, regional deployment, data residency |
| **PII in transcripts/summaries** | Privacy breaches | Guardrails PII detection, configurable redaction |

### 8.4 Infrastructure Threats

| Threat | Impact | Mitigations |
|--------|--------|-------------|
| **CloudFormation stack manipulation** | Full system compromise | IAM, stack policies, CloudFormation service role |
| **KMS key policy misconfiguration** | Data decryption by unauthorized parties | Key policy audit, minimal grants, rotation |
| **VPC/Security Group misconfiguration** | Network-level exposure | IaC-defined groups, regular audit |
| **NAT Gateway single point of failure** | VP connectivity loss | Multi-AZ deployment, monitoring |
