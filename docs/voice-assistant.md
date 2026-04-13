# Voice Assistant

## Table of Contents

- [Overview](#overview)
- [Provider Comparison](#provider-comparison)
- [Activation Modes](#activation-modes)
- [Wake Phrase Configuration](#wake-phrase-configuration)
- [Session Management](#session-management)
- [Barge-In](#barge-in)
- [Group Meeting Mode](#group-meeting-mode)
- [Turn-Taking Sensitivity](#turn-taking-sensitivity)
- [Custom System Prompts](#custom-system-prompts)
- [Voice ID](#voice-id)
- [CloudFormation Parameters](#cloudformation-parameters)
- [See Also](#see-also)

## Overview

LMA can optionally add a voice assistant to the Virtual Participant, allowing it to respond verbally during meetings. The voice assistant uses the meeting transcript as context and has access to the same tools as the chat-based meeting assistant, including knowledge base lookups, action item tracking, and other configured capabilities.

## Provider Comparison

| Feature | Nova Sonic 2 | ElevenLabs |
|---|---|---|
| Provider | AWS (Amazon) | Third-party |
| Latency | Low (native AWS) | Moderate (external API) |
| Session duration | Unlimited (auto-refresh every 5 min) | 8-minute sessions (auto-refresh) |
| Group meeting mode | Yes | No |
| Barge-in support | Yes | Yes |
| Custom system prompts | Yes (base/inject/replace modes) | Yes (via ElevenLabs agent config) |
| Voice selection | Multiple AWS voice IDs | Multiple ElevenLabs voice IDs |
| Turn-taking sensitivity | Configurable (HIGH/MEDIUM/LOW) | Via ElevenLabs settings |
| Cost | Bedrock pricing | ElevenLabs pricing |

## Activation Modes

### always_active

The voice agent is always listening and leads the conversation. Sessions auto-refresh automatically (every 5 minutes for Nova Sonic). This mode is best suited for dedicated assistant meetings where the VP is the primary or sole participant interacting with users.

### wake_phrase

The voice agent activates only when a configured wake phrase is detected in the meeting audio. Once activated, the agent stays active for a configurable duration (5-300 seconds, default 30 seconds) before returning to listening mode. This mode is best for normal meetings where the assistant should mostly listen and only respond when directly addressed.

## Wake Phrase Configuration

Configure the wake phrase using the **VoiceAssistantWakePhrase** parameter:

- Provide a comma-separated list of phrases (e.g., `hey alex,ok alex`)
- Matching is case-insensitive
- Multiple phrases allow flexibility in how participants address the assistant

**Pre-connect optimization**: LMA detects the wake phrase in partial (streaming) transcripts and pre-warms the voice provider connection in the background. This eliminates 1-2 seconds of latency that would otherwise occur when establishing the connection after the wake phrase is fully recognized.

## Session Management

### Nova Sonic 2

Nova Sonic has a native 8-minute session timeout. LMA works around this by automatically refreshing sessions every 5 minutes using keep-alive signals (30-second silence chunks). Conversation history is maintained across session refreshes, so the assistant retains full context of the meeting.

### ElevenLabs

ElevenLabs session timeout is configured within the ElevenLabs platform. Auto-refresh is supported to maintain continuous availability during long meetings.

## Barge-In

The voice assistant supports barge-in, allowing meeting participants to interrupt the assistant mid-sentence. This is implemented through separate audio routing for VP meeting audio versus agent output, ensuring that the assistant can detect incoming speech even while it is speaking and stop its current response to listen.

## Group Meeting Mode

*Nova Sonic only.*

Group meeting mode enables passive listening where the assistant monitors the full meeting conversation but only responds when directly addressed. The assistant uses mute/unmute tools to control its participation:

- **Muted**: The assistant listens to the transcript but does not respond
- **Unmuted**: The assistant actively participates in the conversation

This mode is ideal for multi-participant meetings where the assistant should remain unobtrusive until needed.

## Turn-Taking Sensitivity

*Nova Sonic only.*

Turn-taking sensitivity controls how long the assistant waits after detecting a pause in speech before it begins responding:

| Setting | Pause Duration |
|---|---|
| **HIGH** | 1.5 seconds |
| **MEDIUM** (default) | 1.75 seconds |
| **LOW** | 2.0 seconds |

Higher sensitivity means the assistant responds more quickly after a pause, which feels more conversational but may cause the assistant to begin responding before the speaker has finished. Lower sensitivity gives speakers more time to pause mid-thought without triggering a response.

## Custom System Prompts

Three modes are available for configuring the voice assistant's system prompt:

### base

Uses the default LMA prompt, which includes meeting context, available tools, and standard assistant behavior instructions. No customization is applied.

### inject

Appends your custom text to the end of the default LMA prompt. This allows you to add organization-specific instructions, persona details, or behavioral guidelines while retaining all default capabilities and context.

### replace

Completely replaces the default LMA prompt with your custom prompt. Use this when you need full control over the assistant's behavior and are prepared to provide all necessary context and tool instructions yourself.

## Voice ID

The voice used by the assistant is configurable per provider. Set the desired voice ID in the provider-specific configuration:

- **Nova Sonic 2**: Choose from multiple AWS voice IDs available in the Bedrock console
- **ElevenLabs**: Choose from multiple ElevenLabs voice IDs available in your ElevenLabs account

## CloudFormation Parameters

The following CloudFormation parameters control voice assistant behavior:

| Parameter | Values | Description |
|---|---|---|
| **VoiceAssistantProvider** | `none` (default), `elevenlabs`, `amazon_nova_sonic` | Selects the voice provider or disables the voice assistant |
| **VoiceAssistantActivationMode** | `always_active`, `wake_phrase` | Controls whether the assistant is always listening or wake-phrase activated |
| **VoiceAssistantWakePhrase** | Comma-separated phrases | Wake phrases that activate the assistant (used with `wake_phrase` mode) |
| **VoiceAssistantActivationDuration** | `5`-`300` seconds | How long the assistant stays active after wake phrase detection |

## See Also

- [Nova Sonic 2 Setup](nova-sonic-setup.md) -- Configure the AWS Nova Sonic voice provider
- [ElevenLabs Setup](elevenlabs-setup.md) -- Configure the ElevenLabs voice provider
- [Simli Avatar Setup](simli-avatar-setup.md) -- Add a visual avatar to the voice assistant
- [Virtual Participant](virtual-participant.md) -- The VP that hosts the voice assistant
