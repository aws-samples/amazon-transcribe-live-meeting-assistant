/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * MicroVM ASR engine client for the Virtual Participant: the single-channel
 * counterpart of the transcriber's per-channel sessions. The engine restarts its
 * segment numbering and clock on every connection, so a reconnect bumps a
 * generation counter and carries a cumulative time offset.
 */
import WebSocket from 'ws';
import { LambdaClient, InvokeCommand } from '@aws-sdk/client-lambda';
import { defaultProvider } from '@aws-sdk/credential-provider-node';
import { createSignedFetcher } from 'aws-sigv4-fetch';

const AWS_REGION = process.env.AWS_REGION || 'us-east-1';
const ASR_LAUNCHER_FUNCTION_ARN = process.env.ASR_LAUNCHER_FUNCTION_ARN || '';
// Development escape hatch: connect straight to a locally running ASR server.
const ASR_DIRECT_ENDPOINT = process.env.ASR_DIRECT_ENDPOINT || '';
const ASR_PORT = 8080;
export const ASR_SAMPLE_RATE = 16000;
const ASR_READY_TIMEOUT_MS = parseInt(process.env.ASR_READY_TIMEOUT_MS || '30000', 10);
const ASR_FINISH_TIMEOUT_MS = parseInt(process.env.ASR_FINISH_TIMEOUT_MS || '5000', 10);
// Audio held while the MicroVM starts or a session reconnects: 60 s of 16 kHz mono PCM.
const ASR_MAX_PENDING_BYTES = parseInt(process.env.ASR_MAX_PENDING_BYTES || String(16000 * 2 * 60), 10);
// Outbound frame: 100 ms. Backlog frame when flushing after a (re)connect: 5 s.
const ASR_SEND_CHUNK_BYTES = parseInt(process.env.ASR_SEND_CHUNK_BYTES || '3200', 10);
const ASR_BACKLOG_FRAME_BYTES = parseInt(process.env.ASR_BACKLOG_FRAME_BYTES || String(16000 * 2 * 5), 10);
const ASR_MAX_RETRIES = parseInt(process.env.ASR_MAX_RETRIES || '5', 10);
const ASR_RETRY_BACKOFF_MS = parseInt(process.env.ASR_RETRY_BACKOFF_MS || '2000', 10);
const ASR_MAX_BACKOFF_MS = parseInt(process.env.ASR_MAX_BACKOFF_MS || '10000', 10);
const ASR_MIN_BACKOFF_MS = parseInt(process.env.ASR_MIN_BACKOFF_MS || '500', 10);
// A session must stay up this long after `ready` before its retry counter is
// forgiven, so an engine that dies right after ready cannot reconnect forever.
const ASR_HEALTHY_SESSION_MS = parseInt(process.env.ASR_HEALTHY_SESSION_MS || '10000', 10);

const isLocalTest = process.env.LOCAL_TEST === 'true';

export type AsrEngineName = 'transcribe' | 'microvm';

/** The ASR Config page's switches that concern a Virtual Participant. */
export interface AsrRuntimeSwitches {
    virtualParticipantEngineMicrovm: boolean;
    diarizeVirtualParticipant: boolean;
}

export const DEFAULT_SWITCHES: AsrRuntimeSwitches = {
    virtualParticipantEngineMicrovm: false,
    diarizeVirtualParticipant: false,
};

export interface AsrLease {
    endpointUrl: string;
    microvmId?: string;
    authToken?: string;
}

/** One transcript row from the engine, on the meeting timeline (seconds). */
export interface AsrSegment {
    segmentId: string;
    startSec: number;
    endSec: number;
    text: string;
    isPartial: boolean;
    /** Engine voice id (spk_0, spk_1, ...) when diarization is active; otherwise undefined. */
    speaker?: string;
}

export type SegmentHandler = (segment: AsrSegment) => void;

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
    effective_config?: { diarize?: boolean };
}

export const isMicrovmAsrConfigured = (): boolean =>
    ASR_LAUNCHER_FUNCTION_ARN.length > 0 || ASR_DIRECT_ENDPOINT.length > 0;

/**
 * Per-VP ASR_ENGINE wins (local testing), then the deployment's runtime setting;
 * an engine the deployment cannot serve falls back to Amazon Transcribe.
 */
export function resolveVpAsrEngine(
    switches: AsrRuntimeSwitches,
    override: string | undefined = process.env.ASR_ENGINE,
): AsrEngineName {
    const requested =
        (override || '').trim().toLowerCase() ||
        (switches.virtualParticipantEngineMicrovm ? 'microvm' : 'transcribe');
    if (requested !== 'microvm') {
        return 'transcribe';
    }
    if (!isMicrovmAsrConfigured()) {
        console.warn('[ASR] MicroVM ASR requested but this deployment has no ASR launcher configured; using Amazon Transcribe.');
        return 'transcribe';
    }
    return 'microvm';
}

const GET_ASR_CONFIG = `
  query GetAsrConfig($AsrConfigId: ID!) {
    getAsrConfig(AsrConfigId: $AsrConfigId) {
      virtualParticipantEngineMicrovm
      diarizeVirtualParticipant
    }
  }
`;

/** Read the switches through AppSync (IAM-signed). Never throws: a failed read means Amazon Transcribe. */
export async function fetchAsrRuntimeSwitches(
    graphqlEndpoint: string = process.env.GRAPHQL_ENDPOINT || '',
    fetchImpl: typeof fetch | undefined = undefined,
): Promise<AsrRuntimeSwitches> {
    if (!graphqlEndpoint) {
        return { ...DEFAULT_SWITCHES };
    }
    try {
        const signedFetch = fetchImpl ?? createSignedFetcher({ service: 'appsync', region: AWS_REGION });
        const response = await signedFetch(graphqlEndpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: GET_ASR_CONFIG, variables: { AsrConfigId: 'CustomAsrConfig' } }),
        });
        if (!response.ok) {
            console.warn(`[ASR] getAsrConfig returned HTTP ${response.status}; using defaults`);
            return { ...DEFAULT_SWITCHES };
        }
        const body = (await response.json()) as {
            data?: {
                getAsrConfig?: {
                    virtualParticipantEngineMicrovm?: boolean;
                    diarizeVirtualParticipant?: boolean;
                } | null;
            };
            errors?: unknown[];
        };
        if (body.errors?.length) {
            console.warn(`[ASR] getAsrConfig errors: ${JSON.stringify(body.errors)}; using defaults`);
            return { ...DEFAULT_SWITCHES };
        }
        const record = body.data?.getAsrConfig;
        return {
            virtualParticipantEngineMicrovm: record?.virtualParticipantEngineMicrovm === true,
            diarizeVirtualParticipant: record?.diarizeVirtualParticipant === true,
        };
    } catch (error: any) {
        console.warn(`[ASR] could not read the ASR config; using defaults: ${error?.message || error}`);
        return { ...DEFAULT_SWITCHES };
    }
}

// --- speaker labels ---------------------------------------------------------

/** Normalise an engine voice id to the `spk_N` form used everywhere else in LMA. */
export const formatSpeakerLabel = (raw: string | null | undefined): string | undefined => {
    const trimmed = (raw ?? '').trim();
    if (trimmed.length === 0) {
        return undefined;
    }
    return trimmed.startsWith('spk_') ? trimmed : `spk_${trimmed}`;
};

/** Roster name plus the engine's voice id when diarization is on, in the transcriber's label format. */
export const speakerNameFor = (rosterName: string, voiceId: string | null | undefined): string => {
    const label = formatSpeakerLabel(voiceId);
    return label === undefined ? rosterName : `${rosterName} (${label})`;
};

// --- launcher ---------------------------------------------------------------

let lambdaClient: LambdaClient | undefined;
const lambda = (): LambdaClient => {
    if (!lambdaClient) {
        lambdaClient = new LambdaClient({
            region: AWS_REGION,
            ...(isLocalTest ? { credentials: defaultProvider() } : {}),
        });
    }
    return lambdaClient;
};

const invokeLauncher = async (payload: Record<string, string>): Promise<Record<string, string> | undefined> => {
    if (!ASR_LAUNCHER_FUNCTION_ARN) {
        return undefined;
    }
    try {
        const response = await lambda().send(
            new InvokeCommand({
                FunctionName: ASR_LAUNCHER_FUNCTION_ARN,
                Payload: Buffer.from(JSON.stringify(payload)),
            }),
        );
        if (response.FunctionError) {
            console.error(
                `[ASR] launcher ${payload.action} failed: ${response.FunctionError} ${
                    response.Payload ? Buffer.from(response.Payload).toString('utf8') : ''
                }`,
            );
            return undefined;
        }
        const body = response.Payload
            ? (JSON.parse(Buffer.from(response.Payload).toString('utf8')) as Record<string, string>)
            : undefined;
        if (!body || String(body.ok) !== 'true') {
            console.error(`[ASR] launcher ${payload.action} refused: ${body ? body.reason : 'no response'}`);
            return undefined;
        }
        return body;
    } catch (error: any) {
        console.error(`[ASR] launcher ${payload.action} error: ${error?.message || error}`);
        return undefined;
    }
};

export const acquireLease = async (callId: string): Promise<AsrLease | undefined> => {
    if (ASR_DIRECT_ENDPOINT) {
        console.log(`[ASR] using ASR_DIRECT_ENDPOINT ${ASR_DIRECT_ENDPOINT} (no MicroVM launch)`);
        return { endpointUrl: ASR_DIRECT_ENDPOINT };
    }
    const started = Date.now();
    const body = await invokeLauncher({ action: 'acquire', callId });
    if (!body) {
        return undefined;
    }
    console.log(`[ASR] acquired ASR MicroVM ${body.microvmId} in ${((Date.now() - started) / 1000).toFixed(1)}s`);
    return { endpointUrl: `wss://${body.endpoint}`, microvmId: body.microvmId, authToken: body.authToken };
};

export const releaseLease = async (microvmId: string): Promise<void> => {
    await invokeLauncher({ action: 'release', microvmId });
};

export const subprotocols = (authToken?: string): string[] =>
    authToken
        ? ['lambda-microvms', `lambda-microvms.authentication.${authToken}`, `lambda-microvms.port.${ASR_PORT}`]
        : [];

const backoffDelay = (attempt: number): number =>
    Math.max(ASR_MIN_BACKOFF_MS, Math.min(attempt * ASR_RETRY_BACKOFF_MS, ASR_MAX_BACKOFF_MS));

/**
 * The engine's ingest queue is bounded (64 frames) and drops rather than grows, so
 * a backlog goes out as a few large frames instead of hundreds of 100 ms ones.
 */
export const coalesceBacklog = (pending: Buffer[], maxFrameBytes: number = ASR_BACKLOG_FRAME_BYTES): Buffer[] => {
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

// --- session ----------------------------------------------------------------

export interface MicrovmAsrSessionOptions {
    callId: string;
    lease: AsrLease;
    diarize: boolean;
    onSegment: SegmentHandler;
    /** False once the meeting is over, so a closed socket is not reconnected. */
    isMeetingLive: () => boolean;
}

/** The VP's single ASR session: a WebSocket to the MicroVM plus message-to-row mapping. */
export class MicrovmAsrSession {
    private ws: WebSocket | null = null;
    private open = false;
    private finished = false;
    private gaveUp = false;
    private pending: Buffer[] = [];
    private pendingBytes = 0;
    private outbound: Buffer[] = [];
    private outboundBytes = 0;
    private droppedBytes = 0;
    private generation = 0;
    private timeOffsetSeconds = 0;
    private observedMaxEnd = 0;
    private attempt = 0;
    private readyAt = 0;
    private readyResolve: ((ready: boolean) => void) | null = null;
    private terminationResolve: (() => void) | null = null;
    private diarizeEffective = false;
    private lease: AsrLease;

    constructor(private readonly options: MicrovmAsrSessionOptions) {
        this.lease = options.lease;
    }

    get speakerLabelsActive(): boolean {
        return this.diarizeEffective;
    }

    /** True after ASR_MAX_RETRIES consecutive failed reconnects: the session is dead. */
    get hasGivenUp(): boolean {
        return this.gaveUp;
    }

    get microvmId(): string | undefined {
        return this.lease.microvmId;
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
        this.outbound.push(monoPcm);
        this.outboundBytes += monoPcm.length;
        if (this.outboundBytes >= ASR_SEND_CHUNK_BYTES) {
            this.flushOutbound();
        }
    }

    private flushOutbound(): void {
        if (this.outboundBytes === 0) {
            return;
        }
        const frame = this.outbound.length === 1 ? this.outbound[0] : Buffer.concat(this.outbound);
        this.outbound = [];
        this.outboundBytes = 0;
        this.send(frame);
    }

    private send(frame: Buffer): void {
        if (this.open && this.ws?.readyState === WebSocket.OPEN) {
            try {
                this.ws.send(frame);
            } catch (error: any) {
                console.log(`[ASR] send failed: ${error?.message || error}`);
            }
            return;
        }
        // Not connected yet (the MicroVM is still starting) or reconnecting: buffer,
        // so the opening seconds of a meeting are not lost while it boots.
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
            } catch (error: any) {
                console.log(`[ASR] eos failed: ${error?.message || error}`);
            }
        }
        this.terminationResolve = null;
        this.close();
        if (this.droppedBytes > 0) {
            console.warn(
                `[ASR] dropped ${(this.droppedBytes / (ASR_SAMPLE_RATE * 2)).toFixed(1)}s of audio while disconnected`,
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

    private settleReady(ready: boolean): void {
        const resolve = this.readyResolve;
        this.readyResolve = null;
        resolve?.(ready);
    }

    private connect(): void {
        if (this.finished) {
            return;
        }
        let socket: WebSocket;
        try {
            socket = new WebSocket(this.lease.endpointUrl, subprotocols(this.lease.authToken));
        } catch (error: any) {
            console.error(`[ASR] could not open ${this.lease.endpointUrl}: ${error?.message || error}`);
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
                diarize: this.options.diarize,
                max_speakers: 0,
                // Nothing else: the threshold, utterance floor and turn-cut
                // behaviour are the bundle's operating point baked into the image.
            };
            try {
                socket.send(JSON.stringify(config));
                for (const frame of coalesceBacklog(this.pending)) {
                    socket.send(frame);
                }
            } catch (error: any) {
                console.error(`[ASR] handshake failed: ${error?.message || error}`);
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
            } catch (error: any) {
                console.warn(`[ASR] unparseable frame: ${error?.message || error}`);
                return;
            }
            this.onMessage(message);
        });

        socket.on('error', (error: Error) => {
            console.error(`[ASR] websocket error: ${error.message}`);
        });

        socket.on('close', (code: number, reason: Buffer) => {
            this.open = false;
            if (this.finished || !this.options.isMeetingLive()) {
                return;
            }
            console.warn(`[ASR] session closed unexpectedly (${code} ${reason?.toString() || ''}); reconnecting`);
            this.settleReady(false);
            void this.scheduleReconnect();
        });
    }

    private async scheduleReconnect(): Promise<void> {
        if (this.finished || !this.options.isMeetingLive()) {
            return;
        }
        if (this.readyAt && Date.now() - this.readyAt >= ASR_HEALTHY_SESSION_MS) {
            this.attempt = 0;
        }
        this.readyAt = 0;
        this.attempt += 1;
        if (this.attempt > ASR_MAX_RETRIES) {
            console.error(
                `[ASR] giving up after ${ASR_MAX_RETRIES} consecutive failures; the rest of this meeting will not be transcribed by the MicroVM engine`,
            );
            this.gaveUp = true;
            return;
        }
        await new Promise((resolve) => setTimeout(resolve, backoffDelay(this.attempt)));
        if (this.finished || !this.options.isMeetingLive()) {
            return;
        }

        // Segment numbering and the engine clock restart on a new connection.
        this.generation += 1;
        this.timeOffsetSeconds = this.observedMaxEnd;

        // Mint a fresh token: a long meeting outlives the one that opened the first connection.
        if (this.lease.microvmId) {
            const body = await invokeLauncher({ action: 'token', microvmId: this.lease.microvmId });
            if (body) {
                this.lease = { ...this.lease, authToken: body.authToken };
            }
        }
        if (this.finished || !this.options.isMeetingLive()) {
            return;
        }
        this.connect();
    }

    private onMessage(message: AsrServerMessage): void {
        switch (message.type) {
            case 'ready':
                // The retry counter is forgiven in scheduleReconnect only once the session
                // has stayed up for ASR_HEALTHY_SESSION_MS, not here.
                this.readyAt = Date.now();
                this.diarizeEffective = message.effective_config?.diarize === true;
                if (this.options.diarize && !this.diarizeEffective) {
                    console.warn(
                        '[ASR] per-voice labels were requested but this ASR image has no speaker model baked in; rows carry the roster name only',
                    );
                }
                console.log(
                    `[ASR] session ready (generation ${this.generation}, diarize=${this.diarizeEffective}, timeOffset=${this.timeOffsetSeconds.toFixed(2)}s)`,
                );
                this.settleReady(true);
                break;
            case 'partial':
            case 'final':
                this.emitSegment(message);
                break;
            case 'termination':
                console.log(
                    `[ASR] terminated after ${message.audio_seconds?.toFixed(1) ?? '?'}s audio, ${message.segments ?? 0} segment(s)`,
                );
                this.terminationResolve?.();
                this.terminationResolve = null;
                break;
            case 'error':
                console.error(`[ASR] engine error ${message.code}: ${message.message}`);
                this.settleReady(false);
                break;
            default:
                break;
        }
    }

    private emitSegment(message: AsrServerMessage): void {
        const text = (message.text || '').trim();
        if (text.length === 0) {
            return;
        }
        const startSec = (message.start ?? 0) + this.timeOffsetSeconds;
        const endSec = (message.end ?? message.start ?? 0) + this.timeOffsetSeconds;
        if (endSec > this.observedMaxEnd) {
            this.observedMaxEnd = endSec;
        }
        // The id excludes the speaker: a partial's provisional label can be corrected
        // by its final, and both must land on one row.
        this.options.onSegment({
            segmentId: `vp-g${this.generation}-s${message.segment ?? 0}`,
            startSec,
            endSec,
            text,
            isPartial: message.type !== 'final',
            speaker: message.speaker ?? undefined,
        });
    }
}
