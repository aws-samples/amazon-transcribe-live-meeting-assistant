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
- [Asking the Assistant to Stop Talking](#asking-the-assistant-to-stop-talking)
- [Meeting Mode](#meeting-mode)
  - [Group Meeting Mode](#group-meeting-mode)
  - [Translator Mode](#translator-mode)
- [Changing Mode During a Meeting](#changing-mode-during-a-meeting)
  - [Chat Commands](#chat-commands)
  - [What Happens When the Mode Changes](#what-happens-when-the-mode-changes)
  - [Reading the Log](#reading-the-log)
  - [Other Providers](#other-providers)
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
| Mode changes during a meeting | Yes ([chat commands](#chat-commands)) | No |
| Barge-in support | Yes | Yes |
| "Stop talking" on request | Yes (tool + prompt + transcript + barge-in) | Yes (transcript + barge-in) |
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

When the interruption is a correction or a new question, the assistant acknowledges it in one or two words ("Got it", "Of course") and then answers the new request. When the interruption is a request for silence, it is handled as described next.

## Asking the Assistant to Stop Talking

Saying something like **"Hey Alex, stop talking"** stops the assistant. It does not explain itself, summarize what it was saying, or offer further help — it stops.

**The assistant going quiet is the signal, not the word.** It often answers "Okay" first, and LMA gives the model room to do so, but the acknowledgement is genuinely best-effort and you should not wait to hear it. It is absent whenever the model does not ask to mute itself within about half a second of the request being recognized, because at that point LMA silences the assistant itself and there is nothing left to say the word with; it can also be absent if the word was generated after the audio had already been cut. Silence is the outcome that is guaranteed: a stop request ends in silence, with or without the acknowledgement, and the next wake phrase brings the assistant back.

**How long "stops" takes.** Not instantly. If the model never asks to mute itself, the assistant is audible for the half-second window and then stops. If it does ask — the case where you hear "Okay" — the acknowledgement is allowed to finish playing, so a model that ignored the instruction to say one word can be heard for up to about two seconds after its own request to be muted. Speaking over the assistant still cuts it off immediately, as it always does.

This works in `normal` mode under either activation mode, and in `group` mode (which itself requires `always_active`), and while other participants are talking. Phrasings that are recognized include "stop", "stop talking", "be quiet", "shut up", "mute yourself" and "that's enough", with or without "please" or "can you".

Three mechanisms act together, so that a stop does not depend on any one of them:

1. **A `mute` tool.** Nova Sonic is given a tool it can call to silence itself, and its system prompt tells it that a request to stop is a request for silence rather than the preface to a new request. This is the only mechanism that can produce the spoken "Okay", because it is the model itself deciding to stop — and it is expected to be the slower of the first two, since the model has to hear the audio, decide, and ask, where the transcript only has to be finalized.
2. **The transcript.** LMA watches completed transcript segments for a stop request addressed to the assistant. This is what makes silence certain, because it does not depend on the model noticing. It is also expected to be the faster route, so it holds back briefly — long enough for the `mute` tool above to arrive and the acknowledgement to be heard — and then silences the assistant anyway if the tool does not come. Matching is deliberately strict: the utterance has to name the assistant by wake phrase and has to contain no other request, so "Hey Alex, can you stop the recording and summarize the last hour?" and "Hey Alex, why did the build stop?" leave the assistant talking.
3. **Audio barge-in.** Speaking over the assistant at all cuts its current utterance short, as described under [Barge-In](#barge-in).

A stop request writes a few `[stop-request]` lines to the Virtual Participant's CloudWatch log stream, recording which mechanism silenced the assistant and how long the model took to ask, so that a meeting where the acknowledgement was missing can be explained rather than guessed at. The lines carry only the route, the timings and the state flags — never the utterance.

**Getting the assistant back.** Being asked to stop silences the assistant until it is addressed again — it is not silenced for the rest of the meeting. Saying a wake phrase makes it audible again, whether that comes in as a fresh `wake_phrase` activation or as a wake phrase in an `always_active` session's transcript.

On an `always_active` deployment a wake phrase is the **only** way back, so it is worth knowing the configured phrase even if you never otherwise need it: an always-on assistant is normally addressed without one. Because the assistant hears the whole meeting, the `mute` tool and its prompt both require a stop request to be addressed to the assistant — by name or wake phrase, or as a direct reply to something it just said — so that participants telling each other to be quiet is read as ordinary conversation and leaves it alone.

Translator mode is not affected: it is paused and resumed only by its own configured trigger phrases (see [Translator Mode](#translator-mode)), so a stop request from the transcript route is ignored there.

On **ElevenLabs**, only the transcript mechanism and audio barge-in apply. The ElevenLabs agent's system prompt and tools live in the ElevenLabs dashboard rather than in LMA, so LMA cannot give it a `mute` tool or amend its instructions — which also means it may keep speaking for a moment, and may say more than one word, before LMA's transcript path drops its audio. There is nothing there to wait for, either: with no tool to call, the transcript path silences an ElevenLabs agent at once rather than holding back for an acknowledgement. To get the one-word behavior there, add the equivalent instruction to the agent's own system prompt in the ElevenLabs dashboard.

## Meeting Mode

The voice assistant supports three meeting modes, configured via the **Meeting Mode** selector on the Nova Sonic Configuration page (admin-only). The `group` and `translator` modes need the assistant to be always listening: if the stack is deployed with `VoiceAssistantActivationMode = wake_phrase` they have no effect and the UI surfaces a warning. On Nova Sonic, the chat commands for those two modes switch activation for the current meeting as well, so they work on a `wake_phrase` deployment — see [Changing Mode During a Meeting](#changing-mode-during-a-meeting).

| Mode | Starts muted? | Speaks when… | Stops when asked to? | Typical use |
|---|---|---|---|---|
| `normal` (default) | No | Activated by wake phrase or always-active session | Yes — `mute` tool, transcript, barge-in | Standard assistant behavior |
| `group` | Yes | A configured wake phrase (e.g. "hey Alex") is detected in the transcript | Yes — `mute` tool, transcript, barge-in | Multi-participant meetings where the assistant should remain unobtrusive until addressed |
| `translator` | No | Every completed utterance | No — paused by its own trigger phrase only | Live bidirectional interpreter between two languages |

### Group Meeting Mode

Group meeting mode enables passive listening where the assistant monitors the full meeting conversation but only speaks when directly addressed. The assistant starts muted and unmutes when a wake phrase is detected in a participant's transcript; after `VoiceAssistantActivationDuration` seconds without further wake-phrase mentions it automatically mutes again.

- **Nova Sonic:** Wake-phrase detection is performed by LMA on Nova's FINAL USER transcripts. A wake phrase that is itself a request to stop ("hey Alex, stop talking") does not unmute the assistant. Muting is silent, with no tone — the same as `normal` mode, and the same whichever of the [stop mechanisms](#asking-the-assistant-to-stop-talking) acts first. A tone is used only in [translator mode](#translator-mode), where the assistant speaks no acknowledgement and so has nothing else to confirm the pause with.
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

LMA replaces the agent's system prompt with a translator-specific prompt parameterized on `translatorLanguageA` / `translatorLanguageB`. No `strands_agent` tool is registered, and the only tool offered is the pause/resume trigger for the current state — `mute` while translating, `unmute` while paused — which is why translator mode does not take stop requests from anywhere else. Nova Sonic speaks the translation directly using the configured `voiceId`. A polyglot voice such as `tiffany` (feminine) or `matthew` (masculine) is recommended so the same voice can render both languages.

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
- Translator mode only takes effect while the assistant is always listening, as does `group`. The default `normal` mode works under either activation mode. On Nova Sonic, `LMA translate` in the meeting chat switches activation for the current meeting as well as the mode — see [Changing Mode During a Meeting](#changing-mode-during-a-meeting) — so the stack parameter is the default rather than the only way in.
- If an utterance is in a language other than the two configured, Nova Sonic falls back to translating it into `translatorLanguageA` per the system prompt.

## Changing Mode During a Meeting

*Nova Sonic only.*

The meeting mode and the activation mode are both read when the Virtual Participant starts, from the Nova Sonic Configuration page and the **VoiceAssistantActivationMode** stack parameter respectively. Participants can also change either one while the meeting is running, by typing a command in the meeting chat.

A runtime change lasts for the rest of that meeting and is not written back to configuration: the configuration page keeps holding the deployment's default, and the next meeting starts in it. Anyone in the meeting may issue these commands, like the existing `LMA leave`.

### Chat Commands

Every command must address LMA by name, and the whole message must be the name plus the command — nothing else. That is what keeps ordinary meeting chat from switching modes: "can LMA translate the slides?" and "the LMA group meets on Tuesdays" are left alone, and a bare "translate" is read as one participant talking to another. Matching is case-insensitive and ignores punctuation, so `@LMA, Translate!` works.

**Meeting mode:**

| Type in chat | Effect |
|---|---|
| `LMA translate` | Switch to translator mode, and to always-listening. Aliases: `LMA translator`, `LMA translate mode`, `LMA translator mode`, `LMA interpret` |
| `LMA assist` | Switch to `normal` assistant mode. Leaves activation alone. Aliases: `LMA assistant`, `LMA assist mode`, `LMA assistant mode`, `LMA normal`, `LMA normal mode` |
| `LMA group` | Switch to group mode — the assistant goes quiet and waits to be addressed by wake phrase — and to always-listening. Alias: `LMA group mode` |

`LMA translate` and `LMA group` both switch activation to always-listening as well, because neither mode works without it. A translator speaks on every completed utterance, so with a wake phrase it would receive no audio until addressed and translate nothing in between. Group mode starts muted and the state machine that unmutes it runs on transcripts that only arrive while a session is open, so with a wake phrase it would never become audible at all.

The same rule applies in the other direction: `LMA wake` is **declined** while the assistant is in translator or group mode, and the reply says to switch to assistant mode first with `LMA assist`. Switching back and then asking for the wake phrase takes two commands, which is deliberate — the alternative is one command silently changing the meeting mode nobody asked about.

**Activation mode:**

| Type in chat | Effect |
|---|---|
| `LMA always` | Always listening. Aliases: `LMA always on`, `LMA always active`, `LMA always listen`, `LMA open mic` |
| `LMA wake` | Listen only after a wake phrase. Aliases: `LMA wake word`, `LMA wake phrase`, `LMA on request` |

`LMA assist` leaves activation where `LMA translate` or `LMA group` put it, because `normal` mode works under either activation mode and the command carries no record of what it was before. Use `LMA wake` to get the wake phrase back.

The assistant replies in chat either way — naming the mode it is now in, or saying that nothing changed and why. A command for the mode the meeting is already in is answered and does nothing else, so it costs the meeting no interruption.

These commands do not overlap with the existing chat commands: `LMA leave` / `LMA end` / `LMA stop` still dismiss the Virtual Participant, and `PAUSE` / `START` still pause and resume transcription. A mode command is acted on whether or not transcription is paused.

### What Happens When the Mode Changes

The meeting mode decides the assistant's system prompt and the set of tools it is given, and both are sent once when its Nova session opens — translator mode most visibly, because it *replaces* the base prompt rather than adding to it. So a mode change is not a flag flip: LMA rebuilds the session around the new configuration, using the same mechanism as the periodic session refresh.

What that means in a live meeting:

- **The assistant pauses briefly.** It cannot hear or answer for the length of a rebuild, the same gap the periodic refresh already produces.
- **The transcript is not affected.** Transcription reads a separate PulseAudio sink that the Virtual Participant feeds itself, and that the assistant's voice reaches through a loopback inside the sound server. Nothing on the mode-change path starts or stops an audio process.
- **The conversation so far is not carried into a new mode.** The next session is built for the mode it is now in, so replaying earlier turns would prime it with a conversation that mode never had. An activation-mode change on its own keeps the conversation, since the mode and the prompt are unchanged.
- **Group mode starts quiet.** Switching to `group` leaves the assistant muted until a wake phrase, exactly as deploying in group mode would.
- **On the wake-phrase path, usually nothing is rebuilt immediately.** No session is held open between activations, so the new mode is picked up by the session the next wake phrase creates. The exception is a change that arrives while somebody has the assistant activated: the session is replaced rather than closed, so the window they opened keeps working.

One known limitation on the optional [MicroVM ASR engine](microvm-asr.md): whether the assistant's own speech is transcribed on its own channel is decided once, when the engine run starts. A meeting switched into translator mode partway through keeps transcribing the assistant for the rest of that run, so translations appear in the transcript alongside the speech they translate. The meeting's own transcript is unaffected either way.

### Reading the Log

Every attempt leaves one line in the Virtual Participant's CloudWatch log stream, tagged `[assistant-toggle]`, whether or not it changed anything:

```
[assistant-toggle] vp=<vpId> call=<callId> route=chat by="Alice" result=applied meetingMode=normal->translator activationMode=wake_phrase->always_active session=rebuilt
```

- `route` — which surface asked (`chat` today)
- `by` — who asked, where the surface knows; `-` otherwise
- `result` — `applied`, `unchanged` (already in that configuration), or `declined`
- both modes appear on every line, with `(unchanged)` on the one nobody asked about, so the whole configuration can be read off any single line
- `session` — whether the model session was rebuilt or left alone
- `detail` — present when nothing was applied, saying why

### Other Providers

**ElevenLabs** declines mode changes during a meeting. Its system prompt and tools live in the ElevenLabs dashboard rather than in LMA, so there is nothing for LMA to rebuild a session around; moving only LMA's own mute and wake-phrase handling would leave the agent following instructions for the mode it is no longer in. The command is answered in chat and logged with `result=declined`.

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
