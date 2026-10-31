/**
 * Voice Assistant Provider Interface
 * 
 * Generic interface for voice AI assistants (ElevenLabs, AWS Nova, etc.)
 */

// Type-only: assistant-toggles.ts imports ActivationMode back from here, and
// erasing the import keeps the two modules out of a runtime cycle.
import type { AssistantReconfigureRequest, AssistantReconfigureOutcome } from './assistant-toggles.js';

export type VoiceAssistantProviderType = 'none' | 'elevenlabs' | 'amazon_nova_sonic';
export type ActivationMode = 'always_active' | 'wake_phrase' | 'strands_tool';

export interface VoiceAssistantConfig {
  provider: VoiceAssistantProviderType;
  activationMode: ActivationMode;
  wakePhrase?: string;
  activationDuration?: number;
  /**
   * AWS region for the provider's own API calls.
   *
   * Unset by createVoiceAssistantFromEnv, which is the only production caller: each
   * provider resolves its own region instead. For Nova Sonic that is
   * resolveNovaSonicRegion, which may differ from the stack's region — everything
   * else the Virtual Participant talks to stays local. Passing a value here
   * overrides that resolution.
   */
  region?: string;
  strandsLambdaArn?: string;
}

export interface ElevenLabsConfig extends VoiceAssistantConfig {
  provider: 'elevenlabs';
  apiKey: string;
  agentId?: string;
}

export interface NovaConfig extends VoiceAssistantConfig {
  provider: 'amazon_nova_sonic';
  modelId: string;
  systemPrompt: string;
  knowledgeBaseId?: string;
}

export interface NoOpConfig extends VoiceAssistantConfig {
  provider: 'none';
}

export type ProviderConfig = ElevenLabsConfig | NovaConfig | NoOpConfig;

/**
 * Voice Assistant Provider Interface
 * All voice assistant implementations must implement this interface
 */
export interface VoiceAssistantProvider {
  /**
   * Start the voice assistant (connect WebSocket, initialize resources)
   */
  start(): Promise<void>;

  /**
   * Stop the voice assistant (disconnect, cleanup)
   */
  stop(): Promise<void>;

  /**
   * Change the assistant's meeting mode and/or activation mode while the
   * meeting is running.
   *
   * Both are read once when the provider starts, so changing either means the
   * provider has to rebuild whatever it baked them into. Only the provider knows
   * what that is: for Nova Sonic the meeting mode decides the system prompt and
   * the registered tool set, both of which are fixed for the life of a
   * bidirectional stream, so a mode change is a session rebuild rather than a
   * flag flip. A brief gap in what the assistant can hear is expected; the
   * transcript is produced by a separate audio path and is not affected.
   *
   * Providers that cannot honour a request return it undone — `result:
   * 'declined'` (or `'unchanged'` when the assistant is already doing what was
   * asked), an empty `changed` list and a `detail` explaining why — rather than
   * half-applying it.
   *
   * Every call logs one line (see `formatAssistantToggleLog`), including the
   * calls that change nothing, so the record of what a meeting was asked to do
   * does not depend on the request succeeding.
   *
   * @param request What to change, which route asked, and who asked.
   * @returns What actually changed, and whether the session was rebuilt.
   */
  reconfigure(request: AssistantReconfigureRequest): Promise<AssistantReconfigureOutcome>;

  /**
   * Whether this provider can change mode during a meeting at all — that is,
   * whether `reconfigure` is ever able to apply a request rather than decline
   * every one. The `LMA help` chat reply lists the assistant-mode commands only
   * when this is true, so participants are not told about commands that are
   * always answered with a refusal.
   */
  supportsModeChanges(): boolean;

  /**
   * Send audio chunk to the voice assistant
   * @param chunk PCM audio buffer (16kHz, 16-bit, mono)
   */
  sendAudioChunk(chunk: Buffer): void;

  /**
   * Send a text message to the voice assistant (agent will respond with voice)
   * Used for wake phrase activation with initial context
   * @param text The text message to send
   */
  sendUserMessage(text: string): void;

  /**
   * Pre-warm the connection (start WebSocket/session without activating audio).
   * Called when a wake phrase is detected in a partial transcript so the
   * connection is ready by the time the full segment arrives.
   * Safe to call multiple times — subsequent calls are no-ops if already connecting/connected.
   */
  preConnect(): Promise<void>;

  /**
   * Activate the voice assistant for a duration
   * @param duration Optional duration in seconds (uses default if not provided)
   * @param initialContext Optional transcript context from wake phrase detection
   */
  activate(duration?: number, initialContext?: string): Promise<void>;

  /**
   * Deactivate the voice assistant
   */
  deactivate(): void;

  /**
   * Check if voice assistant is enabled (provider is not 'none')
   */
  isEnabled(): boolean;

  /**
   * Check if voice assistant is active (WebSocket connected and ready)
   */
  isActive(): boolean;

  /**
   * Check if voice assistant is activated (listening mode)
   * - always_active: always returns true
   * - wake_phrase: returns true if recently activated
   * - strands_tool: returns true if activated by Strands
   */
  isActivated(): boolean;

  /**
   * Check if voice assistant is currently speaking
   */
  isSpeaking(): boolean;

  /**
   * Mute the voice assistant — drops all outgoing audio. Idempotent.
   * @param reason Optional log tag identifying the source of the call.
   */
  mute(reason?: string): void;

  /**
   * Unmute the voice assistant — resumes outgoing audio. Idempotent.
   * @param reason Optional log tag identifying the source of the call.
   */
  unmute(reason?: string): void;

  /**
   * Whether the voice assistant is currently muted.
   */
  isMuted(): boolean;

  /**
   * Act on a participant asking the assistant to stop talking: silence the
   * utterance in progress and stay silent until the assistant is addressed
   * again by wake phrase.
   *
   * The provider decides, because only it knows which meeting mode it is in —
   * translator mode is driven by its own pause/resume trigger phrases and
   * declines — and because only it knows whether it has another way of
   * recognising the request. Where it does, it may hold the request for a short
   * window so that route can answer out loud first; it silences the assistant
   * itself if that does not happen, so an accepted request ends in silence.
   *
   * A provider may still call a request off before it resolves, if something
   * unambiguously asks the assistant back inside the window. Whether it can
   * tell that apart from the request itself depends on what it can observe, so
   * that is the provider's judgement and callers should not count on it.
   *
   * @param reason Optional log tag identifying the source of the call.
   * @returns true when the request has been accepted and the assistant will be
   *   silent, immediately or within the provider's own window, and will stay
   *   silent until addressed again; false when the current mode does not take
   *   stop requests from this route.
   */
  requestStopTalking(reason?: string): boolean;

  /**
   * A participant's finished utterance, from the participants' transcript
   * (Amazon Transcribe channel 0, or the ASR MicroVM). Optional: Nova Sonic's
   * translator reads its spoken controls ("stop translating", the resume
   * phrase, the wake phrase while paused) from it.
   */
  noteParticipantUtterance?(text: string): void;
}
