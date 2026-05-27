# Security Controls Implementation Guide

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 1.0 |
| **Last Updated** | 2025-05-18 |
| **Classification** | Internal |

## 1. Overview

This guide details the security controls implemented in the Live Meeting Assistant to mitigate the 54 identified threats. Controls are organized by security domain and mapped to the specific threats they address.

## 2. Authentication & Identity (AUTH)

### 2.1 Amazon Cognito User Pool

**Threats mitigated**: AUTH.T01, AUTH.T02, AUTH.T04, AUTH.T05

| Control | Implementation | Configuration |
|---------|---------------|---------------|
| Email domain restriction | `AllowedSignUpEmailDomain` parameter | CloudFormation parameter |
| Email verification required | Cognito user pool verification settings | Cognito config |
| Optional MFA | TOTP/SMS multi-factor authentication | Cognito config |
| Password policy | Minimum length, complexity requirements | Cognito config |
| Token lifetimes | Access: 1 hour, Refresh: configurable | Cognito config |
| Admin group | Separate Cognito group for privileged operations | CloudFormation |
| Self-registration control | Optional with domain restrictions | Deployment parameter |

### 2.2 Access Control Model

**Threats mitigated**: AUTH.T03, AUTH.T04, VP.T04, ASST.T02

| Level | Mechanism | Scope |
|-------|-----------|-------|
| **Admin group** | Cognito group membership | VP launch, LLM templates, MCP config, user management |
| **Authenticated user** | Valid Cognito JWT | Meeting creation, transcription, assistant use |
| **Meeting owner** | DynamoDB meeting record creator | Full meeting access, sharing, deletion |
| **Shared user** | DynamoDB access grant | Read access to specific meetings |

**Enforcement layers**:
1. **AppSync resolver authorization**: All 39 resolvers check JWT and group claims
2. **Lambda authorization**: Business logic verifies meeting access from DynamoDB
3. **WebSocket authentication**: Meeting-specific connection tokens validated
4. **MCP API Gateway**: Custom Lambda Authorizer with API key validation

### 2.3 MCP API Key Authentication

**Threats mitigated**: MCP.T02, MCP.T04

| Control | Implementation |
|---------|---------------|
| **SHA-256 hashing** | API keys stored as SHA-256 hash in DynamoDB (never plaintext) |
| **Custom Lambda Authorizer** | Validates key hash, extracts permissions, generates IAM policy |
| **Rate limiting** | Per-key throttling (default 100 req/sec) |
| **Access logging** | CloudWatch access logs for all API calls |
| **Request validation** | API Gateway request validation enabled |

## 3. Network Security

### 3.1 VPC Architecture

**Threats mitigated**: VP.T02, AUDIO.T01, MCP.T01

| Control | Implementation |
|---------|---------------|
| **Public subnets** | ALB only (no application workloads) |
| **Private subnets** | All Fargate tasks (WebSocket server, VP) |
| **NAT Gateway** | Controlled internet egress for private subnets |
| **Security groups** | Per-service inbound/outbound restrictions |
| **Multi-AZ** | 2 availability zones for resilience |

### 3.2 ALB Security

**Threats mitigated**: AUDIO.T01, AUDIO.T04, AUTH.T05

| Control | Implementation |
|---------|---------------|
| **HTTPS/WSS only** | TLS 1.2+ listener, HTTP redirected |
| **Security groups** | Restrict inbound to required CIDR ranges |
| **Access logging** | ALB access logs to S3 |
| **Health checks** | Regular health monitoring of Fargate targets |
| **Idle timeout** | Configurable WebSocket idle timeout |

### 3.3 Optional WAFv2

**Threats mitigated**: AUTH.T06, AUDIO.T04, UI.T04

| Control | Implementation |
|---------|---------------|
| **IP allowlisting** | Restrict access to corporate IP ranges |
| **Rate limiting** | Request rate rules per IP |
| **Geo restrictions** | CloudFront geographic access control |
| **Bot control** | Optional AWS managed bot control rules |

## 4. Data Protection

### 4.1 Encryption at Rest

**Threats mitigated**: AUDIO.T05, REC.T01, REC.T05, KB.T04

| Resource | Encryption | Key |
|----------|------------|-----|
| DynamoDB tables | AWS KMS | Customer-managed key |
| S3 audio recordings | SSE-KMS | Customer-managed key |
| S3 UI assets | SSE-S3 | AWS-managed |
| Kinesis Data Stream | KMS | Customer-managed key |
| CloudWatch Logs | KMS | Customer-managed key |
| S3 Vectors (KB index) | Encryption at rest | AWS-managed |

### 4.2 Encryption in Transit

| Resource | Protocol |
|----------|----------|
| Audio streaming | WSS (WebSocket Secure / TLS 1.2+) |
| AppSync API | HTTPS / WSS |
| CloudFront | HTTPS only (TLS 1.2+) |
| AWS service calls | TLS via AWS SDK |
| Third-party APIs | HTTPS (ElevenLabs, Simli, Tavily) |
| MCP API Gateway | HTTPS |

### 4.3 Data Retention

**Threats mitigated**: REC.T02

| Data | Retention Mechanism | Default |
|------|-------------------|---------|
| Meeting transcripts | DynamoDB TTL | 90 days |
| Meeting metadata | DynamoDB TTL | 90 days |
| Audio recordings | S3 lifecycle policy | Configurable |
| CloudWatch Logs | Log group retention | Configurable |
| KB vectors | Manual sync management | Persistent |

### 4.4 Data Isolation

**Threats mitigated**: KB.T01, AUTH.T03, UI.T02

| Mechanism | Implementation |
|-----------|---------------|
| **Single-tenant** | One CloudFormation stack per environment |
| **Meeting-scoped access** | DynamoDB access records per meeting |
| **User-scoped subscriptions** | AppSync subscription filters by user |
| **UUID meeting IDs** | Non-predictable meeting identifiers |

## 5. AI/ML Security

### 5.1 Prompt Injection Defense

**Threats mitigated**: ASST.T01, KB.T03, MCP.T03

| Layer | Control |
|-------|---------|
| **System prompt hardening** | Clear role boundaries, input/output tags separating transcript from instructions |
| **Bedrock Guardrails** | Content filtering, topic denial, PII detection |
| **Context isolation** | RAG/KB content marked as reference data, not instructions |
| **Output validation** | Schema validation on agent outputs |
| **Transcript as data** | System prompts explicitly mark transcript content as untrusted data |
| **Multi-layer detection** | Injection pattern monitoring, canary tokens |

### 5.2 Agent Security (Strands)

**Threats mitigated**: ASST.T03, MCP.T01, MCP.T03

| Control | Implementation |
|---------|---------------|
| **Tool parameter validation** | Schema enforcement on all tool inputs |
| **Bedrock Guardrails** | Applied to all agent interactions |
| **Audit logging** | All tool invocations logged with parameters |
| **Rate limiting** | Tool invocation frequency limits |
| **Scoped context** | Agent context restricted to relevant meeting data |

### 5.3 Knowledge Base Security

**Threats mitigated**: KB.T01, KB.T02, KB.T03, KB.T04

| Control | Implementation |
|---------|---------------|
| **S3 Vectors encryption** | Encryption at rest for vector store |
| **IAM access policy** | Scoped to KB service role only |
| **Meeting metadata** | Source attribution on indexed content |
| **Context isolation** | Retrieved content marked as reference in prompts |

## 6. Audio & Recording Security

### 6.1 Audio Streaming

**Threats mitigated**: AUDIO.T01, AUDIO.T02, AUDIO.T03

| Control | Implementation |
|---------|---------------|
| **WSS encryption** | TLS 1.2+ on all WebSocket connections |
| **Meeting tokens** | Per-session authentication for connections |
| **ALB security groups** | Network-level access restriction |
| **KMS-encrypted Kinesis** | Stream data encrypted at rest |
| **IAM scoping** | Only Fargate tasks can write to Kinesis |

### 6.2 Recording Security

**Threats mitigated**: REC.T01, REC.T03, AUDIO.T05

| Control | Implementation |
|---------|---------------|
| **KMS encryption** | Customer-managed key for all recordings |
| **Block public access** | S3 bucket public access blocked |
| **IAM least-privilege** | Per-function S3 access scoping |
| **CloudTrail data events** | Audit log for all S3 access |
| **Bot identification** | VP identified in meeting participant list |
| **Configurable recording** | Enable/disable recording per deployment |

### 6.3 Consent Management

**Threats mitigated**: REC.T03, VP.T04

| Control | Implementation |
|---------|---------------|
| **VP display name** | Clear bot identification in meetings |
| **Recording configuration** | Admin-configurable recording behavior |
| **Documentation** | Consent requirements documented in deployment guide |
| **Audit trail** | VP launch and recording start events logged |

**Recommended enhancements**:
- Mandatory consent notification before recording
- Configurable consent acknowledgment workflow
- Meeting host approval for VP join
- Audio announcement of recording status

## 7. Virtual Participant Security

### 7.1 Container Security

**Threats mitigated**: VP.T01, VP.T02, VP.T06

| Control | Implementation |
|---------|---------------|
| **Private subnet** | VP tasks in private subnets (NAT egress) |
| **Ephemeral containers** | Fresh container per meeting (no persistence) |
| **Chrome sandbox** | Browser sandbox enabled (not --no-sandbox) |
| **IAM least-privilege** | Minimal task role (write WebSocket, read config) |
| **Health monitoring** | ECS health checks with task restart |
| **Resource limits** | CPU/memory sizing per Fargate task |

### 7.2 Credential Management

**Threats mitigated**: VP.T01, VP.T03

| Control | Implementation |
|---------|---------------|
| **Encrypted parameters** | Meeting URLs/credentials encrypted in task config |
| **KMS log encryption** | CloudWatch Logs encrypted with customer key |
| **No persistent storage** | Credentials exist only in task memory |
| **Admin-only launch** | Only admin group can start VP sessions |

## 8. Extensibility Security

### 8.1 Lambda Hooks

**Threats mitigated**: HOOK.T01, HOOK.T02, HOOK.T03, HOOK.T04, HOOK.T05

| Control | Implementation |
|---------|---------------|
| **Invocation-only** | Platform has only `lambda:InvokeFunction` on hook ARN |
| **Separate IAM** | Hook uses customer-managed IAM role |
| **Error handling** | Continue processing on hook failure (graceful degradation) |
| **Timeout limits** | Configurable hook invocation timeout |
| **Output validation** | Schema validation on hook return values |
| **Original preservation** | Pre-hook transcript available independently |

### 8.2 MCP Integration

**Threats mitigated**: MCP.T01, MCP.T02, MCP.T03, MCP.T05, MCP.T06

| Control | Implementation |
|---------|---------------|
| **IaC-managed servers** | MCP server definitions in CloudFormation |
| **API key auth** | SHA-256 hashed keys with custom authorizer |
| **Rate limiting** | Per-key and global throttling |
| **Response validation** | Size limits, format validation |
| **Access logging** | All MCP calls logged to CloudWatch |
| **Bedrock Guardrails** | Content filtering on tool I/O |

## 9. Third-Party API Security

### 9.1 API Key Management

**Threats mitigated**: VOICE.T03

| API | Current Storage | Recommended |
|-----|----------------|-------------|
| **ElevenLabs** | CloudFormation NoEcho parameter → Lambda env var | AWS Secrets Manager with rotation |
| **Simli** | CloudFormation NoEcho parameter → Lambda env var | AWS Secrets Manager with rotation |
| **Tavily** | CloudFormation NoEcho parameter → Lambda env var | AWS Secrets Manager with rotation |

### 9.2 External API Controls

| Control | Implementation |
|---------|---------------|
| **TLS encryption** | HTTPS for all third-party API calls |
| **Scoped API keys** | Use restricted-scope keys where available |
| **Usage monitoring** | Provider-level usage alerts |
| **Fallback handling** | Graceful degradation on API failure |

## 10. Monitoring & Detection

### 10.1 CloudWatch

**Threats mitigated**: Multiple (detection and response)

| Control | Implementation |
|---------|---------------|
| **Lambda error alarms** | Alert on processing function errors |
| **Kinesis iterator age** | Alert on processing lag |
| **ECS task health** | Alert on task failures/restarts |
| **Transcribe throttling** | Alert on API throttle events |
| **Custom metrics** | Agent tool invocations, MCP calls, VP launches |

### 10.2 Audit Logging

| Source | Coverage |
|--------|----------|
| **CloudTrail** | AWS API calls (IAM, S3, DynamoDB, ECS, KMS) |
| **AppSync Logs** | GraphQL operations, authorization events |
| **ALB Access Logs** | WebSocket connections, source IPs |
| **API Gateway Logs** | MCP API requests, authorizer decisions |
| **X-Ray Tracing** | End-to-end request flow |

## 11. Implementation Checklist

### Pre-Deployment

- [ ] Review Cognito self-registration settings and email domain restrictions
- [ ] Configure KMS key policy with least-privilege grants
- [ ] Plan VPC CIDR ranges and security group rules
- [ ] Review VP meeting platform compliance requirements
- [ ] Document consent notification approach for target jurisdictions
- [ ] Review Lambda hook security requirements with customers
- [ ] Configure WAF IP allowlists (if using WAF)

### Post-Deployment

- [ ] Verify all 39 AppSync resolvers enforce authorization
- [ ] Test meeting-level access control (owner, shared, unauthorized)
- [ ] Verify WebSocket authentication token validation
- [ ] Configure CloudWatch alarm notifications
- [ ] Test DynamoDB TTL is active on all tables
- [ ] Verify S3 lifecycle policies for recordings
- [ ] Test VP bot identification in each meeting platform
- [ ] Validate MCP API key authentication flow
- [ ] Review Bedrock Guardrails configuration

### Ongoing Operations

- [ ] Regular AppSync authorization audit (all resolvers)
- [ ] Monitor KB access patterns for cross-meeting leakage
- [ ] Review MCP tool invocation logs for anomalies
- [ ] Audit VP launch events and recording sessions
- [ ] Rotate third-party API keys (ElevenLabs, Simli, Tavily)
- [ ] Update Chrome/Puppeteer container images
- [ ] Review and update Bedrock Guardrails policies
- [ ] Monitor agent tool invocation patterns
- [ ] Verify data retention compliance (TTL, lifecycle)
- [ ] Periodic IAM role permission review
