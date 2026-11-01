/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * Key frames of the desktop capture app's video: the slides and documents that
 * were on screen, kept when they change and stored on the meeting, as the
 * Virtual Participant does for its own display (docs/meeting-video.md).
 *
 * The video arrives as a fragmented MP4 over the video socket (see
 * videorecording.ts). Each chunk written to disk is also written to the stdin of
 * one FFmpeg process per stream, which decodes only the stream's H.264 key
 * frames -- both desktop encoders emit one at least every two seconds -- and for
 * each writes:
 *
 *   - a 64x36 grayscale sample to stdout, for keyframe-detector.ts;
 *   - a 1568-wide JPEG and a 320-wide thumbnail to a scratch folder;
 *   - a showinfo line on stderr with the frame's presentation time.
 *
 * When the detector keeps a sample, its two JPEGs are uploaded to the
 * recordings bucket under lma-video-keyframes/<posixified CallId>/ and an
 * ADD_VIDEO_KEYFRAME event is written to the call data stream. The other JPEGs
 * are deleted as soon as their sample has been judged.
 *
 * The desktop app gives no screen-share signal, so every sample is judged
 * against the stricter thresholds the Virtual Participant uses where its
 * platform gives none.
 *
 * Times: the stream's presentation times start at zero when the video stream
 * starts, which the client reports as videoTimeOffsetMs after the audio began.
 * The transcript's clock starts with the audio, and the recording is muxed with
 * the video shifted by the same offset, so for the first stream both the
 * frame's Timestamp and its VideoOffset are offset + presentation time. A
 * stream restarted by a fresh encoder is placed on the transcript's clock by
 * wall-clock time instead, and has no VideoOffset, because the mux joins the
 * parts end to end rather than at their wall-clock times.
 *
 * Nothing here may disturb the recording or the audio: every failure stops the
 * key frames for that stream and is logged, and none is thrown to the caller.
 */
import fs from 'fs';
import path from 'path';
import { randomBytes } from 'crypto';
import { spawn, ChildProcess } from 'child_process';
import { FastifyInstance } from 'fastify';
import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import { KeyframeDetector, KeyframeThresholds } from './keyframe-detector';
import { CallVideoKeyframeEvent } from './calleventdata/eventtypes';
import { writeCallEvent } from './calleventdata/transcribe';
import { posixifyFilename, normalizeErrorForLogging } from './utils/common';

const numberEnv = (name: string, fallback: number): number => {
    const raw = process.env[name];
    if (raw === undefined || raw === '') {
        return fallback;
    }
    const parsed = Number(raw);
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
};

const AWS_REGION = process.env['AWS_REGION'] || 'us-east-1';
const AWS_URL_SUFFIX = process.env['AWS_URL_SUFFIX'] || 'amazonaws.com';
const RECORDINGS_BUCKET_NAME = process.env['RECORDINGS_BUCKET_NAME'] || '';
const LOCAL_TEMP_DIR = process.env['LOCAL_TEMP_DIR'] || '/tmp/';
const FFMPEG_PATH = process.env['FFMPEG_PATH'] || 'ffmpeg';
const KEY_PREFIX = process.env['VIDEO_KEYFRAMES_KEY_PREFIX'] || 'lma-video-keyframes/';

export const GRID_WIDTH = 64;
export const GRID_HEIGHT = 36;
export const SAMPLE_BYTES = GRID_WIDTH * GRID_HEIGHT;
const IMAGE_WIDTH = 1568;
const THUMB_WIDTH = 320;

/*
 * The Virtual Participant's thresholds for platforms without a share signal
 * (keyframe-sampler.ts), except the settle count. One sample here is one H.264
 * key frame, about two seconds apart, so two still samples are about four
 * seconds -- close to the Virtual Participant's three at one a second. With
 * three, a recorded test stream lost its first slide while the motion mask was
 * still learning which cells were cameras, and a 15-second slide at the end.
 */
export const DESKTOP_THRESHOLDS: KeyframeThresholds = {
    changeFraction: numberEnv('KEYFRAME_DESKTOP_CHANGE_FRACTION', 0.04),
    stableFraction: numberEnv('KEYFRAME_DESKTOP_STABLE_FRACTION', 0.01),
    settleSamples: numberEnv('KEYFRAME_DESKTOP_SETTLE_SAMPLES', 2),
    maxMovingFraction: numberEnv('KEYFRAME_DESKTOP_MAX_MOVING_FRACTION', 0.25),
};
const PIXEL_DELTA = numberEnv('KEYFRAME_PIXEL_DELTA', 12);
const MIN_INTERVAL_MS = numberEnv('KEYFRAME_MIN_INTERVAL_MS', 4000);
const MAX_PER_STREAM = numberEnv('KEYFRAME_MAX_PER_MEETING', 1000);
// Undecoded bytes allowed to queue on FFmpeg's stdin before the key frames of
// this stream are given up, so a stalled decoder cannot grow the heap.
const MAX_PENDING_INPUT_BYTES = numberEnv('KEYFRAME_MAX_PENDING_INPUT_BYTES', 16 * 1024 * 1024);
const IMAGE_WAIT_MS = 3000;

/** Zero-padded milliseconds, so ids sort in time order, plus a suffix that keeps them unique. */
export const frameIdFor = (timestampSeconds: number, suffix: string = randomBytes(3).toString('hex')): string => {
    const ms = Math.max(0, Math.round(timestampSeconds * 1000));
    return `${String(ms).padStart(10, '0')}-${suffix}`;
};

/** The S3 keys of a frame's two images; meeting deletion removes the folder. */
export const keyframeKeys = (callId: string, frameId: string): { image: string, thumbnail: string } => {
    const base = `${KEY_PREFIX}${posixifyFilename(callId)}/${frameId}`;
    return { image: `${base}.jpg`, thumbnail: `${base}_thumb.jpg` };
};

const SHOWINFO = /\bn:\s*(\d+)\s+pts:\s*-?\d+\s+pts_time:(-?[\d.]+).*?\bs:(\d+)x(\d+)/;

/** The frame number, presentation time and size from one showinfo line, or null. */
export const parseShowinfo = (line: string): { n: number, ptsSeconds: number, width: number, height: number } | null => {
    const m = SHOWINFO.exec(line);
    if (!m) {
        return null;
    }
    return { n: Number(m[1]), ptsSeconds: Number(m[2]), width: Number(m[3]), height: Number(m[4]) };
};

/** A JPEG is complete once it ends with the end-of-image marker. */
const isCompleteJpeg = (b: Buffer): boolean => b.length > 4 && b[b.length - 2] === 0xff && b[b.length - 1] === 0xd9;

export type KeyframeLaneOptions = {
    callId: string,
    /** Transcript-clock seconds at which the stream's presentation time is zero. */
    timelineBaseSeconds: number,
    /** Recording seconds at which the stream's presentation time is zero, or null when unknown. */
    videoOffsetBaseSeconds: number | null,
    tokens: { accessToken?: string, idToken?: string, refreshToken?: string },
};

/** The parts of a lane that touch the outside world, replaceable in tests. */
export type KeyframeLaneIo = {
    readImage: (file: string) => Promise<Buffer | null>,
    upload: (key: string, body: Buffer) => Promise<void>,
    emit: (event: CallVideoKeyframeEvent) => Promise<void>,
    remove: (file: string) => void,
};

const s3Client = new S3Client({ region: AWS_REGION });

const defaultIo = (server: FastifyInstance): KeyframeLaneIo => ({
    readImage: async (file) => {
        const deadline = Date.now() + IMAGE_WAIT_MS;
        // FFmpeg writes the images and the sample of one frame through
        // separate outputs, so the sample can arrive before its image is on
        // disk; wait briefly for a complete file.
        while (Date.now() < deadline) {
            try {
                const b = await fs.promises.readFile(file);
                if (isCompleteJpeg(b)) {
                    return b;
                }
            } catch {
                // not written yet
            }
            await new Promise((r) => setTimeout(r, 100));
        }
        return null;
    },
    upload: async (key, body) => {
        await s3Client.send(new PutObjectCommand({
            Bucket: RECORDINGS_BUCKET_NAME,
            Key: key,
            Body: body,
            ContentType: 'image/jpeg',
        }));
    },
    // Rethrown, so a frame whose event was not written is treated as not saved.
    emit: (event) => writeCallEvent(event, server, { rethrow: true }),
    remove: (file) => {
        fs.promises.unlink(file).catch(() => undefined);
    },
});

export class KeyframeLane {
    readonly dir: string;
    private readonly options: KeyframeLaneOptions;
    private readonly server: FastifyInstance;
    private readonly io: KeyframeLaneIo;
    private readonly detector = new KeyframeDetector({
        sampleBytes: SAMPLE_BYTES,
        pixelDelta: PIXEL_DELTA,
        minIntervalMs: MIN_INTERVAL_MS,
    });
    private proc: ChildProcess | null = null;
    private stdoutPending: Buffer = Buffer.alloc(0);
    private stderrPending = '';
    private readonly samples: Buffer[] = [];
    private readonly frames: Array<{ n: number, ptsSeconds: number, width: number, height: number }> = [];
    private judged = 0;
    /** Samples below this index have had their images removed. */
    private swept = 0;
    /** The index of the sample whose images are being saved, or -1. */
    private savingIndex = -1;
    private emitted = 0;
    private capturing: Promise<void> | null = null;
    private stopped = false;
    private inputEnded = false;
    private exited: Promise<void> = Promise.resolve();

    constructor(options: KeyframeLaneOptions, server: FastifyInstance, io?: KeyframeLaneIo) {
        this.options = options;
        this.server = server;
        this.io = io || defaultIo(server);
        this.dir = path.resolve(
            LOCAL_TEMP_DIR,
            `${posixifyFilename(options.callId)}_keyframes_${randomBytes(3).toString('hex')}`
        );
    }

    /** Key frames announced by this lane. */
    get framesEmitted(): number {
        return this.emitted;
    }

    private log(message: string): void {
        this.server.log.info(`[KEYFRAMES]: [${this.options.callId}] - ${message}`);
    }

    /** Starts the decoder. Returns false (and logs) when it cannot. */
    start(): boolean {
        try {
            fs.mkdirSync(this.dir, { recursive: true });
        } catch (err) {
            this.log(`Key frames off for this stream: no scratch folder (${normalizeErrorForLogging(err)})`);
            return false;
        }
        const args = [
            '-nostdin', '-hide_banner', '-loglevel', 'info',
            // Decode only the H.264 key frames: a picture every ~2 s for a
            // fraction of the decoding cost.
            '-skip_frame', 'nokey',
            '-f', 'mp4', '-i', 'pipe:0',
            '-filter_complex',
            '[0:v]showinfo,split=3[a][b][c];' +
                `[a]scale=${GRID_WIDTH}:${GRID_HEIGHT}:flags=area,format=gray[g];` +
                `[b]scale='min(${IMAGE_WIDTH},iw)':-2[big];` +
                `[c]scale=${THUMB_WIDTH}:-2[small]`,
            '-map', '[g]', '-fps_mode', 'passthrough', '-f', 'rawvideo', 'pipe:1',
            '-map', '[big]', '-fps_mode', 'passthrough', '-q:v', '4', '-f', 'image2', path.join(this.dir, '%06d.jpg'),
            '-map', '[small]', '-fps_mode', 'passthrough', '-q:v', '6', '-f', 'image2', path.join(this.dir, '%06d_t.jpg'),
        ];
        let proc: ChildProcess;
        try {
            proc = spawn(FFMPEG_PATH, args, { stdio: ['pipe', 'pipe', 'pipe'] });
        } catch (err) {
            this.log(`Key frames off for this stream: decoder did not start (${normalizeErrorForLogging(err)})`);
            return false;
        }
        this.proc = proc;
        this.exited = new Promise((resolve) => {
            proc.on('close', (code, signal) => {
                if (!this.stopped && code !== 0) {
                    this.log(`Decoder exited (code=${code}, signal=${signal}); no more key frames for this stream`);
                }
                this.proc = null;
                resolve();
            });
        });
        proc.on('error', (err) => this.log(`Decoder error: ${normalizeErrorForLogging(err)}`));
        // EPIPE once the decoder has gone: the stream simply ends.
        proc.stdin?.on('error', () => undefined);
        proc.stdout?.on('data', (chunk: Buffer) => this.onStdout(chunk));
        proc.stderr?.on('data', (chunk: Buffer) => this.onStderr(chunk.toString()));
        this.log(`Key frame sampling started (timeline base ${this.options.timelineBaseSeconds.toFixed(1)}s)`);
        return true;
    }

    /** Passes one chunk of the fMP4 stream to the decoder. */
    write(data: Uint8Array): void {
        const stdin = this.proc?.stdin;
        if (this.inputEnded || !stdin || stdin.destroyed) {
            return;
        }
        if (stdin.writableLength > MAX_PENDING_INPUT_BYTES) {
            this.log('Decoder is not keeping up with the stream; no more key frames for this stream');
            this.kill();
            return;
        }
        stdin.write(data);
    }

    /** Ends the input, waits for the decoder to finish and for a frame being saved, then removes the scratch folder. */
    async stop(): Promise<void> {
        if (this.stopped || this.inputEnded) {
            return;
        }
        this.inputEnded = true;
        this.proc?.stdin?.end();
        let timer: NodeJS.Timeout | undefined;
        const timeout = new Promise<void>((resolve) => {
            timer = setTimeout(() => {
                this.kill();
                resolve();
            }, 15_000);
        });
        await Promise.race([this.exited, timeout]);
        clearTimeout(timer);
        this.stopped = true;
        if (this.capturing) {
            await this.capturing.catch(() => undefined);
        }
        fs.promises.rm(this.dir, { recursive: true, force: true }).catch(() => undefined);
        this.log(`Key frame sampling stopped after ${this.emitted} key frame(s) from ${this.judged} sample(s)`);
    }

    private kill(): void {
        this.proc?.kill('SIGKILL');
    }

    /** Raw grayscale samples from the decoder; public so tests can drive the lane. */
    onStdout(chunk: Buffer): void {
        this.stdoutPending = this.stdoutPending.length ? Buffer.concat([this.stdoutPending, chunk]) : chunk;
        while (this.stdoutPending.length >= SAMPLE_BYTES) {
            this.samples.push(Buffer.from(this.stdoutPending.subarray(0, SAMPLE_BYTES)));
            this.stdoutPending = this.stdoutPending.subarray(SAMPLE_BYTES);
        }
        this.drain();
    }

    /** Decoder log lines, read for each frame's time and size; public for tests. */
    onStderr(text: string): void {
        this.stderrPending += text;
        const lines = this.stderrPending.split('\n');
        this.stderrPending = lines.pop() || '';
        for (const line of lines) {
            const info = parseShowinfo(line);
            if (info) {
                this.frames.push(info);
            } else if (/error/i.test(line) && !/showinfo/.test(line)) {
                this.server.log.debug(`[KEYFRAMES]: [${this.options.callId}] - FFmpeg: ${line.trim()}`);
            }
        }
        this.drain();
    }

    /** Judges every sample whose time is known, in order. */
    private drain(): void {
        while (this.samples.length && this.frames.length) {
            const sample = this.samples.shift() as Buffer;
            const frame = this.frames.shift() as { n: number, ptsSeconds: number, width: number, height: number };
            this.judge(sample, frame);
        }
    }

    /** The two image files FFmpeg writes for sample `index` (image2 numbers them from 1). */
    private filesFor(index: number): { image: string, thumbnail: string } {
        const stem = path.join(this.dir, String(index + 1).padStart(6, '0'));
        return { image: `${stem}.jpg`, thumbnail: `${stem}_t.jpg` };
    }

    /**
     * Removes the images of every earlier sample except one being saved. The
     * images of a sample can be written after the sample itself has been
     * judged, so removing them only then would miss a late write; sweeping
     * again at each later sample catches it, and keeps the scratch folder to a
     * few files however long the stream runs.
     */
    private sweep(below: number): void {
        for (let i = this.swept; i < below; i++) {
            if (i !== this.savingIndex) {
                const files = this.filesFor(i);
                this.io.remove(files.image);
                this.io.remove(files.thumbnail);
            }
        }
        this.swept = Math.min(below, this.savingIndex === -1 ? below : this.savingIndex);
    }

    private judge(sample: Buffer, frame: { n: number, ptsSeconds: number, width: number, height: number }): void {
        const index = this.judged++;
        this.sweep(index);
        const files = this.filesFor(index);
        const ptsMs = Math.max(0, frame.ptsSeconds * 1000);
        const decision = this.detector.push(sample, ptsMs, DESKTOP_THRESHOLDS);
        const keep = decision.keep && !this.stopped && this.emitted < MAX_PER_STREAM && !this.capturing;
        if (!keep) {
            if (decision.kept) {
                // Kept by the detector but not saved here: give the picture
                // back, so it is judged again rather than counted as captured.
                this.detector.forget(decision.kept);
                if (this.capturing) {
                    this.log('Key frame skipped: the previous one is still being saved');
                }
            }
            this.io.remove(files.image);
            this.io.remove(files.thumbnail);
            return;
        }
        const firstSeen = decision.firstSeenMs / 1000;
        const timestamp = this.options.timelineBaseSeconds + firstSeen;
        const videoOffset = this.options.videoOffsetBaseSeconds === null
            ? null
            : this.options.videoOffsetBaseSeconds + firstSeen;
        const width = Math.min(IMAGE_WIDTH, frame.width);
        const height = frame.width > 0 ? Math.round((width * frame.height) / frame.width / 2) * 2 : 0;
        const kept = decision.kept;
        this.savingIndex = index;
        this.capturing = this.save(files, timestamp, videoOffset, width, height, decision.changeFraction)
            .catch((err) => {
                // Not saved, so not the reference either: a slide still on
                // screen is captured on the next settle rather than lost.
                if (kept) {
                    this.detector.forget(kept);
                }
                this.log(`Key frame not saved: ${normalizeErrorForLogging(err)}`);
            })
            .finally(() => {
                this.io.remove(files.image);
                this.io.remove(files.thumbnail);
                this.savingIndex = -1;
                this.capturing = null;
            });
    }

    private async save(
        files: { image: string, thumbnail: string },
        timestamp: number,
        videoOffset: number | null,
        width: number,
        height: number,
        changeScore: number
    ): Promise<void> {
        const [image, thumbnail] = await Promise.all([this.io.readImage(files.image), this.io.readImage(files.thumbnail)]);
        if (!image || !thumbnail) {
            throw new Error('the decoded images were not written');
        }
        const frameId = frameIdFor(timestamp);
        const keys = keyframeKeys(this.options.callId, frameId);
        await Promise.all([this.io.upload(keys.image, image), this.io.upload(keys.thumbnail, thumbnail)]);
        const url = (key: string) =>
            `https://${RECORDINGS_BUCKET_NAME}.s3.${AWS_REGION}.${AWS_URL_SUFFIX}/${encodeURIComponent(key)}`;
        const round = (v: number) => Math.round(v * 1000) / 1000;
        await this.io.emit({
            EventType: 'ADD_VIDEO_KEYFRAME',
            CallId: this.options.callId,
            FrameId: frameId,
            Timestamp: round(timestamp),
            VideoOffset: videoOffset === null ? null : round(videoOffset),
            ImageUrl: url(keys.image),
            ThumbnailUrl: url(keys.thumbnail),
            Width: width,
            Height: height,
            Source: 'DESKTOP_CAPTURE',
            ShareDetected: null,
            ChangeScore: round(changeScore),
            CreatedAt: new Date().toISOString(),
            AccessToken: this.options.tokens.accessToken,
            IdToken: this.options.tokens.idToken,
            RefreshToken: this.options.tokens.refreshToken,
        });
        this.emitted++;
        this.log(`Key frame ${frameId} at ${timestamp.toFixed(1)}s (change ${(changeScore * 100).toFixed(0)}%)`);
    }
}

/** Whether key frames can be stored at all on this task. */
export const keyframesConfigured = (): boolean => Boolean(RECORDINGS_BUCKET_NAME);
