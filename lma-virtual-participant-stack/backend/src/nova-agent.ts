/**
 * AWS Nova Sonic 2 Voice Assistant Provider
 * 
 * Implements AWS Bedrock Bidirectional Stream integration for Nova Sonic 2
 * Supports real-time voice conversation with audio streaming
 */

import { spawn, ChildProcess } from 'child_process';
import { VoiceAssistantProvider } from './voice-assistant-interface.js';
import { simliAvatar } from './simli-avatar.js';
import {
  BedrockRuntimeClient,
  InvokeModelWithBidirectionalStreamCommand,
  InvokeModelWithBidirectionalStreamInput,
} from '@aws-sdk/client-bedrock-runtime';
import { LambdaClient, InvokeCommand } from '@aws-sdk/client-lambda';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { NodeHttp2Handler } from '@smithy/node-http-handler';
import { defaultProvider } from '@aws-sdk/credential-provider-node';
import { randomUUID } from 'crypto';
import {
  hasNovaSonicConfigTable,
  loadNovaSonicConfig,
  MeetingMode,
  novaSonicConfigTablesFromEnv,
} from './nova-sonic-config-loader.js';
import { configuredWakePhrases, isStopTalkingRequest } from './stop-request.js';
import { publishAssistantTurn } from './translator-transcript.js';
import { toSpokenText } from './spoken-text.js';
import { pcmMs, RecentAudio, TRANSLATOR_DRAIN_MAX_MS } from './translator-turns.js';
import { isNovaThrottle, noteNovaThrottle, throttleBackoffMs } from './nova-throttle.js';
import {
  containsTriggerPhrase,
  isStopTranslatingRequest,
  isVoiceMode,
  predictVoiceModeChange,
  SET_MODE_NOT_ADDRESSED_RESULT,
  SET_MODE_PROMPT,
  SET_MODE_TOOL_SPEC,
  STOPPED_TRANSLATING_PHRASE,
  translatorAddressees,
  voiceModeCommand,
  voiceModeSentence,
} from './voice-mode-switch.js';
import { agentSpeakingDetector } from './agent-speaking-detector.js';
import { pcmDurationMs, renderToolAckClip } from './tool-ack-clip.js';
import { NovaSonicUsage } from './metering.js';
import {
  addressesAssistant,
  LookupSkipReason,
  mutedLookupSkipReason,
  SKIPPED_LOOKUP_RESULT,
  skippedLookupLogLine,
} from './muted-lookup.js';
import { ActivationMode } from './voice-assistant-interface.js';
import {
  applyAssistantChatCommand,
  AssistantReconfigureOutcome,
  AssistantReconfigureRequest,
  describeMeetingMode,
  formatAssistantToggleLog,
  isActivationMode,
  isMeetingMode,
  publishMeetingMode,
  requiresAlwaysListening,
} from './assistant-toggles.js';

/**
 * Whether the assistant is given a `mute` tool it can call when a participant
 * asks it to stop talking.
 *
 * Registered in both conversational meeting modes. `group` has had it since the
 * mode was introduced; `normal` — which is what a `wake_phrase` deployment runs
 * — needs it too, because that is where people are in a meeting with the
 * assistant and want to be able to tell it to be quiet.
 *
 * Translator mode is excluded: its `mute` is a different tool with different
 * semantics (pause/resume on an exact trigger phrase), registered one state at
 * a time by `setupInitialEvents`.
 */
export function hasStopTalkingTool(meetingMode: MeetingMode): boolean {
    return meetingMode === 'normal' || meetingMode === 'group';
}

/**
 * How long the transcript route waits for Nova to call the `mute` tool itself
 * before silencing the assistant on its own.
 *
 * Two routes recognise the same request and only one of them can speak. The
 * tool route is the model noticing, so it is the only route that can produce
 * the spoken "Okay" the prompt asks for — and it is expected to be the slower
 * of the two, because Nova has to hear the audio, decide, and emit a tool-use
 * event where Amazon Transcribe only has to finalise a segment. Left to race,
 * the transcript route is then the one that wins: it shuts the gate before the
 * word exists, and the meeting is left with silence and no acknowledgement at
 * all. Which route actually arrives first is what `tool_latency_ms` records.
 *
 * 500 ms is a starting value and not a measured one: long enough for a tool-use
 * event to arrive on an established bidirectional stream, short enough that
 * someone asking for quiet does not hear the assistant hesitate. Every request
 * logs the tool's actual latency (`[stop-request] ... tool_latency_ms=`), so the
 * next live meeting replaces this estimate with a measurement.
 */
export const STOP_REQUEST_TOOL_GRACE_MS = 500;

/**
 * How long the transcript route keeps its fallback armed after Nova's own
 * `mute` tool call has been seen.
 *
 * The tool route owns the request from that point — it holds the gate open long
 * enough for the acknowledgement to reach the meeting — but the tool-use event
 * and the handler that acts on it are two steps, and the bidirectional stream
 * can close in between. A stop request that silences nothing is the one outcome
 * this feature may not produce, so the fallback is extended rather than
 * cancelled: long enough to sit behind the tool route's own flush, short enough
 * that nobody is left waiting on it.
 *
 * Read in two places: as this window, and as twice this window in
 * noteMuteToolRequested, where it is the age beyond which a tool call is too
 * old to be timed against a request at all. Both are answering "how long could
 * a tool call still be about this request", so they are meant to move together.
 */
export const STOP_REQUEST_TOOL_BACKSTOP_MS = 1500;

/**
 * How long after Nova calls its `mute` tool the acknowledgement may begin.
 *
 * Nova does not reliably say "Okay" before the tool call, as the prompt asks:
 * measured live, it calls `mute` first and speaks the word in the response to
 * the tool result, which arrives after the tool route has already shut the
 * audio gate (MUTE_TOOL_AUDIO_FLUSH_MS), so the word was dropped. The first
 * audio block that starts inside this window is therefore treated as the
 * acknowledgement and played through the shut gate, up to
 * STOP_ACK_MAX_AUDIO_MS of it. A block that starts later is not, and is
 * dropped like any other audio from a muted assistant.
 */
export const STOP_ACK_START_WINDOW_MS = 1500;

/**
 * The most acknowledgement audio played through the shut gate.
 *
 * "Okay" is well under half a second. The cap is what keeps a model that
 * ignored the prompt — and went on with the answer it was asked to stop — from
 * being heard for longer than this: past it the rest of the block is dropped,
 * and the audio already let through drains out as it does for an
 * acknowledgement spoken before the tool call.
 */
export const STOP_ACK_MAX_AUDIO_MS = 1500;

/**
 * The rules of the translator prompt that say which language to speak.
 *
 * Both directions are spelled out, because a speech-to-speech model told only
 * to use "the OTHER language" will sometimes say the utterance back in the
 * language it heard. The last rule asks for speech and text to agree, because
 * the transcript is written from Nova's text: a translation spoken in the wrong
 * language looks correct everywhere except in the meeting itself.
 */
export function translatorDirectionRules(a: string, b: string): string[] {
  return [
    `1. When you hear ${a}, say it in ${b}. When you hear ${b}, say it in ${a}.`,
    '2. Your spoken words must always be in the OTHER language from the one you just heard. '
      + 'Never repeat or paraphrase an utterance in the language it was spoken in: replying in '
      + `${a} to ${a}, or in ${b} to ${b}, is always wrong.`,
    '3. Speak only the translation, naturally, matching the speaker\'s tone. '
      + 'Your speech and your text must be the same words in the same language.',
  ];
}

/**
 * How often a held-open activation window looks again at whether the assistant
 * has gone idle (see checkActivationWindow).
 */
export const ACTIVATION_RECHECK_MS = 1000;

/**
 * How far past the estimated end of playback the output detector may still
 * hold an activation window open (see activationHoldFor).
 */
export const ACTIVATION_AUDIBLE_HOLD_MAX_MS = 10000;

/**
 * How long a participant's turn holds a lapsing activation window open, so that
 * a question asked just before it lapses is not cut off before the model has
 * had the moment it needs to start answering or call a tool.
 *
 * Deliberately short and deliberately not a re-arm: the session hears the
 * whole meeting, so treating every turn as engagement would keep a wake-phrase
 * session open for as long as anyone in the meeting is talking. Being addressed
 * by wake phrase is what re-arms the full window.
 */
export const ACTIVATION_USER_TURN_GRACE_MS = 3000;

/**
 * How long after a `strands_agent` tool call the agent waits for the model's own
 * spoken acknowledgement before playing its pre-rendered one.
 *
 * Measured on a live meeting: when Nova does speak a preamble, its audio block
 * starts within about 150 ms of the tool-use event, so a second is ample for a
 * model that is going to speak and short enough that the room is not left in
 * silence wondering whether it was heard.
 */
export const TOOL_ACK_FALLBACK_MS = 1000;

/**
 * How long before the tool-use event an assistant audio block may have started
 * and still count as the acknowledgement for it — the prompt asks for the
 * sentence before the call, and the model sometimes emits it in that order.
 */
export const TOOL_ACK_LOOKBACK_MS = 2000;

/**
 * After the fallback acknowledgement has played, how long a model preamble that
 * arrives late is dropped rather than played, so the room does not hear two
 * acknowledgements back to back. Audio after this, or after the tool result has
 * gone back to the model, is the answer and is always played.
 */
export const TOOL_ACK_LATE_PREAMBLE_MS = 4000;

/**
 * Which region to reach Amazon Nova Sonic in.
 *
 * Nova Sonic is available in fewer regions than LMA itself, so a deployment
 * pinned to a particular region for compliance can keep everything else local and
 * reach the voice assistant elsewhere (GitHub #508). `AMAZON_NOVA_SONIC_REGION`
 * carries the `AmazonNovaSonicRegion` stack parameter; empty or unset means the
 * stack's own region, which is what every existing deployment gets.
 *
 * An explicitly passed region still wins, so a caller that already knows where to
 * go is not overridden by the environment.
 */
export function resolveNovaSonicRegion(
    configured: string | undefined,
    env: Record<string, string | undefined> = process.env,
): string {
    const fromEnv = (env.AMAZON_NOVA_SONIC_REGION || '').trim();
    return configured || fromEnv || stackRegion(env);
}

/**
 * The region this stack is deployed to — where everything except the Nova Sonic
 * model itself lives.
 *
 * `VP_AWS_REGION` is the fallback because `AWS_REGION` is reserved and rejected at
 * MicroVM image-build time, so the MicroVM path stages it under the prefixed name
 * (the same order is used in status-manager.ts and microvm-supervisor.ts).
 */
export function stackRegion(env: Record<string, string | undefined> = process.env): string {
    return env.AWS_REGION || env.VP_AWS_REGION || 'us-east-1';
}

export interface NovaAgentConfig {
  modelId: string;
  systemPrompt: string;
  knowledgeBaseId?: string;
  activationMode?: string;
  activationDuration?: number;
  region?: string;
  strandsLambdaArn?: string;
  voiceId?: string;
  endpointingSensitivity?: 'HIGH' | 'MEDIUM' | 'LOW';
  /** @deprecated Use meetingMode instead. Kept for back-compat. */
  groupMeetingMode?: boolean;
  meetingMode?: MeetingMode;
  translatorLanguageA?: string;
  translatorLanguageB?: string;
  translatorMutePhrases?: string[];
  translatorUnmutePhrases?: string[];
}

/** How often accumulated Nova Sonic token usage is written to the meeting's metering records. */
const USAGE_FLUSH_INTERVAL_MS = 5 * 60 * 1000;

const DEFAULT_TRANSLATOR_MUTE_PHRASES = ['translator mute', 'alex mute'];
const DEFAULT_TRANSLATOR_UNMUTE_PHRASES = ['translator unmute', 'alex unmute'];

/** How long after "stop translating" a translator's `mute` call is taken as
 *  that request rather than as the pause phrase. In a live meeting Nova's tool
 *  call followed its transcript of the utterance by about 350 ms. */
const LEAVE_TRANSLATOR_MUTE_WINDOW_MS = 5000;

/** Lowercases, collapses whitespace, and strips trailing punctuation so that
 *  speech-recognized text and configured trigger phrases can be compared. */
function normalizeForTrigger(text: string): string {
  return text.toLowerCase().replace(/\s+/g, ' ').replace(/[.,!?;:]+$/g, '').trim();
}

interface SessionData {
  queue: Array<any>;
  isActive: boolean;
  promptName: string;
  audioContentId: string;
  isPromptStartSent: boolean;
  isAudioContentStartSent: boolean;
  sessionStartTime: number;
  lastRefreshTime: number;
  /** For the log lines. */
  id?: string;
  /** Wakes this session's event stream when an event is queued or it closes.
   *  Per session, because in translator mode an outgoing session is still
   *  streaming while the next one starts (see translator-turns.ts). */
  signal?: (() => void) | null;
  /** When the most recent participant turn in this session's transcript ended. */
  lastUserTurnEndAt?: number;
  /** Set on an outgoing translator session that is finishing its answer. */
  draining?: boolean;
  drainStartedAt?: number;
  drainTimer?: NodeJS.Timeout | null;
  /** Whether this session's current answer is still producing audio. */
  audioTurnOpen?: boolean;
  /** Order of creation, so audio from a later session can wait for an earlier one. */
  seq?: number;
  /** Set when a participant has asked a translator to stop: whatever this
   *  session says from then on is a translation of that request, and is dropped. */
  silenced?: boolean;
  /** Drop this session's audio until its current turn ends (an unaddressed
   *  `set_mode` call; see handleSetModeTool). */
  dropTurn?: boolean;
  /** Keep the text of that same reply out of the history and the transcript.
   *  Cleared at the next participant turn rather than with the audio, because
   *  the FINAL text of a turn arrives after its audio has ended. */
  dropReplyText?: boolean;
  /** Whether this session has moved the meeting on to the next translator
   *  session for its current answer. Per session rather than per stream so
   *  that a hand-over refused for the concurrency quota can be undone. */
  handedOver?: boolean;
  /** The translator session this one took the meeting over from, while its
   *  stream is still opening (see keepOutgoingTranslatorSession). */
  handoverFrom?: SessionData | null;
  /** Settles once Amazon Bedrock has accepted or refused this session's stream. */
  opened?: Promise<SessionOpenOutcome>;
  resolveOpened?: (outcome: SessionOpenOutcome) => void;
}

/** How opening a session's stream ended. */
type SessionOpenOutcome = 'open' | 'throttled' | 'failed';

/** Tries in all, on a wake phrase, to open the assistant's session while
 *  Nova Sonic's concurrency quota refuses it, and the backoff between them.
 *  Four tries take at most about 7.5 seconds before the assistant gives up and
 *  stays silent. */
export const ASSISTANT_OPEN_ATTEMPTS = 4;
export const ASSISTANT_OPEN_BACKOFF = { baseMs: 1000, capMs: 4000 };
/** How long a wake-phrase activation waits to hear whether its stream opened
 *  before carrying on as it always has. Measured refusals arrive in about
 *  300 ms. */
export const ASSISTANT_OPEN_WAIT_MS = 5000;
/** Backoff before a translator tries handing over again after a refused
 *  hand-over, and before an always-listening session is rebuilt after a
 *  refused open. */
export const TRANSLATOR_HANDOVER_BACKOFF = { baseMs: 2000, capMs: 60000 };
export const SESSION_RECOVERY_BACKOFF = { baseMs: 2000, capMs: 30000 };

interface ConversationTurn {
  role: 'USER' | 'ASSISTANT';
  content: string;
  timestamp: number;
}

export class NovaAgent implements VoiceAssistantProvider {
  private modelId: string;
  private systemPrompt: string;
  private voiceId: string;
  private endpointingSensitivity: 'HIGH' | 'MEDIUM' | 'LOW';
  private knowledgeBaseId?: string;
  private activationMode: string;
  private groupMeetingMode: boolean;
  private meetingMode: MeetingMode;
  private translatorLanguageA: string;
  private translatorLanguageB: string;
  private translatorMutePhrases: string[];
  private translatorUnmutePhrases: string[];
  private _isActivated: boolean = false;
  private _isActive: boolean = false;
  private _isSpeaking: boolean = false;
  private _isProcessingTool: boolean = false;
  /** Lookups still running. Nova calls tools asynchronously, so a follow-up
   *  can start before the previous one has finished, and the first to finish
   *  must not clear `_isProcessingTool` for the other. */
  private toolCallsInFlight: number = 0;
  private _isMuted: boolean = false;
  // Translator-mode state. Drives the prompt and tool set used on the
  // Nova session; transitions trigger a session refresh.
  private _translatorState: 'UNMUTED' | 'MUTED' = 'UNMUTED';
  private _isInterrupted: boolean = false; // Gate: drops all audio (voice + avatar) after barge-in until next turn
  private activationTimeout: NodeJS.Timeout | null = null;
  private defaultActivationDuration: number;
  /** Where the Nova Sonic model is invoked. NOT the stack's region — see stackRegion(). */
  private novaSonicRegion: string;
  // Nova Sonic token usage for the meeting cost estimate; see meteredUsage().
  private usage: NovaSonicUsage | null = null;
  private usageModelId = '';
  private usageFlushTimer: ReturnType<typeof setInterval> | null = null;
  private audioChunkCount: number = 0;
  
  // Persistent audio playback stream (replaces per-batch paplay spawning)
  private pacatProcess: ChildProcess | null = null;
  /** When the current pacat process was spawned. Reported with the mute-tool
   *  acknowledgement, because a stream spawned milliseconds earlier may not have
   *  finished connecting to PulseAudio and so has played nothing yet. */
  private pacatSpawnedAt: number = 0;
  /** A pacat process that has been handed off to finish playing (see
   *  stopPacatStream's `drain` mode). Held so that a later immediate teardown
   *  can still reach it: two streams writing to `agent_output` at once would be
   *  audible as garble, and barge-in has to be able to cut off everything. */
  private drainingPacatProcess: ChildProcess | null = null;
  /** Upper bound on letting the playback buffer empty instead of discarding it
   *  (see muteDrainingPlayback). Generous for the one word the prompt asks for,
   *  and the limit on how long a model that ignored the prompt goes on being
   *  heard after its own tool call — this much, plus whatever arrived during
   *  MUTE_TOOL_AUDIO_FLUSH_MS. */
  private readonly PLAYBACK_DRAIN_CAP_MS = 1500;
  /** How long the mute-tool route waits for trailing audio chunks of the
   *  acknowledgement before engaging the gate. It does not have to cover
   *  playback itself — muteDrainingPlayback lets the buffer empty — so it only
   *  has to outlast the stream's own event delivery. */
  private readonly MUTE_TOOL_AUDIO_FLUSH_MS = 280;
  private speakingTimeout: NodeJS.Timeout | null = null;
  private readonly SPEAKING_TIMEOUT_MS = 500; // Mark as not speaking after 500ms of silence

  /**
   * When the audio already written to the playback stream will have finished
   * playing, estimated from its length.
   *
   * `_isSpeaking` says whether audio is still *arriving*, which is not the same
   * thing: Nova delivers speech several times faster than real time, so a
   * 25-second answer can finish arriving 20 seconds before the meeting has
   * heard it (measured: audio for one answer arrived over 5 s and was audible
   * for 26 s). Anything that must not happen while the meeting is still
   * hearing the assistant has to ask this, or the output detector, instead.
   * Reset when the stream is cut off; bounded by the drain cap when it drains.
   */
  private playbackEndsAt: number = 0;

  /** Whether the agent's output sink is carrying speech right now — the RMS
   *  detector on `agent_output.monitor`, which is what the meeting hears. A
   *  field so that tests can stand in for PulseAudio. */
  private audibleOutput: () => boolean = () => agentSpeakingDetector.isSpeaking();

  /** Length of the current activation window, set by activate(). */
  private activationWindowMs: number = 0;
  /** When the activation window's idle countdown last restarted: activation,
   *  being addressed again, a barge-in, a tool finishing, or the end of a
   *  stretch in which the assistant was busy (see checkActivationWindow). */
  private lastEngagementAt: number = 0;
  /** What the window is being held open for, so it is logged once per change
   *  rather than once a second. */
  private activationHoldReason: string | null = null;

  /** The pre-rendered "looking that up" acknowledgement, in the configured
   *  voice (see tool-ack-clip.ts), or null until it has been rendered. */
  private toolAckClip: Buffer | null = null;
  /** A `strands_agent` call still waiting to find out whether the model will
   *  acknowledge it out loud. */
  private pendingToolAck: {
    toolUseId: string;
    toolAt: number;
    session: SessionData | null;
    timer: NodeJS.Timeout | null;
    /** When the fallback clip was played, or null if it has not been. */
    fallbackAt: number | null;
    /** Whether the tool result has gone back to the model. */
    resultSent: boolean;
  } | null = null;
  /** When the current fallback clip finishes playing (0 if none is). */
  private toolAckPlayingUntil: number = 0;
  /** Set while an assistant audio block is being discarded as a late,
   *  duplicate acknowledgement; cleared when that block ends. */
  private dropCurrentAudioBlock: boolean = false;
  /** When the most recent assistant audio block started. */
  private lastAssistantAudioStartAt: number = 0;
  /** When the most recent user text block (Nova's transcript of the meeting)
   *  started. */
  private lastUserTextAt: number = 0;
  /** When the most recent barge-in was seen. */
  private lastInterruptionAt: number = 0;
  /** The most recent completed participant turn in Nova's transcript, which is
   *  the turn a tool call that follows it is answering. */
  private lastUserTurnText: string = '';

  /**
   * The stop request currently being resolved, if any.
   *
   * One record for both routes, which is what makes them safe to fire in either
   * order: a second "stop talking" inside the window finds it already set and
   * stacks no timer, the tool route extends the same timer instead of adding
   * one, and whichever route reaches mute() first makes the other a no-op.
   */
  private stopRequest: {
    /** When this request was first recognised, by either route. */
    seenAt: number;
    /** Whether the transcript route is the one that recognised it. */
    fromTranscript: boolean;
    /** Whether Nova's own `mute` tool call has been seen for it. */
    toolSeen: boolean;
    /** Whether a dropped acknowledgement has already been reported, so a
     *  chunk-rate audio gate cannot flood the log. */
    ackDropLogged: boolean;
    /** Whether the tool route shut the gate behind a drained acknowledgement.
     *  Audio dropped after that is not a lost acknowledgement — the word was
     *  allowed out before the gate closed — so it is reported differently. */
    gateShutByDrain: boolean;
    timer: NodeJS.Timeout | null;
  } | null = null;

  /**
   * The acknowledgement Nova may still speak after its own `mute` tool call
   * (see STOP_ACK_START_WINDOW_MS).
   *
   * Opened when the tool-use event is seen, in the conversational modes only.
   * `blockOpenedAt` is set by the first AUDIO content block that starts inside
   * the window, and from then on that block's audio is played even once the
   * gate has shut, until the block ends, the budget runs out, or anything that
   * ends the stop request — being asked back, a barge-in, a mode change, an
   * absolute mute — clears it.
   */
  private stopAck: {
    /** When the `mute` tool-use event was seen. */
    toolAt: number;
    /** Where the stop request was recognised, for the log lines — fixed when
     *  the window opens, because the request record can resolve before the
     *  acknowledgement has finished. */
    route: 'transcript' | 'tool';
    /** When the acknowledgement block started, or null if none has yet. */
    blockOpenedAt: number | null;
    /** Acknowledgement audio counted so far, in bytes of playback PCM. */
    bytes: number;
    /** Ends the acknowledgement at its deadline if its block never reports
     *  an end — a stream that errors mid-block, say. */
    timer: NodeJS.Timeout | null;
  } | null = null;

  /**
   * When the transcript route last recognised a stop request, kept after the
   * request itself has resolved so that a tool call arriving too late can still
   * be timed against it — the measurement that says by how much the window was
   * too short.
   *
   * Scoped to the stretch of silence that request caused: set when the
   * transcript route accepts a request and cleared when the assistant is made
   * audible again, and additionally bounded where it is read, so a tool call is
   * never timed against a request the meeting has already moved on from.
   */
  private lastTranscriptStopAt: number | null = null;
  
  // Session refresh configuration. Nova closes the stream itself after 295s
  // without interactive content ("Timed out waiting for audio bytes or
  // interactive content"), and neither continuous meeting silence nor the 30s
  // keep-alive counts as interactive — so at the old 300s this timer lost the
  // race by 5 seconds in every quiet meeting. 240s pre-empts it with margin.
  private readonly SESSION_REFRESH_INTERVAL = 4 * 60 * 1000;
  private sessionRefreshTimer: NodeJS.Timeout | null = null;
  private isRefreshing: boolean = false;
  private refreshQueued: boolean = false;
  private lastUserActivityAt: number = 0;
  private readonly USER_QUIET_MS_BEFORE_REFRESH = 5000;
  private keepAliveTimer: NodeJS.Timeout | null = null;
  private readonly KEEP_ALIVE_INTERVAL = 30 * 1000; // Send keep-alive every 30 seconds
  
  // Conversation session tracking for group meeting mode
  private conversationActive: boolean = false;
  private conversationTimeout: NodeJS.Timeout | null = null;
  private autoMuteQueued: boolean = false;
  
  // Conversation history for session refresh
  private conversationHistory: ConversationTurn[] = [];
  private readonly MAX_HISTORY_TURNS = 10; // Keep last 10 turns for context
  
  // Bedrock client and session management
  private bedrockClient: BedrockRuntimeClient | null = null;
  private sessionId: string | null = null;
  private session: SessionData | null = null;
  private streamingActive: boolean = false;

  // Translator mode: a fresh session per translated turn (translator-turns.ts).
  /** Outgoing translator sessions still finishing their answer. */
  private drainingSessions: Set<SessionData> = new Set();
  /** Audio held back, per session, while an earlier session's answer is still
   *  producing its own, so two answers never interleave. */
  private heldPlayback: Map<SessionData, Buffer[]> = new Map();
  /** Numbers sessions in the order they are created (SessionData.seq). */
  private sessionSeq: number = 0;
  /** Meeting audio as sent, for replaying into the next translator session. */
  private recentMeetingAudio = new RecentAudio();
  /** Translated turns so far, for the per-turn log line. */
  private translatorTurn: number = 0;
  /** No translator hand-over is attempted before this time, after one was
   *  refused for Nova Sonic's concurrency quota. */
  private translatorHandoverBlockedUntil: number = 0;
  /** Consecutive hand-overs refused for the quota; reset by one that opens. */
  private translatorHandoverThrottles: number = 0;
  /** Consecutive session opens refused for the quota; reset by one that opens. */
  private sessionOpenThrottles: number = 0;
  /** A wake-phrase activation still opening its session, so a second wake
   *  phrase meanwhile does not open another. */
  private activationOpening: Promise<boolean> | null = null;
  /** The context of the latest wake phrase while an activation is opening. */
  private activationContext: string | undefined = undefined;
  /** A field so that tests can shorten it. */
  private assistantOpenBackoff: { baseMs: number; capMs: number } = ASSISTANT_OPEN_BACKOFF;

  // Changing mode by voice (voice-mode-switch.ts).
  /** When a participant's turn last addressed the assistant by wake phrase. */
  private lastAddressedAt: number = 0;
  /** A `set_mode` switch waiting for its confirmation to be spoken. */
  private pendingVoiceModeSwitch: {
    command: { meetingMode?: MeetingMode; activationMode?: ActivationMode };
    session: SessionData | null;
    sawAudio: boolean;
    timer: NodeJS.Timeout | null;
  } | null = null;
  /** "Okay, I've stopped translating." in the configured voice, rendered when
   *  translator mode is entered, or null. */
  private stoppedTranslatingClip: Buffer | null = null;
  private stoppedTranslatingClipRendering = false;
  /** Set while "stop translating" is being applied. */
  private leavingTranslator = false;
  /** When a participant last asked, in translator mode, to stop translating —
   *  on either transcript. A `mute` call that follows it is that request,
   *  read by the model as the pause phrase (see handleMuteTool). */
  private leaveTranslatorRequestedAt = 0;
  /** Whether the participants' transcript (Amazon Transcribe channel 0, or the
   *  ASR MicroVM) has reached this agent in this meeting, through
   *  noteParticipantUtterance. While it has, a paused translator is resumed or
   *  left from that transcript and the model is not sent the meeting's audio. */
  private participantTranscriptSeen = false;
  
  // Lambda client and Strands agent configuration
  private lambdaClient: LambdaClient | null = null;
  private strandsLambdaArn?: string;

  constructor(config: NovaAgentConfig) {
    this.modelId = config.modelId;
    this.systemPrompt = config.systemPrompt;
    this.voiceId = config.voiceId || 'tiffany'; // Default to tiffany (polyglot voice)
    this.endpointingSensitivity = config.endpointingSensitivity || 'MEDIUM'; // Default to MEDIUM
    this.knowledgeBaseId = config.knowledgeBaseId;
    this.activationMode = config.activationMode || 'wake_phrase';
    // meetingMode is the canonical selector; derive groupMeetingMode for back-compat
    this.meetingMode = config.meetingMode
      || (config.groupMeetingMode ? 'group' : 'normal');
    this.groupMeetingMode = (this.meetingMode === 'group');
    this.translatorLanguageA = (config.translatorLanguageA || 'English').trim() || 'English';
    this.translatorLanguageB = (config.translatorLanguageB || 'Spanish').trim() || 'Spanish';
    this.translatorMutePhrases = (config.translatorMutePhrases && config.translatorMutePhrases.length > 0)
      ? config.translatorMutePhrases.map(normalizeForTrigger).filter(p => p.length > 0)
      : [...DEFAULT_TRANSLATOR_MUTE_PHRASES];
    this.translatorUnmutePhrases = (config.translatorUnmutePhrases && config.translatorUnmutePhrases.length > 0)
      ? config.translatorUnmutePhrases.map(normalizeForTrigger).filter(p => p.length > 0)
      : [...DEFAULT_TRANSLATOR_UNMUTE_PHRASES];
    this.defaultActivationDuration = config.activationDuration || 30;
    this.novaSonicRegion = resolveNovaSonicRegion(config.region);
    this.strandsLambdaArn = config.strandsLambdaArn || process.env.STRANDS_LAMBDA_ARN;
    
    // Set initial activation state based on mode
    this._isActivated = (this.activationMode === 'always_active');
    
    // Set initial mute state based on meeting mode:
    // - 'group'      -> start muted (unmute on wake phrase)
    // - 'translator' -> always unmuted (speak every translation)
    // - 'normal'     -> unmuted
    this._isMuted = (this.meetingMode === 'group');

    console.log('✓ AWS Nova Sonic 2 agent initialized');
    console.log(`  Model: ${this.modelId}`);
    console.log(`  Voice: ${this.voiceId}`);
    console.log(`  Endpointing sensitivity: ${this.endpointingSensitivity}`);
    const local = stackRegion();
    console.log(
      `  Nova Sonic region: ${this.novaSonicRegion}` +
        (this.novaSonicRegion !== local ? ` (stack region is ${local})` : ''),
    );
    console.log(`  Activation mode: ${this.activationMode}`);
    console.log(`  Meeting mode: ${this.meetingMode}`);
    if (this.meetingMode === 'translator') {
      console.log(`  🌐 Translator mode: ${this.translatorLanguageA} ↔ ${this.translatorLanguageB}`);
      if (this.activationMode !== 'always_active') {
        console.warn(`  ⚠️  Translator mode requires activation mode 'always_active' to work end-to-end. Current: ${this.activationMode}`);
      }
    } else if (this.meetingMode === 'group') {
      console.log('  Group meeting mode: enabled (starts muted)');
    }
    if (this.strandsLambdaArn) {
      console.log(`  Strands agent tool: enabled`);
    }
  }

  /** Set by stop(), so a recovery timer that fires afterwards does nothing. */
  private stopped: boolean = false;
  /** The pending session-recovery attempt, if any (see scheduleSessionCreateRetry). */
  private sessionRecoveryTimer: NodeJS.Timeout | null = null;

  async start(): Promise<void> {
    console.log('Starting AWS Nova Sonic 2 agent...');
    this.stopped = false;

    try {
      // Load configuration from DynamoDB if a table is named
      const configTables = novaSonicConfigTablesFromEnv();
      if (hasNovaSonicConfigTable(configTables)) {
        try {
          const dynamoDbClient = new DynamoDBClient({
            // The stack's region, NOT the Nova Sonic one: both tables are created
            // in the stack's region and the task role's grants (and their
            // kms:ViaService condition) are pinned to ${AWS::Region}. Following the
            // Nova Sonic region here would hit tables that do not exist, and
            // loadNovaSonicConfig would quietly fall back to its defaults —
            // discarding the operator's configured prompt, voice and meeting mode.
            region: stackRegion(),
            credentials: defaultProvider(),
          });
          
          const config = await loadNovaSonicConfig(dynamoDbClient, configTables);
          console.log('✓ Loaded Nova Sonic config from DynamoDB');
          
          // Update system prompt, model ID, voice ID, endpointing sensitivity, and meeting mode from config
          this.systemPrompt = config.systemPrompt;
          this.modelId = config.modelId;
          if (config.voiceId) {
            this.voiceId = config.voiceId;
          }
          if (config.endpointingSensitivity) {
            this.endpointingSensitivity = config.endpointingSensitivity;
          }
          // Meeting mode is the canonical selector; fall back to legacy groupMeetingMode
          if (config.meetingMode) {
            this.meetingMode = config.meetingMode;
          } else if (typeof config.groupMeetingMode === 'boolean') {
            this.meetingMode = config.groupMeetingMode ? 'group' : 'normal';
          }
          this.groupMeetingMode = (this.meetingMode === 'group');
          // Mute state derives from meeting mode: group starts muted, translator/normal start unmuted
          this._isMuted = (this.meetingMode === 'group');

          if (config.translatorLanguageA && config.translatorLanguageA.trim() !== '') {
            this.translatorLanguageA = config.translatorLanguageA.trim();
          }
          if (config.translatorLanguageB && config.translatorLanguageB.trim() !== '') {
            this.translatorLanguageB = config.translatorLanguageB.trim();
          }
          if (config.translatorMutePhrases && config.translatorMutePhrases.length > 0) {
            this.translatorMutePhrases = config.translatorMutePhrases
              .map(normalizeForTrigger)
              .filter(p => p.length > 0);
          }
          if (config.translatorUnmutePhrases && config.translatorUnmutePhrases.length > 0) {
            this.translatorUnmutePhrases = config.translatorUnmutePhrases
              .map(normalizeForTrigger)
              .filter(p => p.length > 0);
          }

          console.log(`  Updated system prompt (${config.systemPrompt.length} chars)`);
          console.log(`  Updated model ID: ${config.modelId}`);
          console.log(`  Updated voice ID: ${this.voiceId}`);
          console.log(`  Updated endpointing sensitivity: ${this.endpointingSensitivity}`);
          console.log(`  Updated meeting mode: ${this.meetingMode}`);
          if (this.meetingMode === 'translator') {
            console.log(`  🌐 Translator mode: ${this.translatorLanguageA} ↔ ${this.translatorLanguageB}`);
            console.log(`  🔇 Mute trigger phrases: ${this.translatorMutePhrases.map(p => `"${p}"`).join(', ')}`);
            console.log(`  🔓 Unmute trigger phrases: ${this.translatorUnmutePhrases.map(p => `"${p}"`).join(', ')}`);
            if (this.activationMode !== 'always_active') {
              console.warn(`  ⚠️  Translator mode requires activation mode 'always_active' to work end-to-end. Current: ${this.activationMode}`);
            }
          }
        } catch (error) {
          console.error('Failed to load Nova Sonic config from DynamoDB:', error);
          console.log('Using environment variable configuration as fallback');
        }
      }
      
      // Initialize Bedrock client
      const nodeHttp2Handler = new NodeHttp2Handler({
        requestTimeout: 300000,
        sessionTimeout: 300000,
        disableConcurrentStreams: false,
        maxConcurrentStreams: 20,
      });

      this.bedrockClient = new BedrockRuntimeClient({
        // The only client that follows the Nova Sonic region.
        region: this.novaSonicRegion,
        credentials: defaultProvider(),
        requestHandler: nodeHttp2Handler,
      });

      // Initialize Lambda client if Strands Lambda ARN is configured
      if (this.strandsLambdaArn) {
        this.lambdaClient = new LambdaClient({
          // The stack's region, for the same reason as the config table: the Strands
          // function is deployed locally and granted by its exact ARN, so a client in
          // another region fails every tool call.
          region: stackRegion(),
          credentials: defaultProvider(),
        });
        console.log('✓ Lambda client initialized for Strands agent tool');

        // In the background: the clip is only needed once somebody asks the
        // assistant something, and a render that fails costs the fallback
        // acknowledgement and nothing else.
        const client = this.bedrockClient;
        renderToolAckClip(client, this.modelId, this.voiceId, undefined, {
          shouldContinue: () => !this.stopped,
        })
          .then((clip) => {
            this.toolAckClip = clip;
          })
          .catch((error) => {
            console.warn('[tool-ack] action=render-failed', error);
          });
      }
      if (this.meetingMode === 'translator') {
        this.renderStoppedTranslatingClip();
      }

      // In wake_phrase mode, defer session creation until activation
      if (this.activationMode === 'wake_phrase') {
        console.log('⏸️  Wake phrase mode - Session will be created on first activation');
        console.log('   This saves costs by not keeping connection open when not in use');
        return;
      }

      // For always_active mode, create session immediately
      await this.createSession();
      console.log('✓ AWS Nova Sonic 2 agent started successfully');
    } catch (error) {
      console.error('Failed to start Nova agent:', error);
      throw error;
    }
  }

  private async createSession(isRefresh: boolean = false): Promise<void> {
    if (this.streamingActive) {
      console.log('Session already active');
      return;
    }

    this.session = this.newSessionData();
    this.sessionId = this.session.id!;
    // A tool call answers a turn of the session that issued it, so a turn
    // heard by an earlier session must not let a lookup through on this one.
    this.lastUserTurnText = '';

    console.log(`Creating Nova session: ${this.sessionId}${isRefresh ? ' (refresh)' : ''}`);

    // Start the bidirectional stream
    this.streamingActive = true;
    this._isActive = true;
    
    // Initiate the streaming in the background
    this.initiateBidirectionalStreaming().catch(error => {
      console.error('Error in bidirectional streaming:', error);
      this._isActive = false;
      this.streamingActive = false;
    });

    // Wait a moment for the stream to initialize
    await new Promise(resolve => setTimeout(resolve, 100));

    // Send initial events
    await this.setupInitialEvents(isRefresh);

    // Start session refresh timer for always_active mode
    if (this.activationMode === 'always_active') {
      this.startSessionRefreshTimer();
      // Delay keep-alive start to ensure session is fully initialized
      setTimeout(() => {
        this.startKeepAliveTimer();
      }, 2000);
    }
  }

  private newSessionData(): SessionData {
    const now = Date.now();
    let resolveOpened: (outcome: SessionOpenOutcome) => void = () => {};
    const opened = new Promise<SessionOpenOutcome>((resolve) => {
      resolveOpened = resolve;
    });
    return {
      opened,
      resolveOpened,
      id: randomUUID(),
      seq: ++this.sessionSeq,
      queue: [],
      isActive: true,
      promptName: randomUUID(),
      audioContentId: randomUUID(),
      isPromptStartSent: false,
      isAudioContentStartSent: false,
      sessionStartTime: now,
      lastRefreshTime: now,
      signal: null,
    };
  }

  /** Whether a translator session that has started answering should hand the
   *  meeting over to a new one (see translator-turns.ts). Only the current
   *  session, only after it has heard a participant's turn, and not while the
   *  translator is paused or a refresh is already replacing it. */
  private shouldStartNextTranslatorSession(session: SessionData | null): boolean {
    return !!session
      && session === this.session
      && session.isActive
      && !session.draining
      && !!session.lastUserTurnEndAt
      && this.meetingMode === 'translator'
      && this._translatorState === 'UNMUTED'
      && !this.isRefreshing
      && !this.stopped
      && !!this.bedrockClient
      && Date.now() >= this.translatorHandoverBlockedUntil;
  }

  /**
   * Move the meeting's audio on to a new translator session, leaving `outgoing`
   * to finish the answer it has started.
   *
   * The new session's setup is queued before anything else, then the audio
   * heard since the participant's turn ended, and from then on sendAudioChunk
   * feeds it. All of that waits in its queue until its stream is up, so the
   * time it takes to connect costs latency, not speech.
   */
  private startNextTranslatorSession(outgoing: SessionData): void {
    const backlog = this.recentMeetingAudio.since(outgoing.lastUserTurnEndAt ?? Date.now());
    this.translatorTurn += 1;

    outgoing.draining = true;
    outgoing.drainStartedAt = Date.now();
    outgoing.drainTimer = setTimeout(
      () => this.closeDrainingSession(outgoing, 'timed-out'),
      TRANSLATOR_DRAIN_MAX_MS,
    );
    this.drainingSessions.add(outgoing);

    const next = this.newSessionData();
    next.handoverFrom = outgoing;
    this.session = next;
    this.sessionId = next.id!;
    this.lastUserTurnText = '';
    console.log(`Creating Nova session: ${next.id} (translator turn ${this.translatorTurn + 1})`);
    // Queues every setup event before returning: with no history to replay
    // there is nothing in it to wait for, so no audio can be queued ahead of it.
    this.setupInitialEvents(false).catch(error => {
      console.error('Error setting up the next translator session:', error);
    });
    for (const chunk of backlog) {
      this.queueAudio(next, chunk);
    }
    this.initiateBidirectionalStreaming(next).catch(error => {
      console.error('Error in bidirectional streaming:', error);
    });
    if (this.activationMode === 'always_active') {
      this.startSessionRefreshTimer();
    }

    console.log(
      `[translator-turn] n=${this.translatorTurn} from=${outgoing.id} to=${next.id}`
      + ` backlog_ms=${pcmMs(backlog)} draining=${this.drainingSessions.size}`,
    );
  }

  private queueAudio(session: SessionData, chunk: Buffer): void {
    this.queueEvent(session, {
      event: {
        audioInput: {
          promptName: session.promptName,
          contentName: session.audioContentId,
          content: chunk.toString('base64'),
        },
      },
    });
  }

  /**
   * Whether audio from `session` has to wait: an earlier outgoing session's
   * answer is still producing audio, or audio of its own is already waiting.
   *
   * Decided by creation order, not by whether `session` is outgoing: a session
   * hands over at the start of its answer, before its first audio arrives, so
   * every answering session is outgoing by the time its audio does.
   */
  private holdsPlaybackFor(session: SessionData | null): boolean {
    if (!session) return false;
    if (this.heldPlayback.has(session)) return true;
    return this.earlierAnswerOpen(session);
  }

  private earlierAnswerOpen(session: SessionData): boolean {
    for (const outgoing of this.drainingSessions) {
      if (outgoing !== session && (outgoing.seq ?? 0) < (session.seq ?? 0) && outgoing.audioTurnOpen) {
        return true;
      }
    }
    return false;
  }

  private holdPlayback(session: SessionData, chunk: Buffer): void {
    const held = this.heldPlayback.get(session);
    if (held) {
      held.push(chunk);
    } else {
      this.heldPlayback.set(session, [chunk]);
    }
  }

  /** Play what was held back, oldest session first, for every session that no
   *  longer has an earlier answer to wait for. */
  private releaseHeldPlayback(): void {
    const waiting = [...this.heldPlayback.keys()].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
    for (const session of waiting) {
      if (this.earlierAnswerOpen(session)) return;
      const held = this.heldPlayback.get(session) ?? [];
      this.heldPlayback.delete(session);
      for (const chunk of held) {
        this.playAudio(chunk).catch(err => {
          console.error('Error playing audio:', err);
        });
      }
      // This session's own answer may still be producing audio, and anything
      // later waits for it in turn.
      if (session.audioTurnOpen && session.draining) return;
    }
  }

  /** End an outgoing translator session. `why` is logged. */
  private closeDrainingSession(session: SessionData, why: string): void {
    if (!this.drainingSessions.delete(session)) return;
    if (session.drainTimer) {
      clearTimeout(session.drainTimer);
      session.drainTimer = null;
    }
    session.audioTurnOpen = false;
    this.queueEvent(session, { event: { contentEnd: { promptName: session.promptName, contentName: session.audioContentId } } });
    this.queueEvent(session, { event: { promptEnd: { promptName: session.promptName } } });
    this.queueEvent(session, { event: { sessionEnd: {} } });
    // Long enough for the stream to send them, as refreshSession allows.
    setTimeout(() => {
      session.isActive = false;
      session.signal?.();
    }, 300);
    console.log(
      `[translator-turn] closed ${session.id}: ${why}`
      + ` after ${Date.now() - (session.drainStartedAt ?? Date.now())} ms`,
    );
    if (why === 'closed') {
      // The agent is closing its session: nothing is left to play into.
      if (this.drainingSessions.size === 0) this.heldPlayback.clear();
    } else {
      this.releaseHeldPlayback();
    }
  }

  /** End every outgoing translator session. */
  private closeDrainingSessions(why: string): void {
    for (const session of [...this.drainingSessions]) {
      this.closeDrainingSession(session, why);
    }
  }

  /** An outgoing session whose stream has already ended. */
  private forgetDrainingSession(session: SessionData): void {
    if (!this.drainingSessions.delete(session)) return;
    if (session.drainTimer) {
      clearTimeout(session.drainTimer);
      session.drainTimer = null;
    }
    session.isActive = false;
    session.audioTurnOpen = false;
    console.log(`[translator-turn] ${session.id} ended before it was closed`);
    this.releaseHeldPlayback();
  }

  private startKeepAliveTimer(): void {
    // Clear any existing timer
    if (this.keepAliveTimer) {
      clearInterval(this.keepAliveTimer);
    }

    console.log(`💓 Keep-alive timer started - sending silence every ${this.KEEP_ALIVE_INTERVAL / 1000} seconds`);
    
    // Send silence chunks periodically to prevent 55-second timeout
    this.keepAliveTimer = setInterval(() => {
      // Only send keep-alive if session is active and not refreshing
      if (this.session && this.session.isActive && !this.isRefreshing) {
        // Create a small silence audio chunk (100ms of silence at 16kHz, 16-bit PCM)
        const silenceDuration = 0.1; // 100ms
        const sampleRate = 16000;
        const bytesPerSample = 2; // 16-bit = 2 bytes
        const silenceBytes = Math.floor(silenceDuration * sampleRate * bytesPerSample);
        const silenceChunk = Buffer.alloc(silenceBytes, 0); // All zeros = silence
        
        // Send silence chunk directly to queue (bypass sendAudioChunk checks)
        this.addEventToQueue({
          event: {
            audioInput: {
              promptName: this.session.promptName,
              contentName: this.session.audioContentId,
              content: silenceChunk.toString('base64'),
            },
          },
        });
      }
    }, this.KEEP_ALIVE_INTERVAL);
  }

  private stopKeepAliveTimer(): void {
    if (this.keepAliveTimer) {
      clearInterval(this.keepAliveTimer);
      this.keepAliveTimer = null;
      console.log('✓ Keep-alive timer stopped');
    }
  }

  private async setupInitialEvents(isRefresh: boolean = false): Promise<void> {
    if (!this.session) return;

    console.log(`Setting up initial events${isRefresh ? ' (with conversation history)' : ''}...`);

    // Session start
    this.addEventToQueue({
      event: {
        sessionStart: {
          inferenceConfiguration: {
            maxTokens: 1024,
            topP: 0.9,
            temperature: 0.7,
          },
          turnDetectionConfiguration: {
            endpointingSensitivity: this.endpointingSensitivity,
          },
        },
      },
    });

    // Prompt start with tool configuration if Strands tool is available
    const promptStartEvent: any = {
      event: {
        promptStart: {
          promptName: this.session.promptName,
          textOutputConfiguration: {
            mediaType: 'text/plain',
          },
          audioOutputConfiguration: {
            audioType: 'SPEECH',
            encoding: 'base64',
            mediaType: 'audio/lpcm',
            sampleRateHertz: 16000,
            sampleSizeBits: 16,
            channelCount: 1,
            voiceId: this.voiceId, // Use configured voice ID
          },
        },
      },
    };

    const tools = this.buildToolSpecs();

    if (tools.length > 0) {
      promptStartEvent.event.promptStart.toolUseOutputConfiguration = {
        mediaType: 'application/json',
      };

      promptStartEvent.event.promptStart.toolConfiguration = {
        tools: tools,
        toolChoice: {
          auto: {},
        },
      };

      console.log(
        `✓ Tools configured in promptStart: ${tools.map((t: any) => t.toolSpec.name).join(', ')}`,
      );
    }

    this.addEventToQueue(promptStartEvent);
    this.session.isPromptStartSent = true;

    // System prompt with tool usage instructions if Strands tool is configured
    const textPromptID = randomUUID();
    this.addEventToQueue({
      event: {
        contentStart: {
          promptName: this.session.promptName,
          contentName: textPromptID,
          type: 'TEXT',
          interactive: false,
          role: 'SYSTEM',
          textInputConfiguration: {
            mediaType: 'text/plain',
          },
        },
      },
    });

    const finalSystemPrompt = this.buildSystemPromptContent();
    console.log('📋 Final system prompt being sent to Nova:');
    console.log('─'.repeat(80));
    console.log(finalSystemPrompt);
    console.log('─'.repeat(80));
    console.log(`   Total length: ${finalSystemPrompt.length} characters`);
    
    this.addEventToQueue({
      event: {
        textInput: {
          promptName: this.session.promptName,
          contentName: textPromptID,
          content: finalSystemPrompt,
        },
      },
    });

    this.addEventToQueue({
      event: {
        contentEnd: {
          promptName: this.session.promptName,
          contentName: textPromptID,
        },
      },
    });

    // If this is a refresh, add conversation history. Not in translator mode:
    // there is nothing to carry over between translated turns, and earlier turns
    // are what a translator session drifts on (see translator-turns.ts).
    if (isRefresh && this.meetingMode !== 'translator') {
      await this.setupConversationHistory();
    }

    // Audio content start
    this.addEventToQueue({
      event: {
        contentStart: {
          promptName: this.session.promptName,
          contentName: this.session.audioContentId,
          type: 'AUDIO',
          interactive: true,
          role: 'USER',
          audioInputConfiguration: {
            audioType: 'SPEECH',
            encoding: 'base64',
            mediaType: 'audio/lpcm',
            sampleRateHertz: 16000,
            sampleSizeBits: 16,
            channelCount: 1,
          },
        },
      },
    });
    this.session.isAudioContentStartSent = true;

    console.log('Initial events setup complete');
  }

  /**
   * The tool specs to register on the Nova prompt for the current meeting mode
   * and translator state.
   *
   *   - Strands agent: non-translator modes, when a Strands ARN is configured.
   *   - Stop-talking `mute`: every conversational mode (`normal` and `group`).
   *   - Translator `mute` / `unmute`: translator mode only, one at a time —
   *     exposing just the tool for the current state keeps the model from
   *     spontaneously calling the opposite transition (e.g. self-unmuting
   *     while paused).
   */
  private buildToolSpecs(): any[] {
    const translatorMode = (this.meetingMode === 'translator');
    const tools: any[] = [];

    if (this.strandsLambdaArn && !translatorMode) {
      tools.push({
        toolSpec: {
          name: 'strands_agent',
          description: 'Delegate complex queries to the Strands agent, which has access to document search, meeting history, web search, and other specialized tools. Use this for questions about documents, past meetings, current information from the web, or any query requiring specialized knowledge or data access.',
          inputSchema: {
            json: JSON.stringify({
              type: 'object',
              properties: {
                query: {
                  type: 'string',
                  description: 'The user\'s question or request to be processed by the Strands agent',
                },
              },
              required: ['query'],
            }),
          },
        },
      });
    }

    if (hasStopTalkingTool(this.meetingMode)) {
      tools.push({
        toolSpec: {
          name: 'mute',
          description: 'Mute yourself immediately. Call this ONLY when a participant explicitly asks YOU to stop, stop talking, be quiet, shut up, mute yourself, etc. The request must be addressed to you, by name or wake phrase, or be a direct reply to something you just said. Do NOT call this when participants say such things to each other.',
          inputSchema: {
            json: JSON.stringify({
              type: 'object',
              properties: {},
            }),
          },
        },
      });
    }

    if (!translatorMode) {
      tools.push(SET_MODE_TOOL_SPEC);
    }

    if (translatorMode) {
      const mutePhraseList = this.translatorMutePhrases.map(p => `"${p}"`).join(' or ');
      const unmutePhraseList = this.translatorUnmutePhrases.map(p => `"${p}"`).join(' or ');
      if (this._translatorState === 'UNMUTED') {
        tools.push({
          toolSpec: {
            name: 'mute',
            description: `Mute yourself. Call this tool if and only if you hear the exact phrase ${mutePhraseList}. Do not call this tool for any other reason.`,
            inputSchema: {
              json: JSON.stringify({
                type: 'object',
                properties: {},
              }),
            },
          },
        });
      } else {
        tools.push({
          toolSpec: {
            name: 'unmute',
            description: `Unmute yourself. Call this tool if and only if you hear the exact phrase ${unmutePhraseList}. Do not call this tool for any other reason.`,
            inputSchema: {
              json: JSON.stringify({
                type: 'object',
                properties: {},
              }),
            },
          },
        });
      }
    }

    return tools;
  }

  private buildSystemPromptContent(): string {
    // Translator mode REPLACES the base system prompt. Each translator state
    // (UNMUTED / MUTED) gets its own focused prompt with no mention of the
    // opposite state — this, combined with state-conditional tool registration
    // in setupInitialEvents(), prevents spontaneous state transitions.
    if (this.meetingMode === 'translator') {
      const a = this.translatorLanguageA;
      const b = this.translatorLanguageB;
      const mutePhraseList = this.translatorMutePhrases.map(p => `"${p}"`).join(' or ');
      const unmutePhraseList = this.translatorUnmutePhrases.map(p => `"${p}"`).join(' or ');

      if (this._translatorState === 'UNMUTED') {
        return [
          'TRANSLATOR MODE:',
          `You are a live bidirectional interpreter between ${a} and ${b}.`,
          '',
          'Rules (follow these strictly):',
          // Each direction is named outright. Nova 2 Sonic, given only "the
          // OTHER language" and "speak in A or B", repeated an English
          // utterance back in English in 4 of 17 single-utterance replays of
          // this session setup; with these three lines, in none of 24 (18 of
          // them with the previous "unclear or in a third language" wording
          // of rule 6, 6 with the current one).
          ...translatorDirectionRules(a, b),
          '4. Do NOT greet, explain, summarize, answer questions, or add any commentary.',
          '5. Preserve meaning, names, numbers, and dates. Keep output concise.',
          `6. If an utterance is in neither ${a} nor ${b}, translate it into ${a}.`,
          '7. Never say anything on your own. Do not introduce yourself. Do not ask clarifying questions.',
          '',
          'Begin translating from the very next utterance you hear.',
          '',
          '---',
          '',
          'PAUSE TRIGGER — INDEPENDENT INSTRUCTION:',
          `If, and only if, you hear the exact phrase ${mutePhraseList}, do the following:`,
          '  - Call the `mute` tool.',
          '  - Do NOT translate the phrase. Do NOT speak any response. Do NOT say anything else.',
          'Otherwise, never call the `mute` tool.',
          'You have only one tool available: `mute`. There are no other tools.',
        ].join('\n');
      }

      return [
        'TRANSLATOR MODE — PAUSED:',
        'You are currently paused. Your job right now is to stay completely silent and listen for one specific trigger phrase.',
        '',
        'Rules (follow these strictly):',
        '1. Do NOT translate ANY utterance, in any language, for any reason.',
        '2. Do NOT speak any response.',
        '3. Do NOT generate any audio output.',
        '4. Do NOT call any tool other than the one described below.',
        '5. Do NOT comment on, acknowledge, or react to anything you hear.',
        '6. Stay completely silent until the resume trigger phrase below is heard.',
        '',
        'Even if you hear urgent-sounding speech, questions, names being called, or many utterances accumulating — you remain paused. Translation does not resume on its own. It only resumes when you hear the exact resume trigger below and call the `unmute` tool.',
        '',
        '---',
        '',
        'RESUME TRIGGER — INDEPENDENT INSTRUCTION:',
        `If, and only if, you hear the exact phrase ${unmutePhraseList}, do the following:`,
        '  - Call the `unmute` tool.',
        '  - Do NOT speak any response. Do NOT say anything else.',
        'Otherwise, never call the `unmute` tool.',
        'You have only one tool available: `unmute`. There are no other tools.',
      ].join('\n');
    }

    let systemPromptContent = '';
    
    // Add group meeting mode instructions (simplified - wake phrase detection handled by our code)
    // Stop-request handling is not repeated here: it lives in the STOP REQUESTS
    // block below, shared by every mode that registers the `mute` tool.
    if (this.groupMeetingMode) {
      systemPromptContent += 'GROUP MEETING MODE:\n' +
        'You are in a group meeting where multiple people are talking. ' +
        'You will hear all conversation, but your audio output is managed externally - ' +
        'you will only be heard when someone addresses you by name.\n' +
        'Just respond naturally to whatever you hear. Keep responses concise.\n' +
        // A spoken reply to talk not aimed at the assistant is only dropped
        // audio, but a lookup is a Strands invocation whether or not anyone
        // hears the answer. The agent declines one requested while muted
        // (muted-lookup.ts); this keeps the model from asking for it.
        (this.strandsLambdaArn
          ? 'Only use strands_agent when someone asks YOU for information - by name, or as a '
            + 'follow-up to something you just said. Do NOT look anything up in response to '
            + 'participants talking to each other.\n'
          : '') +
        '\n';
    }

    // Add base system prompt
    systemPromptContent += this.systemPrompt;

    const stopToolAvailable = hasStopTalkingTool(this.meetingMode);

    // Add interruption handling instructions (applies to all modes)
    systemPromptContent += '\n\nINTERRUPTION HANDLING:\n' +
      'When a user interrupts you mid-response (you will see { "interrupted" : true }):\n' +
      '1. IMMEDIATELY acknowledge the interruption with a brief phrase like:\n' +
      '   - "Got it" or "Sure" or "Okay" (if they\'re correcting you)\n' +
      '   - "Of course" or "Absolutely" (if they\'re asking something new)\n' +
      '   - Keep it to 1-2 words for minimal interruption\n' +
      (stopToolAvailable
        // An interruption that asks for silence must not be treated as the
        // preface to a new request, or the acknowledgement is followed by a
        // paragraph of explanation and the assistant never actually stops.
        ? '2. If the interruption was asking you to stop, stop talking, be quiet, shut up or mute '
          + 'yourself, follow STOP REQUESTS below instead of continuing.\n'
          + '3. Otherwise, proceed with their new request\n'
        : '2. THEN proceed with their new request\n') +
      'This provides instant feedback and shows you\'re responsive.\n';

    // Add stop-request handling. Registered wherever the `mute` tool is, so the
    // prompt and the tool set cannot drift apart.
    if (stopToolAvailable) {
      systemPromptContent += '\n\nSTOP REQUESTS:\n' +
        'When someone asks YOU to stop, stop talking, be quiet, shut up or mute yourself:\n' +
        '1. Say exactly one short word - "Okay" - and nothing else.\n' +
        '2. Call the `mute` tool.\n' +
        '3. Then stay silent. Do NOT explain yourself, do NOT summarize what you were saying, '
        + 'do NOT offer further help, and do NOT ask whether they need anything else.\n' +
        'A request to stop is a request for silence, not the start of a new request. '
        + 'Someone will say your wake phrase again when they want you back.\n' +
        // You hear the whole meeting, so most of what is said is not aimed at
        // you. Acting on a "be quiet" meant for another participant would take
        // the assistant out of the meeting, and in an always_active deployment
        // only a wake phrase brings it back.
        'Only act on a request that is addressed to YOU - by name or wake phrase, or as a '
        + 'direct reply to something you just said. Participants telling EACH OTHER to be quiet '
        + 'is ordinary meeting conversation: stay as you are and do NOT call the `mute` tool.\n';
    }

    // The set_mode tool is registered in every mode that reaches this point
    // (translator mode returns its own prompt above).
    systemPromptContent += '\n\n' + SET_MODE_PROMPT;

    // Add Strands agent tool instructions.
    //
    // Nova Sonic is speech-to-speech: it has no private scratchpad, so any
    // reasoning it produces is synthesized and played into the meeting. A
    // numbered "first / then / after" procedure here was observed being recited
    // back as a spoken plan ("I should first acknowledge their request... Then,
    // I'll call the strands_agent..."), so the guidance is a single instruction
    // plus an explicit rule that planning is never spoken. A filter cannot do
    // this job: the SPECULATIVE text block arrives before its audio, but the
    // audio is already being generated, and Nova's own FINAL text confirms what
    // was voiced only after the fact.
    if (this.strandsLambdaArn) {
      systemPromptContent += '\n\nINFORMATION REQUESTS:\n' +
        'You have the strands_agent tool, a sub-agent that looks up information and takes 5-10 seconds to respond.\n' +
        'Use strands_agent when users ask about:\n' +
        '- Meeting summaries or transcripts\n' +
        '- Past meetings or meeting history\n' +
        '- Documents or knowledge base content\n' +
        '- Web search or current information\n' +
        '- Any query requiring specialized knowledge or data access\n\n' +
        'When a request needs strands_agent, say one short sentence to the person first, such as '
        + '"Let me search for that information. This may take a moment.", then call the tool. '
        + 'This sentence is required every time you call strands_agent, including for follow-up '
        + 'questions and corrections later in the same conversation, and however many times you have '
        + 'said it before: never call strands_agent without saying it first, because the people '
        + 'listening hear nothing while the tool runs. '
        + 'When the result arrives, answer naturally with the information.\n\n' +
        'SPOKEN OUTPUT ONLY:\n' +
        'Everything you say is played aloud in the meeting. Speak only words meant for the people '
        + 'listening. Never say your reasoning or your plan out loud: do not describe the request in the '
        + 'third person (for example "the user asked..."), do not narrate the steps you are about to take, '
        + 'and never mention strands_agent or any other tool by name.';
    }
    
    return systemPromptContent;
  }

  /** Whether `text` contains any configured translator mute or unmute trigger phrase
   *  (after normalization). Used to keep the trigger utterance itself out of replayed
   *  history, since lexical proximity to the opposite trigger can cause the new
   *  session to immediately call the opposite tool. */
  private isTranslatorTriggerUtterance(text: string): boolean {
    if (this.meetingMode !== 'translator') return false;
    const normalized = normalizeForTrigger(text);
    if (!normalized) return false;
    return this.translatorMutePhrases.some(p => normalized.includes(p))
      || this.translatorUnmutePhrases.some(p => normalized.includes(p));
  }

  private addToConversationHistory(role: 'USER' | 'ASSISTANT', content: string): void {
    if (role === 'USER' && this.isTranslatorTriggerUtterance(content)) {
      console.log(`💬 Skipping translator trigger utterance from history: "${content.substring(0, 60)}..."`);
      return;
    }

    this.conversationHistory.push({
      role,
      content,
      timestamp: Date.now(),
    });

    if (this.conversationHistory.length > this.MAX_HISTORY_TURNS) {
      this.conversationHistory = this.conversationHistory.slice(-this.MAX_HISTORY_TURNS);
    }
  }

  private async setupConversationHistory(): Promise<void> {
    if (!this.session || this.conversationHistory.length === 0) return;

    // Find the first USER turn - history must start with USER
    let startIndex = this.conversationHistory.findIndex(turn => turn.role === 'USER');
    
    if (startIndex === -1) {
      // No USER turns found - manufacture one to satisfy Nova's requirement
      console.log('📜 No USER turns in history - manufacturing initial USER turn');
      const historyToSend: ConversationTurn[] = [
        { role: 'USER', content: 'Continue our conversation', timestamp: Date.now() },
        ...this.conversationHistory
      ];
      
      console.log(`📜 Setting up conversation history (${historyToSend.length} turns, starting with manufactured USER)...`);
      await this.sendHistoryTurns(historyToSend);
    } else {
      // Get history starting from first USER turn
      const historyToSend = this.conversationHistory.slice(startIndex);
      
      console.log(`📜 Setting up conversation history (${historyToSend.length} turns, starting with USER)...`);
      
      await this.sendHistoryTurns(historyToSend);
    }

    console.log('✓ Conversation history setup complete');
  }

  private async sendHistoryTurns(turns: ConversationTurn[]): Promise<void> {
    if (!this.session) return;

    // Send each turn as a text content block
    for (const turn of turns) {
      const textContentId = randomUUID();
      
      this.addEventToQueue({
        event: {
          contentStart: {
            promptName: this.session.promptName,
            contentName: textContentId,
            type: 'TEXT',
            interactive: false,
            role: turn.role,
            textInputConfiguration: {
              mediaType: 'text/plain',
            },
          },
        },
      });

      this.addEventToQueue({
        event: {
          textInput: {
            promptName: this.session.promptName,
            contentName: textContentId,
            content: turn.content,
            role: turn.role,
          },
        },
      });

      this.addEventToQueue({
        event: {
          contentEnd: {
            promptName: this.session.promptName,
            contentName: textContentId,
          },
        },
      });
    }
  }

  private startSessionRefreshTimer(): void {
    // Clear any existing timer
    if (this.sessionRefreshTimer) {
      clearTimeout(this.sessionRefreshTimer);
    }

    console.log(`🔄 Session refresh timer started - will queue refresh in ${this.SESSION_REFRESH_INTERVAL / 60000} minutes`);
    
    this.sessionRefreshTimer = setTimeout(() => {
      // Queue the refresh - it will execute when agent is idle
      this.refreshQueued = true;
      console.log('⏰ Session refresh queued - waiting for agent to be idle');
      this.checkAndExecuteQueuedRefresh();
    }, this.SESSION_REFRESH_INTERVAL);
  }

  private checkAndExecuteQueuedRefresh(): void {
    if (!this.refreshQueued) return;

    // A refresh already in flight is building the session this one would build.
    // Retry rather than fall through: `refreshSession` declines to start a
    // second teardown, so going on from here would clear the flag and then do
    // nothing, dropping the queued refresh entirely.
    if (this.isRefreshing) {
      console.log('🔄 Refresh already in flight - will retry refresh check in 2 seconds');
      setTimeout(() => this.checkAndExecuteQueuedRefresh(), 2000);
      return;
    }

    // Check if agent is busy
    if (this._isSpeaking || this._isProcessingTool) {
      console.log('🔊 Agent busy - will retry refresh check in 2 seconds');
      setTimeout(() => this.checkAndExecuteQueuedRefresh(), 2000);
      return;
    }

    // A refresh tears down the bidirectional stream, so a user mid-utterance
    // loses whatever they were saying: the old session dies with their audio
    // and the new one only receives the text as history, which never triggers
    // a response. The agent then appears deaf until re-addressed. Nova's USER
    // text events are the freshest signal we have for "someone is talking",
    // so hold the refresh until they have been quiet for a few seconds.
    if (Date.now() - this.lastUserActivityAt < this.USER_QUIET_MS_BEFORE_REFRESH) {
      console.log('🗣️  User recently speaking - will retry refresh check in 2 seconds');
      setTimeout(() => this.checkAndExecuteQueuedRefresh(), 2000);
      return;
    }

    // Agent is idle - execute refresh
    this.refreshQueued = false;
    this.refreshSession().catch(error => {
      console.error('Error executing queued refresh:', error);
    });
  }

  private checkAndExecuteQueuedAutoMute(): void {
    if (!this.autoMuteQueued) return;

    // Check if agent is busy
    if (this._isSpeaking || this._isProcessingTool) {
      console.log('🔊 Agent busy - will retry auto-mute check in 1 second');
      setTimeout(() => this.checkAndExecuteQueuedAutoMute(), 1000);
      return;
    }

    // Agent is idle - execute auto-mute
    this.autoMuteQueued = false;
    this.executeAutoMute();
  }

  private executeAutoMute(): void {
    if (!this.groupMeetingMode || this._isMuted) return;
    
    console.log('🔇 Auto-muting agent after conversation end (group meeting mode)');
    this._isMuted = true;
    this.conversationActive = false;
    this.clearConversationTimeout();
  }

  private startConversationSession(): void {
    if (!this.groupMeetingMode) return;
    
    console.log('💬 Starting conversation session');
    this.conversationActive = true;
    this.clearConversationTimeout();
    
    // Set timeout to auto-mute after defaultActivationDuration seconds of no "Alex" mentions
    const timeoutMs = this.defaultActivationDuration * 1000;
    this.conversationTimeout = setTimeout(() => {
      console.log(`⏰ Conversation timeout reached (${this.defaultActivationDuration}s) - queueing auto-mute`);
      this.autoMuteQueued = true;
      this.checkAndExecuteQueuedAutoMute();
    }, timeoutMs);
    
    console.log(`⏱️  Conversation timeout set for ${this.defaultActivationDuration} seconds`);
  }

  private containsWakePhrase(text: string): boolean {
    const wakePhrases = configuredWakePhrases();

    // Normalize input: strip punctuation and collapse whitespace
    // This handles transcription artifacts like "hey, alex" or "ok. alex"
    const normalized = text.toLowerCase()
      .replace(/[,.\?!;:'"]/g, ' ')  // Replace punctuation with spaces
      .replace(/\s+/g, ' ')           // Collapse multiple spaces
      .trim();
    
    // DEBUG: Log the wake phrases being checked
    console.log('🔍 DEBUG containsWakePhrase():');
    console.log(`   Input text: "${text}"`);
    console.log(`   Normalized (punctuation stripped): "${normalized}"`);
    console.log(`   Configured wake phrases: [${wakePhrases.map(p => `"${p}"`).join(', ')}]`);
    
    // Use word boundary matching to avoid false positives
    // "okay" should NOT match "ok alex"
    const matchResult = wakePhrases.some((phrase: string) => {
      // Escape all regex special characters, then allow flexible whitespace between words
      const escapedPhrase = phrase.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\s+/g, '\\s+');
      
      // Create regex with word boundaries
      // \b ensures we match complete words, not partial matches
      // nosemgrep: javascript.lang.security.audit.detect-non-literal-regexp.detect-non-literal-regexp -- Inputs are regex-escaped by the preceding .replace() call; escaped wake-phrase cannot produce catastrophic backtracking
      const regex = new RegExp(`\\b${escapedPhrase}\\b`, 'i');
      const matches = regex.test(normalized);
      
      // DEBUG: Log each phrase test
      console.log(`   Testing phrase "${phrase}" (regex: /\\b${escapedPhrase}\\b/i): ${matches ? '✓ MATCH' : '✗ no match'}`);
      
      return matches;
    });
    
    console.log(`   Final result: ${matchResult ? '✓ WAKE PHRASE DETECTED' : '✗ No wake phrase'}`);
    return matchResult;
  }

  private resetConversationTimeout(): void {
    if (!this.groupMeetingMode || !this.conversationActive) return;
    
    console.log('🔄 Resetting conversation timeout - user mentioned wake phrase');
    this.clearConversationTimeout();
    
    // Restart timeout
    const timeoutMs = this.defaultActivationDuration * 1000;
    this.conversationTimeout = setTimeout(() => {
      console.log(`⏰ Conversation timeout reached (${this.defaultActivationDuration}s) - queueing auto-mute`);
      this.autoMuteQueued = true;
      this.checkAndExecuteQueuedAutoMute();
    }, timeoutMs);
  }

  private clearConversationTimeout(): void {
    if (this.conversationTimeout) {
      clearTimeout(this.conversationTimeout);
      this.conversationTimeout = null;
    }
  }

  private async refreshSession(): Promise<void> {
    if (!this.session || !this.session.isActive) {
      console.log('⚠️  Cannot refresh - session not active');
      return;
    }

    // Don't refresh if already refreshing
    if (this.isRefreshing) {
      console.log('⚠️  Session refresh already in progress');
      return;
    }

    console.log('🔄 Refreshing Nova session - closing old session and creating new one...');
    
    try {
      // Set refreshing flag
      this.isRefreshing = true;

      // An outgoing translator session belongs to the state being left.
      this.closeDrainingSessions('refresh');

      // Stop keep-alive timer during refresh
      this.stopKeepAliveTimer();

      // Close the old session gracefully (like stopAudio in the example)
      if (this.session) {
        try {
          // End audio content
          if (this.session.isAudioContentStartSent) {
            this.addEventToQueue({
              event: {
                contentEnd: {
                  promptName: this.session.promptName,
                  contentName: this.session.audioContentId,
                },
              },
            });
          }

          // End prompt
          if (this.session.isPromptStartSent) {
            this.addEventToQueue({
              event: {
                promptEnd: {
                  promptName: this.session.promptName,
                },
              },
            });
          }

          // End session
          this.addEventToQueue({
            event: {
              sessionEnd: {},
            },
          });

          // Wait for events to be sent
          await new Promise(resolve => setTimeout(resolve, 300));

          // Mark session as inactive
          this.session.isActive = false;
          this.session.signal?.();
        } catch (error) {
          console.error('Error closing old session during refresh:', error);
        }
      }

      // Reset session state
      this.session = null;
      this.sessionId = null;
      this._isActive = false;
      this.streamingActive = false;

      // Small delay before creating new session
      await new Promise(resolve => setTimeout(resolve, 100));

      // Create new session (like startNewChat in the example)
      // This will also restart the keep-alive timer
      await this.createSession(true); // Pass true to indicate this is a refresh

      console.log('✅ Session refreshed successfully - new session created and ready');

      // Clear refreshing flag
      this.isRefreshing = false;

    } catch (error) {
      console.error('❌ Error during session refresh:', error);
      // Clear refreshing flag on error
      this.isRefreshing = false;
      this._isActive = false;
      this.streamingActive = false;
      
      // Try to recover by creating a new session
      this.scheduleSessionCreateRetry(() => {
        console.log('🔄 Attempting recovery - creating new session');
        this.createSession().catch(err => {
          console.error('Error in recovery session creation:', err);
        });
      });
    }
  }

  /**
   * Run a session-recovery attempt a second from now, unless the agent has
   * been stopped by then.
   *
   * Both recoveries — refreshSession's own and the one a failed mode change
   * schedules — go through here, so that stop() can reach them. A bare timer
   * could not be cleared, and would open a fresh Bedrock session for an agent
   * whose meeting has already ended. Only one is held: a later recovery
   * supersedes an earlier one, since both would build the same session.
   */
  private scheduleSessionCreateRetry(attempt: () => void, delayMs: number = 1000): void {
    if (this.sessionRecoveryTimer) {
      clearTimeout(this.sessionRecoveryTimer);
    }
    this.sessionRecoveryTimer = setTimeout(() => {
      this.sessionRecoveryTimer = null;
      if (this.stopped) {
        console.log('⏹️  Agent stopped - skipping session recovery');
        return;
      }
      attempt();
    }, delayMs);
  }

  private addEventToQueue(event: any): void {
    if (this.session) {
      this.queueEvent(this.session, event);
    }
  }

  /** Queue an event on a given session, which need not be the current one. */
  private queueEvent(session: SessionData, event: any): void {
    if (!session.isActive) return;
    session.queue.push(event);
    session.signal?.();
  }

  /** The request stream of one session. Bound to that session rather than to
   *  `this.session`, which in translator mode moves on to the next session
   *  while this one is still finishing its answer. */
  private async *generateEventStream(
    session: SessionData | null = this.session,
  ): AsyncIterable<InvokeModelWithBidirectionalStreamInput> {
    if (!session) return;

    const textEncoder = new TextEncoder();

    while (session.isActive) {
      // Wait for an event to be queued, or for the session to close
      if (session.queue.length === 0) {
        await new Promise<void>(resolve => {
          session.signal = resolve;
        });
        session.signal = null;
      }

      if (!session.isActive) break;

      while (session.queue.length > 0) {
        const event = session.queue.shift();
        if (event) {
          yield {
            chunk: {
              bytes: textEncoder.encode(JSON.stringify(event)),
            },
          };
        }
      }
    }
  }

  /** A session's stream has been accepted: the refusal streaks end. */
  private noteSessionOpened(session: SessionData): void {
    session.resolveOpened?.('open');
    this.sessionOpenThrottles = 0;
    if (session.handoverFrom) {
      session.handoverFrom = null;
      this.translatorHandoverThrottles = 0;
    }
  }

  /**
   * Undo a translator hand-over whose new session was refused for Nova Sonic's
   * concurrency quota, so the meeting stays on the session it was leaving: one
   * stream rather than none.
   *
   * That session stops draining and becomes the current one again, is sent the
   * meeting audio the refused session was sent in its place, and hands over
   * again only after the next participant turn, and not before a backoff that
   * grows while the refusals continue. Until then it translates on its own,
   * as a translator did before it had a session per turn. Returns false when
   * there is no such session to keep — it has already finished its answer, or
   * the agent has moved on — and the caller recovers as for any failed session.
   */
  private keepOutgoingTranslatorSession(failed: SessionData): boolean {
    const outgoing = failed.handoverFrom;
    failed.handoverFrom = null;
    if (
      !outgoing
      || failed !== this.session
      || !this.drainingSessions.has(outgoing)
      || !outgoing.isActive
      || this.stopped
    ) {
      return false;
    }

    this.drainingSessions.delete(outgoing);
    if (outgoing.drainTimer) {
      clearTimeout(outgoing.drainTimer);
      outgoing.drainTimer = null;
    }
    outgoing.draining = false;
    outgoing.drainStartedAt = undefined;
    outgoing.handedOver = false;
    outgoing.lastUserTurnEndAt = undefined;

    failed.isActive = false;
    failed.signal?.();
    this.heldPlayback.delete(failed);

    this.session = outgoing;
    this.sessionId = outgoing.id!;
    // It heard silence while the refused session was being sent the meeting.
    for (const chunk of this.recentMeetingAudio.since(failed.sessionStartTime)) {
      this.queueAudio(outgoing, chunk);
    }

    const delay = throttleBackoffMs(this.translatorHandoverThrottles, TRANSLATOR_HANDOVER_BACKOFF);
    this.translatorHandoverThrottles += 1;
    this.translatorHandoverBlockedUntil = Date.now() + delay;
    console.warn(
      `[translator-turn] action=kept-session reason=throttled session=${outgoing.id} refused=${failed.id}`
      + ` next_handover_after_ms=${delay} streak=${this.translatorHandoverThrottles}`,
    );
    this.releaseHeldPlayback();
    return true;
  }

  /** Rebuild an always-listening session whose open was refused for the quota,
   *  after a backoff. Superseded if anything replaces the session meanwhile. */
  private scheduleThrottledRecovery(failed: SessionData): void {
    const delay = throttleBackoffMs(this.sessionOpenThrottles, SESSION_RECOVERY_BACKOFF);
    this.sessionOpenThrottles += 1;
    console.warn(
      `[nova-throttle] action=retry-session in_ms=${delay} streak=${this.sessionOpenThrottles}`,
    );
    const attempt = (): void => {
      if (this.session !== failed || this.streamingActive) return;
      if (this.isRefreshing) {
        this.scheduleSessionCreateRetry(attempt, 1000);
        return;
      }
      this.refreshSession().catch((error) => {
        console.error('Error rebuilding the session after a refused open:', error);
      });
    };
    this.scheduleSessionCreateRetry(attempt, delay);
  }

  private async initiateBidirectionalStreaming(session: SessionData | null = this.session): Promise<void> {
    if (!session) return;
    if (!this.bedrockClient) {
      session.resolveOpened?.('failed');
      return;
    }

    /** Whether Bedrock accepted the stream. Only a refusal to open it is the
     *  concurrency quota; anything after that is an ordinary stream error. */
    let accepted = false;
    try {
      console.log('Starting bidirectional stream...');

      const command = new InvokeModelWithBidirectionalStreamCommand({
        modelId: this.modelId,
        body: this.generateEventStream(session),
      });

      const response = await this.bedrockClient.send(command);
      accepted = true;
      this.meteredUsage().noteSession();
      this.noteSessionOpened(session);

      console.log('Stream established, processing responses...');

      // Process response stream
      await this.processResponseStream(response, session);

      // A session that is no longer the current one was replaced on purpose:
      // its ending is not a reason to rebuild the current one.
      if (session !== this.session) {
        this.forgetDrainingSession(session);
        return;
      }

      // If we get here, the stream closed normally or with an error
      // Check if session is still supposed to be active (always_active mode).
      // Not once the agent is stopping: stop() closes the session, and its
      // stream ends before the session is cleared.
      if (this.activationMode === 'always_active' && !this.isRefreshing && !this.stopped) {
        console.log('⚠️  Stream closed unexpectedly - triggering automatic refresh');
        // Trigger immediate refresh to recover
        this.refreshSession().catch(error => {
          console.error('Error in automatic refresh after stream close:', error);
        });
      }

    } catch (error) {
      const throttled = !accepted && isNovaThrottle(error);
      session.resolveOpened?.(throttled ? 'throttled' : 'failed');
      if (throttled) {
        // A refusal is expected under load, and handled below: logged on one
        // line rather than with a stack trace.
        noteNovaThrottle(
          session.handoverFrom ? 'translator-turn' : 'assistant-session',
          `session=${session.id}`,
        );
      } else {
        console.error('Error in bidirectional streaming:', error);
      }
      // A translator's next session refused for the quota: the session it was
      // taking over from carries on instead.
      if (throttled && this.keepOutgoingTranslatorSession(session)) {
        return;
      }
      // An outgoing translator session failing takes nothing else with it.
      if (session !== this.session) {
        this.forgetDrainingSession(session);
        return;
      }
      this._isActive = false;
      this.streamingActive = false;

      // An always-listening session refused for the quota is rebuilt after a
      // backoff that grows while the refusals continue, instead of a second
      // from now. A wake-phrase session is retried by activate() itself.
      if (throttled) {
        if (this.activationMode === 'always_active') {
          this.scheduleThrottledRecovery(session);
        }
        return;
      }

      // Trigger automatic refresh if in always_active mode
      if (this.activationMode === 'always_active' && !this.isRefreshing) {
        console.log('⚠️  Stream error detected - triggering automatic refresh');
        setTimeout(() => {
          this.refreshSession().catch(err => {
            console.error('Error in automatic refresh after stream error:', err);
          });
        }, 1000);
      }
    }
  }

  private async processResponseStream(response: any, session: SessionData | null = this.session): Promise<void> {
    const textDecoder = new TextDecoder();
    let toolUseContent: any = null;
    let toolUseId: string = '';
    let toolName: string = '';
    /** Set at a `strands_agent` tool-use event when the lookup is not to be run. */
    let lookupSkip: LookupSkipReason | null = null;
    
    // Track current conversation turn for history
    let currentTurn: { role: 'USER' | 'ASSISTANT' | null; content: string; isFinal: boolean } = {
      role: null,
      content: '',
      isFinal: false,
    };

    try {
      for await (const event of response.body) {
        if (!session?.isActive) break;

        if (event.chunk?.bytes) {
          const textResponse = textDecoder.decode(event.chunk.bytes);

          try {
            const jsonResponse = JSON.parse(textResponse);

            // Speech and text tokens, as the model counts them, for the
            // meeting's cost estimate. Only the delta is added, so the
            // cumulative totals of a refreshed session are not counted twice.
            if (jsonResponse.event?.usageEvent) {
              this.meteredUsage().add(jsonResponse.event.usageEvent);
            }

            if (jsonResponse.event?.contentStart) {
              console.log('Content start:', jsonResponse.event.contentStart.type);
              // In translator mode up to three sessions deliver events here at
              // once, and the agent-wide audio state below (the interruption
              // gate, the dropped-block flag, the stop acknowledgement's block
              // counting) is shared between them. That is safe because only
              // the newest session ever hears the meeting: an outgoing one hears
              // silence, so it is never barged in on and never asked to stop,
              // and the newest has not started speaking, because a session
              // hands over the moment it starts an answer.
              
              // Clear interruption gate when Nova starts a new AUDIO content block
              if (jsonResponse.event.contentStart.type === 'AUDIO' && this._isInterrupted) {
                console.log('✓ New audio content started - clearing interruption gate');
                this._isInterrupted = false;
              }
              if (jsonResponse.event.contentStart.type === 'AUDIO') {
                session.audioTurnOpen = true;
                if (this.pendingVoiceModeSwitch && this.pendingVoiceModeSwitch.session === session) {
                  this.pendingVoiceModeSwitch.sawAudio = true;
                }
                this.noteAudioBlockStarted();
                this.noteAssistantAudioBlockStarted();
              } else if (
                jsonResponse.event.contentStart.type === 'TEXT'
                && jsonResponse.event.contentStart.role === 'USER'
              ) {
                this.lastUserTextAt = Date.now();
              }
              
              // Translator mode: the session has started answering a
              // participant's turn, so the meeting moves on to a new session.
              if (
                !session.handedOver
                && jsonResponse.event.contentStart.role === 'ASSISTANT'
                && this.shouldStartNextTranslatorSession(session)
              ) {
                session.handedOver = true;
                this.startNextTranslatorSession(session);
              }

              // Check if this is a FINAL transcript (for conversation history)
              const additionalFields = jsonResponse.event.contentStart.additionalModelFields;
              if (additionalFields) {
                try {
                  const fields = JSON.parse(additionalFields);
                  if (fields.generationStage === 'FINAL') {
                    currentTurn.role = jsonResponse.event.contentStart.role;
                    currentTurn.content = '';
                    currentTurn.isFinal = true;
                  }
                } catch (e) {
                  // additionalModelFields might not be JSON
                }
              }
            } else if (jsonResponse.event?.textOutput) {
              const textContent = jsonResponse.event.textOutput.content;
              console.log('Text output:', textContent);
              
              // Check for interruption signal
              if (textContent.includes('"interrupted"') && textContent.includes('true')) {
                this.handleInterruption();
              }
              
              // Accumulate text for current turn if it's a FINAL transcript
              if (currentTurn.role && currentTurn.isFinal) {
                currentTurn.content += textContent;
                if (currentTurn.role === 'USER') {
                  this.lastUserActivityAt = Date.now();
                }
              }
            } else if (jsonResponse.event?.audioOutput) {
              // Received audio from Nova - play it immediately (non-blocking)
              const audioBuffer = Buffer.from(jsonResponse.event.audioOutput.content, 'base64');
              if (session?.silenced || session?.dropTurn) {
                // A translation of "stop translating", or a reply to an
                // unaddressed set_mode call: not to be heard.
              } else if (session && this.holdsPlaybackFor(session)) {
                this.holdPlayback(session, audioBuffer);
              } else {
                this.playAudio(audioBuffer).catch(err => {
                  console.error('Error playing audio:', err);
                });
              }
            } else if (jsonResponse.event?.toolUse) {
              // Store tool use information
              console.log('Tool use requested:', jsonResponse.event.toolUse.toolName);
              toolUseContent = jsonResponse.event.toolUse;
              toolUseId = jsonResponse.event.toolUse.toolUseId;
              toolName = jsonResponse.event.toolUse.toolName;
              if (toolName === 'strands_agent') {
                // Decided here, at the tool-use event, so that a lookup that will
                // not be run never arms the acknowledgement either.
                lookupSkip = this.strandsLookupSkipReason();
                if (!lookupSkip) {
                  // Also the earliest point: the acknowledgement fallback is timed
                  // from the tool-use event, not from the end of its content block.
                  this.noteStrandsToolRequested(toolUseId);
                }
              } else if (toolName === 'mute' && !session?.silenced) {
                // Earliest point the transcript route can be told to stand
                // down: the handler below is a step later, and every
                // millisecond of this window belongs to the acknowledgement.
                // Not for a session that heard "stop translating": its call
                // is that request (handleMuteTool), and the mode it is about
                // to be in has a stop request of its own to protect.
                this.noteMuteToolRequested();
              }
            } else if (jsonResponse.event?.contentEnd && jsonResponse.event?.contentEnd?.type === 'TOOL') {
              // Process tool use asynchronously when content ends
              console.log(`Processing tool use: ${toolName}`);
              if (toolName === 'strands_agent' && toolUseContent) {
                if (lookupSkip) {
                  this.skipStrandsLookup(toolUseId, lookupSkip).catch(error => {
                    console.error('Error answering a skipped lookup:', error);
                  });
                } else {
                  this.processToolUseAsync(toolUseId, toolName, toolUseContent).catch(error => {
                    console.error('Error in async tool processing:', error);
                  });
                }
                lookupSkip = null;
              } else if (toolName === 'mute') {
                // Handle mute tool call
                this.handleMuteTool(toolUseId, session).catch(error => {
                  console.error('Error handling mute tool:', error);
                });
              } else if (toolName === 'set_mode' && toolUseContent) {
                this.handleSetModeTool(toolUseId, toolUseContent, session).catch(error => {
                  console.error('Error handling set_mode tool:', error);
                });
              } else if (toolName === 'unmute') {
                // Handle unmute tool call (translator-mode only)
                this.handleUnmuteTool(toolUseId, session).catch(error => {
                  console.error('Error handling unmute tool:', error);
                });
              }
            } else if (jsonResponse.event?.contentEnd) {
              // The stop reason tells a finished turn (END_TURN) from one still
              // being spoken (PARTIAL_TURN) or cut off (INTERRUPTED).
              const endReason = jsonResponse.event.contentEnd.stopReason;
              console.log(`Content end: ${jsonResponse.event.contentEnd.type}${endReason ? ` ${endReason}` : ''}`);
              if (jsonResponse.event.contentEnd.type === 'AUDIO') {
                this.noteAudioBlockEnded();
                this.dropCurrentAudioBlock = false;
                // PARTIAL_TURN separates the blocks of one answer; anything else ends it.
                if (endReason && endReason !== 'PARTIAL_TURN') {
                  session.audioTurnOpen = false;
                  session.dropTurn = false;
                  if (this.heldPlayback.size > 0) {
                    this.releaseHeldPlayback();
                  }
                  // The confirmation of a set_mode switch has been generated
                  // (it plays out from the buffer), so the switch can be made.
                  const pending = this.pendingVoiceModeSwitch;
                  if (pending && pending.session === session && pending.sawAudio) {
                    this.runVoiceModeSwitch('confirmed');
                  }
                }
              }
              
              // "Stop translating": translator mode registers only its pause
              // tool, so it is left by this phrase (voice-mode-switch.ts). The
              // participants' transcript is read for it as well
              // (noteParticipantUtterance); whichever arrives first acts.
              if (
                this.meetingMode === 'translator'
                && currentTurn.role === 'USER'
                && currentTurn.isFinal
              ) {
                this.handleTranslatorUtterance(currentTurn.content, session, 'nova');
              }

              // Skip appending while paused in translator mode — replaying
              // muted-period utterances on the next UNMUTED session would
              // pressure the model to translate them as backlog.
              const skipForTranslatorMuted = (
                this.meetingMode === 'translator' &&
                this._translatorState === 'MUTED'
              );

              // A reply nobody heard — the translation of "stop translating", or
              // the answer to an unaddressed set_mode call — is kept out of the
              // history and the transcript as well.
              const unheardReply = currentTurn.role === 'ASSISTANT'
                && !!(session?.silenced || session?.dropReplyText);

              if (
                currentTurn.role && currentTurn.isFinal && currentTurn.content.trim()
                && !skipForTranslatorMuted && !unheardReply
              ) {
                this.addToConversationHistory(currentTurn.role, currentTurn.content.trim());
                console.log(`💬 Saved ${currentTurn.role} turn to history: "${currentTurn.content.substring(0, 50)}..."`);
                if (currentTurn.role === 'ASSISTANT') {
                  // What the assistant actually said, for the transcript path
                  // when no ASR source is transcribing its voice. Not published
                  // while a translator is paused, like the history above.
                  publishAssistantTurn(currentTurn.content);
                }
                
                // GROUP MEETING MODE: Transcript-based wake phrase detection
                // This is the PRIMARY mechanism for unmuting in group meeting mode.
                // We watch Nova's FINAL USER transcripts for wake phrases and unmute/mute
                // entirely from our code - no tool calls needed (eliminates infinite loops).
                if (currentTurn.role === 'USER') {
                  this.lastUserTurnText = currentTurn.content.trim();
                  session.lastUserTurnEndAt = Date.now();
                  // A participant's new turn is not the dropped one.
                  session.dropTurn = false;
                  session.dropReplyText = false;
                  this.handleUserTurnWakePhrase(currentTurn.content);
                }
              }

              // An outgoing translator session is done once the final text of
              // its answer — what the meeting transcript is written from — is in.
              if (
                session.draining
                && currentTurn.role === 'ASSISTANT'
                && currentTurn.isFinal
                && endReason
                && endReason !== 'PARTIAL_TURN'
              ) {
                this.closeDrainingSession(session, 'answered');
              }
              
              // Reset current turn
              currentTurn = { role: null, content: '', isFinal: false };
            }
          } catch (e) {
            // Not JSON, might be raw data
            console.log('Non-JSON response:', textResponse.substring(0, 100));
          }
        } else if (event.modelStreamErrorException) {
          console.error('Model stream error:', event.modelStreamErrorException);
        } else if (event.internalServerException) {
          console.error('Internal server error:', event.internalServerException);
        }
      }

      console.log('Response stream processing complete');
    } catch (error) {
      console.error('Error processing response stream:', error);
    }
  }

  /**
   * React to a wake phrase in a FINAL user transcript turn.
   *
   * In group meeting mode this is the PRIMARY unmute trigger: the agent starts
   * muted and is made audible entirely from our code, with no tool call (which
   * is what eliminates the infinite loops the tool-driven version had). In
   * normal mode it is how the agent becomes audible again after being asked to
   * stop talking, which is the only route available in always_active mode
   * because activate() returns early there.
   *
   * In both modes the utterance that asked for silence must not be the thing
   * that undoes it: "Hey Alex, stop talking" contains a wake phrase, and the
   * mute it triggers can land either side of this turn.
   *
   * That protection rests on the stop being recognisable here by the same
   * strict matcher, so it holds for the phrasings the matcher covers. A stop
   * the model honours from the prompt alone — wording the matcher does not list
   * — is only safe while this turn is seen before the tool call it provokes. If
   * a live meeting ever shows that order flipping, the fix is to ignore
   * wake-phrase unmutes for a short window after the mute-tool route engages
   * the gate — muteDrainingPlayback in the conversational modes, mute() in
   * translator mode.
   */
  private handleUserTurnWakePhrase(content: string): void {
    if (!this.containsWakePhrase(content)) {
      return;
    }
    this.lastAddressedAt = Date.now();

    // Being addressed again re-arms an activation window that is running. The
    // scribe does not activate an assistant that is already activated, so for
    // a follow-up inside the window this is the only place it is noticed.
    if (this.activationMode !== 'always_active' && this._isActivated) {
      console.log('🔁 Activation window re-armed (addressed again)');
      this.noteEngagement();
    }

    const askedToStop = isStopTalkingRequest(content, configuredWakePhrases());

    // Note what this deliberately does NOT do: call off a stop request that is
    // still resolving. A wake phrase here means the assistant is wanted, and
    // activate() acts on exactly that — but `content` is Nova's own transcript
    // of speech Amazon Transcribe has already read, and two independent
    // recognisers need not agree on whether an utterance is strictly a stop
    // command. "Hey Alex, stop talking about that" carries a wake phrase and is
    // not a stop command, so a single misheard word would otherwise disarm the
    // fallback for the very request that armed it, leaving a participant who
    // asked for quiet with an assistant still talking.
    //
    // Both branches below do call it off, but only once the assistant is
    // already silent — the state in which a wake phrase is far more likely to
    // mean "come back" than to be a second reading of the request, and the same
    // condition that already governs making it audible again. Without that, a
    // fallback or backstop still armed would silence the assistant again a
    // moment after it was asked back.
    if (this.groupMeetingMode) {
      if (this._isMuted && !askedToStop) {
        console.log('🔓 Wake phrase detected in user speech - proactively unmuting agent');
        // Group mode clears the gate directly rather than through unmute(),
        // because unmute() marks the conversation active and would suppress the
        // session started just below — so the request in flight has to be
        // called off here by hand.
        this.cancelStopRequest('addressed-again');
        this.lastTranscriptStopAt = null;
        this._isMuted = false;
        // Start conversation session if not already active
        if (!this.conversationActive) {
          this.startConversationSession();
        }
      }
      this.resetConversationTimeout();
      return;
    }

    if (this.meetingMode === 'normal' && this._isMuted && !askedToStop) {
      this.unmute('wake phrase in user speech');
    }
    // No conversation timeout to re-arm: the auto-mute timer belongs to group
    // mode, which returned above, and is the only mode that goes back to muted
    // on its own.
  }

  /**
   * Why a `strands_agent` lookup requested now must not be run, or null if it
   * may. See muted-lookup.ts: a muted assistant that was not addressed by the
   * turn it is responding to was not asked for anything, and its answer would
   * be dropped by the mute anyway.
   */
  private strandsLookupSkipReason(): LookupSkipReason | null {
    return mutedLookupSkipReason(this._isMuted, this.lastUserTurnText);
  }

  /**
   * Answer a lookup that is not being run, without invoking the Strands Lambda.
   * The model is still owed a result for its tool use, and the result tells it
   * why, so that it stays silent rather than retrying.
   */
  private async skipStrandsLookup(toolUseId: string, reason: LookupSkipReason): Promise<void> {
    console.log(skippedLookupLogLine(reason, this.meetingMode, this.activationMode));
    if (!this.session || !this.session.isActive) {
      return;
    }
    try {
      await this.sendToolResult(toolUseId, SKIPPED_LOOKUP_RESULT);
    } catch (err) {
      console.warn('Failed to send the skipped-lookup tool result (non-fatal):', err);
    }
  }

  private async processToolUseAsync(toolUseId: string, toolName: string, toolUseContent: any): Promise<void> {
    // The session that issued this tool use. A Strands call takes 5-10 seconds,
    // which is long enough for the session to be replaced underneath it: by the
    // automatic refresh that recovers from the stream closing or erroring, and
    // by a mid-meeting mode change. (Not by the periodic refresh, which waits
    // for `_isProcessingTool` to clear, nor by a translator pause/resume
    // transition, which runs in the one mode that registers no such tool.)
    //
    // A `toolUseId` means nothing to a session that never issued it, and after a
    // switch into translator mode the new session has no such tool at all, so
    // the result goes to the session that asked for it or to nothing.
    const issuingSession = this.session;
    const sessionStillOurs = () => this.session === issuingSession && !!this.session?.isActive;

    try {
      // Set flag to prevent deactivation while tool is processing
      this.toolCallsInFlight++;
      this._isProcessingTool = true;
      console.log(`🔧 Starting async tool processing: ${toolName}`);
      
      // Clear conversation timeout during tool execution to prevent premature auto-mute
      // The timeout will be restarted after tool completes
      this.clearConversationTimeout();
      
      // Invoke Strands Lambda
      const toolResult = await this.invokeStrandsAgent(toolUseContent);
      // Whatever the model says from here on is the answer, not a preamble.
      this.noteToolResultReady(toolUseId);
      
      // Send tool result back to Nova immediately (only to the session that
      // issued the tool use, and only while it is still active)
      if (sessionStillOurs()) {
        await this.sendToolResult(toolUseId, toolResult);
        console.log('✓ Tool result sent to Nova - Nova will now process and generate audio response');
      } else {
        console.log('⚠️  Session replaced or closed before tool result could be sent - discarding it');
      }
    } catch (error) {
      console.error('Error processing tool use:', error);
      this.noteToolResultReady(toolUseId);
      // Send error result back to Nova, under the same condition
      if (sessionStillOurs()) {
        await this.sendToolResult(toolUseId, {
          error: 'Failed to process tool request',
          details: error instanceof Error ? error.message : String(error)
        });
      }
    } finally {
      // Clear flag after tool processing completes
      // The _isSpeaking flag will keep the session open while Nova generates and plays audio
      setTimeout(() => {
        this.toolCallsInFlight = Math.max(0, this.toolCallsInFlight - 1);
        this._isProcessingTool = this.toolCallsInFlight > 0;
        // The activation window counts its idle time from here at the
        // earliest, so a lookup that took most of the window still leaves the
        // whole of it for the answer and whatever is asked next.
        this.noteEngagement();
        
        // Add 2-second padding before restarting conversation timeout
        // This gives Nova time to start speaking before the countdown begins
        setTimeout(() => {
          // Restart conversation timeout after tool completes (fresh 30 seconds)
          // This ensures Nova has time to deliver the tool result
          if (this.groupMeetingMode && this.conversationActive) {
            console.log('🔄 Restarting conversation timeout after tool completion (with 2s padding)');
            this.startConversationSession();
          }
        }, 2000);
        
        // Check if there's a queued refresh waiting for agent to be idle
        if (this.refreshQueued) {
          this.checkAndExecuteQueuedRefresh();
        }
      }, 2000);
      console.log(`✓ Async tool processing complete: ${toolName}`);
    }
  }

  private async invokeStrandsAgent(toolUseContent: any): Promise<any> {
    if (!this.lambdaClient || !this.strandsLambdaArn) {
      throw new Error('Strands Lambda not configured');
    }

    console.log('Invoking Strands agent Lambda directly...');
    
    // Parse the tool use content to get the query
    const contentObject = JSON.parse(toolUseContent.content);
    const query = contentObject.query;
    
    console.log(`Query for Strands agent: ${query}`);

    // Build payload for direct Strands agent invocation
    // The Strands agent Lambda expects: text, call_id, conversation_history, etc.
    const callId = process.env.VP_CALL_ID || process.env.MEETING_NAME || 'nova-voice-agent';
    const username = process.env.LMA_USER || 'NovaAgent';
    
    const payload = {
      text: query,  // Strands agent expects 'text' field
      call_id: callId,
      conversation_history: [],
      userEmail: username,
      dynamodb_table_name: process.env.DYNAMODB_TABLE_NAME || '',  // Use main CallEventTable, not VPTaskRegistry
      dynamodb_pk: `c#${callId}`,  // Use callId for dynamodb_pk (matches database schema)
    };

    console.log('Strands agent payload:', JSON.stringify(payload));

    // Invoke the Strands Lambda function with RequestResponse for synchronous result
    const command = new InvokeCommand({
      FunctionName: this.strandsLambdaArn,
      InvocationType: 'RequestResponse',
      Payload: JSON.stringify(payload),
    });

    const response = await this.lambdaClient.send(command);
    
    // Parse the Lambda response
    const responsePayload = JSON.parse(new TextDecoder().decode(response.Payload));
    
    console.log('Strands agent response received:', JSON.stringify(responsePayload).substring(0, 200));
    
    // Extract the message from the response and ensure it's a string. Only an
    // answer the agent wrote is reduced to spoken text below; a response
    // without one is passed on as JSON, unchanged.
    const message = responsePayload.message;
    if (!message) {
      return JSON.stringify(responsePayload);
    }
    if (typeof message !== 'string') {
      return JSON.stringify(message);
    }

    // The answer is written for the meeting chat, in markdown. Nova Sonic is
    // handed it to speak, and given a markdown answer late in a session it
    // can end its turn after the first line: replaying a live meeting's four
    // lookups, the fourth answer stopped after "here's the 5-day forecast for
    // Herndon, VA:" in 6 runs of 6, and was spoken in full in 6 of 6 once
    // reduced to plain sentences.
    const spoken = toSpokenText(message);
    if (spoken !== message) {
      console.log(`Strands answer reduced to spoken text (${message.length} -> ${spoken.length} chars)`);
    }
    return spoken;
  }

  private async sendToolResult(toolUseId: string, result: any): Promise<void> {
    if (!this.session || !this.session.isActive) {
      throw new Error('No active session to send tool result');
    }

    console.log(`Sending tool result for tool use ID: ${toolUseId}`);
    const contentId = randomUUID();

    // Tool content start
    this.addEventToQueue({
      event: {
        contentStart: {
          promptName: this.session.promptName,
          contentName: contentId,
          interactive: false,
          type: 'TOOL',
          role: 'TOOL',
          toolResultInputConfiguration: {
            toolUseId: toolUseId,
            type: 'TEXT',
            textInputConfiguration: {
              mediaType: 'text/plain'
            }
          }
        }
      }
    });

    // Tool content input - wrap result in JSON format as required by Nova
    // Nova expects tool results to be JSON, not plain strings
    let resultContent: string;
    if (typeof result === 'string') {
      // Wrap string result in JSON object
      resultContent = JSON.stringify({ result: result });
    } else {
      resultContent = JSON.stringify(result);
    }
    
    this.addEventToQueue({
      event: {
        toolResult: {
          promptName: this.session.promptName,
          contentName: contentId,
          content: resultContent
        }
      }
    });

    // Tool content end
    this.addEventToQueue({
      event: {
        contentEnd: {
          promptName: this.session.promptName,
          contentName: contentId
        }
      }
    });

    console.log('Tool result sent to Nova');
  }

  private async handleMuteTool(toolUseId: string, issuingSession: SessionData | null = this.session): Promise<void> {
    // Nova's translator calls its `mute` tool for "stop translating" as well as
    // for the pause phrase. A participant who asked to stop translating has
    // asked to leave translator mode, and pausing instead leaves a translator
    // that only the resume phrase brings back.
    if (issuingSession?.silenced) {
      // The session already acted on as "stop translating".
      console.log('[voice-mode] action=mute-tool-ignored why=stop-translating');
      return;
    }
    if (
      this.meetingMode === 'translator'
      && (this.leavingTranslator || Date.now() - this.leaveTranslatorRequestedAt < LEAVE_TRANSLATOR_MUTE_WINDOW_MS)
    ) {
      console.log('[voice-mode] action=mute-tool-taken-as-stop-translating');
      this.handleStopTranslating(issuingSession, 'tool');
      return;
    }
    console.log('🔇 Mute tool called - muting agent');
    // Only translator mode gets an audible cue, sounded before the audio gate
    // engages so the beep itself isn't dropped. That mode replaces the base
    // system prompt and so carries no STOP REQUESTS block, leaving the tone the
    // room's only sign that the pause took effect. The conversational modes
    // answer "Okay" out loud instead, and the transcript route in scribe.ts
    // silences the assistant without a tone, so sounding one here would make
    // one request land two ways depending on which route reached the gate
    // first.
    if (this.meetingMode === 'translator') {
      this.playConfirmationTone('mute');
    }
    // Everything below reads the meeting mode, so note which one this call was
    // made in before awaiting.
    const generation = this.meetingModeGeneration;
    const calledInMode = this.meetingMode;
    // Wait for trailing audio chunks of the tone, or of the spoken "Okay" the
    // conversational prompt asks for, before the gate engages — events for a
    // turn can still be arriving when its tool call is dispatched.
    await new Promise(resolve => setTimeout(resolve, this.MUTE_TOOL_AUDIO_FLUSH_MS));

    // A participant changed the meeting mode during that wait. The tool call
    // belonged to the session the change has torn down, and the change has
    // already decided whether the assistant is audible — so finishing here
    // would act on the wrong mode: gating an assistant that was just made
    // audible, or, called from `normal` and resumed in translator, pausing a
    // translator nobody asked to pause. Its tool result would also go to a
    // session that never issued the tool use.
    if (this.meetingModeGeneration !== generation) {
      console.log(
        `[stop-request] route=tool action=superseded-by-mode-change`
        + ` from=${calledInMode} to=${this.meetingMode}`,
      );
      return;
    }
    // "Stop translating" arrived on the participants' transcript during that
    // wait, and leaving translator mode is under way.
    if (this.meetingMode === 'translator' && this.leavingTranslator) {
      console.log('[voice-mode] action=mute-tool-superseded why=stop-translating');
      return;
    }

    if (hasStopTalkingTool(this.meetingMode)) {
      // playback_age_ms is the diagnostic that matters most here: a pacat only
      // milliseconds old was spawned for this acknowledgement, which means the
      // barge-in killed the previous one and the word is still in the buffer
      // rather than in the room. Hence draining rather than discarding.
      //
      // Read through pacatProcess rather than from pacatSpawnedAt alone, so
      // that a stream which has gone away reports -1 instead of the age of a
      // process that no longer exists. Conditioning on the stream covers every
      // teardown path at once, where clearing the timestamp would have to be
      // kept in step with each of them.
      const playbackAgeMs = this.pacatProcess && this.pacatSpawnedAt
        ? Date.now() - this.pacatSpawnedAt
        : -1;
      // `gate=` says which of the two outcomes this line records: engaging the
      // gate behind the acknowledgement, or finding it already shut because the
      // fallback got there first — in which case nothing was drained and the
      // word, if it was generated at all, was dropped.
      console.log(
        `[stop-request] route=tool action=acknowledged`
        + ` gate=${this._isMuted ? 'already-shut' : 'draining'}`
        + ` flush_ms=${this.MUTE_TOOL_AUDIO_FLUSH_MS}`
        + ` playback_age_ms=${playbackAgeMs} interrupted=${this._isInterrupted}`
        + ` speaking=${this._isSpeaking} mode=${this.meetingMode}`,
      );
      this.muteDrainingPlayback('tool');
    } else {
      // Translator mode: the tone has flushed and the pause should be absolute.
      this.mute('tool');
    }

    // Acknowledge the tool result on the current session before refreshing —
    // provided it is still the session that issued the tool use. An
    // activation-only change can replace the session during the flush above
    // without changing the meeting mode, and the mute still stands then, but
    // its toolUseId means nothing to the replacement.
    // A translator session that has already handed over gets no result: the
    // refresh below closes it anyway.
    if (this.session && this.session.isActive && this.session === issuingSession) {
      const muteInstruction = (this.meetingMode === 'translator')
        ? 'You are now paused. The session is being refreshed.'
        // Muted, the model still hears the meeting and keeps answering it; the
        // answers are dropped, but a lookup it starts is not (muted-lookup.ts).
        : 'You are now muted. Stay silent and do not look anything up until someone '
          + 'addresses you by name again.';
      try {
        await this.sendToolResult(toolUseId, {
          status: 'muted',
          state: 'MUTED',
          instruction: muteInstruction,
        });
      } catch (err) {
        console.warn('Failed to send mute tool result (non-fatal):', err);
      }
      console.log('✓ Agent muted successfully');
    }

    // In translator mode, swap into the MUTED-state session (paused prompt,
    // only the `unmute` tool registered).
    if (this.meetingMode === 'translator') {
      this._translatorState = 'MUTED';
      console.log('🔄 Translator state -> MUTED, refreshing session...');
      if (this.participantTranscriptSeen) {
        console.log('[translator] action=paused audio=withheld resume=transcript');
      }
      this.refreshSession().catch(error => {
        console.error('Error refreshing session for translator MUTE transition:', error);
      });
    }
  }

  private async handleUnmuteTool(toolUseId: string, issuingSession: SessionData | null = this.session): Promise<void> {
    if (this.meetingMode === 'translator' && this._translatorState === 'UNMUTED') {
      // Resumed already, from the participants' transcript.
      console.log('[translator] action=unmute-tool-ignored why=already-resumed');
      return;
    }
    console.log('🔓 Unmute tool called - unmuting agent');
    this.unmute('tool');
    // Audio gate is now off; play the unmute confirmation tone directly.
    this.playConfirmationTone('unmute');

    if (this.session && this.session.isActive && this.session === issuingSession) {
      const unmuteInstruction = (this.meetingMode === 'translator')
        ? 'You are now resuming. The session is being refreshed.'
        : 'You are now unmuted.';
      try {
        await this.sendToolResult(toolUseId, {
          status: 'unmuted',
          state: 'UNMUTED',
          instruction: unmuteInstruction,
        });
      } catch (err) {
        console.warn('Failed to send unmute tool result (non-fatal):', err);
      }
      console.log('✓ Agent unmuted successfully');
    }

    // In translator mode, swap into the UNMUTED-state session (translator
    // prompt, only the `mute` tool registered).
    if (this.meetingMode === 'translator') {
      this._translatorState = 'UNMUTED';
      console.log('🔄 Translator state -> UNMUTED, refreshing session...');
      this.refreshSession().catch(error => {
        console.error('Error refreshing session for translator UNMUTE transition:', error);
      });
    }
  }

  sendAudioChunk(chunk: Buffer): void {
    // Check activation mode - don't send if not activated
    if (this.activationMode !== 'always_active' && !this._isActivated) {
      return;
    }
    
    // NOTE: We DO send audio even when agent is speaking to enable barge-in
    // Audio feedback is prevented by the audio routing configuration:
    // - Agent audio goes to agent_output sink
    // - Meeting audio comes from 'default' source
    // - These are separate audio paths, preventing feedback loops
    
    // Don't send audio during session refresh
    if (this.isRefreshing) {
      return;
    }

    // A paused translator is not sent the meeting while the participants'
    // transcript is there to resume it from. Sent the audio, it answers what
    // it hears ("I'm paused, say translator unmute") with replies the mute
    // gate drops, and it has resumed itself on speech that only mentioned the
    // translator.
    if (this.meetingMode === 'translator' && this._translatorState === 'MUTED' && this.participantTranscriptSeen) {
      return;
    }
    
    if (!this._isActive || !this.session || !this.session.isActive) {
      return;
    }

    this.audioChunkCount++;
    if (this.audioChunkCount % 100 === 0) {
      console.log(`📡 Sent ${this.audioChunkCount} audio chunks to Nova (${chunk.length} bytes each)`);
    }

    // Send audio chunk to Nova Sonic 2 (even when speaking, to enable barge-in)
    this.queueAudio(this.session, chunk);

    if (this.meetingMode === 'translator') {
      // Kept for the next translator session (translator-turns.ts), and an
      // outgoing session hears silence in its place, so a new speaker does not
      // cut its answer off.
      this.recentMeetingAudio.push(chunk);
      if (this.drainingSessions.size > 0) {
        const silence = Buffer.alloc(chunk.length);
        for (const outgoing of this.drainingSessions) {
          this.queueAudio(outgoing, silence);
        }
      }
    }
  }

  sendUserMessage(text: string): void {
    if (!this._isActive || !this.session || !this.session.isActive) {
      console.error('Cannot send message: Session not active');
      return;
    }

    console.log('📤 Sending text message to Nova:', text);
    
    // Track user message in conversation history
    this.addToConversationHistory('USER', text);
    
    // Create a text content block
    const textContentId = randomUUID();
    
    this.addEventToQueue({
      event: {
        contentStart: {
          promptName: this.session.promptName,
          contentName: textContentId,
          type: 'TEXT',
          interactive: true,
          role: 'USER',
          textInputConfiguration: {
            mediaType: 'text/plain',
          },
        },
      },
    });

    this.addEventToQueue({
      event: {
        textInput: {
          promptName: this.session.promptName,
          contentName: textContentId,
          content: text,
        },
      },
    });

    this.addEventToQueue({
      event: {
        contentEnd: {
          promptName: this.session.promptName,
          contentName: textContentId,
        },
      },
    });
  }

  /** Generate a sine-wave PCM tone (16-bit signed LE, mono) at the given
   *  sample rate, with a short attack/release envelope to prevent clicks.
   *  Returns a Buffer ready to write to the pacat playback stream. */
  private generateTonePcm(frequencyHz: number, durationMs: number, sampleRate: number, amplitude = 0.25): Buffer {
    const totalSamples = Math.floor((durationMs / 1000) * sampleRate);
    const buf = Buffer.alloc(totalSamples * 2);
    const fadeSamples = Math.min(Math.floor(0.01 * sampleRate), Math.floor(totalSamples / 4)); // 10ms or 1/4 of the tone, whichever is smaller
    const twoPiF = 2 * Math.PI * frequencyHz;
    for (let i = 0; i < totalSamples; i++) {
      let envelope = 1;
      if (i < fadeSamples) envelope = i / fadeSamples;
      else if (i > totalSamples - fadeSamples) envelope = (totalSamples - i) / fadeSamples;
      const sample = Math.sin(twoPiF * (i / sampleRate)) * amplitude * envelope;
      const intSample = Math.max(-32768, Math.min(32767, Math.round(sample * 32767)));
      buf.writeInt16LE(intSample, i * 2);
    }
    return buf;
  }

  /** Plays a confirmation tone directly to the meeting audio sink, bypassing
   *  Nova entirely.
   *
   *  Translator mode's pause and resume cues are the only callers: that mode
   *  replaces the base system prompt, so it has no spoken acknowledgement and
   *  the tone is the room's only confirmation. The conversational modes say
   *  "Okay" out loud instead, and `unmute` is registered as a tool only in
   *  translator mode, so neither tone reaches them. */
  private playConfirmationTone(kind: 'mute' | 'unmute'): void {
    const sampleRate = parseInt(process.env.NOVA_PLAYBACK_RATE || '16000');
    let pcm: Buffer;
    if (kind === 'mute') {
      // Single low descending-feeling tone.
      pcm = this.generateTonePcm(420, 220, sampleRate);
    } else {
      // Two-tone rising chirp.
      const a = this.generateTonePcm(620, 130, sampleRate);
      const gap = Buffer.alloc(Math.floor(0.03 * sampleRate) * 2); // 30ms gap
      const b = this.generateTonePcm(880, 150, sampleRate);
      pcm = Buffer.concat([a, gap, b]);
    }
    if (this.writePlayback(pcm, false)) {
      console.log(`🔔 Played ${kind} confirmation tone (${pcm.length} bytes)`);
    } else {
      console.warn(`⚠️  Could not play ${kind} confirmation tone — pacat stream unavailable`);
    }
  }

  private ensurePacatStream(): ChildProcess | null {
    // An assistant that is about to be heard has nothing left to say from
    // before. Two streams on `agent_output` at once would be audible as garble,
    // so a handed-off stream gives way here rather than playing underneath —
    // before the early return as well as before a spawn, since a stream that is
    // already live is just as capable of overlapping it.
    this.killDrainingPacatStream();

    if (this.pacatProcess && !this.pacatProcess.killed) {
      return this.pacatProcess;
    }

    const playbackRate = parseInt(process.env.NOVA_PLAYBACK_RATE || '16000');
    // 20ms was too tight to survive scheduling jitter: on a 2-vCPU MicroVM,
    // Chromium + the avatar's 1080p rescale + video recording + Transcribe
    // together stall the writer long enough to underrun the buffer, which is
    // audible as crackling (GitHub #543). 80ms gives 4x the headroom and is
    // still far below conversational turn-taking latency, so the assistant does
    // not feel slower to reply.
    const playbackLatencyMs = parseInt(process.env.NOVA_PLAYBACK_LATENCY_MS || '80');

    console.log(
      `🔊 Starting persistent pacat playback stream (${playbackRate}Hz, ${playbackLatencyMs}ms buffer)...`,
    );
    const spawned = spawn('pacat', [
      '--playback',
      '--device=agent_output',
      '--format=s16le',
      '--rate=' + playbackRate,
      '--channels=1',
      '--latency-msec=' + playbackLatencyMs,
    ]);
    this.pacatSpawnedAt = Date.now();
    this.pacatProcess = spawned;

    // Every handler reports the stream it was given, never "whatever is current
    // now" — see forgetPacatStream.
    spawned.stdin?.on('error', (error: any) => {
      if (error.code === 'EPIPE' || error.code === 'ERR_STREAM_DESTROYED') {
        console.log('🔊 pacat stdin closed (expected during cleanup)');
      } else {
        console.error('❌ pacat stdin error:', error);
      }
      this.forgetPacatStream(spawned);
    });

    spawned.on('close', (code: number) => {
      console.log(`🔊 pacat process exited with code ${code}`);
      this.forgetPacatStream(spawned);
    });

    spawned.on('error', (error: Error) => {
      console.error('❌ pacat process error:', error);
      this.forgetPacatStream(spawned);
    });

    spawned.stderr?.on('data', (data: Buffer) => {
      const msg = data.toString().trim();
      if (msg) console.log(`pacat: ${msg}`);
    });

    console.log('✓ Persistent pacat playback stream started');
    return spawned;
  }

  /**
   * Stop the persistent pacat process (called during cleanup/session close).
   *
   * `immediate` discards whatever PulseAudio still has buffered, which is what
   * barge-in, an ordinary mute and shutdown all want — including any stream
   * that an earlier `drain` handed off, so that barge-in can always cut off
   * everything the meeting can hear. `drain` closes the input and lets the
   * buffer play out, bounded by PLAYBACK_DRAIN_CAP_MS; only the stop-request
   * acknowledgement uses it (see muteDrainingPlayback).
   */
  private stopPacatStream(mode: 'immediate' | 'drain' = 'immediate'): void {
    if (mode === 'immediate') {
      this.killDrainingPacatStream();
      // Nothing written so far will be heard.
      this.playbackEndsAt = 0;
      this.toolAckPlayingUntil = 0;
    } else {
      // At most the drain cap of it will be.
      const cap = Date.now() + this.PLAYBACK_DRAIN_CAP_MS;
      this.playbackEndsAt = Math.min(this.playbackEndsAt, cap);
      this.toolAckPlayingUntil = Math.min(this.toolAckPlayingUntil, cap);
    }

    const process_ = this.pacatProcess;
    if (process_ && !process_.killed) {
      try {
        process_.stdin?.end();
        if (mode === 'immediate') {
          process_.kill('SIGTERM');
        } else {
          // An earlier hand-off that has not finished gives way first: two
          // streams on `agent_output` at once would be audible as garble, and
          // only one can be held.
          this.killDrainingPacatStream();

          // Hand the process off: it exits on its own once the buffer empties,
          // and the cap stops one that never drains from outliving the request
          // that silenced it.
          this.drainingPacatProcess = process_;
          const cap = setTimeout(() => {
            // This hand-off's own stream and no other: a later one has its own
            // cap and must not be cut short by this timer.
            if (this.drainingPacatProcess === process_) {
              this.killDrainingPacatStream();
            }
          }, this.PLAYBACK_DRAIN_CAP_MS);
          cap.unref?.();
        }
      } catch (e) {
        // Ignore errors during cleanup
      }
      this.pacatProcess = null;
      console.log(`✓ Persistent pacat stream stopped (${mode})`);
    }
    // Clear speaking state. Note what _isSpeaking means after a `drain`: this
    // agent has stopped accepting speech, not that the meeting has stopped
    // hearing any — the handed-off stream can still be playing out the
    // acknowledgement. It is read to decide whether the agent is busy
    // (deactivation, session refresh, auto-mute), for which "no longer
    // producing audio" is the right question, so clearing it here is correct;
    // anything that needs "nothing is audible" has to ask about the streams.
    if (this.speakingTimeout) {
      clearTimeout(this.speakingTimeout);
      this.speakingTimeout = null;
    }
    this._isSpeaking = false;
  }

  /**
   * Drop the references to a playback stream that has gone away, and only to
   * that stream.
   *
   * A stream reporting its exit is not necessarily the one in use: a handed-off
   * stream (see stopPacatStream's `drain` mode) exits after a replacement may
   * already have been spawned, and clearing the replacement's reference would
   * orphan it — audible, because nothing left could stop it and the next audio
   * chunk would spawn a third alongside it.
   */
  private forgetPacatStream(spawned: ChildProcess): void {
    if (this.pacatProcess === spawned) {
      this.pacatProcess = null;
    }
    if (this.drainingPacatProcess === spawned) {
      this.drainingPacatProcess = null;
    }
  }

  /** End a handed-off playback stream, whether its own cap or a later teardown
   *  got there first. Idempotent. */
  private killDrainingPacatStream(): void {
    const draining = this.drainingPacatProcess;
    this.drainingPacatProcess = null;
    if (draining && !draining.killed) {
      try {
        draining.kill('SIGTERM');
      } catch (e) {
        // Ignore errors during cleanup
      }
      // Cutting a drain short discards audio the avatar holds its own copy of,
      // so the lips stop with the sound instead of finishing an utterance
      // nobody can hear — the same call mute() makes on the immediate path. A
      // drain that ran out on its own needs nothing: its audio was played in
      // full, and by then there is no live process to find here.
      if (simliAvatar.isConnected()) {
        simliAvatar.clearAudioBuffer().catch(() => {});
      }
    }
  }

  private async playAudio(audioBuffer: Buffer): Promise<void> {
    // Drop all audio after barge-in until Nova starts a new response turn.
    //
    // This gate is cleared by the contentStart that opens the next AUDIO block
    // (see processResponseStream), which arrives before that block's first
    // audioOutput — so an acknowledgement generated in a new turn after the
    // barge-in passes, and the gate is deliberately NOT relaxed for it.
    // Relaxing it would let the tail of the very utterance the participant
    // asked to stop through, which is the one thing barge-in exists to prevent.
    // noteStopAckDropped is how a live meeting tells us if that assumption is
    // wrong on the stop path.
    if (this._isInterrupted) {
      this.noteStopAckDropped('interrupted');
      return;
    }

    // A model preamble that arrived after the fallback acknowledgement already
    // said the same thing (see noteAssistantAudioBlockStarted).
    if (this.dropCurrentAudioBlock) {
      return;
    }

    // Counted whether or not the gate has shut yet, so the budget covers the
    // whole acknowledgement rather than only the part after the gate.
    const isStopAck = this.takeStopAckAudio(audioBuffer.length);

    // Mute gate: drops outgoing audio while the agent is muted (group-mode
    // auto-mute, mute-tool, or translator-mode pause) — except the bounded
    // acknowledgement of a stop request made through the mute tool.
    if (this._isMuted && !isStopAck) {
      this.noteStopAckDropped('muted');
      console.log('🔇 Audio dropped - agent is muted');
      return;
    }
    
    // Write audio directly to persistent pacat stream (non-blocking).
    //
    // Before the avatar, not after: ensurePacatStream makes way for this
    // utterance by ending a stream that was still draining, and ending one
    // clears the avatar's buffer. Sending this chunk first would hand the
    // avatar the opening of the new utterance and then discard it along with
    // the audio it was making way for.
    this.writePlayback(audioBuffer);
    this.markSpeaking();
  }

  /** Write PCM to the playback stream and the avatar, and advance the estimate
   *  of when the meeting will have heard it. Every gate is the caller's job. */
  private writePlayback(pcm: Buffer, toAvatar: boolean = true): boolean {
    const pacat = this.ensurePacatStream();
    const written = !!pacat?.stdin && !pacat.stdin.destroyed;
    if (written) {
      pacat!.stdin!.write(pcm);
      const now = Date.now();
      this.playbackEndsAt = Math.max(now, this.playbackEndsAt) + pcm.length / this.playbackBytesPerMs();
    }

    // Forward audio to Simli avatar for lip-sync animation
    // This runs in parallel with PulseAudio playback - Simli only uses it for video
    if (toAvatar && simliAvatar.isConnected()) {
      simliAvatar.sendAudioData(pcm).catch(err => {
        // Non-critical - avatar lip-sync failure shouldn't affect audio
      });
    }
    return written;
  }

  /** Audio is arriving: hold `_isSpeaking` until SPEAKING_TIMEOUT_MS after the
   *  last of it, then run whatever was queued behind the agent going idle. */
  private markSpeaking(): void {
    this._isSpeaking = true;
    if (this.speakingTimeout) {
      clearTimeout(this.speakingTimeout);
    }
    this.speakingTimeout = setTimeout(() => {
      this._isSpeaking = false;
      this.speakingTimeout = null;
      console.log('✅ Assistant audio stopped arriving - speaking state cleared');
      
      // Check if there's a queued refresh waiting for agent to be idle
      if (this.refreshQueued) {
        this.checkAndExecuteQueuedRefresh();
      }
      
      // Check if there's a queued auto-mute waiting for agent to finish speaking
      if (this.autoMuteQueued) {
        this.checkAndExecuteQueuedAutoMute();
      }
    }, this.SPEAKING_TIMEOUT_MS);
  }

  private _preConnecting: boolean = false;
  private _preConnectPromise: Promise<void> | null = null;

  async preConnect(): Promise<void> {
    if (this.activationMode === 'always_active') return; // already connected
    if (this._isActive) return; // already has a session
    if (this._preConnecting) {
      if (this._preConnectPromise) await this._preConnectPromise;
      return;
    }

    console.log('🔌 Pre-connecting Nova session (wake phrase detected in partial)...');
    this._preConnecting = true;
    this._preConnectPromise = this.createSession()
      .then(() => {
        console.log('✓ Nova session pre-connected — ready for activation');
      })
      .catch((error) => {
        console.error('❌ Nova pre-connect failed:', error);
      })
      .finally(() => {
        this._preConnecting = false;
        this._preConnectPromise = null;
      });

    await this._preConnectPromise;
  }

  async activate(duration?: number, initialContext?: string): Promise<void> {
    if (this.activationMode === 'always_active') {
      // Already always active, no need to activate
      return;
    }

    const activationDuration = duration || this.defaultActivationDuration;
    console.log(`🎤 Nova agent activated for ${activationDuration} seconds`);
    // Activation is by wake phrase.
    this.lastAddressedAt = Date.now();
    
    // A wake phrase heard while an earlier one is still opening the session
    // does not open another: the activation already running finishes the job,
    // and asks the latest question it was given.
    if (this.activationOpening) {
      if (initialContext) this.activationContext = initialContext;
      console.log('⏳ Activation already opening its session - not opening another');
      return;
    }
    this.activationContext = initialContext;

    // Set before anything awaits, so that the wait for a pre-connect is
    // covered too. A pre-connected session may itself have been refused for
    // the quota, or not have heard back yet; either way it is settled before
    // activating.
    const opening = (async () => {
      // Create session if not already active (may already be pre-connected)
      if (!this._isActive && this._preConnecting && this._preConnectPromise) {
        console.log('⏳ Waiting for in-flight pre-connect to complete...');
        await this._preConnectPromise;
      }
      return this.openAssistantSession();
    })();
    this.activationOpening = opening;
    let opened: boolean;
    try {
      opened = await opening;
    } finally {
      if (this.activationOpening === opening) this.activationOpening = null;
    }
    initialContext = this.activationContext;
    this.activationContext = undefined;
    if (!opened || this.stopped) {
      return; // Don't activate if no session could be opened
    }

    this._isActivated = true;

    // An activation is a decision to listen, so a stop request still resolving
    // is called off rather than being allowed to silence the session that has
    // just begun.
    //
    // This is narrower than it looks, and deliberately not relied on anywhere:
    // the scribe only activates an assistant that is not already activated, and
    // only asks an activated one to stop, so a request in flight is normally
    // resolved by one route or the other and not by this. What is left is a
    // request that outlives its own activation — the activation lapsing and a
    // wake phrase starting a new one inside the window — which this covers.
    //
    // Safe here, and not on Nova's own transcript turn, because this and the
    // stop route read the same Amazon Transcribe segments and the scribe sends
    // a segment down one path or the other, never both, so the utterance that
    // asked for silence cannot be the one that activates.
    this.cancelStopRequest('activated');
    // The assistant may not have been silenced here, in which case the
    // unmute() below returns early without clearing this. Clearing it either
    // way keeps a later tool call from being timed against a request that was
    // called off — the same reason the group-mode branch of
    // handleUserTurnWakePhrase clears it by hand.
    this.lastTranscriptStopAt = null;

    // Being addressed by wake phrase is how the agent becomes audible again
    // after it was asked to stop talking — otherwise a single stop request
    // would silence it for the rest of the meeting.
    //
    // Only in `normal` mode: `group` owns its own muted-until-addressed state
    // machine (and starts muted by design) and `translator` is driven by its
    // explicit pause/resume trigger phrases. Both of those run always_active,
    // where this method returns above.
    if (this.meetingMode === 'normal' && this._isMuted) {
      this.unmute('wake phrase activation');
    }

    // Handle initial context by sending as text message
    if (initialContext) {
      const question = this.extractQuestion(initialContext);
      if (question && question.length > 5) {
        console.log('📝 Sending initial question to Nova:', question);
        this.sendUserMessage(question);
      }
    }

    // The window is an idle timeout, not a deadline: see checkActivationWindow.
    this.activationWindowMs = activationDuration * 1000;
    this.activationHoldReason = null;
    this.noteEngagement();
    this.scheduleActivationCheck(this.activationWindowMs);
  }

  /**
   * Make sure a wake-phrase activation has a session whose stream Amazon
   * Bedrock accepted, retrying with backoff while it is refused for Nova
   * Sonic's concurrency quota.
   *
   * Resolves false only when every try was refused. In that case the
   * assistant says nothing and is not activated — the participant hears no
   * answer rather than waiting on an assistant that will never reply — and the
   * next wake phrase tries again. Otherwise it resolves true, including when
   * Bedrock has not answered within ASSISTANT_OPEN_WAIT_MS or the stream failed
   * for some other reason, both of which are handled as they were before.
   */
  private async openAssistantSession(): Promise<boolean> {
    for (let attempt = 0; attempt < ASSISTANT_OPEN_ATTEMPTS; attempt++) {
      if (this.stopped) return false;
      if (!this._isActive) {
        console.log('🔌 Creating Nova session...');
        try {
          await this.createSession();
          console.log('✓ Session created');
        } catch (error) {
          console.error('❌ Failed to create session:', error);
          return false;
        }
      } else if (attempt === 0) {
        console.log('✓ Session already active (pre-connected)');
      }
      const session = this.session as SessionData | null;
      // Nothing to wait on: as before, an active agent or a created session
      // is taken as ready.
      if (!session) return true;
      const outcome = await this.waitForSessionOpen(session);
      // Any other failure is not the quota and is not retried here: the
      // activation goes ahead as it always has, and the stream's own error
      // handling deals with it.
      if (outcome !== 'throttled') return true;
      this.discardSession(session);
      if (attempt + 1 >= ASSISTANT_OPEN_ATTEMPTS) break;
      const delay = throttleBackoffMs(attempt, this.assistantOpenBackoff);
      console.warn(
        `[nova-throttle] action=retry-activation attempt=${attempt + 1}/${ASSISTANT_OPEN_ATTEMPTS} retry_in_ms=${delay}`,
      );
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
    console.warn(
      `[nova-throttle] action=gave-up-activation attempts=${ASSISTANT_OPEN_ATTEMPTS}`
      + ' - the assistant stays silent for this wake phrase; the next one tries again',
    );
    return false;
  }

  /** How `session`'s stream open ended, or 'pending' if Bedrock has not
   *  answered within ASSISTANT_OPEN_WAIT_MS. */
  private async waitForSessionOpen(session: SessionData): Promise<SessionOpenOutcome | 'pending'> {
    if (!session.opened) return 'open';
    let timer: NodeJS.Timeout | null = null;
    const timeout = new Promise<'pending'>((resolve) => {
      timer = setTimeout(() => resolve('pending'), ASSISTANT_OPEN_WAIT_MS);
    });
    try {
      return await Promise.race([session.opened, timeout]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  /** Forget a session whose stream was never opened. */
  private discardSession(session: SessionData): void {
    session.isActive = false;
    session.signal?.();
    if (this.session === session) {
      this.session = null;
      this.sessionId = null;
      this._isActive = false;
      this.streamingActive = false;
    }
  }

  private scheduleActivationCheck(delayMs: number): void {
    if (this.activationTimeout) {
      clearTimeout(this.activationTimeout);
    }
    this.activationTimeout = setTimeout(() => {
      this.activationTimeout = null;
      this.checkActivationWindow();
    }, Math.max(0, delayMs));
  }

  /** Restart the activation window's idle countdown. */
  private noteEngagement(): void {
    this.lastEngagementAt = Date.now();
  }

  /**
   * Why the activation window must not lapse yet, or null if nothing holds it.
   *
   * A session closed while the meeting is still hearing the assistant cannot
   * hear the participant who asks it to stop, and a session closed while a
   * lookup is running throws its answer away — so all of these hold it, however
   * long they take. A participant's turn holds it only briefly (see
   * ACTIVATION_USER_TURN_GRACE_MS), and only within twice the window, so that a
   * busy meeting cannot keep a wake-phrase session open indefinitely.
   */
  private activationHoldFor(now: number): string | null {
    if (this._isProcessingTool) return 'tool';
    if (this._isSpeaking) return 'audio-arriving';
    if (this.playbackEndsAt > now) return 'playback';
    // The detector covers what the estimate cannot see — PulseAudio's own
    // latency, a stream that took a moment to connect — so it is believed only
    // shortly past the estimate. A detector that never reports silence must
    // not keep a wake-phrase session open for the rest of the meeting.
    if (this.audibleOutput() && now - this.playbackEndsAt < ACTIVATION_AUDIBLE_HOLD_MAX_MS) return 'audible';
    const idleSince = Math.max(this.lastEngagementAt, this.playbackEndsAt);
    if (
      now - this.lastUserTextAt < ACTIVATION_USER_TURN_GRACE_MS
      && now - idleSince < 2 * this.activationWindowMs
    ) {
      return 'user-turn';
    }
    return null;
  }

  /**
   * Close the activation window once the assistant has been idle for the whole
   * of it.
   *
   * Idle time is counted from the latest of: the activation itself, being
   * addressed again, a barge-in, a lookup finishing, and the end of any stretch
   * in which the assistant was busy — so the configured duration is the quiet
   * time after the assistant last spoke, not a deadline from the wake phrase
   * that a slow lookup and a long answer could use up between them. Recorded
   * on a live meeting with a 30-second window: a wake phrase at 22:08:49, two
   * lookups, and an answer that started at 22:09:11 and was audible for 26
   * seconds, which the session closed 8 seconds into — leaving the participant
   * who then said "stop" talking to a session that could no longer hear it.
   */
  private checkActivationWindow(): void {
    if (this.activationMode === 'always_active' || !this._isActivated) {
      return;
    }
    const now = Date.now();
    const hold = this.activationHoldFor(now);
    if (hold) {
      if (hold !== this.activationHoldReason) {
        console.log(`⏳ Activation window held open (${hold})`);
      }
      this.activationHoldReason = hold;
      // A participant's turn is not engagement (see ACTIVATION_USER_TURN_GRACE_MS);
      // everything else is the assistant being busy, and the idle countdown
      // starts again when it stops.
      if (hold !== 'user-turn') {
        this.noteEngagement();
      }
      this.scheduleActivationCheck(ACTIVATION_RECHECK_MS);
      return;
    }
    this.activationHoldReason = null;
    const idleSince = Math.max(this.lastEngagementAt, this.playbackEndsAt);
    const remaining = this.activationWindowMs - (now - idleSince);
    if (remaining > 0) {
      this.scheduleActivationCheck(remaining);
      return;
    }
    console.log(`⏰ Activation window elapsed (idle ${now - idleSince} ms of ${this.activationWindowMs} ms)`);
    this.deactivate();
  }

  private extractQuestion(context: string): string {
    // Remove all punctuation first, then remove wake phrases
    let cleaned = context.toLowerCase()
      .replace(/[,.\?!;:]/g, ' ')  // Replace punctuation with spaces
      .replace(/\s+/g, ' ')         // Normalize multiple spaces
      .trim();
    
    // Remove wake phrases dynamically from environment
    for (const phrase of configuredWakePhrases()) {
      cleaned = cleaned.replace(phrase, '').trim();
    }
    
    // Clean up any remaining leading/trailing spaces
    cleaned = cleaned.trim();
    
    return cleaned;
  }

  deactivate(): void {
    if (this.activationMode === 'always_active') {
      // Can't deactivate always_active mode
      return;
    }

    console.log('🔇 Nova agent deactivated');
    this._isActivated = false;
    this.activationHoldReason = null;

    // Clear timeout
    if (this.activationTimeout) {
      clearTimeout(this.activationTimeout);
      this.activationTimeout = null;
    }

    // A closed session cannot hear a participant ask it to stop, so no
    // assistant audio is left playing after it. The window does not lapse
    // while audio is still to be heard, so this only acts on a direct
    // deactivate(); it is here so that the guarantee holds for every caller.
    const unplayedMs = Math.round(this.playbackEndsAt - Date.now());
    if (this.activationMode === 'wake_phrase' && unplayedMs > 0) {
      console.log(`🔇 Cutting ${unplayedMs} ms of unplayed assistant audio as the session closes`);
      try {
        this.stopPacatStream();
      } catch (err) {
        console.warn('stopPacatStream during deactivate() failed (non-fatal):', err);
      }
      if (simliAvatar.isConnected()) {
        simliAvatar.clearAudioBuffer().catch(() => {});
      }
    }
    this.cancelToolAck('deactivated');

    // In wake_phrase mode, close session to save costs
    if (this.activationMode === 'wake_phrase' && this.session) {
      console.log('💰 Closing session to save costs (wake_phrase mode)');
      this.closeSession();
    }
  }

  private async closeSession(): Promise<void> {
    if (!this.session) return;

    console.log('Closing Nova session...');
    this.closeDrainingSessions('closed');
    this.cancelToolAck('session-closed');
    this.dropCurrentAudioBlock = false;

    // Stop keep-alive timer
    this.stopKeepAliveTimer();

    // Clear session refresh timer
    if (this.sessionRefreshTimer) {
      clearTimeout(this.sessionRefreshTimer);
      this.sessionRefreshTimer = null;
      console.log('✓ Session refresh timer cleared');
    }
    
    // Clear conversation timeout
    this.clearConversationTimeout();

    try {
      // End audio content
      if (this.session.isAudioContentStartSent) {
        this.addEventToQueue({
          event: {
            contentEnd: {
              promptName: this.session.promptName,
              contentName: this.session.audioContentId,
            },
          },
        });
      }

      // End prompt
      if (this.session.isPromptStartSent) {
        this.addEventToQueue({
          event: {
            promptEnd: {
              promptName: this.session.promptName,
            },
          },
        });
      }

      // End session
      this.addEventToQueue({
        event: {
          sessionEnd: {},
        },
      });

      // Wait for events to be sent
      await new Promise(resolve => setTimeout(resolve, 500));

      // Mark session as inactive
      this.session.isActive = false;
      this.session.signal?.();

      this.session = null;
      this.sessionId = null;
      this._isActive = false;
      this.streamingActive = false;

      console.log('✓ Session closed');
    } catch (error) {
      console.error('Error closing session:', error);
      // Force cleanup
      this.session = null;
      this.sessionId = null;
      this._isActive = false;
      this.streamingActive = false;
    }
  }

  async stop(): Promise<void> {
    console.log('Stopping Nova agent...');

    // Before anything awaits, so a recovery timer firing during the teardown
    // below already sees it.
    this.stopped = true;
    if (this.sessionRecoveryTimer) {
      clearTimeout(this.sessionRecoveryTimer);
      this.sessionRecoveryTimer = null;
    }

    // Stop persistent audio playback stream
    this.stopPacatStream();

    // Stop keep-alive timer
    this.stopKeepAliveTimer();

    // Clear session refresh timer
    if (this.sessionRefreshTimer) {
      clearTimeout(this.sessionRefreshTimer);
      this.sessionRefreshTimer = null;
    }

    // Clear activation timeout
    if (this.activationTimeout) {
      clearTimeout(this.activationTimeout);
      this.activationTimeout = null;
    }
    
    // Clear conversation timeout
    this.clearConversationTimeout();

    // Nothing left to silence once the agent is going away.
    this.cancelStopRequest('agent-stopped');
    this.cancelToolAck('agent-stopped');

    // Close session if active
    if (this.session) {
      await this.closeSession();
    }

    if (this.usageFlushTimer) {
      clearInterval(this.usageFlushTimer);
      this.usageFlushTimer = null;
    }
    await this.usage?.flush();

    this._isActivated = false;
    console.log('✓ Nova agent stopped');
  }

  /**
   * The token accumulator for the current model, written every few minutes so a
   * long meeting's usage is on record before it ends, and once more on stop.
   */
  private meteredUsage(): NovaSonicUsage {
    if (!this.usage || this.usageModelId !== this.modelId) {
      // A model change mid-meeting: what the previous model used is written
      // under its own id before counting starts for the new one.
      void this.usage?.flush();
      this.usage = new NovaSonicUsage(
        this.modelId,
        this.novaSonicRegion !== stackRegion() ? this.novaSonicRegion : undefined,
      );
      this.usageModelId = this.modelId;
    }
    if (!this.usageFlushTimer) {
      this.usageFlushTimer = setInterval(() => {
        void this.usage?.flush();
      }, USAGE_FLUSH_INTERVAL_MS);
      this.usageFlushTimer.unref?.();
    }
    return this.usage;
  }

  /** Every meeting mode and activation mode can be switched to; see reconfigure. */
  supportsModeChanges(): boolean {
    return true;
  }

  isEnabled(): boolean {
    return true; // Provider is selected
  }

  isActive(): boolean {
    return this._isActive;
  }

  isActivated(): boolean {
    return this._isActivated;
  }

  isSpeaking(): boolean {
    return this._isSpeaking;
  }

  /**
   * Mute the agent — drops all outgoing audio until unmuted. Idempotent.
   * Flushes any buffered playback so the agent stops mid-utterance.
   *
   * Has a sibling: muteDrainingPlayback does the same thing but lets the
   * playback buffer empty first. Anything added here that is about entering the
   * muted state, rather than about cutting playback off, belongs there too.
   */
  mute(reason?: string): void {
    // An ordinary mute is absolute, so nothing more of an acknowledgement is
    // let through after it — including when the tool route had already shut
    // the gate and this call changes nothing else.
    const cutAck = this.clearStopAck(reason ? `muted:${reason.replace(/\s+/g, '-')}` : 'muted');
    if (this._isMuted) {
      if (cutAck) {
        // The gate was already shut by the tool route with the
        // acknowledgement's stream held live; cut that audio off too.
        try {
          this.stopPacatStream();
        } catch (err) {
          console.warn('stopPacatStream during mute() failed (non-fatal):', err);
        }
        if (simliAvatar.isConnected()) {
          simliAvatar.clearAudioBuffer().catch(() => {});
        }
      }
      return;
    }
    console.log(`🔇 Nova agent muted${reason ? ` (${reason})` : ''}`);
    this._isMuted = true;
    this.conversationActive = false;
    try {
      this.stopPacatStream();
    } catch (err) {
      console.warn('stopPacatStream during mute() failed (non-fatal):', err);
    }
    // The avatar buffers its own copy of the speech for lip-sync, so without
    // this it keeps mouthing the rest of an utterance nobody can hear. Same
    // call the barge-in path makes.
    if (simliAvatar.isConnected()) {
      simliAvatar.clearAudioBuffer().catch(() => {});
    }
  }

  /**
   * Engage the audio gate, but let the playback buffer empty first instead of
   * discarding it.
   *
   * mute() tears the stream down, which is right everywhere else: an ordinary
   * mute has to stop the assistant mid-word. Here the audio already buffered IS
   * the answer to the request — and the barge-in that precedes a stop request
   * has usually just killed the stream, so the acknowledgement is written to a
   * pacat spawned for it. A process that has not finished connecting to
   * PulseAudio has played nothing yet, and no fixed wait can tell the
   * difference, so waiting on the buffer rather than on the clock is what makes
   * the word audible. PLAYBACK_DRAIN_CAP_MS bounds it.
   *
   * The avatar's own buffer is deliberately left alone, unlike in mute(): the
   * audio is still going to play, so the lips should keep up with it.
   */
  private muteDrainingPlayback(reason: string): void {
    if (this._isMuted) {
      return;
    }
    // The acknowledgement is mid-block: keep its playback stream live so the
    // rest of the word is written to it, and drain it when the block ends
    // (finishStopAck). Handing it off now would mean the next chunk of the
    // same word spawning a new stream, which kills the one holding its start.
    const holdForAck = this.stopAck !== null && this.stopAck.blockOpenedAt !== null;
    console.log(
      `🔇 Nova agent muted (${reason}, ${holdForAck ? 'holding playback for the acknowledgement' : 'draining playback'})`,
    );
    this._isMuted = true;
    this.conversationActive = false;
    // Whatever was buffered has been let out, so audio dropped from here on is
    // not the acknowledgement going missing — see noteStopAckDropped.
    if (this.stopRequest) {
      this.stopRequest.gateShutByDrain = true;
    }
    if (holdForAck) {
      return;
    }
    try {
      this.stopPacatStream('drain');
    } catch (err) {
      console.warn('draining playback during mute failed (non-fatal):', err);
    }
  }

  /**
   * Unmute the agent — resumes outgoing audio. Idempotent.
   */
  unmute(reason?: string): void {
    if (!this._isMuted) {
      return;
    }
    console.log(`🔓 Nova agent unmuted${reason ? ` (${reason})` : ''}`);
    this._isMuted = false;
    this.conversationActive = true;

    // Having actually made the assistant audible again supersedes a stop
    // request that is still resolving. Without this, a fallback or backstop
    // armed moments ago would silence it again immediately.
    //
    // Below the guard, not above it: only a real transition means the assistant
    // is wanted, which is the condition handleUserTurnWakePhrase relies on and
    // the one that keeps this from being reachable while a request is merely
    // deferred and the assistant still audible. The utterance that asked for
    // silence therefore cannot call its own request off here — though a
    // misreading of it arriving after the gate has engaged can, which is the
    // trade handleUserTurnWakePhrase explains.
    this.cancelStopRequest(reason ? `unmuted:${reason.replace(/\s+/g, '-')}` : 'unmuted');

    // The stretch of silence that request caused is over, so a tool call from
    // here on belongs to something else and must not be timed against it.
    this.lastTranscriptStopAt = null;
  }

  isMuted(): boolean {
    return this._isMuted;
  }

  /**
   * Act on a participant asking the agent to stop talking (see
   * VoiceAssistantProvider.requestStopTalking).
   *
   * In `normal` and `group` mode this silences the agent; it becomes audible
   * again the next time it is addressed by wake phrase.
   *
   * Where Nova has a `mute` tool to call, the gate is not closed here and now:
   * it is deferred by STOP_REQUEST_TOOL_GRACE_MS so the model has room to answer
   * out loud first. If the tool call arrives in that window the tool route
   * finishes the job and this one stands down; if it does not, this one mutes
   * anyway. The fallback is unconditional — being asked for quiet and ignored is
   * a worse outcome than being answered without the courtesy word.
   */
  requestStopTalking(reason?: string): boolean {
    // Translator mode declines. Its pause/resume is a state machine keyed on
    // exact trigger phrases: muting from here would leave _translatorState
    // saying UNMUTED with only the `mute` tool registered, so the resume phrase
    // would have no tool to call and the translator would never speak again.
    if (this.meetingMode === 'translator') {
      console.log('🌐 Translator mode - stop requests are only taken from the configured pause phrase');
      this.logStopRequest('declined-translator');
      return false;
    }

    // The fallback acknowledgement is not something anyone should have to sit
    // through after asking for quiet, and must not start after it either.
    this.silenceToolAck('stop-request');

    if (this._isMuted) {
      // Already silent, most likely because the tool route got there first.
      this.logStopRequest('already-silent');
      return true;
    }

    // Deferring only buys something where there is a tool to wait for, because
    // the tool call is what produces the spoken acknowledgement. Translator —
    // the only mode without one — has already declined above.
    if (!hasStopTalkingTool(this.meetingMode)) {
      this.logStopRequest('muted-immediately');
      this.mute(reason || 'stop requested');
      return true;
    }

    if (this.stopRequest) {
      // Someone repeating themselves inside the window must not stack a second
      // timer or push the deadline out: the request already in flight is
      // guaranteed to silence the assistant. Which kind of request that is
      // matters to the account — nothing was deferred on this route if the
      // model asked to mute itself first and the transcript only caught up.
      this.logStopRequest(
        this.stopRequest.fromTranscript ? 'already-deferred' : 'already-claimed-by-tool',
      );
      return true;
    }

    const seenAt = Date.now();
    this.stopRequest = {
      seenAt,
      fromTranscript: true,
      toolSeen: false,
      ackDropLogged: false,
      gateShutByDrain: false,
      timer: null,
    };
    this.lastTranscriptStopAt = seenAt;
    this.logStopRequest('deferred');
    this.stopRequest.timer = setTimeout(
      () => this.resolveStopRequest(reason || 'stop requested'),
      STOP_REQUEST_TOOL_GRACE_MS,
    );
    return true;
  }

  /**
   * Record that Nova has asked to mute itself, and hand the request in flight
   * over to the tool route.
   *
   * Called when the tool-use event is seen, which is a step earlier than the
   * handler that acts on it — the earliest point at which the transcript route
   * can be told to stand down, and so the point that gives the acknowledgement
   * the most room.
   */
  private noteMuteToolRequested(): void {
    // Translator mode's `mute` is a different tool with different semantics and
    // no spoken acknowledgement; it has nothing to do with this race.
    if (!hasStopTalkingTool(this.meetingMode)) {
      return;
    }

    // Before the acknowledgement window opens: the clip's remaining audio
    // would otherwise be drained ahead of the model's "Okay".
    this.silenceToolAck('stop-request');

    const existing = this.stopRequest;

    // A tool call that misses the window is the measurement that matters most:
    // it says by how much STOP_REQUEST_TOOL_GRACE_MS was too short. The record
    // it would have claimed is gone by then — resolveStopRequest clears it — so
    // the time the transcript route recognised the request outlives it, and
    // the latency is reported from there with `late=true` to say which side of
    // the deadline it landed on.
    //
    // Only for as long as a tool call could still plausibly be answering that
    // request, though. A silenced assistant goes on hearing the meeting, and in
    // always_active mode it can stay silent for many minutes, so a `mute` call
    // long afterwards is about something else: timing it against the original
    // request would put a reading worth nothing into the one set of numbers the
    // window is to be tuned from.
    const now = Date.now();
    const lateFrom =
      existing === null
      && this.lastTranscriptStopAt !== null
      && now - this.lastTranscriptStopAt <= STOP_REQUEST_TOOL_BACKSTOP_MS * 2
        ? this.lastTranscriptStopAt
        : null;
    const against = existing?.fromTranscript ? existing.seenAt : lateFrom;
    const late = lateFrom !== null;
    const latency = against === null ? 'n/a' : `${now - against}`;

    console.log(
      `[stop-request] route=tool action=claimed transcript_deferred=${against !== null}`
      + ` tool_latency_ms=${latency} late=${late}`
      + ` speaking=${this._isSpeaking} interrupted=${this._isInterrupted}`
      + ` muted=${this._isMuted} mode=${this.meetingMode}`,
    );

    // A second `mute` call ends the acknowledgement of the first, the same way
    // calling the request off does: a stream held live for it behind the shut
    // gate is handed off to drain, and the ending is logged.
    if (this.clearStopAck('superseded-by-tool-call') && this._isMuted) {
      try {
        this.stopPacatStream('drain');
      } catch (err) {
        console.warn('draining acknowledgement playback failed (non-fatal):', err);
      }
    }

    // Only for an assistant that is still audible: if the transcript fallback
    // has already silenced it, this tool call is not the one silencing it, and
    // there is no acknowledgement of its to let through.
    this.stopAck = this._isMuted
      ? null
      : {
        toolAt: now,
        route: existing?.fromTranscript ? 'transcript' : 'tool',
        blockOpenedAt: null,
        bytes: 0,
        timer: null,
      };

    if (existing) {
      existing.toolSeen = true;
      if (existing.timer) {
        clearTimeout(existing.timer);
      }
      existing.timer = setTimeout(
        () => this.resolveStopRequest('stop requested (tool backstop)'),
        STOP_REQUEST_TOOL_BACKSTOP_MS,
      );
      return;
    }

    // No transcript route to stand down — the model noticed on its own. Still
    // worth recording, so that a dropped acknowledgement is reported and the
    // backstop covers a stream that closes before handleMuteTool runs.
    this.stopRequest = {
      seenAt: Date.now(),
      fromTranscript: false,
      toolSeen: true,
      ackDropLogged: false,
      gateShutByDrain: false,
      timer: setTimeout(
        () => this.resolveStopRequest('stop requested (tool backstop)'),
        STOP_REQUEST_TOOL_BACKSTOP_MS,
      ),
    };
  }

  /**
   * Close out the stop request in flight: mute if nothing else has, and report
   * which route got there.
   */
  private resolveStopRequest(reason: string): void {
    const request = this.stopRequest;
    if (!request) {
      return;
    }
    if (request.timer) {
      clearTimeout(request.timer);
    }
    this.stopRequest = null;

    const elapsedMs = Date.now() - request.seenAt;
    const route = request.fromTranscript ? 'transcript' : 'tool';
    if (this._isMuted) {
      // The other route already silenced the assistant — on the tool path that
      // means the meeting heard "Okay" and then nothing, which is the whole
      // point of the window.
      console.log(
        `[stop-request] route=${route} action=stood-down elapsed_ms=${elapsedMs}`
        + ` tool_seen=${request.toolSeen} interrupted=${this._isInterrupted}`,
      );
      return;
    }

    console.log(
      `[stop-request] route=${route} action=fired-fallback elapsed_ms=${elapsedMs}`
      + ` tool_seen=${request.toolSeen} interrupted=${this._isInterrupted}`
      + ` speaking=${this._isSpeaking}`,
    );
    this.mute(reason);
  }

  /** Drop a stop request that is still resolving. */
  private cancelStopRequest(why: string): void {
    // Whatever calls the request off — being asked back, a mode change, the
    // agent stopping — ends the acknowledgement's exemption with it. Behind a
    // shut gate, a stream held live for the acknowledgement is handed off to
    // drain, as finishStopAck would have done; nothing more is written to it.
    if (this.clearStopAck(why) && this._isMuted) {
      try {
        this.stopPacatStream('drain');
      } catch (err) {
        console.warn('draining acknowledgement playback failed (non-fatal):', err);
      }
    }
    const request = this.stopRequest;
    if (!request) {
      return;
    }
    if (request.timer) {
      clearTimeout(request.timer);
    }
    this.stopRequest = null;
    console.log(
      `[stop-request] route=${request.fromTranscript ? 'transcript' : 'tool'}`
      + ` action=cancelled why=${why}`
      + ` elapsed_ms=${Date.now() - request.seenAt} tool_seen=${request.toolSeen}`,
    );
  }

  // -------------------------------------------------------------------------
  // Acknowledging a lookup (`[tool-ack]`)
  // -------------------------------------------------------------------------
  //
  // A `strands_agent` lookup takes 5-10 seconds, and the prompt asks the model
  // to say one sentence before it calls the tool. On a live meeting the model
  // did so for each first question after a wake phrase and for none of the
  // follow-ups inside the same session, which left the room in silence for the
  // whole lookup. So the agent checks: if no assistant audio has started within
  // TOOL_ACK_FALLBACK_MS of the tool-use event, it plays a short clip rendered
  // in the same voice at startup (see tool-ack-clip.ts). Every decision writes
  // one `[tool-ack]` line: `source=model` or `source=fallback` with the latency
  // from the tool-use event, or `source=none` with the reason nothing played.
  //
  // The clip goes through the same playback stream as Nova's own speech, so
  // barge-in and every mute cut it off; it is not started while the assistant
  // is muted or a stop request is being resolved; and a stop request arriving
  // while it plays cuts it rather than letting it drain.

  /** A `strands_agent` tool-use event has been seen. */
  private noteStrandsToolRequested(toolUseId: string): void {
    const now = Date.now();
    this.cancelToolAck('superseded');
    const preambleAt = this.lastAssistantAudioStartAt;
    if (preambleAt > 0 && preambleAt >= now - TOOL_ACK_LOOKBACK_MS && preambleAt > this.lastUserTextAt) {
      console.log(`[tool-ack] source=model latency_ms=${preambleAt - now}`);
      return;
    }
    const pending: NonNullable<NovaAgent['pendingToolAck']> = {
      toolUseId,
      toolAt: now,
      session: this.session,
      timer: null,
      fallbackAt: null,
      resultSent: false,
    };
    pending.timer = setTimeout(() => this.fireToolAckFallback(pending), TOOL_ACK_FALLBACK_MS);
    this.pendingToolAck = pending;
  }

  /** An assistant AUDIO block has started. */
  private noteAssistantAudioBlockStarted(): void {
    const now = Date.now();
    this.lastAssistantAudioStartAt = now;
    // Each block decides for itself, so a dropped block whose end never
    // arrived cannot take the next one with it.
    this.dropCurrentAudioBlock = false;
    const pending = this.pendingToolAck;
    if (!pending) {
      return;
    }
    if (pending.fallbackAt === null) {
      console.log(`[tool-ack] source=model latency_ms=${now - pending.toolAt}`);
      this.clearToolAck();
      return;
    }
    if (!pending.resultSent && now - pending.toolAt < TOOL_ACK_LATE_PREAMBLE_MS) {
      // The model's own preamble, arriving after the clip already said it.
      this.dropCurrentAudioBlock = true;
      console.log(`[tool-ack] action=late-preamble-dropped latency_ms=${now - pending.toolAt}`);
      return;
    }
    // Past the window this is the answer, or a reply to something new.
    this.clearToolAck();
  }

  /** The lookup has finished, so what the model says next is the answer. */
  private noteToolResultReady(toolUseId: string): void {
    const pending = this.pendingToolAck;
    if (!pending || pending.toolUseId !== toolUseId) {
      return;
    }
    if (pending.fallbackAt === null) {
      console.log(`[tool-ack] source=none reason=result-first latency_ms=${Date.now() - pending.toolAt}`);
    }
    pending.resultSent = true;
    this.clearToolAck();
  }

  /** Why the fallback must not play now, or null if it may. */
  private toolAckSkipReason(pending: NonNullable<NovaAgent['pendingToolAck']>): string | null {
    if (this.session !== pending.session || !this.session?.isActive) return 'session-changed';
    if (this.activationMode !== 'always_active' && !this._isActivated) return 'not-activated';
    if (this._isMuted) return 'muted';
    if (this.stopRequest || this.stopAck) return 'stop-request';
    if (this.lastInterruptionAt > pending.toolAt) return 'interrupted';
    // Somebody has said something since the call, which the model is about to
    // answer; a clip now would collide with that answer.
    if (this.lastUserTextAt > pending.toolAt) return 'user-turn';
    // The room is not silent: the assistant is still saying something, and a
    // clip queued behind it would be heard late and out of place.
    if (this._isSpeaking) return 'speaking';
    if (this.playbackEndsAt > Date.now()) return 'playing';
    if (!this.toolAckClip) return 'no-clip';
    return null;
  }

  private fireToolAckFallback(pending: NonNullable<NovaAgent['pendingToolAck']>): void {
    if (this.pendingToolAck !== pending) {
      return;
    }
    pending.timer = null;
    const now = Date.now();
    const skip = this.toolAckSkipReason(pending);
    if (skip) {
      console.log(`[tool-ack] source=none reason=${skip} latency_ms=${now - pending.toolAt}`);
      this.clearToolAck();
      return;
    }
    const clip = this.toolAckClip as Buffer;
    // Deliberately not through playAudio's barge-in gate: an interruption that
    // predates the tool call has already cut off the utterance it interrupted,
    // and the gate only stays shut because Nova has not opened a block since.
    if (!this.writePlayback(clip)) {
      console.log(`[tool-ack] source=none reason=no-playback latency_ms=${now - pending.toolAt}`);
      this.clearToolAck();
      return;
    }
    this.markSpeaking();
    pending.fallbackAt = now;
    this.toolAckPlayingUntil = this.playbackEndsAt;
    console.log(
      `[tool-ack] source=fallback latency_ms=${now - pending.toolAt} audio_ms=${pcmDurationMs(clip)}`,
    );
  }

  private clearToolAck(): void {
    const pending = this.pendingToolAck;
    if (pending?.timer) {
      clearTimeout(pending.timer);
    }
    this.pendingToolAck = null;
  }

  /** Abandon a pending acknowledgement decision, saying why if nothing had played. */
  private cancelToolAck(why: string): void {
    const pending = this.pendingToolAck;
    if (pending && pending.fallbackAt === null) {
      console.log(`[tool-ack] source=none reason=${why} latency_ms=${Date.now() - pending.toolAt}`);
    }
    this.clearToolAck();
  }

  /** Cancel a pending acknowledgement and cut off one still playing. */
  private silenceToolAck(why: string): void {
    this.cancelToolAck(why);
    if (this.toolAckPlayingUntil > Date.now()) {
      console.log(`[tool-ack] action=cut why=${why}`);
      try {
        this.stopPacatStream();
      } catch (err) {
        console.warn('stopPacatStream while cutting the acknowledgement failed (non-fatal):', err);
      }
      if (simliAvatar.isConnected()) {
        simliAvatar.clearAudioBuffer().catch(() => {});
      }
    }
  }

  /** Barge-in: drop all assistant audio until Nova opens its next audio block. */
  private handleInterruption(): void {
    console.log('⚠️  Interruption detected - dropping all audio until next turn');
    this._isInterrupted = true;
    this.lastInterruptionAt = Date.now();
    // Somebody talking over the assistant is talking to it: the window's idle
    // countdown restarts, so a correction is not cut off by the window lapsing.
    this.noteEngagement();
    // Someone talking over an acknowledgement that is already playing wants it
    // to stop too. One that has not started yet is left alone: the barge-in
    // marker for the very utterance that asked for silence can arrive after
    // the tool call.
    if (this.stopAck && this.stopAck.blockOpenedAt !== null) {
      this.clearStopAck('interrupted');
    }
    this.stopPacatStream();
    // Clear Simli avatar's server-side audio buffer via ClearBuffer() SDK method
    if (simliAvatar.isConnected()) {
      simliAvatar.clearAudioBuffer().catch(() => {});
    }
  }

  /** Bytes of playback PCM per millisecond (16-bit mono at the playback rate). */
  private playbackBytesPerMs(): number {
    return (parseInt(process.env.NOVA_PLAYBACK_RATE || '16000') * 2) / 1000;
  }

  /**
   * An AUDIO content block has started. The first one inside the window after
   * the `mute` tool call is the acknowledgement; once the window has passed
   * without one, there is no acknowledgement to wait for.
   */
  private noteAudioBlockStarted(): void {
    const ack = this.stopAck;
    if (!ack || ack.blockOpenedAt !== null) {
      return;
    }
    const delayMs = Date.now() - ack.toolAt;
    if (delayMs > STOP_ACK_START_WINDOW_MS) {
      this.clearStopAck('window-passed');
      return;
    }
    ack.blockOpenedAt = Date.now();
    console.log(
      `[stop-request] route=${ack.route} action=ack-started delay_ms=${delayMs}`
      + ` gate=${this._isMuted ? 'shut' : 'open'}`,
    );
    // The same deadline takeStopAckAudio applies, enforced without waiting for
    // a chunk that may never come.
    const deadline = ack.toolAt + STOP_ACK_START_WINDOW_MS + STOP_ACK_MAX_AUDIO_MS;
    ack.timer = setTimeout(() => {
      if (this.stopAck === ack) {
        this.finishStopAck('ack-timed-out');
      }
    }, Math.max(0, deadline - Date.now()));
    ack.timer.unref?.();
  }

  /** The acknowledgement block, if one is playing, has ended. */
  private noteAudioBlockEnded(): void {
    if (this.stopAck && this.stopAck.blockOpenedAt !== null) {
      this.finishStopAck('ack-played');
    }
  }

  /**
   * Whether a chunk of assistant audio belongs to the acknowledgement and still
   * fits its budget. Counts it if so. A chunk that would exceed the budget, or
   * that arrives after the latest point an acknowledgement could still be
   * playing, ends the acknowledgement instead.
   */
  private takeStopAckAudio(bytes: number): boolean {
    const ack = this.stopAck;
    if (!ack || ack.blockOpenedAt === null) {
      return false;
    }
    const capBytes = Math.round(STOP_ACK_MAX_AUDIO_MS * this.playbackBytesPerMs());
    const deadline = ack.toolAt + STOP_ACK_START_WINDOW_MS + STOP_ACK_MAX_AUDIO_MS;
    if (ack.bytes + bytes > capBytes || Date.now() > deadline) {
      this.finishStopAck('ack-capped');
      return false;
    }
    ack.bytes += bytes;
    return true;
  }

  /**
   * End the acknowledgement: report how much of it was played, and if the gate
   * has shut behind it, let what was written drain out rather than cutting it
   * off — bounded by PLAYBACK_DRAIN_CAP_MS, as for an acknowledgement spoken
   * before the tool call.
   */
  private finishStopAck(action: 'ack-played' | 'ack-capped' | 'ack-timed-out'): void {
    const ack = this.stopAck;
    if (!ack) {
      return;
    }
    this.stopAck = null;
    if (ack.timer) {
      clearTimeout(ack.timer);
    }
    const audioMs = Math.round(ack.bytes / this.playbackBytesPerMs());
    console.log(
      `[stop-request] route=${ack.route} action=${action} audio_ms=${audioMs}`
      + ` gate=${this._isMuted ? 'shut' : 'open'}`
      + ` elapsed_ms=${Date.now() - ack.toolAt}`,
    );
    if (this._isMuted) {
      try {
        this.stopPacatStream('drain');
      } catch (err) {
        console.warn('draining acknowledgement playback failed (non-fatal):', err);
      }
    }
  }

  /**
   * Withdraw the acknowledgement's exemption from the gate. Idempotent.
   *
   * Returns whether an acknowledgement had started. If it had and the gate is
   * shut, its playback stream may have been held live for it rather than
   * handed off (see muteDrainingPlayback), so the caller has to decide what
   * becomes of the audio already written: cut it off, or let it drain.
   */
  private clearStopAck(why: string): boolean {
    const ack = this.stopAck;
    if (!ack) {
      return false;
    }
    this.stopAck = null;
    if (ack.timer) {
      clearTimeout(ack.timer);
    }
    // Only worth a line once an acknowledgement had started: an unused window
    // closing is the ordinary case and says nothing.
    if (ack.blockOpenedAt !== null) {
      console.log(
        `[stop-request] route=${ack.route} action=ack-cut why=${why}`
        + ` audio_ms=${Math.round(ack.bytes / this.playbackBytesPerMs())}`,
      );
    }
    return ack.blockOpenedAt !== null;
  }

  /**
   * Note that assistant audio was dropped while a stop request was resolving,
   * and say which of the two gates discarded it.
   *
   * `ack-dropped` means the acknowledgement itself was lost: the gate shut
   * before the word could reach the meeting. Once the tool route has shut the
   * gate behind a drained acknowledgement the word was already allowed out, so
   * audio after that is something else the model went on saying — reported as
   * `post-gate-dropped`, because calling it a lost acknowledgement would report
   * a failure in a meeting that heard the word perfectly well.
   *
   * Reported once per request, and only while a request is in flight, so
   * ordinary barge-in and ordinary muting are unaffected. That also means audio
   * dropped after the transcript fallback has closed the request out is not
   * reported here — a tool call arriving that late reports itself, with
   * `late=true` and the latency that says by how much, which is the same
   * finding stated as a measurement.
   */
  private noteStopAckDropped(gate: 'interrupted' | 'muted'): void {
    const request = this.stopRequest;
    if (!request || request.ackDropLogged) {
      return;
    }
    request.ackDropLogged = true;
    // `route` names where the request was recognised, the same as on every
    // other line in the family, so that one CloudWatch filter reads coherently.
    console.log(
      `[stop-request] route=${request.fromTranscript ? 'transcript' : 'tool'}`
      + ` action=${request.gateShutByDrain ? 'post-gate-dropped' : 'ack-dropped'}`
      + ` gate=${gate}`
      + ` elapsed_ms=${Date.now() - request.seenAt} tool_seen=${request.toolSeen}`,
    );
  }

  /** One line per stop-request decision on the transcript route — the only
   *  route that decides anything here, which is why `route` is not a parameter.
   *  Carries the flags and the timings, never the utterance, which is
   *  participant speech. */
  private logStopRequest(action: string): void {
    console.log(
      `[stop-request] route=transcript action=${action} mode=${this.meetingMode}`
      + ` window_ms=${STOP_REQUEST_TOOL_GRACE_MS} speaking=${this._isSpeaking}`
      + ` interrupted=${this._isInterrupted} muted=${this._isMuted}`,
    );
  }

  // -------------------------------------------------------------------------
  // Changing mode mid-meeting (see VoiceAssistantProvider.reconfigure)
  // -------------------------------------------------------------------------

  /** True while a reconfiguration is applying its change and rebuilding. */
  private _reconfiguring: boolean = false;

  /** Incremented on every meeting-mode change. A handler that awaits partway
   *  through compares it before acting, so it does not finish a job that
   *  belonged to the mode the agent has since left. */
  private meetingModeGeneration: number = 0;

  /** How long to wait for a session refresh already in flight before giving up
   *  on rebuilding on top of it. The refresh itself is a teardown, a 300ms
   *  flush and a fresh session, so this is several times the expected wait.
   *  Not `readonly`, because the tests shorten it to exercise the bound
   *  without waiting five seconds. */
  private RECONFIGURE_REFRESH_WAIT_MS = 5000;

  /**
   * Change the meeting mode and/or activation mode on a running agent.
   *
   * Both are baked into the Nova session rather than consulted per turn: the
   * meeting mode decides the system prompt (`buildSystemPromptContent`) and the
   * registered tool set (`buildToolSpecs`), and both are sent once at
   * `promptStart`. Translator mode is the clearest case — it *replaces* the base
   * prompt rather than adding to it — but it is not a special case: no meeting
   * mode can change without a new session. So this applies the new values to the
   * agent's own state and then rebuilds the session through the same
   * `refreshSession` path the four-minute keep-alive refresh and the translator
   * pause/resume transitions already use.
   *
   * The rebuild touches nothing outside the Bedrock stream. The transcript is
   * cut from `combined_audio`, which is fed by the Virtual Participant's own
   * pacat stream and by a PulseAudio loopback — neither of which this method
   * starts, stops or reaches — so the assistant goes quiet for the length of a
   * rebuild and the transcript does not.
   */
  async reconfigure(request: AssistantReconfigureRequest): Promise<AssistantReconfigureOutcome> {
    const meetingModeBefore = this.meetingMode;
    // Written from the stack parameter and the provider config, both of which
    // the factory has already narrowed to this union.
    const activationModeBefore = this.activationMode as ActivationMode;

    const notApplied = (
      result: 'unchanged' | 'declined',
      detail: string,
    ): AssistantReconfigureOutcome =>
      this.recordReconfigure(request, {
        result,
        changed: [],
        sessionRebuilt: false,
        meetingModeBefore,
        meetingModeAfter: meetingModeBefore,
        activationModeBefore,
        activationModeAfter: activationModeBefore,
        // The pair in use, for the log, when one was asked about and the
        // assistant is translating with it.
        translatorLanguagesAfter: request.translatorLanguages && meetingModeBefore === 'translator'
          ? { a: this.translatorLanguageA, b: this.translatorLanguageB }
          : undefined,
        detail,
      });

    // A stopped agent opens no session, whichever path would open it — the
    // same rule the recovery timers follow. `stop()` leaves the Bedrock client
    // in place, so without this a command arriving after it could still build
    // one through the rebuild below.
    if (this.stopped) {
      return notApplied('declined', 'the voice assistant has stopped');
    }

    // A request is taken whole or not at all. Applying the half that parsed
    // would leave the assistant in a configuration nobody asked for, and a
    // request can carry strings from further away than this method can see —
    // the chat vocabulary is a closed set, but nothing about this signature
    // requires its caller to be.
    if (request.meetingMode !== undefined && !isMeetingMode(request.meetingMode)) {
      return notApplied('declined', `unrecognised meeting mode "${request.meetingMode}"`);
    }
    if (request.activationMode !== undefined && !isActivationMode(request.activationMode)) {
      return notApplied('declined', `unrecognised activation mode "${request.activationMode}"`);
    }

    // The translator pair, when one was asked for. `a` falls back to the
    // configured first language when the route could not name the meeting's
    // own, and to the second when the first is the language asked for — so
    // "translate English" on a deployment configured English/Spanish still
    // produces a pair rather than English to English.
    // Case-insensitive: the configured pair is free text from the
    // configuration page, and the chat sends canonical names.
    const same = (x: string, y: string) => x.trim().toLowerCase() === y.trim().toLowerCase();
    let languagesAfter: { a: string; b: string } | undefined;
    if (request.translatorLanguages !== undefined) {
      const b = (request.translatorLanguages.b || '').trim();
      let a = (request.translatorLanguages.a || '').trim();
      if (!a) {
        a = same(this.translatorLanguageA, b) ? this.translatorLanguageB : this.translatorLanguageA;
      }
      if (!b || same(a, b)) {
        return notApplied('declined', 'the translator needs two different languages');
      }
      languagesAfter = { a, b };
    }

    const changed: Array<'meetingMode' | 'activationMode' | 'translatorLanguages'> = [];
    if (request.meetingMode !== undefined && request.meetingMode !== meetingModeBefore) {
      changed.push('meetingMode');
    }
    if (request.activationMode !== undefined && request.activationMode !== activationModeBefore) {
      changed.push('activationMode');
    }
    if (
      languagesAfter
      && (!same(languagesAfter.a, this.translatorLanguageA) || !same(languagesAfter.b, this.translatorLanguageB))
    ) {
      changed.push('translatorLanguages');
    }

    // Ahead of the no-change check: the new values are on the agent as soon as
    // a change starts applying, so a repeat of the same command mid-rebuild
    // would otherwise be told the assistant is already there while it is still
    // switching.
    if (this._reconfiguring) {
      return notApplied('declined', 'a mode change is already in progress');
    }

    // Asking for the configuration it is already in costs the meeting nothing.
    // Without this, a participant retyping a command — or a chat observer
    // delivering one twice — would tear down a working session each time.
    if (changed.length === 0) {
      return notApplied(
        'unchanged',
        languagesAfter
          ? `already translating between ${this.translatorLanguageA} and ${this.translatorLanguageB}`
          : 'already in the requested configuration',
      );
    }

    // Validate the combination, not just each axis. Group and translator mode
    // are silent rather than merely limited without an always-listening
    // assistant, which is why the chat commands for those modes ask for it — and
    // the same rule has to hold coming the other way, or asking only for the
    // wake phrase would leave a group-mode assistant inaudible for the rest of
    // the meeting. Declined with the command that makes it possible, rather than
    // quietly changing the meeting mode nobody asked about.
    //
    // After the no-change check, not before it: a deployment configured as group
    // mode on wake-phrase activation is already in a combination this rule
    // forbids, and `LMA wake` there should say the assistant is already doing
    // that rather than claim it has to stay always listening when it never was.
    const resultingMeetingMode = request.meetingMode ?? meetingModeBefore;
    const resultingActivationMode = request.activationMode ?? activationModeBefore;
    if (requiresAlwaysListening(resultingMeetingMode) && resultingActivationMode !== 'always_active') {
      // Worded from which half was asked for. A request that names the meeting
      // mode is asking to enter it, and has not been told what it needs; a
      // request that names only the activation mode is asking to leave a mode
      // the assistant is already in. Saying "the assistant is in translator
      // mode" to the first would name a mode it has not reached.
      const named = describeMeetingMode(resultingMeetingMode);
      return notApplied(
        'declined',
        request.meetingMode !== undefined
          ? `${named} needs the assistant to be always listening — ask for that too`
          : `the assistant is in ${named}, which has to stay always listening`
              + ' — switch to assistant mode first with "LMA assist"'
              + (resultingMeetingMode === 'translator' ? ', or say "stop translating"' : ''),
      );
    }


    this._reconfiguring = true;
    try {
      // Before the meeting mode, so entering translator mode logs the new pair.
      if (changed.includes('translatorLanguages') && languagesAfter) {
        this.translatorLanguageA = languagesAfter.a;
        this.translatorLanguageB = languagesAfter.b;
        // Turns translated under the old pair would prime the new session to
        // keep translating into the language it has just left.
        this.conversationHistory = [];
        console.log(`🌐 Translator languages are now ${languagesAfter.a} ↔ ${languagesAfter.b} (this meeting only)`);
      }
      if (changed.includes('meetingMode')) {
        this.applyMeetingMode(request.meetingMode as MeetingMode);
      }
      if (changed.includes('activationMode')) {
        this.applyActivationMode(request.activationMode as ActivationMode);
      }

      // The new values are on the agent by this point, so a rebuild that throws
      // is a session left behind rather than a request that did not happen. It
      // is reported as applied with no rebuild and the reason attached, which
      // keeps the one-line-per-attempt contract and gives the participant an
      // answer, instead of escaping into a platform chat handler as a rejected
      // promise. The queued refresh is how the session catches up.
      let sessionRebuilt = false;
      let detail: string | undefined;
      try {
        const rebuild = await this.rebuildSessionForNewMode();
        sessionRebuilt = rebuild === 'rebuilt';
        if (rebuild === 'deferred') {
          // Applied, but the live session is still the one the agent has left,
          // so the reply says it is finishing rather than reading as complete.
          detail = 'the assistant will finish switching shortly';
        }
      } catch (error) {
        console.error('❌ Error rebuilding the Nova session for a mode change:', error);
        // Only promise the participant a recovery when one was actually
        // scheduled. Where none is needed the mode change is simply done, and
        // the session the next wake phrase builds carries it.
        detail = this.scheduleSessionRecovery()
          ? 'the assistant will finish switching shortly'
          : undefined;
      }

      return this.recordReconfigure(request, {
        result: 'applied',
        changed,
        sessionRebuilt,
        meetingModeBefore,
        meetingModeAfter: this.meetingMode,
        activationModeBefore,
        activationModeAfter: this.activationMode as ActivationMode,
        translatorLanguagesAfter: this.meetingMode === 'translator'
          ? { a: this.translatorLanguageA, b: this.translatorLanguageB }
          : undefined,
        detail,
      });
    } finally {
      this._reconfiguring = false;
    }
  }

  // -------------------------------------------------------------------------
  // Changing mode by voice (voice-mode-switch.ts)
  // -------------------------------------------------------------------------

  /** How recently the assistant must have been addressed by wake phrase for a
   *  `set_mode` call to count as asked for. Nova splits one utterance into
   *  several turns, so the turn just before the call need not hold the phrase. */
  private readonly VOICE_MODE_ADDRESSED_WINDOW_MS = 15000;

  /** How long a `set_mode` switch waits for its confirmation to be spoken.
   *  Not `readonly`, so the tests can shorten it. */
  private VOICE_MODE_SWITCH_MAX_WAIT_MS = 4000;

  /**
   * A `set_mode` call: answer it with the sentence to say, and arm the switch
   * to run once that sentence has been generated.
   */
  private async handleSetModeTool(
    toolUseId: string,
    toolUseContent: any,
    issuingSession: SessionData | null,
  ): Promise<void> {
    let mode: unknown;
    try {
      mode = JSON.parse(toolUseContent.content)?.mode;
    } catch {
      mode = undefined;
    }
    const addressed = addressesAssistant(this.lastUserTurnText, configuredWakePhrases())
      || Date.now() - this.lastAddressedAt < this.VOICE_MODE_ADDRESSED_WINDOW_MS;

    const reply = async (result: object): Promise<void> => {
      if (issuingSession && issuingSession === this.session && issuingSession.isActive) {
        try {
          await this.sendToolResult(toolUseId, result);
        } catch (err) {
          console.warn('Failed to send set_mode tool result (non-fatal):', err);
        }
      }
    };

    if (!isVoiceMode(mode) || !addressed) {
      console.log(`[voice-mode] mode=${isVoiceMode(mode) ? mode : 'unrecognised'} action=ignored addressed=${addressed}`);
      // The model calls the tool for talk about modes that was not addressed to
      // it, and told to stay silent it still answered out loud after the call
      // in 8 runs of 8 (amazon.nova-2-sonic-v1:0, October 2026), so whatever it
      // says for the rest of this turn is dropped.
      if (issuingSession) {
        issuingSession.dropTurn = true;
        issuingSession.dropReplyText = true;
      }
      await reply(SET_MODE_NOT_ADDRESSED_RESULT);
      return;
    }

    const command = voiceModeCommand(mode);
    const state = {
      meetingMode: this.meetingMode,
      activationMode: this.activationMode as ActivationMode,
      reconfiguring: this._reconfiguring || !!this.pendingVoiceModeSwitch,
    };
    const prediction = predictVoiceModeChange(state, command);
    const sentence = voiceModeSentence(state, command, prediction);
    console.log(`[voice-mode] mode=${mode} action=${prediction.result === 'applied' ? 'switching' : prediction.result}`);

    if (prediction.result === 'applied') {
      this.pendingVoiceModeSwitch = {
        command,
        session: issuingSession,
        sawAudio: false,
        timer: setTimeout(() => this.runVoiceModeSwitch('timed-out'), this.VOICE_MODE_SWITCH_MAX_WAIT_MS),
      };
    } else if (!this.pendingVoiceModeSwitch) {
      // A request that changes nothing rebuilds nothing, so it goes through the
      // chat route's path at once: that writes its [assistant-toggle] line and
      // lets the restore memory record it (a refused "wake mode" in listen mode
      // is remembered for when the meeting goes back to assistant mode). Not
      // while a voice switch is waiting, which `reconfigure` cannot see: it
      // would apply this request instead of declining it.
      applyAssistantChatCommand(this, command, null, undefined, 'voice').catch((error) => {
        console.error('Error recording a voice mode request:', error);
      });
    }
    await reply({
      status: prediction.result,
      instruction: `Say exactly "${sentence}" and nothing else.`,
    });
  }

  /** Make the switch a `set_mode` call asked for, through the chat route's own
   *  path so the activation mode a translator or listen mode replaced comes back. */
  private runVoiceModeSwitch(why: 'confirmed' | 'timed-out'): void {
    const pending = this.pendingVoiceModeSwitch;
    if (!pending) return;
    this.pendingVoiceModeSwitch = null;
    if (pending.timer) clearTimeout(pending.timer);
    if (this.stopped) return;
    // The sentence was decided when the tool was called. If another route has
    // changed the mode since, the switch it announced may no longer apply.
    const now = predictVoiceModeChange({
      meetingMode: this.meetingMode,
      activationMode: this.activationMode as ActivationMode,
      reconfiguring: this._reconfiguring,
    }, pending.command);
    if (now.result !== 'applied') {
      console.log(`[voice-mode] action=superseded after=${why} now=${now.result}`);
      return;
    }
    console.log(`[voice-mode] action=switch after=${why}`);
    applyAssistantChatCommand(this, pending.command, null, undefined, 'voice').catch((error) => {
      console.error('Error applying a voice mode change:', error);
    });
  }

  /** Who a translator answers to, for "Alex, stop translating". */
  private translatorAddressees(): string[] {
    return translatorAddressees(configuredWakePhrases(), this.translatorMutePhrases);
  }

  /**
   * A participant's finished utterance, from the participants' own transcript
   * (Amazon Transcribe channel 0, or the ASR MicroVM), passed on by the scribe.
   *
   * Nova's own transcript of the same speech arrives on its session, and in
   * translator mode it has been seen to be wrong, so the controls a translator
   * answers to are read from both, and the first to recognise one acts. While
   * the translator is paused only this one is read: the paused session is not
   * sent the meeting's audio (sendAudioChunk).
   */
  noteParticipantUtterance(text: string): void {
    if (!text || !text.trim()) return;
    this.participantTranscriptSeen = true;
    this.handleTranslatorUtterance(text, this.session, 'transcript');
  }

  /**
   * What a translator does with a participant's utterance, beyond translating it:
   *
   * - "Stop translating" (voice-mode-switch.ts), paused or not, leaves
   *   translator mode for assistant mode.
   * - While paused, the resume phrase resumes translating. On Nova's
   *   transcript that is the paused session's `unmute` tool, so only the
   *   participants' transcript is matched for it here.
   * - While paused, the wake phrase leaves translator mode, and what was asked
   *   is passed to the assistant. A wake phrase addresses the assistant, not
   *   the translator: resuming would translate the question rather than answer
   *   it, and staying paused leaves the participant talking to an assistant
   *   that never answers.
   */
  private handleTranslatorUtterance(
    text: string,
    session: SessionData | null,
    route: 'nova' | 'transcript',
  ): void {
    if (this.meetingMode !== 'translator' || this.stopped) return;
    if (isStopTranslatingRequest(text, this.translatorAddressees())) {
      this.leaveTranslatorRequestedAt = Date.now();
      this.handleStopTranslating(session, route);
      return;
    }
    if (this._translatorState !== 'MUTED') return;
    // A paused session still hearing the meeting has no participants'
    // transcript to defer to; one that is not hears nothing to act on.
    if (route === 'nova' && this.participantTranscriptSeen) return;
    if (route === 'transcript' && containsTriggerPhrase(text, this.translatorUnmutePhrases)) {
      this.resumeTranslator(route);
      return;
    }
    if (this.isTranslatorTriggerUtterance(text)) return;
    // addressesAssistant rather than containsWakePhrase, which logs every
    // utterance it is given — here, everything said while the translator is paused.
    if (addressesAssistant(text, configuredWakePhrases()) && !isStopTalkingRequest(text, configuredWakePhrases())) {
      this.handleStopTranslating(session, route, text);
    }
  }

  /** Resume a paused translator from the participants' transcript, as the
   *  `unmute` tool does from the paused session. */
  private resumeTranslator(route: 'transcript'): void {
    if (this._reconfiguring || this.leavingTranslator) return;
    console.log(`[translator] action=resume route=${route}`);
    this.unmute(route);
    this.playConfirmationTone('unmute');
    this._translatorState = 'UNMUTED';
    this.refreshSession().catch(error => {
      console.error('Error refreshing session for translator UNMUTE transition:', error);
    });
  }

  /**
   * "Stop translating": leave translator mode for assistant mode, through the
   * chat route's path, so the activation mode translator mode replaced comes
   * back with it.
   *
   * The session that heard the request is about to translate it, so its audio
   * is dropped from here on. The confirmation cannot come from the model —
   * after the switch there may be no session at all (a wake-phrase meeting
   * holds none between activations) — so it is a clip in the configured
   * voice, rendered when translator mode was entered, or the pause tone if the
   * clip is not available.
   *
   * `request` is a wake-phrase utterance heard while the translator was
   * paused: once in assistant mode, it is handed to the assistant as if it had
   * been said there.
   */
  private handleStopTranslating(
    session: SessionData | null,
    route: 'nova' | 'transcript' | 'tool' = 'nova',
    request?: string,
  ): void {
    // Nova can deliver the same request on two sessions around a hand-over,
    // and both transcripts carry it.
    if (this.leavingTranslator) return;
    if (this._reconfiguring) {
      // Another change is under way; this request is not being acted on.
      this.leaveTranslatorRequestedAt = 0;
      return;
    }
    this.leavingTranslator = true;
    if (session) session.silenced = true;
    console.log(
      `[voice-mode] mode=assistant action=stop-translating route=${route}`
      + `${request !== undefined ? ' why=wake-phrase-while-paused' : ''}`,
    );
    applyAssistantChatCommand(this, { meetingMode: 'normal' }, null, undefined, 'voice')
      .then(() => {
        if (this.meetingMode !== 'normal') {
          console.log(`[voice-mode] action=stop-translating-declined meetingMode=${this.meetingMode}`);
          // Still translating, so this session's later answers are to be heard,
          // and a pause asked for next is a pause.
          if (session) session.silenced = false;
          this.leaveTranslatorRequestedAt = 0;
          return;
        }
        if (this.stoppedTranslatingClip) {
          this.writePlayback(this.stoppedTranslatingClip);
          this.markSpeaking();
        } else {
          this.playConfirmationTone('mute');
        }
        if (request !== undefined) {
          this.passRequestToAssistant(request);
        }
      })
      .catch((error) => {
        console.error('Error leaving translator mode by voice:', error);
        if (session && this.meetingMode === 'translator') session.silenced = false;
        this.leaveTranslatorRequestedAt = 0;
      })
      .finally(() => {
        this.leavingTranslator = false;
      });
  }

  /** Hand a wake-phrase utterance to the assistant, as the scribe does on a
   *  wake phrase: an activation with it as context, or, where the assistant is
   *  always listening and so never activated, the question as a message. */
  private passRequestToAssistant(request: string): void {
    if (this.activationMode === 'always_active') {
      const question = this.extractQuestion(request);
      if (question && this.session?.isActive) {
        this.sendUserMessage(question);
      }
      return;
    }
    this.activate(undefined, request).catch((error) => {
      console.error('Error activating the assistant after leaving translator mode:', error);
    });
  }

  /** Render the stopped-translating confirmation, once, in the background. */
  private renderStoppedTranslatingClip(): void {
    const client = this.bedrockClient;
    if (!client || this.stoppedTranslatingClip || this.stoppedTranslatingClipRendering) return;
    this.stoppedTranslatingClipRendering = true;
    renderToolAckClip(client, this.modelId, this.voiceId, STOPPED_TRANSLATING_PHRASE, {
      shouldContinue: () => !this.stopped,
    })
      .then((clip) => {
        this.stoppedTranslatingClip = clip;
      })
      .catch((error) => {
        console.warn('[voice-mode] action=render-failed', error);
      })
      .finally(() => {
        this.stoppedTranslatingClipRendering = false;
      });
  }

  /** Emit the one canonical log line for a toggle and return the outcome
   *  unchanged, so every exit from `reconfigure` is recorded. */
  private recordReconfigure(
    request: AssistantReconfigureRequest,
    outcome: AssistantReconfigureOutcome,
  ): AssistantReconfigureOutcome {
    console.log(formatAssistantToggleLog(request, outcome));
    return outcome;
  }

  /**
   * Move the agent's own state onto a new meeting mode.
   *
   * Every derivation here matches the constructor and `start()`, so a mode
   * reached by toggle is indistinguishable from the same mode configured up
   * front. Group mode starts muted and waits for a wake phrase; the other two
   * start audible.
   */
  private applyMeetingMode(mode: MeetingMode): void {
    this.meetingMode = mode;
    this.groupMeetingMode = (mode === 'group');
    // Set directly rather than through mute(), which also flushes the playback
    // stream and the avatar's buffer. Those belong to a stop request, where the
    // point is to cut the assistant off mid-word. A mode change is not that: the
    // rebuild below stops new audio arriving within a few hundred milliseconds
    // anyway, and letting the last of an utterance play out is better than
    // clipping it.
    this._isMuted = (mode === 'group');

    // A stop request still resolving was made under the mode being left, and
    // the new mode has just decided whether the assistant is audible, so the
    // request is superseded rather than carried across. Called off by hand
    // because the mute state is set directly above, and the cancellation that
    // unmute() performs never runs.
    //
    // Carrying it would go wrong in every direction. Into translator, its
    // fallback would mute a translator whose pause state still says UNMUTED,
    // with only the `mute` tool registered — so the resume phrase would have no
    // tool to call and it would never speak again, which is why translator
    // declines stop requests in the first place. Into `normal`, it would
    // silence an assistant the mode change has just made audible. And either
    // way the session the request was waiting on is about to be torn down.
    //
    // Only the meeting mode does this. An activation-only change leaves the
    // mute state alone, so a request made a moment before it still stands and
    // is resolved as usual — mute() is a gate on the agent, not on the session.
    this.cancelStopRequest('mode-changed');
    this.lastTranscriptStopAt = null;
    // Lets a mute tool call already past its tool-use event see that the mode
    // it was answering has gone (see handleMuteTool).
    this.meetingModeGeneration += 1;

    // The transcription path reads the mode from the shared meeting details, per
    // transcript result on the Amazon Transcribe path. Leaving it on the mode the
    // meeting started in would decide whether the assistant's own speech belongs
    // in the transcript from a mode the assistant is no longer in.
    publishMeetingMode(mode);

    // Translator pause/resume is a two-state machine whose state decides which
    // single tool is registered. A mode change arrives with no opinion about it,
    // so both entering and leaving translator mode start from unpaused — leaving
    // MUTED behind would register `unmute` against a prompt that never mentions
    // it.
    this._translatorState = 'UNMUTED';

    // The group-mode auto-mute timer belongs to the mode that armed it.
    this.clearConversationTimeout();
    this.conversationActive = false;
    this.autoMuteQueued = false;

    // The next session's prompt is written for the new mode, so replaying the
    // old mode's turns into it primes it with a conversation that mode never
    // had — a translator opening by answering the last question it was asked.
    this.conversationHistory = [];

    console.log(`🔀 Meeting mode is now ${mode}`);
    if (mode === 'translator') {
      console.log(`  🌐 Translator mode: ${this.translatorLanguageA} ↔ ${this.translatorLanguageB}`);
      this.renderStoppedTranslatingClip();
      // A "stop translating" from a previous spell in translator mode says
      // nothing about a pause asked for in this one.
      this.leaveTranslatorRequestedAt = 0;
    }
  }

  /** Move the agent's own state onto a new activation mode, matching the
   *  constructor's derivation of `_isActivated`. */
  private applyActivationMode(mode: ActivationMode): void {
    this.activationMode = mode;
    this._isActivated = (mode === 'always_active');
    this.activationHoldReason = null;

    if (this.activationTimeout) {
      // The window the old mode was counting down belongs to that mode, whatever
      // the new one is. Cleared unconditionally: a timer left armed into
      // `always_active` only fails to do anything because `deactivate()` returns
      // early there, which is a reason the next reader should not have to find.
      clearTimeout(this.activationTimeout);
      this.activationTimeout = null;
    }

    console.log(`🔀 Activation mode is now ${mode}`);
  }

  /**
   * Give the agent a session built for the configuration it is now in.
   *
   * @returns `'rebuilt'` when a session was built or torn down; `'not-needed'`
   *   when the agent is holding no session that needs replacing, which on
   *   `wake_phrase` activation is the normal, correct state and the next wake
   *   phrase builds one from the new configuration; `'deferred'` when a refresh
   *   already in flight has the rebuild and the live session is, for now, still
   *   the one the agent has left.
   */
  private async rebuildSessionForNewMode(): Promise<'rebuilt' | 'not-needed' | 'deferred'> {
    // A periodic refresh already queued is superseded: this rebuild produces the
    // fresh session it was going to produce, and leaving the flag set would tear
    // the new session down again as soon as the agent next went idle.
    this.refreshQueued = false;

    if (!(await this.waitWhileRefreshing())) {
      // A refresh that has not settled within the bound is itself building a
      // session, and `refreshSession` declines to start a second one. The new
      // values are already on the agent, so whether that session carries them
      // depends on how far through its prompt it already was — report no rebuild
      // rather than claim one.
      //
      // Queue one and start the idle-refresh machinery after it, so the session
      // converges on the new mode rather than waiting for the four-minute timer:
      // if that in-flight refresh had already sent its promptStart, the live
      // session is running the old mode's prompt and tools while everything else
      // says otherwise. Setting the flag alone would not be enough — it is only
      // acted on when the assistant finishes speaking or when the periodic timer
      // fires, and a freshly switched group-mode assistant starts muted and so
      // may not speak for a long time.
      console.warn('⚠️  Session refresh still in flight — queueing a rebuild behind it');
      this.refreshQueued = true;
      this.checkAndExecuteQueuedRefresh();
      return 'deferred';
    }

    const hasLiveSession = !!(this.session && this.session.isActive);

    // Keyed on `wake_phrase` specifically rather than on "not always_active":
    // that is the one mode whose contract is to hold no session between
    // activations. `start()` opens a session immediately for every other value,
    // so tearing one down here for a mode that expects to keep it would leave
    // the assistant worse off than it started.
    if (this.activationMode === 'wake_phrase') {
      if (!hasLiveSession) {
        return 'not-needed';
      }
      if (this._isActivated) {
        // A session exists because somebody addressed the assistant and the
        // activation window they opened is still running. Replace it rather than
        // close it: closing would leave the window counting down against no
        // session, so the assistant would be deaf for the rest of it.
        return await this.refreshIntoNewMode();
      }
      // Otherwise the wake-phrase contract applies — no session is held open
      // between activations, so the rebuild is a teardown and the next wake
      // phrase builds one from the new configuration.
      //
      // This also closes a session `preConnect` opened on a wake phrase seen in
      // a partial transcript, in the second or two before the final arrives.
      // That is the right outcome rather than a loss: that session was built for
      // the mode the agent has just left, so it could not be used as it stands,
      // and `activate()` creates a replacement when the final transcript lands.
      // The only cost is the pre-warm, for a wake phrase in flight at the moment
      // someone changed the mode.
      await this.closeSession();
      return 'rebuilt';
    }

    if (!hasLiveSession) {
      // Switching to a mode that holds a session open, from one that was not:
      // the assistant has to start listening now, not at the next wake phrase.
      await this.createSession();
      if (this.session && this.session.isActive) {
        return 'rebuilt';
      }
      // `createSession` returns early when a stream is already marked active, so
      // it can resolve without having opened anything. Report from what is
      // there, the same way `refreshIntoNewMode` does.
      console.warn('⚠️  No session opened for the new mode — scheduling a recovery');
      this.scheduleSessionRecovery();
      return 'deferred';
    }

    return await this.refreshIntoNewMode();
  }

  /**
   * Replace the live session with one built for the mode the agent is now in,
   * and report what actually happened.
   *
   * `refreshSession` catches its own failures: it schedules a retry a second
   * later and returns normally rather than throwing, so the `catch` in
   * `reconfigure` never sees them. The only way to tell a refresh that produced
   * a session from one that did not is to look at what it left behind —
   * otherwise a failed refresh would be logged as `session=rebuilt` and
   * confirmed to the participant with no caveat.
   */
  private async refreshIntoNewMode(): Promise<'rebuilt' | 'deferred'> {
    await this.refreshSession();
    if (this.session && this.session.isActive) {
      return 'rebuilt';
    }
    console.warn('⚠️  Session refresh for a mode change produced no session — its own retry will build one');
    return 'deferred';
  }

  /**
   * Get the agent back towards a session built for the mode it is now in, after
   * a rebuild failed.
   *
   * The two failures need different recoveries, and the one that matters is the
   * second. `refreshSession` declines to act when there is no active session, so
   * queueing a refresh after a failed `createSession` would clear the queue and
   * do nothing — and that is exactly the path `LMA translate` or `LMA group`
   * takes on a wake-phrase deployment, where there is no session to begin with.
   * An `always_active` agent with no session is also not recoverable by being
   * addressed, because `activate()` returns early in that mode.
   *
   * @returns whether a recovery was scheduled, so the caller only promises one
   *   when there is one.
   */
  private scheduleSessionRecovery(): boolean {
    if (this.session && this.session.isActive) {
      // A session exists but was built for the mode the agent has left. The
      // idle-refresh machinery replaces it.
      this.refreshQueued = true;
      this.checkAndExecuteQueuedRefresh();
      return true;
    }

    if (this.activationMode === 'wake_phrase') {
      // No session is meant to be open, so there is nothing to recover: the one
      // the next wake phrase builds carries the new mode. Keyed on
      // `wake_phrase` for the same reason as rebuildSessionForNewMode — it is
      // the one mode that holds no session between activations.
      return false;
    }

    // Build it directly — the same recovery `refreshSession` makes for itself
    // when its own rebuild fails.
    this.scheduleSessionCreateRetry(() => {
      if (this.session && this.session.isActive) {
        return;
      }
      // `createSession` returns without doing anything while `streamingActive`
      // is set, and a failure that left the flag behind would make this retry
      // open nothing as well, after the participant was told the switch was
      // finishing. With no active session the flag can only be stale: the
      // session is assigned before the flag is set, and the event stream ends
      // as soon as there is no session to read from, so nothing is streaming.
      if (this.streamingActive) {
        console.warn('⚠️  Clearing a stream flag left set with no session, so the recovery can open one');
        this.streamingActive = false;
        this._isActive = false;
      }
      console.log('🔄 Recovering the Nova session after a failed mode change');
      this.createSession().catch(error => {
        console.error('Error recovering the Nova session after a mode change:', error);
      });
    });
    return true;
  }

  /** Wait, with a bound, for a session refresh already in flight to settle, so
   *  the rebuild replaces a finished session rather than racing one that is
   *  still sending its prompt.
   *
   *  @returns true when nothing is refreshing and the caller may proceed. */
  private async waitWhileRefreshing(): Promise<boolean> {
    const deadline = Date.now() + this.RECONFIGURE_REFRESH_WAIT_MS;
    while (this.isRefreshing && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    return !this.isRefreshing;
  }
}
