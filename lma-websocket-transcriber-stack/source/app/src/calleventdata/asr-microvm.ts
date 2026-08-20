/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * MicroVM ASR engine: streaming transcription and speaker diarization on an
 * on-demand AWS Lambda MicroVM, as an alternative to Amazon Transcribe.
 *
 * One MicroVM per meeting carries one WebSocket session per audio channel. The
 * engine returns the speaker label together with the text it was derived from,
 * so unlike pairing a separate diarizer with a separate ASR service there are no
 * two timelines to align.
 *
 * Transcript rows are written through the same writeSegmentToKds() the Amazon
 * Transcribe path uses, so nothing downstream of Kinesis is engine-aware.
 */
import { FastifyInstance } from 'fastify';
import WebSocket from 'ws';
import { LambdaClient, InvokeCommand } from '@aws-sdk/client-lambda';

import { AsrRuntimeConfig, getAsrRuntimeConfig, isSpeakerModelMeasured } from './asr-config';
import { CallMetaData, SocketCallData } from './eventtypes';
import { TranscriptSegmentRecord, diarizationSettingsFor, writeSegmentToKds } from './transcribe';
import { anyChannelDiarized, diarizationEnabledFor } from './diarization';
import { normalizeErrorForLogging } from '../utils/common';
import {
    ASR_CHANNEL_IDS,
    ASR_SAMPLE_RATE,
    AsrChannelId,
    ChannelResampler,
    SpeakerNameRegistry,
    StereoDeinterleaver,
    channelToTranscriptChannel,
} from './asr-audio';

const AWS_REGION = process.env['AWS_REGION'] || 'us-east-1';
const ASR_ENGINE_DEFAULT = (process.env['ASR_ENGINE_DEFAULT'] || 'transcribe').toLowerCase();
const ASR_LAUNCHER_FUNCTION_ARN = process.env['ASR_LAUNCHER_FUNCTION_ARN'] || '';
// The diarization knobs and the deployment default engine come from asr-config.ts,
// which layers the ASR Config table's overrides over these same env defaults.
// Development escape hatch: connect straight to a locally running ASR server
// (ws://host:8080) instead of launching a MicroVM.
const ASR_DIRECT_ENDPOINT = process.env['ASR_DIRECT_ENDPOINT'] || '';
const ASR_READY_TIMEOUT_MS = parseInt(process.env['ASR_READY_TIMEOUT_MS'] || '30000', 10);
const ASR_FINISH_TIMEOUT_MS = parseInt(process.env['ASR_FINISH_TIMEOUT_MS'] || '5000', 10);
// Audio held while the MicroVM starts or a session reconnects, as bytes of 16 kHz
// mono 16-bit PCM. The default is 60 seconds, which covers a MicroVM launch.
const ASR_MAX_PENDING_BYTES = parseInt(
    process.env['ASR_MAX_PENDING_BYTES'] || String(16000 * 2 * 60),
    10
);
// Outbound frame size: 100 ms of 16 kHz mono 16-bit PCM.
const ASR_SEND_CHUNK_BYTES = parseInt(process.env['ASR_SEND_CHUNK_BYTES'] || '3200', 10);
// Frame size used when flushing a backlog: 5 seconds of the same PCM.
const ASR_BACKLOG_FRAME_BYTES = parseInt(
    process.env['ASR_BACKLOG_FRAME_BYTES'] || String(16000 * 2 * 5),
    10
);
const ASR_MAX_RETRIES = parseInt(process.env['ASR_MAX_RETRIES'] || '5', 10);
const ASR_RETRY_BACKOFF_MS = parseInt(process.env['ASR_RETRY_BACKOFF_MS'] || '2000', 10);
const ASR_MAX_BACKOFF_MS = parseInt(process.env['ASR_MAX_BACKOFF_MS'] || '10000', 10);
const ASR_MIN_BACKOFF_MS = parseInt(process.env['ASR_MIN_BACKOFF_MS'] || '500', 10);
const ASR_PORT = 8080;

const lambdaClient = new LambdaClient({ region: AWS_REGION });

export type AsrEngineName = 'transcribe' | 'microvm';

export interface AsrLease {
    endpointUrl: string;
    microvmId?: string;
    authToken?: string;
}

interface AsrSessionOptions {
    diarize: boolean;
    maxSpeakers: number;
    /** Undefined leaves the bundle's calibrated value baked into the image in force. */
    speakerThreshold?: number;
    endpointingMs: number;
    minSegmentMs?: number;
    requireCorroboration?: boolean;
    splitOnSpeakerChange?: boolean;
    liveTurnCut?: boolean;
    turnCutIntervalMs?: number;
    maxOpenSegmentMs?: number;
}

export interface AsrSessionSet {
    lease: AsrLease;
    deinterleaver: StereoDeinterleaver;
    sessions: Map<AsrChannelId, AsrChannelSession>;
}

/** Injected in tests so the engine-to-transcript mapping runs without Kinesis. */
export type SegmentWriter = (
    segment: TranscriptSegmentRecord,
    callMetadata: CallMetaData,
    server: FastifyInstance
) => Promise<void>;

interface AsrServerMessage {
    type: string;
    segment?: number;
    text?: string;
    start?: number;
    end?: number;
    speaker?: string | null;
    code?: string;
    message?: string;
    audio_seconds?: number;
    segments?: number;
    effective_config?: { diarize?: boolean; sample_rate?: number };
}

export const isMicrovmAsrConfigured = (): boolean =>
    ASR_LAUNCHER_FUNCTION_ARN.length > 0 || ASR_DIRECT_ENDPOINT.length > 0;

/** Whether a meeting that cannot start the MicroVM engine falls back to Amazon Transcribe. */
export const shouldFallbackToTranscribe = (): boolean =>
    (process.env['ASR_FALLBACK_TO_TRANSCRIBE'] || 'true') === 'true';

/**
 * Which engine transcribes this meeting.
 *
 * The engine is the DEPLOYMENT's choice and diarization is the CLIENT's: both
 * engines partition speakers now, so asking for speaker labels no longer implies
 * an engine. A client may still name one explicitly. A request the deployment
 * cannot serve falls back to Amazon Transcribe rather than failing.
 */
export const resolveAsrEngine = (
    callMetaData: CallMetaData,
    server?: FastifyInstance,
    runtime?: AsrRuntimeConfig
): AsrEngineName => {
    const deploymentDefault = runtime
        ? (runtime.engineDefaultMicrovm ? 'microvm' : 'transcribe')
        : ASR_ENGINE_DEFAULT;
    const requested = callMetaData.asrEngine?.toLowerCase() || deploymentDefault;
    if (requested !== 'microvm') {
        return 'transcribe';
    }
    if (!isMicrovmAsrConfigured()) {
        server?.log.warn(
            `[ASR]: [${callMetaData.callId}] - MicroVM ASR was requested but this deployment has no ASR launcher configured; using Amazon Transcribe.`
        );
        return 'transcribe';
    }
    return 'microvm';
};

const invokeLauncher = async (
    payload: Record<string, string>,
    server: FastifyInstance
): Promise<Record<string, string> | undefined> => {
    if (!ASR_LAUNCHER_FUNCTION_ARN) {
        return undefined;
    }
    try {
        const response = await lambdaClient.send(
            new InvokeCommand({
                FunctionName: ASR_LAUNCHER_FUNCTION_ARN,
                Payload: Buffer.from(JSON.stringify(payload)),
            })
        );
        if (response.FunctionError) {
            server.log.error(
                `[ASR]: launcher ${payload['action']} failed: ${response.FunctionError} ${
                    response.Payload ? Buffer.from(response.Payload).toString('utf8') : ''
                }`
            );
            return undefined;
        }
        const body = response.Payload
            ? (JSON.parse(Buffer.from(response.Payload).toString('utf8')) as Record<string, string>)
            : undefined;
        if (!body || String(body['ok']) !== 'true') {
            server.log.error(
                `[ASR]: launcher ${payload['action']} refused: ${body ? body['reason'] : 'no response'}`
            );
            return undefined;
        }
        return body;
    } catch (error) {
        server.log.error(
            `[ASR]: launcher ${payload['action']} error: ${normalizeErrorForLogging(error)}`
        );
        return undefined;
    }
};

export const acquireLease = async (
    callId: string,
    server: FastifyInstance
): Promise<AsrLease | undefined> => {
    if (ASR_DIRECT_ENDPOINT) {
        server.log.info(
            `[ASR]: [${callId}] - Using ASR_DIRECT_ENDPOINT ${ASR_DIRECT_ENDPOINT} (no MicroVM launch).`
        );
        return { endpointUrl: ASR_DIRECT_ENDPOINT };
    }
    const started = Date.now();
    const body = await invokeLauncher({ action: 'acquire', callId }, server);
    if (!body) {
        return undefined;
    }
    server.log.info(
        `[ASR]: [${callId}] - Acquired ASR MicroVM ${body['microvmId']} in ${(
            (Date.now() - started) /
            1000
        ).toFixed(1)}s`
    );
    return {
        endpointUrl: `wss://${body['endpoint']}`,
        microvmId: body['microvmId'],
        authToken: body['authToken'],
    };
};

export const releaseLease = async (
    microvmId: string,
    server: FastifyInstance
): Promise<void> => {
    await invokeLauncher({ action: 'release', microvmId }, server);
};

export const subprotocols = (authToken?: string): string[] =>
    authToken
        ? [
            'lambda-microvms',
            `lambda-microvms.authentication.${authToken}`,
            `lambda-microvms.port.${ASR_PORT}`,
        ]
        : [];

const backoffDelay = (attempt: number): number =>
    Math.max(ASR_MIN_BACKOFF_MS, Math.min(attempt * ASR_RETRY_BACKOFF_MS, ASR_MAX_BACKOFF_MS));

/**
 * Merge buffered audio into a few large frames before flushing it.
 *
 * The engine's ingest queue is bounded (64 frames) and DROPS frames rather than
 * growing without bound, so sending a whole backlog as hundreds of 100 ms frames
 * in one tight loop loses audio. Each merged frame is one queue entry, so a
 * 60-second backlog becomes 12 entries instead of 600 and nothing is dropped; the
 * engine then decodes the catch-up faster than real time.
 */
export const coalesceBacklog = (
    pending: Buffer[],
    maxFrameBytes: number = ASR_BACKLOG_FRAME_BYTES
): Buffer[] => {
    const frames: Buffer[] = [];
    let batch: Buffer[] = [];
    let batchBytes = 0;
    for (const chunk of pending) {
        batch.push(chunk);
        batchBytes += chunk.length;
        if (batchBytes >= maxFrameBytes) {
            frames.push(batch.length === 1 ? batch[0] : Buffer.concat(batch));
            batch = [];
            batchBytes = 0;
        }
    }
    if (batchBytes > 0) {
        frames.push(batch.length === 1 ? batch[0] : Buffer.concat(batch));
    }
    return frames;
};

/**
 * One channel's ASR session: a WebSocket to the MicroVM plus the mapping from
 * engine messages to transcript rows.
 *
 * The engine restarts its segment numbering and its clock on every connection,
 * so a reconnect bumps a generation counter (keeping SegmentIds unique) and
 * carries a cumulative time offset (keeping the meeting timeline monotonic) —
 * the same approach the Amazon Transcribe path takes for its reconnects.
 */
export class AsrChannelSession {
    private ws: WebSocket | null = null;

    private open = false;

    private finished = false;

    private pending: Buffer[] = [];

    private pendingBytes = 0;

    private outbound: Buffer[] = [];

    private outboundBytes = 0;

    private droppedBytes = 0;

    private generation = 0;

    private timeOffsetSeconds = 0;

    private observedMaxEnd = 0;

    private attempt = 0;

    private resampler: ChannelResampler;

    private writes: Promise<void> = Promise.resolve();

    private readyResolve: ((ready: boolean) => void) | null = null;

    private terminationResolve: (() => void) | null = null;

    private diarizeEffective = false;

    constructor(
        private readonly server: FastifyInstance,
        private readonly socketData: SocketCallData,
        private readonly channelId: AsrChannelId,
        private lease: AsrLease,
        private readonly registry: SpeakerNameRegistry,
        private readonly options: AsrSessionOptions,
        private readonly writeSegment: SegmentWriter = writeSegmentToKds
    ) {
        this.resampler = new ChannelResampler(
            socketData.callMetadata.samplingRate || ASR_SAMPLE_RATE
        );
    }

    private get callId(): string {
        return this.socketData.callMetadata.callId;
    }

    get speakerLabelsActive(): boolean {
        return this.diarizeEffective;
    }

    /** Connect and wait for the engine's ready handshake. */
    async start(): Promise<boolean> {
        const ready = new Promise<boolean>((resolve) => {
            this.readyResolve = resolve;
        });
        const timer = setTimeout(() => this.settleReady(false), ASR_READY_TIMEOUT_MS);
        this.connect();
        const result = await ready;
        clearTimeout(timer);
        return result;
    }

    pushPcm(monoPcm: Buffer): void {
        if (this.finished || monoPcm.length === 0) {
            return;
        }
        const resampled = this.resampler.process(monoPcm);
        if (resampled.length === 0) {
            return;
        }
        // A browser worklet posts 128 frames at a time — about 43 samples per
        // channel once resampled to 16 kHz — so sending each one straight out
        // would be hundreds of tiny WebSocket frames per second per channel.
        // Coalesce into ~100 ms frames instead; the added latency is immaterial
        // next to the engine's endpointing.
        this.outbound.push(resampled);
        this.outboundBytes += resampled.length;
        if (this.outboundBytes >= ASR_SEND_CHUNK_BYTES) {
            this.flushOutbound();
        }
    }

    private flushOutbound(): void {
        if (this.outboundBytes === 0) {
            return;
        }
        const frame =
            this.outbound.length === 1 ? this.outbound[0] : Buffer.concat(this.outbound);
        this.outbound = [];
        this.outboundBytes = 0;
        this.send(frame);
    }

    private send(frame: Buffer): void {
        if (this.open && this.ws?.readyState === WebSocket.OPEN) {
            try {
                this.ws.send(frame);
            } catch (error) {
                this.server.log.debug(
                    `[ASR]: [${this.callId}][${this.channelId}] - send failed: ${normalizeErrorForLogging(error)}`
                );
            }
            return;
        }
        // Not connected yet (the MicroVM is still starting) or reconnecting.
        // Buffering here is what stops the opening seconds of a meeting being
        // lost while the MicroVM boots.
        if (this.pendingBytes + frame.length <= ASR_MAX_PENDING_BYTES) {
            this.pending.push(frame);
            this.pendingBytes += frame.length;
        } else {
            this.droppedBytes += frame.length;
        }
    }

    /** Flush the tail utterance, then close. */
    async finish(): Promise<void> {
        if (this.finished) {
            return;
        }
        this.finished = true;
        this.flushOutbound();
        if (this.open && this.ws?.readyState === WebSocket.OPEN) {
            const terminated = new Promise<void>((resolve) => {
                this.terminationResolve = resolve;
            });
            try {
                this.ws.send(JSON.stringify({ type: 'eos' }));
                await Promise.race([
                    terminated,
                    new Promise<void>((resolve) => setTimeout(resolve, ASR_FINISH_TIMEOUT_MS)),
                ]);
            } catch (error) {
                this.server.log.debug(
                    `[ASR]: [${this.callId}][${this.channelId}] - eos failed: ${normalizeErrorForLogging(error)}`
                );
            }
        }
        this.terminationResolve = null;
        this.close();
        await this.writes;
        if (this.droppedBytes > 0) {
            this.server.log.warn(
                `[ASR]: [${this.callId}][${this.channelId}] - dropped ${(
                    this.droppedBytes /
                    (ASR_SAMPLE_RATE * 2)
                ).toFixed(1)}s of audio while disconnected.`
            );
        }
    }

    close(): void {
        this.open = false;
        const socket = this.ws;
        this.ws = null;
        this.pending = [];
        this.pendingBytes = 0;
        if (socket) {
            try {
                socket.removeAllListeners();
                socket.close();
            } catch {
                // already gone
            }
        }
    }

    updateLease(lease: AsrLease): void {
        this.lease = lease;
    }

    private settleReady(ready: boolean): void {
        const resolve = this.readyResolve;
        this.readyResolve = null;
        resolve?.(ready);
    }

    private connect(): void {
        let socket: WebSocket;
        try {
            socket = new WebSocket(this.lease.endpointUrl, subprotocols(this.lease.authToken));
        } catch (error) {
            this.server.log.error(
                `[ASR]: [${this.callId}][${this.channelId}] - could not open ${
                    this.lease.endpointUrl
                }: ${normalizeErrorForLogging(error)}`
            );
            this.settleReady(false);
            void this.scheduleReconnect();
            return;
        }
        this.ws = socket;

        socket.on('open', () => {
            this.open = true;
            const config = {
                type: 'config',
                sample_rate: ASR_SAMPLE_RATE,
                encoding: 'pcm_s16le',
                channels: 1,
                interim_results: true,
                word_timestamps: false,
                endpointing_ms: this.options.endpointingMs,
                diarize: this.options.diarize,
                max_speakers: this.options.maxSpeakers,
                ...(this.options.speakerThreshold === undefined
                    ? {}
                    : { speaker_threshold: this.options.speakerThreshold }),
                // Omitted rather than nulled when unset, so the engine keeps
                // whatever the image was built with.
                ...(this.options.minSegmentMs === undefined
                    ? {}
                    : { min_segment_ms: this.options.minSegmentMs }),
                ...(this.options.requireCorroboration === undefined
                    ? {}
                    : { require_corroboration: this.options.requireCorroboration }),
                ...(this.options.splitOnSpeakerChange === undefined
                    ? {}
                    : { split_on_speaker_change: this.options.splitOnSpeakerChange }),
                ...(this.options.liveTurnCut === undefined
                    ? {}
                    : { live_turn_cut: this.options.liveTurnCut }),
                ...(this.options.turnCutIntervalMs === undefined
                    ? {}
                    : { turn_cut_interval_ms: this.options.turnCutIntervalMs }),
                ...(this.options.maxOpenSegmentMs === undefined
                    ? {}
                    : { max_open_segment_ms: this.options.maxOpenSegmentMs }),
            };
            try {
                socket.send(JSON.stringify(config));
                for (const frame of coalesceBacklog(this.pending)) {
                    socket.send(frame);
                }
            } catch (error) {
                this.server.log.error(
                    `[ASR]: [${this.callId}][${this.channelId}] - handshake failed: ${normalizeErrorForLogging(error)}`
                );
            }
            this.pending = [];
            this.pendingBytes = 0;
        });

        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                return;
            }
            let message: AsrServerMessage;
            try {
                message = JSON.parse(data.toString()) as AsrServerMessage;
            } catch (error) {
                this.server.log.warn(
                    `[ASR]: [${this.callId}][${this.channelId}] - unparseable frame: ${normalizeErrorForLogging(error)}`
                );
                return;
            }
            this.onMessage(message);
        });

        socket.on('error', (error: Error) => {
            this.server.log.error(
                `[ASR]: [${this.callId}][${this.channelId}] - websocket error: ${normalizeErrorForLogging(error)}`
            );
        });

        socket.on('close', (code: number, reason: Buffer) => {
            this.open = false;
            if (this.finished || this.socketData.ended) {
                return;
            }
            this.server.log.warn(
                `[ASR]: [${this.callId}][${this.channelId}] - session closed unexpectedly (${code} ${reason?.toString() || ''}); reconnecting.`
            );
            this.settleReady(false);
            void this.scheduleReconnect();
        });
    }

    private async scheduleReconnect(): Promise<void> {
        if (this.finished || this.socketData.ended) {
            return;
        }
        this.attempt += 1;
        if (this.attempt > ASR_MAX_RETRIES) {
            this.server.log.error(
                `[ASR]: [${this.callId}][${this.channelId}] - giving up after ${ASR_MAX_RETRIES} consecutive failures.`
            );
            return;
        }
        await new Promise((resolve) => setTimeout(resolve, backoffDelay(this.attempt)));
        if (this.finished || this.socketData.ended) {
            return;
        }

        // Segment numbering and the engine clock restart on a new connection.
        this.generation += 1;
        this.timeOffsetSeconds = this.observedMaxEnd;

        // Mint a fresh token rather than reasoning about its remaining TTL: a
        // long meeting outlives the token that opened the first connection.
        if (this.lease.microvmId) {
            const body = await invokeLauncher(
                { action: 'token', microvmId: this.lease.microvmId },
                this.server
            );
            if (body) {
                this.lease = { ...this.lease, authToken: body['authToken'] };
            }
        }
        this.connect();
    }

    private onMessage(message: AsrServerMessage): void {
        switch (message.type) {
            case 'ready':
                this.attempt = 0;
                this.diarizeEffective = message.effective_config?.diarize === true;
                if (this.options.diarize && !this.diarizeEffective) {
                    this.server.log.warn(
                        `[ASR]: [${this.callId}][${this.channelId}] - diarization was requested but this ASR image has no speaker model baked in; transcript will be labelled by channel.`
                    );
                }
                this.server.log.info(
                    `[ASR]: [${this.callId}][${this.channelId}] - session ready (generation ${this.generation}, diarize=${this.diarizeEffective}, timeOffset=${this.timeOffsetSeconds.toFixed(2)}s)`
                );
                this.settleReady(true);
                break;
            case 'partial':
            case 'final':
                this.queueSegment(message);
                break;
            case 'termination':
                this.server.log.info(
                    `[ASR]: [${this.callId}][${this.channelId}] - terminated after ${
                        message.audio_seconds?.toFixed(1) ?? '?'
                    }s audio, ${message.segments ?? 0} segment(s)`
                );
                this.terminationResolve?.();
                this.terminationResolve = null;
                break;
            case 'error':
                this.server.log.error(
                    `[ASR]: [${this.callId}][${this.channelId}] - engine error ${message.code}: ${message.message}`
                );
                this.settleReady(false);
                break;
            default:
                this.server.log.debug(
                    `[ASR]: [${this.callId}][${this.channelId}] - ignoring ${message.type} frame`
                );
        }
    }

    private queueSegment(message: AsrServerMessage): void {
        const text = (message.text || '').trim();
        if (text.length === 0) {
            return;
        }
        const isPartial = message.type !== 'final';
        const start = (message.start ?? 0) + this.timeOffsetSeconds;
        const end = (message.end ?? message.start ?? 0) + this.timeOffsetSeconds;
        if (end > this.observedMaxEnd) {
            this.observedMaxEnd = end;
        }
        // SegmentId deliberately excludes the speaker: a partial's provisional
        // speaker can be corrected by its final, and both must land on one row.
        const segmentId = `${this.channelId}-g${this.generation}-s${message.segment ?? 0}`;
        const speaker = this.registry.nameFor(
            this.channelId,
            message.speaker,
            this.channelName()
        );

        this.writes = this.writes.then(async () => {
            await this.writeSegment(
                {
                    SegmentId: segmentId,
                    Channel: channelToTranscriptChannel(this.channelId),
                    StartTime: start,
                    EndTime: end,
                    Transcript: text,
                    IsPartial: isPartial,
                    Speaker: speaker,
                },
                this.socketData.callMetadata,
                this.server
            );
        });
    }

    private channelName(): string {
        const callMetadata = this.socketData.callMetadata;
        return this.channelId === 'ch_0'
            ? callMetadata.activeSpeaker || 'n/a'
            : callMetadata.agentId || 'n/a';
    }
}

/**
 * Acquire a MicroVM and open one ASR session per audio channel.
 *
 * The sessions are registered on the call BEFORE the MicroVM is acquired, so
 * audio that arrives while it boots is buffered rather than lost — otherwise the
 * opening seconds of every meeting would be missing from the transcript.
 *
 * Returns false if the engine could not be started, so the caller can fall back
 * to Amazon Transcribe for this meeting instead of losing its transcript.
 */
export const startMicrovmAsr = async (
    socketData: SocketCallData,
    server: FastifyInstance
): Promise<boolean> => {
    const callMetadata = socketData.callMetadata;

    const runtime = await getAsrRuntimeConfig(server);
    // Unlike Amazon Transcribe, whose ShowSpeakerLabel is stream-level, this engine
    // runs an independent session per channel — so "diarize the tab but not the
    // microphone" is honoured for real rather than by discarding half the labels.
    const settings = diarizationSettingsFor(callMetadata);
    // A threshold that was never measured against this embedder is not a default,
    // it is a guess — and a wrong guess fragments one speaker into many or merges
    // several into one, which is worse than the channel labels this falls back to.
    // An admin-set threshold (typed, or applied from a calibration run) counts as
    // the measurement.
    const thresholdTrusted = isSpeakerModelMeasured() || runtime.speakerThresholdOverridden;
    if (anyChannelDiarized(settings) && !thresholdTrusted) {
        server.log.warn(
            `[ASR]: [${callMetadata.callId}] - speaker labels withheld: this deployment's speaker model has no measured operating point. Run a calibration from the ASR Config page, or set a speaker threshold there. Transcribing with channel labels instead.`
        );
    }
    const optionsFor = (channelId: AsrChannelId): AsrSessionOptions => ({
        diarize: thresholdTrusted && diarizationEnabledFor(channelId, settings),
        // A client-supplied count wins: only the person in the meeting knows how
        // many people share their microphone (the Upload Audio page asks the same
        // question). 0 still means "discover as many as appear".
        maxSpeakers: callMetadata.maxSpeakers ?? runtime.maxSpeakers,
        speakerThreshold: runtime.speakerThreshold,
        endpointingMs: runtime.endpointingMs,
        minSegmentMs: runtime.minSegmentMs,
        requireCorroboration: runtime.requireCorroboration,
        splitOnSpeakerChange: runtime.splitOnSpeakerChange,
        liveTurnCut: runtime.liveTurnCut,
        turnCutIntervalMs: runtime.turnCutIntervalMs,
        maxOpenSegmentMs: runtime.maxOpenSegmentMs,
    });
    const registry = new SpeakerNameRegistry();
    const sessions = new Map<AsrChannelId, AsrChannelSession>();
    // The endpoint is filled in by updateLease() once the MicroVM is running; the
    // sessions only need it to connect, and until then they buffer.
    const pendingLease: AsrLease = { endpointUrl: '' };
    for (const channelId of ASR_CHANNEL_IDS) {
        sessions.set(
            channelId,
            new AsrChannelSession(
                server,
                socketData,
                channelId,
                pendingLease,
                registry,
                optionsFor(channelId)
            )
        );
    }
    socketData.asr = { lease: pendingLease, deinterleaver: new StereoDeinterleaver(), sessions };

    const lease = await acquireLease(callMetadata.callId, server);
    if (!lease) {
        socketData.asr = undefined;
        return false;
    }
    socketData.asr.lease = lease;
    for (const session of sessions.values()) {
        session.updateLease(lease);
    }

    const results = await Promise.all(
        [...sessions.values()].map((session) => session.start())
    );
    if (!results.every(Boolean)) {
        server.log.error(
            `[ASR]: [${callMetadata.callId}] - one or more ASR sessions failed to start; releasing the MicroVM.`
        );
        await stopMicrovmAsr(socketData, server);
        return false;
    }

    const diarized = [...sessions.entries()]
        .filter(([channelId]) => optionsFor(channelId).diarize)
        .map(([channelId]) => channelId);
    server.log.info(
        `[ASR]: [${callMetadata.callId}] - MicroVM ASR active on ${sessions.size} channel(s), client rate ${callMetadata.samplingRate}Hz, diarized channel(s)=${diarized.length ? diarized.join(',') : 'none'}`
    );
    return true;
};

export const pushAsrAudio = (socketData: SocketCallData, data: Uint8Array): void => {
    const asr = socketData.asr;
    if (!asr) {
        return;
    }
    const split = asr.deinterleaver.split(data);
    for (const [channelId, session] of asr.sessions) {
        session.pushPcm(split[channelId]);
    }
};

export const stopMicrovmAsr = async (
    socketData: SocketCallData,
    server: FastifyInstance
): Promise<void> => {
    const asr = socketData.asr;
    if (!asr) {
        return;
    }
    socketData.asr = undefined;

    await Promise.all(
        [...asr.sessions.values()].map(async (session) => {
            try {
                await session.finish();
            } catch (error) {
                server.log.error(
                    `[ASR]: [${socketData.callMetadata.callId}] - error finishing ASR session: ${normalizeErrorForLogging(error)}`
                );
            }
        })
    );

    if (asr.lease.microvmId) {
        await invokeLauncher(
            { action: 'release', microvmId: asr.lease.microvmId },
            server
        );
        server.log.info(
            `[ASR]: [${socketData.callMetadata.callId}] - released ASR MicroVM ${asr.lease.microvmId}`
        );
    }
};
