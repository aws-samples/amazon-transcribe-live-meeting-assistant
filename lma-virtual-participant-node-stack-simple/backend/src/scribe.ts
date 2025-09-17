import {
    TranscribeStreamingClient,
    StartStreamTranscriptionCommand,
} from '@aws-sdk/client-transcribe-streaming';
import { spawn, ChildProcess } from 'child_process';
import { createWriteStream } from 'fs';
import { details } from './details.js';
import { sendAddTranscriptSegment, sendStartMeeting, sendEndMeeting } from './kinesis-stream.js';

// Global current speaker (matching Python)
let currentSpeaker = "none";

export class TranscriptionService {
    private process: ChildProcess | null = null;
    private startTime: number | null = null;
    private readonly channels = 1;
    private readonly sampleRate = 16000; // in hertz
    private transcribeClient: TranscribeStreamingClient;
    private isTranscribing = false;

    constructor() {
        this.transcribeClient = new TranscribeStreamingClient({
            region: process.env.AWS_REGION || 'us-east-1',
        });
    }

    private async *audioStream() {
        // For local testing without Transcribe permissions, don't start FFmpeg
        if (!process.env.ECS_CONTAINER_METADATA_URI_V4) {
            console.log('Local testing mode - skipping audio capture');
            return;
        }

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
        } catch (error) {
            console.log('Audio process error:', error);
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

        // Send start meeting event to Kinesis (LMA integration)
        try {
            await sendStartMeeting();
        } catch (error: any) {
            if (error.name === 'AccessDeniedException') {
                console.log('Note: Kinesis permission error expected during local testing');
            } else {
                console.error('Failed to send start meeting event:', error);
            }
        }

        // For local testing without AWS permissions, skip actual transcription
        if (!process.env.ECS_CONTAINER_METADATA_URI_V4) {
            console.log('Local testing mode - skipping AWS Transcribe (no permissions)');
            console.log('Meeting join and chat functionality will still work');
            this.isTranscribing = false;
            return;
        }

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
                    
                    // Update status to ACTIVE when transcription starts (matching Python scribe.py)
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

                // Process transcription results in parallel (matching Python asyncio.gather)
                const recordingStream = createWriteStream(details.tmpRecordingFilename);
                
                await Promise.all([
                    this.writeAudio(response, recordingStream),
                    this.handleTranscriptEvents(response)
                ]);

                console.log('Transcription completed successfully');
                break;

            } catch (error: any) {
                if (error.name === 'AccessDeniedException' && error.message.includes('transcribe:StartStreamTranscription')) {
                    console.log('Error: Transcribe permission denied during local testing');
                    console.log('Note: This is expected when running locally without proper IAM permissions');
                    break; // Don't retry permission errors
                } else {
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
        }

        this.isTranscribing = false;
        console.log('Transcription service stopped');
    }

    private processTranscriptResult(result: any): void {
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

        // Send end meeting event to Kinesis (LMA integration)
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
        // Update global current speaker (matching Python)
        currentSpeaker = speaker;
        
        const timestamp = Date.now();
        details.speakers.push({ name: speaker, timestamp });
        
        const formattedTime = this.formatTimestamp(timestamp);
        console.log(`[${formattedTime}] Speaker changed to: ${speaker}`);
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

    // Method to handle transcription restart (for LMA commands)
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
            let isProcessing = true;

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

            // Process audio chunks
            this.process.stdout?.on('data', async (chunk: Buffer) => {
                if (details.start && this.isTranscribing) {
                    try {
                        // Send to Transcribe (matching Python send_audio_event)
                        await transcribeResponse.input_stream?.send_audio_event?.({ audio_chunk: chunk });
                        // Write to recording stream (matching Python)
                        recordingStream.write(chunk);
                    } catch (error) {
                        console.log('Audio processing error:', error);
                    }
                }
            });

            // Keep processing while meeting is active
            while (details.start && this.isTranscribing) {
                await new Promise(resolve => setTimeout(resolve, 100)); // 100ms chunks
            }

            // End stream when done
            await transcribeResponse.input_stream?.end_stream?.();
            recordingStream.close();
            
        } catch (error) {
            console.log('Write audio error:', error);
        }
    }

    // Handle transcript events (matching Python MyEventHandler)
    private async handleTranscriptEvents(transcribeResponse: any): Promise<void> {
        try {
            // Process transcript results (matching Python MyEventHandler.handle_events)
            for await (const event of transcribeResponse.TranscriptResultStream ?? []) {
                if (!this.isTranscribing) {
                    break;
                }

                for (const result of event.TranscriptEvent?.Transcript?.Results ?? []) {
                    // Send all results to Kinesis (matching Python behavior)
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
        } catch (error) {
            console.log('Handle transcript events error:', error);
        }
    }
}

// Export singleton instance
export const transcriptionService = new TranscriptionService();
