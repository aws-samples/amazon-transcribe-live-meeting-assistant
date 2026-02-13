import {
    TranscribeStreamingClient,
    StartStreamTranscriptionCommand,
} from '@aws-sdk/client-transcribe-streaming';
import { defaultProvider } from '@aws-sdk/credential-provider-node';
import { spawn, ChildProcess } from 'child_process';
import { createWriteStream } from 'fs';
import { details } from './details.js';
import { sendAddTranscriptSegment, sendStartMeeting, sendEndMeeting } from './kinesis-stream.js';
import { voiceAssistant } from './voice-assistant.js';

// Global current speaker (matching Python)
let currentSpeaker = "none";

// Local testing mode - skip AWS services
const isLocalTest = process.env.LOCAL_TEST === 'true';

export class TranscriptionService {
    private process: ChildProcess | null = null;
    private startTime: number | null = null;
    private readonly channels = 1;
    private readonly sampleRate = 16000; // in hertz
    private transcribeClient: TranscribeStreamingClient;
    private isTranscribing = false;
    private mockTranscriptionInterval: NodeJS.Timeout | null = null;
    
    // Wake phrase detection and transcript buffering
    private transcriptBuffer: Array<{
        text: string;
        timestamp: number;
        isPartial: boolean;
    }> = [];
    private bufferWindowMs = 10000; // Keep last 10 seconds
    private captureDelayMs = 3000; // Wait 3 seconds after wake phrase to capture full question
    private wakePhrases = ['hey alex', 'ok alex', 'hi alex', 'hello alex'];
    private isCapturingContext = false;

    constructor() {
        // In local test mode, explicitly use default provider which checks credentials file first
        // Otherwise use default credential chain (EC2 instance role in production)
        const clientConfig: any = {
            region: process.env.AWS_REGION || 'us-east-1',
        };
        
        if (isLocalTest) {
            console.log('Using AWS credentials from environment/file for local testing');
            // Disable EC2 metadata to force credentials from environment or file
            process.env.AWS_EC2_METADATA_DISABLED = 'true';
            clientConfig.credentials = defaultProvider();
        }
        
        this.transcribeClient = new TranscribeStreamingClient(clientConfig);
    }

    private async *audioStream() {

        this.process = spawn('ffmpeg', [
            '-f',
            'pulse',
            '-i',
            'default',
            '-ac',
            String(this.channels),
            '-ar',
            String(this.sampleRate),
            '-acodec',
            'pcm_s16le',
            '-f',
            's16le',
            '-loglevel',
            'warning',
            '-',
        ]);

        // Add error handler for ffmpeg process
        this.process.on('error', (error: any) => {
            const msg = `FFmpeg process error: ${error.message}`;
            if (isLocalTest) {
                console.error(msg + ' (non-fatal in local test)');
            } else {
                console.error(msg + ' (fatal in production)');
            }
        });

        this.process.stderr?.on('data', (data: any) => {
            const msg = data.toString();
            if (!msg.includes('size=') && !msg.includes('time=')) {
                console.log('FFmpeg stderr:', msg.trim());
            }
        });

        try {
            for await (const chunk of this.process.stdout!) {
                if (!details.start) {
                    yield {
                        AudioEvent: { AudioChunk: Buffer.alloc(chunk.length) },
                    };
                } else {
                    yield { AudioEvent: { AudioChunk: chunk } };
                }
                if (!this.startTime) {
                    this.startTime = Date.now();
                }
            }
        } catch (error: any) {
            const msg = `Audio stream error: ${error.message}`;
            if (isLocalTest) {
                console.log(msg + ' (non-fatal in local test)');
            } else {
                console.error(msg + ' (fatal in production)');
                throw error;
            }
        }
    }

    private formatTimestamp(timestamp: number): string {
        const dateTime = new Date(timestamp);
        return dateTime.toLocaleTimeString('en-US', {
            hour: '2-digit',
            minute: '2-digit',
            hour12: false,
        });
    }

    async startTranscription(): Promise<void> {
        if (this.isTranscribing) {
            console.log('Transcription already running');
            return;
        }

        console.log('Starting transcription service');
        this.isTranscribing = true;
        
        // Start voice assistant if enabled
        if (voiceAssistant.isEnabled()) {
            try {
                await voiceAssistant.start();
                console.log('✓ Voice assistant started alongside transcription');
            } catch (error) {
                console.error('Failed to start voice assistant:', error);
                // Non-critical - continue with transcription
            }
        }

        // Send start meeting event to Kinesis
        try {
            await sendStartMeeting();
        } catch (error: any) {
            if (error.name === 'AccessDeniedException') {
                console.log('Note: Kinesis permission error expected during local testing');
            } else {
                console.error('Failed to send start meeting event:', error);
            }
        }

        // In local test mode, skip Kinesis/AppSync but still run transcription if agent is enabled
        // (Transcription provides audio stream for ElevenLabs agent)

        const maxRetries = 5;
        const retryDelay = 5000; // 5 seconds
        let sessionId: string | undefined;

        for (let attempt = 0; attempt < maxRetries; attempt++) {
            try {
                // Build transcription parameters
                const transcriptionParams: any = {
                    AudioStream: this.audioStream(),
                    MediaSampleRateHertz: this.sampleRate,
                    MediaEncoding: 'pcm',
                    LanguageCode: details.transcribeLanguageCode,
                    ShowSpeakerLabel: true,
                };

                // Add optional parameters
                if (details.customVocabularyName) {
                    transcriptionParams.VocabularyName = details.customVocabularyName;
                }

                if (details.enableContentRedaction && details.transcribeLanguageCode === 'en-US') {
                    transcriptionParams.ContentRedactionType = details.transcribeContentRedactionType;
                }

                if (sessionId) {
                    transcriptionParams.SessionId = sessionId;
                    console.log(`Resuming transcription session: ${sessionId}`);
                } else {
                    console.log(`Starting new transcription session with language: ${details.transcribeLanguageCode}`);
                }

                const command = new StartStreamTranscriptionCommand(transcriptionParams);
                const response = await this.transcribeClient.send(command);

                if (!sessionId) {
                    sessionId = response.SessionId;
                    console.log(`New transcription session ID: ${sessionId}`);
                    
                    // Update status to ACTIVE when transcription starts
                    const vpId = process.env.VIRTUAL_PARTICIPANT_ID;
                    if (vpId) {
                        try {
                            const { VirtualParticipantStatusManager } = await import('./status-manager.js');
                            const statusManager = new VirtualParticipantStatusManager(vpId);
                            await statusManager.setActive();
                            console.log(`VP ${vpId} status: ACTIVE (transcription started)`);
                        } catch (error) {
                            console.log(`Failed to update VP status to ACTIVE: ${error}`);
                        }
                    }
                }

                const recordingStream = createWriteStream(details.tmpRecordingFilename);
                
                // In local test mode, wrap with error handlers to prevent crashes
                // In production, let errors propagate to crash the task
                if (isLocalTest) {
                    try {
                        await Promise.all([
                            this.writeAudio(response, recordingStream).catch(err => {
                                console.error('Audio write error (non-fatal in local test):', err.message);
                                return Promise.resolve();
                            }),
                            this.handleTranscriptEvents(response).catch(err => {
                                console.error('Transcript event error (non-fatal in local test):', err.message);
                                return Promise.resolve();
                            })
                        ]);
                    } catch (streamError: any) {
                        console.error('Stream processing error (non-fatal in local test):', streamError.message);
                    }
                } else {
                    // Production mode - let errors crash the task
                    await Promise.all([
                        this.writeAudio(response, recordingStream),
                        this.handleTranscriptEvents(response)
                    ]);
                }

                console.log('Transcription completed successfully');
                break;

            } catch (error: any) {
                console.error(`Transcription error (attempt ${attempt + 1}/${maxRetries}):`, error.message);
                
                if (attempt < maxRetries - 1) {
                    console.log(`Retrying in ${retryDelay / 1000} seconds...`);
                    await new Promise(resolve => setTimeout(resolve, retryDelay));
                } else {
                    console.error('Max retries reached. Transcription stopped.');
                    break;
                }
            }
        }
        this.isTranscribing = false;
        console.log('Transcription service stopped');
    }

    private processTranscriptResult(result: any): void {
        const transcript = result.Alternatives?.[0]?.Transcript || '';
        const isPartial = result.IsPartial;
        const timestamp = Date.now();
        
        if (transcript) {
            console.log(`📝 Transcribed: "${transcript}" (IsPartial: ${isPartial})`);
        }
        
        // Add to transcript buffer (only non-partial for accuracy)
        if (!isPartial && transcript) {
            this.transcriptBuffer.push({ text: transcript, timestamp, isPartial });
            
            // Trim old entries
            const cutoff = timestamp - this.bufferWindowMs;
            this.transcriptBuffer = this.transcriptBuffer.filter(t => t.timestamp > cutoff);
            
            // Check for wake phrase
            if (this.detectWakePhrase(transcript)) {
                this.handleWakePhraseDetected(timestamp);
            }
        }
        
        for (const item of result.Alternatives?.[0]?.Items ?? []) {
            const word = item.Content;
            const wordType = item.Type;
            
            if (wordType === 'pronunciation') {
                const timestamp = this.startTime! + (item.StartTime! * 1000);
                const speaker = this.getCurrentSpeaker();
                
                const formattedTime = this.formatTimestamp(timestamp);
                
                // Check if we should append to existing caption or create new one
                if (
                    details.captions.length === 0 ||
                    !details.captions[details.captions.length - 1]
                        .split(': ')[0]
                        .includes(speaker)
                ) {
                    details.captions.push(`[${formattedTime}] ${speaker}: ${word}`);
                } else {
                    details.captions[details.captions.length - 1] += ` ${word}`;
                }
            } else if (wordType === 'punctuation') {
                if (details.captions.length > 0) {
                    details.captions[details.captions.length - 1] += word;
                }
            }
        }
    }

    private getCurrentSpeaker(): string {
        if (details.speakers.length === 0) {
            return 'Unknown';
        }

        const currentTime = Date.now();
        // Find the most recent speaker
        let currentSpeaker = 'Unknown';
        for (const speaker of details.speakers) {
            if (speaker.timestamp <= currentTime) {
                currentSpeaker = speaker.name;
            } else {
                break;
            }
        }
        return currentSpeaker;
    }

    async stopTranscription(): Promise<void> {
        console.log('Stopping transcription service');
        this.isTranscribing = false;

        if (this.process) {
            this.process.kill();
            this.process = null;
        }
        
        // Stop voice assistant if running
        if (voiceAssistant.isEnabled()) {
            try {
                await voiceAssistant.stop();
                console.log('✓ Voice assistant stopped');
            } catch (error) {
                console.error('Error stopping voice assistant:', error);
            }
        }

        // Send end meeting event to Kinesis
        try {
            await sendEndMeeting();
        } catch (error: any) {
            if (error.name === 'AccessDeniedException') {
                console.log('Note: Kinesis permission error expected during local testing');
            } else {
                console.error('Failed to send end meeting event:', error);
            }
        }
    }

    async speakerChange(speaker: string): Promise<void> {
        // Update global current speaker
        currentSpeaker = speaker;
        
        const timestamp = Date.now();
        details.speakers.push({ name: speaker, timestamp });
        
        const formattedTime = this.formatTimestamp(timestamp);
        console.log(`[${formattedTime}] Speaker changed to: ${speaker}`);
    }

    // Wake phrase detection methods
    private detectWakePhrase(text: string): boolean {
        // Remove all punctuation and normalize whitespace
        const normalized = text.toLowerCase()
            .replace(/[,.\?!;:]/g, ' ')  // Replace punctuation with spaces
            .replace(/\s+/g, ' ')         // Normalize multiple spaces
            .trim();
        
        return this.wakePhrases.some(phrase => normalized.includes(phrase));
    }

    private async handleWakePhraseDetected(detectionTime: number): Promise<void> {
        // Don't activate if already activated or if voice assistant not enabled
        if (!voiceAssistant.isEnabled() || voiceAssistant.isActivated()) {
            return;
        }

        // Don't activate if we're already capturing context
        if (this.isCapturingContext) {
            return;
        }

        console.log('🎤 Wake phrase detected, capturing context...');
        this.isCapturingContext = true;
        
        // Wait to capture additional context after wake phrase
        await new Promise(resolve => setTimeout(resolve, this.captureDelayMs));
        
        // Get transcript from detection time onwards
        const contextTranscript = this.transcriptBuffer
            .filter(t => t.timestamp >= detectionTime - 2000) // Include 2s before wake phrase
            .map(t => t.text)
            .join(' ');
        
        console.log('📝 Captured context:', contextTranscript);
        
        // Activate voice assistant with context
        voiceAssistant.activate(30, contextTranscript);
        
        this.isCapturingContext = false;
    }

    // Utility methods for status
    isActive(): boolean {
        return this.isTranscribing;
    }

    getSessionInfo(): { isActive: boolean; startTime: number | null; sessionId?: string } {
        return {
            isActive: this.isTranscribing,
            startTime: this.startTime,
        };
    }

    // Method to handle transcription restart
    async restartTranscription(): Promise<void> {
        if (this.isTranscribing) {
            await this.stopTranscription();
            // Small delay before restart
            await new Promise(resolve => setTimeout(resolve, 1000));
        }
        await this.startTranscription();
    }

    // Parallel audio processing (matching Python write_audio function)
    private async writeAudio(transcribeResponse: any, recordingStream: any): Promise<void> {
        try {
            // Create audio input queue (matching Python asyncio.Queue)
            const audioQueue: Buffer[] = [];
            // Start FFmpeg process for audio capture
            this.process = spawn('ffmpeg', [
                '-f', 'pulse',
                '-i', 'default',
                '-ac', '1',
                '-ar', '16000',
                '-acodec', 'pcm_s16le',
                '-f', 's16le',
                '-loglevel', 'warning',
                '-'
            ]);

            // Add error handlers for the process
            this.process.on('error', (error: any) => {
                const msg = `FFmpeg process error: ${error.message}`;
                if (isLocalTest) {
                    console.error(msg + ' (non-fatal in local test)');
                } else {
                    console.error(msg + ' (fatal in production)');
                    throw error;
                }
            });

            this.process.stderr?.on('data', (data: any) => {
                const msg = data.toString();
                if (!msg.includes('size=') && !msg.includes('time=')) {
                    console.log('FFmpeg:', msg.trim());
                }
            });

            // Process audio chunks
            this.process.stdout?.on('data', async (chunk: Buffer) => {
                if (details.start && this.isTranscribing) {
                    try {
                        // Send to Transcribe
                        await transcribeResponse.input_stream?.send_audio_event?.({ audio_chunk: chunk });
                        recordingStream.write(chunk);
                        
                        // Also send to voice assistant if enabled and activated
                        if (voiceAssistant.isEnabled() && voiceAssistant.isActive() && voiceAssistant.isActivated()) {
                            voiceAssistant.sendAudioChunk(chunk);
                        }
                    } catch (error: any) {
                        const msg = `Audio chunk processing error: ${error.message}`;
                        if (isLocalTest) {
                            console.log(msg + ' (non-fatal in local test)');
                        } else {
                            console.error(msg);
                            throw error;
                        }
                    }
                }
            });

            // Keep processing while meeting is active
            while (details.start && this.isTranscribing) {
                await new Promise(resolve => setTimeout(resolve, 100)); // 100ms chunks
            }

            // End stream when done
            try {
                await transcribeResponse.input_stream?.end_stream?.();
            } catch (error: any) {
                const msg = `End stream error: ${error.message}`;
                if (isLocalTest) {
                    console.log(msg + ' (non-fatal in local test)');
                } else {
                    console.error(msg);
                    throw error;
                }
            }
            
            try {
                recordingStream.close();
            } catch (error: any) {
                const msg = `Recording stream close error: ${error.message}`;
                if (isLocalTest) {
                    console.log(msg + ' (non-fatal in local test)');
                } else {
                    console.error(msg);
                    throw error;
                }
            }
            
        } catch (error: any) {
            const msg = `Write audio error: ${error.message || error}`;
            if (isLocalTest) {
                console.log(msg + ' (non-fatal in local test)');
            } else {
                console.error(msg + ' (fatal in production)');
                throw error;
            }
        }
    }

    // Handle transcript events
    private async handleTranscriptEvents(transcribeResponse: any): Promise<void> {
        try {
            // Process transcript results
            for await (const event of transcribeResponse.TranscriptResultStream ?? []) {
                if (!this.isTranscribing) {
                    break;
                }
                for (const result of event.TranscriptEvent?.Transcript?.Results ?? []) {
                    // Send all results to Kinesis
                    try {
                        await sendAddTranscriptSegment(currentSpeaker, result);
                    } catch (error) {
                        console.error('Failed to send transcript to Kinesis:', error);
                    }

                    // Process for local captions only for non-partial results
                    if (result.IsPartial === false) {
                        this.processTranscriptResult(result);
                    }
                }
            }
        } catch (error: any) {
            // In local test mode, handle errors gracefully
            // In production, let errors propagate to kill the task
            if (isLocalTest) {
                if (error.code === 'ERR_STREAM_PREMATURE_CLOSE') {
                    console.log('Transcribe stream closed prematurely (non-fatal in local test) - meeting will continue');
                } else if (error.name === 'AccessDeniedException') {
                    console.log('Transcribe permission denied (non-fatal in local test) - meeting will continue without transcription');
                } else {
                    console.log('Handle transcript events error (non-fatal in local test):', error.message || error);
                }
            } else {
                // Production mode - rethrow to kill the task
                console.error('Handle transcript events error (fatal in production):', error.message || error);
                throw error;
            }
        }
    }
}

// Export singleton instance
export const transcriptionService = new TranscriptionService();
