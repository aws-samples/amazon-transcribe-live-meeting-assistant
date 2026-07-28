import {
    TranscribeStreamingClient,
    StartStreamTranscriptionCommand,
} from '@aws-sdk/client-transcribe-streaming';
import { defaultProvider } from '@aws-sdk/credential-provider-node';
import { spawn, ChildProcess } from 'child_process';
import { createWriteStream } from 'fs';
import { details } from './details.js';
import { sendAddTranscriptSegment, sendStartMeeting, sendEndMeeting, kinesisStreamManager } from './kinesis-stream.js';
import { voiceAssistant } from './voice-assistant.js';
import { agentSpeakingDetector } from './agent-speaking-detector.js';

// Global current speaker (matching Python)
let currentSpeaker = "none";

// Local testing mode - skip AWS services
const isLocalTest = process.env.LOCAL_TEST === 'true';

// True when a Transcribe streaming error indicates the SessionId we tried to
// resume is no longer valid (expired / closed / unknown), so the next attempt
// must start a fresh session rather than reuse the stale id. Kept in sync with
// the equivalent check in the WebSocket transcriber (transcribe.ts).
const isStaleSessionError = (error: any): boolean => {
    const message = (error?.message ?? '').toLowerCase();
    // Require the word "session" on every message-based match: otherwise
    // unrelated errors that merely contain "has expired" (e.g. the STS
    // "security token ... has expired" credential error) would be misread as a
    // stale SessionId and needlessly discard a still-valid session.
    return (
        (message.includes('session') &&
            (message.includes('expired') ||
                message.includes('not found') ||
                message.includes('invalid'))) ||
        error?.name === 'SessionExpiredException'
    );
};

export class TranscriptionService {
    private process: ChildProcess | null = null;           // FFmpeg: combined_audio.monitor → Transcribe
    private novaAudioProcess: ChildProcess | null = null;  // FFmpeg: meeting_audio.monitor → Nova/recording
    private meetingToCombinedPipe: ChildProcess | null = null; // pacat: meeting audio → combined_audio sink
    private startTime: number | null = null;
    // Cumulative timeline offset (seconds) applied to Transcribe timestamps.
    // Amazon Transcribe resets Item.StartTime/EndTime to 0 on every new
    // StartStreamTranscription request (reusing SessionId does NOT resume the
    // timeline). On each reconnect we advance this by the highest EndTime seen
    // so transcript segments continue the meeting timeline instead of jumping
    // back to 0 and overlapping earlier segments (GitHub #292). Kept consistent
    // with the WebSocket transcriber (transcribe.ts).
    private transcribeTimeOffsetSeconds = 0;
    // Set by handleTranscriptEvents when Transcribe closes the result stream
    // server-side while the meeting is still live (idle / duration limit /
    // transient close). It unblocks writeAudio's spin loop so the session's
    // Promise.all resolves and the retry loop reconnects instead of hanging with
    // a dead transcription pipe (GitHub #292). Mirrors the WebSocket
    // transcriber's clean-close reconnect (transcribe.ts).
    private reconnectRequested = false;
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
    private wakePhrases: string[];
    private preConnectTriggered = false;
    private wakeDetectionTimestamp: number | null = null;

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
        
        // Initialize voice assistant wake phrases from environment variable
        const wakePhraseEnv = process.env.VOICE_ASSISTANT_WAKE_PHRASES || 'hey alex,ok alex,hi alex,hello alex';
        this.wakePhrases = wakePhraseEnv.split(',').map(p => p.trim().toLowerCase());
        console.log('Wake phrases configured:', this.wakePhrases);
    }

    private async *audioStream() {

        // Capture from combined_audio.monitor to get both meeting and agent audio for transcription
        this.process = spawn('ffmpeg', [
            '-f',
            'pulse',
            '-i',
            'combined_audio.monitor',  // Combined audio (meeting + agent) for transcription
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
            if (error.code === 'ERR_STREAM_PREMATURE_CLOSE' || !this.isTranscribing) {
                console.log(msg + ' (expected during transcription teardown, non-fatal)');
                return;
            }
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

        // maxRetries bounds CONSECUTIVE failed (re)connects, not the total over
        // the life of the meeting. A session that ran long enough resets the
        // counter (see healthySessionMs), so a long meeting is not aborted
        // merely because it accumulated a handful of transient reconnects spread
        // across an hour. Kept consistent with the WebSocket transcriber
        // (transcribe.ts, GitHub #292).
        const maxRetries = 5;
        const retryDelay = 5000; // 5 seconds
        const healthySessionMs = 10_000;
        let sessionId: string | undefined;
        let consecutiveFailures = 0;

        // The recording write stream spans the WHOLE meeting, not one Transcribe
        // session. Open it ONCE in append mode and reuse it across reconnects —
        // recreating it per session (truncate mode) would discard everything
        // recorded before each reconnect. writeAudio no longer closes it; it is
        // closed once when the meeting ends (below).
        const recordingStream = createWriteStream(details.tmpRecordingFilename, { flags: 'a' });

        // Loop over Transcribe sessions for the life of the meeting. Each
        // iteration is one StartStreamTranscription session; we reconnect when a
        // session ends unexpectedly (clean server-side close or transient error)
        // while the meeting is still live.
        for (;;) {
            if (!this.isTranscribing || !details.start) {
                console.log('Meeting ended - stopping transcription session loop');
                break;
            }
            this.reconnectRequested = false;
            const sessionStartMs = Date.now();
            try {
                const transcriptionParams: any = {
                    AudioStream: this.audioStream(),
                    MediaSampleRateHertz: this.sampleRate,
                    MediaEncoding: 'pcm',
                    ShowSpeakerLabel: true,
                };

                const langCode = details.transcribeLanguageCode;
                const langOptions = (details.transcribeLanguageOptions || '')
                    .split(',')
                    .map((s) => s.trim())
                    .filter((s) => s.length > 0)
                    .join(',');
                const preferredLang = (details.transcribePreferredLanguage || '').trim();

                if (langCode === 'identify-language' || langCode === 'identify-multiple-languages') {
                    if (!langOptions) {
                        throw new Error(
                            `TRANSCRIBE_LANGUAGE_CODE='${langCode}' requires TRANSCRIBE_LANGUAGE_OPTIONS ` +
                            `(at least two comma-separated language codes, e.g. 'en-US,hi-IN').`,
                        );
                    }
                    if (langCode === 'identify-multiple-languages') {
                        transcriptionParams.IdentifyMultipleLanguages = true;
                    } else {
                        transcriptionParams.IdentifyLanguage = true;
                    }
                    transcriptionParams.LanguageOptions = langOptions;
                    if (preferredLang) {
                        transcriptionParams.PreferredLanguage = preferredLang;
                    }
                } else {
                    transcriptionParams.LanguageCode = langCode;
                }

                if (details.customVocabularyName) {
                    transcriptionParams.VocabularyName = details.customVocabularyName;
                }

                if (details.enableContentRedaction && langCode === 'en-US') {
                    transcriptionParams.ContentRedactionType = details.transcribeContentRedactionType;
                }

                if (sessionId) {
                    transcriptionParams.SessionId = sessionId;
                    console.log(`Resuming transcription session: ${sessionId}`);
                } else if (transcriptionParams.IdentifyMultipleLanguages || transcriptionParams.IdentifyLanguage) {
                    const mode = transcriptionParams.IdentifyMultipleLanguages ? 'multi' : 'single';
                    console.log(
                        `Starting new transcription session with language identification (${mode}) ` +
                        `options=[${langOptions}]${preferredLang ? ` preferred=${preferredLang}` : ''}`,
                    );
                } else {
                    console.log(`Starting new transcription session with language: ${langCode}`);
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

                // The session's result stream ended WITHOUT throwing. If the
                // meeting is still live this is an unexpected server-side close
                // (Amazon Transcribe closes streams on its own schedule — idle
                // behaviour, session duration limits, transient close). Reconnect
                // instead of returning, which previously left the client
                // streaming audio into a dead transcription pipe (GitHub #292).
                if (this.isTranscribing && details.start) {
                    this.teardownSessionProcesses();
                    // A session that ran long enough is healthy — reset the
                    // consecutive-failure counter so ordinary periodic reconnects
                    // never accumulate toward maxRetries. An immediate clean close
                    // (<healthySessionMs) DOES count, so a session that opens and
                    // instantly closes over and over cannot reconnect forever.
                    if (Date.now() - sessionStartMs >= healthySessionMs) {
                        consecutiveFailures = 0;
                    } else {
                        consecutiveFailures += 1;
                        if (consecutiveFailures >= maxRetries) {
                            console.error(`Max consecutive reconnects (${maxRetries}) reached on clean close. Transcription stopped.`);
                            break;
                        }
                    }
                    console.log(
                        `Transcribe result stream closed while meeting is live; reconnecting. ` +
                        `timeOffset: ${this.transcribeTimeOffsetSeconds.toFixed(2)}s`,
                    );
                    await new Promise(resolve => setTimeout(resolve, 1000));
                    continue;
                }

                console.log('Transcription completed successfully');
                break;

            } catch (error: any) {
                // nosemgrep: javascript.lang.security.audit.unsafe-formatstring.unsafe-formatstring -- ECMAScript template literals do not interpret util.format specifiers
                console.error(`Transcription error (consecutive failure ${consecutiveFailures + 1}/${maxRetries}):`, error.message);

                const isNonRetryable =
                    error.name === 'BadRequestException' ||
                    error.name === 'ValidationException' ||
                    error.name === 'InvalidParameterException' ||
                    error.$metadata?.httpStatusCode === 400 ||
                    error.message?.includes('validation error') ||
                    error.message?.includes('non-retryable streaming request');

                // If the resumed SessionId is no longer valid (expired / closed /
                // not found / invalid), clear it so the next retry starts a FRESH
                // session; the cumulative time offset keeps the timeline continuous.
                // Kept consistent with isStaleSessionError() in the WebSocket
                // transcriber (transcribe.ts).
                if (isStaleSessionError(error)) {
                    console.log(`Transcribe SessionId '${sessionId}' is stale/expired - will start a new session on retry`);
                    sessionId = undefined;
                }
                
                // Promise.all rejects the instant handleTranscriptEvents throws,
                // WITHOUT waiting for the concurrent writeAudio — so its spin
                // loop is now orphaned. Signal it to exit (it polls every 100ms)
                // so it can't linger as an idle timer. Safe against the loop-top
                // reset: every retry path below waits ≥1s before the next
                // iteration clears the flag, far longer than the poll interval.
                this.reconnectRequested = true;

                // Kill the session's FFmpeg/pacat processes to prevent
                // ERR_STREAM_PREMATURE_CLOSE from bubbling up as an uncaught
                // exception; they are respawned on the next (re)connect.
                this.teardownSessionProcesses();

                if (isNonRetryable) {
                    console.error(
                        'Non-retryable Transcribe configuration error — aborting without further retries. ' +
                        'Check TRANSCRIBE_LANGUAGE_CODE / TRANSCRIBE_LANGUAGE_OPTIONS / TRANSCRIBE_PREFERRED_LANGUAGE.',
                    );
                    break;
                }

                // Stop retrying once the meeting has ended — no point restarting
                // a session for a call that is over.
                if (!this.isTranscribing || !details.start) {
                    console.log('Meeting ended - not retrying transcription session.');
                    break;
                }

                // Count only CONSECUTIVE failures: a session that ran long enough
                // before failing is treated as healthy and resets the counter,
                // so a long meeting is not aborted by transient reconnects spread
                // across it (GitHub #292).
                if (Date.now() - sessionStartMs >= healthySessionMs) {
                    consecutiveFailures = 0;
                }
                consecutiveFailures += 1;

                if (consecutiveFailures >= maxRetries) {
                    console.error(`Max consecutive retries (${maxRetries}) reached. Transcription stopped.`);
                    break;
                }

                console.log(`Retrying in ${retryDelay / 1000} seconds (consecutive failure ${consecutiveFailures}/${maxRetries})...`);
                await new Promise(resolve => setTimeout(resolve, retryDelay));
            }
        }

        // Meeting is over: close the recording stream exactly once.
        try {
            recordingStream.end();
        } catch (error: any) {
            console.error('Failed to close recording stream:', error?.message ?? error);
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
        
        // Detect wake phrase in partials to pre-warm the voice agent connection
        if (isPartial && transcript && !this.preConnectTriggered) {
            if (this.detectWakePhrase(transcript)) {
                console.log('⚡ Wake phrase detected in PARTIAL transcript — pre-connecting voice agent');
                this.preConnectTriggered = true;
                this.wakeDetectionTimestamp = timestamp;
                voiceAssistant.preConnect().catch(err => {
                    console.error('Pre-connect error (non-fatal):', err);
                });
            }
        }
        
        // On completed segments, activate with full context if wake phrase was detected
        if (!isPartial && transcript) {
            this.transcriptBuffer.push({ text: transcript, timestamp, isPartial });
            
            // Trim old entries
            const cutoff = timestamp - this.bufferWindowMs;
            this.transcriptBuffer = this.transcriptBuffer.filter(t => t.timestamp > cutoff);
            
            // Check for wake phrase in the completed segment
            if (this.detectWakePhrase(transcript) || this.preConnectTriggered) {
                const detectionTime = this.wakeDetectionTimestamp || timestamp;
                // Reset pre-connect state for next wake phrase
                this.preConnectTriggered = false;
                this.wakeDetectionTimestamp = null;
                
                this.handleWakePhraseDetected(detectionTime);
            }
        }
        
        for (const item of result.Alternatives?.[0]?.Items ?? []) {
            const word = item.Content;
            const wordType = item.Type;
            
            if (wordType === 'pronunciation') {
                // Add the cumulative reconnect offset (see transcribeTimeOffsetSeconds)
                // so captions stay on the meeting timeline after a session restart.
                const timestamp =
                    this.startTime! + ((item.StartTime! + this.transcribeTimeOffsetSeconds) * 1000);
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

    // Kill the FFmpeg/pacat trio spawned for a single Transcribe session so the
    // next (re)connect starts them fresh. Used by the retry loop between
    // sessions (reconnect on clean close or error) AND by stopTranscription.
    // Deliberately does NOT touch isTranscribing, the recording stream, or the
    // Kinesis/voice-assistant lifecycle — those span the whole meeting.
    private teardownSessionProcesses(): void {
        if (this.process) {
            try { this.process.kill(); } catch (_) { /* ignore */ }
            this.process = null;
        }
        if (this.novaAudioProcess) {
            try { this.novaAudioProcess.kill(); } catch (_) { /* ignore */ }
            this.novaAudioProcess = null;
        }
        if (this.meetingToCombinedPipe) {
            try {
                if (this.meetingToCombinedPipe.stdin && !this.meetingToCombinedPipe.stdin.destroyed) {
                    this.meetingToCombinedPipe.stdin.end();
                }
                this.meetingToCombinedPipe.kill();
            } catch (_) { /* ignore */ }
            this.meetingToCombinedPipe = null;
        }
    }

    async stopTranscription(): Promise<void> {
        console.log('Stopping transcription service');
        this.isTranscribing = false;

        this.teardownSessionProcesses();

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

        // Build context from recent transcript buffer
        const contextTranscript = this.transcriptBuffer
            .filter(t => t.timestamp >= detectionTime - 2000) // Include 2s before wake phrase
            .map(t => t.text)
            .join(' ');
        
        console.log('🎤 Wake phrase detected — activating immediately with context:', contextTranscript);
        
        // Activate voice assistant with context (connection already pre-warmed)
        voiceAssistant.activate(30, contextTranscript);
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

    // Captures meeting-only audio (meeting_audio.monitor) via a separate FFmpeg process.
    // Feeds audio to: (1) recording file, (2) combined_audio sink for Transcribe, (3) voice assistant.
    // Uses separate process variables to avoid overwriting the transcription FFmpeg in audioStream().
    // Channel separation (meeting vs combined) prevents Nova from hearing its own voice.
    private async writeAudio(transcribeResponse: any, recordingStream: any): Promise<void> {
        try {
            // Pipe meeting audio into combined_audio sink for Transcribe
            this.meetingToCombinedPipe = spawn('pacat', [
                '--playback',
                '--device=combined_audio',
                '--format=s16le',
                '--rate=16000',
                '--channels=1',
                '--raw',
                '--latency-msec=20'
            ]);
            
            this.meetingToCombinedPipe.on('error', (error: any) => {
                console.error(`pacat (meeting→combined) error: ${error.message}`);
            });
            
            this.meetingToCombinedPipe.stderr?.on('data', (data: any) => {
                const msg = data.toString().trim();
                if (msg) console.log(`pacat (meeting→combined): ${msg}`);
            });
            
            // Capture meeting-only audio for Nova and recording
            this.novaAudioProcess = spawn('ffmpeg', [
                '-f', 'pulse',
                '-i', 'meeting_audio.monitor',  // Meeting audio only (no agent feedback)
                '-ac', '1',
                '-ar', '16000',
                '-acodec', 'pcm_s16le',
                '-f', 's16le',
                '-loglevel', 'warning',
                '-'
            ]);

            // Add error handlers for the process
            this.novaAudioProcess.on('error', (error: any) => {
                const msg = `FFmpeg (Nova audio) process error: ${error.message}`;
                if (isLocalTest) {
                    console.error(msg + ' (non-fatal in local test)');
                } else {
                    console.error(msg + ' (fatal in production)');
                    throw error;
                }
            });

            this.novaAudioProcess.stderr?.on('data', (data: any) => {
                const msg = data.toString();
                if (!msg.includes('size=') && !msg.includes('time=')) {
                    console.log('FFmpeg:', msg.trim());
                }
            });

            // Process audio chunks from meeting_audio.monitor
            this.novaAudioProcess.stdout?.on('data', async (chunk: Buffer) => {
                if (details.start && this.isTranscribing) {
                    try {
                        recordingStream.write(chunk);

                        // Forward meeting audio to combined_audio sink for Transcribe
                        if (this.meetingToCombinedPipe && this.meetingToCombinedPipe.stdin && !this.meetingToCombinedPipe.stdin.destroyed) {
                            this.meetingToCombinedPipe.stdin.write(chunk);
                        }

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

            // Keep processing while the meeting is active AND this session is
            // still current. reconnectRequested is set when Transcribe closes
            // the result stream server-side mid-meeting; exiting here lets the
            // session's Promise.all resolve so the retry loop can reconnect
            // (GitHub #292). The recording stream is NOT closed here — it spans
            // the whole meeting and is owned by startTranscription's loop.
            while (details.start && this.isTranscribing && !this.reconnectRequested) {
                await new Promise(resolve => setTimeout(resolve, 100)); // 100ms chunks
            }

            // End the Transcribe input stream for this session when done.
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
        // The reconnect offset must stay CONSTANT for the whole session and only
        // advance once, when this session ends (mirrors the WebSocket
        // transcriber). Transcribe's Item.StartTime/EndTime are already
        // monotonic WITHIN a session and only reset to 0 on a NEW request — so
        // advancing the field per-result (the previous behaviour) compounded the
        // offset on every event and pushed segment timestamps far past
        // wall-clock even with zero reconnects (GitHub #292).
        const sessionOffsetSeconds = this.transcribeTimeOffsetSeconds;
        // Highest absolute (offset-adjusted) segment EndTime seen this session;
        // seeds the next reconnect's offset. Starts at the session offset so a
        // session that produces nothing still carries the timeline forward.
        let observedMaxEndTime = sessionOffsetSeconds;
        try {
            // Process transcript results
            for await (const event of transcribeResponse.TranscriptResultStream ?? []) {
                if (!this.isTranscribing) {
                    break;
                }
                for (const result of event.TranscriptEvent?.Transcript?.Results ?? []) {
                    const lmaIdentity = (details.lmaIdentity || '').trim();
                    const scribeIdentity = (details.scribeIdentity || '').trim();
                    const speakerIsVp =
                        !!currentSpeaker &&
                        currentSpeaker !== 'none' &&
                        ((lmaIdentity.length > 0 && currentSpeaker === lmaIdentity) ||
                            (scribeIdentity.length > 0 && currentSpeaker === scribeIdentity));

                    const suppressAgentTranscript =
                        details.meetingMode === 'translator' &&
                        (agentSpeakingDetector.isSpeaking() || speakerIsVp);

                    if (suppressAgentTranscript) {
                        if (!result.IsPartial) {
                            const text = result.Alternatives?.[0]?.Transcript || '';
                            if (text) {
                                console.log(`🌐 Translator mode: suppressing agent-origin transcript segment: "${text}"`);
                            }
                        }
                        try {
                            kinesisStreamManager.syncTranscriptSegmentState(
                                currentSpeaker,
                                result,
                                sessionOffsetSeconds,
                            );
                        } catch (error) {
                            console.error('Failed to sync transcript segment state during suppression:', error);
                        }
                    } else {
                        // Send all results to Kinesis
                        try {
                            const segMaxEndTime = await sendAddTranscriptSegment(
                                currentSpeaker,
                                result,
                                sessionOffsetSeconds,
                            );
                            if (segMaxEndTime > observedMaxEndTime) {
                                observedMaxEndTime = segMaxEndTime;
                            }
                        } catch (error) {
                            console.error('Failed to send transcript to Kinesis:', error);
                        }

                        // Process all results (partial + final) for wake phrase pre-connect and activation
                        this.processTranscriptResult(result);
                    }
                }
            }

            // The TranscriptResultStream ended without throwing. If the meeting
            // is still live, Amazon Transcribe closed the session server-side
            // (idle / duration limit / transient close). Signal writeAudio to
            // stop its spin loop so this session's Promise.all resolves and the
            // retry loop reconnects instead of hanging with a dead pipe
            // (GitHub #292). Mirrors the WebSocket transcriber's clean-close
            // reconnect.
            if (this.isTranscribing && details.start) {
                console.log('Transcribe result stream closed server-side while meeting is live - requesting reconnect');
                this.reconnectRequested = true;
            }
        } catch (error: any) {
            // Classify errors as retryable (transient) vs fatal.
            // NOTE: 'non-retryable streaming request' means the Transcribe service explicitly
            // told us not to retry — it must NOT be listed here as retryable.
            const isRetryableError =
                error.code === 'ERR_STREAM_PREMATURE_CLOSE' ||
                error.message?.includes('stream is too big') ||
                error.message?.includes('has expired') ||
                error.message?.includes('http2 request did not get a response');

            if (isLocalTest) {
                if (error.code === 'ERR_STREAM_PREMATURE_CLOSE') {
                    console.log('Transcribe stream closed prematurely (non-fatal in local test) - meeting will continue');
                } else if (error.name === 'AccessDeniedException') {
                    console.log('Transcribe permission denied (non-fatal in local test) - meeting will continue without transcription');
                } else {
                    console.log('Handle transcript events error (non-fatal in local test):', error.message || error);
                }
            } else if (isRetryableError) {
                // Retryable errors - throw to trigger retry loop in startTranscription()
                // but don't let them become uncaught exceptions
                console.error(`Handle transcript events error (retryable): ${error.message || error}`);
                throw error;
            } else {
                // Non-retryable errors - throw to trigger retry/exit
                console.error('Handle transcript events error (fatal in production):', error.message || error);
                throw error;
            }
        } finally {
            // Advance the cumulative offset exactly ONCE, as this session ends,
            // so the next session's timestamps continue the meeting timeline
            // instead of restarting at 0. Runs on both clean close and (re)throw.
            if (observedMaxEndTime > this.transcribeTimeOffsetSeconds) {
                this.transcribeTimeOffsetSeconds = observedMaxEndTime;
            }
        }
    }
}

// Export singleton instance
export const transcriptionService = new TranscriptionService();
