import { spawn, ChildProcess } from 'child_process';
import WebSocket from 'ws';
import { VoiceAssistantProvider } from './voice-assistant-interface.js';

export interface ElevenLabsAgentConfig {
  apiKey: string;
  agentId?: string;
  activationMode?: string;
  activationDuration?: number;
}

export class ElevenLabsAgent implements VoiceAssistantProvider {
  private apiKey: string;
  private agentId: string | null;
  private ws: WebSocket | null = null;
  private audioProcess: ChildProcess | null = null;
  private enabled: boolean;
  private isConnected: boolean = false;
  private _isSpeaking: boolean = false; // Track when agent is playing audio
  private audioQueue: Buffer[] = []; // Queue for audio chunks
  private isPlayingQueue: boolean = false; // Track if queue is being processed
  private activationMode: string;
  private _isActivated: boolean = false;
  private activationTimeout: NodeJS.Timeout | null = null;
  private defaultActivationDuration: number;

  constructor(config: ElevenLabsAgentConfig) {
    this.apiKey = config.apiKey || '';
    this.agentId = config.agentId || null;
    this.enabled = !!this.apiKey;
    this.activationMode = config.activationMode || 'always_active';
    this.defaultActivationDuration = config.activationDuration || 30;
    
    // Set initial activation state based on mode
    this._isActivated = (this.activationMode === 'always_active');

    if (this.enabled) {
      console.log('✓ ElevenLabs Conversational AI agent enabled');
      console.log(`  Activation mode: ${this.activationMode}`);
    } else {
      console.log('ElevenLabs agent disabled - no API key provided');
    }
  }

  async start(): Promise<void> {
    console.log(`ElevenLabs agent start() called - enabled: ${this.enabled}, apiKey: ${this.apiKey ? 'SET' : 'NOT SET'}`);
    
    if (!this.enabled) {
      console.log('ElevenLabs agent disabled - skipping (no API key)');
      return;
    }

    try {
      console.log('Starting ElevenLabs Conversational AI agent...');
      console.log(`Agent ID: ${this.agentId || '(using default agent)'}`);
      console.log(`Activation mode: ${this.activationMode}`);

      // In wake_phrase mode, defer WebSocket connection until activation
      if (this.activationMode === 'wake_phrase') {
        console.log('⏸️  Wake phrase mode - WebSocket will connect on first activation');
        console.log('   This saves costs by not keeping connection open when not in use');
        return;
      }

      // For always_active mode, connect immediately
      await this.connectWebSocket();

      console.log('✓ ElevenLabs agent started successfully');
    } catch (error) {
      console.error('Failed to start ElevenLabs agent:', error);
      console.error('Error details:', error);
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

        console.log(`Connecting to ElevenLabs WebSocket: ${wsUrl}`);
        console.log(`Output format: ${outputFormat}`);

        this.ws = new WebSocket(wsUrl, {
          headers: {
            'xi-api-key': this.apiKey,
          },
        });

        this.ws.on('open', () => {
          console.log('✓ Connected to ElevenLabs Conversational AI');
          this.isConnected = true;

          // Send initial configuration (no overrides - use agent's configured settings)
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
            console.log('📨 WebSocket message:', JSON.stringify(message).substring(0, 200));
            await this.handleWebSocketMessage(message);
          } catch (error) {
            console.log('📨 Binary data received:', data.length, 'bytes');
            // Binary audio data - play it
            await this.playAudio(data);
          }
        });

        this.ws.on('error', (error) => {
          console.error('❌ WebSocket error:', error);
          console.error('   Error details:', JSON.stringify(error));
          this.isConnected = false;
          reject(error);
        });

        this.ws.on('close', (code, reason) => {
          console.log(`❌ WebSocket connection closed`);
          console.log(`   Close code: ${code}`);
          console.log(`   Close reason: ${reason || 'none'}`);
          this.isConnected = false;
        });
      } catch (error) {
        reject(error);
      }
    });
  }

  private async handleWebSocketMessage(message: any): Promise<void> {
    console.log('Received message:', message.type);

    switch (message.type) {
      case 'conversation_initiation_metadata':
        const convId = message.conversation_initiation_metadata_event?.conversation_id || message.conversation_id;
        console.log('✓ Conversation initialized:', convId);
        // Start capturing audio from meeting and sending to agent
        this.startAudioCapture();
        break;

      case 'audio':
        // Audio response from agent - play it in the meeting
        console.log('🔊 Received audio response from agent');
        if (message.audio_event?.audio_base_64) {
          const audioBuffer = Buffer.from(message.audio_event.audio_base_64, 'base64');
          console.log(`   Audio size: ${audioBuffer.length} bytes`);
          await this.playAudio(audioBuffer);
        }
        break;

      case 'agent_response':
        console.log('📝 Agent text response:', message.agent_response_event?.agent_response);
        break;

      case 'user_transcript':
        console.log('👤 Agent heard:', message.user_transcription_event?.user_transcript);
        break;

      case 'interruption':
        console.log('⚠️  Agent interrupted');
        break;

      case 'ping':
        // Respond to ping to keep connection alive
        this.ws?.send(JSON.stringify({ type: 'pong', event_id: message.ping_event?.event_id }));
        break;

      default:
        console.log('📨 Unknown message type:', message.type, JSON.stringify(message).substring(0, 200));
    }
  }

  // Method to receive audio chunks from transcription service
  private audioChunkCount = 0;
  
  sendAudioChunk(audioChunk: Buffer): void {
    // Check activation mode - don't send if not activated
    if (this.activationMode !== 'always_active' && !this._isActivated) {
      return;
    }
    
    // Don't send audio to agent when agent is speaking (prevent feedback loop)
    if (this._isSpeaking) {
      return;
    }
    
    if (this.isConnected && this.ws?.readyState === WebSocket.OPEN) {
      this.audioChunkCount++;
      if (this.audioChunkCount % 100 === 0) {
        console.log(`📡 Sent ${this.audioChunkCount} audio chunks to agent (${audioChunk.length} bytes each)`);
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
      console.error('Cannot send message: WebSocket is not connected');
      return;
    }

    console.log('📤 Sending text message to agent:', text);
    
    // Correct format per ElevenLabs API docs
    const message = {
      type: 'user_message',
      text: text  // Use 'text' not 'user_message'
    };

    this.ws.send(JSON.stringify(message));
  }

  private startAudioCapture(): void {
    console.log('Audio capture will be provided by transcription service');
    // Audio chunks will be sent via sendAudioChunk() method
    // No need to create separate FFmpeg process
  }

  private async playAudio(audioBuffer: Buffer): Promise<void> {
    // Add audio to queue
    this.audioQueue.push(audioBuffer);
    console.log(`🎵 Audio chunk queued (${audioBuffer.length} bytes) - Queue size: ${this.audioQueue.length}`);
    
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
      
      console.log(`🎵 Playing audio chunk (${audioBuffer.length} bytes) - ${this.audioQueue.length} remaining in queue`);
      
      // Allow rate adjustment via environment variable
      const playbackRate = parseInt(process.env.ELEVENLABS_PLAYBACK_RATE || '16000');
      
      await new Promise<void>((resolve, reject) => {
        // Play audio at specified rate
        const paplay = spawn('paplay', [
          '--device=agent_output',
          '--format=s16le',
          '--rate=' + playbackRate,
          '--channels=1',
          '--raw'
        ]);

        // Send audio to paplay
        paplay.stdin.write(audioBuffer);
        paplay.stdin.end();

        // Handle paplay completion
        paplay.on('close', (code) => {
          if (code === 0) {
            console.log('✅ Audio chunk played');
            resolve();
          } else {
            console.error(`❌ paplay failed with code ${code}`);
            reject(new Error(`paplay failed with code ${code}`));
          }
        });

        paplay.on('error', (error) => {
          console.error('❌ paplay error:', error);
          reject(error);
        });

        paplay.stderr.on('data', (data) => {
          const msg = data.toString();
          if (msg.trim()) {
            console.log(`paplay: ${msg.trim()}`);
          }
        });
      });
    }

    // Clear speaking flag after all audio is played
    setTimeout(() => {
      this._isSpeaking = false;
      this.isPlayingQueue = false;
      console.log('✅ All audio chunks played to virtual microphone');
    }, 500); // Short delay after last chunk
  }

  async stop(): Promise<void> {
    console.log('Stopping ElevenLabs agent...');

    // Clear activation timeout
    if (this.activationTimeout) {
      clearTimeout(this.activationTimeout);
      this.activationTimeout = null;
    }

    if (this.audioProcess) {
      this.audioProcess.kill();
      this.audioProcess = null;
    }

    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }

    this.isConnected = false;
    console.log('✓ ElevenLabs agent stopped');
  }

  // Activation control methods
  async activate(duration?: number, initialContext?: string): Promise<void> {
    if (this.activationMode === 'always_active') {
      // Already always active, no need to activate
      return;
    }

    const activationDuration = duration || this.defaultActivationDuration;
    console.log(`🎤 Voice assistant activated for ${activationDuration} seconds`);
    
    // Connect WebSocket if not already connected (for wake_phrase mode)
    if (!this.isConnected) {
      console.log('🔌 Connecting to ElevenLabs WebSocket...');
      try {
        await this.connectWebSocket();
        console.log('✓ WebSocket connected');
      } catch (error) {
        console.error('❌ Failed to connect WebSocket:', error);
        return; // Don't activate if connection fails
      }
    }
    
    this._isActivated = true;

    // Handle initial context by sending as text message
    if (initialContext) {
      const question = this.extractQuestion(initialContext);
      if (question && question.length > 5) {
        console.log('📝 Sending initial question to agent:', question);
        // Send as text message - agent will respond with voice
        this.sendUserMessage(question);
      }
    }

    // Clear any existing timeout
    if (this.activationTimeout) {
      clearTimeout(this.activationTimeout);
    }

    // Set timeout to deactivate (with speaking check)
    this.activationTimeout = setTimeout(() => {
      this.deactivateWithSpeakingCheck();
    }, activationDuration * 1000);
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

  private deactivateWithSpeakingCheck(): void {
    // If agent is speaking, wait for it to finish
    if (this._isSpeaking) {
      console.log('🔊 Agent is speaking, delaying deactivation...');
      setTimeout(() => {
        this.deactivateWithSpeakingCheck();
      }, 1000);
      return;
    }
    
    this.deactivate();
  }

  deactivate(): void {
    if (this.activationMode === 'always_active') {
      // Can't deactivate always_active mode
      return;
    }

    console.log('🔇 Voice assistant deactivated');
    this._isActivated = false;

    // Clear timeout
    if (this.activationTimeout) {
      clearTimeout(this.activationTimeout);
      this.activationTimeout = null;
    }

    // In wake_phrase mode, disconnect WebSocket to save costs
    if (this.activationMode === 'wake_phrase' && this.ws) {
      console.log('💰 Disconnecting WebSocket to save costs (wake_phrase mode)');
      this.ws.close();
      this.ws = null;
      this.isConnected = false;
    }
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
}
