---
title: "LMA WebSocket Audio Streaming API Specification"
---

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
- Real-time speech-to-text transcription via Amazon Transcribe
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

### What the Server Records

The transcriber's connection log lines identify the request without reproducing the tokens.
For each connection attempt the server records the request path with the query string
removed, the client IP taken from `X-Forwarded-For`, and a fixed set of non-sensitive
headers; any other header is reported as a count only. The allowlisted header names are:

`host`, `origin`, `referer`, `user-agent`, `content-type`, `content-length`, `connection`,
`upgrade`, `x-forwarded-for`, `x-forwarded-proto`, `x-forwarded-port`,
`sec-websocket-version`, `sec-websocket-extensions`.

`origin` and `referer` are URLs, so their values also have the query string removed before
they are logged.

The same rule covers the control frames (`START`, `END`, `SPEAKER_CHANGE`, `START_VIDEO`,
`END_VIDEO`) and the Kinesis records derived from them: wherever one of those objects is
written to the log, its `accessToken` / `idToken` / `refreshToken` fields — and the
PascalCase `AccessToken` / `IdToken` / `RefreshToken` used on the Kinesis records — appear
as `[REDACTED]`. Everything else about the frame is logged normally, so a frame that omits
a field still shows as omitted.

This holds whichever option above you choose, so a client that passes tokens on the query
string does not produce different log content than one using headers. It also holds at
every log level, including `LogLevel: DEBUG`.

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
6. **Close** — The server will close the WebSocket connection after processing the END event. The client may also close the connection. If the client disconnects without sending END, the server holds the meeting for 60 seconds for the client to reconnect, and then triggers end-of-call processing (see [Reconnection](#reconnection)).

> **Important:** If the WebSocket connection is closed unexpectedly (network error, client crash), the server ends the meeting, including writing the call end event, 60 seconds later unless the client reconnects and sends START for it again. Explicitly sending the END message is strongly recommended for clean shutdown.

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
  "localSpeakerName": "ana@example.com",
  "remoteSpeakerName": "Other Participant",
  "samplingRate": 16000,
  "activeSpeaker": "Other Participant",
  "diarizeSystemChannel": true,
  "diarizeMicChannel": false,
  "clientType": "DESKTOP_APP"
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `callEvent` | `string` | **Yes** | — | Must be `"START"` |
| `callId` | `string` (UUID) | No | Auto-generated UUID | Unique identifier for this session. Use UUID v4 format. If omitted, the server generates one. |
| `localSpeakerName` | `string` | No | Auto-generated UUID | The speaker name for the microphone channel (channel 1), usually the user's email address or name. It is also stored as the meeting's `AgentId`, which the meeting list shows as **Owner Name**. Older name: `agentId`. |
| `remoteSpeakerName` | `string` | No | `"Other Participant"` | The speaker name for the meeting-audio channel (channel 0) when the START has no `activeSpeaker`, until a `SPEAKER_CHANGE` names someone else. It is stored as the meeting's `CustomerPhoneNumber` attribute and stands in for the placeholder speaker name in the transcript. Free-form text. Older name: `fromNumber`. |
| `samplingRate` | `number` | **Yes** | — | Audio sample rate in Hz. Must match the actual audio being sent. Supported values: `8000` or `16000`. |
| `activeSpeaker` | `string` | No | Value of `remoteSpeakerName` | Name of the currently active speaker on the meeting/remote channel (channel 0). |
| `diarizeSystemChannel` | `boolean` | No | The deployment default (see below) | Enable Amazon Transcribe speaker partitioning (diarization) on the system / meeting audio channel (channel 0). See [Speaker Identification](#speaker-identification-diarization). |
| `diarizeMicChannel` | `boolean` | No | The deployment default (see below) | Same, for the microphone channel (channel 1). |
| `clientType` | `string` | No | `API` | Which client is capturing the meeting: `STREAM_AUDIO` (the Stream Audio page and its embeddable components), `BROWSER_EXTENSION` or `DESKTOP_APP`. Stored on the meeting as its capture source; any other value, or none, is stored as `API`. See [Capture Context](#capture-context). |

> **Older field names.** Clients written for LMA before 0.4.0 send the two
> speaker names as `agentId` (for `localSpeakerName`) and `fromNumber` (for
> `remoteSpeakerName`), names LMA inherited from a telephony solution. The
> server still accepts them, in every message type, and a client need not change.
> If a message carries both spellings of one name, the documented one is used.
> Those clients may also send `toNumber`, a free-form label the server stores as
> the meeting's `SystemPhoneNumber` attribute (default `"System"`) and does not
> otherwise use; it has no newer name, and a new client need not send it.
> The in-repository clients that are built and deployed with the stack (the
> Stream Audio page, the embeddable components, the browser extension, the
> sample CLI client) send the documented names. The Desktop Capture Apps for
> macOS and Windows still send `agentId`, `fromNumber` and `toNumber`, which
> keeps them working against deployments of earlier versions.

> **Precedence:** if the client sends **either** diarization field, its values are
> used verbatim (a field left out counts as `false`). Only when **neither** field
> is present does the server fall back to the deployment-wide default, applied to
> both channels: **Amazon Transcribe › Identify speakers within a channel by
> default** on the deployment's Configuration page (off unless an Admin changes
> it). Anything other than a JSON
> `true` — including the string `"true"` — is treated as `false`.

#### 6.1.2 SPEAKER_CHANGE Message

Sent during a session to update who is currently speaking on the meeting channel (channel 0). This is optional and only relevant for scenarios where multiple remote participants share a single audio channel.

```json
{
  "callEvent": "SPEAKER_CHANGE",
  "callId": "550e8400-e29b-41d4-a716-446655440000",
  "localSpeakerName": "ana@example.com",
  "activeSpeaker": "New Speaker Name"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `callEvent` | `string` | **Yes** | Must be `"SPEAKER_CHANGE"` |
| `callId` | `string` (UUID) | **Yes** | Must match the `callId` from the START message |
| `localSpeakerName` | `string` | **Yes** | The same `localSpeakerName` as in START (older name: `agentId`). Used to determine if the speaker change applies to the meeting channel. |
| `activeSpeaker` | `string` | **Yes** | Name of the new active speaker on the meeting channel. If the name matches `localSpeakerName`, the change is ignored (the microphone channel's speaker is already known). |

> **Behavior:** The server only updates the meeting channel (channel 0) speaker. If `activeSpeaker` equals `localSpeakerName`, the event is ignored because that speaker is already attributed to channel 1.

**Where the change is placed.** The server places each change on the meeting's
audio clock at the point it arrives, which is the amount of audio received on the
session so far. It then names each meeting-channel segment after whoever was
active for most of the segment's audio, with every change moved back by 0.75 s
(the `SPEAKER_CHANGE_LAG_SECONDS` environment variable on the transcriber task
overrides this). For that to be right, send a change as soon as it happens, on the
same connection as the audio, and keep sending audio as it is captured rather than
in large batches. This applies on the ASR MicroVM engine, and on Amazon Transcribe
when the meeting channel is diarized. Otherwise, a segment takes the speaker active
when Amazon Transcribe returns it. An `activeSpeaker` of `none` or `n/a` means
nobody is speaking: the previous speaker carries on.

**With the meeting channel diarized**, the speaker changes are also counted
against each voice label. Once a voice has at least 20 s of speech and one name
holds at least 70% of it, that voice's segments are named after that name even
where the reported changes lag or flicker. The voice id is written only under the
name the voice is attributed to, so `Ana (spk_1)` and `Sam (spk_1)` do not both
appear. See
[Naming rows from the voice as well](virtual-participant.md#naming-rows-from-the-voice-as-well),
which describes the same rule for the Virtual Participant. A client that never
sends `SPEAKER_CHANGE` gets the meeting channel's own name plus the voice id, as
before.

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

#### 6.1.4 Frames the Server Sends

The server sends JSON text frames to the client only for meeting consent
(below). Each has an `event` field, not `callEvent`. A client that does not
use meeting consent never asks for one, and may ignore any text frame it
receives.

#### 6.1.5 Meeting Consent Frames

When the deployment's [meeting consent](meeting-consent.md) mode is Notify or
Opt-in, a client shows its user a consent step before streaming and sends what
the user confirmed with `START`. The server records it on the meeting once the
meeting exists, and transcribes and records audio only while the meeting's
consent allows it. See
[Stream Audio and the Desktop Capture App](meeting-consent.md#stream-audio-and-the-desktop-capture-app)
for the behaviour; this section is the protocol.

**Ask for the settings** (any time on an open connection, usually once on
connecting):

```json
{ "callEvent": "CONSENT_SETTINGS" }
```

The server answers:

```json
{
  "event": "CONSENT_SETTINGS",
  "available": true,
  "mode": "opt_in",
  "rule": "owner_attests",
  "supported": true,
  "noticeTemplate": "{LMA_USER} would like to record and transcribe this meeting ({meeting}) ...",
  "consentPhrase": "I consent",
  "objectionPhrase": "I object",
  "clientsWithoutConsent": "accept_with_warning"
}
```

| Field | Meaning |
|-------|---------|
| `available` | `false` when the settings could not be read, with a `message`; the other fields are then absent |
| `mode` | `off`, `notify` or `opt_in`. With `off` no consent step is needed |
| `rule` | For `opt_in`, `owner_attests`: the rule a streaming client's meeting runs under, met by the user's confirmation. `null` otherwise |
| `supported` | `false` when the deployment's opt-in rule cannot be met by a streaming client, with the `reason`; the server refuses such a meeting |
| `noticeTemplate` | The notice (notify) or consent request (opt-in) to show. Fill `{LMA_USER}` (the user, or "The meeting organizer"), `{meeting}` (the meeting's name, or "this meeting"), `{consent_phrase}` and `{objection_phrase}` (the two phrases below); leave other text in braces as written |
| `clientsWithoutConsent` | What the server does with a `START` that has no `consent` field: `accept_with_warning` or `reject` |

**Send the step with START**, in a `consent` field:

```json
{
  "callEvent": "START",
  "callId": "Weekly sync - 2026-11-02-10:00:00.000",
  "localSpeakerName": "ana@example.com",
  "samplingRate": 16000,
  "consent": {
    "mode": "opt_in",
    "confirmed": true,
    "method": "verbal",
    "noticeText": "ana@example.com would like to record and transcribe this meeting (Weekly sync) ...",
    "lmaUser": "ana@example.com",
    "meetingName": "Weekly sync",
    "statement": "Everyone agreed at the start of the call.",
    "client": "Stream Audio"
  }
}
```

| Field | Required | Meaning |
|-------|----------|---------|
| `mode` | Yes | The mode the step was shown for. An opt-in meeting whose step was shown for another mode is refused (the settings changed) |
| `confirmed` | For opt-in | `true` when the user confirmed: in opt-in, that everyone agreed; in notify, that the notice was given. An opt-in meeting without it is refused |
| `method` | For opt-in | `verbal`, `chat` or `invite_terms`: how everyone was told or agreed |
| `noticeText` | No | The notice as shown, placeholders filled (at most 2,000 characters); recorded as the notice posted |
| `lmaUser`, `meetingName` | No | Fill the placeholders of the notice stored on the meeting (at most 100 characters each) |
| `statement` | No | The user's own words about how everyone agreed (at most 500 characters) |
| `client` | No | The client's name, used in the attestation's statement when there is none |

A client that implements the consent step should send the `consent` field
with every `START`, with `mode: "off"` when it has not read the settings. In an
opt-in deployment such a `START` is refused with `CONSENT_NOT_CONFIRMED`, which
tells the client to read the settings again, instead of being treated by
`clientsWithoutConsent` as a client without a consent step.

In opt-in, audio sent after `START` is discarded until the meeting's consent
allows capture, usually a second or two later; there is no need to wait before
sending it.

**Report an objection** while streaming:

```json
{ "callEvent": "CONSENT_OBJECTION", "participantName": "Bo Chen", "source": "voice", "text": "I object" }
```

`participantName` (default *A participant*), `source` (`voice`, the default,
or `chat`) and `text` are optional. The server stops transcribing and
recording at once, ends the meeting as if `END` had been sent, records the
objection, and closes the connection. Do not send `END` afterwards.

**Send an evidence image** after `START`:

```json
{
  "callEvent": "CONSENT_EVIDENCE",
  "image": "data:image/jpeg;base64,/9j/4AAQ...",
  "capturedAt": "2026-11-02T10:01:30.000Z",
  "note": "The meeting window after the request"
}
```

`image` is a JPEG or PNG, as base64 with or without the `data:` prefix, of at
most 3.75 MB; at most five are accepted per meeting. The server stores it under
the meeting's evidence folder, adds it to the consent record, and asks for it
to be checked. `CONSENT_OBJECTION` and `CONSENT_EVIDENCE` apply to the meeting
started on the same connection; their `callId`, if sent, is not used. They may
be sent straight after `START`: the server holds them until it has registered
the meeting (up to eight per connection). A consent step is recorded only on a
meeting owned by the signed-in user who sent `START`.

**What the server sends back**, after each step and on refusal:

```json
{
  "event": "CONSENT_STATE",
  "callId": "Weekly sync - 2026-11-02-10:00:00.000",
  "mode": "opt_in",
  "status": "granted",
  "captureAllowed": true,
  "reason": "the meeting owner attested that everyone agreed"
}
```

| Field | Meaning |
|-------|---------|
| `status` | The record's status: `pending`, `granted`, `objected` or `not_required` |
| `captureAllowed` | `false` only when the server has stopped, or will not start, capturing the meeting; the meeting is then ended and the connection closed. Stop capturing and do not reconnect for the same meeting |
| `code`, `message` | When something was refused or not recorded: `CONSENT_REQUIRED` (no consent step, and the deployment refuses such clients), `CONSENT_NOT_CONFIRMED`, `CONSENT_RULE_UNSUPPORTED`, `CONSENT_NOT_RECORDED` (the record could not be written; in notify capture goes on), `NOTICE_NOT_RECORDED` (the notice was not recorded as posted; capture goes on), `MEETING_NOT_OWNED` (the meeting named belongs to another user; start again with another name), `OBJECTION_RECEIVED`, `EVIDENCE_REFUSED`, `EVIDENCE_NOT_STORED`, `NO_MEETING` |
| `evidence` | After an evidence image: `evidenceId`, `stored`, and once checked the `verdict` (`supports_consent`, `objection_found` or `inconclusive`) |

A refused `START` is answered with a `CONSENT_STATE` frame and the connection is
closed with code `1008`; no meeting is created.

#### Full CallMetaData Schema

For reference, here is the complete TypeScript type used by the server:

```typescript
type CallMetaData = {
  callId: string;           // UUID v4
  callEvent: string;        // "START" | "SPEAKER_CHANGE" | "END"
  localSpeakerName?: string;  // Speaker name for microphone channel (ch_1); older name agentId
  remoteSpeakerName?: string; // Speaker name for meeting channel (ch_0); older name fromNumber
  toNumber?: string;        // Older clients only: stored as SystemPhoneNumber, otherwise unused
  samplingRate: number;     // 8000 or 16000
  activeSpeaker: string;    // Current speaker on meeting channel (ch_0)
  shouldRecordCall?: boolean;
  diarizeSystemChannel?: boolean;  // START only: diarize ch_0 (system/meeting)
  diarizeMicChannel?: boolean;     // START only: diarize ch_1 (microphone)
  consent?: object;         // START only: the client's consent step (6.1.5)
  clientType?: string;      // START only: STREAM_AUDIO | BROWSER_EXTENSION | DESKTOP_APP (Capture Context)
  channels?: {              // Server-managed; do not send from client
    [channelId: string]: {
      currentSpeakerName: string | null;
      speakers: string[];
      startTimes: number[];
    };
  };
};
```

#### Capture Context

Each meeting's record says how it was captured and transcribed, in four fields
of the `Call` type that `getCall` returns. The server writes them from the
`START` message and the settings the meeting starts with; a client sets only
the first, through `clientType`.

| Field | Values |
|-------|--------|
| `CaptureSource` | `STREAM_AUDIO`, `BROWSER_EXTENSION` or `DESKTOP_APP` from the START's `clientType`, and `API` for any other client of this API. Meetings that do not come through this API are `VIRTUAL_PARTICIPANT` (a [Virtual Participant](virtual-participant.md) joined the meeting) and `UPLOAD` (an uploaded recording). |
| `TranscriptionEngine` | `TRANSCRIBE` (Amazon Transcribe) or `MICROVM_ASR` (the [MicroVM ASR engine](microvm-asr.md)). A meeting the MicroVM could not take, and that Amazon Transcribe transcribed instead, reads `TRANSCRIBE`. |
| `TranscriptionLanguage` | The Amazon Transcribe language setting the meeting used: a language code such as `en-US`, or `identify-language` / `identify-multiple-languages` when Transcribe identified the language from the deployment's language options. Not set for `MICROVM_ASR`, whose model sets the language; a meeting that moved to the MicroVM after START can still hold the language START recorded, so read it only when `TranscriptionEngine` is `TRANSCRIBE`. |
| `SpeakerSeparation` | The channels speaker separation (diarization) was asked for, after the [precedence](#611-start-message) above: `BOTH`, `REMOTE` (the meeting audio, channel 0), `LOCAL` (the microphone, channel 1) or `NONE`. |

The engine is the one the meeting is expected to start with when its record is
created. If another one transcribes it — Amazon Transcribe taking over because
no MicroVM could be started, or an opt-in meeting that waited for consent while
the engine setting changed — the server writes the transcription fields again
when transcription starts. The `clientType` is the client's own statement: it is
informational, and does not change how the meeting is processed. All four fields are absent on meetings recorded before LMA stored
them, and a client that does not send `clientType` keeps working unchanged.

### 6.2 Binary Messages (Audio Data)

After sending the START message, stream audio data as **binary WebSocket frames**. Each frame contains raw PCM audio samples with no headers or metadata.

The server sends no reply to START, and a client does not need to wait before sending audio. Audio that arrives while the server is still handling the START (reading settings, checking consent, opening or resuming the meeting's session) is held and then delivered in the order it arrived, ahead of any audio sent later. The hold keeps up to 8 seconds of audio per connection at the START's `samplingRate` (two channels of 16-bit PCM, with the rate taken as between 8 and 48 kHz); beyond that the oldest audio is dropped and the server logs a warning. If the START is refused, or the connection closes before it has been handled, the held audio is discarded.

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
| **Stereo** | 2 | Two interleaved channels. Channel 0 (left) = meeting/remote audio (transcript channel `REMOTE`). Channel 1 (right) = microphone/local audio (transcript channel `LOCAL`). |

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

The server supports three mechanisms for speaker attribution:

### Channel-Based Attribution (Stereo)

When streaming stereo audio:
- **Channel 0** (left) → Attributed to the meeting/remote participant(s). Speaker name is the `activeSpeaker` value.
- **Channel 1** (right) → Attributed to the local user. Speaker name is the `localSpeakerName` value.

Each transcript segment records which of the two it came from in its `Channel`
field: `REMOTE` for channel 0 and `LOCAL` for channel 1. A client never sends
these names; they appear in what LMA stores and returns, such as the GraphQL
`TranscriptSegment.Channel` field, the transcript Lambda hook's input and the
transcript-segment records on the meeting's Kinesis data stream.

LMA versions before 0.4.0 used the names `CALLER` (for `REMOTE`) and `AGENT`
(for `LOCAL`). Meetings recorded by those versions keep the older names until
they expire, and LMA reads them as the new ones. A producer that writes
transcript segments to the Kinesis data stream directly, or calls the
`addTranscriptSegment` mutation, may still send `CALLER` or `AGENT`; LMA stores
them as `REMOTE` and `LOCAL`.

### Active Speaker Updates

For scenarios where multiple participants share the meeting channel (channel 0), use `SPEAKER_CHANGE` messages to update who is currently speaking. The server applies the new speaker name to subsequent transcription segments on channel 0.

**Rules:**
- `SPEAKER_CHANGE` only affects channel 0 (the meeting channel)
- If `activeSpeaker` matches `localSpeakerName`, the change is ignored (channel 1 always belongs to the local speaker)
- Speaker names are free-form strings

### Speaker Identification (Diarization)

Channel-based attribution gives one name per channel, which is not enough when
several people share a channel — a multi-participant call captured from a browser
tab, or a conference-room microphone. Amazon Transcribe speaker partitioning
(diarization) tells those voices apart, and it can be enabled **per channel** with
the `diarizeSystemChannel` / `diarizeMicChannel` fields on the START message.

The label is **appended** to the channel's existing speaker name rather than
replacing it, so client-supplied names still come through:

| Base speaker name | With diarization |
|---|---|
| `Other Participant` | `Other Participant (spk_0)`, `Other Participant (spk_1)`, … |
| `alice@example.com` | `alice@example.com (spk_0)` |
| `Bob` (from `SPEAKER_CHANGE`) | `Bob (spk_0)` |

Speakers are numbered **per channel**, each starting at `spk_0`. The same person
speaking on both channels therefore gets a different number on each.

**Behaviour worth knowing before you enable it:**

- **Labels arrive only on final segments.** Amazon Transcribe does not label
  partial results, so a segment appears with the bare base name while it is still
  partial and gains its `(spk_N)` when it finalizes.
- **Numbering restarts on reconnect.** Speaker numbers are scoped to one
  Transcribe session. If the session reconnects mid-meeting (see
  [Reconnection](#reconnection)), the same person may come back as a different
  `spk_N`.
- **Accuracy degrades above about five voices per channel.** Transcribe can emit
  up to `spk_29`, but the streaming API has no equivalent of the batch API's
  `MaxSpeakerLabels`, so the count cannot be capped.
- **Language support varies.** Diarization is gated per language. On a language
  that does not support it, no labels are produced and speaker names are simply
  unchanged.
- **Segments are split at speaker turns.** A single Transcribe result routinely
  spans several turns — in natural conversation there is no pause to break on, so
  30-second results containing four turns are normal. The server recovers the turn
  structure from the per-item labels and emits one transcript segment per turn.
- **A live segment is bounded at 20 seconds.** Amazon Transcribe caps a result near
  30 seconds and never labels a partial, so deferring entirely to its result
  boundary would leave the live transcript showing one unlabelled block for that
  long. Each result is therefore chunked into windows of at most
  `DIARIZATION_MAX_SEGMENT_SECONDS` (default 20); a window whose audio is already in
  the past is emitted as final even while Transcribe still calls the result partial,
  so the live view settles and splits within that bound. Set the value to `0` to
  disable chunking and follow Transcribe's boundaries exactly.

  Two consequences worth knowing. A window boundary can fall in the middle of one
  speaker's turn, producing two adjacent segments for the same speaker — the UI
  merges those back together on display. And an early window is written twice: once
  without a label when it settles, then once more with its `(spk_N)` label when the
  final arrives (same segment id, so it updates in place rather than duplicating).
- **Short label runs are absorbed, not split.** Word-level labels are noisy: a
  single word flips to another speaker mid-utterance, and punctuation carries no
  label at all. A run of same-speaker words must reach both
  `DIARIZATION_MIN_RUN_WORDS` (default 3) and `DIARIZATION_MIN_RUN_SECONDS`
  (default 0.5) to become its own segment; anything shorter is merged into its
  neighbour. No transcript text is ever moved or dropped by this — only the
  speaker attribution changes. The defaults were fitted to a real two-speaker
  recording in which spurious runs measured 1-2 words / 0.1-0.9 s and real turns
  6-42 words / 1.2-13.4 s. A consequence worth knowing: a genuine one- or two-word
  interjection ("Yeah.") is attributed to the surrounding speaker.
- **Tuning.** Every final result on a diarized channel logs a `[DIARIZATION]` line
  at INFO giving the raw runs (label, words, seconds), the resulting segments, and
  how many runs were absorbed — for example
  `runs [spk_0x23w/7.3s, spk_2x19w/6.2s, spk_1x1w/0.1s] -> 2 segment(s) [spk_0, spk_2] (absorbed 1)`.
  Use these to re-derive the thresholds for your own audio.
- **No extra Transcribe cost.** Diarization is included in the standard rate, and
  the two channels are billed as a single stream.

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
| Binary data received before START | Server logs an error; audio is dropped. Always send START first. Audio sent after a START the server is still handling is held, not dropped (see [Binary Messages](#62-binary-messages-audio-data)). |
| END received without START | Server logs an error and ignores the message. |
| Duplicate END messages | Server logs a warning; second END is ignored. |
| WebSocket error (network) | Server logs the error and forces a close, triggering automatic end-of-call cleanup. |
| Client disconnects unexpectedly | Server holds the meeting for 60 seconds for the client to reconnect (see [Reconnection](#reconnection)); if it does not, the server writes the call end event and cleans up resources. |
| Meeting consent refuses the meeting, or stops it | Server sends a `CONSENT_STATE` frame with `captureAllowed: false` and closes the connection with code `1008`. See [Meeting Consent Frames](#615-meeting-consent-frames). |

### Reconnection

If a connection drops without an END, the server holds the meeting for 60
seconds. The window is the `AUDIO_RECONNECT_GRACE_MS` environment variable of
the transcriber's container, in milliseconds: `0` turns holding off, and a value
that is not a number keeps the default. The stack does not set this variable
and has no parameter for it, so it is changed only by editing the transcriber's
ECS task definition (registering a new revision with the variable added and
updating the service to it). A stack update that replaces the task definition
does not carry that edit over. To continue the meeting, the client must:
1. Open a new WebSocket connection
2. Send START again with the **same `callId`**, authenticated as the same user
3. Begin streaming audio again

The server then continues the same meeting on the new connection: the audio
recording carries on in the same file, and the transcript carries on from where
the audio recording is, so the two stay in step for the meeting page's player.
Audio the client could not send while it was disconnected is missing from both.
The meeting gets one END, when the client sends it or the connection closes
for good. A START for the same meeting from the same user while its old
connection is still open (the client noticed the drop first) moves the meeting
to the new connection and closes the old one.

A video stream of the meeting (`START_VIDEO` on its own connection) keeps the
times its client gives it. A stream resumed with `videoResume: true` continues
its own timestamps, so if the client went on recording video while the audio
connection was down, the video and its key frames are ahead of the transcript
from then on by the audio that was lost. A stream restarted without
`videoResume` has its key frames placed at the duration of the audio received
so far, which leaves out the time the audio connection was down, as the
transcript does; how closely depends on the client sending audio as it is
captured.

A meeting nobody reconnects to within the 60 seconds is ended as if its
connection had closed for good. A START with a new `callId` starts a new
meeting.

A client that reconnects should therefore:

- Send the meeting's START again on **every** connection it opens for the
  meeting, as that connection's first frame, before any audio. Audio on a
  connection that has had no START is dropped. Send the same START as the first
  time, including its `consent` field: the server checks the meeting's consent
  again on each START.
- Authenticate each connection with current tokens. The tokens are checked when
  the connection opens, so a reconnect made with tokens that have expired since
  the first connection is refused with `401`.
- Hold a bounded amount of the audio captured while disconnected (LMA's clients
  hold about 3 seconds), and send it after the START; it can follow the START
  at once, since the server holds audio that arrives while it handles the
  START. The recording and the transcript carry on from where they were, so whatever was not sent is missing
  from both and they stay in step with each other.
- Stop reconnecting for a meeting the server ended for its consent
  (`CONSENT_STATE` with `captureAllowed: false`), and send no further START for
  it.
- Treat a meeting whose connection could not be restored within the 60 seconds
  as ended, rather than send START for it again, which would start a second
  session (below).

LMA's own clients do this. The Stream Audio page and its embeddable variants
reconnect after 1, 2 and 4 seconds and then every 5 seconds, fetch tokens for
each connection, send the START again, and then send up to the last 3 seconds of
audio captured while disconnected (older audio is dropped). If no connection
opens within 55 seconds of the drop, they stop streaming and say that the
meeting was ended; the meeting keeps what was captured before the drop. The 55
seconds are counted from when the browser reports the connection closed, which
can be later than the server notices. The Desktop Capture App also holds about
3 seconds of audio and sends START on every connection. The browser extension
does not reconnect: a dropped connection stops its capture.

The held meeting lives on the transcriber task that served it. With more than
one task (`TranscriberDesiredTaskCount` above 1), a reconnection can reach a
different task, which starts a second session for the same `callId`: its
transcript starts again from zero, and its audio recording is stored beside the
meeting's first one (`<callId>_<timestamp>.wav` under the recordings prefix)
rather than replacing it, while the meeting keeps its first recording. The same
happens to a reconnection after the 60 seconds.

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
    localSpeakerName: 'me@example.com',
    remoteSpeakerName: 'Remote Participant',
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
    localSpeakerName: 'user@example.com',
    remoteSpeakerName: 'Meeting Audio',
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
            "localSpeakerName": "me@example.com",
            "remoteSpeakerName": "Remote Speaker",
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
- `LOCAL_SPEAKER_NAME` — Speaker name for the microphone channel (older name `AGENT_ID`)
- `REMOTE_SPEAKER_NAME` — Speaker name for the meeting-audio channel (older name `CALL_FROM_NUMBER`)

### Browser Extension

**Location:** `lma-browser-extension-stack/`

A Chrome extension that captures browser tab audio and microphone input, mixes them into a stereo stream, and sends it over WebSocket. Demonstrates:
- Query-string authentication (browser WebSocket limitation)
- AudioWorklet-based audio capture at 16 kHz
- Float32 to Int16 PCM conversion
- Stereo interleaving (tab audio on channel 0, microphone on channel 1)
- SPEAKER_CHANGE messages based on meeting provider participant detection
- Mute/pause functionality

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
| `CONFIGURATION_TABLE_NAME` | — | The configuration table. The Amazon Transcribe options and the recording default are read from it as each meeting starts; the variables below are their fallbacks |
| `SHOULD_RECORD_CALL` | `false` | Whether to record and upload audio to S3, when the Meeting Recording section (**Meeting Recording › Record meeting audio** on the Configuration page) cannot be read. Set from the transcriber nested stack's own `EnableAudioRecording` parameter, which the main stack does not pass |
| `SHOW_SPEAKER_LABEL` | `false` | Default for speaker partitioning (diarization), applied to both channels, when the Amazon Transcribe section does not say. Used **only** when a client sends neither `diarizeSystemChannel` nor `diarizeMicChannel`. The deployment's value is **Amazon Transcribe › Identify speakers within a channel by default** on the Configuration page; this variable, set from the transcriber nested stack's own `ShowSpeakerLabel` parameter, which the main stack does not pass, is its fallback. |
| `DIARIZATION_MIN_RUN_WORDS` | `3` | Minimum same-speaker words for a run to become its own transcript segment. Shorter runs are absorbed into the neighbouring turn. Not set by the CloudFormation template — override on the task definition to re-tune. |
| `DIARIZATION_MIN_RUN_SECONDS` | `0.5` | Minimum duration, in seconds, for the same. A run must clear **both** thresholds. |
| `DIARIZATION_MAX_SEGMENT_SECONDS` | `20` | Longest stretch of audio allowed in one in-progress segment. Bounds how long the live transcript can hold an unlabelled block, since Transcribe caps results near 30s and never labels a partial. `0` disables chunking. |
| `CPU_HEALTH_THRESHOLD` | `50` | CPU usage % threshold for health check |
| `LOCAL_TEMP_DIR` | `/tmp/` | Temporary directory for recording files |
| `WS_LOG_LEVEL` | `info` | Server log level. Set from the stack's `LogLevel` parameter |
| `LOG_FORMAT` | `json` | `json` writes one JSON record per line (see [Log Format](troubleshooting.md#log-format)); `text` writes readable `pino-pretty` lines, for a local run |
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
