# LMA WebSocket Audio Streaming API Specification

> **Version:** 1.0  
> **Last Updated:** March 2026  
> **Status:** Current

This document specifies everything a client application needs to connect, authenticate, and stream real-time audio to the LMA (Live Meeting Assistant) WebSocket transcriber service.

---

## Table of Contents

- [1. Overview](#1-overview)
- [2. Architecture](#2-architecture)
- [3. Endpoint](#3-endpoint)
- [4. Authentication](#4-authentication)
- [5. Connection Lifecycle](#5-connection-lifecycle)
- [6. Message Protocol](#6-message-protocol)
  - [6.1 Text Messages (JSON Control Messages)](#61-text-messages-json-control-messages)
  - [6.2 Binary Messages (Audio Data)](#62-binary-messages-audio-data)
- [7. Audio Format Requirements](#7-audio-format-requirements)
- [8. Speaker Tracking](#8-speaker-tracking)
- [9. Error Handling](#9-error-handling)
- [10. Complete Client Examples](#10-complete-client-examples)
  - [10.1 JavaScript / TypeScript (Node.js)](#101-javascript--typescript-nodejs)
  - [10.2 JavaScript (Browser)](#102-javascript-browser)
  - [10.3 Python](#103-python)
- [11. Reference Implementations](#11-reference-implementations)
- [12. Server Configuration Reference](#12-server-configuration-reference)

---

## 1. Overview

The LMA WebSocket transcriber service accepts real-time audio streams over a WebSocket connection and produces live transcriptions using Amazon Transcribe. Clients connect via a secure WebSocket (`wss://`), authenticate with Amazon Cognito JWT tokens, send JSON control messages to start/end a session, and stream raw PCM audio as binary frames.

**Key capabilities:**
- Real-time speech-to-text transcription via Amazon Transcribe (standard or Call Analytics)
- Stereo audio support with per-channel speaker attribution
- Active speaker tracking for multi-participant meetings
- Optional server-side audio recording to Amazon S3

---

## 2. Architecture

```
┌──────────────┐     wss://      ┌──────────────┐     HTTP     ┌─────────────┐
│              │ ──────────────▶ │              │ ──────────▶ │             │
│  Your Client │                 │  CloudFront  │              │     ALB     │
│              │ ◀────────────── │  (TLS/WSS)   │ ◀────────── │  (port 80)  │
└──────────────┘                 └──────────────┘              └──────┬──────┘
                                                                      │
                                                                      ▼
                                                               ┌─────────────┐
                                                               │   Fargate    │
                                                               │  WebSocket   │
                                                               │   Server     │
                                                               │  (port 8080) │
                                                               └──────┬──────┘
                                                                      │
                                                        ┌─────────────┼─────────────┐
                                                        ▼             ▼             ▼
                                                  ┌──────────┐ ┌──────────┐ ┌──────────┐
                                                  │ Amazon   │ │ Kinesis  │ │ Amazon   │
                                                  │Transcribe│ │  Data    │ │   S3     │
                                                  │Streaming │ │ Streams  │ │(optional)│
                                                  └──────────┘ └──────────┘ └──────────┘
```

The traffic flow is:
1. **Client** connects via `wss://` to the **CloudFront** distribution domain
2. **CloudFront** terminates TLS and forwards the WebSocket connection to the **Application Load Balancer** over HTTP
3. The **ALB** routes the request to an **ECS Fargate** task running the WebSocket server
4. The server authenticates the JWT token, then streams audio to **Amazon Transcribe** and writes events to **Kinesis Data Streams**

---

## 3. Endpoint

### WebSocket URL

```
wss://<CLOUDFRONT_DOMAIN>/api/v1/ws
```

The `<CLOUDFRONT_DOMAIN>` is the CloudFront distribution domain name created during LMA stack deployment. You can find it in:

- **CloudFormation Outputs** — look for the WebSocket endpoint output in the `lma-websocket-transcriber-stack`
- **LMA Web UI Settings** — the WebSocket URL is displayed in the application settings

### Health Check (informational)

```
GET https://<CLOUDFRONT_DOMAIN>/health/check
```

Returns `200 OK` when the server is healthy, or `503` when CPU utilization exceeds the configured threshold.

---

## 4. Authentication

The WebSocket server requires a valid **Amazon Cognito JWT access token** for authentication. The token is verified against the LMA Cognito User Pool before the WebSocket upgrade is completed.

### Required Tokens

| Token | Required | Description |
|-------|----------|-------------|
| `access_token` | **Yes** | Cognito access token (JWT). Used for authentication. Must be prefixed with `Bearer `. |
| `id_token` | No (recommended) | Cognito ID token. Passed through to downstream services for user identity. |
| `refresh_token` | No (recommended) | Cognito refresh token. Passed through to downstream services for token refresh. |

### How to Obtain Tokens

Authenticate against the LMA Cognito User Pool using any standard Cognito authentication flow:

- **Cognito Hosted UI** — OAuth 2.0 / OIDC flow
- **AWS SDK** — `InitiateAuth` or `AdminInitiateAuth` API calls
- **Amplify** — `Auth.signIn()` 

The User Pool ID and App Client ID are available from the LMA CloudFormation stack outputs.

### Passing Tokens to the Server

Tokens can be provided in **either** HTTP headers or query string parameters on the WebSocket upgrade request. Both methods are supported; you may use whichever is more convenient for your client platform.

#### Option A: HTTP Headers (recommended)

```
authorization: Bearer <access_token>
id_token: <id_token>
refresh_token: <refresh_token>
```

#### Option B: Query String Parameters

```
wss://<CLOUDFRONT_DOMAIN>/api/v1/ws?authorization=Bearer%20<access_token>&id_token=<id_token>&refresh_token=<refresh_token>
```

> **Note:** Query string parameters are useful for browser-based clients where the native `WebSocket` API does not support custom headers.

### Authentication Failure

If authentication fails, the server responds with **HTTP 401 Unauthorized** before the WebSocket upgrade completes, and the connection is rejected. Common failure reasons:

- Missing `authorization` header/parameter
- Token not prefixed with `Bearer `
- Expired or invalid JWT token
- Token not issued by the expected Cognito User Pool

---

## 5. Connection Lifecycle

A complete streaming session follows this sequence:

```
Client                                          Server
  │                                                │
  │  1. WebSocket Connect (with auth tokens)       │
  │ ─────────────────────────────────────────────▶ │
  │                                                │  ← JWT verification
  │            101 Switching Protocols             │
  │ ◀───────────────────────────────────────────── │
  │                                                │
  │  2. Text: START message (JSON)                 │
  │ ─────────────────────────────────────────────▶ │
  │                                                │  ← Starts Transcribe session
  │                                                │  ← Writes call start event to Kinesis
  │                                                │
  │  3. Binary: audio chunk                        │
  │ ─────────────────────────────────────────────▶ │
  │  3. Binary: audio chunk                        │
  │ ─────────────────────────────────────────────▶ │
  │  3. Binary: audio chunk ...                    │
  │ ─────────────────────────────────────────────▶ │  ← Streams to Transcribe
  │                                                │  ← Writes transcript segments to Kinesis
  │                                                │
  │  4. (Optional) Text: SPEAKER_CHANGE (JSON)     │
  │ ─────────────────────────────────────────────▶ │  ← Updates active speaker
  │                                                │
  │  5. Text: END message (JSON)                   │
  │ ─────────────────────────────────────────────▶ │
  │                                                │  ← Writes call end event to Kinesis
  │                                                │  ← Uploads recording to S3 (if enabled)
  │                                                │  ← Cleans up resources
  │                                                │
  │            WebSocket Close                     │
  │ ◀───────────────────────────────────────────── │
  │                                                │
```

### Step-by-Step

1. **Connect** — Open a WebSocket connection to `wss://<CLOUDFRONT_DOMAIN>/api/v1/ws` with authentication tokens.
2. **Send START** — Immediately send a JSON text message with `callEvent: "START"` and session metadata. This **must** be the first text message. The server will not process audio until a START message is received.
3. **Stream Audio** — Send raw PCM audio data as binary WebSocket frames. Continue sending audio for the duration of the session.
4. **Speaker Changes** *(optional)* — Send `SPEAKER_CHANGE` text messages when the active speaker changes in a multi-participant meeting.
5. **Send END** — When the session is complete, send a JSON text message with `callEvent: "END"` to signal the server to finalize the transcription, upload recordings, and clean up.
6. **Close** — The server will close the WebSocket connection after processing the END event. The client may also close the connection. If the client disconnects without sending END, the server will automatically trigger end-of-call processing.

> **Important:** If the WebSocket connection is closed unexpectedly (network error, client crash), the server automatically triggers end-of-call cleanup including writing the call end event. However, explicitly sending the END message is strongly recommended for clean shutdown.

---

## 6. Message Protocol

The WebSocket protocol uses two frame types:
- **Text frames** — JSON-encoded control messages
- **Binary frames** — Raw PCM audio data

### 6.1 Text Messages (JSON Control Messages)

All text messages are JSON objects conforming to the `CallMetaData` schema. The `callEvent` field determines the message type.

#### 6.1.1 START Message

Sent once at the beginning of a session to initialize the transcription.

```json
{
  "callEvent": "START",
  "callId": "550e8400-e29b-41d4-a716-446655440000",
  "agentId": "agent@example.com",
  "fromNumber": "Customer Name",
  "toNumber": "Meeting Name",
  "samplingRate": 16000,
  "activeSpeaker": "Customer Name"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `callEvent` | `string` | **Yes** | — | Must be `"START"` |
| `callId` | `string` (UUID) | No | Auto-generated UUID | Unique identifier for this session. Use UUID v4 format. If omitted, the server generates one. |
| `agentId` | `string` | No | Auto-generated UUID | Identifier for the agent/user (e.g., email address or username). Used as the speaker name for the microphone channel (channel 1) in stereo mode. |
| `fromNumber` | `string` | No | `"Customer Phone"` | Label for the caller / remote participant. Used as speaker name for channel 0. Despite the name, this is a free-form string — not necessarily a phone number. |
| `toNumber` | `string` | No | `"System Phone"` | Label for the system / meeting. Free-form string. |
| `samplingRate` | `number` | **Yes** | — | Audio sample rate in Hz. Must match the actual audio being sent. Supported values: `8000` or `16000`. |
| `activeSpeaker` | `string` | No | Value of `fromNumber` | Name of the currently active speaker on the meeting/remote channel (channel 0). |

#### 6.1.2 SPEAKER_CHANGE Message

Sent during a session to update who is currently speaking on the meeting channel (channel 0). This is optional and only relevant for scenarios where multiple remote participants share a single audio channel.

```json
{
  "callEvent": "SPEAKER_CHANGE",
  "callId": "550e8400-e29b-41d4-a716-446655440000",
  "agentId": "agent@example.com",
  "activeSpeaker": "New Speaker Name"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `callEvent` | `string` | **Yes** | Must be `"SPEAKER_CHANGE"` |
| `callId` | `string` (UUID) | **Yes** | Must match the `callId` from the START message |
| `agentId` | `string` | **Yes** | Same agent ID from START. Used to determine if the speaker change applies to the meeting channel. |
| `activeSpeaker` | `string` | **Yes** | Name of the new active speaker on the meeting channel. If the name matches `agentId`, the change is ignored (the agent's channel is already known). |

> **Behavior:** The server only updates the meeting channel (channel 0) speaker. If `activeSpeaker` equals `agentId`, the event is ignored because the agent is already attributed to channel 1.

#### 6.1.3 END Message

Sent to signal the end of the audio streaming session.

```json
{
  "callEvent": "END",
  "callId": "550e8400-e29b-41d4-a716-446655440000"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `callEvent` | `string` | **Yes** | Must be `"END"` |
| `callId` | `string` (UUID) | **Yes** | Must match the `callId` from the START message |
| `shouldRecordCall` | `boolean` | No | Override whether the server saves the audio recording to S3. If omitted, uses the server's default configuration. |

#### Full CallMetaData Schema

For reference, here is the complete TypeScript type used by the server:

```typescript
type CallMetaData = {
  callId: string;           // UUID v4
  callEvent: string;        // "START" | "SPEAKER_CHANGE" | "END"
  agentId?: string;         // Speaker name for microphone channel (ch_1)
  fromNumber?: string;      // Speaker label for meeting channel (ch_0)
  toNumber?: string;        // Meeting/system label
  samplingRate: number;     // 8000 or 16000
  activeSpeaker: string;    // Current speaker on meeting channel (ch_0)
  shouldRecordCall?: boolean;
  channels?: {              // Server-managed; do not send from client
    [channelId: string]: {
      currentSpeakerName: string | null;
      speakers: string[];
      startTimes: number[];
    };
  };
};
```

### 6.2 Binary Messages (Audio Data)

After sending the START message, stream audio data as **binary WebSocket frames**. Each frame contains raw PCM audio samples with no headers or metadata.

**Requirements:**
- Each binary frame contains raw PCM audio bytes — **no WAV headers, no container format**
- Send audio continuously at roughly real-time pace
- The server buffers audio internally; there is no strict frame size requirement, but see [recommended chunk sizes](#chunk-size-recommendations) below

---

## 7. Audio Format Requirements

### Encoding

| Parameter | Value |
|-----------|-------|
| **Format** | Linear PCM (raw, uncompressed) |
| **Bit Depth** | 16-bit signed integer (little-endian) |
| **Bytes per Sample** | 2 |
| **Sample Rate** | `8000` Hz or `16000` Hz |
| **Channels** | 1 (mono) or 2 (stereo interleaved) |

### Mono vs. Stereo

| Mode | Channels | Description |
|------|----------|-------------|
| **Mono** | 1 | Single audio source. All audio is attributed to the active speaker. |
| **Stereo** | 2 | Two interleaved channels. Channel 0 (left) = meeting/remote audio ("CALLER"). Channel 1 (right) = microphone/local audio ("AGENT"). |

**Stereo interleaving format:**

```
[Ch0_Sample1][Ch1_Sample1][Ch0_Sample2][Ch1_Sample2]...
```

Each sample is a 16-bit little-endian signed integer (2 bytes). So each stereo sample pair is 4 bytes.

### Chunk Size Recommendations

While the server accepts binary frames of any size, the following chunk sizes are recommended for optimal latency and performance:

| Sample Rate | Channels | Recommended Chunk Duration | Bytes per Chunk |
|-------------|----------|---------------------------|-----------------|
| 8000 Hz | 1 (mono) | 200 ms | 3,200 bytes |
| 8000 Hz | 2 (stereo) | 200 ms | 6,400 bytes |
| 16000 Hz | 1 (mono) | 200 ms | 6,400 bytes |
| 16000 Hz | 2 (stereo) | 200 ms | 12,800 bytes |

**Formula:** `bytes = sampleRate × channels × bytesPerSample × (chunkDurationMs / 1000)`

The server uses an internal buffer (`BlockStream`) sized at: `(samplingRate / 10) × 2 × 2` bytes (i.e., 100 ms of stereo audio). Sending chunks of ~100–200 ms provides a good balance between latency and efficiency.

### Converting from Float32 to Int16 PCM

If your audio source provides 32-bit floating-point samples (common with Web Audio API, AudioWorklet, etc.), convert to 16-bit signed integers:

```javascript
function float32ToInt16(float32Array) {
  const int16Array = new Int16Array(float32Array.length);
  for (let i = 0; i < float32Array.length; i++) {
    const s = Math.max(-1, Math.min(1, float32Array[i]));
    int16Array[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }
  return int16Array;
}
```

### Sending a WAV File

If your source is a WAV file, you must **strip the WAV header** (typically the first 44 bytes) and send only the raw PCM data. The server expects no container headers in binary frames.

---

## 8. Speaker Tracking

The server supports two mechanisms for speaker attribution:

### Channel-Based Attribution (Stereo)

When streaming stereo audio:
- **Channel 0** (left) → Attributed to the meeting/remote participant(s). Speaker name is the `activeSpeaker` value.
- **Channel 1** (right) → Attributed to the local agent/user. Speaker name is the `agentId` value.

### Active Speaker Updates

For scenarios where multiple participants share the meeting channel (channel 0), use `SPEAKER_CHANGE` messages to update who is currently speaking. The server applies the new speaker name to subsequent transcription segments on channel 0.

**Rules:**
- `SPEAKER_CHANGE` only affects channel 0 (the meeting channel)
- If `activeSpeaker` matches `agentId`, the change is ignored (channel 1 always belongs to the agent)
- Speaker names are free-form strings

---

## 9. Error Handling

### Connection Errors

| Scenario | HTTP Status | Description |
|----------|-------------|-------------|
| Missing authorization | `401` | No `authorization` header or query parameter provided |
| Invalid Bearer format | `401` | Token is not in `Bearer <token>` format |
| Expired/invalid JWT | `401` | Token failed Cognito verification |
| Server overloaded | `503` | Health check failing due to CPU threshold exceeded |

### Runtime Errors

| Scenario | Behavior |
|----------|----------|
| Binary data received before START | Server logs an error; audio is dropped. Always send START first. |
| END received without START | Server logs an error and ignores the message. |
| Duplicate END messages | Server logs a warning; second END is ignored. |
| WebSocket error (network) | Server logs the error and forces a close, triggering automatic end-of-call cleanup. |
| Client disconnects unexpectedly | Server detects the close event and automatically writes a call end event and cleans up resources. |

### Reconnection

The server does not support session resumption. If a connection drops, the client must:
1. Open a new WebSocket connection
2. Send a new START message (optionally with a new `callId`)
3. Begin streaming audio again

The previous session will be finalized automatically by the server.

---

## 10. Complete Client Examples

### 10.1 JavaScript / TypeScript (Node.js)

```typescript
import { WebSocket } from 'ws';
import { randomUUID } from 'crypto';
import * as fs from 'fs';

// Configuration
const WS_URL = 'wss://<CLOUDFRONT_DOMAIN>/api/v1/ws';
const ACCESS_TOKEN = '<cognito_access_token>';
const ID_TOKEN = '<cognito_id_token>';
const REFRESH_TOKEN = '<cognito_refresh_token>';

const SAMPLE_RATE = 16000;
const BYTES_PER_SAMPLE = 2;
const CHANNELS = 1;
const CHUNK_DURATION_MS = 200;
const CHUNK_SIZE = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * (CHUNK_DURATION_MS / 1000);

const callId = randomUUID();

// 1. Connect with authentication headers
const ws = new WebSocket(WS_URL, {
  headers: {
    authorization: `Bearer ${ACCESS_TOKEN}`,
    id_token: ID_TOKEN,
    refresh_token: REFRESH_TOKEN,
  },
});

ws.on('open', () => {
  console.log('Connected');

  // 2. Send START message
  const startMessage = JSON.stringify({
    callEvent: 'START',
    callId: callId,
    agentId: 'my-agent@example.com',
    fromNumber: 'Remote Participant',
    toNumber: 'My Meeting',
    samplingRate: SAMPLE_RATE,
    activeSpeaker: 'Remote Participant',
  });
  ws.send(startMessage);
  console.log('START sent');

  // 3. Stream audio from a raw PCM file (no WAV header)
  const audioStream = fs.createReadStream('audio.raw', {
    highWaterMark: CHUNK_SIZE,
  });

  let chunkIndex = 0;
  audioStream.on('data', (chunk: Buffer) => {
    // Pause the read stream and schedule the send at real-time pace
    audioStream.pause();
    setTimeout(() => {
      ws.send(chunk, { binary: true });
      chunkIndex++;
      audioStream.resume();
    }, CHUNK_DURATION_MS);
  });

  audioStream.on('end', () => {
    // 4. Send END message
    const endMessage = JSON.stringify({
      callEvent: 'END',
      callId: callId,
    });
    ws.send(endMessage);
    console.log('END sent');
  });
});

ws.on('close', (code) => {
  console.log(`Connection closed with code: ${code}`);
});

ws.on('error', (err) => {
  console.error('WebSocket error:', err);
});
```

### 10.2 JavaScript (Browser)

Browser WebSocket does not support custom headers, so use query string authentication:

```javascript
const WS_URL = 'wss://<CLOUDFRONT_DOMAIN>/api/v1/ws';
const ACCESS_TOKEN = '<cognito_access_token>';
const ID_TOKEN = '<cognito_id_token>';
const REFRESH_TOKEN = '<cognito_refresh_token>';

const SAMPLE_RATE = 16000;

// 1. Connect with auth tokens in query string
const wsUrl = `${WS_URL}?authorization=${encodeURIComponent('Bearer ' + ACCESS_TOKEN)}&id_token=${encodeURIComponent(ID_TOKEN)}&refresh_token=${encodeURIComponent(REFRESH_TOKEN)}`;
const ws = new WebSocket(wsUrl);

const callId = crypto.randomUUID();

ws.onopen = () => {
  // 2. Send START
  ws.send(JSON.stringify({
    callEvent: 'START',
    callId: callId,
    agentId: 'user@example.com',
    fromNumber: 'Meeting Audio',
    toNumber: 'My Meeting',
    samplingRate: SAMPLE_RATE,
    activeSpeaker: 'Meeting Audio',
  }));

  // 3. Start capturing audio
  startAudioCapture();
};

async function startAudioCapture() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const audioContext = new AudioContext({ sampleRate: SAMPLE_RATE });
  const source = audioContext.createMediaStreamSource(stream);

  await audioContext.audioWorklet.addModule('audio-processor.js');
  const processor = new AudioWorkletNode(audioContext, 'audio-processor');

  processor.port.onmessage = (event) => {
    const float32Data = event.data; // Float32Array from AudioWorklet
    const int16Data = float32ToInt16(float32Data);
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(int16Data.buffer);  // Send as binary
    }
  };

  source.connect(processor);
  processor.connect(audioContext.destination);
}

function float32ToInt16(float32Array) {
  const int16Array = new Int16Array(float32Array.length);
  for (let i = 0; i < float32Array.length; i++) {
    const s = Math.max(-1, Math.min(1, float32Array[i]));
    int16Array[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }
  return int16Array;
}

function stopStreaming() {
  // 4. Send END
  ws.send(JSON.stringify({
    callEvent: 'END',
    callId: callId,
  }));
}

ws.onclose = (event) => {
  console.log(`Connection closed: code=${event.code}`);
};

ws.onerror = (event) => {
  console.error('WebSocket error:', event);
};
```

**AudioWorklet processor** (`audio-processor.js`):
```javascript
class AudioProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0];
    if (input && input[0]) {
      this.port.postMessage(input[0]); // Send Float32Array
    }
    return true;
  }
}
registerProcessor('audio-processor', AudioProcessor);
```

### 10.3 Python

```python
import asyncio
import json
import uuid
import struct
import websockets

WS_URL = "wss://<CLOUDFRONT_DOMAIN>/api/v1/ws"
ACCESS_TOKEN = "<cognito_access_token>"
ID_TOKEN = "<cognito_id_token>"
REFRESH_TOKEN = "<cognito_refresh_token>"

SAMPLE_RATE = 16000
CHANNELS = 1
BYTES_PER_SAMPLE = 2
CHUNK_DURATION_MS = 200
CHUNK_SIZE = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * CHUNK_DURATION_MS // 1000

async def stream_audio(pcm_file_path: str):
    call_id = str(uuid.uuid4())

    headers = {
        "authorization": f"Bearer {ACCESS_TOKEN}",
        "id_token": ID_TOKEN,
        "refresh_token": REFRESH_TOKEN,
    }

    async with websockets.connect(WS_URL, extra_headers=headers) as ws:
        # Send START
        start_msg = json.dumps({
            "callEvent": "START",
            "callId": call_id,
            "agentId": "agent@example.com",
            "fromNumber": "Remote Speaker",
            "toNumber": "My Meeting",
            "samplingRate": SAMPLE_RATE,
            "activeSpeaker": "Remote Speaker",
        })
        await ws.send(start_msg)
        print(f"START sent for call {call_id}")

        # Stream audio
        with open(pcm_file_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                await ws.send(chunk)
                await asyncio.sleep(CHUNK_DURATION_MS / 1000)

        # Send END
        end_msg = json.dumps({
            "callEvent": "END",
            "callId": call_id,
        })
        await ws.send(end_msg)
        print("END sent")

asyncio.run(stream_audio("audio.raw"))
```

---

## 11. Reference Implementations

The LMA repository includes two complete client implementations:

### Sample CLI Client

**Location:** `utilities/websocket-client/`

A TypeScript command-line tool that streams a stereo WAV file to the WebSocket server. Demonstrates:
- Header-based authentication with three JWT tokens
- WAV file parsing and header stripping
- Real-time paced audio chunk streaming
- START/END message lifecycle

**Usage:**
```bash
cd utilities/websocket-client
npm install
npx ts-node src/index.ts wss://<endpoint>/api/v1/ws --wavfile sample.wav
```

**Environment variables:**
- `LMA_ACCESS_JWT_TOKEN` — Cognito access token
- `LMA_ID_JWT_TOKEN` — Cognito ID token
- `LMA_REFRESH_JWT_TOKEN` — Cognito refresh token
- `SAMPLE_RATE` — Audio sample rate (default: `8000`)
- `BYTES_PER_SAMPLE` — Bytes per sample (default: `2`)
- `CHUNK_SIZE_IN_MS` — Chunk duration in ms (default: `200`)

---

## 12. Server Configuration Reference

These environment variables configure the server and are provided here for reference. Client developers do not need to set these, but understanding them helps when debugging or discussing capabilities with a deployment administrator.

| Variable | Default | Description |
|----------|---------|-------------|
| `SERVERPORT` | `8080` | Port the WebSocket server listens on |
| `SERVERHOST` | `127.0.0.1` | Host the server binds to |
| `AWS_REGION` | `us-east-1` | AWS region for Transcribe, S3, Kinesis |
| `USERPOOL_ID` | — | Cognito User Pool ID for JWT verification |
| `RECORDINGS_BUCKET_NAME` | — | S3 bucket for audio recordings |
| `RECORDING_FILE_PREFIX` | `lma-audio-recordings/` | S3 key prefix for recordings |
| `SHOULD_RECORD_CALL` | `false` | Whether to record and upload audio to S3 |
| `CPU_HEALTH_THRESHOLD` | `50` | CPU usage % threshold for health check |
| `LOCAL_TEMP_DIR` | `/tmp/` | Temporary directory for recording files |
| `WS_LOG_LEVEL` | `debug` | Server log level |
| `WS_LOG_INTERVAL` | `120` | Seconds between periodic health check log entries |

---

## Quick Reference Card

```
┌─────────────────────────────────────────────────────────┐
│                  LMA WebSocket API                      │
├─────────────────────────────────────────────────────────┤
│ Endpoint:  wss://<CLOUDFRONT_DOMAIN>/api/v1/ws          │
│ Auth:      Bearer <cognito_access_token>                │
│            (header or query string)                     │
│ Audio:     16-bit PCM, little-endian                    │
│            8000 or 16000 Hz, mono or stereo             │
│ Protocol:  Text frames = JSON control messages          │
│            Binary frames = raw PCM audio                │
│ Flow:      CONNECT → START → AUDIO... → END → CLOSE    │
└─────────────────────────────────────────────────────────┘
```
