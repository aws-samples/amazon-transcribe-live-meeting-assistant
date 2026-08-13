/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Run a calibration against a meeting this deployment already recorded.
 *
 * The pure half of calibration lives in asr-calibration.ts; this is the part that
 * touches the world: stream the recording out of S3, split it into stretches where
 * one channel clearly dominates, embed those on an ASR MicroVM in embed mode, and
 * hand the vectors to deriveOperatingPoint().
 *
 * It runs where the audio and the engine already are — the transcriber task — so
 * calibration reuses the recording bucket, the MicroVM launcher and the WebSocket
 * client rather than duplicating all three in a Lambda.
 *
 * Nothing is written anywhere: a run proposes an operating point and the admin
 * decides. That keeps a bad recording (one speaker, a phone bridge, heavy
 * cross-talk) from silently reconfiguring everyone's meetings.
 */
import { FastifyInstance } from 'fastify';
import WebSocket from 'ws';
import { randomUUID } from 'crypto';
import { S3Client, GetObjectCommand } from '@aws-sdk/client-s3';
import { Readable } from 'stream';

import { AsrLease, acquireLease, releaseLease, subprotocols } from './asr-microvm';
import {
    CALIBRATION_SAMPLE_RATE,
    CalibrationResult,
    CalibrationSegment,
    EmbeddedSegment,
    WavFormat,
    collectCalibrationSegments,
    deriveOperatingPoint,
    parseWavHeader,
    segmentDurationSec,
} from './asr-calibration';
import { ChannelResampler, StereoDeinterleaver } from './asr-audio';
import { normalizeErrorForLogging, posixifyFilename } from '../utils/common';

const AWS_REGION = process.env['AWS_REGION'] || 'us-east-1';
const RECORDINGS_BUCKET_NAME = process.env['RECORDINGS_BUCKET_NAME'] || '';
const RECORDING_FILE_PREFIX =
    process.env['RECORDINGS_KEY_PREFIX'] ||
    process.env['RECORDING_FILE_PREFIX'] ||
    'lma-audio-recordings/';
// How much of a recording to read. Twenty minutes is far more speech than the
// statistics need, and it bounds the memory this holds (16 kHz mono per channel,
// so ~38 MB each) on a 1 GB task that is also transcribing live meetings.
const MAX_MINUTES = parseFloat(process.env['ASR_CALIBRATION_MAX_MINUTES'] || '20');
// Segments embedded per channel. Pairs grow with the square of this, so 12 already
// gives 66 same-speaker and 144 cross-speaker comparisons.
const MAX_SEGMENTS_PER_CHANNEL = parseInt(
    process.env['ASR_CALIBRATION_MAX_SEGMENTS'] || '12',
    10
);
// Fewer than this on a channel is not a sample, and launching a MicroVM to embed
// it would cost more than the answer is worth.
const MIN_SEGMENTS_PER_CHANNEL = 3;
const EMBED_TIMEOUT_MS = parseInt(process.env['ASR_CALIBRATION_TIMEOUT_MS'] || '180000', 10);
// A header this long means the object is not a WAV we can read.
const MAX_HEADER_BYTES = 64 * 1024;

const s3Client = new S3Client({ region: AWS_REGION });

/** Carries the HTTP status the route should return, so the reason survives. */
export class CalibrationError extends Error {
    constructor(
        message: string,
        readonly status: number
    ) {
        super(message);
        this.name = 'CalibrationError';
    }
}

export interface CalibrationRequest {
    callId?: string;
    recordingKey?: string;
    maxSegmentsPerChannel?: number;
}

export interface CalibrationRun {
    recordingKey: string;
    sourceSampleRate: number;
    audioSecondsAnalysed: number;
    segmentsFound: number;
    segmentsEmbedded: number;
    embeddingDim: number;
    result: CalibrationResult;
}

export interface CalibrationDeps {
    fetchRecording: (key: string) => Promise<AsyncIterable<Uint8Array>>;
    acquire: (id: string, server: FastifyInstance) => Promise<AsrLease | undefined>;
    release: (microvmId: string, server: FastifyInstance) => Promise<void>;
    embed: (
        lease: AsrLease,
        segments: CalibrationSegment[],
        server: FastifyInstance
    ) => Promise<number[][]>;
}

interface ChannelAudio {
    format: WavFormat;
    channel0: Buffer;
    channel1: Buffer;
    secondsRead: number;
    truncated: boolean;
}

/**
 * Decode a stereo recording into two 16 kHz mono channels.
 *
 * Streamed rather than buffered whole: a 48 kHz stereo hour is 690 MB on the wire
 * and this task has 1 GB for everything it does. Only the resampled channels are
 * kept, and only up to the time cap.
 */
export const readChannels = async (
    stream: AsyncIterable<Uint8Array>,
    maxSeconds: number
): Promise<ChannelAudio> => {
    const maxOutputBytes = Math.max(1, Math.floor(maxSeconds * CALIBRATION_SAMPLE_RATE * 2));
    const deinterleaver = new StereoDeinterleaver();
    let resampler: ChannelResampler | undefined;
    let resamplerOther: ChannelResampler | undefined;
    let format: WavFormat | undefined;
    let header = Buffer.alloc(0);
    const parts0: Buffer[] = [];
    const parts1: Buffer[] = [];
    let outputBytes = 0;
    let truncated = false;

    for await (const chunk of stream) {
        let pcm = Buffer.from(chunk.buffer, chunk.byteOffset, chunk.byteLength);

        if (!format) {
            header = Buffer.concat([header, pcm]);
            format = parseWavHeader(header);
            if (!format) {
                if (header.length > MAX_HEADER_BYTES) {
                    throw new CalibrationError(
                        'that object does not look like a WAV recording',
                        400
                    );
                }
                continue;
            }
            if (format.channels !== 2) {
                throw new CalibrationError(
                    'calibration needs the two-channel recording (microphone and meeting audio); ' +
                        `this one has ${format.channels}. Channel identity is what tells the two ` +
                        'speakers apart, so a mono recording carries no ground truth.',
                    400
                );
            }
            resampler = new ChannelResampler(format.sampleRate);
            resamplerOther = new ChannelResampler(format.sampleRate);
            pcm = header.subarray(format.dataOffset);
        }

        const split = deinterleaver.split(pcm);
        const out0 = resampler!.process(split.ch_0);
        const out1 = resamplerOther!.process(split.ch_1);
        if (out0.length > 0) {
            parts0.push(Buffer.from(out0));
            parts1.push(Buffer.from(out1));
            outputBytes += out0.length;
        }
        if (outputBytes >= maxOutputBytes) {
            truncated = true;
            break;
        }
    }

    if (!format) {
        throw new CalibrationError('that recording is empty or not a WAV file', 400);
    }

    return {
        format,
        channel0: Buffer.concat(parts0),
        channel1: Buffer.concat(parts1),
        secondsRead: outputBytes / (CALIBRATION_SAMPLE_RATE * 2),
        truncated,
    };
};

interface EmbedMessage {
    type: string;
    index?: number;
    dim?: number;
    vector?: number[];
    code?: string;
    message?: string;
}

/**
 * Embed each segment on the MicroVM, one at a time.
 *
 * One frame per utterance, and the next only after the previous vector comes back:
 * that keeps the mapping from vector to channel unambiguous (the engine answers in
 * order) and keeps a long meeting from pushing megabytes at an engine that decodes
 * them one at a time anyway.
 */
export const embedSegments = async (
    lease: AsrLease,
    segments: CalibrationSegment[],
    server: FastifyInstance
): Promise<number[][]> =>
    new Promise<number[][]>((resolve, reject) => {
        const vectors: number[][] = [];
        let sent = 0;
        let settled = false;
        let socket: WebSocket;

        const finish = (error?: Error): void => {
            if (settled) {
                return;
            }
            settled = true;
            clearTimeout(timer);
            try {
                socket.removeAllListeners();
                socket.close();
            } catch {
                // already gone
            }
            if (error) {
                reject(error);
            } else {
                resolve(vectors);
            }
        };

        const timer = setTimeout(
            () =>
                finish(
                    new CalibrationError(
                        `the ASR engine embedded ${vectors.length} of ${segments.length} utterances before timing out`,
                        504
                    )
                ),
            EMBED_TIMEOUT_MS
        );

        const sendNext = (): void => {
            if (sent >= segments.length) {
                socket.send(JSON.stringify({ type: 'eos' }));
                return;
            }
            socket.send(segments[sent].pcm);
            sent += 1;
        };

        try {
            socket = new WebSocket(lease.endpointUrl, subprotocols(lease.authToken));
        } catch (error) {
            finish(
                new CalibrationError(
                    `could not reach the ASR engine: ${normalizeErrorForLogging(error)}`,
                    502
                )
            );
            return;
        }

        socket.on('open', () => {
            socket.send(
                JSON.stringify({
                    type: 'config',
                    sample_rate: CALIBRATION_SAMPLE_RATE,
                    encoding: 'pcm_s16le',
                    channels: 1,
                    mode: 'embed',
                    diarize: false,
                })
            );
        });

        socket.on('message', (data: WebSocket.RawData, isBinary: boolean) => {
            if (isBinary) {
                return;
            }
            let message: EmbedMessage;
            try {
                message = JSON.parse(data.toString()) as EmbedMessage;
            } catch {
                return;
            }
            switch (message.type) {
                case 'ready':
                    sendNext();
                    break;
                case 'embedding':
                    vectors.push(message.vector || []);
                    sendNext();
                    break;
                case 'termination':
                    finish();
                    break;
                case 'error':
                    finish(
                        new CalibrationError(
                            `the ASR engine cannot embed audio: ${message.message || message.code}`,
                            503
                        )
                    );
                    break;
                default:
                    break;
            }
        });

        socket.on('error', (error: Error) => {
            server.log.error(
                `[ASR CALIBRATE]: websocket error: ${normalizeErrorForLogging(error)}`
            );
            finish(
                new CalibrationError(
                    `the connection to the ASR engine failed: ${error.message}`,
                    502
                )
            );
        });

        socket.on('close', (code: number) => {
            // Vectors already collected are usable; the statistics say so if there
            // are too few. A close with nothing is a failure worth reporting.
            finish(
                vectors.length > 0
                    ? undefined
                    : new CalibrationError(
                        `the ASR engine closed the session (${code}) before embedding anything`,
                        502
                    )
            );
        });
    });

const defaultDeps: CalibrationDeps = {
    fetchRecording: async (key: string) => {
        if (!RECORDINGS_BUCKET_NAME) {
            throw new CalibrationError(
                'this deployment has no recordings bucket, so there is no audio to calibrate from',
                503
            );
        }
        try {
            const response = await s3Client.send(
                new GetObjectCommand({ Bucket: RECORDINGS_BUCKET_NAME, Key: key })
            );
            if (!response.Body) {
                throw new CalibrationError(`recording ${key} is empty`, 404);
            }
            return response.Body as Readable;
        } catch (error) {
            if (error instanceof CalibrationError) {
                throw error;
            }
            const name = (error as { name?: string }).name;
            if (name === 'NoSuchKey' || name === 'NotFound') {
                throw new CalibrationError(
                    `no recording found at ${key}. Calibration needs a meeting that was recorded ` +
                        '(Stream Audio with recording enabled).',
                    404
                );
            }
            throw new CalibrationError(
                `could not read ${key}: ${normalizeErrorForLogging(error)}`,
                502
            );
        }
    },
    acquire: acquireLease,
    release: releaseLease,
    embed: embedSegments,
};

/**
 * The recording key for a request, restricted to the recordings prefix.
 *
 * The caller is an authenticated admin, but the task role can read the whole
 * bucket, so an arbitrary key would turn this route into a file reader.
 */
export const resolveRecordingKey = (request: CalibrationRequest): string => {
    const explicit = (request.recordingKey || '').trim();
    const callId = (request.callId || '').trim();
    if (!explicit && !callId) {
        throw new CalibrationError('give either a callId or a recordingKey', 400);
    }
    const key = explicit || `${RECORDING_FILE_PREFIX}${posixifyFilename(callId)}.wav`;
    if (key.includes('..') || !key.startsWith(RECORDING_FILE_PREFIX)) {
        throw new CalibrationError(
            `recordingKey must be an object under ${RECORDING_FILE_PREFIX}`,
            400
        );
    }
    if (!key.toLowerCase().endsWith('.wav')) {
        throw new CalibrationError('calibration reads the .wav recording, not the raw stream', 400);
    }
    return key;
};

let running = false;

/**
 * Measure this deployment's diarization operating point from one recorded meeting.
 *
 * Single-flight: a second concurrent run would launch a second MicroVM and compete
 * for the same task's CPU with the meetings being transcribed on it.
 */
export const runCalibration = async (
    request: CalibrationRequest,
    server: FastifyInstance,
    deps: CalibrationDeps = defaultDeps
): Promise<CalibrationRun> => {
    if (running) {
        throw new CalibrationError('a calibration is already running; try again shortly', 409);
    }
    running = true;
    const started = Date.now();
    try {
        const recordingKey = resolveRecordingKey(request);
        server.log.info(`[ASR CALIBRATE]: reading ${recordingKey}`);

        const audio = await readChannels(await deps.fetchRecording(recordingKey), MAX_MINUTES * 60);
        const perChannel = Math.max(
            1,
            Math.min(request.maxSegmentsPerChannel || MAX_SEGMENTS_PER_CHANNEL, 40)
        );
        const segments = collectCalibrationSegments(audio.channel0, audio.channel1, perChannel);
        const onChannel = (channel: string): number =>
            segments.filter((segment) => segment.channel === channel).length;

        server.log.info(
            `[ASR CALIBRATE]: ${recordingKey} - ${audio.secondsRead.toFixed(0)}s of ${
                audio.format.sampleRate
            }Hz audio${audio.truncated ? ' (truncated)' : ''}, ${onChannel('ch_1')} microphone and ${onChannel(
                'ch_0'
            )} meeting-audio utterances`
        );

        const base = {
            recordingKey,
            sourceSampleRate: audio.format.sampleRate,
            audioSecondsAnalysed: Number(audio.secondsRead.toFixed(1)),
            segmentsFound: segments.length,
        };

        // Refuse before spending a MicroVM: a recording where only one side spoke
        // has no different-speaker comparison to make, which is the whole point.
        if (onChannel('ch_0') < MIN_SEGMENTS_PER_CHANNEL || onChannel('ch_1') < MIN_SEGMENTS_PER_CHANNEL) {
            const result = deriveOperatingPoint([]);
            result.notes.push(
                `This recording gave ${onChannel('ch_1')} clear utterance(s) on the microphone and ` +
                    `${onChannel('ch_0')} on the meeting audio; calibration needs at least ` +
                    `${MIN_SEGMENTS_PER_CHANNEL} of each. Use a meeting where both sides spoke ` +
                    'and the two were not talking over each other.'
            );
            return { ...base, segmentsEmbedded: 0, embeddingDim: 0, result };
        }

        const lease = await deps.acquire(`calibrate-${randomUUID()}`, server);
        if (!lease) {
            throw new CalibrationError(
                'could not start an ASR MicroVM to embed the audio; check the launcher function logs',
                503
            );
        }

        let vectors: number[][];
        try {
            vectors = await deps.embed(lease, segments, server);
        } finally {
            if (lease.microvmId) {
                await deps.release(lease.microvmId, server).catch((error) => {
                    server.log.error(
                        `[ASR CALIBRATE]: could not release MicroVM ${lease.microvmId}: ${normalizeErrorForLogging(error)}`
                    );
                });
            }
        }

        // Sliced, not zipped blindly: a vector with no segment behind it has no
        // channel, and guessing one would corrupt the ground truth this rests on.
        const embedded: EmbeddedSegment[] = vectors.slice(0, segments.length).map((vector, index) => ({
            channel: segments[index].channel,
            durationSec: segmentDurationSec(segments[index]),
            vector,
        }));
        const result = deriveOperatingPoint(embedded);
        if (audio.truncated) {
            result.notes.push(
                `Only the first ${MAX_MINUTES} minutes of the recording were analysed.`
            );
        }

        server.log.info(
            `[ASR CALIBRATE]: ${recordingKey} - ${embedded.length} embeddings in ${(
                (Date.now() - started) /
                1000
            ).toFixed(1)}s: threshold=${result.speakerThreshold ?? 'none'} separation=${
                Number.isFinite(result.separation) ? result.separation.toFixed(3) : 'n/a'
            } confidence=${result.confidence}`
        );

        return {
            ...base,
            segmentsEmbedded: embedded.length,
            embeddingDim: embedded[0]?.vector.length || 0,
            result,
        };
    } finally {
        running = false;
    }
};
