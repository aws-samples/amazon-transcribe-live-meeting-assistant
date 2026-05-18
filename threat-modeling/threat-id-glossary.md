# Threat ID Glossary

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 1.0 |
| **Last Updated** | 2025-05-18 |
| **Classification** | Internal |
| **Total Threat IDs** | 54 |

## 1. Threat ID Naming Convention

Threat IDs follow the pattern: `{CATEGORY}.T{NN}`

| Prefix | Category | Scope | Document |
|--------|----------|-------|----------|
| **AUDIO** | Audio Streaming | WebSocket, Transcribe, Kinesis, recording pipeline | [transcription-audio.md](feature-threats/transcription-audio.md) |
| **ASST** | Meeting Assistant | Strands Agents, Bedrock, prompt injection, tool abuse | [meeting-assistant.md](feature-threats/meeting-assistant.md) |
| **VP** | Virtual Participant | Headless Chrome, meeting platform interaction, credentials | [virtual-participant.md](feature-threats/virtual-participant.md) |
| **VOICE** | Voice Assistant | Nova Sonic, ElevenLabs, Simli, wake phrase, vocalization | [voice-assistant.md](feature-threats/voice-assistant.md) |
| **MCP** | MCP Integration | API Gateway, external tools, data exfiltration | [mcp-integration.md](feature-threats/mcp-integration.md) |
| **KB** | Knowledge Base | Bedrock KB, RAG, cross-meeting search, OpenSearch | [knowledge-base.md](feature-threats/knowledge-base.md) |
| **AUTH** | Authentication | Cognito, JWT, RBAC, meeting access control, WAF | [authentication-access.md](feature-threats/authentication-access.md) |
| **UI** | Web UI | React, CloudFront, AppSync, iframe, subscriptions | [web-ui.md](feature-threats/web-ui.md) |
| **REC** | Recording/Storage | S3 audio, DynamoDB, retention, consent, sharing | [recording-storage.md](feature-threats/recording-storage.md) |
| **HOOK** | Lambda Hooks | Customer extensibility, data exfiltration, tampering | [lambda-hooks.md](feature-threats/lambda-hooks.md) |

## 2. Complete Threat ID Reference

### AUDIO — Audio Streaming (6 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| AUDIO.T01 | WebSocket connection hijacking | Spoofing | 6 (High) |
| AUDIO.T02 | Audio stream interception (eavesdropping) | ID | 4 (Medium) |
| AUDIO.T03 | Kinesis stream injection/tampering | Tampering | 3 (Medium) |
| AUDIO.T04 | Transcribe service abuse / cost escalation | DoS | 4 (Medium) |
| AUDIO.T05 | Audio recording unauthorized access | ID | 4 (Medium) |
| AUDIO.T06 | Speaker attribution manipulation | Tampering, Repudiation | 2 (Low) |

### ASST — Meeting Assistant (5 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| ASST.T01 | Prompt injection via meeting transcript content | Tampering, EoP | 9 (Very High) |
| ASST.T02 | LLM prompt template tampering | Tampering, EoP | 4 (Medium) |
| ASST.T03 | Agent tool abuse via manipulated context | EoP | 6 (High) |
| ASST.T04 | Model hallucination in meeting summaries | Tampering | 4 (Medium) |
| ASST.T05 | Automatic processing resource exhaustion | DoS | 4 (Medium) |

### VP — Virtual Participant (6 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| VP.T01 | Meeting credential exposure | ID | 6 (High) |
| VP.T02 | Headless Chrome sandbox escape | EoP | 4 (Medium) |
| VP.T03 | Meeting platform API abuse | DoS, Spoofing | 4 (Medium) |
| VP.T04 | Unauthorized meeting recording via VP | ID, Repudiation | 9 (Very High) |
| VP.T05 | Voice assistant impersonation | Spoofing, Tampering | 3 (Medium) |
| VP.T06 | Container resource exhaustion | DoS | 2 (Low) |

### VOICE — Voice Assistant (5 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| VOICE.T01 | Wake phrase spoofing / false activation | Spoofing | 6 (High) |
| VOICE.T02 | Voice synthesis deepfake abuse | Spoofing, Tampering | 4 (Medium) |
| VOICE.T03 | Third-party API key exposure (ElevenLabs/Simli) | ID | 4 (Medium) |
| VOICE.T04 | Avatar video stream manipulation | Tampering, Spoofing | 2 (Low) |
| VOICE.T05 | Sensitive information vocalized in meetings | ID | 6 (High) |

### MCP — MCP Integration (6 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| MCP.T01 | Meeting data exfiltration via MCP tools | ID | 8 (Critical) |
| MCP.T02 | MCP API key compromise (inbound) | Spoofing, EoP | 6 (High) |
| MCP.T03 | MCP response injection (prompt injection via tool) | Tampering | 6 (High) |
| MCP.T04 | Rate limit bypass / DDoS via MCP API | DoS | 2 (Low) |
| MCP.T05 | Unauthorized MCP tool registration | Tampering, EoP | 3 (Medium) |
| MCP.T06 | Cross-meeting data access via MCP tools | ID | 6 (High) |

### KB — Knowledge Base (5 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| KB.T01 | Cross-meeting data leakage via Knowledge Base | ID | 9 (Very High) |
| KB.T02 | Knowledge Base poisoning via transcript | Tampering | 6 (High) |
| KB.T03 | RAG context injection (indirect prompt injection) | Tampering, EoP | 6 (High) |
| KB.T04 | OpenSearch Serverless data exposure | ID | 3 (Medium) |
| KB.T05 | Knowledge Base denial of service | DoS | 1 (Low) |

### AUTH — Authentication & Access Control (6 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| AUTH.T01 | Self-registration abuse | Spoofing | 6 (High) |
| AUTH.T02 | JWT token theft / session hijacking | Spoofing, ID | 6 (High) |
| AUTH.T03 | Meeting access control bypass | EoP | 6 (High) |
| AUTH.T04 | Admin group privilege escalation | EoP | 4 (Medium) |
| AUTH.T05 | WebSocket authentication weakness | Spoofing | 3 (Medium) |
| AUTH.T06 | WAF bypass / IP restriction evasion | Spoofing | 2 (Low) |

### UI — Web UI (5 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| UI.T01 | Cross-site scripting (XSS) via transcript content | Tampering, ID | 3 (Medium) |
| UI.T02 | AppSync subscription eavesdropping | ID | 6 (High) |
| UI.T03 | Iframe embedding security | ID, Tampering | 4 (Medium) |
| UI.T04 | GraphQL API abuse / introspection | ID, DoS | 4 (Medium) |
| UI.T05 | CloudFront configuration exposure | ID | 2 (Low) |

### REC — Recording & Storage (5 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| REC.T01 | Unauthorized audio recording access | ID | 4 (Medium) |
| REC.T02 | Meeting data retention compliance violation | ID, Repudiation | 6 (High) |
| REC.T03 | Recording without consent | ID, Repudiation | 9 (Very High) |
| REC.T04 | Meeting sharing over-permission | ID | 4 (Medium) |
| REC.T05 | DynamoDB data exposure via IAM | ID | 3 (Medium) |

### HOOK — Lambda Hooks (5 threats)

| ID | Short Name | STRIDE | Risk |
|----|-----------|--------|------|
| HOOK.T01 | Data exfiltration via hook Lambda | ID | 8 (Critical) |
| HOOK.T02 | Hook result tampering | Tampering | 3 (Medium) |
| HOOK.T03 | Hook Lambda failure cascade | DoS | 4 (Medium) |
| HOOK.T04 | Hook IAM over-privilege accessing platform resources | EoP | 3 (Medium) |
| HOOK.T05 | Supply chain attack via hook dependencies | Tampering | 3 (Medium) |

## 3. STRIDE Abbreviations

| Abbreviation | Full Name |
|-------------|-----------|
| **S** | Spoofing |
| **T** | Tampering |
| **R** | Repudiation |
| **ID** | Information Disclosure |
| **DoS** | Denial of Service |
| **EoP** | Elevation of Privilege |
