# Data Flows

## Document Information

| Field | Value |
|-------|-------|
| **Document Version** | 1.0 |
| **Last Updated** | 2025-05-18 |
| **Classification** | Internal |

## 1. Overview

This document describes the primary data flows through the Live Meeting Assistant, identifying where data crosses trust boundaries, undergoes transformation, and is persisted. Each flow is analyzed for security-relevant characteristics.

## 2. Audio Capture & Transcription Flow (Core Pipeline)

### 2.1 WebSocket Audio Streaming

```mermaid
sequenceDiagram
    participant Client as Audio Source (Browser/Extension/VP)
    participant ALB as Application Load Balancer
    participant Fargate as Fargate WebSocket Server
    participant Transcribe as Amazon Transcribe Streaming
    participant Kinesis as Kinesis Data Stream
    participant S3 as S3 Recording Bucket

    Client->>ALB: WebSocket upgrade (wss://)
    ALB->>Fargate: Forward connection
    Client->>Fargate: Stream two-channel audio (binary frames)
    Fargate->>Transcribe: Start streaming transcription session
    Fargate->>S3: Write stereo audio recording (parallel)
    
    loop Audio Frames
        Client->>Fargate: Audio chunk (PCM/Opus)
        Fargate->>Transcribe: Forward audio bytes
        Transcribe-->>Fargate: Partial/final transcription results
        Fargate->>Kinesis: Write transcript event (speaker, text, timestamps)
    end
    
    Client->>Fargate: End stream signal
    Fargate->>S3: Finalize audio recording
    Fargate->>Kinesis: Write call end event
```

**Data in transit**: Raw audio bytes (PCM/Opus), transcription results (text + speaker labels + timestamps), audio recording (stereo WAV/Opus).

**Trust boundary crossings**:
- TB1→TB2: Audio client connects via HTTPS/WSS through ALB
- TB3→TB4: Fargate sends audio to Transcribe Streaming API
- TB3 internal: Fargate writes to Kinesis and S3

**Security controls**:
- ALB security groups restrict inbound connections
- WebSocket connection requires valid meeting token
- TLS 1.2+ encryption for all connections
- Fargate tasks in private subnets (egress via NAT)
- S3 bucket encrypted with customer-managed KMS key
- Kinesis stream encrypted with KMS

### 2.2 Virtual Participant Audio Capture

```mermaid
sequenceDiagram
    participant Admin as Admin User
    participant AppSync as AppSync API
    participant Lambda as VP Launcher Lambda
    participant ECS as ECS Fargate Task
    participant Platform as Meeting Platform (Zoom/Teams/etc.)
    participant Fargate as WebSocket Server

    Admin->>AppSync: Start Virtual Participant (meeting URL)
    AppSync->>Lambda: Invoke VP launcher
    Lambda->>ECS: Run Fargate task (headless Chrome)
    ECS->>Platform: Join meeting via browser (Playwright/CloakBrowser)
    Platform-->>ECS: Meeting audio/video stream
    ECS->>ECS: Capture audio from Chrome (two-channel)
    ECS->>Fargate: Stream audio via internal WebSocket
    Fargate->>Fargate: Process as standard audio stream
```

**Data in transit**: Meeting platform credentials, meeting URL, captured audio from Chrome browser, platform audio/video streams.

**Trust boundary crossings**:
- TB3→TB8: Fargate task connects to external meeting platform
- TB3 internal: VP task streams to WebSocket server

**Security controls**:
- VP tasks run in private subnets
- Meeting credentials stored as ECS task parameters (encrypted)
- No persistent storage on VP container (ephemeral)
- Platform-specific auth (OAuth, meeting passwords)

## 3. Event Processing Flow

### 3.1 Transcript Processing (Call Event Processor)

```mermaid
sequenceDiagram
    participant Kinesis as Kinesis Data Stream
    participant CEP as Call Event Processor Lambda
    participant Bedrock as Amazon Bedrock
    participant Guardrails as Bedrock Guardrails
    participant Translate as Amazon Translate
    participant AppSync as AppSync API
    participant DDB as DynamoDB

    Kinesis->>CEP: Batch of transcript events
    CEP->>CEP: Parse events, identify call context
    
    alt Translation Enabled
        CEP->>Translate: Translate transcript segments
        Translate-->>CEP: Translated text
    end
    
    alt AI Processing Triggered
        CEP->>Bedrock: Invoke Strands Agent (transcript context + tools)
        Bedrock->>Guardrails: Apply content filters
        Guardrails-->>Bedrock: Filtered response
        Bedrock-->>CEP: Agent response (summary/action items/etc.)
    end
    
    CEP->>AppSync: Mutation - store transcript segment
    AppSync->>DDB: Write transcript record
    CEP->>AppSync: Mutation - publish real-time update
    AppSync-->>AppSync: Push to subscribers (WebSocket)
```

**Data transformation**: Raw Kinesis events → parsed transcript segments → translated text → AI-processed summaries/insights → DynamoDB records + real-time subscriptions.

**Sensitive data exposure**: Full meeting transcript text sent to Bedrock for AI processing. Translation sends text to Amazon Translate. Both contain potentially sensitive meeting content.

**Trust boundary crossings**:
- TB3→TB4: Lambda sends transcript text to Bedrock, Translate, Guardrails
- TB3 internal: Lambda writes to AppSync/DynamoDB

### 3.2 Meeting Assistant Agent Flow

```mermaid
sequenceDiagram
    participant User as User (via UI)
    participant AppSync as AppSync API
    participant Lambda as Meeting Assist Lambda
    participant Strands as Strands Agent
    participant Bedrock as Amazon Bedrock
    participant KB as Bedrock Knowledge Base
    participant MCP as MCP Tools
    participant Tavily as Tavily Search
    participant Hook as Lambda Hook

    User->>AppSync: Send assistant query
    AppSync->>Lambda: Invoke meeting assist
    Lambda->>Strands: Initialize agent with tools
    Strands->>Bedrock: Prompt with meeting context + query
    
    loop Tool Use
        Bedrock-->>Strands: Tool call request
        alt Knowledge Base Query
            Strands->>KB: Semantic search across meetings
            KB-->>Strands: Relevant transcript chunks
        else MCP Tool
            Strands->>MCP: External tool call
            MCP-->>Strands: Tool result
        else Web Search
            Strands->>Tavily: Search query
            Tavily-->>Strands: Search results
        else Lambda Hook
            Strands->>Hook: Custom processing
            Hook-->>Strands: Hook result
        end
        Strands->>Bedrock: Continue with tool result
    end
    
    Bedrock-->>Strands: Final response
    Strands-->>Lambda: Agent output
    Lambda->>AppSync: Publish response
    AppSync-->>User: Real-time response (subscription)
```

**Trust boundary crossings**:
- TB1→TB3: User query via AppSync
- TB3→TB4: Meeting context to Bedrock, KB queries
- TB3→TB5: Tavily web search
- TB3→TB6: MCP tool calls to external servers
- TB3→TB7: Lambda hook invocations

## 4. Voice Assistant Flow

### 4.1 Nova Sonic Voice Interaction

```mermaid
sequenceDiagram
    participant User as Meeting Participant
    participant VP as Virtual Participant
    participant Lambda as Voice Assistant Lambda
    participant NovaSonic as Nova Sonic (Bedrock)
    participant Agent as Meeting Assist Agent
    participant Avatar as Simli Avatar API

    User->>VP: Speak (voice input in meeting)
    VP->>Lambda: Audio stream (detected speech)
    Lambda->>NovaSonic: Voice-to-voice processing
    NovaSonic->>Agent: Extracted intent / query
    Agent-->>NovaSonic: Text response
    NovaSonic-->>Lambda: Synthesized speech audio
    Lambda->>VP: Audio response for meeting
    VP->>VP: Play audio in meeting
    
    alt Avatar Enabled
        Lambda->>Avatar: Speech audio + viseme data
        Avatar-->>Lambda: Animated video frames
        Lambda->>VP: Display avatar in meeting
    end
```

**Trust boundary crossings**:
- TB3→TB4: Voice audio to Nova Sonic
- TB3→TB6: Avatar requests to Simli API
- TB3→TB8: Audio output played into meeting platform

**Security note**: Voice assistant processes live audio which may contain sensitive meeting content. Third-party services (Simli) receive audio/viseme data.

## 5. Web UI & Authentication Flows

### 5.1 Authentication Flow

```mermaid
sequenceDiagram
    participant Browser as Browser
    participant CF as CloudFront
    participant WAF as WAFv2 (Optional)
    participant Cognito as Cognito
    participant AppSync as AppSync

    Browser->>CF: Load React SPA
    CF->>WAF: IP allowlist check (if enabled)
    WAF-->>CF: Allowed
    CF-->>Browser: Static assets from S3
    Browser->>Cognito: Authenticate (email/password + optional MFA)
    Cognito-->>Browser: JWT tokens (ID, Access, Refresh)
    Browser->>AppSync: GraphQL request + JWT
    AppSync->>AppSync: Validate JWT, check Cognito groups (admin)
    AppSync-->>Browser: Authorized response
```

**Trust boundary crossings**: TB1→TB2 (browser to CloudFront/WAF/Cognito), TB2→TB3 (JWT to AppSync).

### 5.2 Real-Time Meeting UI Flow

```mermaid
sequenceDiagram
    participant Browser as Browser
    participant AppSync as AppSync
    participant DDB as DynamoDB

    Browser->>AppSync: Subscribe to meeting updates (WSS)
    
    loop Real-Time Updates
        AppSync-->>Browser: New transcript segment
        AppSync-->>Browser: Translation update
        AppSync-->>Browser: AI summary/action items
        AppSync-->>Browser: Meeting status changes
    end
    
    Browser->>AppSync: Query meeting history
    AppSync->>DDB: Retrieve meeting records
    DDB-->>AppSync: Meeting data
    AppSync-->>Browser: Historical meetings
```

## 6. MCP API Gateway Flow

```mermaid
sequenceDiagram
    participant Client as External MCP Client
    participant APIGW as API Gateway
    participant Authorizer as Lambda Authorizer
    participant DDB as DynamoDB (API Keys)
    participant Lambda as MCP Handler Lambda
    participant Agent as Meeting Assist Agent

    Client->>APIGW: MCP request + API key header
    APIGW->>Authorizer: Validate API key
    Authorizer->>DDB: Lookup SHA-256(API key)
    DDB-->>Authorizer: Key record (permissions, rate limit)
    Authorizer-->>APIGW: Allow/Deny + policy
    APIGW->>Lambda: Forward MCP request
    Lambda->>Agent: Process tool request
    Agent-->>Lambda: Tool result
    Lambda-->>APIGW: MCP response
    APIGW-->>Client: Response (with rate limit headers)
```

**Trust boundary crossings**: TB1→TB2→TB3. External clients authenticate via API key, validated against SHA-256 hash in DynamoDB.

**Security controls**:
- Custom Lambda Authorizer validates API keys
- SHA-256 hashing (keys not stored in plaintext)
- Rate limiting (100 req/sec default)
- Request validation at API Gateway level
- Access logging enabled
- Throttling per API key

## 7. Knowledge Base & Search Flow

```mermaid
sequenceDiagram
    participant Lambda as Processing Lambda
    participant S3 as S3 Transcript Bucket
    participant BedrockKB as Bedrock Knowledge Base
    participant S3Vectors as S3 Vectors
    participant Agent as Meeting Assist Agent

    Lambda->>S3: Store meeting transcript document
    Lambda->>BedrockKB: Trigger data source sync
    BedrockKB->>S3: Read transcript documents
    BedrockKB->>BedrockKB: Chunk and embed text
    BedrockKB->>S3Vectors: Store vector embeddings

    Agent->>BedrockKB: Semantic search query
    BedrockKB->>S3Vectors: Vector similarity search
    S3Vectors-->>BedrockKB: Matching transcript chunks
    BedrockKB-->>Agent: Retrieved meeting context
```

**Security note**: Knowledge Base contains transcripts from ALL meetings indexed into a single vector store. Cross-meeting access control is not natively enforced at the KB layer — any authenticated user querying the assistant can potentially retrieve content from any indexed meeting.

## 8. Browser Extension Flow

```mermaid
sequenceDiagram
    participant Extension as Chrome Extension
    participant Tab as Browser Tab (Meeting)
    participant ALB as ALB / WebSocket Server
    participant Fargate as Fargate

    Extension->>Tab: Capture tab audio (chrome.tabCapture API)
    Tab-->>Extension: Audio stream (MediaStream)
    Extension->>Extension: Encode audio (PCM/Opus)
    Extension->>ALB: WebSocket connection (wss://)
    
    loop Audio Streaming
        Extension->>ALB: Audio frames
        ALB->>Fargate: Forward frames
    end
```

**Trust boundary crossings**: TB1→TB2. Browser extension captures audio from the active tab and transmits to the LMA backend.

**Security note**: Extension has broad audio capture permissions. Extension code runs in the user's browser with access to tab audio content.

## 9. Lambda Hook Flow

```mermaid
sequenceDiagram
    participant CEP as Call Event Processor
    participant Hook as Customer Lambda Hook
    participant ExtSys as External System

    CEP->>Hook: Invoke with transcript segment + context
    Hook->>Hook: Custom processing logic
    Hook->>ExtSys: Optional - send data externally
    ExtSys-->>Hook: Response
    Hook-->>CEP: Processed result (or pass-through)
```

**Trust boundary crossings**: TB3→TB7. Customer-managed Lambda hooks receive transcript data and can send it to external systems.

## 10. Summary of Cross-Boundary Data Flows

| Flow | From | To | Data Sensitivity | Controls |
|------|------|----|-----------------|----------|
| Audio streaming | TB1 | TB2→TB3 | High (live speech) | WSS/TLS, meeting token, ALB security groups |
| Audio to Transcribe | TB3 | TB4 | High (raw audio) | TLS, IAM roles |
| Transcripts to Bedrock | TB3 | TB4 | High (meeting content) | TLS, IAM roles, Guardrails |
| VP joining meetings | TB3 | TB8 | Medium (meeting credentials) | OAuth, encrypted parameters |
| Voice to Nova Sonic | TB3 | TB4 | High (voice audio) | TLS, IAM roles |
| Avatar to Simli | TB3 | TB6 | Medium (speech/viseme data) | TLS, API key auth |
| Audio to ElevenLabs | TB3 | TB6 | Medium (text for TTS) | TLS, API key auth |
| MCP tool calls | TB3 | TB6 | Variable (meeting context) | API key auth, TLS |
| Tavily web search | TB3 | TB5 | Low (search queries) | API key auth, TLS |
| KB vector search | TB3 | TB4→TB5 | High (all meeting transcripts) | IAM, encryption |
| Lambda hooks | TB3 | TB7 | High (transcript data) | IAM, invocation-only |
| Browser extension audio | TB1 | TB2 | High (tab audio) | WSS/TLS, extension permissions |
| Real-time subscriptions | TB3 | TB1 | High (live transcripts) | JWT auth, WSS |
| MCP API inbound | TB1 | TB3 | Variable | API key, Lambda authorizer, rate limiting |
