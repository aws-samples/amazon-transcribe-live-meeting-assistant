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
  - [Nova Sonic Concurrency Quota](#nova-sonic-concurrency-quota)
  - [ElevenLabs](#elevenlabs)
- [Barge-In](#barge-in)
- [How the Assistant Appears in the Transcript](#how-the-assistant-appears-in-the-transcript)
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
| Session duration | Unlimited (auto-refresh every 4 min) | 8-minute sessions (auto-refresh) |
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

The voice agent is always listening and leads the conversation. Sessions auto-refresh automatically (every 4 minutes for Nova Sonic). This mode is best suited for dedicated assistant meetings where the VP is the primary or sole participant interacting with users.

### wake_phrase

The voice agent activates only when a configured wake phrase is detected in the meeting audio. Once activated, the agent stays active until it has been idle for the configured duration (`VoiceAssistantActivationDuration`, 5-300 seconds, default 30 seconds), and then closes its session and returns to listening mode. This mode is best for normal meetings where the assistant should mostly listen and only respond when directly addressed.

The duration is idle time, not a deadline from the wake phrase. The countdown starts again whenever the assistant is busy and whenever someone engages with it: it does not run while a lookup is in progress or while the meeting can still hear the assistant's answer (Nova Sonic delivers speech several times faster than it is played, so the answer is still playing well after the last of it has arrived), and it restarts when the assistant is addressed by wake phrase again or spoken over. Any wake phrase heard while the window is running restarts it, including "Hey Alex, stop", so a participant who has just silenced the assistant can still follow up. A question asked just as the window is about to lapse is given about three seconds for the assistant to start answering; conversation among the other participants is not engagement and can delay closing only that way, by a few seconds at a time and never beyond twice the configured duration. Because the session stays open for as long as the assistant is audible, asking it to stop while it is still talking always reaches a session that can hear the request. These rules apply to both Amazon Nova Sonic and ElevenLabs; on ElevenLabs, "busy" means a `strands_agent` client tool call is running or the agent's audio is still arriving or queued for playback.

## Wake Phrase Configuration

Configure the wake phrase using the **VoiceAssistantWakePhrase** parameter:

- Provide a comma-separated list of phrases (e.g., `hey alex,ok alex`)
- Matching is case-insensitive
- Multiple phrases allow flexibility in how participants address the assistant

**Pre-connect optimization**: LMA detects the wake phrase in partial (streaming) transcripts and pre-warms the voice provider connection in the background. This eliminates 1-2 seconds of latency that would otherwise occur when establishing the connection after the wake phrase is fully recognized.

## Session Management

### Nova Sonic 2

Nova Sonic has a native 8-minute session timeout. LMA works around this by automatically refreshing sessions every 4 minutes using keep-alive signals (30-second silence chunks). Conversation history is maintained across session refreshes, so the assistant retains full context of the meeting.

### Nova Sonic Concurrency Quota

Amazon Bedrock limits how many Nova Sonic streams one AWS account can have open at the same time in a region. The quota is **On-demand InvokeModel concurrent requests for Amazon Nova 2 Sonic**, it is **20** per account per region, and it cannot be raised through Service Quotas. Every Virtual Participant's voice assistant in the account and region draws on the same 20, whichever LMA stack it belongs to, along with anything else in the account that uses Nova Sonic.

How many streams a meeting holds:

| What the meeting is doing | Nova Sonic streams |
|---|---|
| Wake-phrase assistant, not addressed | 0 — no stream is open until the wake phrase |
| Wake-phrase assistant, during an activation window | 1 |
| Always-listening assistant (`normal` or `group` mode) | 1, for the whole meeting |
| Translator | 2 to 3 while people are speaking (see [Translator Mode](#nova-sonic)), 1 when the meeting is quiet |
| Any meeting with the meeting-assistant tool, at the start | 1 more for a few seconds, to record the "One moment, let me look that up" clip (and in translator mode one more, for the "I've stopped translating" clip) |

So, as a rule of thumb, an account can run about six to ten translator meetings at once in one region, or about twenty meetings with an assistant listening, before Bedrock starts refusing new streams. Measured: of 24 streams opened at once in one region, 20 opened and 4 were refused within about 300 ms with `ThrottlingException: Too many connections, please wait before trying again.`

When Bedrock refuses a stream, the Virtual Participant degrades rather than failing:

- **A translator** that cannot open the session for its next turn keeps translating on the session it already has — one stream instead of two or three, which is how translator mode worked before it had a session per turn and is more prone to drift over a long conversation. It tries a new session again on a later turn, after a backoff that starts at one to two seconds and grows to at most a minute while the refusals continue.
- **A wake-phrase assistant** that cannot open its session retries three more times with a randomised backoff, about eight seconds in all at most. Saying the wake phrase again while it retries opens no further stream; the latest question asked is the one answered. If it still cannot connect it says nothing and is not activated, so the next wake phrase tries again. An always-listening assistant whose session is refused retries in the background with a growing backoff, up to 30 seconds apart.
- **The "One moment, let me look that up" clip** is rendered in the background and never delays the Virtual Participant joining. A render that is refused is retried up to three more times over about half a minute and then skipped; the assistant then simply has no spoken acknowledgement for a lookup.

Each refusal writes a `[nova-throttle] context=<assistant-session | translator-turn | tool-ack-clip>` line to the Virtual Participant's CloudWatch log stream, a translator that keeps its session writes `[translator-turn] action=kept-session reason=throttled`, and an assistant that gives up writes `[nova-throttle] action=gave-up-activation`. Each refusal is also counted in the `NovaSonicStreamsThrottled` metric, graphed on the stack's CloudWatch dashboard (see [Monitoring](monitoring.md#metrics-lma-publishes)); Amazon Bedrock counts the same refusals in its account-wide `InvocationThrottles` metric, which the stack's `bedrock-throttling` alarm reads. A sustained non-zero value there means voice assistants in the account are degrading; the remedy is fewer concurrent voice-assistant meetings in that account and region, or spreading deployments across regions or accounts, since the quota itself cannot be raised.

### ElevenLabs

ElevenLabs session timeout is configured within the ElevenLabs platform. Auto-refresh is supported to maintain continuous availability during long meetings.

## Barge-In

The voice assistant supports barge-in, allowing meeting participants to interrupt the assistant mid-sentence. This is implemented through separate audio routing for VP meeting audio versus agent output, ensuring that the assistant can detect incoming speech even while it is speaking and stop its current response to listen.

When the interruption is a correction or a new question, the assistant acknowledges it in one or two words ("Got it", "Of course") and then answers the new request. When the interruption is a request for silence, it is handled as described next.

## How the Assistant Appears in the Transcript

The meeting transcript keeps the participants and the assistant apart, on both transcription engines. On Amazon Transcribe the Virtual Participant sends one stream with two channels and channel identification on: the meeting audio on the first channel and the assistant's voice on the second. On the [MicroVM ASR engine](microvm-asr.md) it opens one session for each. Either way:

- Participants' lines are on the `CALLER` channel, named after the attendee the meeting showed speaking during each line (never after the Virtual Participant, even while the meeting shows it speaking); see [How Speaker Names Are Assigned](virtual-participant.md#how-speaker-names-are-assigned). Only these lines are checked for wake phrases and [stop requests](#asking-the-assistant-to-stop-talking), so the assistant cannot wake or stop itself.
- The assistant's lines are on the `AGENT` channel, under the Virtual Participant's name, and are never [voice-separated](virtual-participant.md#transcription-engine).
- A participant who speaks over the assistant, including to interrupt it, gets a line of their own instead of having their words merged into the assistant's.

In [translator mode](#translator-mode) the assistant's channel on Amazon Transcribe is still sent but not written to the transcript; its lines come from the text of each translation instead. On the MicroVM ASR engine that holds only for a meeting that starts in translator mode — see [Translator Mode](#translator-mode) for a meeting switched into it. A two-channel Amazon Transcribe stream is billed for its duration, the same as a single mono stream. The S3 audio recording and the video recording still contain both the meeting and the assistant, mixed.

## Asking the Assistant to Stop Talking

Saying something like **"Hey Alex, stop talking"** stops the assistant. It does not explain itself, summarize what it was saying, or offer further help — it stops.

**The assistant going quiet is the signal, not the word.** It often answers "Okay" first, and LMA gives the model room to do so, but the acknowledgement is genuinely best-effort and you should not wait to hear it. It is absent whenever the model does not ask to mute itself within about half a second of the request being recognized, because at that point LMA silences the assistant itself and there is nothing left to say the word with. When the model does ask, it often asks first and says the word afterwards, in its reply to being muted; LMA plays that reply too, provided it starts within 1.5 seconds of the model's request to be muted, and cuts it off after 1.5 seconds of audio. Silence is the outcome that is guaranteed: a stop request ends in silence, with or without the acknowledgement, and the next wake phrase brings the assistant back.

**How long "stops" takes.** Not instantly. If the model never asks to mute itself, the assistant is audible for the half-second window and then stops. If it does ask — the case where you hear "Okay" — the acknowledgement is allowed to finish playing, whether the model said it before or after asking, so a model that ignored the instruction to say one word can be heard for up to about three seconds after its own request to be muted, of which at most 1.5 seconds is a reply that started after the request. A reply that starts more than 1.5 seconds after that request is not played at all. The reply is recognized by its timing, not its content: in an `always_active` session, a reply to something else said in the meeting that happens to start inside that window is played the same way, up to the same 1.5-second limit. Speaking over the assistant still cuts it off immediately, as it always does.

This works in `normal` mode under either activation mode, and in `group` mode (which itself requires `always_active`), and while other participants are talking. Phrasings that are recognized include "stop", "stop talking", "be quiet", "shut up", "mute yourself" and "that's enough", with or without "please" or "can you".

Three mechanisms act together, so that a stop does not depend on any one of them:

1. **A `mute` tool.** Nova Sonic is given a tool it can call to silence itself, and its system prompt tells it that a request to stop is a request for silence rather than the preface to a new request. This is the only mechanism that can produce the spoken "Okay", because it is the model itself deciding to stop — and it is expected to be the slower of the first two, since the model has to hear the audio, decide, and ask, where the transcript only has to be finalized.
2. **The transcript.** LMA watches completed transcript segments for a stop request addressed to the assistant. This is what makes silence certain, because it does not depend on the model noticing. It is also expected to be the faster route, so it holds back briefly — long enough for the `mute` tool above to arrive and the acknowledgement to be heard — and then silences the assistant anyway if the tool does not come. Matching is deliberately strict: the utterance has to name the assistant by wake phrase and has to contain no other request, so "Hey Alex, can you stop the recording and summarize the last hour?" and "Hey Alex, why did the build stop?" leave the assistant talking.
3. **Audio barge-in.** Speaking over the assistant at all cuts its current utterance short, as described under [Barge-In](#barge-in).

A stop request writes a few `[stop-request]` lines to the Virtual Participant's CloudWatch log stream, recording which mechanism silenced the assistant and how long the model took to ask, so that a meeting where the acknowledgement was missing can be explained rather than guessed at. An acknowledgement spoken after the model's request to be muted is recorded as `action=ack-started` followed by `action=ack-played` (or `action=ack-capped` if it reached the 1.5-second limit), each with the milliseconds of audio played. The lines carry only the route, the timings and the state flags — never the utterance.

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

While it is muted the assistant still hears the meeting, but it does not run meeting-assistant lookups (the `strands_agent` tool — document search, meeting history, web search) for conversation that is not addressed to it. If the model asks for one anyway, LMA answers it without invoking the Strands Lambda and writes a `[lookup] action=skipped reason=not-addressed` line to the Virtual Participant's log. A question that starts with a wake phrase unmutes the assistant first, so its lookup runs as usual, as do follow-ups while the assistant is still audible. The same applies to an assistant in `normal` mode that has been asked to stop talking, until it is next addressed by wake phrase, and to ElevenLabs.

- **Nova Sonic:** Wake-phrase detection is performed by LMA on Nova's FINAL USER transcripts. A wake phrase that is itself a request to stop ("hey Alex, stop talking") does not unmute the assistant. Muting is silent, with no tone — the same as `normal` mode, and the same whichever of the [stop mechanisms](#asking-the-assistant-to-stop-talking) acts first. A tone is used only in [translator mode](#translator-mode), where the assistant speaks no acknowledgement and so has nothing else to confirm the pause with.
- **ElevenLabs:** Wake-phrase detection is performed by LMA on ElevenLabs `user_transcript` messages. The ElevenLabs agent's persona (system prompt, first message, tools, voice, language) is configured in the ElevenLabs dashboard — LMA does not override it. Configure your ElevenLabs agent normally and set LMA's Meeting Mode to `group`; LMA will drop the agent's audio output until a wake phrase is detected.

### Translator Mode

Demo:


https://github.com/user-attachments/assets/160be5d7-010e-446c-82b3-f496ba2c5969


Translator mode turns the voice assistant into a live bidirectional interpreter between two configured languages. The assistant listens continuously, automatically detects which of the two languages each utterance is in, and speaks the translation in the other language.

Configuration attributes (set on the Nova Sonic Configuration page, which stores them in the shared [configuration store](configuration-store.md)):

- `meetingMode`: `translator`
- `translatorLanguageA`: the first language (default `English`)
- `translatorLanguageB`: the second language (default `Spanish`)

#### Nova Sonic

LMA replaces the agent's system prompt with a translator-specific prompt parameterized on `translatorLanguageA` / `translatorLanguageB`. No `strands_agent` tool is registered, and the only tool offered is the pause/resume trigger for the current state — `mute` while translating, `unmute` while paused — which is why translator mode does not take stop requests from anywhere else. Translator mode is left by saying "stop translating"; see [Changing Mode During a Meeting](#changing-mode-during-a-meeting). Nova Sonic speaks the translation directly using the configured `voiceId`. A polyglot voice such as `tiffany` (feminine) or `matthew` (masculine) is recommended so the same voice can render both languages.

Example:

- English speaker: "Hi, how are you today?"
- Assistant (in French): "Salut, comment vas-tu aujourd'hui ?"
- French speaker: "Très bien, merci, et toi ?"
- Assistant (in English): "Very well, thank you, and you?"

**How translations appear in the transcript.** The meeting transcript is produced in the meeting's configured transcription language, which can only render one side of a bilingual conversation. So in translator mode the assistant's own channel is not written to the transcript, and each translation is written instead as the assistant's line, from the text Nova Sonic generated for that turn; if the assistant is interrupted, the line can include words it did not get to say. On Amazon Transcribe the participants are on a channel of their own (see [How the Assistant Appears in the Transcript](#how-the-assistant-appears-in-the-transcript)), so everything a participant says is transcribed in full, including while the assistant is speaking. Changing mode does not restart transcription, so this takes effect immediately on Amazon Transcribe. On the MicroVM ASR engine the assistant's voice gets its own transcript channel when a meeting starts outside translator mode, and that channel keeps transcribing the assistant's audio in the meeting language after a switch into translator mode; translations are written from Nova Sonic's text only for meetings that start in translator mode (or once that channel stops). On ElevenLabs the translation is not written to the transcript.

**One session per translated turn.** A Nova Sonic session's translations drift over the course of a conversation: after a few turns it starts transcribing one language as the other and "translating" that back, so participants hear their own words repeated. So when a session starts speaking a translation, the meeting's audio moves to a new session, opened at that moment, and the audio heard since the speaker finished is replayed into it first, so the next speaker's opening words reach it even while it is still connecting. The previous session is left to finish its translation — it hears silence from then on, so somebody starting to speak does not cut it off — and is closed once its text for the transcript has arrived. If the next translation starts arriving while the previous one is still arriving, it is held back and played after it. No conversation history is carried into a translator session, including on the periodic session refresh. Each hand-over writes a line to the Virtual Participant log: `[translator-turn] n=<turn> from=<session> to=<session> backlog_ms=<replayed audio> draining=<sessions still finishing>`. Each previous session's end writes `[translator-turn] closed <session>: <reason> after <ms> ms`, where the reason is `answered` (its final text arrived), `timed-out` (it had not finished after 30 seconds), `refresh` (the session it handed over to was replaced, for a pause, a mode change or the periodic refresh) or `closed` (the assistant's session was closed), or `[translator-turn] <session> ended before it was closed` if its stream ended on its own.

**What this costs.** A translator meeting now holds more than one Nova Sonic stream at a time: the current one, plus each previous session until it has finished its translation, which is usually about five seconds and at most 30. With turns close together, three can be open at once, and every translated turn opens a new stream. Each of those counts against your account's Amazon Bedrock quota of 20 concurrent Nova Sonic streams per region; see [Nova Sonic Concurrency Quota](#nova-sonic-concurrency-quota) for what that allows and what happens when it is reached. Billing changes too: the silence a previous session hears while it finishes is billed as speech input, the audio replayed into the next session (about half a second per turn, at most three) is billed twice, and every session is sent the translator prompt again.

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
- The pair can also be chosen for one meeting from the chat with `LMA translate <language>`; see [Choosing the Language from Chat](#choosing-the-language-from-chat).
- If an utterance is in a language other than the two configured, Nova Sonic falls back to translating it into `translatorLanguageA` per the system prompt.
- **The assistant can occasionally speak in the wrong language.** Which language Nova Sonic speaks is decided by the model, so a turn can come out in the language that was just spoken rather than in its translation. Within one Nova Sonic session this gets more likely with every turn, so on Nova Sonic each translated turn gets a session of its own (see **One session per translated turn** under [Translator Mode](#translator-mode)); what remains is the occasional single turn. The likeliest case is two people speaking back to back with no pause, which Nova Sonic can treat as one turn and translate into one language. The meeting transcript may not show it: translations are written from Nova Sonic's text, and in one recorded meeting the text was in the right language while the speech was not.

## Changing Mode During a Meeting

*Nova Sonic only.*

The meeting mode and the activation mode are both read when the Virtual Participant starts, from the Nova Sonic Configuration page and the **VoiceAssistantActivationMode** stack parameter respectively. Participants can also change either one while the meeting is running, by typing a command in the meeting chat.

A runtime change lasts for the rest of that meeting and is not written back to configuration: the configuration page keeps holding the deployment's default, and the next meeting starts in it. Anyone in the meeting may issue these commands, like the existing `LMA leave`.

**For operators:** this makes **VoiceAssistantActivationMode** a default rather than a fixed setting. `LMA always` — and `LMA translate` and `LMA listen`, which both need an always-listening assistant — will put a meeting on always-listening whatever the stack was deployed with, on the word of any participant including a guest. Always-listening streams meeting audio to the model continuously rather than only after a wake phrase, so it changes both what the meeting costs and when audio leaves the Virtual Participant. There is no per-deployment switch to disable these commands; open an issue if your deployment needs one.

### Chat Commands

Every command must address LMA by name, and the whole message must be the name plus the command — nothing else. That is what keeps ordinary meeting chat from switching modes: "can LMA translate the slides?" and "can LMA listen in?" are left alone, and a bare "translate" is read as one participant talking to another. Matching is case-insensitive and ignores punctuation, so `@LMA, Translate!` works.

**Meeting mode:**

| Type in chat | Effect |
|---|---|
| `LMA translate` | Switch to translator mode, and to always-listening, keeping the language pair in use (the configured one unless changed in this meeting). Aliases: `LMA translator`, `LMA translate mode`, `LMA translator mode`, `LMA interpret` |
| `LMA translate <language>` | The same, translating between the meeting's language and `<language>` for the rest of this meeting — see [Choosing the Language from Chat](#choosing-the-language-from-chat). Also `LMA translate to <language>`, `LMA translate into <language>` |
| `LMA assist` | Switch to `normal` assistant mode. After `LMA translate` or `LMA listen`, also restores the activation mode they replaced (see below). Aliases: `LMA assistant`, `LMA assist mode`, `LMA assistant mode`, `LMA normal`, `LMA normal mode` |
| `LMA listen` | Switch to listen mode — the **Group Meeting** mode on the Nova Sonic Configuration page (`group`): the assistant goes quiet and waits to be addressed by wake phrase — and to always-listening. Aliases: `LMA listen mode`, `LMA listening mode`, and `LMA group` / `LMA group mode`, which are also accepted (the configuration's name for the mode) but not listed by `LMA help` |

`LMA listen` chooses the meeting mode and is a different command from `LMA always listen`, an alias of `LMA always` that only changes activation; each is matched as a whole message, so neither is mistaken for the other.

`LMA translate` and `LMA listen` both switch activation to always-listening as well, because neither mode works without it. A translator speaks on every completed utterance, so with a wake phrase it would receive no audio until addressed and translate nothing in between. Group mode starts muted and the state machine that unmutes it runs on transcripts that only arrive while a session is open, so with a wake phrase it would never become audible at all.

The same rule applies in the other direction: `LMA wake` is **declined** while the assistant is in translator or listen mode, and the reply says to switch to assistant mode first with `LMA assist` (in translator mode it also says that saying "stop translating" does the same). (On a deployment configured as group or translator mode with `wake_phrase` activation — the combination the configuration page warns about — `LMA wake` is instead answered as a no-op, since that is already what the meeting is doing. `LMA assist` is the way out of it.) Asking for the wake phrase is declined rather than silently changing the meeting mode nobody asked about; `LMA assist` on its own then brings the wake phrase back, as described below.

**Activation mode:**

| Type in chat | Effect |
|---|---|
| `LMA always` | Always listening. Aliases: `LMA always on`, `LMA always active`, `LMA always listen`, `LMA open mic` |
| `LMA wake` | Listen only after a wake phrase. Aliases: `LMA wake word`, `LMA wake phrase`, `LMA on request` |

When `LMA translate` or `LMA listen` switched the assistant to always-listening, `LMA assist` switches it back to the activation mode it was in before — so a wake-phrase meeting that was translated for a while returns to the wake phrase, and the reply says so (`switched to assistant mode, now listening for the wake phrase.`). Going from one of those modes straight to the other keeps the original activation mode to restore. The exception is an explicit choice in between: if somebody typed `LMA always` (or `LMA wake`) while translator or listen mode was running and the assistant accepted it or was already doing that, `LMA assist` leaves activation as it is. A declined `LMA wake` does count as a choice: it is remembered, and the next `LMA assist` switches to the wake phrase even if the meeting was always listening before translator or listen mode started. The restore is logged as `[assistant-toggle] requesting restore of activationMode=<mode> on leaving an always-listening meeting mode for meetingMode=normal` just before the usual toggle line, which says whether it applied. Nothing here is persisted; the next meeting starts in the configured activation mode.

The assistant replies in chat either way — naming the mode it is now in, or saying that nothing changed and why. A command for the mode the meeting is already in is answered and does nothing else, so it costs the meeting no interruption.

These commands do not overlap with the existing chat commands: `LMA leave` / `LMA end` / `LMA stop` still dismiss the Virtual Participant, `LMA pause` / `LMA start` pause and resume transcription, and `LMA help` posts the list of commands in chat — including these, in their canonical form, when the assistant can change mode. A mode command is acted on whether or not transcription is paused.

### Choosing the Language from Chat

`LMA translate <language>` switches to translator mode with the pair (the meeting's language, `<language>`) for the rest of the meeting. Typed again while translating, it changes the pair and rebuilds the session around it. `LMA translate` on its own keeps whichever pair is in use.

- **The meeting's language** is the one it is transcribed in: the Virtual Participant's Transcribe language code (`en-US` is English, `fr-CA` is French, and so on). With language identification it is the preferred language, if one is set. When there is no single language to go on, or the meeting is transcribed in a language Nova Sonic does not speak (Japanese, say), the configured `translatorLanguageA` is kept as the first half (or `translatorLanguageB`, if `translatorLanguageA` is the language asked for). Asking for the meeting's own language is answered with a reminder to name the other one, and changes nothing.
- **Supported languages** are the ones Amazon Nova 2 Sonic [documents speech support for](https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-language-support.html): English, Spanish, French, German, Italian, Portuguese and Hindi. Names are matched case- and accent-insensitively, in English or in the language itself and a few neighbours — `Spanish`, `español`, `espanol` and `castellano` all work. One language is named at a time: `LMA translate English to French` is answered with a reminder to name only the other language. A single unrecognised word (`LMA translate Klingon`) is answered with the supported list; a longer message that merely starts with "translate" is ordinary chat and is left alone.
- **It lasts for this meeting only.** The reply says so: `switched to translator mode (English and French), now always listening. This applies to this meeting only; the default language pair is set on the Nova Sonic Configuration page.` The choice is **not** saved as the deployment default for anyone, including the person who launched the Virtual Participant. A chat sender is known to the Virtual Participant only by the display name the meeting platform shows, which participants choose for themselves, and the Virtual Participant knows its owner only as an LMA sign-in (an email address), so no chat sender can be matched to the owner with confidence. Rather than guess, the chat never writes configuration; the default pair stays an Admin setting on the Nova Sonic Configuration page.
- The `[assistant-toggle]` line carries a `translator="English<->French"` field when a pair was asked for, marked `(unchanged)` when it was already in use.

### Asking the Assistant

Outside translator mode, the same changes can be asked for out loud. Address the assistant by its wake phrase and say what you want:

| Say | Effect |
|---|---|
| "Hey Alex, switch to translator mode" | `LMA translate` |
| "Hey Alex, go back to assistant mode" | `LMA assist`, including restoring the activation mode a translator or listen mode replaced |
| "Hey Alex, listen mode" | `LMA listen` |
| "Hey Alex, always listen" | `LMA always` |
| "Hey Alex, wake mode" | `LMA wake` |

The assistant has a `set_mode` tool for this, and a call to it is applied as the chat command in the table, so the rules above hold the same way and the `[assistant-toggle]` log line reads `route=voice`. The assistant confirms in one sentence — "Switching to translator mode.", "I'm already in assistant mode.", or why it can't — and then switches; it says the sentence first because the switch replaces the session that would say it. A call is only acted on when the participant's turn just before it contained the wake phrase, or the assistant was addressed by wake phrase within the last 15 seconds: the model does call the tool when participants merely talk about a mode, and what it says in reply to such a call is not played. Each call writes a `[voice-mode]` line to the Virtual Participant log. A spoken change is confirmed out loud only; unlike a typed command, it is not answered in the meeting chat. Whether something addressed to the assistant is a request to switch — rather than, say, a question about what translator mode does — is the model's judgement, so a mistaken switch is possible; "go back to assistant mode" or `LMA assist` undoes it.

Translator mode keeps only its pause tool, so it is left with a phrase instead: "Stop translating". The assistant's name is optional — "Alex, stop translating" and "Hey Alex, stop translating" work too, with the name being a wake phrase or the name in one of the translator's pause phrases — and so are these wordings: "stop translation", "stop the translation", "stop the translator", "stop interpreting", "no more translating", "no more translation", "end translation", "end the translation", "end translator mode", "exit translator", "exit translator mode", "exit translation", "exit translation mode", "leave translator mode", "translator off", "translation off", "turn off translation", "turn off the translator", "turn off translator mode", "assistant mode", "switch to assistant mode", "go back to assistant mode" and "back to assistant mode". Filler such as "please", "okay" or "now" is ignored. It works whether the translator is translating or paused. The translation of the request itself is not played; the assistant switches to assistant mode — restoring the activation mode translator mode replaced — and says "Okay, I've stopped translating." in its own voice.

The phrase has to be the whole utterance, apart from the name and filler. That is what keeps ordinary speech out now that the name is not needed: "we should stop translating the slides after lunch", "Alex, stop translating this part later" and "did you stop translating?" are translated as usual. The phrases are English only, so a participant speaking the other language of the pair leaves translator mode with `LMA assist` in the chat, or by saying the English phrase.

The phrase is read from two transcripts, and whichever recognises it first acts: Nova Sonic's own transcript of the meeting, and the participants' transcript (Amazon Transcribe's participant channel, or the MicroVM ASR engine). Nova Sonic's transcript has been seen to be wrong in translator mode, so the participants' transcript is the more reliable of the two. Nova Sonic's translator also tends to call its pause tool when it hears "stop translating"; a pause tool call that comes from the same utterance, or within five seconds of either transcript recognising the phrase, is taken as the request to leave translator mode rather than as a pause.

**While the translator is paused**, it answers to three things, read from the participants' transcript:

- The resume phrase (`translator unmute` or `alex unmute` by default) resumes translating, with the same rising tone as before.
- "Stop translating" (or any of the wordings above) leaves translator mode, as it does while translating.
- The wake phrase ("Hey Alex, …") leaves translator mode as well, and what was said is passed to the assistant as if it had been said in assistant mode: "Hey Alex, are you there?" is answered. A wake phrase addresses the assistant rather than the translator, so resuming translation would translate the question instead of answering it. "Hey Alex, stop" on its own is not taken this way, and the translator stays paused.

A paused translator is not sent the meeting's audio once the participants' transcript has reached it in that meeting. Sent the audio, the paused model kept replying ("I'm paused; say translator unmute") with speech the pause then dropped, and it resumed itself on speech that only mentioned the translator. Its session stays open, so resuming takes about as long as it did before. If a meeting has no participants' transcript, the paused model is still sent the audio and resumes with its own tool, as before. If the participants' transcript stops partway through a meeting while the translator is paused, the translator cannot be resumed by voice, and `LMA assist` in the chat is the way out. The Virtual Participant log records the pause as `[translator] action=paused audio=withheld resume=transcript`, a resume from the transcript as `[translator] action=resume route=transcript`, and leaving as `[voice-mode] mode=assistant action=stop-translating route=<nova|transcript|tool>`, with `why=wake-phrase-while-paused` when a wake phrase caused it.

### What Happens When the Mode Changes

The meeting mode decides the assistant's system prompt and the set of tools it is given, and both are sent once when its Nova session opens — translator mode most visibly, because it *replaces* the base prompt rather than adding to it. So a mode change is not a flag flip: LMA rebuilds the session around the new configuration, using the same mechanism as the periodic session refresh.

What that means in a live meeting:

- **The assistant pauses while it switches.** It cannot hear or answer for the length of a rebuild — reckon on about a second, and on several if it has to wait behind a session refresh already running — the same gap the periodic refresh already produces. Unlike that refresh, which waits for a quiet moment, a command is acted on when it arrives — so an utterance the assistant was in the middle of hearing is lost, and whoever was speaking may need to repeat their question. The transcript still has it. A meeting-assistant search already running is dropped for the same reason, and that question needs asking again too; the assistant goes quiet on it rather than answering into the new mode.
- **The transcript is not affected.** Transcription reads the meeting audio and the assistant's output sink through capture processes the Virtual Participant owns, and the assistant's output sink exists whether or not a Nova session is open. Nothing on the mode-change path starts or stops an audio process.
- **The conversation so far is not carried into a new mode.** The next session is built for the mode it is now in, so replaying earlier turns would prime it with a conversation that mode never had. An activation-mode change on its own does not discard the conversation, since the mode and the prompt are unchanged — the assistant keeps it, and replays it into the next session it *refreshes*. A session opened from nothing, which is what `LMA always` does when none was held and what the next wake phrase does after `LMA wake`, starts without the replay.
- **Listen mode starts quiet.** Switching to listen mode (`group`) leaves the assistant muted until a wake phrase, exactly as deploying in group mode would.
- **A "stop talking" still being resolved is called off by a meeting-mode change.** In the conversational modes the assistant holds a [stop request](#asking-the-assistant-to-stop-talking) for a moment so it can say "Okay" first. A meeting-mode change inside that moment decides for itself whether the assistant is audible, so the request is superseded rather than carried into the new mode — most importantly, it never pauses a translator, which is only paused by its own trigger phrase. A change to the activation mode alone leaves the request standing. Either way, the next stop request is taken the way the new mode takes it. The stand-down is recorded as `[stop-request] ... action=cancelled why=mode-changed`, or `action=superseded-by-mode-change` if the assistant's own mute was already under way.
- **On a wake-phrase deployment, `LMA assist` usually rebuilds nothing immediately.** No session is held open between activations, so the new mode is picked up by the session the next wake phrase creates. The exception is a change that arrives while somebody has the assistant activated: the session is replaced rather than closed, so the window they opened keeps working. `LMA translate` and `LMA listen` are different — they switch the assistant to always-listening, which opens a session straight away.

One known limitation on the optional [MicroVM ASR engine](microvm-asr.md): whether the assistant's own speech is transcribed on its own channel is decided once, when the engine run starts. A meeting switched into translator mode partway through keeps transcribing the assistant for the rest of that run, so translations appear in the transcript alongside the speech they translate. The meeting's own transcript is unaffected either way.

### Reading the Log

Every attempt leaves one line in the Virtual Participant's CloudWatch log stream, tagged `[assistant-toggle]`, whether or not it changed anything:

```
[assistant-toggle] vp=<vpId> call=<callId> route=chat by="Alice" result=applied meetingMode=normal->translator activationMode=wake_phrase->always_active session=rebuilt
```

- `route` — which surface asked (`chat` or `voice`)
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
| **VoiceAssistantActivationDuration** | `5`-`300` seconds | How long the assistant stays active once it is idle after a wake phrase (see [wake_phrase](#wake_phrase)) |

## See Also

- [Nova Sonic 2 Setup](nova-sonic-setup.md) -- Configure the AWS Nova Sonic voice provider
- [ElevenLabs Setup](elevenlabs-setup.md) -- Configure the ElevenLabs voice provider
- [Simli Avatar Setup](simli-avatar-setup.md) -- Add a visual avatar to the voice assistant
- [Virtual Participant](virtual-participant.md) -- The VP that hosts the voice assistant
