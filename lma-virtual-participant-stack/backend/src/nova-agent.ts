/**
 * AWS Nova Sonic 2 Voice Assistant Provider
 * 
 * Implements AWS Bedrock Bidirectional Stream integration for Nova Sonic 2
 * Supports real-time voice conversation with audio streaming
 */

import { spawn, ChildProcess } from 'child_process';
import { VoiceAssistantProvider } from './voice-assistant-interface.js';
import {
  BedrockRuntimeClient,
  InvokeModelWithBidirectionalStreamCommand,
  InvokeModelWithBidirectionalStreamInput,
} from '@aws-sdk/client-bedrock-runtime';
import { NodeHttp2Handler } from '@smithy/node-http-handler';
import { defaultProvider } from '@aws-sdk/credential-provider-node';
import { randomUUID } from 'crypto';

export interface NovaAgentConfig {
  modelId: string;
  systemPrompt: string;
  knowledgeBaseId?: string;
  activationMode?: string;
  activationDuration?: number;
  region?: string;
}

interface SessionData {
  queue: Array<any>;
  isActive: boolean;
  promptName: string;
  audioContentId: string;
  isPromptStartSent: boolean;
  isAudioContentStartSent: boolean;
}

export class NovaAgent implements VoiceAssistantProvider {
  private modelId: string;
  private systemPrompt: string;
  private knowledgeBaseId?: string;
  private activationMode: string;
  private _isActivated: boolean = false;
  private _isActive: boolean = false;
  private _isSpeaking: boolean = false;
  private audioQueue: Buffer[] = [];
  private isPlayingQueue: boolean = false;
  private activationTimeout: NodeJS.Timeout | null = null;
  private defaultActivationDuration: number;
  private region: string;
  private audioChunkCount: number = 0;
  
  // Bedrock client and session management
  private bedrockClient: BedrockRuntimeClient | null = null;
  private sessionId: string | null = null;
  private session: SessionData | null = null;
  private streamingActive: boolean = false;
  private queueSignal: (() => void) | null = null;
  private closeSignal: (() => void) | null = null;

  constructor(config: NovaAgentConfig) {
    this.modelId = config.modelId;
    this.systemPrompt = config.systemPrompt;
    this.knowledgeBaseId = config.knowledgeBaseId;
    this.activationMode = config.activationMode || 'always_active';
    this.defaultActivationDuration = config.activationDuration || 30;
    this.region = config.region || process.env.AWS_REGION || 'us-east-1';
    
    // Set initial activation state based on mode
    this._isActivated = (this.activationMode === 'always_active');

    console.log('✓ AWS Nova Sonic 2 agent initialized');
    console.log(`  Model: ${this.modelId}`);
    console.log(`  Region: ${this.region}`);
    console.log(`  Activation mode: ${this.activationMode}`);
  }

  async start(): Promise<void> {
    console.log('Starting AWS Nova Sonic 2 agent...');
    
    try {
      // Initialize Bedrock client
      const nodeHttp2Handler = new NodeHttp2Handler({
        requestTimeout: 300000,
        sessionTimeout: 300000,
        disableConcurrentStreams: false,
        maxConcurrentStreams: 20,
      });

      this.bedrockClient = new BedrockRuntimeClient({
        region: this.region,
        credentials: defaultProvider(),
        requestHandler: nodeHttp2Handler,
      });

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

  private async createSession(): Promise<void> {
    if (this.streamingActive) {
      console.log('Session already active');
      return;
    }

    this.sessionId = randomUUID();
    this.session = {
      queue: [],
      isActive: true,
      promptName: randomUUID(),
      audioContentId: randomUUID(),
      isPromptStartSent: false,
      isAudioContentStartSent: false,
    };

    console.log(`Creating Nova session: ${this.sessionId}`);

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
    await this.setupInitialEvents();
  }

  private async setupInitialEvents(): Promise<void> {
    if (!this.session) return;

    console.log('Setting up initial events...');

    // Session start
    this.addEventToQueue({
      event: {
        sessionStart: {
          inferenceConfiguration: {
            maxTokens: 1024,
            topP: 0.9,
            temperature: 0.7,
          },
        },
      },
    });

    // Prompt start
    this.addEventToQueue({
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
            voiceId: 'tiffany',
          },
        },
      },
    });
    this.session.isPromptStartSent = true;

    // System prompt
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

    this.addEventToQueue({
      event: {
        textInput: {
          promptName: this.session.promptName,
          contentName: textPromptID,
          content: this.systemPrompt,
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

  private addEventToQueue(event: any): void {
    if (!this.session || !this.session.isActive) return;
    
    this.session.queue.push(event);
    if (this.queueSignal) {
      this.queueSignal();
    }
  }

  private async *generateEventStream(): AsyncIterable<InvokeModelWithBidirectionalStreamInput> {
    if (!this.session) return;

    const textEncoder = new TextEncoder();

    while (this.session.isActive) {
      // Wait for events in queue or close signal
      if (this.session.queue.length === 0) {
        await new Promise<void>(resolve => {
          this.queueSignal = resolve;
          if (this.closeSignal) {
            this.closeSignal = () => {
              this.session!.isActive = false;
              resolve();
            };
          }
        });
      }

      if (!this.session.isActive) break;

      while (this.session.queue.length > 0) {
        const event = this.session.queue.shift();
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

  private async initiateBidirectionalStreaming(): Promise<void> {
    if (!this.bedrockClient || !this.session) return;

    try {
      console.log('Starting bidirectional stream...');

      const command = new InvokeModelWithBidirectionalStreamCommand({
        modelId: this.modelId,
        body: this.generateEventStream(),
      });

      const response = await this.bedrockClient.send(command);

      console.log('Stream established, processing responses...');

      // Process response stream
      await this.processResponseStream(response);

    } catch (error) {
      console.error('Error in bidirectional streaming:', error);
      this._isActive = false;
      this.streamingActive = false;
    }
  }

  private async processResponseStream(response: any): Promise<void> {
    const textDecoder = new TextDecoder();

    try {
      for await (const event of response.body) {
        if (!this.session?.isActive) break;

        if (event.chunk?.bytes) {
          const textResponse = textDecoder.decode(event.chunk.bytes);

          try {
            const jsonResponse = JSON.parse(textResponse);

            if (jsonResponse.event?.contentStart) {
              console.log('Content start:', jsonResponse.event.contentStart.type);
            } else if (jsonResponse.event?.textOutput) {
              console.log('Text output:', jsonResponse.event.textOutput.content);
            } else if (jsonResponse.event?.audioOutput) {
              // Received audio from Nova
              const audioBuffer = Buffer.from(jsonResponse.event.audioOutput.content, 'base64');
              await this.playAudio(audioBuffer);
            } else if (jsonResponse.event?.contentEnd) {
              console.log('Content end:', jsonResponse.event.contentEnd.type);
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

  sendAudioChunk(chunk: Buffer): void {
    // Check activation mode - don't send if not activated
    if (this.activationMode !== 'always_active' && !this._isActivated) {
      return;
    }
    
    // Don't send audio to agent when agent is speaking (prevent feedback loop)
    if (this._isSpeaking) {
      return;
    }
    
    if (!this._isActive || !this.session || !this.session.isActive) {
      return;
    }

    this.audioChunkCount++;
    if (this.audioChunkCount % 100 === 0) {
      console.log(`📡 Sent ${this.audioChunkCount} audio chunks to Nova (${chunk.length} bytes each)`);
    }

    // Send audio chunk to Nova Sonic 2
    this.addEventToQueue({
      event: {
        audioInput: {
          promptName: this.session.promptName,
          contentName: this.session.audioContentId,
          content: chunk.toString('base64'),
        },
      },
    });
  }

  sendUserMessage(text: string): void {
    if (!this._isActive || !this.session || !this.session.isActive) {
      console.error('Cannot send message: Session not active');
      return;
    }

    console.log('📤 Sending text message to Nova:', text);
    
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

  private async playAudio(audioBuffer: Buffer): Promise<void> {
    // Add audio to queue
    this.audioQueue.push(audioBuffer);
    
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

    // Batch audio chunks to reduce choppiness
    const batchSize = 10; // Process 10 chunks at a time for smoother playback
    const playbackRate = parseInt(process.env.NOVA_PLAYBACK_RATE || '16000');
    
    while (this.audioQueue.length > 0) {
      // Collect multiple chunks into a batch
      const batch: Buffer[] = [];
      for (let i = 0; i < batchSize && this.audioQueue.length > 0; i++) {
        batch.push(this.audioQueue.shift()!);
      }
      
      // Concatenate batch into single buffer
      const combinedBuffer = Buffer.concat(batch);
      
      // Play audio at 16kHz (Nova Sonic 2 output format)
      
      await new Promise<void>((resolve, reject) => {
        const paplay = spawn('paplay', [
          '--device=agent_output',
          '--format=s16le',
          '--rate=' + playbackRate,
          '--channels=1',
          '--raw'
        ]);

        // Send audio to paplay
        paplay.stdin.write(combinedBuffer);
        paplay.stdin.end();

        // Handle paplay completion
        paplay.on('close', (code: number) => {
          if (code === 0) {
            resolve();
          } else {
            console.error(`❌ paplay failed with code ${code}`);
            reject(new Error(`paplay failed with code ${code}`));
          }
        });

        paplay.on('error', (error: Error) => {
          console.error('❌ paplay error:', error);
          reject(error);
        });

        paplay.stderr.on('data', (data: Buffer) => {
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

  async activate(duration?: number, initialContext?: string): Promise<void> {
    if (this.activationMode === 'always_active') {
      // Already always active, no need to activate
      return;
    }

    const activationDuration = duration || this.defaultActivationDuration;
    console.log(`🎤 Nova agent activated for ${activationDuration} seconds`);
    
    // Create session if not already active (for wake_phrase mode)
    if (!this._isActive) {
      console.log('🔌 Creating Nova session...');
      try {
        await this.createSession();
        console.log('✓ Session created');
      } catch (error) {
        console.error('❌ Failed to create session:', error);
        return; // Don't activate if session creation fails
      }
    }
    
    this._isActivated = true;

    // Handle initial context by sending as text message
    if (initialContext) {
      const question = this.extractQuestion(initialContext);
      if (question && question.length > 5) {
        console.log('📝 Sending initial question to Nova:', question);
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
      console.log('🔊 Nova is speaking, delaying deactivation...');
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

    console.log('🔇 Nova agent deactivated');
    this._isActivated = false;

    // Clear timeout
    if (this.activationTimeout) {
      clearTimeout(this.activationTimeout);
      this.activationTimeout = null;
    }

    // In wake_phrase mode, close session to save costs
    if (this.activationMode === 'wake_phrase' && this.session) {
      console.log('💰 Closing session to save costs (wake_phrase mode)');
      this.closeSession();
    }
  }

  private async closeSession(): Promise<void> {
    if (!this.session) return;

    console.log('Closing Nova session...');

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
      if (this.closeSignal) {
        this.closeSignal();
      }

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

    // Clear activation timeout
    if (this.activationTimeout) {
      clearTimeout(this.activationTimeout);
      this.activationTimeout = null;
    }

    // Close session if active
    if (this.session) {
      await this.closeSession();
    }

    this._isActivated = false;
    console.log('✓ Nova agent stopped');
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
}
