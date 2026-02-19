/**
 * Voice Assistant Provider Interface
 * 
 * Generic interface for voice AI assistants (ElevenLabs, AWS Nova, etc.)
 */

export type VoiceAssistantProviderType = 'none' | 'elevenlabs' | 'aws_nova';
export type ActivationMode = 'always_active' | 'wake_phrase' | 'strands_tool';

export interface VoiceAssistantConfig {
  provider: VoiceAssistantProviderType;
  activationMode: ActivationMode;
  wakePhrase?: string;
  activationDuration?: number;
  region?: string;
  strandsLambdaArn?: string;
}

export interface ElevenLabsConfig extends VoiceAssistantConfig {
  provider: 'elevenlabs';
  apiKey: string;
  agentId?: string;
}

export interface NovaConfig extends VoiceAssistantConfig {
  provider: 'aws_nova';
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
}
