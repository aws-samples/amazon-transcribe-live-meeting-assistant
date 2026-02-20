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
import { LambdaClient, InvokeCommand } from '@aws-sdk/client-lambda';
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
  strandsLambdaArn?: string;
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
  private _isProcessingTool: boolean = false;
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
  
  // Lambda client and Strands agent configuration
  private lambdaClient: LambdaClient | null = null;
  private strandsLambdaArn?: string;

  constructor(config: NovaAgentConfig) {
    this.modelId = config.modelId;
    this.systemPrompt = config.systemPrompt;
    this.knowledgeBaseId = config.knowledgeBaseId;
    this.activationMode = config.activationMode || 'wake_phrase';
    this.defaultActivationDuration = config.activationDuration || 30;
    this.region = config.region || process.env.AWS_REGION || 'us-east-1';
    this.strandsLambdaArn = config.strandsLambdaArn || process.env.STRANDS_LAMBDA_ARN;
    
    // Set initial activation state based on mode
    this._isActivated = (this.activationMode === 'always_active');

    console.log('✓ AWS Nova Sonic 2 agent initialized');
    console.log(`  Model: ${this.modelId}`);
    console.log(`  Region: ${this.region}`);
    console.log(`  Activation mode: ${this.activationMode}`);
    if (this.strandsLambdaArn) {
      console.log(`  Strands agent tool: enabled`);
    }
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

      // Initialize Lambda client if Strands Lambda ARN is configured
      if (this.strandsLambdaArn) {
        this.lambdaClient = new LambdaClient({
          region: this.region,
          credentials: defaultProvider(),
        });
        console.log('✓ Lambda client initialized for Strands agent tool');
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
            voiceId: 'tiffany',
          },
        },
      },
    };

    // Add tool configuration if Strands Lambda ARN is configured
    if (this.strandsLambdaArn) {
      promptStartEvent.event.promptStart.toolUseOutputConfiguration = {
        mediaType: 'application/json',
      };
      promptStartEvent.event.promptStart.toolConfiguration = {
        tools: [
          {
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
          },
        ],
        toolChoice: {
          auto: {},
        },
      };
      console.log('✓ Strands agent tool configured in promptStart');
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

    // Append tool usage instructions if Strands tool is available
    let systemPromptContent = this.systemPrompt;
    if (this.strandsLambdaArn) {
      systemPromptContent += '\n\nIMPORTANT: You have access to the strands_agent tool. Use it when users ask about:\n' +
        '- Meeting summaries or transcripts\n' +
        '- Past meetings or meeting history\n' +
        '- Documents or knowledge base content\n' +
        '- Web search or current information\n' +
        '- Any query requiring specialized knowledge or data access\n' +
        'When you use the tool, first acknowledge the request (e.g., "Let me search for that"), then invoke the tool. ' +
        'Pass the user\'s query in the "query" parameter.';
    }

    this.addEventToQueue({
      event: {
        textInput: {
          promptName: this.session.promptName,
          contentName: textPromptID,
          content: systemPromptContent,
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
    let toolUseContent: any = null;
    let toolUseId: string = '';
    let toolName: string = '';

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
            } else if (jsonResponse.event?.toolUse) {
              // Store tool use information
              console.log('Tool use requested:', jsonResponse.event.toolUse.toolName);
              toolUseContent = jsonResponse.event.toolUse;
              toolUseId = jsonResponse.event.toolUse.toolUseId;
              toolName = jsonResponse.event.toolUse.toolName;
            } else if (jsonResponse.event?.contentEnd && jsonResponse.event?.contentEnd?.type === 'TOOL') {
              // Process tool use when content ends
              console.log(`Processing tool use: ${toolName}`);
              
              if (toolName === 'strands_agent' && toolUseContent) {
                try {
                  // Set flag to prevent deactivation while tool is processing
                  this._isProcessingTool = true;
                  
                  // Invoke Strands Lambda
                  const toolResult = await this.invokeStrandsAgent(toolUseContent);
                  
                  // Send tool result back to Nova (only if session is still active)
                  if (this.session && this.session.isActive) {
                    await this.sendToolResult(toolUseId, toolResult);
                    console.log('✓ Tool result sent to Nova');
                  } else {
                    console.log('⚠️  Session closed before tool result could be sent');
                  }
                } catch (error) {
                  console.error('Error processing tool use:', error);
                  // Send error result back to Nova (only if session is still active)
                  if (this.session && this.session.isActive) {
                    await this.sendToolResult(toolUseId, {
                      error: 'Failed to process tool request',
                      details: error instanceof Error ? error.message : String(error)
                    });
                  }
                } finally {
                  // Clear flag after tool processing completes
                  this._isProcessingTool = false;
                }
              }
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
    
    // Extract the message from the response and ensure it's a string
    const message = responsePayload.message || JSON.stringify(responsePayload);
    
    // Return as plain text string for Nova
    return typeof message === 'string' ? message : JSON.stringify(message);
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
