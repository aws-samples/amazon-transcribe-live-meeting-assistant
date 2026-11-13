/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
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

import { AsrRuntimeConfig } from './asr-config';
import { CHANNEL_SYSTEM, CallMetaData, SocketCallData } from './eventtypes';
import { pageSpeakersFor } from './speaker-fusion';
import { TranscriptSegmentRecord, diarizationSettingsFor, writeSegmentToKds } from './transcribe';
import { TranscriptionSettings, getSessionSettings } from './session-settings';
import { microvmRowLanguageCode, parseBundleLanguages, transcribeOnlyOptions } from '../config/configuration-store';
import { diarizationEnabledFor } from './diarization';
// Type-only in the other direction, so this is not a runtime cycle.
import { fetchEnrolledVoices } from './voice-enrollment';
import { normalizeErrorForLogging } from '../utils/common';
import { meterMicrovm } from '../metering';
import { recordedAudioSeconds } from './session-lifecycle';
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
// which layers the AsrConfig table's overrides over these same env defaults.
// Development escape hatch: connect straight to a locally running ASR server
// (ws://host:8080) instead of launching a MicroVM.
const ASR_DIRECT_ENDPOINT = process.env['ASR_DIRECT_ENDPOINT'] || '';
const ASR_READY_TIMEOUT_MS = parseInt(process.env['ASR_READY_TIMEOUT_MS'] || '30000', 10);
const ASR_FINISH_TIMEOUT_MS = parseInt(process.env['ASR_FINISH_TIMEOUT_MS'] || '5000', 10);
// How much longer finishing a session waits for the engine's last finals, per
// second of this connection's audio the engine has not yet finalised, and the
// ceiling on the whole wait (finishWaitMs).
const ASR_FINISH_MS_PER_OPEN_SECOND = parseInt(process.env['ASR_FINISH_MS_PER_OPEN_SECOND'] || '2000', 10);
const ASR_FINISH_MAX_WAIT_MS = parseInt(process.env['ASR_FINISH_MAX_WAIT_MS'] || '60000', 10);
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
// How long a session must stay up after `ready` before the retry counter is
// forgiven. Without this an engine that dies right after becoming ready would
// reset the counter each time and reconnect on a short backoff forever.
const ASR_HEALTHY_SESSION_MS = parseInt(process.env['ASR_HEALTHY_SESSION_MS'] || '10000', 10);
const ASR_MIN_BACKOFF_MS = parseInt(process.env['ASR_MIN_BACKOFF_MS'] || '500', 10);
const ASR_PORT = 8080;

const lambdaClient = new LambdaClient({ region: AWS_REGION });

export type AsrEngineName = 'transcribe' | 'microvm';

export interface AsrLease {
    endpointUrl: string;
    microvmId?: string;
    authToken?: string;
    /** When the launch was requested (epoch ms): the start of what is billed. */
    acquiredAtMs?: number;
    /** The MicroVM's configured memory, as the launcher reports it. */
    memoryMiB?: number;
}

/**
 * The size the ASR image is built at when a launcher does not report one: the
 * `BundleMemory` mapping in lma-asr-microvm-stack/template.yaml gives every
 * bundle 8 GiB.
 */
const DEFAULT_ASR_MICROVM_MEMORY_MIB = 8192;

/** Record a released MicroVM's lifetime for the meeting cost estimate. */
const meterLease = async (callId: string, lease: AsrLease, server: FastifyInstance): Promise<void> => {
    if (!lease.microvmId || !lease.acquiredAtMs) {
        return;
    }
    const reported = typeof lease.memoryMiB === 'number' && lease.memoryMiB > 0;
    await meterMicrovm(
        callId,
        (Date.now() - lease.acquiredAtMs) / 1000,
        reported ? (lease.memoryMiB as number) : DEFAULT_ASR_MICROVM_MEMORY_MIB,
        { size: reported ? 'reported by the launcher' : 'assumed from the image default' },
        server.log
    );
};

/** What a session asks of the engine; the operating point is baked into the image, not negotiable. */
interface AsrSessionOptions {
    diarize: boolean;
    /** Cap on distinct voices for this channel; 0 discovers as many as appear. */
    maxSpeakers: number;
    /**
     * Voices somebody consented to enroll, so their speech carries their name
     * from the first word instead of `spk_0` until a human renames it.
     *
     * Empty unless the deployment has voice enrollment switched on AND the
     * person has an active consent to the terms currently in force -- the
     * decision about which enrollments may be used is made before this, by the
     * code that reads them, not here.
     */
    enrolledVoices?: EnrolledVoice[];
    /**
     * Part of every SegmentId this session writes, so a later session for the
     * same meeting (a client that reconnects to another task, or after this one
     * ended the meeting) cannot overwrite this one's rows. Defaults to the time
     * the session was created.
     */
    runTag?: string;
    /**
     * The meeting's language code (the Amazon Transcribe section's language),
     * which a row in that language takes as its LanguageCode; see
     * microvmRowLanguageCode.
     */
    meetingLanguageCode?: string;
}

/** One known voice and the name to label it with. */
export interface EnrolledVoice {
    label: string;
    vector: number[];
    /**
     * How many observations the seed counts as. Anchors the identity so one
     * mis-assigned utterance cannot move an enrolled centroid as far as the
     * whole enrollment did, while still letting it adapt to this meeting's room
     * and microphone.
     */
    weight?: number;
}

/**
 * Cap on seeds sent to one session.
 *
 * A guard against a configuration or a query returning more than a meeting
 * plausibly has, not a product limit: a meeting with more enrolled attendees
 * than this is better served by the transcript miner, and session setup should
 * not become unbounded work.
 */
export const MAX_ENROLLED_VOICES = 32;

export interface AsrSessionSet {
    lease: AsrLease;
    deinterleaver: StereoDeinterleaver;
    sessions: Map<AsrChannelId, AsrChannelSession>;
    /**
     * True while the MicroVM is being acquired and its sessions started. The
     * meeting's audio stream then holds the same audio for an Amazon Transcribe
     * fallback, and is bounded (trimHeldFallbackAudio).
     */
    starting?: boolean;
}

/** Seconds of audio held while the MicroVM starts: the per-channel backlog cap, as a duration. */
const ASR_MAX_PENDING_SECONDS = ASR_MAX_PENDING_BYTES / (ASR_SAMPLE_RATE * 2);

/**
 * Bound the audio the meeting's stream holds for an Amazon Transcribe fallback
 * while its MicroVM is acquired, at the same duration as the MicroVM sessions'
 * own backlog. Past it the OLDEST audio is dropped, for the reason the backlog
 * drops its oldest (AsrChannelSession.send): what is kept is one unbroken
 * stretch ending now, and the bytes dropped (heldAudioDroppedBytes) put the
 * fallback's clock at its start. Returns the bytes dropped by this call.
 */
export const trimHeldFallbackAudio = (
    socketData: SocketCallData,
    server: FastifyInstance,
    maxSeconds: number = ASR_MAX_PENDING_SECONDS
): number => {
    const source = socketData.audioInputStream;
    if (!source || source.destroyed) {
        return 0;
    }
    const rate = socketData.callMetadata.samplingRate || ASR_SAMPLE_RATE;
    // The meeting stream is always two channels of 16-bit PCM.
    const capBytes = Math.round(maxSeconds * rate) * 4;
    const writable = (source as unknown as { writableLength?: number }).writableLength ?? 0;
    let excess = source.readableLength + writable - capBytes;
    let dropped = 0;
    // Only the readable side can be taken from; what waits on the writable side
    // moves across as it is read, so a later frame's call takes the rest.
    while (excess > 0 && source.readableLength > 0) {
        // Whole stereo frames, so the stream keeps its sample alignment.
        const want = Math.min(source.readableLength, Math.ceil(excess / 4) * 4);
        const chunk = source.read(want) as Buffer | null;
        if (!chunk) {
            break;
        }
        dropped += chunk.length;
        excess -= chunk.length;
    }
    if (dropped > 0) {
        if (!socketData.heldAudioDroppedBytes) {
            server.log.warn(
                `[ASR]: [${socketData.callMetadata.callId}] - The MicroVM is taking longer than ${maxSeconds.toFixed(0)}s to start; ` +
                    'the oldest audio held for it is being dropped (it stays in the recording).'
            );
        }
        socketData.heldAudioDroppedBytes = (socketData.heldAudioDroppedBytes ?? 0) + dropped;
    }
    return dropped;
};

/**
 * How long finishing a session waits for the engine's termination after eos.
 *
 * On eos the engine decodes whatever audio it has not yet reached, then closes
 * the utterance still open and sends its final; that final is the only row the
 * open utterance gets. Its cost grows with the audio not yet finalised -- an
 * utterance that has run for twenty seconds, or a backlog sent when the session
 * opened -- and on a busy MicroVM it has taken well over five seconds, so a
 * fixed grace cut the final off. The wait is the usual grace plus
 * `perOpenSecondMs` for every second of audio not yet finalised, up to
 * `maxMs`. An engine that has caught up answers within a fraction of a second,
 * so the bound only matters when it has not.
 */
export const finishWaitMs = (
    unfinalisedSeconds: number,
    baseMs: number = ASR_FINISH_TIMEOUT_MS,
    perOpenSecondMs: number = ASR_FINISH_MS_PER_OPEN_SECOND,
    maxMs: number = ASR_FINISH_MAX_WAIT_MS
): number =>
    Math.min(Math.max(baseMs, maxMs), baseMs + Math.max(0, unfinalisedSeconds) * perOpenSecondMs);

/** The usual grace alone: for a session being discarded, or a task shutting down. */
export const ASR_FINISH_GRACE_MS = ASR_FINISH_TIMEOUT_MS;

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
    /** Base language (`es`) of a final's text; absent or null when the engine names none. */
    language?: string | null;
    code?: string;
    message?: string;
    audio_seconds?: number;
    segments?: number;
    effective_config?: { diarize?: boolean; sample_rate?: number };
}

/**
 * The seeds to put on the wire: valid, deduplicated and bounded.
 *
 * A malformed seed is dropped rather than failing the session -- losing a name
 * is a far smaller harm than losing the transcript, and the engine takes the
 * same view. Two enrollments with the same display name are both kept: dropping
 * one would silently discard somebody's enrollment, and the engine keeps them
 * apart by their vectors regardless of what they are called.
 */
export const enrolledVoicesForConfig = (voices?: EnrolledVoice[]): EnrolledVoice[] => {
    if (!voices || voices.length === 0) {
        return [];
    }
    const usable = voices.filter(
        (voice) =>
            typeof voice?.label === 'string' &&
            voice.label.trim().length > 0 &&
            Array.isArray(voice.vector) &&
            voice.vector.length > 0 &&
            voice.vector.every((value) => Number.isFinite(value))
    );
    // Dimensions must agree, or the engine is comparing vectors a different
    // model produced. The majority dimension wins, since a single odd seed is
    // far likelier to be the stale one.
    const counts = new Map<number, number>();
    for (const voice of usable) {
        counts.set(voice.vector.length, (counts.get(voice.vector.length) ?? 0) + 1);
    }
    let dimension = 0;
    let best = 0;
    for (const [size, count] of counts) {
        if (count > best) {
            best = count;
            dimension = size;
        }
    }
    return usable
        .filter((voice) => voice.vector.length === dimension)
        .slice(0, MAX_ENROLLED_VOICES)
        .map((voice) => ({
            label: voice.label.trim(),
            vector: voice.vector,
            weight: voice.weight && voice.weight > 0 ? voice.weight : 8,
        }));
};

export const isMicrovmAsrConfigured = (): boolean =>
    ASR_LAUNCHER_FUNCTION_ARN.length > 0 || ASR_DIRECT_ENDPOINT.length > 0;

/** Whether a meeting that cannot start the MicroVM engine falls back to Amazon Transcribe. */
export const shouldFallbackToTranscribe = (): boolean =>
    (process.env['ASR_FALLBACK_TO_TRANSCRIBE'] || 'true') === 'true';

/**
 * How many speakers to expect on one channel.
 *
 * A client-supplied count wins, because only the person in the meeting knows how
 * many people share their microphone. But a FALSY client value means "no opinion",
 * not "unbounded": `0` is the wire representation of a blank field, and treating it
 * as an explicit choice (which `??` does, since 0 is not nullish) let every Stream
 * Audio meeting silently override a cap an admin had set, so the cap never applied.
 *
 * The cost is that a client can no longer demand "no cap" against a deployment that
 * set one — it gets the deployment's value instead. That is the lesser problem: a
 * cap is a deliberate act by an admin, and 0 is what a client sends when it has
 * nothing to say.
 */
export const resolveMaxSpeakers = (
    clientValue: number | undefined,
    deploymentValue: number
): number => clientValue || deploymentValue;

/**
 * Which engine transcribes this meeting.
 *
 * The engine is the deployment's choice (the Configuration page's Transcription Engine section) and diarization the client's;
 * a client may still name an engine (--asr-engine). Unservable requests fall back.
 *
 * A meeting whose Amazon Transcribe options use one the ASR MicroVM engine does
 * not offer (content redaction, a custom vocabulary or language model, language
 * identification, a language the deployed bundle does not transcribe) stays on Amazon Transcribe, so the
 * option is applied rather than dropped. The bundle's languages are ASR_MODEL_LANGUAGES, the ASR stack's
 * AsrModelLanguages output; without it only English is taken. That holds for a client that named the MicroVM engine too: those
 * options are the deployment's, set by an administrator, and a client's engine
 * request does not override them.
 */
export const resolveAsrEngine = (
    callMetaData: CallMetaData,
    server?: FastifyInstance,
    runtime?: AsrRuntimeConfig,
    transcription?: TranscriptionSettings,
    bundleLanguages: readonly string[] = parseBundleLanguages(process.env['ASR_MODEL_LANGUAGES'])
): AsrEngineName => {
    const deploymentDefault = runtime
        ? (runtime.streamingEngineMicrovm ? 'microvm' : 'transcribe')
        : ASR_ENGINE_DEFAULT;
    const requested = callMetaData.asrEngine?.toLowerCase() || deploymentDefault;
    if (requested !== 'microvm') {
        return 'transcribe';
    }
    // Info for a meeting that took the deployment's default; a client that
    // named the engine itself is told louder.
    const report = (message: string): void => {
        if (callMetaData.asrEngine) {
            server?.log.warn(message);
        } else {
            server?.log.info(message);
        }
    };
    if (!isMicrovmAsrConfigured()) {
        // A switch or client asked for the MicroVM engine on a deployment
        // without it (EnableMicrovmAsr=false, or a Region without Lambda
        // MicroVMs): the expected fallback.
        report(
            `[ASR]: [${callMetaData.callId}] - MicroVM ASR was requested but this deployment has no ASR launcher configured; using Amazon Transcribe.`
        );
        return 'transcribe';
    }
    const transcribeOnly = transcription ? transcribeOnlyOptions(transcription, bundleLanguages) : [];
    if (transcribeOnly.length > 0) {
        report(
            `[ASR]: [${callMetaData.callId}] - MicroVM ASR was requested, but this meeting's Amazon Transcribe options use ` +
                `${transcribeOnly.join(', ')}, which the MicroVM engine does not offer; using Amazon Transcribe.`
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
        acquiredAtMs: started,
        memoryMiB: Number(body['memoryMiB']) > 0 ? Number(body['memoryMiB']) : undefined,
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
 * the same approach the Amazon Transcribe path takes for its reconnects. The
 * offset is the duration of the audio this channel has handed on so far (sent
 * on an earlier connection, or dropped while disconnected), which is where the
 * next connection's audio sits in the meeting's audio recording.
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

    /**
     * Bytes of this channel's audio no later connection will hear: sent on a
     * connection, or dropped from the backlog. Read as the new connection's
     * time offset when it opens.
     */
    private consumedBytes = 0;

    private readonly runTag: string;

    private attempt = 0;

    private readyAt = 0;

    private resampler: ChannelResampler;

    private writes: Promise<void> = Promise.resolve();

    private readyResolve: ((ready: boolean) => void) | null = null;

    private terminationResolve: (() => void) | null = null;

    private diarizeEffective = false;

    /**
     * Engine time (on the current connection's own clock) up to which the
     * engine has sent finals. Audio past it is still in an open utterance, or
     * not yet decoded; finishing the session waits for it (finishWaitMs).
     */
    private finalisedThroughSeconds = 0;

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
        this.runTag = options.runTag ?? Date.now().toString(36);
        // Audio already in the recording when the session is created (what the
        // client sent while START was handled, or while the engine was chosen)
        // never reaches this engine, so the first connection's clock starts
        // after it rather than at 0. Counted at this engine's rate and width.
        this.consumedBytes = Math.round(recordedAudioSeconds(socketData) * ASR_SAMPLE_RATE) * 2;
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
            // Counted whether or not the send succeeds: either way no later
            // connection hears these bytes, and they are in the recording.
            this.consumedBytes += frame.length;
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
        // lost while the MicroVM boots. Past the cap the OLDEST audio goes, so
        // what is kept is one unbroken stretch ending now: the next connection
        // then starts at a known point (consumedBytes) on the recording's clock.
        // Dropping the newest instead would leave a hole in the middle of what
        // the engine hears, and every row after it would sit too early.
        this.pending.push(frame);
        this.pendingBytes += frame.length;
        while (this.pendingBytes > ASR_MAX_PENDING_BYTES && this.pending.length > 1) {
            const dropped = this.pending.shift() as Buffer;
            if (this.droppedBytes === 0) {
                this.server.log.warn(
                    `[ASR]: [${this.callId}][${this.channelId}] - more than ${ASR_MAX_PENDING_SECONDS.toFixed(0)}s of audio is waiting for the engine; dropping the oldest.`
                );
            }
            this.pendingBytes -= dropped.length;
            this.droppedBytes += dropped.length;
            this.consumedBytes += dropped.length;
        }
    }

    /** Seconds of this connection's audio past the engine's last final. */
    private get unfinalisedSeconds(): number {
        const sentSeconds = this.consumedBytes / (ASR_SAMPLE_RATE * 2) - this.timeOffsetSeconds;
        return Math.max(0, sentSeconds - this.finalisedThroughSeconds);
    }

    /**
     * Send eos and wait for the engine's termination, which comes after the
     * final of every utterance still open, then close. Those finals are written
     * before this resolves. `maxWaitMs` overrides the wait (finishWaitMs), for a
     * session whose rows are not needed or a task that is shutting down.
     */
    async finish(options: { maxWaitMs?: number } = {}): Promise<void> {
        if (this.finished) {
            return;
        }
        this.finished = true;
        this.flushOutbound();
        if (this.open && this.ws?.readyState === WebSocket.OPEN) {
            const terminated = new Promise<'terminated'>((resolve) => {
                this.terminationResolve = () => resolve('terminated');
            });
            // The engine still has to decode what it has not reached and close
            // the open utterance; how long that takes grows with the audio not
            // yet finalised. A fixed grace cut off the final of a meeting's
            // last utterance, which is often its only row on a short stream.
            const unfinalised = this.unfinalisedSeconds;
            const waitMs = options.maxWaitMs ?? finishWaitMs(unfinalised);
            const startedAt = Date.now();
            let timer: NodeJS.Timeout | undefined;
            try {
                this.ws.send(JSON.stringify({ type: 'eos' }));
                const outcome = await Promise.race([
                    terminated,
                    new Promise<'timeout'>((resolve) => {
                        timer = setTimeout(() => resolve('timeout'), waitMs);
                    }),
                ]);
                clearTimeout(timer);
                if (outcome === 'timeout') {
                    this.server.log.warn(
                        `[ASR]: [${this.callId}][${this.channelId}] - the engine had not finished ${waitMs} ms after the end of the stream (${unfinalised.toFixed(1)}s of audio not yet finalised); closing without its last rows.`
                    );
                } else {
                    this.server.log.info(
                        `[ASR]: [${this.callId}][${this.channelId}] - engine finished ${Date.now() - startedAt} ms after the end of the stream (${unfinalised.toFixed(1)}s of audio had not been finalised).`
                    );
                }
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
            // This connection's clock starts at the first byte it is sent: the
            // backlog below, then live audio.
            this.timeOffsetSeconds = this.consumedBytes / (ASR_SAMPLE_RATE * 2);
            const config = {
                type: 'config',
                sample_rate: ASR_SAMPLE_RATE,
                encoding: 'pcm_s16le',
                channels: 1,
                interim_results: true,
                word_timestamps: false,
                diarize: this.options.diarize,
                max_speakers: this.options.maxSpeakers,
                // Voices this meeting is allowed to recognise by name. Omitted
                // entirely when there are none, so a deployment without voice
                // enrollment sends exactly the config it sent before.
                ...(enrolledVoicesForConfig(this.options.enrolledVoices).length > 0
                    ? { enrolled_voices: enrolledVoicesForConfig(this.options.enrolledVoices) }
                    : {}),
                // Nothing else. The threshold, utterance floor and turn-cut
                // behaviour are the bundle's calibrated operating point baked
                // into the image; omitting them here is what keeps that in force.
            };
            // The backlog is this connection's, sent or not, from here on.
            this.consumedBytes += this.pendingBytes;
            this.finalisedThroughSeconds = 0;
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
            if (this.finished) {
                // Nothing more can arrive: a finish waiting for termination
                // stops waiting rather than running out its bound.
                this.terminationResolve?.();
                this.terminationResolve = null;
                return;
            }
            if (this.socketData.ended) {
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
        if (this.readyAt && Date.now() - this.readyAt >= ASR_HEALTHY_SESSION_MS) {
            this.attempt = 0;
        }
        this.readyAt = 0;
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

        // Segment numbering and the engine clock restart on a new connection;
        // the clock's new offset is set when it opens (see connect()).
        this.generation += 1;

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
                // Deliberately NOT this.attempt = 0 here: an engine that dies right
                // after ready would then retry every backoff interval forever. The
                // counter is forgiven in scheduleReconnect only once the session has
                // stayed up for ASR_HEALTHY_SESSION_MS, matching the Transcribe path.
                this.readyAt = Date.now();
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
            case 'final':
                this.finalisedThroughSeconds = Math.max(
                    this.finalisedThroughSeconds,
                    message.end ?? message.start ?? 0
                );
                this.queueSegment(message);
                break;
            case 'partial':
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
        // SegmentId deliberately excludes the speaker: a partial's provisional
        // speaker can be corrected by its final, and both must land on one row.
        const segmentId = `${this.channelId}-${this.runTag}-g${this.generation}-s${message.segment ?? 0}`;
        // Only a final names a language; a partial's row gets it when its final lands.
        const languageCode = microvmRowLanguageCode(
            isPartial ? undefined : message.language,
            this.options.meetingLanguageCode ?? ''
        );
        // The meeting audio is named after who the page showed during the row,
        // or the attendee its voice has been proven to (speaker-fusion.ts); the
        // microphone is the user's own, and the page says nothing about it.
        const speaker =
            this.channelId === CHANNEL_SYSTEM
                ? pageSpeakersFor(this.socketData.callMetadata).nameRow({
                    session: `${this.runTag}-g${this.generation}`,
                    label: message.speaker,
                    startTime: start,
                    endTime: end,
                    isPartial,
                    fallbackName: this.channelName(),
                    rowId: segmentId,
                }).speaker
                : this.registry.nameFor(this.channelId, message.speaker, this.channelName());

        this.writes = this.writes
            .then(async () => {
                await this.writeSegment(
                    {
                        SegmentId: segmentId,
                        Channel: channelToTranscriptChannel(this.channelId),
                        StartTime: start,
                        EndTime: end,
                        Transcript: text,
                        IsPartial: isPartial,
                        Speaker: speaker,
                        ...(languageCode ? { LanguageCode: languageCode } : {}),
                    },
                    this.socketData.callMetadata,
                    this.server
                );
            })
            // Without a per-link catch, one rejection leaves the chain in a
            // rejected state and every later .then() is skipped - the channel
            // would silently stop writing segments for the rest of the meeting.
            .catch((error) => {
                this.server.log.error(
                    `[ASR]: [${this.callId}][${this.channelId}] - segment write failed: ${normalizeErrorForLogging(error)}`
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
 * A meeting that ENDS while the MicroVM is acquired is still transcribed: the
 * end of a meeting waits for this to settle (engine-start.ts), the sessions are
 * started as usual and sent the audio buffered meanwhile, and then finished at
 * once, so the rows land before the meeting's END event is written. Only an
 * explicit stopMicrovmAsr during the acquisition (a capture discarded for
 * consent, a task shutting down, or that wait running out) abandons it.
 *
 * Returns false if the engine could not be started, so the caller can fall back
 * to Amazon Transcribe for this meeting instead of losing its transcript.
 */
export const startMicrovmAsr = async (
    socketData: SocketCallData,
    server: FastifyInstance,
    acquire: (callId: string, server: FastifyInstance) => Promise<AsrLease | undefined> = acquireLease
): Promise<boolean> => {
    const callMetadata = socketData.callMetadata;

    // Unlike Amazon Transcribe, whose ShowSpeakerLabel is stream-level, this engine
    // runs an independent session per channel — so "diarize the tab but not the
    // microphone" is honoured for real rather than by discarding half the labels.
    // The deployment default beneath the client's choice is the Amazon Transcribe
    // section's speaker-partitioning setting, read as Amazon Transcribe reads it.
    const { transcription } = socketData.sessionSettings ?? (await getSessionSettings(server.log));
    const settings = diarizationSettingsFor(callMetadata, transcription.showSpeakerLabel);
    // Only the person in the meeting knows how many people share their microphone,
    // so the client's count is the only cap; blank means discover.
    const optionsFor = (channelId: AsrChannelId): AsrSessionOptions => ({
        diarize: diarizationEnabledFor(channelId, settings),
        maxSpeakers: resolveMaxSpeakers(callMetadata.maxSpeakers, 0),
        meetingLanguageCode: transcription.languageCode,
    });
    // Started here and awaited after the lease, so asking who has enrolled runs
    // alongside the MicroVM boot instead of adding to it. Resolves to an empty
    // list when enrollment is off, and on any failure.
    const enrolledVoices = fetchEnrolledVoices(callMetadata.callId, server);
    const registry = new SpeakerNameRegistry();
    const options = new Map<AsrChannelId, AsrSessionOptions>();
    const sessions = new Map<AsrChannelId, AsrChannelSession>();
    // The endpoint is filled in by updateLease() once the MicroVM is running; the
    // sessions only need it to connect, and until then they buffer.
    const pendingLease: AsrLease = { endpointUrl: '' };
    for (const channelId of ASR_CHANNEL_IDS) {
        // Held in the map rather than rebuilt per use, so the enrolled voices
        // filled in below reach the object the session actually reads.
        options.set(channelId, optionsFor(channelId));
        sessions.set(
            channelId,
            new AsrChannelSession(
                server,
                socketData,
                channelId,
                pendingLease,
                registry,
                options.get(channelId) as AsrSessionOptions
            )
        );
    }
    const asrSet: AsrSessionSet = {
        lease: pendingLease,
        deinterleaver: new StereoDeinterleaver(),
        sessions,
        starting: true,
    };
    socketData.asr = asrSet;
    try {
        return await startAcquiredSessions(socketData, server, asrSet, acquire, enrolledVoices, options);
    } finally {
        asrSet.starting = false;
    }
};

const startAcquiredSessions = async (
    socketData: SocketCallData,
    server: FastifyInstance,
    asrSet: AsrSessionSet,
    acquire: (callId: string, server: FastifyInstance) => Promise<AsrLease | undefined>,
    enrolledVoices: Promise<EnrolledVoice[]>,
    options: Map<AsrChannelId, AsrSessionOptions>
): Promise<boolean> => {
    const callMetadata = socketData.callMetadata;
    const sessions = asrSet.sessions;
    const lease = await acquire(callMetadata.callId, server);
    if (!lease) {
        if (socketData.asr === asrSet) {
            socketData.asr = undefined;
        }
        return false;
    }
    // stopMicrovmAsr can run while the acquire is in flight (a MicroVM boot
    // takes seconds) and clear socketData.asr, with nothing to release because
    // the pending lease carries no microvmId yet. Writing through the cleared
    // reference would throw, and the freshly acquired MicroVM would run
    // unreleased until its max-duration backstop.
    if (socketData.asr !== asrSet) {
        server.log.warn(
            `[ASR]: [${callMetadata.callId}] - the MicroVM ASR engine was stopped during MicroVM acquisition; releasing ${lease.microvmId}.`
        );
        if (lease.microvmId) {
            await releaseLease(lease.microvmId, server);
            await meterLease(callMetadata.callId, lease, server);
        }
        return false;
    }
    asrSet.lease = lease;
    for (const session of sessions.values()) {
        session.updateLease(lease);
    }

    // Both channels get the same seeds: somebody who enrolled may be on the
    // microphone or in the meeting audio, and each channel is an independent
    // session with its own view of who is speaking. Set before start(), which
    // is when the config is sent.
    const voices = await enrolledVoices;
    if (voices.length > 0) {
        for (const channelOptions of options.values()) {
            channelOptions.enrolledVoices = voices;
        }
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
        .filter(([channelId]) => options.get(channelId)?.diarize)
        .map(([channelId]) => channelId);
    server.log.info(
        `[ASR]: [${callMetadata.callId}] - MicroVM ASR active on ${sessions.size} channel(s), client rate ${callMetadata.samplingRate}Hz, diarized channel(s)=${diarized.length ? diarized.join(',') : 'none'}`
    );
    if (socketData.ended) {
        // The meeting ended while the MicroVM started: the sessions were sent
        // its audio as they opened. Finish them now, so the tail is flushed and
        // the MicroVM released before the meeting's end goes on.
        server.log.info(
            `[ASR]: [${callMetadata.callId}] - Call ended while the MicroVM was being acquired; finishing the audio sent to it.`
        );
        await stopMicrovmAsr(socketData, server);
    }
    return true;
};

export const pushAsrAudio = (socketData: SocketCallData, data: Uint8Array, server?: FastifyInstance): void => {
    const asr = socketData.asr;
    if (!asr) {
        return;
    }
    const split = asr.deinterleaver.split(data);
    for (const [channelId, session] of asr.sessions) {
        session.pushPcm(split[channelId]);
    }
    if (asr.starting && server) {
        trimHeldFallbackAudio(socketData, server);
    }
};

/**
 * Finish every channel's session -- each waits for the engine's last finals and
 * writes them -- then release the MicroVM. `options.finishWaitMs` caps each
 * session's wait (AsrChannelSession.finish), for a capture being discarded or a
 * task shutting down.
 */
export const stopMicrovmAsr = async (
    socketData: SocketCallData,
    server: FastifyInstance,
    options: { finishWaitMs?: number } = {}
): Promise<void> => {
    const asr = socketData.asr;
    if (!asr) {
        return;
    }
    socketData.asr = undefined;

    await Promise.all(
        [...asr.sessions.values()].map(async (session) => {
            try {
                await session.finish({ maxWaitMs: options.finishWaitMs });
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
        await meterLease(socketData.callMetadata.callId, asr.lease, server);
    }
};
