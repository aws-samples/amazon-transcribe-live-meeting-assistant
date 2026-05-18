---
title: "Voice Assistant"
---

# Voice Assistant

## Table of Contents

- [Overview](#overview)
- [Provider Comparison](#provider-comparison)
- [Activation Modes](#activation-modes)
  - [always_active](#always_active)
  - [wake_phrase](#wake_phrase)
- [Wake Phrase Configuration](#wake-phrase-configuration)
- [Session Management](#session-management)
  - [Nova Sonic 2](#nova-sonic-2)
  - [ElevenLabs](#elevenlabs)
- [Barge-In](#barge-in)
- [Meeting Mode](#meeting-mode)
  - [Group Meeting Mode](#group-meeting-mode)
  - [Translator Mode](#translator-mode)
- [Turn-Taking Sensitivity](#turn-taking-sensitivity)
- [Custom System Prompts](#custom-system-prompts)
  - [base](#base)
  - [inject](#inject)
  - [replace](#replace)
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
| Meeting modes | `normal`, `group`, `translator` | `normal`, `group`, `translator` |
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

## Meeting Mode

The voice assistant supports three meeting modes, configured via the **Meeting Mode** selector on the Nova Sonic Configuration page (admin-only). All three modes require the stack to be deployed with `VoiceAssistantActivationMode = always_active` — in `wake_phrase` mode, `group` and `translator` have no effect and the UI will surface a warning.

| Mode | Starts muted? | Speaks when… | Typical use |
|---|---|---|---|
| `normal` (default) | No | Activated by wake phrase or always-active session | Standard assistant behavior |
| `group` | Yes | A configured wake phrase (e.g. "hey Alex") is detected in the transcript | Multi-participant meetings where the assistant should remain unobtrusive until addressed |
| `translator` | No | Every completed utterance | Live bidirectional interpreter between two languages |

### Group Meeting Mode

Group meeting mode enables passive listening where the assistant monitors the full meeting conversation but only speaks when directly addressed. The assistant starts muted and unmutes when a wake phrase is detected in a participant's transcript; after `VoiceAssistantActivationDuration` seconds without further wake-phrase mentions it automatically mutes again.

- **Nova Sonic:** Wake-phrase detection is performed by LMA on Nova's FINAL USER transcripts, and a `mute` tool is registered so the assistant can re-mute when asked (e.g. "stop talking").
- **ElevenLabs:** Wake-phrase detection is performed by LMA on ElevenLabs `user_transcript` messages. The ElevenLabs agent's persona (system prompt, first message, tools, voice, language) is configured in the ElevenLabs dashboard — LMA does not override it. Configure your ElevenLabs agent normally and set LMA's Meeting Mode to `group`; LMA will drop the agent's audio output until a wake phrase is detected.

### Translator Mode

Demo:


https://github.com/user-attachments/assets/160be5d7-010e-446c-82b3-f496ba2c5969


Translator mode turns the voice assistant into a live bidirectional interpreter between two configured languages. The assistant listens continuously, automatically detects which of the two languages each utterance is in, and speaks the translation in the other language.

Configuration attributes (set on the Nova Sonic Configuration page or directly in the `CustomNovaSonicConfig` DynamoDB item):

- `meetingMode`: `translator`
- `translatorLanguageA`: the first language (default `English`)
- `translatorLanguageB`: the second language (default `Spanish`)

#### Nova Sonic

LMA replaces the agent's system prompt with a translator-specific prompt parameterized on `translatorLanguageA` / `translatorLanguageB`, and registers no tools (no `strands_agent`, no `mute`). Nova Sonic speaks the translation directly using the configured `voiceId`. A polyglot voice such as `tiffany` (feminine) or `matthew` (masculine) is recommended so the same voice can render both languages.

Example:

- English speaker: "Hi, how are you today?"
- Assistant (in French): "Salut, comment vas-tu aujourd'hui ?"
- French speaker: "Très bien, merci, et toi ?"
- Assistant (in English): "Very well, thank you, and you?"

#### ElevenLabs

The ElevenLabs agent's persona is configured in the ElevenLabs dashboard — LMA does not override it. To use translator mode with ElevenLabs:

1. In the ElevenLabs dashboard, create or edit the agent and configure it as a bidirectional interpreter between your two languages. For example, set the agent's system prompt to something like:
   > "You are a live bidirectional interpreter between English and Spanish. For every utterance you hear, detect which of these two languages it is in and translate it into the other language. Speak only the translation, in the target language. Do not greet, explain, or add commentary. Preserve names, numbers, and meaning. If an utterance is unclear or in a third language, translate it into English."
2. Set the agent's first message to empty.
3. Choose a voice that can render both languages naturally (an ElevenLabs multilingual voice).
4. In LMA, set Meeting Mode to `translator` and set `translatorLanguageA` / `translatorLanguageB` to match your agent's configured languages. LMA reads these to keep its own state and docs consistent, but does not push them to ElevenLabs.
5. Deploy the main stack with `VoiceAssistantActivationMode = always_active` so LMA keeps the WebSocket open and the agent can translate every utterance without a wake phrase.

#### Limitations

- A single agent voice is used for both translation directions. For different voices per direction, two concurrent agents would be required (not supported today).
- Meeting Mode settings only take effect when the stack is deployed with `VoiceAssistantActivationMode = always_active`.
- If an utterance is in a language other than the two configured, Nova Sonic falls back to translating it into `translatorLanguageA` per the system prompt.

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
