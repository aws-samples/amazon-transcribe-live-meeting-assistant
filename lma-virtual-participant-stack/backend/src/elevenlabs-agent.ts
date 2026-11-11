import { spawn, ChildProcess } from 'child_process';
import WebSocket from 'ws';
import { VoiceAssistantProvider } from './voice-assistant-interface.js';
import { LambdaClient, InvokeCommand } from '@aws-sdk/client-lambda';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { defaultProvider } from '@aws-sdk/credential-provider-node';
import { simliAvatar } from './simli-avatar.js';
import {
  hasNovaSonicConfigTable,
  loadNovaSonicConfig,
  MeetingMode,
  novaSonicConfigTablesFromEnv,
} from './nova-sonic-config-loader.js';
import { configuredWakePhrases, foldWakeSpellings, isStopTalkingRequest } from './stop-request.js';
import { mutedLookupSkipReason, SKIPPED_LOOKUP_RESULT, skippedLookupLogLine } from './muted-lookup.js';
import { ActivationMode } from './voice-assistant-interface.js';
import {
  AssistantReconfigureOutcome,
  AssistantReconfigureRequest,
  formatAssistantToggleLog,
  VoiceControlOutcome,
  VoiceControlState,
} from './assistant-toggles.js';
import { agentSpeakingDetector } from './agent-speaking-detector.js';
import { ActivationWindow } from './voice-activation-window.js';
import { spokenStrandsAnswer } from './spoken-text.js';
import { createLogger } from './logger.js';
import { inMeetingRequestPayload } from './meeting-assist-request.js';

const log = createLogger('elevenlabs-agent');

export interface ElevenLabsAgentConfig {
  apiKey: string;
  agentId?: string;
  activationMode?: string;
  activationDuration?: number;
  region?: string;
  strandsLambdaArn?: string;
  meetingMode?: MeetingMode;
  translatorLanguageA?: string;
  translatorLanguageB?: string;
}

export class ElevenLabsAgent implements VoiceAssistantProvider {
  private apiKey: string;
  private agentId: string | null;
  private ws: WebSocket | null = null;
  private audioProcess: ChildProcess | null = null;
  private enabled: boolean;
  private isConnected: boolean = false;
  private _isSpeaking: boolean = false; // Track when agent is playing audio
  /** strands_agent lookups in progress; overlapping calls each hold the window. */
  private toolCallsInFlight: number = 0;
  private audioQueue: Buffer[] = []; // Queue for audio chunks
  private isPlayingQueue: boolean = false; // Track if queue is being processed
  private activationMode: string;
  private _isActivated: boolean = false;
  private _isMuted: boolean = false;
  /** The most recent participant turn the agent heard, which is the turn a
   *  tool call that follows it is answering. */
  private lastUserTranscript: string = '';
  private defaultActivationDuration: number;

  /** When the local playback queue last finished (0 if nothing has played). */
  private playbackEndedAt: number = 0;
  /** When the agent last reported a participant's turn (user_transcript). */
  private lastUserTranscriptAt: number = 0;
  /** Whether the agent's output sink is carrying speech right now, which is
   *  what the meeting hears. A field so tests can stand in for PulseAudio. */
  private audibleOutput: () => boolean = () => agentSpeakingDetector.isSpeaking();
  /** The wake-phrase activation window: idle time, not a deadline from the
   *  wake phrase (see voice-activation-window.ts). */
  private activationWindow = new ActivationWindow(
    {
      toolRunning: () => this.toolCallsInFlight > 0,
      audioArriving: () => this._isSpeaking || this.audioQueue.length > 0,
      // There is no playback estimate on this provider: each chunk is played
      // to completion by paplay, and queued or playing audio is covered by
      // audioArriving. This is only ever in the past, as the reference point
      // for the detector cap and for idle time.
      playbackEndsAt: () => this.playbackEndedAt,
      audible: () => this.audibleOutput(),
      lastUserTurnAt: () => this.lastUserTranscriptAt,
    },
    () => this.deactivate(),
  );
  private region: string;

  // Meeting-mode configuration (parity with Nova Sonic)
  private meetingMode: MeetingMode = 'normal';
  private translatorLanguageA: string = 'English';
  private translatorLanguageB: string = 'Spanish';

  // Group meeting mode: conversation session tracking (mirrors Nova)
  private conversationActive: boolean = false;
  private conversationTimeout: NodeJS.Timeout | null = null;

  // Lambda client and Strands agent configuration
  private lambdaClient: LambdaClient | null = null;
  private strandsLambdaArn?: string;

  constructor(config: ElevenLabsAgentConfig) {
    this.apiKey = config.apiKey || '';
    this.agentId = config.agentId || null;
    this.enabled = !!this.apiKey;
    this.activationMode = config.activationMode || 'wake_phrase';
    this.defaultActivationDuration = config.activationDuration || 30;
    this.region = config.region || process.env.AWS_REGION || 'us-east-1';
    this.strandsLambdaArn = config.strandsLambdaArn || process.env.STRANDS_LAMBDA_ARN;
    if (config.meetingMode) this.meetingMode = config.meetingMode;
    if (config.translatorLanguageA) this.translatorLanguageA = config.translatorLanguageA;
    if (config.translatorLanguageB) this.translatorLanguageB = config.translatorLanguageB;
    
    // Set initial activation state based on mode
    this._isActivated = (this.activationMode === 'always_active');
    // Group mode starts muted; translator/normal start unmuted
    this._isMuted = (this.meetingMode === 'group');

    if (this.enabled) {
      log.info('✓ ElevenLabs Conversational AI agent enabled');
      log.info(`  Activation mode: ${this.activationMode}`);
      log.info(`  Meeting mode: ${this.meetingMode}`);
      if (this.meetingMode === 'translator') {
        log.info(`  🌐 Translator mode: ${this.translatorLanguageA} ↔ ${this.translatorLanguageB}`);
      } else if (this.meetingMode === 'group') {
        log.info('  Group meeting mode: enabled (starts muted)');
      }
      if (this.strandsLambdaArn) {
        log.info(`  Strands agent tool: enabled`);
      }
    } else {
      log.info('ElevenLabs agent disabled - no API key provided');
    }
  }

  async start(): Promise<void> {
    log.info(`ElevenLabs agent start() called - enabled: ${this.enabled}, apiKey: ${this.apiKey ? 'SET' : 'NOT SET'}`);
    
    if (!this.enabled) {
      log.info('ElevenLabs agent disabled - skipping (no API key)');
      return;
    }

    try {
      log.info('Starting ElevenLabs Conversational AI agent...');
      log.info(`Agent ID: ${this.agentId || '(using default agent)'}`);
      log.info(`Activation mode: ${this.activationMode}`);

      // Load shared voice-assistant configuration from DynamoDB (the Nova Sonic
      // settings are shared across providers for meeting-mode / translator settings).
      const configTables = novaSonicConfigTablesFromEnv();
      if (hasNovaSonicConfigTable(configTables)) {
        try {
          const dynamoDbClient = new DynamoDBClient({
            region: this.region,
            credentials: defaultProvider(),
          });
          const config = await loadNovaSonicConfig(dynamoDbClient, configTables);
          log.info('✓ Loaded voice-assistant config from DynamoDB');

          if (config.meetingMode) {
            this.meetingMode = config.meetingMode;
          } else if (typeof config.groupMeetingMode === 'boolean') {
            this.meetingMode = config.groupMeetingMode ? 'group' : 'normal';
          }
          this._isMuted = (this.meetingMode === 'group');

          if (config.translatorLanguageA && config.translatorLanguageA.trim() !== '') {
            this.translatorLanguageA = config.translatorLanguageA.trim();
          }
          if (config.translatorLanguageB && config.translatorLanguageB.trim() !== '') {
            this.translatorLanguageB = config.translatorLanguageB.trim();
          }

          log.info(`  Meeting mode: ${this.meetingMode}`);
          if (this.meetingMode === 'translator') {
            log.info(`  🌐 Translator mode: ${this.translatorLanguageA} ↔ ${this.translatorLanguageB}`);
            if (this.activationMode !== 'always_active') {
              log.warn(`  ⚠️  Translator mode requires activation mode 'always_active' to work end-to-end. Current: ${this.activationMode}`);
            }
          }
        } catch (error) {
          log.error('Failed to load voice-assistant config from DynamoDB:', error);
          log.info('Using constructor configuration as fallback');
        }
      }

      // Initialize Lambda client if Strands Lambda ARN is configured
      if (this.strandsLambdaArn) {
        this.lambdaClient = new LambdaClient({
          region: this.region,
          credentials: defaultProvider(),
        });
        log.info('✓ Lambda client initialized for Strands agent tool');
      }

      // In wake_phrase mode, defer WebSocket connection until activation
      if (this.activationMode === 'wake_phrase') {
        log.info('⏸️  Wake phrase mode - WebSocket will connect on first activation');
        log.info('   This saves costs by not keeping connection open when not in use');
        return;
      }

      // For always_active mode, connect immediately
      await this.connectWebSocket();

      log.info('✓ ElevenLabs agent started successfully');
    } catch (error) {
      log.error('Failed to start ElevenLabs agent:', error);
      log.error('Error details:', error);
      throw error;
    }
  }

  private async connectWebSocket(): Promise<void> {
    return new Promise((resolve, reject) => {
      try {
        // ElevenLabs Conversational AI WebSocket endpoint
        // Request 24kHz output format explicitly (default might be different)
        const outputFormat = process.env.ELEVENLABS_OUTPUT_FORMAT || 'pcm_16000';
        const wsUrl = this.agentId
          ? `wss://api.elevenlabs.io/v1/convai/conversation?agent_id=${this.agentId}&output_format=${outputFormat}`
          : `wss://api.elevenlabs.io/v1/convai/conversation?output_format=${outputFormat}`;

        log.info(`Connecting to ElevenLabs WebSocket: ${wsUrl}`);
        log.info(`Output format: ${outputFormat}`);

        this.ws = new WebSocket(wsUrl, {
          headers: {
            'xi-api-key': this.apiKey,
          },
        });

        this.ws.on('open', () => {
          log.info('✓ Connected to ElevenLabs Conversational AI');
          this.isConnected = true;
          // A turn heard on an earlier connection must not let a lookup through on this one.
          this.lastUserTranscript = '';

          // Send initial configuration. The ElevenLabs agent's persona (system
          // prompt, first message, tools, language, voice) is configured in the
          // ElevenLabs dashboard — LMA does NOT override the prompt here.
          //
          // For 'translator' mode, users should configure their ElevenLabs agent
          // as a bidirectional interpreter between their two languages in the
          // ElevenLabs dashboard, and set LMA's Meeting Mode to 'translator' +
          // Activation Mode to 'always_active' so LMA keeps the connection open
          // and doesn't apply group-mode mute gating.
          //
          // For 'group' mode, the agent stays muted until a wake phrase is
          // detected in the user transcript (handled below in user_transcript).
          this.ws?.send(
            JSON.stringify({
              type: 'conversation_initiation_client_data',
            })
          );

          resolve();
        });

        this.ws.on('message', async (data: Buffer) => {
          try {
            const message = JSON.parse(data.toString());
            log.debug('📨 WebSocket message:', JSON.stringify(message).substring(0, 200));
            await this.handleWebSocketMessage(message);
          } catch (error) {
            log.info('📨 Binary data received:', data.length, 'bytes');
            // Binary audio data - play it
            await this.playAudio(data);
          }
        });

        this.ws.on('error', (error) => {
          log.error('❌ WebSocket error:', error);
          log.error('   Error details:', JSON.stringify(error));
          this.isConnected = false;
          reject(error);
        });

        const socket = this.ws;
        this.ws.on('close', (code, reason) => {
          // stop() and the wake-phrase deactivation clear this.ws as they close
          // the socket, so a socket that is still this.ws closed unexpectedly.
          const unexpected = this.ws === socket;
          const line = `${unexpected ? '❌' : '🔌'} WebSocket connection closed (code ${code}, reason: ${reason || 'none'})`;
          if (unexpected) log.warn(line);
          else log.info(line);
          this.isConnected = false;
        });
      } catch (error) {
        reject(error);
      }
    });
  }

  private async handleWebSocketMessage(message: any): Promise<void> {
    log.info('Received message:', message.type);
    
    // Log full message for debugging tool calls
    if (message.type && (message.type.includes('tool') || message.type === 'client_tool_call')) {
      log.debug('📋 Full tool message:', JSON.stringify(message));
    }

    switch (message.type) {
      case 'conversation_initiation_metadata':
        const convId = message.conversation_initiation_metadata_event?.conversation_id || message.conversation_id;
        log.info('✓ Conversation initialized:', convId);
        // Start capturing audio from meeting and sending to agent
        this.startAudioCapture();
        break;

      case 'audio':
        // Audio response from agent - play it in the meeting
        log.info('🔊 Received audio response from agent');
        if (message.audio_event?.audio_base_64) {
          const audioBuffer = Buffer.from(message.audio_event.audio_base_64, 'base64');
          log.info(`   Audio size: ${audioBuffer.length} bytes`);
          await this.playAudio(audioBuffer);
        }
        break;

      case 'agent_response':
        log.debug('📝 Agent text response:', message.agent_response_event?.agent_response);
        break;

      case 'user_transcript': {
        const userText: string = message.user_transcription_event?.user_transcript || '';
        log.debug('👤 Agent heard:', userText);
        if (userText) {
          this.lastUserTranscript = userText;
          this.lastUserTranscriptAt = Date.now();
        }
        // Being addressed again re-arms a running activation window. The scribe
        // does not activate an assistant that is already activated, so for a
        // follow-up inside the window this is the only place it is noticed.
        if (this.activationMode !== 'always_active' && this._isActivated && userText
          && this.containsWakePhrase(userText)) {
          this.activationWindow.rearm('addressed again');
        }
        // Group-meeting wake-phrase handling for ElevenLabs (parity with Nova).
        // In translator mode we don't gate on wake phrases — every utterance
        // triggers a translation.
        if (this.chatMuted) {
          // Muted from chat: only "LMA unmute" brings it back.
        } else if (this.meetingMode === 'group' && userText && this.containsWakePhrase(userText)) {
          // "Hey Alex, stop talking" contains the wake phrase, so without this
          // guard the utterance that asked for silence would itself make the
          // agent audible again.
          const askedToStop = isStopTalkingRequest(userText, configuredWakePhrases());
          if (this._isMuted && !askedToStop) {
            log.info('🔓 Wake phrase detected in user transcript - proactively unmuting agent');
            this._isMuted = false;
            if (!this.conversationActive) this.startConversationSession();
          }
          this.resetConversationTimeout();
        } else if (this.meetingMode === 'normal' && userText && this.containsWakePhrase(userText)) {
          // Normal mode: a fresh wake phrase is how the agent becomes audible
          // again after a stop request, which matters most in always_active
          // mode where activate() is never called. No conversation timeout is
          // armed here: the auto-mute timer belongs to group mode, which is the
          // only mode that returns to muted on its own, and
          // resetConversationTimeout() ignores every other mode anyway.
          if (this._isMuted && !isStopTalkingRequest(userText, configuredWakePhrases())) {
            this.unmute('wake phrase in user transcript');
          }
        }
        break;
      }

      case 'interruption':
        log.info('⚠️  Agent interrupted');
        // Somebody talking over the assistant is talking to it, so the idle
        // countdown restarts and a correction is not cut off by the window.
        this.activationWindow.noteEngagement();
        break;

      case 'ping':
        // Respond to ping to keep connection alive
        this.ws?.send(JSON.stringify({ type: 'pong', event_id: message.ping_event?.event_id }));
        break;

      case 'client_tool_call':
        // Handle client tool calls (ElevenLabs client-side tools)
        log.info('🔧 Client tool call received:', message.tool_name);
        await this.handleClientToolCall(message);
        break;

      default:
        log.info('📨 Unknown message type:', message.type);
        log.debug('📨 Unknown message:', JSON.stringify(message).substring(0, 200));
    }
  }

  /**
   * Handle client tool calls from ElevenLabs agent
   */
  private async handleClientToolCall(message: any): Promise<void> {
    // Extract tool call details from the message structure
    const toolCall = message.client_tool_call;
    const toolName = toolCall.tool_name;
    const toolCallId = toolCall.tool_call_id;
    const parameters = toolCall.parameters || {};

    log.info(`Processing client tool: ${toolName}`);
    log.info(`Tool call ID: ${toolCallId}`);
    log.info(`Parameters:`, JSON.stringify(parameters));

    // A muted agent that was not addressed was not asked for anything, and its
    // answer would be dropped; see muted-lookup.ts.
    if (toolName === 'strands_agent') {
      const skip = mutedLookupSkipReason(this._isMuted, this.lastUserTranscript);
      if (skip) {
        log.info(skippedLookupLogLine(skip, this.meetingMode, this.activationMode));
        try {
          await this.sendClientToolResult(toolCallId, SKIPPED_LOOKUP_RESULT);
        } catch (err) {
          log.warn('Failed to send the skipped-lookup tool result (non-fatal):', err);
        }
        return;
      }
    }

    try {
      // Holds the activation window open until the lookup finishes.
      this.toolCallsInFlight++;

      let result: any;

      // Route to appropriate tool handler
      if (toolName === 'strands_agent') {
        result = await this.invokeStrandsAgent(parameters);
      } else {
        throw new Error(`Unknown tool: ${toolName}`);
      }

      // Send tool result back to ElevenLabs
      await this.sendClientToolResult(toolCallId, result);
      log.info('✓ Tool result sent to ElevenLabs');

    } catch (error) {
      log.error('Error processing client tool call:', error);
      // Send error result back to ElevenLabs
      await this.sendClientToolResult(toolCallId, {
        error: 'Failed to process tool request',
        details: error instanceof Error ? error.message : String(error)
      });
    } finally {
      this.toolCallsInFlight = Math.max(0, this.toolCallsInFlight - 1);
      // The window counts its idle time from here at the earliest, so a lookup
      // that took most of the window still leaves the whole of it for the answer.
      this.activationWindow.noteEngagement();
    }
  }

  /**
   * Invoke the Strands Lambda agent (similar to Nova agent implementation)
   */
  private async invokeStrandsAgent(parameters: any): Promise<any> {
    if (!this.lambdaClient || !this.strandsLambdaArn) {
      throw new Error('Strands Lambda not configured');
    }

    log.info('Invoking Strands agent Lambda...');
    
    const query = parameters.query;
    if (!query) {
      throw new Error('Missing required parameter: query');
    }
    
    log.info(`Query for Strands agent: ${String(query ?? '').length} characters`);
    log.debug(`Query for Strands agent: ${query}`);

    // A request spoken to the voice assistant in the meeting: answered as the
    // Meeting Assistant section's In-meeting requests setting allows
    // (meeting-assist-request.ts).
    const payload = inMeetingRequestPayload(query, 'voice_assistant', {
      fallbackCallId: 'elevenlabs-voice-agent',
      fallbackUser: 'ElevenLabsAgent',
    });

    log.info('Strands agent payload:', JSON.stringify(payload));

    // Invoke the Strands Lambda function synchronously
    const command = new InvokeCommand({
      FunctionName: this.strandsLambdaArn,
      InvocationType: 'RequestResponse',
      Payload: JSON.stringify(payload),
    });

    const response = await this.lambdaClient.send(command);
    
    // Parse the Lambda response
    const responsePayload = JSON.parse(new TextDecoder().decode(response.Payload));
    
    log.info(`Strands agent response received (status ${response.StatusCode ?? 'unknown'})`);
    log.debug('Strands agent response:', JSON.stringify(responsePayload).substring(0, 200));
    
    // The answer is written for the meeting chat, in markdown, and the
    // ElevenLabs agent speaks it: reduced to spoken text, as the Nova Sonic
    // agent does, so that markup and link addresses are not read out. A
    // response without a written answer is passed on as JSON.
    return spokenStrandsAnswer(responsePayload);
  }

  /**
   * Send client tool result back to ElevenLabs
   */
  private async sendClientToolResult(toolCallId: string, result: any): Promise<void> {
    if (!this.ws || !this.isConnected) {
      throw new Error('WebSocket not connected');
    }

    log.info(`Sending client tool result for tool call ID: ${toolCallId}`);

    // Format result as string if needed
    const resultString = typeof result === 'string' ? result : JSON.stringify(result);

    // Send tool result to ElevenLabs
    this.ws.send(JSON.stringify({
      type: 'client_tool_result',
      tool_call_id: toolCallId,
      result: resultString,
      is_error: false
    }));

    log.info('Client tool result sent');
  }

  // Method to receive audio chunks from transcription service
  private audioChunkCount = 0;
  
  sendAudioChunk(audioChunk: Buffer): void {
    // Check activation mode - don't send if not activated
    if (this.activationMode !== 'always_active' && !this._isActivated) {
      return;
    }
    
    // Don't send audio to agent when agent is speaking (prevent feedback loop).
    // Exception: in translator mode we still send audio while speaking, so that
    // the interpreter can pick up the reply from the other party immediately.
    // Audio feedback is prevented by PulseAudio routing (agent_output sink is
    // separate from the default mic source), not by this gate.
    if (this._isSpeaking && this.meetingMode !== 'translator') {
      return;
    }
    
    if (this.isConnected && this.ws?.readyState === WebSocket.OPEN) {
      this.audioChunkCount++;
      if (this.audioChunkCount % 100 === 0) {
        log.info(`📡 Sent ${this.audioChunkCount} audio chunks to agent (${audioChunk.length} bytes each)`);
      }
      // Send audio to ElevenLabs agent using correct format from docs
      const audioBase64 = audioChunk.toString('base64');
      this.ws.send(
        JSON.stringify({
          user_audio_chunk: audioBase64,
        })
      );
    }
  }

  /**
   * Send a text message to the agent (agent will respond with voice)
   * This is used for wake phrase activation with initial context
   */
  public sendUserMessage(text: string): void {
    if (!this.ws || !this.isConnected) {
      log.error('Cannot send message: WebSocket is not connected');
      return;
    }

    log.info(`📤 Sending text message to agent (${text.length} characters)`);
    log.debug('📤 Text message to agent:', text);
    
    // Correct format per ElevenLabs API docs
    const message = {
      type: 'user_message',
      text: text  // Use 'text' not 'user_message'
    };

    this.ws.send(JSON.stringify(message));
  }

  private startAudioCapture(): void {
    log.info('Audio capture will be provided by transcription service');
    // Audio chunks will be sent via sendAudioChunk() method
    // No need to create separate FFmpeg process
  }

  private async playAudio(audioBuffer: Buffer): Promise<void> {
    // Group-meeting mode: drop audio from the agent while muted. This mirrors
    // the Nova agent and gives us wake-phrase-gated speech for ElevenLabs too.
    if (this._isMuted) {
      log.info('🔇 Audio dropped - agent is muted (group meeting mode)');
      return;
    }

    // Add audio to queue
    this.audioQueue.push(audioBuffer);
    log.info(`🎵 Audio chunk queued (${audioBuffer.length} bytes) - Queue size: ${this.audioQueue.length}`);
    
    // Forward audio to Simli avatar for lip-sync animation
    // This runs in parallel with PulseAudio playback - Simli only uses it for video
    if (simliAvatar.isConnected()) {
      simliAvatar.sendAudioData(audioBuffer).catch(err => {
        // Non-critical - avatar lip-sync failure shouldn't affect audio
      });
    }
    
    // Start processing queue if not already processing
    if (!this.isPlayingQueue) {
      this.processAudioQueue();
    }
  }

  private async processAudioQueue(): Promise<void> {
    if (this.isPlayingQueue || this.audioQueue.length === 0) {
      return;
    }

    this.isPlayingQueue = true;
    this._isSpeaking = true;

    while (this.audioQueue.length > 0) {
      const audioBuffer = this.audioQueue.shift()!;
      
      log.info(`🎵 Playing audio chunk (${audioBuffer.length} bytes) - ${this.audioQueue.length} remaining in queue`);
      
      try {
        await this.playChunk(audioBuffer);
      } catch (error) {
        // One chunk failing must not leave the queue marked as playing for
        // good, which would also hold the activation window open.
        log.error('❌ Audio chunk playback failed (continuing with the queue):', error);
      }
    }

    // Clear speaking flag after all audio is played
    setTimeout(() => {
      this._isSpeaking = false;
      this.isPlayingQueue = false;
      this.playbackEndedAt = Date.now();
      log.info('✅ All audio chunks played to virtual microphone');
      // A chunk that arrived during this delay found the queue still marked as
      // playing and was only queued, so it is picked up here.
      if (this.audioQueue.length > 0) {
        this.processAudioQueue();
      }
    }, 500); // Short delay after last chunk
  }

  /** Play one chunk to the agent_output sink; resolves when paplay exits. */
  private playChunk(audioBuffer: Buffer): Promise<void> {
    // Allow rate adjustment via environment variable
    const playbackRate = parseInt(process.env.ELEVENLABS_PLAYBACK_RATE || '16000');

    return new Promise<void>((resolve, reject) => {
      // Play audio at specified rate
      const paplay = spawn('paplay', [
        '--device=agent_output',
        '--format=s16le',
        '--rate=' + playbackRate,
        '--channels=1',
        '--raw'
      ]);

      // Handle stdin errors (EPIPE when paplay exits before write completes)
      paplay.stdin.on('error', (error: any) => {
        if (error.code === 'EPIPE' || error.code === 'ERR_STREAM_DESTROYED') {
          // Expected during cleanup - paplay exited before we finished writing
          resolve();
        } else {
          log.error('❌ paplay stdin error:', error);
          reject(error);
        }
      });

      // Send audio to paplay
      paplay.stdin.write(audioBuffer);
      paplay.stdin.end();

      // Handle paplay completion
      paplay.on('close', (code) => {
        if (code === 0) {
          log.info('✅ Audio chunk played');
        }
        // During cleanup, non-zero exit is expected
        resolve();
      });

      paplay.on('error', (error) => {
        log.error('❌ paplay error:', error);
        reject(error);
      });

      paplay.stderr.on('data', (data) => {
        const msg = data.toString();
        if (msg.trim()) {
          log.info(`paplay: ${msg.trim()}`);
        }
      });
    });
  }

  async stop(): Promise<void> {
    log.info('Stopping ElevenLabs agent...');

    this.activationWindow.stop();

    if (this.audioProcess) {
      this.audioProcess.kill();
      this.audioProcess = null;
    }

    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }

    this.isConnected = false;
    log.info('✓ ElevenLabs agent stopped');
  }

  private _preConnecting: boolean = false;
  private _preConnectPromise: Promise<void> | null = null;

  async preConnect(): Promise<void> {
    if (this.activationMode === 'always_active') return; // already connected
    if (this.isConnected) return; // already connected
    if (this.chatMuted) return; // will not be activated (setMutedByChat)
    if (this._preConnecting) {
      if (this._preConnectPromise) await this._preConnectPromise;
      return;
    }

    log.info('🔌 Pre-connecting ElevenLabs WebSocket (wake phrase detected in partial)...');
    this._preConnecting = true;
    this._preConnectPromise = this.connectWebSocket()
      .then(() => {
        log.info('✓ ElevenLabs WebSocket pre-connected — ready for activation');
      })
      .catch((error) => {
        log.error('❌ ElevenLabs pre-connect failed:', error);
      })
      .finally(() => {
        this._preConnecting = false;
        this._preConnectPromise = null;
      });

    await this._preConnectPromise;
  }

  // Activation control methods
  async activate(duration?: number, initialContext?: string): Promise<void> {
    if (this.activationMode === 'always_active') {
      // Already always active, no need to activate
      return;
    }

    if (this.chatMuted) {
      log.info('[voice-control] wake phrase ignored - the voice assistant is muted from chat');
      return;
    }

    const activationDuration = duration || this.defaultActivationDuration;
    log.info(`🎤 Voice assistant activated (closes after ${activationDuration} seconds idle)`);
    
    // Connect WebSocket if not already connected (may already be pre-connected)
    if (!this.isConnected) {
      if (this._preConnecting && this._preConnectPromise) {
        log.info('⏳ Waiting for in-flight pre-connect to complete...');
        await this._preConnectPromise;
      }
      if (!this.isConnected) {
        log.info('🔌 Connecting to ElevenLabs WebSocket...');
        try {
          await this.connectWebSocket();
          log.info('✓ WebSocket connected');
        } catch (error) {
          log.error('❌ Failed to connect WebSocket:', error);
          return; // Don't activate if connection fails
        }
      }
    } else {
      log.info('✓ WebSocket already connected (pre-connected)');
    }
    
    this._isActivated = true;

    // Being addressed by wake phrase is how the agent becomes audible again
    // after it was asked to stop talking. Only in `normal` mode: `group` owns
    // its own muted-until-addressed state machine and `translator` is driven
    // by trigger phrases, and both run always_active where this method returns
    // above.
    if (this.meetingMode === 'normal' && this._isMuted) {
      this.unmute('wake phrase activation');
    }

    // Handle initial context by sending as text message
    if (initialContext) {
      const question = this.extractQuestion(initialContext);
      if (question && question.length > 5) {
        log.info(`📝 Sending initial question to agent (${question.length} characters)`);
        log.debug('📝 Initial question to agent:', question);
        // Send as text message - agent will respond with voice
        this.sendUserMessage(question);
      }
    }

    // The window is an idle timeout, not a deadline: see voice-activation-window.ts.
    this.activationWindow.start(activationDuration * 1000);
  }

  private extractQuestion(context: string): string {
    // Remove all punctuation first, then remove wake phrases
    let cleaned = context.toLowerCase()
      .replace(/[,.\?!;:]/g, ' ')  // Replace punctuation with spaces
      .replace(/\s+/g, ' ')         // Normalize multiple spaces
      .trim();
    
    // Remove wake phrases
    const wakePhrases = ['hey alex', 'ok alex', 'hi alex', 'hello alex'];
    for (const phrase of wakePhrases) {
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

    log.info('🔇 Voice assistant deactivated');
    this._isActivated = false;
    this.activationWindow.stop();

    // A closed connection cannot hear a participant ask it to stop, so no
    // queued assistant audio is played after it. The window does not lapse
    // while audio is queued, so this only acts on a direct deactivate().
    if (this.activationMode === 'wake_phrase' && this.audioQueue.length > 0) {
      log.info(`🔇 Dropping ${this.audioQueue.length} queued audio chunks as the connection closes`);
      this.audioQueue = [];
    }

    // In wake_phrase mode, disconnect WebSocket to save costs
    if (this.activationMode === 'wake_phrase' && this.ws) {
      log.info('💰 Disconnecting WebSocket to save costs (wake_phrase mode)');
      this.ws.close();
      this.ws = null;
      this.isConnected = false;
    }
  }

  // ---- Group-meeting wake-phrase helpers (parity with Nova agent) ----

  private containsWakePhrase(text: string): boolean {
    const wakePhrases = configuredWakePhrases();
    const normalized = foldWakeSpellings(text.toLowerCase()
      .replace(/[,.\?!;:'"]/g, ' ')
      .replace(/\s+/g, ' ')
      .trim());
    return wakePhrases.some((phrase: string) => {
      const escapedPhrase = foldWakeSpellings(phrase).replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/\s+/g, '\\s+');
      // nosemgrep: javascript.lang.security.audit.detect-non-literal-regexp.detect-non-literal-regexp -- Inputs are regex-escaped above; escaped wake-phrase cannot produce catastrophic backtracking
      const regex = new RegExp(`\\b${escapedPhrase}\\b`, 'i');
      return regex.test(normalized);
    });
  }

  private startConversationSession(): void {
    if (this.meetingMode !== 'group') return;
    log.info('💬 Starting conversation session (group mode)');
    this.conversationActive = true;
    this.clearConversationTimeout();
    const timeoutMs = this.defaultActivationDuration * 1000;
    this.conversationTimeout = setTimeout(() => {
      log.info(`⏰ Conversation timeout (${this.defaultActivationDuration}s) - re-muting agent`);
      this._isMuted = true;
      this.conversationActive = false;
      this.clearConversationTimeout();
    }, timeoutMs);
  }

  private resetConversationTimeout(): void {
    if (this.meetingMode !== 'group' || !this.conversationActive) return;
    this.clearConversationTimeout();
    const timeoutMs = this.defaultActivationDuration * 1000;
    this.conversationTimeout = setTimeout(() => {
      log.info(`⏰ Conversation timeout (${this.defaultActivationDuration}s) - re-muting agent`);
      this._isMuted = true;
      this.conversationActive = false;
      this.clearConversationTimeout();
    }, timeoutMs);
  }

  private clearConversationTimeout(): void {
    if (this.conversationTimeout) {
      clearTimeout(this.conversationTimeout);
      this.conversationTimeout = null;
    }
  }

  /** The agent's prompt and tools live in the ElevenLabs dashboard, so reconfigure declines every request. */
  supportsModeChanges(): boolean {
    return false;
  }

  isEnabled(): boolean {
    return this.enabled;
  }

  isActive(): boolean {
    return this.isConnected;
  }

  isActivated(): boolean {
    return this._isActivated;
  }

  isSpeaking(): boolean {
    return this._isSpeaking;
  }

  /**
   * Mute the agent — drops all outgoing audio until unmuted. Idempotent.
   * Drains the playback queue so pending chunks are not played.
   */
  mute(reason?: string): void {
    if (this._isMuted) {
      return;
    }
    log.info(`🔇 ElevenLabs agent muted${reason ? ` (${reason})` : ''}`);
    this._isMuted = true;
    this.conversationActive = false;
    if (this.audioQueue.length > 0) {
      log.info(`🔇 Dropping ${this.audioQueue.length} queued audio chunks on mute`);
      this.audioQueue = [];
    }
    // The avatar buffers its own copy of the speech for lip-sync, so without
    // this it keeps mouthing the rest of an utterance nobody can hear.
    if (simliAvatar.isConnected()) {
      simliAvatar.clearAudioBuffer().catch(() => {});
    }
  }

  /**
   * Unmute the agent — resumes outgoing audio. Idempotent.
   */
  unmute(reason?: string): void {
    if (!this._isMuted) {
      return;
    }
    log.info(`🔓 ElevenLabs agent unmuted${reason ? ` (${reason})` : ''}`);
    this._isMuted = false;
    if (!this.conversationActive) {
      this.conversationActive = true;
    }
  }

  isMuted(): boolean {
    return this._isMuted;
  }

  /** Set by "LMA mute" in chat; only "LMA unmute" clears it (setMutedByChat). */
  private chatMuted = false;

  /**
   * "LMA mute" / "LMA unmute" in chat (see
   * VoiceAssistantProvider.setMutedByChat). Muted holds through wake phrases
   * until unmuted from chat, and ends a wake-phrase activation at once.
   */
  setMutedByChat(muted: boolean): VoiceControlOutcome {
    const state = (): VoiceControlState => {
      if (this.chatMuted) return 'muted';
      if (this.meetingMode === 'group') return 'listen-mode';
      return this.activationMode === 'always_active' ? 'listening' : 'listening-for-wake-phrase';
    };
    if (muted) {
      if (this.chatMuted) return { result: 'unchanged', state: state() };
      this.chatMuted = true;
      this.mute('muted from chat');
      if (this.activationMode === 'wake_phrase' && this._isActivated) {
        this.deactivate();
      }
      return { result: 'applied', state: state() };
    }
    if (!this.chatMuted && !this._isMuted) return { result: 'unchanged', state: state() };
    this.chatMuted = false;
    if (this.meetingMode !== 'group') this.unmute('unmuted from chat');
    return { result: 'applied', state: state() };
  }

  /**
   * Act on a participant asking the agent to stop talking (see
   * VoiceAssistantProvider.requestStopTalking).
   *
   * This is the only stop mechanism available on this provider: the ElevenLabs
   * agent's prompt and tools are configured in the ElevenLabs dashboard, so LMA
   * cannot register a `mute` tool or amend its instructions the way it can for
   * Nova Sonic. In `normal` mode the agent becomes audible again the next time
   * it is addressed by wake phrase.
   */
  requestStopTalking(reason?: string): boolean {
    if (!this.enabled) {
      return false;
    }
    // Translator mode declines, matching Nova Sonic: its pause/resume belongs
    // to the trigger-phrase state machine, not to this route.
    if (this.meetingMode === 'translator') {
      log.info('🌐 Translator mode - stop requests are only taken from the configured pause phrase');
      this.logStopRequest('declined-translator');
      return false;
    }

    // There is nothing to defer for. LMA cannot give this provider a `mute`
    // tool or amend its prompt — both live in the ElevenLabs dashboard — so
    // there is no tool call to wait for, and the transcript route is the whole
    // mechanism and acts at once. The agent may well still be speaking at that
    // moment; what it says is just not something LMA can ask for.
    this.logStopRequest('muted-immediately');
    this.mute(reason || 'stop requested');
    return true;
  }

  /** One line per stop-request decision, in the same family Nova Sonic writes,
   *  so that one CloudWatch filter covers both providers — including the
   *  outcomes where nothing is muted. Carries the flags, never the utterance,
   *  which is participant speech. */
  private logStopRequest(action: string): void {
    log.info(
      `[stop-request] route=transcript action=${action} provider=elevenlabs`
      + ` mode=${this.meetingMode} speaking=${this.isSpeaking()} muted=${this._isMuted}`,
    );
  }

  /**
   * Mid-meeting mode changes are declined on this provider.
   *
   * Changing mode means giving the agent a different system prompt and a
   * different tool set, and on ElevenLabs both live in the ElevenLabs dashboard
   * rather than in LMA — reconnecting here would move the mute gating and the
   * wake-phrase handling onto a new mode while the agent itself kept following
   * the instructions it was configured with, which is a worse state than the one
   * the request started from. Declining says so instead.
   */
  async reconfigure(request: AssistantReconfigureRequest): Promise<AssistantReconfigureOutcome> {
    const outcome: AssistantReconfigureOutcome = {
      result: 'declined',
      changed: [],
      sessionRebuilt: false,
      meetingModeBefore: this.meetingMode,
      meetingModeAfter: this.meetingMode,
      activationModeBefore: this.activationMode as ActivationMode,
      activationModeAfter: this.activationMode as ActivationMode,
      detail: 'this voice assistant cannot change mode during a meeting',
    };
    log.info(formatAssistantToggleLog(request, outcome));
    return outcome;
  }
}
