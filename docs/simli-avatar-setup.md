# Simli Avatar Setup Guide

## Overview

This guide walks you through setting up the Simli avatar integration for the LMA Virtual Participant. When enabled, Simli provides an animated, lip-synced avatar that appears as the Virtual Participant's camera feed in meetings. The avatar's lip movements are driven in real-time by the voice assistant's audio output (AWS Nova Sonic or ElevenLabs).

**What it looks like:** Instead of the Virtual Participant joining with no camera, meeting participants see a realistic animated face that moves its lips in sync with the assistant's speech.

## Prerequisites

- LMA deployed (version 0.2.31 and above)
- A voice assistant configured and working (`amazon_nova_sonic` or `elevenlabs`)
- Simli account with API access

## How It Works

```
Voice Assistant (Nova Sonic / ElevenLabs)
    ↓ Audio output (PCM16 16kHz)
    ↓
Simli Avatar Manager
    ↓ Forwards audio to Simli SDK
    ↓
Simli Cloud (lip-sync rendering)
    ↓ Returns video stream via WebRTC
    ↓
Background Puppeteer Page (renders avatar video)
    ↓ Bridges video via internal RTCPeerConnection
    ↓
Meeting Page (getUserMedia override)
    ↓ Meeting sees avatar as VP's camera
    ↓
Meeting Participants See Animated Avatar
```

## Step 1: Create a Simli Account

### 1.1 Sign Up

1. Go to [simli.com](https://simli.com)
2. Sign up or log in to your account
3. Navigate to your dashboard

### 1.2 Get Your API Key

1. In the Simli dashboard, go to **API Keys** or **Settings**
2. Create a new API key or copy your existing one
3. Save this securely — you'll need it for CloudFormation deployment

### 1.3 Get a Face ID

1. In the Simli dashboard, browse available avatar faces
2. Select a face that suits your use case
3. Copy the **Face ID** for the selected avatar
4. Save this for later use

> **Tip:** Simli offers a variety of avatar faces. Choose one that matches the personality and tone of your voice assistant.

## Step 2: Deploy LMA with Simli Configuration

### 2.1 Required CloudFormation Parameters

When deploying or updating your LMA stack, set these parameters:

| Parameter | Value | Description |
|-----------|-------|-------------|
| `SimliApiKey` | Your Simli API key | API key from Step 1.2 |
| `SimliFaceId` | Your chosen Face ID | Face ID from Step 1.3 |
| `VoiceAssistantProvider` | `amazon_nova_sonic` or `elevenlabs` | A voice assistant must be enabled |
| `VoiceAssistantActivationMode` | `wake_phrase` or `always_active` | Choose activation mode |

> **Important:** Simli avatar is meant to be used with a voice assistant. The avatar is driven by the voice assistant's audio output — without a voice assistant, there is no audio to animate the avatar.

### 2.2 Optional Parameters

These parameters are available in the Virtual Participant stack (`lma-virtual-participant-stack/template.yaml`):

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SimliTransportMode` | `livekit` | Transport mode for Simli connection. `livekit` is more firewall-friendly; `p2p` offers slightly lower latency. |

### 2.3 Environment Variables (Advanced)

The following environment variables are set automatically by CloudFormation but can be overridden for local testing:

| Variable | Default | Description |
|----------|---------|-------------|
| `SIMLI_API_KEY` | (from CFN) | Simli API key |
| `SIMLI_FACE_ID` | (from CFN) | Simli Face ID |
| `SIMLI_TRANSPORT_MODE` | `livekit` | Transport mode (`livekit` or `p2p`) |
| `SIMLI_MAX_SESSION_LENGTH` | `3600` | Maximum avatar session length in seconds |
| `SIMLI_MAX_IDLE_TIME` | `300` | Maximum idle time before session ends (seconds) |

## Step 3: Verify Installation

### 3.1 Check CloudFormation Deployment

1. Go to AWS CloudFormation console
2. Find your LMA stack
3. Verify the nested Virtual Participant stack deployed successfully
4. Check that `SimliApiKey` and `SimliFaceId` parameters are set

### 3.2 Test the Avatar

1. Start a meeting on any supported platform (Zoom, Teams, Webex, or Chime)
2. Invite the Virtual Participant to join
3. When the VP joins, you should see:
   - The VP's camera is **ON** (not turned off as usual)
   - An animated avatar face appears as the camera feed
4. Activate the voice assistant (say "Hey Alex" or use always-active mode)
5. When the assistant speaks, the avatar's lips should move in sync with the audio

### 3.3 Expected Log Messages

In CloudWatch logs for the Virtual Participant, look for:

```
✓ Simli Avatar enabled
  Face ID: <your-face-id>
  Transport mode: livekit
  Max session length: 3600s
  Max idle time: 300s
Initializing Simli Avatar...
✓ Simli page loaded with audio isolation patch (background throttling disabled)
Waiting for Simli avatar to connect...
✓ Simli avatar is ready and visible
✓ Simli Avatar initialized successfully (audio isolated)
✓ Camera and microphone permissions granted for meeting platforms
✓ Simli getUserMedia override injected into meeting page
✓ Simli video stream connected to meeting page
```

## Step 4: Meeting Platform Behavior

When Simli avatar is active, the Virtual Participant's behavior changes slightly per platform:

### Zoom
- Video is kept **ON** (normally the VP turns video off)
- If video is off when joining, the VP automatically clicks to turn it on
- The avatar appears as the VP's camera feed

### Microsoft Teams
- Video toggle is **not clicked** (normally the VP turns video off before joining)
- The avatar stream is provided via the getUserMedia override

### Webex
- Video button is **not clicked** to turn off (normally the VP disables video)
- The avatar appears as the camera feed

### Amazon Chime
- Standard behavior — avatar is provided via getUserMedia override

## Troubleshooting

### Issue: Avatar Not Appearing (Camera Off)

**Cause:** Simli initialization failed or VP turned off camera

**Solution:**
- Check CloudWatch logs for `Failed to initialize Simli Avatar` errors
- Verify `SIMLI_API_KEY` and `SIMLI_FACE_ID` are set correctly
- Ensure the Simli API key is valid and has not expired
- Check that the Face ID exists in your Simli account

### Issue: Avatar Shows But Lips Don't Move

**Cause:** Audio not being forwarded to Simli

**Solution:**
- Verify a voice assistant is configured and working (`VoiceAssistantProvider` is not `none`)
- Check logs for `🎭 Sent X audio chunks to Simli avatar` messages
- If no audio chunk messages appear, the voice assistant may not be producing audio
- Test the voice assistant independently first (say "Hey Alex, are you there?")

### Issue: "Simli Avatar: SDK Load Failed"

**Cause:** Simli JS SDK failed to load from CDN

**Solution:**
- Check network connectivity from the ECS task
- The SDK loads from `esm.sh` with a fallback to `unpkg.com`
- Verify the ECS task has outbound internet access
- Check if a firewall or proxy is blocking CDN access

### Issue: Video Track Keeps Reconnecting

**Cause:** Video track ending or becoming stale

**Solution:**
- This is normal behavior — the system polls every 5 seconds and automatically reconnects dead video tracks
- Check logs for `Simli video track needs re-connection...` messages
- If reconnections are very frequent, check Simli service status
- Verify the `SIMLI_MAX_IDLE_TIME` is sufficient for your meeting duration

### Issue: Audio Echo or Feedback

**Cause:** Simli's echoed audio leaking into meeting audio

**Solution:**
- This should not happen — multiple audio isolation layers are in place
- Check logs for `[Simli-AudioBlock] Blocked AudioNode connection to speakers`
- Verify the audio isolation patch is installed: `[Simli-AudioBlock] AudioContext patch installed`
- Check that all media elements are muted: `[Simli-AudioBlock] All media elements muted and audio tracks disabled`

### Issue: "Simli avatar did not become ready within timeout"

**Cause:** Simli connection taking too long

**Solution:**
- This is a warning, not a fatal error — the system continues anyway
- Check Simli service status at [simli.com](https://simli.com)
- Verify your API key has sufficient quota
- Try switching transport mode (`livekit` ↔ `p2p`)
- Check network latency from the ECS task to Simli servers

### Issue: Avatar Works Initially But Stops Mid-Meeting

**Cause:** Session timeout or idle timeout reached

**Solution:**
- Increase `SIMLI_MAX_SESSION_LENGTH` (default: 3600 seconds = 1 hour)
- Increase `SIMLI_MAX_IDLE_TIME` (default: 300 seconds = 5 minutes)
- The idle timer resets each time audio is sent to Simli
- For long meetings, ensure the voice assistant is used periodically

## Security Considerations

### API Key Security

- The Simli API key is stored as a `NoEcho` CloudFormation parameter (encrypted, not visible in console)
- The key is passed to the ECS task as an environment variable
- Never commit API keys to source control
- Rotate API keys periodically via the Simli dashboard

### Data Flow

- Voice assistant audio is sent to Simli's cloud service for lip-sync rendering
- Only the audio output from the voice assistant is sent — meeting participant audio is **not** sent to Simli
- Video is rendered by Simli and streamed back via WebRTC
- The avatar video stays within the browser process and is provided to the meeting platform

### Network Requirements

- Outbound HTTPS access to Simli CDN (`esm.sh`, `unpkg.com`) for SDK loading
- Outbound WebRTC access to Simli servers for video streaming
- The `livekit` transport mode is more firewall-friendly than `p2p`

## Cost Considerations

### Simli Pricing

- Simli charges based on session duration and usage
- Check [simli.com](https://simli.com) for current pricing
- The avatar session runs for the duration of the meeting
- Idle time (no audio being sent) still counts toward session duration

### Optimization Tips

- Use **wake phrase mode** for the voice assistant to reduce overall meeting duration where the assistant is active
- Set appropriate `SIMLI_MAX_IDLE_TIME` to end sessions when the assistant is not being used
- For short meetings, the default session limits are sufficient
- Monitor usage in your Simli dashboard

## Summary

✅ **What You Get:**
- Animated lip-synced avatar as the Virtual Participant's camera feed
- Real-time lip synchronization driven by voice assistant audio
- Works with Zoom, Teams, Webex, and Chime
- Automatic video track management and reconnection
- Audio isolation to prevent echo and feedback

✅ **What You Need:**
- Simli account with API key and Face ID
- A voice assistant configured (Nova Sonic or ElevenLabs)
- 2 CloudFormation parameters (`SimliApiKey` + `SimliFaceId`)

✅ **Key Features:**
- Seamless integration with existing voice assistants
- No changes needed to meeting platform configuration
- Automatic camera management per platform (Zoom, Teams, Webex)
- Resilient video track with automatic reconnection
- Multiple layers of audio isolation
- Configurable transport mode, session length, and idle timeout

That's it! Your Virtual Participant now has a face — an animated avatar that brings the AI assistant to life in your meetings!
